from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CONTROL_DIRS = {
    ".git",
    ".orchestrator",
    ".self_heal",
    "__pycache__",
    ".next",
    "node_modules",
    "tmp/worktrees",
}
VALIDATION_REPORT_PATHS = {
    "artifact_validation": Path(".orchestrator/reports/artifact_validation.json"),
    "build_validation": Path(".orchestrator/reports/build_validation.json"),
    "runtime_validation": Path(".orchestrator/reports/runtime_validation.json"),
}
DEFAULT_STAGE_ORDER = [
    "Environment preflight",
    "Context analysis",
    "Planner generation",
    "Planner schema validation",
    "Worker generation",
    "Artifact validation",
    "Build validation",
    "Runtime validation",
    "Final acceptance summary",
]
SOURCE_FILE_PATTERN = re.compile(
    r"(?P<path>[A-Za-z0-9_./\-[\]]+\.[A-Za-z0-9_]+)(?:(?::\d+(?::\d+)?)|(?:#L\d+(?:C\d+)?))?$"
)


@dataclass
class RunResult:
    attempt: int
    command: list[str]
    log_path: Path
    output: str
    exit_code: int
    duration_seconds: float


@dataclass
class Diagnosis:
    failure_class: str
    root_label: str
    summary: str
    suspected_files: list[str] = field(default_factory=list)
    repair_hints: list[str] = field(default_factory=list)
    validator_summary: str | None = None
    validator_findings: list[dict[str, Any]] = field(default_factory=list)
    validator_raw_source_files: list[str] = field(default_factory=list)
    validator_source_files: list[str] = field(default_factory=list)
    persisted_report_path: str | None = None
    persisted_report_excerpt: dict[str, Any] | None = None
    used_persisted_report: bool = False


@dataclass
class RepairResult:
    command: list[str]
    stdout_path: Path
    stderr_path: Path
    message_path: Path
    events: list[dict[str, Any]]
    usage: dict[str, Any]
    parsed_message: dict[str, Any] | None
    exit_code: int
    duration_seconds: float
    raw_error_payloads: list[dict[str, Any]]


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def tail_text(path: Path, max_lines: int = 50) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def load_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def is_control_path(relative_path: str) -> bool:
    candidate = Path(relative_path)
    if not candidate.parts:
        return False
    if candidate.parts[0] in {".git", ".orchestrator", ".self_heal", "__pycache__", ".next", "node_modules"}:
        return True
    normalized = candidate.as_posix()
    return normalized.startswith("tmp/worktrees/")


