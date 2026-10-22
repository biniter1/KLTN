"""
discover_candidates() + khớp config với cạnh — TIER1_DESIGN.md mục 1 và 1b (chốt 15/10/2026).

    C = C_observed ∪ C_potential, gộp theo CẶP (pair_key = ns nguồn, svc nguồn, ns đích, svc đích)

Vì sao gặp nhau ở mức cặp: config thường không ghi protocol (`cartservice:7070` không nói
là gRPC), traffic có thể chưa biết port — khớp theo đủ 7 phần khóa cạnh thì gần như không
cặp nào gặp được. SA không nằm trong pair_key: lệch SA được GHI CỜ (`sa_mismatch`) thay
vì làm hỏng việc khớp. Đích config chưa phân giải -> pair_key ("raw", host) (schema.py).

Sau khi gặp nhau, `match_config_to_edges` so từng bản ghi config với từng cạnh của cặp:

    EXACT      cùng port, protocol config trùng hoặc config không ghi protocol (hoặc TCP)
    PORT_ONLY  cùng port, protocol lệch                      -> cờ protocol_mismatch
    PAIR_ONLY  không so được port (config hoặc cạnh thiếu port)
    config dư  port config đã biết, MỌI cạnh có port đã biết, không cạnh nào trùng
               -> kết quả NOT_OBSERVED riêng cho port đó (bản cũ: mất luôn, vd redis
                  sentinel 26379 khi traffic chỉ thấy 6379)

Mỗi cạnh chọn 1 config: mức khớp cao nhất -> finding sạch -> item_id nhỏ nhất (tất định).

Cờ `config_port_differs` (chốt 22/10/2026): cạnh đã có port, config của cùng cặp có port đã
biết nhưng KHÔNG port nào trùng. Nếu port của cạnh là port THẤY ĐƯỢC (observed/observed_log)
thì config lệch chỉ là config dư (đi nhánh NOT_OBSERVED như trên); nếu port của cạnh là port
SUY RA từ Service thì đây là mâu thuẫn -> cửa ra (Planner/hàng đợi review) chặn với
PORT_CONFLICT. Tầng 1 chỉ ghi cờ, không đổi điểm/trạng thái.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .aggregation import TrafficAggregate
from .schema import ConfigEvidenceRecord, IdentityKey

PairKey = tuple[str, str, str, str]  # (source_ns, source_svc, dest_ns, dest_svc)

EXACT = "EXACT"
PORT_ONLY = "PORT_ONLY"
PAIR_ONLY = "PAIR_ONLY"
_LEVEL_RANK = {EXACT: 0, PORT_ONLY: 1, PAIR_ONLY: 2}


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
    """2 vòng lặp trên cùng 1 dict theo pair_key (`setdefault`): cùng cặp -> cùng ô."""
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

    for cand in candidates.values():
        cand.config_evidence.sort(key=lambda e: e.sort_key)

    return candidates


# ---------------------------------------------------------------------------
# Khớp config với cạnh (mục 1b)
# ---------------------------------------------------------------------------


def _protocol_compatible(config_protocol: Optional[str], edge_protocol: str) -> bool:
    if not config_protocol:
        return True
    cp = config_protocol.upper()
    return cp == "TCP" or cp == edge_protocol.upper()


@dataclass
class EdgeConfigMatch:
    evidence: Optional[ConfigEvidenceRecord] = None
    level: Optional[str] = None
    flags: list[str] = field(default_factory=list)


@dataclass
class MatchResult:
    per_edge: dict[IdentityKey, EdgeConfigMatch]
    leftovers: list[ConfigEvidenceRecord]  # config dư -> NOT_OBSERVED riêng theo port


def match_config_to_edges(cand: Candidate) -> MatchResult:
    edges = list(cand.observed.keys())
    options: dict[IdentityKey, list[tuple[str, ConfigEvidenceRecord]]] = {k: [] for k in edges}
    leftovers: list[ConfigEvidenceRecord] = []
    all_edge_ports_known = all(k.port is not None for k in edges)

    for ev in cand.config_evidence:
        placed = False
        if ev.port is not None:
            for k in edges:
                if k.port is not None and k.port == ev.port:
                    level = EXACT if _protocol_compatible(ev.protocol, k.protocol) else PORT_ONLY
                    options[k].append((level, ev))
                    placed = True
        if placed:
            continue
        if ev.port is not None and all_edge_ports_known:
            leftovers.append(ev)
            continue
        for k in edges:
            options[k].append((PAIR_ONLY, ev))

    per_edge: dict[IdentityKey, EdgeConfigMatch] = {}
    for k in edges:
        opts = options[k]
        if not opts:
            per_edge[k] = EdgeConfigMatch()
            continue
        level, ev = min(
            opts,
            key=lambda lv: (_LEVEL_RANK[lv[0]], 0 if lv[1].config_finding.is_clean_match else 1, lv[1].sort_key),
        )
        flags: list[str] = []
        if level == PORT_ONLY:
            flags.append("protocol_mismatch")
        if (ev.source_service_account and k.source_service_account
                and k.source_service_account != "unknown"
                and ev.source_service_account != k.source_service_account):
            flags.append("sa_mismatch")
        per_edge[k] = EdgeConfigMatch(evidence=ev, level=level, flags=flags)

    config_ports = {ev.port for ev in cand.config_evidence if ev.port is not None}
    if config_ports:
        for k in edges:
            if k.port is not None and k.port not in config_ports:
                per_edge[k].flags.append("config_port_differs")

    return MatchResult(per_edge=per_edge, leftovers=leftovers)


def group_by_port(records: list[ConfigEvidenceRecord]) -> list[list[ConfigEvidenceRecord]]:
    """Cặp chỉ có config / config dư: 1 kết quả NOT_OBSERVED cho MỖI port (mục 1b)."""
    groups: dict[Optional[int], list[ConfigEvidenceRecord]] = {}
    for ev in records:
        groups.setdefault(ev.port, []).append(ev)
    return [groups[p] for p in sorted(groups, key=lambda x: (x is None, x or 0))]
