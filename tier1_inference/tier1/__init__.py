"""
Tier 1 (Inference Layer) — implement theo TIER1_DESIGN.md.

Import nhanh:

    from tier1 import run_tier1, ScoringConfig, storage
    from tier1.schema import TrafficSeries, ConfigEvidenceRecord, ServiceRef

Cấu trúc module -> mục tương ứng trong TIER1_DESIGN.md, xem README.md ở root repo
để có bảng đối chiếu đầy đủ (dùng làm căn cứ khi trình bày).
"""

from .config import ScoringConfig, DEFAULT_CONFIG
from .pipeline import run_tier1
from .schema import (
    ConfigEvidenceRecord,
    ConfigFinding,
    DependencyEdgeResult,
    DependencyStatus,
    IdentityKey,
    Scope,
    ServiceRef,
    TrafficClass,
    TrafficSeries,
)

__all__ = [
    "run_tier1",
    "ScoringConfig",
    "DEFAULT_CONFIG",
    "ConfigEvidenceRecord",
    "ConfigFinding",
    "DependencyEdgeResult",
    "DependencyStatus",
    "IdentityKey",
    "Scope",
    "ServiceRef",
    "TrafficClass",
    "TrafficSeries",
]
