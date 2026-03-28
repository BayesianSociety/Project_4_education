from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


PINK_BOLD = "\033[1;95m"
RESET = "\033[0m"


@dataclass
class Console:
    verbose: bool = True

    def step(self, message: str) -> None:
        if not self.verbose:
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"{PINK_BOLD}[{timestamp}] {message}{RESET}", flush=True)

