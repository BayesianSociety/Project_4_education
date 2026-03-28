from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RuntimeConfig:
    model: str
    sandbox_mode: str
    approval_policy: str
    workspace_root: str
    project_description: str
    output_root: str
    max_concurrency: int
    timeout_seconds: int
    planner_repair_attempts: int
    codex_bin: str = "codex"
    color: str = "always"
    verbose: bool = True


@dataclass
class ProjectBrief:
    path: str
    content: str
    sha256: str


@dataclass
class ValidationRule:
    name: str
    kind: str
    target: str
    details: str


@dataclass
class PlanTask:
    id: str
    role: str
    summary: str
    owned_paths: list[str]
    required_outputs: list[str]
    read_only_inputs: list[str]
    forbidden_paths: list[str]
    dependencies: list[str]
    contracts: list[str]
    validation_rules: list[ValidationRule]
    build_expectations: list[str]
    runtime_expectations: list[str]


@dataclass
class Planner:
    task_summary: str
    roles: list[str]
    contracts: list[str]
    validation_rules: list[ValidationRule]
    build_expectations: list[str]
    runtime_expectations: list[str]
    tasks: list[PlanTask]


@dataclass
class StepResult:
    step_id: str
    role: str
    status: str
    summary: str
    changed_files: list[str] = field(default_factory=list)
    created_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    raw_output_path: str | None = None
    message_path: str | None = None
    worktree_path: str | None = None
    attempt: int = 1


@dataclass
class StageManifest:
    stage: str
    created_at: str
    workspace_root: str
    snapshot_path: str
    sha256_manifest_path: str
    metadata: dict[str, Any]


def planner_from_dict(data: dict[str, Any]) -> Planner:
    rules = [ValidationRule(**rule) for rule in data.get("validation_rules", [])]
    tasks: list[PlanTask] = []
    for task in data.get("tasks", []):
        task_rules = [ValidationRule(**rule) for rule in task.get("validation_rules", [])]
        tasks.append(
            PlanTask(
                id=task["id"],
                role=task["role"],
                summary=task["summary"],
                owned_paths=list(task.get("owned_paths", [])),
                required_outputs=list(task.get("required_outputs", [])),
                read_only_inputs=list(task.get("read_only_inputs", [])),
                forbidden_paths=list(task.get("forbidden_paths", [])),
                dependencies=list(task.get("dependencies", [])),
                contracts=list(task.get("contracts", [])),
                validation_rules=task_rules,
                build_expectations=list(task.get("build_expectations", [])),
                runtime_expectations=list(task.get("runtime_expectations", [])),
            )
        )
    return Planner(
        task_summary=data["task_summary"],
        roles=list(data.get("roles", [])),
        contracts=list(data.get("contracts", [])),
        validation_rules=rules,
        build_expectations=list(data.get("build_expectations", [])),
        runtime_expectations=list(data.get("runtime_expectations", [])),
        tasks=tasks,
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def to_dict(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return value

