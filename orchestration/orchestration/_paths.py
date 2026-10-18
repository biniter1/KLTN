"""Thêm 3 thư mục anh em vào `sys.path` để import `tier1`, `tier2_agent`, `scenario_pool`.

Bố cục repo giả định (giống snapshot hiện tại):

    <repo>/tier1_inference/tier1/...
    <repo>/tier2_agent/tier2_agent/...
    <repo>/infra/scenarios/scenario_pool.py
    <repo>/orchestration/orchestration/...   <- package này

Ghi đè bằng biến môi trường nếu bố cục khác: KLTN_TIER1_PATH, KLTN_TIER2_PATH,
KLTN_SCENARIOS_PATH (thư mục CHỨA package/module tương ứng).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_DEFAULTS = {
    "KLTN_TIER1_PATH": _REPO_ROOT / "tier1_inference",
    "KLTN_TIER2_PATH": _REPO_ROOT / "tier2_agent",
    "KLTN_SCENARIOS_PATH": _REPO_ROOT / "infra" / "scenarios",
}


def _install() -> None:
    for env_name, default in _DEFAULTS.items():
        path = Path(os.environ.get(env_name, str(default))).resolve()
        if path.exists() and str(path) not in sys.path:
            sys.path.insert(0, str(path))


_install()
