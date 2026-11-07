"""TOU period edits must not undo each other (#475).

A period's start word packs enable (bit 15), priority (bits 13-14) and start time. The
Priority and Enable selects rebuilt it from the coordinator's last poll, so two changes to
one period inside a poll interval collided: the second write was based on the word before
the first, and put it back. A MID 25KTL3-XH owner setting several TOU values in a row found
it hit and miss.

The packing lives in tou_packing.py, which is importable here; the selects and time entities
import Home Assistant, so their staged write path is checked from source.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

tou = importlib.import_module("growatt_under_test.tou_packing")
COMPONENT = Path(__file__).parent.parent / "custom_components" / "growatt_modbus"

# Period 3 from the reporter's scans: 0xCF00 = enabled, Grid, 15:00; end 0x1000 = 16:00.
GRID_ON_1500 = 0xCF00


def test_priority_keeps_time_and_enable():
    assert tou.with_priority(GRID_ON_1500, 1) == 0xAF00   # Battery, still enabled, 15:00


def test_enable_keeps_time_and_priority():
    assert tou.with_enable(GRID_ON_1500, False) == 0x4F00  # Grid, disabled, 15:00
    assert tou.with_enable(0x4F00, True) == GRID_ON_1500


def test_time_keeps_enable_and_priority():
    assert tou.with_time(GRID_ON_1500, 7, 30) == 0xC71E


def _method(path: str, cls: str, name: str) -> str:
    source = (COMPONENT / path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    klass = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == cls)
    fn = next(n for n in klass.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return ast.get_source_segment(source, fn)


@pytest.mark.parametrize("cls", ["GrowattModTouPriority", "GrowattModTouEnable"])
def test_the_selects_only_update_the_draft(cls):
    body = _method("select.py", cls, "async_select_option")
    assert "update_draft" in body
    assert "read_holding_registers" not in body
    assert "write_registers" not in body


@pytest.mark.parametrize("path, cls, name", [
    ("select.py", "GrowattModTouPriority", "async_select_option"),
    ("select.py", "GrowattModTouEnable", "async_select_option"),
    ("time.py", "GrowattModTouTime", "async_set_value"),
])
def test_every_tou_entity_updates_the_shared_draft(path, cls, name):
    body = _method(path, cls, name)
    assert "update_draft" in body
    assert "write_registers" not in body


def test_mod_tou_setup_has_one_set_of_entities():
    select_source = (COMPONENT / "select.py").read_text(encoding="utf-8")
    time_source = (COMPONENT / "time.py").read_text(encoding="utf-8")
    assert "GrowattTouDraft" not in select_source
    assert "GrowattTouDraft" not in time_source
