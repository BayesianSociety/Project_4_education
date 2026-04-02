# Codex Multi-Agent Workflow

This repository now contains a production-oriented Python 3.10 orchestrator that drives a Codex-only multi-agent workflow through `codex exec` without using the OpenAI API or Codex SDK.

The workflow is phase-based and explicit:

1. Environment preflight
2. Context analysis
3. Planner generation
4. Planner schema validation
5. Planner repair loop if needed
6. Worker generation
7. Artifact validation
8. Build validation
9. Runtime validation
10. Final acceptance summary

It always reads `Project_description.md` in full at ingestion time, hashes it, stores the canonical brief in shared run state, and treats it as the run's source of truth.

## Start

Run the orchestrator from the repository root:

```bash
python3 orchestrator.py
```

Useful flags:

```bash
python3 orchestrator.py --max-concurrency 2 --timeout-seconds 1800 --model gpt-5-codex
```

Run the bounded self-healing supervisor when you want automatic failure capture plus conservative Codex repair attempts:

```bash
python3 codex_supervisor.py --project-brief Project_description.md
```

Useful supervisor flags:

```bash
python3 codex_supervisor.py --project-brief Project_description.md --max-run-attempts 3 --max-identical-failures 2 --verbose
```

## What It Does

- Uses `codex exec` for every role step.
- Keeps the role set small and fixed: Orchestrator, Context Analyst, Architect, Backend Producer, Frontend Producer, Verification Agent.
- Uses separate git worktrees for editing workers under `tmp/worktrees/`.
- Bounds concurrent edit workers with an asyncio semaphore.
- Enforces per-step filesystem allowlists from planner ownership.
- Writes SHA-256 snapshots and manifests after major stages under `runs/<timestamp>/`.
- Uses JSON Schema files in [`schemas/`](/home/postnl/multi-agent-producer_V0/Project_4_education/schemas) to force structured agent outputs.
- Records shared state and decisions under `runs/<timestamp>/shared/`.
- Prints every step in bold pink ANSI output when verbose mode is enabled.

## Repository Layout

- [`orchestrator.py`](/home/postnl/multi-agent-producer_V0/Project_4_education/orchestrator.py)
  Entrypoint.
- [`codex_workflow/`](/home/postnl/multi-agent-producer_V0/Project_4_education/codex_workflow)
  Runtime package.
- [`public/assets/backgrounds/`](/home/postnl/multi-agent-producer_V0/Project_4_education/public/assets/backgrounds)
  Canonical background asset directory.
- [`public/assets/sprites/`](/home/postnl/multi-agent-producer_V0/Project_4_education/public/assets/sprites)
  Canonical sprite asset directory.
- [`design/layout_refs/`](/home/postnl/multi-agent-producer_V0/Project_4_education/design/layout_refs)
  Canonical layout reference directory.

## Notes

- The orchestrator is stdlib-only and targets Python 3.10.
- It does not install packages or require network access for planner validation.
- Verification logic is schema-driven and expects downstream Codex roles to read artifacts directly instead of inferring them from prose.
- `git worktree` creation requires a writable `.git` directory. Preflight fails early if the repository checkout does not allow that.
- `codex_supervisor.py` writes its self-heal audit trail under `.self_heal/`, including `attempts.jsonl`, per-run logs in `.self_heal/run_logs/`, per-repair records in `.self_heal/repair_logs/`, generated schemas in `.self_heal/schemas/`, and `.self_heal/final_report.json`.
- The supervisor uses bounded `codex exec` repair attempts only. It snapshots the workspace, blocks out-of-scope edits, and stops on repeated identical failures or when the configured attempt budget is exhausted.
- When an orchestrator implementation exposes checkpoint resume flags, the supervisor reruns conservatively from the earliest invalidated stage. In the current repository it falls back to a full restart because `orchestrator.py` does not yet expose `--resume-from-stage`.
