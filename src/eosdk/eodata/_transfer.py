"""Pure resume/range planning for ranged multipart transfers.

No I/O here beyond the sidecar state file helpers: the planner is exhaustively
unit-testable, and the same plans will feed the async backend later.

Resume protocol: a sidecar ``<target>.eosdk-partial.json`` records the object's
size, validator (ETag), part size, and completed byte ranges. On resume the
object is re-validated (size + ETag); any mismatch discards the state — ranges
from different object versions are never spliced together.
"""

# Design reference: SPEC.md §6.6 (the resume protocol). Repo-only; not shipped.

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

STATE_SUFFIX = ".eosdk-partial.json"


def coalesce(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent inclusive byte ranges."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


@dataclass(frozen=True)
class RangePlan:
    """Inclusive byte ranges still to download, split into <= part_size chunks."""

    ranges: tuple[tuple[int, int], ...]
    total_size: int
    part_size: int
    validator: str | None

    @property
    def bytes_remaining(self) -> int:
        return sum(end - start + 1 for start, end in self.ranges)

    @property
    def complete(self) -> bool:
        return not self.ranges


def plan_ranges(
    total_size: int,
    part_size: int,
    completed: Sequence[tuple[int, int]] = (),
    *,
    validator: str | None = None,
) -> RangePlan:
    """Compute the ranges still needed for an object of ``total_size`` bytes."""
    if total_size < 0 or part_size <= 0:
        raise ValueError("total_size must be >= 0 and part_size > 0")
    if total_size == 0:
        return RangePlan((), 0, part_size, validator)

    done = coalesce([(max(0, s), min(e, total_size - 1)) for s, e in completed if s <= e])
    missing: list[tuple[int, int]] = []
    cursor = 0
    for start, end in done:
        if cursor < start:
            missing.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
    if cursor <= total_size - 1:
        missing.append((cursor, total_size - 1))

    chunks: list[tuple[int, int]] = []
    for start, end in missing:
        position = start
        while position <= end:
            chunks.append((position, min(position + part_size - 1, end)))
            position += part_size
    return RangePlan(tuple(chunks), total_size, part_size, validator)


@dataclass
class TransferState:
    """Sidecar contents next to a ``.part`` file."""

    key: str
    total_size: int
    part_size: int
    validator: str | None
    completed: list[tuple[int, int]] = field(default_factory=list)

    def mark_complete(self, start: int, end: int) -> None:
        self.completed = coalesce([*self.completed, (start, end)])

    def matches(self, *, total_size: int, validator: str | None) -> bool:
        return self.total_size == total_size and (
            self.validator is None or validator is None or self.validator == validator
        )


def state_path(target: Path) -> Path:
    return target.with_name(target.name + STATE_SUFFIX)


def load_state(target: Path) -> TransferState | None:
    try:
        data = json.loads(state_path(target).read_text())
        data["completed"] = [tuple(pair) for pair in data.get("completed", [])]
        return TransferState(**data)
    except (OSError, ValueError, TypeError):
        return None


def save_state(target: Path, state: TransferState) -> None:
    path = state_path(target)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state)))
    tmp.replace(path)


def clear_state(target: Path) -> None:
    state_path(target).unlink(missing_ok=True)
