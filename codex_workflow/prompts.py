from __future__ import annotations

from textwrap import dedent

from .models import PlanTask, Planner, ProjectBrief, RuntimeConfig


ROLE_PREFIX = dedent(
    """
    You are one role inside a Codex-only multi-agent workflow.
    Follow the repository state and the project brief exactly.
    Do not assume hidden requirements.
    If you are asked to edit, only edit the exact owned files listed below.
    Return only a JSON object matching the supplied schema.
    """
).strip()


def build_context_analyst_prompt(config: RuntimeConfig, brief: ProjectBrief) -> str:
    return dedent(
        f"""
        {ROLE_PREFIX}

        ROLE: Context Analyst
        STAGE: Context analysis
        WORKSPACE_ROOT: {config.workspace_root}
        CANONICAL_BRIEF_PATH: {brief.path}
        CANONICAL_BRIEF_SHA256: {brief.sha256}

        Read `{brief.path}` in full before producing output. Treat it as the canonical run brief.
        Analyze repository state and the brief together. Produce structured requirements that extract:
        - scope
        - constraints
        - success criteria
        - expected outputs
        - stack-specific requirements
        - validation expectations
        - missing information
        - important repository constraints

        Also identify relevant asset paths and layout reference inputs if present.
        """
    ).strip()


def build_architect_prompt(config: RuntimeConfig, brief: ProjectBrief, context_analysis_json: str) -> str:
    return dedent(
        f"""
        {ROLE_PREFIX}

        ROLE: Architect
        STAGE: Planner generation
        WORKSPACE_ROOT: {config.workspace_root}
        CANONICAL_BRIEF_PATH: {brief.path}
        CANONICAL_BRIEF_SHA256: {brief.sha256}

        Read `{brief.path}` in full before producing output.
        Use the context analysis JSON below plus the repository state to generate a machine-checkable plan.

        Constraints:
        - Use concrete relative file paths only in path fields.
        - No overlapping ownership across tasks.
        - Produce tasks only for roles: backend_producer, frontend_producer, verification_agent.
        - Validation expectations must be offline-safe when dependencies are not guaranteed.
        - Outputs must map to real files, routes, commands, or runtime checks.
        - Plan for Next.js 16.x, TypeScript, SQLite with better-sqlite3 when the brief requires them.

        CONTEXT_ANALYSIS_JSON:
        {context_analysis_json}
        """
    ).strip()


def build_planner_repair_prompt(
    config: RuntimeConfig,
    brief: ProjectBrief,
    previous_plan_json: str,
    error_message: str,
) -> str:
    return dedent(
        f"""
        {ROLE_PREFIX}

        ROLE: Architect
        STAGE: Planner repair
        WORKSPACE_ROOT: {config.workspace_root}
        CANONICAL_BRIEF_PATH: {brief.path}
        CANONICAL_BRIEF_SHA256: {brief.sha256}

        Read `{brief.path}` in full before producing output.
        Repair the previous plan.
        Reject vague fields. Keep the task model typed and concrete.

        VALIDATION_ERROR:
        {error_message}

        PREVIOUS_PLAN_JSON:
        {previous_plan_json}
        """
    ).strip()


def build_worker_prompt(config: RuntimeConfig, brief: ProjectBrief, planner: Planner, task: PlanTask) -> str:
    return dedent(
        f"""
        {ROLE_PREFIX}

        ROLE: {task.role}
        STAGE: Worker generation
        WORKSPACE_ROOT: {config.workspace_root}
        CANONICAL_BRIEF_PATH: {brief.path}
        CANONICAL_BRIEF_SHA256: {brief.sha256}

        Read `{brief.path}` in full before producing output.

        You own only:
        {chr(10).join(f"- {path}" for path in task.owned_paths)}

        Read-only inputs:
        {chr(10).join(f"- {path}" for path in task.read_only_inputs) if task.read_only_inputs else "- (none specified)"}

        Forbidden files:
        {chr(10).join(f"- {path}" for path in task.forbidden_paths) if task.forbidden_paths else "- every path outside owned files"}

        Required outputs:
        {chr(10).join(f"- {path}" for path in task.required_outputs)}

        Contracts:
        {chr(10).join(f"- {item}" for item in task.contracts) if task.contracts else "- honor the shared planner contracts"}

        Validation rules:
        {chr(10).join(f"- {rule.kind}: {rule.target} :: {rule.details}" for rule in task.validation_rules)}

        Build expectations:
        {chr(10).join(f"- {item}" for item in task.build_expectations) if task.build_expectations else "- satisfy planner expectations"}

        Runtime expectations:
        {chr(10).join(f"- {item}" for item in task.runtime_expectations) if task.runtime_expectations else "- satisfy planner expectations"}

        Shared planner summary:
        {planner.task_summary}

        Return a concise summary of the changes and any blockers. Do not claim success if files were not produced.
        """
    ).strip()


def build_verification_prompt(
    config: RuntimeConfig,
    brief: ProjectBrief,
    planner: Planner,
    evidence_paths: list[str],
) -> str:
    joined_evidence = "\n".join(f"- {path}" for path in evidence_paths)
    return dedent(
        f"""
        {ROLE_PREFIX}

        ROLE: Verification Agent
        STAGE: Artifact validation, build validation, runtime validation, final acceptance summary
        WORKSPACE_ROOT: {config.workspace_root}
        CANONICAL_BRIEF_PATH: {brief.path}
        CANONICAL_BRIEF_SHA256: {brief.sha256}

        Read `{brief.path}` in full before producing output.
        Validate repository outputs against the planner and the brief.
        Prefer structural and semantic checks over wording checks.
        Read the relevant artifacts directly instead of inferring their contents.
        Normalize routes before comparing equivalent route families.

        Planner summary:
        {planner.task_summary}

        Evidence paths:
        {joined_evidence if joined_evidence else "- repository root"}
        """
    ).strip()

