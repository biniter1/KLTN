"""Orchestration layer của KLTN — Batch Orchestrator + API Server (ORCHESTRATION_DESIGN.md).

Import `orchestration._paths` trước tiên để `tier1` (tier1_inference/), `tier2_agent`
(tier2_agent/) và `scenario_pool` (infra/scenarios/) import được khi chạy từ repo gốc.
"""

from . import _paths  # noqa: F401  (side effect: sys.path)

__all__ = ["_paths"]
