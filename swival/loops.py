"""Background, snapshot-isolated /loop registrations for REPL mode.

The registry is accessed only from the REPL main thread (loops fire between
user commands), so no locking is needed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .input_dispatch import ParsedInput


MAX_ACTIVE_LOOPS = 4
WARN_FAILURES = 3
CANCEL_FAILURES = 6


def monotonic() -> float:
    return time.monotonic()


@dataclass
class LoopRegistration:
    id: int
    interval_seconds: int
    prompt: str
    parsed_prompt: ParsedInput
    last_fire: float
    created_at: float
    consecutive_failures: int = 0
    last_error: str | None = None


@dataclass
class LoopRegistry:
    _next_id: int = 1
    _loops: list[LoopRegistration] = field(default_factory=list)

    def is_full(self) -> bool:
        return len(self._loops) >= MAX_ACTIVE_LOOPS

    def __len__(self) -> int:
        return len(self._loops)

    def __iter__(self):
        return iter(list(self._loops))

    def register(
        self,
        *,
        interval_seconds: int,
        prompt: str,
        parsed_prompt: ParsedInput,
    ) -> LoopRegistration:
        reg = LoopRegistration(
            id=self._next_id,
            interval_seconds=interval_seconds,
            prompt=prompt,
            parsed_prompt=parsed_prompt,
            last_fire=monotonic(),
            created_at=time.time(),
        )
        self._next_id += 1
        self._loops.append(reg)
        return reg

    def get(self, loop_id: int) -> LoopRegistration | None:
        for reg in self._loops:
            if reg.id == loop_id:
                return reg
        return None

    def remove(self, loop_id: int) -> bool:
        for i, reg in enumerate(self._loops):
            if reg.id == loop_id:
                del self._loops[i]
                return True
        return False

    def clear(self) -> int:
        n = len(self._loops)
        self._loops.clear()
        return n

    def reset(self) -> int:
        return self.clear()

    def summary_line(self) -> str | None:
        if not self._loops:
            return None
        return f"loops: {len(self._loops)} active"

    def due(self) -> list[LoopRegistration]:
        """Return registrations whose interval has elapsed, in id order."""
        if not self._loops:
            return []
        t = monotonic()
        return [r for r in self._loops if t - r.last_fire >= r.interval_seconds]
