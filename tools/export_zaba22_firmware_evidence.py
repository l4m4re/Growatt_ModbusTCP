#!/usr/bin/env python3
"""Export the public-safe ZABA22 firmware correlation from GII.

The firmware image and captures stay outside this repository.  This file is a
consumer snapshot of the derived, hash-pinned correlation artifact maintained
by growatt-inverter-info.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = (
    ROOT.parent
    / "growatt-inverter-info/sources/evidence/zaba22-firmware-register-correlation.json"
)
DEFAULT_OUTPUT = ROOT / "Protocols/growatt_zaba22_firmware_register_correlation.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_revision(path: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def export(source: Path, output: Path) -> None:
    correlation: dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
    payload = {
        "schema": "growatt-zaba22-firmware-register-correlation-v1",
        "source": {
            "repository": "growatt-inverter-info",
            "repository_revision": git_revision(source.parent.parent.parent),
            "artifact_path": "sources/evidence/zaba22-firmware-register-correlation.json",
            "artifact_sha256": sha256(source),
            "redistribution": "derived public-safe evidence only; firmware and captures remain external",
        },
        "correlation": correlation,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Exported ZABA22 firmware correlation to {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    export(args.source, args.output)


if __name__ == "__main__":
    main()
