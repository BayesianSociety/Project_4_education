from __future__ import annotations

import asyncio
import sys

from codex_workflow.workflow import build_arg_parser, run_from_args


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        asyncio.run(run_from_args(args))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
