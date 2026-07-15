from pathlib import Path

import pytest

from eosdk.eodata._transfer import (
    TransferState,
    coalesce,
    load_state,
    plan_ranges,
    save_state,
    state_path,
)


class TestCoalesce:
    def test_merges_overlapping_and_adjacent(self) -> None:
        assert coalesce([(0, 9), (10, 19), (30, 39), (35, 50)]) == [(0, 19), (30, 50)]

    def test_unsorted_input(self) -> None:
        assert coalesce([(20, 29), (0, 9)]) == [(0, 9), (20, 29)]


class TestPlanRanges:
    def test_empty_state_full_plan(self) -> None:
        plan = plan_ranges(100, part_size=40)
        assert plan.ranges == ((0, 39), (40, 79), (80, 99))
        assert plan.bytes_remaining == 100

    def test_exact_multiple(self) -> None:
        plan = plan_ranges(80, part_size=40)
        assert plan.ranges == ((0, 39), (40, 79))

    def test_partially_complete(self) -> None:
        plan = plan_ranges(100, part_size=40, completed=[(0, 39)])
        assert plan.ranges == ((40, 79), (80, 99))
        assert plan.bytes_remaining == 60

    def test_hole_in_middle(self) -> None:
        plan = plan_ranges(100, part_size=100, completed=[(0, 9), (50, 99)])
        assert plan.ranges == ((10, 49),)

    def test_complete(self) -> None:
        plan = plan_ranges(100, part_size=40, completed=[(0, 99)])
        assert plan.complete
        assert plan.bytes_remaining == 0

    def test_zero_size_object(self) -> None:
        assert plan_ranges(0, part_size=40).complete

    def test_completed_ranges_clamped_to_size(self) -> None:
        plan = plan_ranges(50, part_size=50, completed=[(40, 400)])
        assert plan.ranges == ((0, 39),)

    def test_invalid_args(self) -> None:
        with pytest.raises(ValueError, match="part_size"):
            plan_ranges(10, part_size=0)


class TestStateSidecar:
    def test_round_trip(self, tmp_path: Path) -> None:
        target = tmp_path / "product.zip"
        state = TransferState(key="k", total_size=100, part_size=40, validator='"etag"')
        state.mark_complete(0, 39)
        state.mark_complete(40, 79)  # adjacent: coalesced
        save_state(target, state)

        loaded = load_state(target)
        assert loaded is not None
        assert loaded.completed == [(0, 79)]
        assert loaded.matches(total_size=100, validator='"etag"')
        assert not loaded.matches(total_size=100, validator='"other"')
        assert not loaded.matches(total_size=999, validator='"etag"')

    def test_missing_or_corrupt(self, tmp_path: Path) -> None:
        target = tmp_path / "product.zip"
        assert load_state(target) is None
        state_path(target).write_text("{not json")
        assert load_state(target) is None
