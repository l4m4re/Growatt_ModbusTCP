"""Tests for the staged MOD/MIN TL-XH TOU editor."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import time
from types import SimpleNamespace

import pytest

from growatt_under_test.const import MOD_TOU_PERIODS
from growatt_under_test.tou_editor import (
    TouDraft,
    TouEditor,
    TouEditorError,
    _apply_pair,
    _period_definition,
)


class _FakeClient:
    def __init__(self, values: list[int]) -> None:
        self.values = values
        self.reads = 0
        self.writes: list[tuple[int, list[int]]] = []

    @contextmanager
    def write_batch(self, _what: str):
        yield

    def read_holding_registers(self, _register: int, _count: int) -> list[int]:
        self.reads += 1
        return list(self.values)

    def write_registers(self, register: int, values: list[int]) -> bool:
        self.writes.append((register, values))
        self.values = list(values)
        return True


def _draft(start: time = time(2, 32), end: time = time(15, 14), priority: int = 0) -> TouDraft:
    return TouDraft(
        start=start,
        end=end,
        priority=priority,
        enabled=True,
        base_pair=(0x8220, 0x0F0E),
        dirty_fields=frozenset({"start", "end", "priority", "enabled"}),
    )


def test_apply_writes_one_complete_pair():
    client = _FakeClient([0x8220, 0x0F0E])
    result = _apply_pair(
        client,
        _period_definition(1),
        _draft(start=time(3, 30), end=time(16, 0)),
        (0x8220, 0x0F0E),
    )

    assert result.written is True
    assert client.writes == [(3038, [0x831E, 0x1000])]
    assert client.reads == 2


def test_apply_skips_a_pair_that_already_matches():
    client = _FakeClient([0x8220, 0x0F0E])
    result = _apply_pair(client, _period_definition(1), _draft(), (0x8220, 0x0F0E))

    assert result.written is False
    assert client.writes == []


def test_apply_rejects_external_change_without_writing():
    client = _FakeClient([0x8221, 0x0F0E])

    with pytest.raises(TouEditorError, match="changed externally"):
        _apply_pair(client, _period_definition(1), _draft(), (0x8220, 0x0F0E))

    assert client.writes == []


def test_period_five_starts_after_the_register_gap():
    assert _period_definition(4)["end_reg"] == 3045
    assert _period_definition(5)["start_reg"] == 3050


def test_draft_update_does_not_call_modbus():
    coordinator = SimpleNamespace(
        has_real_data=True,
        data=SimpleNamespace(mod_tou_1_start=0x8220, mod_tou_1_end=0x0F0E),
        async_update_listeners=lambda: None,
    )
    editor = TouEditor(coordinator)
    editor.update_draft(1, "priority", 2)

    assert editor.draft(1).priority == 2
    assert editor.is_dirty(1) is True


def test_poll_does_not_replace_a_dirty_draft():
    coordinator = SimpleNamespace(
        has_real_data=True,
        data=SimpleNamespace(mod_tou_1_start=0x8220, mod_tou_1_end=0x0F0E),
        async_update_listeners=lambda: None,
    )
    editor = TouEditor(coordinator)
    editor.update_draft(1, "priority", 2)
    coordinator.data.mod_tou_1_start = 0xA220

    assert editor.draft(1).priority == 2


def test_reverting_a_field_clears_its_pending_state():
    coordinator = SimpleNamespace(
        has_real_data=True,
        data=SimpleNamespace(mod_tou_1_start=0x8220, mod_tou_1_end=0x0F0E),
        async_update_listeners=lambda: None,
    )
    editor = TouEditor(coordinator)
    editor.update_draft(1, "priority", 2)
    editor.update_draft(1, "priority", 0)

    assert editor.is_dirty(1) is False


def test_all_periods_are_defined_as_two_register_pairs():
    assert len(MOD_TOU_PERIODS) == 9
    assert all(period["end_reg"] == period["start_reg"] + 1 for period in MOD_TOU_PERIODS)
