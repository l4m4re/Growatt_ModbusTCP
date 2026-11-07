from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
EVIDENCE = ROOT / "Protocols/growatt_zaba22_firmware_register_correlation.json"
SOURCE = (
    ROOT.parent
    / "growatt-inverter-info/sources/evidence/zaba22-firmware-register-correlation.json"
)
GII_SOURCE = ROOT / "Protocols/growatt_gii_register_source.json"


def test_zaba22_firmware_snapshot_is_hash_pinned_and_conservative() -> None:
    snapshot = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert snapshot["schema"] == "growatt-zaba22-firmware-register-correlation-v1"
    if not SOURCE.exists():
        pytest.skip("the sibling GII checkout is unavailable")
    assert snapshot["source"]["artifact_sha256"] == hashlib.sha256(
        SOURCE.read_bytes()
    ).hexdigest()

    correlation = snapshot["correlation"]
    direct = {
        (row["table"], row["function_code"]): row
        for row in correlation["direct_routes"]
    }
    assert direct[("holding", 3)]["address_end"] == 30498
    assert direct[("input", 4)]["address_end"] == 31299
    assert {
        (row["table"], row["address_start"], row["address_end"])
        for row in correlation["post_table_case_search"]
    } == {
        ("input", 31300, 31399),
        ("input", 31500, 31599),
        ("holding", 32000, 32099),
    }
    semantics = correlation["legacy_read_semantics"]
    assert semantics["schema"] == "growatt-zaba22-legacy-read-semantics.v1"
    assert semantics["summary"]["row_count"] == 136
    assert semantics["confidence_boundary"]["not_proven"]
    provider = correlation["legacy_memory_provider"]
    assert provider["schema"] == "growatt-zaba22-legacy-memory-provider.v1"
    assert provider["summary"]["row_count"] == 122
    assert provider["summary"]["row_count_by_function_code"] == {"3": 44, "4": 78}
    capture = correlation["capture_conformance"]
    assert capture["schema"] == "growatt-zaba22-capture-conformance-aggregate.v1"
    assert capture["aggregate_summary"]["records"] == 16653
    assert capture["aggregate_summary"]["all_three_matches"] == 16653
    assert capture["aggregate_summary"]["write_readback_matches"] == 653
    assert "firmware and captures" in snapshot["source"]["redistribution"]


def test_gii_snapshot_preserves_per_register_firmware_correlation() -> None:
    snapshot = json.loads(GII_SOURCE.read_text(encoding="utf-8"))
    rows = {row["address"]: row for row in snapshot["rows"] if row["table"] == "holding"}
    vpp_rows = [
        row
        for row in snapshot["rows"]
        if row["address"] is not None
        and (
            30000 <= row["address"] <= 30999
            or 31000 <= row["address"] <= 31999
            or 32000 <= row["address"] <= 32099
        )
    ]
    assert len(vpp_rows) == 180
    assert all("other_evidence" in row["evidence"] for row in vpp_rows)

    direct = rows[30100]["evidence"]["other_evidence"]
    assert direct
    assert direct[0]["firmware_confidence"] == "high"
    assert direct[0]["semantic_name_confidence"] == "source_correlation_only"
    assert direct[0]["firmware_resolved_byte_range"] == {
        "start": "0x2000aba4",
        "end": "0x2000aba5",
    }
    assert direct[0]["firmware_routes"] == ["direct VPP holding table"]

    unresolved = rows[32000]["evidence"]["other_evidence"]
    assert unresolved
    assert unresolved[0]["firmware_confidence"] == "unresolved"
    assert unresolved[0]["firmware_read_status"] == [
        "not_established_in_scoped_artifacts"
    ]