def snapshot_workspace(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for current_root, dirs, filenames in os.walk(root):
        current = Path(current_root)
        relative_root = current.relative_to(root).as_posix() if current != root else ""
        dirs[:] = [
            item
            for item in dirs
            if not is_control_path(f"{relative_root}/{item}".strip("/"))
        ]
        for filename in filenames:
            full_path = current / filename
            relative = full_path.relative_to(root).as_posix()
            if is_control_path(relative):
                continue
            digest = hashlib.sha256()
            with full_path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            files[relative] = digest.hexdigest()
    return files


def diff_snapshots(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    created = sorted(path for path in after if path not in before)
    deleted = sorted(path for path in before if path not in after)
    modified = sorted(path for path in after if path in before and after[path] != before[path])
    return {"created": created, "modified": modified, "deleted": deleted}


def normalize_source_path(value: str) -> str:
    normalized = value.strip()
    normalized = re.sub(r"#L\d+(?:C\d+)?$", "", normalized)
    normalized = re.sub(r":\d+(?::\d+)?$", "", normalized)
    return normalized


def extract_paths_from_text(text: str) -> list[str]:
    results: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9_./\-[\]#:]+", text):
        match = SOURCE_FILE_PATTERN.match(token.rstrip(".,;:"))
        if match:
            results.add(normalize_source_path(match.group("path")))
    return sorted(results)


def flatten_strings(value: Any) -> list[str]:
    items: list[str] = []
    if isinstance(value, str):
        items.append(value)
    elif isinstance(value, dict):
        for nested in value.values():
            items.extend(flatten_strings(nested))
    elif isinstance(value, list):
        for nested in value:
            items.extend(flatten_strings(nested))
    return items


def extract_findings(report: Any) -> list[dict[str, Any]]:
    if not isinstance(report, dict):
        return []
    for key in ("findings", "issues", "errors", "violations", "results"):
        value = report.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def extract_summary(report: Any) -> str | None:
    if not isinstance(report, dict):
        return None
    for key in ("summary", "message", "root_cause", "status_detail"):
        value = report.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_source_files_from_finding(finding: dict[str, Any]) -> list[str]:
    explicit: list[str] = []
    for key in ("source_files", "files", "file_paths", "paths"):
        value = finding.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    explicit.append(item)
    if explicit:
        return explicit
    inferred: list[str] = []
    for text in flatten_strings(finding):
        inferred.extend(extract_paths_from_text(text))
    return sorted(dict.fromkeys(inferred))


def summarize_report(report: Any) -> tuple[str | None, list[dict[str, Any]], list[str], dict[str, Any] | None]:
    summary = extract_summary(report)
    findings = extract_findings(report)
    source_files: list[str] = []
    for finding in findings:
        source_files.extend(extract_source_files_from_finding(finding))
    source_files = sorted(dict.fromkeys(source_files))
    excerpt = None
    if isinstance(report, dict):
        excerpt = {
            "summary": summary,
            "status": report.get("status"),
            "root_cause": report.get("root_cause"),
            "findings": findings[:10],
        }
    return summary, findings, source_files, excerpt


def load_plan_tasks(plan_path: Path) -> list[dict[str, Any]]:
    plan = load_json(plan_path)
    if isinstance(plan, dict) and isinstance(plan.get("tasks"), list):
        return [item for item in plan["tasks"] if isinstance(item, dict)]
    return []


def supports_orchestrator_option(option_name: str) -> bool:
    try:
        from codex_workflow.workflow import build_arg_parser

        parser = build_arg_parser()
        for action in parser._actions:
            if option_name in action.option_strings:
                return True
    except Exception:
        return False
    return False


def choose_project_brief_flag() -> str:
    if supports_orchestrator_option("--project-brief"):
        return "--project-brief"
    return "--project-description"


def parse_created_outside_allowlist(text: str) -> list[str]:
    created: set[str] = set()
    for match in re.finditer(r"Created outside allowlist:\s*(.+)", text):
        line = match.group(1).strip()
        parts = [item.strip(" '\"") for item in re.split(r"[,\]]\s*", line) if item.strip(" '\"[")]
        created.update(item for item in parts if item)
    if not created:
        match = re.search(r"unexpected changes=\[(.*?)\]", text, re.DOTALL)
        if match:
            parts = [item.strip(" '\"\n") for item in match.group(1).split(",") if item.strip()]
            created.update(item for item in parts)
    return sorted(created)


def classify_failure(output: str, exit_code: int) -> str:
    haystack = output.lower()
    if exit_code == 0:
        return "success"
    if "schema" in haystack and ("validation" in haystack or "json" in haystack):
        return "schema_compatibility"
    if "worktree" in haystack and any(term in haystack for term in ("already exists", "contains modified", "locked")):
        return "stale_worktree"
    if "dependency" in haystack and any(term in haystack for term in ("deadlock", "cycle", "cyclic")):
        return "dependency_deadlock"
    if "filesystem policy" in haystack or "created outside allowlist" in haystack or "unexpected changes=" in haystack:
        return "filesystem_policy"
    if "artifact validation" in haystack or "07_artifact_validation" in haystack:
        return "artifact_validation"
    if "build validation" in haystack:
        return "build_validation"
    if "runtime validation" in haystack:
        return "runtime_validation"
    if "codex exec" in haystack or "timed out after" in haystack or "broken pipe" in haystack:
        return "codex_exec_failure"
    return "unknown"


def classify_from_reports(root: Path, current_classification: str) -> str:
    if current_classification not in {"artifact_validation", "build_validation", "runtime_validation", "unknown"}:
        return current_classification
    for failure_class, report_path in VALIDATION_REPORT_PATHS.items():
        if not (root / report_path).exists():
            continue
        report = load_json(root / report_path)
        summary, findings, _, _ = summarize_report(report)
        haystack = " ".join(flatten_strings(report)).lower() if report is not None else ""
        if summary or findings:
            if "ownership-routing" in haystack or "source path" in haystack:
                return f"{failure_class}_source_path_normalization_bug"
            if "filesystem" in haystack:
                return "filesystem_policy"
            return failure_class
    return current_classification


def diagnose_failure(root: Path, failure_class: str, output: str) -> Diagnosis:
    failure_class = classify_from_reports(root, failure_class)
    diagnosis = Diagnosis(
        failure_class=failure_class,
        root_label=failure_class,
        summary=failure_class.replace("_", " "),
    )
    if failure_class.startswith("artifact_validation"):
        report_path = root / VALIDATION_REPORT_PATHS["artifact_validation"]
    elif failure_class == "build_validation":
        report_path = root / VALIDATION_REPORT_PATHS["build_validation"]
    elif failure_class == "runtime_validation":
        report_path = root / VALIDATION_REPORT_PATHS["runtime_validation"]
    else:
        report_path = None

    if report_path and report_path.exists():
        report = load_json(report_path)
        summary, findings, source_files, excerpt = summarize_report(report)
        diagnosis.validator_summary = summary
        diagnosis.validator_findings = findings
        diagnosis.validator_raw_source_files = list(source_files)
        diagnosis.validator_source_files = [normalize_source_path(item) for item in source_files]
        diagnosis.persisted_report_path = str(report_path.relative_to(root))
        diagnosis.persisted_report_excerpt = excerpt
        diagnosis.used_persisted_report = True
        diagnosis.suspected_files.extend(diagnosis.validator_source_files)
        if summary:
            diagnosis.summary = summary
        for finding in findings[:10]:
            detail = finding.get("message") or finding.get("summary") or finding.get("detail")
            if isinstance(detail, str) and detail.strip():
                diagnosis.repair_hints.append(detail.strip())

    if failure_class == "filesystem_policy":
        created_paths = parse_created_outside_allowlist(output)
        diagnosis.suspected_files.extend(created_paths)
        plan_tasks = load_plan_tasks(root / ".orchestrator/plan.json")
        normalized_created = [normalize_source_path(item) for item in created_paths]
        owned_paths = {
            path: task.get("id", "unknown")
            for task in plan_tasks
            for path in task.get("owned_paths", [])
            if isinstance(path, str)
        }
        if any("[" in item and "]" in item for item in normalized_created):
            owned_literal = [item for item in normalized_created if item in owned_paths]
            if owned_literal:
                diagnosis.root_label = "filesystem_policy_bracket_path_mismatch"
                diagnosis.summary = "Dynamic route paths appear to be treated as glob syntax instead of literal owned files"
                diagnosis.repair_hints.append("Normalize or compare bracketed Next.js route paths literally in filesystem policy checks.")
        elif any(item in owned_paths for item in normalized_created):
            diagnosis.root_label = "filesystem_policy_owned_path_mismatch"
            diagnosis.summary = "Filesystem policy rejected files that are already owned by the worker plan"
            diagnosis.repair_hints.append("Compare created paths against normalized owned paths before rejecting worker output.")

    if failure_class == "artifact_validation":
        plan_tasks = load_plan_tasks(root / ".orchestrator/plan.json")
        raw_paths = diagnosis.validator_raw_source_files
        normalized_paths = diagnosis.validator_source_files
        task_matches_raw = match_plan_paths(plan_tasks, raw_paths)
        task_matches_normalized = match_plan_paths(plan_tasks, normalized_paths)
        if normalized_paths and not task_matches_raw and task_matches_normalized:
            diagnosis.failure_class = "artifact_validation_source_path_normalization_bug"
            diagnosis.root_label = diagnosis.failure_class
            diagnosis.summary = "Validator source files map after line-suffix normalization, indicating a Stage 7 routing bug."
            diagnosis.repair_hints.append("Normalize validator source file annotations before ownership or routing comparisons.")
            diagnosis.suspected_files = ["orchestrator.py", "codex_supervisor.py"]

    if not diagnosis.suspected_files:
        diagnosis.suspected_files.extend(extract_paths_from_text(output))
    diagnosis.suspected_files = sorted(dict.fromkeys(item for item in diagnosis.suspected_files if item))
    if not diagnosis.repair_hints and diagnosis.validator_summary:
        diagnosis.repair_hints.append(diagnosis.validator_summary)
    return diagnosis


def match_plan_paths(tasks: list[dict[str, Any]], candidate_paths: list[str]) -> list[str]:
    matches: list[str] = []
    candidates = set(candidate_paths)
    for task in tasks:
        for key in ("owned_paths", "required_outputs", "contracts"):
            for value in task.get(key, []):
                if isinstance(value, str) and value in candidates:
                    matches.append(value)
    return sorted(dict.fromkeys(matches))


def repo_files(root: Path) -> list[str]:
    return sorted(snapshot_workspace(root).keys())


def editable_scope(root: Path, diagnosis: Diagnosis, prompt_hardening: bool) -> list[str]:
    always = {"orchestrator.py", "codex_supervisor.py"}
    if prompt_hardening:
        always.add("Prompt_V4_Codex_Supervisor.md")

    if diagnosis.failure_class in {
        "artifact_validation_source_path_normalization_bug",
        "filesystem_policy_bracket_path_mismatch",
        "filesystem_policy_owned_path_mismatch",
        "schema_compatibility",
        "stale_worktree",
        "dependency_deadlock",
        "codex_exec_failure",
    }:
        scoped = set(always)
        scoped.update(item for item in diagnosis.suspected_files if item in repo_files(root))
        return sorted(scoped)

    if diagnosis.failure_class in {"artifact_validation", "build_validation", "runtime_validation", "unknown"}:
        scoped = set(always)
        scoped.update(
            path
            for path in repo_files(root)
            if not is_control_path(path)
        )
        return sorted(scoped)

    scoped = set(always)
    scoped.update(item for item in diagnosis.suspected_files if item in repo_files(root))
    return sorted(scoped)


def build_repair_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "final_status",
            "root_cause",
            "root_cause_evidence",
            "summary",
            "changed_files",
            "investigated_files",
            "alternative_hypotheses_considered",
            "verification",
            "blockers",
        ],
        "properties": {
            "final_status": {"type": "string"},
            "root_cause": {"type": "string"},
            "root_cause_evidence": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
            "changed_files": {"type": "array", "items": {"type": "string"}},
            "investigated_files": {"type": "array", "items": {"type": "string"}},
            "alternative_hypotheses_considered": {"type": "array", "items": {"type": "string"}},
            "verification": {"type": "array", "items": {"type": "string"}},
            "blockers": {"type": "array", "items": {"type": "string"}},
        },
    }


