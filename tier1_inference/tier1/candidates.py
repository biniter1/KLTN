"""
discover_candidates() — TIER1_DESIGN.md mục 1.

    C = C_observed ∪ C_potential

Bản pseudocode gốc trong tài liệu trả về một set (source,dest,protocol,port) đơn giản.
Ở đây triển khai đầy đủ hơn một chút để pipeline.py có đủ thông tin đi tiếp: với mỗi
CẶP (source,dest) — không phải mỗi identity_key đầy đủ, vì C_potential (config evidence)
thường không có protocol/port (mục 1: "cặp suy ra từ bằng chứng cấu hình TĨNH") — trả về
danh sách các identity_key đã quan sát (có thể nhiều, khác protocol/port) CỘNG danh sách
config evidence khớp cặp đó.

Việc quyết định NOT_OBSERVED hay không (cặp chỉ có trong C_potential, không có trong
C_observed) làm ở status.py, không làm ở đây — file này chỉ gom nhóm.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .aggregation import TrafficAggregate
from .schema import ConfigEvidenceRecord, IdentityKey


PairKey = tuple[str, str, str, str]  # (source_ns, source_svc, dest_ns, dest_svc)


@dataclass
class Candidate:
    pair_key: PairKey
    observed: dict[IdentityKey, TrafficAggregate] = field(default_factory=dict)
    config_evidence: list[ConfigEvidenceRecord] = field(default_factory=list)

    @property
    def is_observed(self) -> bool:
        """Thuộc C_observed: có traffic thật (count>0) cho ít nhất 1 tổ hợp protocol/port."""
        return len(self.observed) > 0

    @property
    def is_potential_only(self) -> bool:
        """Chỉ thuộc C_potential: có bằng chứng cấu hình nhưng chưa từng thấy traffic (mục 1)."""
        return not self.is_observed and len(self.config_evidence) > 0


def discover_candidates(
    traffic_aggregates: dict[IdentityKey, TrafficAggregate],
    config_evidence: list[ConfigEvidenceRecord],
) -> dict[PairKey, Candidate]:
    """
    C_observed  = tập cặp có traffic thật trong traffic_aggregates (count > 0)
    C_potential = tập cặp suy ra từ config_evidence (đã resolve identity_key -- mục 1 collector)
    C           = C_observed ∪ C_potential, gộp theo pair_key (source, dest)
    """
    candidates: dict[PairKey, Candidate] = {}

    for ident_key, agg in traffic_aggregates.items():
        if agg.count <= 0:
            continue  # mục 1: C_observed chỉ gồm cặp có count > 0
        pk = ident_key.pair_key()
        cand = candidates.setdefault(pk, Candidate(pair_key=pk))
        cand.observed[ident_key] = agg

    for ev in config_evidence:
        pk = ev.pair_key()
        cand = candidates.setdefault(pk, Candidate(pair_key=pk))
        cand.config_evidence.append(ev)

    return candidates
