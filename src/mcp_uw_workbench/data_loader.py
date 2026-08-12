"""Synthetic data loaders. Files live in `data/` at the repo root.

Loaded once at module import time and held in memory. Cheap because the
dataset is small and synthetic.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

# Walk from src/mcp_uw_workbench/data_loader.py to repo root:
#   parents[0] = src/mcp_uw_workbench/
#   parents[1] = src/
#   parents[2] = repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _REPO_ROOT / "data"


@lru_cache(maxsize=1)
def load_applicants() -> dict[str, dict[str, Any]]:
    """Return applicants indexed by `applicant_id`."""
    raw = json.loads((_DATA_DIR / "applicants.json").read_text())
    return {a["applicant_id"]: a for a in raw}


@lru_cache(maxsize=1)
def load_rate_tables() -> dict[str, Any]:
    return json.loads((_DATA_DIR / "rate_tables.json").read_text())


def get_applicant(applicant_id: str) -> dict[str, Any]:
    """Lookup or raise KeyError. Callers should map to MCP error responses."""
    return load_applicants()[applicant_id]
