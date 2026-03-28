from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import os
import shutil


IGNORED_PARTS = {".git", "__pycache__", ".next", "node_modules"}


@dataclass
class Snapshot:
    files: dict[str, str]


@dataclass
class DiffResult:
    created: list[str]
    modified: list[str]
    deleted: list[str]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def should_ignore(relative_path: str) -> bool:
    return any(part in IGNORED_PARTS for part in Path(relative_path).parts)


def snapshot_tree(root: Path) -> Snapshot:
    files: dict[str, str] = {}
    for current_root, dirs, filenames in os.walk(root):
        dirs[:] = [item for item in dirs if item not in IGNORED_PARTS]
        current = Path(current_root)
        for name in filenames:
            full = current / name
            rel = full.relative_to(root).as_posix()
            if should_ignore(rel):
                continue
            files[rel] = sha256_file(full)
    return Snapshot(files=files)


def diff_snapshots(before: Snapshot, after: Snapshot) -> DiffResult:
    before_files = before.files
    after_files = after.files
    created = sorted(path for path in after_files if path not in before_files)
    deleted = sorted(path for path in before_files if path not in after_files)
    modified = sorted(
        path for path in after_files if path in before_files and after_files[path] != before_files[path]
    )
    return DiffResult(created=created, modified=modified, deleted=deleted)


def write_snapshot(path: Path, snapshot: Snapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{sha}  {rel}" for rel, sha in sorted(snapshot.files.items())]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def enforce_allowlist(diff: DiffResult, allowlist: set[str], delete_allowlist: set[str] | None = None) -> None:
    delete_allowlist = delete_allowlist or set()
    illegal_changes = sorted(path for path in diff.created + diff.modified if path not in allowlist)
    illegal_deletes = sorted(path for path in diff.deleted if path not in delete_allowlist)
    if illegal_changes or illegal_deletes:
        raise ValueError(
            "Filesystem policy violation: "
            f"unexpected changes={illegal_changes}, unexpected deletions={illegal_deletes}"
        )


def copy_paths(src_root: Path, dst_root: Path, relative_paths: list[str]) -> list[str]:
    copied: list[str] = []
    for rel in relative_paths:
        src = src_root / rel
        dst = dst_root / rel
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)
    return copied

