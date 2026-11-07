#!/usr/bin/env python3
"""Export the GII specification as a consumer-facing register source.

GII remains the semantic authority for the exported snapshot. This file is a
generated interchange artifact; it is not a second hand-maintained register
map.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GII = ROOT.parent / "growatt-inverter-info" / "spec/growatt-register-spec.json"
DEFAULT_OUTPUT = ROOT / "Protocols/growatt_gii_register_source.json"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_revision(path: Path) -> str | None:
    try:
        return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def export(gii_path: Path, output: Path) -> None:
    spec = load(gii_path)
    path_by_id = {item["path_id"]: item for item in spec["applicability"]["paths"]}
    rows = []
    for register in sorted(
        spec["registers"],
        key=lambda item: (item["table"], item["address"] is None, item["address"] or -1, item["register_id"]),
    ):
        rows.append(
            {
                "table": register["table"],
                "function_code": register["function_code"],
                "address": register["address"],
                "address_end": register["address_end"],
                "length_words": register["length_words"],
                "canonical_name": register["canonical_name"],
                "semantic_key": register["semantic_key"],
                "source_aliases": register["source_aliases"],
                "datatype": register["datatype"],
                "access": register["access"],
                "applicability": [
                    {
                        "family_id": path_by_id[path_ref]["family_id"],
                        "family_label": path_by_id[path_ref]["family_label"],
                        "declared_start": path_by_id[path_ref]["declared_start"],
                        "declared_end": path_by_id[path_ref]["declared_end"],
                        "qualifier": path_by_id[path_ref].get("qualifier"),
                    }
                    for path_ref in register["applicability_path_refs"]
                    if path_ref in path_by_id
                ],
                "evidence": {
                    "validation_state": register["evidence"].get("validation_state"),
                    "ha_readiness": register["evidence"].get("ha_readiness"),
                    "conflict_refs": register["evidence"].get("conflict_refs", []),
                    "other_evidence": register["evidence"].get("other_evidence", []),
                },
                "source_register_id": register["source_register_id"],
                "source_provenance": register["source_provenance"],
            }
        )
    payload = {
        "schema": "growatt-gii-register-source-v1",
        "source": {
            "repository": "growatt-inverter-info",
            "revision": git_revision(gii_path.parent.parent),
            "spec_sha256": sha256(gii_path),
            "spec_path": "spec/growatt-register-spec.json",
            "authority": spec["authority"],
        },
        "row_count": len(rows),
        "rows": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Exported {len(rows)} GII rows to {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gii-spec", type=Path, default=DEFAULT_GII)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    export(args.gii_spec, args.output)


if __name__ == "__main__":
    main()
