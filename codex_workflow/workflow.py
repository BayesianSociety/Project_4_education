from __future__ import annotations

from pathlib import Path
from typing import Any
import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess

from .console import Console
from .execution import classify_failure, run_codex_exec
from .filesystem import copy_paths, diff_snapshots, enforce_allowlist, snapshot_tree, write_snapshot
from .models import (
    Planner,
    ProjectBrief,
    RuntimeConfig,
    StageManifest,
    StepResult,
    planner_from_dict,
    to_dict,
    utc_now,
    write_json,
)
from .planner_validation import PlannerValidationError, validate_planner
from .prompts import (
    build_architect_prompt,
    build_context_analyst_prompt,
    build_planner_repair_prompt,
    build_verification_prompt,
    build_worker_prompt,
)


class WorkflowError(RuntimeError):
    pass


class Orchestrator:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.root = Path(config.workspace_root).resolve()
        self.output_root = Path(config.output_root).resolve()
        self.console = Console(verbose=config.verbose)
        self.schemas_dir = self.root / "schemas"
        self.run_id = utc_now().replace(":", "").replace("-", "")
        self.run_root = self.output_root / self.run_id
        self.manifest_dir = self.run_root / "manifests"
        self.logs_dir = self.run_root / "logs"
        self.shared_dir = self.run_root / "shared"
        self.worktree_root = self.root / "tmp" / "worktrees"
        self.context_schema = self.schemas_dir / "context_analysis.schema.json"
        self.plan_schema = self.schemas_dir / "planner.schema.json"
        self.worker_schema = self.schemas_dir / "worker_result.schema.json"
        self.verify_schema = self.schemas_dir / "verification_result.schema.json"
        self.decision_log_path = self.shared_dir / "decision_log.jsonl"

    def log_decision(self, stage: str, decision: str, details: dict[str, Any] | None = None) -> None:
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": utc_now(),
            "stage": stage,
            "decision": decision,
            "details": details or {},
        }
        with self.decision_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def _ensure_scaffold(self) -> None:
        self.console.step("Ensuring canonical asset and runtime directories exist")
        for path in (
            self.root / "public" / "assets" / "backgrounds",
            self.root / "public" / "assets" / "sprites",
            self.root / "design" / "layout_refs",
            self.root / "tmp" / "worktrees",
            self.root / "runs",
        ):
            path.mkdir(parents=True, exist_ok=True)

    def _stage_manifest(self, stage: str, metadata: dict[str, Any]) -> None:
        snapshot = snapshot_tree(self.root)
        snapshot_path = self.manifest_dir / f"{stage}_snapshot.txt"
        manifest_path = self.manifest_dir / f"{stage}_sha256.txt"
        write_snapshot(snapshot_path, snapshot)
        write_snapshot(manifest_path, snapshot)
        manifest = StageManifest(
            stage=stage,
            created_at=utc_now(),
            workspace_root=str(self.root),
            snapshot_path=str(snapshot_path),
            sha256_manifest_path=str(manifest_path),
            metadata=metadata,
        )
        write_json(self.manifest_dir / f"{stage}_manifest.json", to_dict(manifest))

    def _load_project_brief(self) -> ProjectBrief:
        brief_path = (self.root / self.config.project_description).resolve()
        self.console.step(f"Reading project brief in full from {brief_path}")
        content = brief_path.read_text(encoding="utf-8")
        sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        brief = ProjectBrief(path=self.config.project_description, content=content, sha256=sha256)
        write_json(self.shared_dir / "project_brief.json", to_dict(brief))
        return brief

    def _preflight(self, brief: ProjectBrief) -> None:
        self.console.step("Running environment preflight")
        required_commands = ["python3", "git", self.config.codex_bin]
        if "Next.js 16.x only" in brief.content:
            required_commands.extend(["node", "npm"])
        tool_results: dict[str, str] = {}
        for command in required_commands:
            result = shutil.which(command)
            if not result:
                raise WorkflowError(
                    f"Preflight failed: required command {command!r} not found. "
                    "Install the missing tool before running the orchestrator."
                )
            tool_results[command] = result
        git_dir = self.root / ".git"
        if not git_dir.exists():
            raise WorkflowError("Preflight failed: .git directory not found; worktree isolation requires a git repository.")
        if not os.access(git_dir, os.W_OK):
            raise WorkflowError(
                "Preflight failed: .git is not writable, so git worktrees cannot be created. "
                "Run the orchestrator in a writable repository checkout."
            )
        self.log_decision("environment_preflight", "validated_required_tools", tool_results)
        self._stage_manifest("01_environment_preflight", {"tools": tool_results, "brief_sha256": brief.sha256})

    async def _run_readonly_agent(
        self,
        *,
        stage_name: str,
        role: str,
        prompt: str,
        schema_path: Path,
    ) -> dict[str, Any]:
        stage_dir = self.logs_dir / stage_name / role
        outcome = await run_codex_exec(
            codex_bin=self.config.codex_bin,
            prompt=prompt,
            schema_path=schema_path,
            workdir=self.root,
            output_dir=stage_dir,
            model=self.config.model,
            sandbox_mode=self.config.sandbox_mode,
            color=self.config.color,
            timeout_seconds=self.config.timeout_seconds,
            console=self.console,
        )
        stdout_text = outcome.stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr_text = outcome.stderr_path.read_text(encoding="utf-8", errors="replace")
        classification = classify_failure(outcome.returncode, stdout_text, stderr_text)
        if classification != "success":
            raise WorkflowError(f"{role} failed during {stage_name}: {classification}")
        if not isinstance(outcome.parsed_message, dict):
            raise WorkflowError(f"{role} did not return a JSON object")
        write_json(stage_dir / "parsed_message.json", outcome.parsed_message)
        return outcome.parsed_message

    async def _run_worker_task(self, brief: ProjectBrief, planner: Planner, task, semaphore: asyncio.Semaphore) -> StepResult:
        async with semaphore:
            self.console.step(f"Preparing worktree for {task.id}")
            worktree = self.worktree_root / task.id
            logs = self.logs_dir / "06_worker_generation" / task.id
            if worktree.exists():
                shutil.rmtree(worktree)
            subprocess.run(["git", "worktree", "prune"], cwd=self.root, check=False)
            subprocess.run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], cwd=self.root, check=True)
            before = snapshot_tree(worktree)
            result_payload: dict[str, Any] | None = None
            usage: dict[str, Any] = {}
            last_classification = "task_failure"
            max_attempts = 2
            attempt = 0
            try:
                for attempt in range(1, max_attempts + 1):
                    self.console.step(f"Running {task.id} attempt {attempt}")
                    prompt = build_worker_prompt(self.config, brief, planner, task)
                    outcome = await run_codex_exec(
                        codex_bin=self.config.codex_bin,
                        prompt=prompt,
                        schema_path=self.worker_schema,
                        workdir=worktree,
                        output_dir=logs / f"attempt_{attempt}",
                        model=self.config.model,
                        sandbox_mode=self.config.sandbox_mode,
                        color=self.config.color,
                        timeout_seconds=self.config.timeout_seconds,
                        console=self.console,
                    )
                    stdout_text = outcome.stdout_path.read_text(encoding="utf-8", errors="replace")
                    stderr_text = outcome.stderr_path.read_text(encoding="utf-8", errors="replace")
                    last_classification = classify_failure(outcome.returncode, stdout_text, stderr_text)
                    if last_classification == "success" and isinstance(outcome.parsed_message, dict):
                        result_payload = outcome.parsed_message
                        usage = outcome.usage
                        break
                    if last_classification != "retryable_infrastructure":
                        break
                    self.console.step(f"{task.id} hit retryable infrastructure failure; recreating staged workspace")
                    shutil.rmtree(worktree)
                    subprocess.run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], cwd=self.root, check=True)
                    before = snapshot_tree(worktree)
            except Exception:
                subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.root, check=False)
                raise

            if result_payload is None:
                raise WorkflowError(f"{task.id} failed: {last_classification}")

            after = snapshot_tree(worktree)
            diff = diff_snapshots(before, after)
            allowlist = set(task.owned_paths)
            enforce_allowlist(diff, allowlist)
            changed = sorted(set(diff.created + diff.modified))
            copied = copy_paths(worktree, self.root, changed)
            step = StepResult(
                step_id=task.id,
                role=task.role,
                status=result_payload.get("status", "completed"),
                summary=result_payload.get("summary", ""),
                changed_files=changed,
                created_files=diff.created,
                deleted_files=diff.deleted,
                blockers=list(result_payload.get("blockers", [])),
                usage=usage,
                raw_output_path=str(logs),
                message_path=str(logs / f"attempt_{attempt}" / "last_message.json"),
                worktree_path=str(worktree),
                attempt=attempt,
            )
            write_json(logs / "result.json", to_dict(step))
            self.log_decision("worker_generation", "copied_worker_outputs", {"task_id": task.id, "files": copied})
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.root, check=False)
            return step

    async def run(self) -> None:
        self._ensure_scaffold()
        brief = self._load_project_brief()
        self._preflight(brief)

        self.console.step("Stage 2: Context analysis")
        context_analysis = await self._run_readonly_agent(
            stage_name="02_context_analysis",
            role="context_analyst",
            prompt=build_context_analyst_prompt(self.config, brief),
            schema_path=self.context_schema,
        )
        write_json(self.shared_dir / "context_analysis.json", context_analysis)
        self._stage_manifest("02_context_analysis", {"context_analysis": context_analysis})

        self.console.step("Stages 3-5: Planner generation, validation, and repair loop")
        planner_payload = await self._run_readonly_agent(
            stage_name="03_planner_generation",
            role="architect",
            prompt=build_architect_prompt(self.config, brief, json.dumps(context_analysis, indent=2)),
            schema_path=self.plan_schema,
        )
        planner: Planner | None = None
        for attempt in range(1, self.config.planner_repair_attempts + 1):
            try:
                candidate = planner_from_dict(planner_payload)
                validate_planner(candidate)
                planner = candidate
                break
            except PlannerValidationError as exc:
                if attempt >= self.config.planner_repair_attempts:
                    raise WorkflowError(f"Planner validation failed after repair loop: {exc}") from exc
                self.console.step(f"Planner validation failed: {exc}; requesting targeted repair")
                planner_payload = await self._run_readonly_agent(
                    stage_name="05_planner_repair_loop",
                    role=f"architect_repair_{attempt}",
                    prompt=build_planner_repair_prompt(
                        self.config,
                        brief,
                        json.dumps(planner_payload, indent=2),
                        str(exc),
                    ),
                    schema_path=self.plan_schema,
                )
        assert planner is not None
        write_json(self.shared_dir / "planner.json", to_dict(planner))
        self._stage_manifest("04_planner_schema_validation", {"planner": to_dict(planner)})

        self.console.step("Stage 6: Worker generation with bounded concurrency")
        worker_tasks = [task for task in planner.tasks if task.role in {"backend_producer", "frontend_producer"}]
        semaphore = asyncio.Semaphore(self.config.max_concurrency)
        worker_results = await asyncio.gather(
            *(self._run_worker_task(brief, planner, task, semaphore) for task in worker_tasks)
        )
        write_json(self.shared_dir / "worker_results.json", [to_dict(item) for item in worker_results])
        self._stage_manifest("06_worker_generation", {"worker_results": [to_dict(item) for item in worker_results]})

        evidence_paths = sorted({path for item in worker_results for path in item.changed_files + item.created_files})
        self.console.step("Stages 7-9: Artifact, build, and runtime validation")
        verification = await self._run_readonly_agent(
            stage_name="07_08_09_validation",
            role="verification_agent",
            prompt=build_verification_prompt(self.config, brief, planner, evidence_paths),
            schema_path=self.verify_schema,
        )
        write_json(self.shared_dir / "verification.json", verification)
        self._stage_manifest("07_artifact_validation", {"verification": verification})

        self.console.step("Stage 10: Final acceptance summary")
        final_summary = {
            "run_id": self.run_id,
            "completed_at": utc_now(),
            "brief_sha256": brief.sha256,
            "planner": to_dict(planner),
            "worker_results": [to_dict(item) for item in worker_results],
            "verification": verification,
        }
        write_json(self.run_root / "final_summary.json", final_summary)
        self._write_markdown_summary(final_summary)
        self._stage_manifest("10_final_acceptance_summary", {"final_summary_path": str(self.run_root / "final_summary.json")})

    def _write_markdown_summary(self, final_summary: dict[str, Any]) -> None:
        worker_lines = []
        for result in final_summary["worker_results"]:
            worker_lines.append(
                f"- `{result['step_id']}` ({result['role']}): {result['status']} :: {result['summary']}"
            )
        verification = final_summary["verification"]
        content = "\n".join(
            [
                "# Final Acceptance Summary",
                "",
                f"- Run ID: `{final_summary['run_id']}`",
                f"- Completed At: `{final_summary['completed_at']}`",
                f"- Brief SHA-256: `{final_summary['brief_sha256']}`",
                "",
                "## Worker Results",
                *worker_lines,
                "",
                "## Verification",
                f"- Status: `{verification.get('status', 'unknown')}`",
                f"- Summary: {verification.get('summary', '')}",
            ]
        )
        (self.run_root / "FINAL_ACCEPTANCE_SUMMARY.md").write_text(content + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Codex-only multi-agent workflow orchestrator")
    parser.add_argument("--workspace-root", default=".", help="Repository root")
    parser.add_argument("--project-description", default="Project_description.md", help="Canonical project brief path")
    parser.add_argument("--output-root", default="runs", help="Root directory for manifests and logs")
    parser.add_argument("--model", default="gpt-5-codex", help="Codex model to use")
    parser.add_argument("--sandbox-mode", default="workspace-write", help="Codex sandbox mode")
    parser.add_argument("--approval-policy", default="never", help="Recorded approval policy for manifests")
    parser.add_argument("--max-concurrency", type=int, default=2, help="Maximum concurrent worker agents")
    parser.add_argument("--timeout-seconds", type=int, default=1200, help="Per-agent timeout")
    parser.add_argument("--planner-repair-attempts", type=int, default=3, help="Maximum planner validation attempts")
    parser.add_argument("--codex-bin", default="codex", help="Codex executable name")
    parser.add_argument("--quiet", action="store_true", help="Disable verbose pink step logging")
    return parser


async def run_from_args(args: argparse.Namespace) -> None:
    config = RuntimeConfig(
        model=args.model,
        sandbox_mode=args.sandbox_mode,
        approval_policy=args.approval_policy,
        workspace_root=str(Path(args.workspace_root).resolve()),
        project_description=args.project_description,
        output_root=str((Path(args.workspace_root) / args.output_root).resolve()),
        max_concurrency=args.max_concurrency,
        timeout_seconds=args.timeout_seconds,
        planner_repair_attempts=args.planner_repair_attempts,
        codex_bin=args.codex_bin,
        verbose=not args.quiet,
    )
    orchestrator = Orchestrator(config)
    await orchestrator.run()
