from __future__ import annotations

from pathlib import PurePosixPath
import re

from .models import Planner


PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


class PlannerValidationError(ValueError):
    pass


def _validate_relative_path(value: str, field_name: str) -> None:
    path = PurePosixPath(value)
    if not value or value.startswith("/") or ".." in path.parts:
        raise PlannerValidationError(f"{field_name}: expected a concrete relative file path, got {value!r}")
    if path.name == "" or not PATH_RE.match(value):
        raise PlannerValidationError(f"{field_name}: invalid path syntax {value!r}")


def validate_planner(planner: Planner) -> None:
    if not planner.task_summary.strip():
        raise PlannerValidationError("task_summary: value must not be empty")
    if len(planner.tasks) < 2:
        raise PlannerValidationError("tasks: expected at least backend and frontend worker tasks")

    seen_ownership: dict[str, str] = {}
    task_ids = {task.id for task in planner.tasks}
    if len(task_ids) != len(planner.tasks):
        raise PlannerValidationError("tasks: duplicate task id detected")

    for task in planner.tasks:
        if task.role not in {"backend_producer", "frontend_producer", "verification_agent"}:
            raise PlannerValidationError(f"tasks[{task.id}].role: unsupported role {task.role!r}")
        if not task.summary.strip():
            raise PlannerValidationError(f"tasks[{task.id}].summary: must not be empty")
        if not task.owned_paths:
            raise PlannerValidationError(f"tasks[{task.id}].owned_paths: must not be empty")
        if not task.required_outputs:
            raise PlannerValidationError(f"tasks[{task.id}].required_outputs: must not be empty")
        for index, path in enumerate(task.owned_paths):
            _validate_relative_path(path, f"tasks[{task.id}].owned_paths[{index}]")
            if path in seen_ownership:
                raise PlannerValidationError(
                    f"tasks[{task.id}].owned_paths[{index}]: overlaps with task {seen_ownership[path]}"
                )
            seen_ownership[path] = task.id
        for index, path in enumerate(task.required_outputs):
            _validate_relative_path(path, f"tasks[{task.id}].required_outputs[{index}]")
        for index, path in enumerate(task.read_only_inputs):
            _validate_relative_path(path, f"tasks[{task.id}].read_only_inputs[{index}]")
        for index, path in enumerate(task.forbidden_paths):
            _validate_relative_path(path, f"tasks[{task.id}].forbidden_paths[{index}]")
        for dependency in task.dependencies:
            if dependency not in task_ids:
                raise PlannerValidationError(f"tasks[{task.id}].dependencies: unknown dependency {dependency!r}")
        for index, rule in enumerate(task.validation_rules):
            if not rule.kind.strip():
                raise PlannerValidationError(f"tasks[{task.id}].validation_rules[{index}].kind: must not be empty")
            if not rule.target.strip():
                raise PlannerValidationError(f"tasks[{task.id}].validation_rules[{index}].target: must not be empty")

