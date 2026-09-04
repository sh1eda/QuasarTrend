#!/usr/bin/env python3
"""Regenerate the immutable V2 initialization artifacts without market access."""
from __future__ import annotations

from pathlib import Path

from quasartrend.research.xau_v2_initialization import regenerate_v2_artifacts


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    for name, path in regenerate_v2_artifacts(root).items():
        print(f"{name}: {path.relative_to(root)}")