def parse_jsonl_events(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    if not path.exists():
        return events, usage, errors
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(payload)
        if isinstance(payload.get("usage"), dict):
            usage = payload["usage"]
        event_type = str(payload.get("type", "")).lower()
        if "error" in event_type or payload.get("error"):
            errors.append(payload)
    return events, usage, errors


def run_streaming_command(command: list[str], workdir: Path, log_path: Path) -> RunResult:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output_chunks: list[str] = []
    assert process.stdout is not None
    with log_path.open("w", encoding="utf-8") as handle:
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            handle.write(line)
            handle.flush()
            output_chunks.append(line)
    exit_code = process.wait()
    duration = time.monotonic() - start
    return RunResult(
        attempt=0,
        command=command,
        log_path=log_path,
        output="".join(output_chunks),
        exit_code=exit_code,
        duration_seconds=duration,
    )


def git_state(root: Path) -> dict[str, str]:
    def run_git(args: list[str]) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        return result.stdout.strip()

    return {
        "status": run_git(["status", "--short"]),
        "worktree_list": run_git(["worktree", "list"]),
        "head": run_git(["rev-parse", "HEAD"]),
    }


def choose_resume_stage(changed_files: list[str], supported_resume: bool) -> tuple[str | None, str]:
    if not supported_resume:
        return None, "Orchestrator does not expose --resume-from-stage; full restart required."
    if not changed_files:
        return None, "No repository changes detected; cannot justify resume."
    earliest_index = len(DEFAULT_STAGE_ORDER) - 1
    for path in changed_files:
        if path in {"orchestrator.py", "codex_supervisor.py", "Prompt_V4_Codex_Supervisor.md"}:
            earliest_index = min(earliest_index, 0)
        elif path.endswith(".py") and path.startswith("codex_workflow/"):
            earliest_index = min(earliest_index, 1)
        elif any(part in path for part in ("schema", "planner", "plan")):
            earliest_index = min(earliest_index, 2)
        elif any(part in path for part in ("app/", "pages/", "src/", "public/", "design/")):
            earliest_index = min(earliest_index, 4)
        elif path.endswith(".md"):
            earliest_index = min(earliest_index, 8)
        else:
            earliest_index = min(earliest_index, 0)
    return DEFAULT_STAGE_ORDER[earliest_index], f"Changed files invalidate from stage: {DEFAULT_STAGE_ORDER[earliest_index]}"


def python_compile_check(root: Path, changed_files: list[str]) -> list[str]:
    results: list[str] = []
    py_files = [str(root / path) for path in changed_files if path.endswith(".py")]
    if not py_files:
        return results
    completed = subprocess.run(
        [sys.executable, "-m", "py_compile", *py_files],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        results.append("Python compilation passed for changed Python files.")
    else:
        results.append(f"Python compilation failed: {completed.stdout.strip()}")
    return results


def failure_fingerprint(failure_class: str, output: str) -> str:
    tail = "\n".join(output.splitlines()[-40:])
    digest = hashlib.sha256(f"{failure_class}\n{tail}".encode("utf-8")).hexdigest()
    return digest


class Supervisor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = Path(args.workspace_root).resolve()
        self.self_heal_root = self.root / ".self_heal"
        self.run_logs_dir = self.self_heal_root / "run_logs"
        self.repair_logs_dir = self.self_heal_root / "repair_logs"
        self.schemas_dir = self.self_heal_root / "schemas"
        self.attempts_jsonl = self.self_heal_root / "attempts.jsonl"
        self.final_report_path = self.self_heal_root / "final_report.json"
        self.repair_schema_path = self.schemas_dir / "repair_result.schema.json"
        self.repair_schema = build_repair_schema()
        json_dump(self.repair_schema_path, self.repair_schema)

    def log(self, message: str) -> None:
        print(message, flush=True)

    def verbose(self, label: str, payload: Any) -> None:
        if not self.args.verbose:
            return
        if isinstance(payload, (dict, list)):
            rendered = json.dumps(payload, indent=2, sort_keys=True)
        else:
            rendered = str(payload)
        print(f"[verbose] {label}: {rendered}", flush=True)

    def orchestrator_command(self, resume_stage: str | None = None) -> list[str]:
        command = [sys.executable, "orchestrator.py", choose_project_brief_flag(), self.args.project_brief]
        if supports_orchestrator_option("--max-concurrency") and self.args.max_concurrency is not None:
            command.extend(["--max-concurrency", str(self.args.max_concurrency)])
        if supports_orchestrator_option("--bootstrap-plan") and self.args.bootstrap_plan:
            command.extend(["--bootstrap-plan", self.args.bootstrap_plan])
        if supports_orchestrator_option("--dry-run-preflight") and self.args.dry_run_preflight:
            command.append("--dry-run-preflight")
        if supports_orchestrator_option("--quiet") and not self.args.verbose:
            command.append("--quiet")
        if supports_orchestrator_option("--resume-from-stage") and resume_stage:
            command.extend(["--resume-from-stage", resume_stage])
        return command

    def build_repair_prompt(
        self,
        *,
        run_result: RunResult,
        diagnosis: Diagnosis,
        allowed_files: list[str],
        resume_stage: str | None,
    ) -> str:
        decision_log = tail_text(self.root / ".orchestrator/decision_log.jsonl")
        git_info = git_state(self.root)
        plan_excerpt = load_json(self.root / ".orchestrator/plan.json")
        if isinstance(plan_excerpt, dict):
            plan_excerpt = {"task_summary": plan_excerpt.get("task_summary"), "tasks": plan_excerpt.get("tasks", [])[:10]}
        prompt = {
            "role": "bounded_repair_agent",
            "workspace_root": str(self.root),
            "failure_class": diagnosis.failure_class,
            "local_diagnosis_summary": diagnosis.summary,
            "root_label": diagnosis.root_label,
            "repair_hints": diagnosis.repair_hints[:12],
            "suspected_files": diagnosis.suspected_files[:50],
            "validator_summary": diagnosis.validator_summary,
            "validator_source_files": diagnosis.validator_source_files[:50],
            "persisted_report_path": diagnosis.persisted_report_path,
            "persisted_report_excerpt": diagnosis.persisted_report_excerpt,
            "used_persisted_report": diagnosis.used_persisted_report,
            "orchestrator_command_that_must_succeed_next": self.orchestrator_command(resume_stage),
            "allowed_editable_files": allowed_files,
            "investigative_scope": "repo-wide" if diagnosis.failure_class in {"artifact_validation", "build_validation", "runtime_validation", "unknown"} else "targeted",
            "recent_failure_output_tail": "\n".join(run_result.output.splitlines()[-120:]),
            "recent_decision_log_tail": decision_log,
            "git_worktree_state": git_info["worktree_list"],
            "git_status": git_info["status"],
            "plan_excerpt": plan_excerpt,
            "instructions": [
                "Inspect the relevant code paths across the repository before editing.",
                "Treat the local diagnosis and persisted validator findings as strong evidence unless clearly disproved by code.",
                "Prioritize validator-cited files before speculative edits.",
                "If validator source files are line-annotated, check whether Stage 7 routing in orchestrator.py is failing before broad app changes.",
                "Use the smallest durable fix.",
                "Do not use network access.",
                "Do not install packages.",
                "Do not edit files outside the allowed_editable_files list.",
                "Return only structured JSON matching the supplied schema.",
            ],
        }
        if diagnosis.failure_class in {"artifact_validation", "build_validation", "runtime_validation", "unknown"}:
            prompt["instructions"].extend(
                [
                    "Perform repo-wide root-cause analysis before proposing edits.",
                    "Trace cross-file dependencies and explain why the chosen root cause is more likely than nearby symptoms.",
                ]
            )
        return json.dumps(prompt, indent=2, sort_keys=True)

    def run_codex_repair(self, prompt: str, repair_id: str) -> RepairResult:
        output_dir = self.repair_logs_dir / repair_id
        output_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = output_dir / "stdout.jsonl"
        stderr_path = output_dir / "stderr.log"
        message_path = output_dir / "last_message.json"
        prompt_path = output_dir / "prompt.json"
        prompt_path.write_text(prompt + "\n", encoding="utf-8")
        command = [
            self.args.codex_bin,
            "exec",
            "-",
            "--json",
            "--color",
            "never",
            "--sandbox",
            "workspace-write",
            "--model",
            self.args.model,
            "--output-schema",
            str(self.repair_schema_path),
            "-o",
            str(message_path),
            "-C",
            str(self.root),
        ]
        self.verbose("codex_command", command)
        start = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout_text, stderr_text = process.communicate(prompt, timeout=self.args.repair_timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout_text, stderr_text = process.communicate()
            stderr_text = (stderr_text or "") + f"\nTimed out after {self.args.repair_timeout_seconds} seconds.\n"
        stdout_path.write_text(stdout_text or "", encoding="utf-8")
        stderr_path.write_text(stderr_text or "", encoding="utf-8")
        exit_code = process.returncode
        duration = time.monotonic() - start
        parsed_message = None
        if message_path.exists():
            text = message_path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                try:
                    parsed_message = json.loads(text)
                except json.JSONDecodeError:
                    parsed_message = {"raw_message": text}
        events, usage, errors = parse_jsonl_events(stdout_path)
        return RepairResult(
            command=command,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            message_path=message_path,
            events=events,
            usage=usage,
            parsed_message=parsed_message,
            exit_code=exit_code,
            duration_seconds=duration,
            raw_error_payloads=errors,
        )

    def run(self) -> int:
        final_report: dict[str, Any] = {
            "started_at": utc_timestamp(),
            "status": "running",
            "attempts": [],
        }
        repeated: Counter[str] = Counter()
        resume_stage: str | None = None
        supports_resume = supports_orchestrator_option("--resume-from-stage")

        for run_attempt in range(1, self.args.max_run_attempts + 1):
            command = self.orchestrator_command(resume_stage)
            log_name = f"run_attempt_{run_attempt:02d}.log"
            self.log(f"Supervisor run attempt {run_attempt}/{self.args.max_run_attempts}")
            if resume_stage:
                self.log(f"Selected resume stage: {resume_stage}")
            run_result = run_streaming_command(command, self.root, self.run_logs_dir / log_name)
            run_result.attempt = run_attempt

            record: dict[str, Any] = {
                "timestamp": utc_timestamp(),
                "kind": "run_attempt",
                "run_attempt": run_attempt,
                "command": command,
                "exit_code": run_result.exit_code,
                "duration_seconds": round(run_result.duration_seconds, 3),
                "log_path": str((self.run_logs_dir / log_name).relative_to(self.root)),
            }
            append_jsonl(self.attempts_jsonl, record)
            final_report["attempts"].append(record)

            if run_result.exit_code == 0:
                final_report["status"] = "success"
                final_report["completed_at"] = utc_timestamp()
                final_report["final_run_attempt"] = run_attempt
                json_dump(self.final_report_path, final_report)
                return 0

            failure_class = classify_failure(run_result.output, run_result.exit_code)
            diagnosis = diagnose_failure(self.root, failure_class, run_result.output)
            fingerprint = failure_fingerprint(diagnosis.failure_class, run_result.output)
            repeated[fingerprint] += 1
            self.verbose("failure_classification", {"initial": failure_class, "diagnosed": diagnosis.failure_class})
            self.verbose("diagnosis_summary", diagnosis.summary)
            self.verbose("validator_hints", diagnosis.repair_hints)
            self.verbose("suspected_files", diagnosis.suspected_files)

            if repeated[fingerprint] > self.args.max_identical_failures:
                self.log("Blocked status: repeated identical failure fingerprint exceeded limit.")
                final_report["status"] = "blocked"
                final_report["blocked_reason"] = "repeated_identical_failure"
                final_report["failure_class"] = diagnosis.failure_class
                final_report["failure_fingerprint"] = fingerprint
                final_report["completed_at"] = utc_timestamp()
                json_dump(self.final_report_path, final_report)
                return 1

            if run_attempt >= self.args.max_run_attempts:
                self.log("Blocked status: repair budget exhausted before another rerun was possible.")
                final_report["status"] = "blocked"
                final_report["blocked_reason"] = "max_attempts_exhausted"
                final_report["failure_class"] = diagnosis.failure_class
                final_report["completed_at"] = utc_timestamp()
                json_dump(self.final_report_path, final_report)
                return 1

            repair_attempt = run_attempt
            self.log(f"Repair attempt {repair_attempt}/{max(self.args.max_run_attempts - 1, 1)}")
            allowed_files = editable_scope(self.root, diagnosis, prompt_hardening=self.args.prompt_hardening)
            self.verbose("allowed_editable_files", allowed_files)
            self.verbose("persisted_report_included", diagnosis.used_persisted_report)

            before_snapshot = snapshot_workspace(self.root)
            repair_prompt = self.build_repair_prompt(
                run_result=run_result,
                diagnosis=diagnosis,
                allowed_files=allowed_files,
                resume_stage=resume_stage,
            )
            repair_id = f"repair_attempt_{repair_attempt:02d}"
            repair_result = self.run_codex_repair(repair_prompt, repair_id)
            after_snapshot = snapshot_workspace(self.root)
            diff = diff_snapshots(before_snapshot, after_snapshot)
            changed_files = sorted(set(diff["created"] + diff["modified"] + diff["deleted"]))
            disallowed = sorted(path for path in changed_files if path not in set(allowed_files))
            verification = python_compile_check(self.root, changed_files)
            resume_stage, resume_reason = choose_resume_stage(changed_files, supports_resume)
            self.verbose("repair_result_summary", repair_result.parsed_message or {})
            self.verbose("detected_changed_files", changed_files)
            self.verbose("disallowed_changes", disallowed)
            self.verbose("resume_stage_reasoning", resume_reason)

            repair_log = {
                "timestamp": utc_timestamp(),
                "kind": "repair_attempt",
                "repair_attempt": repair_attempt,
                "failure_class": diagnosis.failure_class,
                "diagnosis_summary": diagnosis.summary,
                "allowed_editable_files": allowed_files,
                "repair_prompt_path": str((self.repair_logs_dir / repair_id / "prompt.json").relative_to(self.root)),
                "codex_command": repair_result.command,
                "codex_exit_code": repair_result.exit_code,
                "codex_duration_seconds": round(repair_result.duration_seconds, 3),
                "codex_usage": repair_result.usage,
                "codex_result": repair_result.parsed_message,
                "codex_error_payloads": repair_result.raw_error_payloads,
                "changed_files": changed_files,
                "disallowed_changes": disallowed,
                "verification": verification,
                "resume_stage": resume_stage,
                "resume_reason": resume_reason,
                "stdout_path": str(repair_result.stdout_path.relative_to(self.root)),
                "stderr_path": str(repair_result.stderr_path.relative_to(self.root)),
                "message_path": str(repair_result.message_path.relative_to(self.root)),
            }
            json_dump(self.repair_logs_dir / f"{repair_id}.json", repair_log)
            append_jsonl(self.attempts_jsonl, repair_log)
            final_report["attempts"].append(repair_log)

            if repair_result.exit_code != 0:
                self.log("Blocked status: codex repair command failed.")
                final_report["status"] = "blocked"
                final_report["blocked_reason"] = "codex_exec_failure"
                final_report["failure_class"] = diagnosis.failure_class
                final_report["codex_error_payloads"] = repair_result.raw_error_payloads
                final_report["completed_at"] = utc_timestamp()
                json_dump(self.final_report_path, final_report)
                return 1
            if disallowed:
                self.log("Blocked status: repair edited files outside the allowed scope.")
                final_report["status"] = "blocked"
                final_report["blocked_reason"] = "disallowed_edit_scope"
                final_report["failure_class"] = diagnosis.failure_class
                final_report["disallowed_changes"] = disallowed
                final_report["completed_at"] = utc_timestamp()
                json_dump(self.final_report_path, final_report)
                return 1

        self.log("Blocked status: maximum repair attempts exhausted.")
        final_report["status"] = "blocked"
        final_report["blocked_reason"] = "max_attempts_exhausted"
        final_report["completed_at"] = utc_timestamp()
        json_dump(self.final_report_path, final_report)
        return 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded self-healing supervisor for orchestrator.py")
    parser.add_argument("--workspace-root", default=".", help="Repository root")
    parser.add_argument("--project-brief", default="Project_description.md", help="Project brief path")
    parser.add_argument("--max-concurrency", type=int, default=2, help="Forwarded orchestrator max concurrency")
    parser.add_argument("--bootstrap-plan", help="Forwarded bootstrap plan when supported")
    parser.add_argument("--dry-run-preflight", action="store_true", help="Forwarded dry-run preflight when supported")
    parser.add_argument("--max-run-attempts", type=int, default=3, help="Maximum total orchestrator runs including reruns")
    parser.add_argument("--max-identical-failures", type=int, default=2, help="Maximum repeated identical fingerprints")
    parser.add_argument("--codex-bin", default="codex", help="Codex executable")
    parser.add_argument("--model", default="gpt-5-codex", help="Codex model for repair")
    parser.add_argument("--repair-timeout-seconds", type=int, default=1200, help="Timeout for each codex repair exec")
    parser.add_argument("--prompt-hardening", action="store_true", help="Allow prompt file edits when needed")
    parser.add_argument("--verbose", action="store_true", help="Print detailed supervisor reasoning")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    supervisor = Supervisor(args)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
