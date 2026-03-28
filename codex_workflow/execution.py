from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import asyncio
import json
import os
import shlex
import signal

from .console import Console


RETRYABLE_SIGNATURES = (
    "connection reset",
    "broken pipe",
    "timed out",
    "unexpected eof",
    "stream ended",
    "transport error",
    "runtime corruption",
)


@dataclass
class ExecOutcome:
    returncode: int
    stdout_path: Path
    stderr_path: Path
    message_path: Path
    parsed_message: dict[str, Any] | None
    events: list[dict[str, Any]]
    usage: dict[str, Any]


async def _read_stream(stream: asyncio.StreamReader, output_path: Path) -> None:
    with output_path.open("wb") as handle:
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            handle.flush()


def classify_failure(returncode: int, stdout_text: str, stderr_text: str) -> str:
    if returncode == 0:
        return "success"
    haystack = f"{stdout_text}\n{stderr_text}".lower()
    if any(signature in haystack for signature in RETRYABLE_SIGNATURES):
        return "retryable_infrastructure"
    return "task_failure"


def parse_json_events(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    events: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    if not path.exists():
        return events, usage
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(payload)
        if isinstance(payload, dict) and "usage" in payload and isinstance(payload["usage"], dict):
            usage = payload["usage"]
    return events, usage


async def run_codex_exec(
    *,
    codex_bin: str,
    prompt: str,
    schema_path: Path,
    workdir: Path,
    output_dir: Path,
    model: str,
    sandbox_mode: str,
    color: str,
    timeout_seconds: int,
    console: Console,
) -> ExecOutcome:
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = output_dir / "stdout.jsonl"
    stderr_path = output_dir / "stderr.log"
    message_path = output_dir / "last_message.json"
    cmd = [
        codex_bin,
        "exec",
        "-",
        "--json",
        "--color",
        color,
        "--sandbox",
        sandbox_mode,
        "--model",
        model,
        "--output-schema",
        str(schema_path),
        "-o",
        str(message_path),
        "-C",
        str(workdir),
    ]
    console.step(f"Launching: {' '.join(shlex.quote(part) for part in cmd)}")
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    process.stdin.write(prompt.encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()

    stdout_task = asyncio.create_task(_read_stream(process.stdout, stdout_path))
    stderr_task = asyncio.create_task(_read_stream(process.stderr, stderr_path))
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        await stdout_task
        await stderr_task
        raise TimeoutError(f"codex exec timed out after {timeout_seconds} seconds")
    await stdout_task
    await stderr_task

    parsed_message = None
    if message_path.exists():
        text = message_path.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            try:
                parsed_message = json.loads(text)
            except json.JSONDecodeError:
                parsed_message = {"raw_message": text}

    events, usage = parse_json_events(stdout_path)
    return ExecOutcome(
        returncode=process.returncode,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        message_path=message_path,
        parsed_message=parsed_message,
        events=events,
        usage=usage,
    )
