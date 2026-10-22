"""
Orchestrator — nối toàn bộ các bước thành `run_tier1()`, đúng thứ tự TIER1_DESIGN.md
(cập nhật 15/10/2026):

    [stage 1] classify từng series (mục 2.2) — DNS cũng quyết ở đây (traffic_class == DNS)
    [stage 2] aggregate lên cấp cạnh (mục 2b)
    [stage 3] discover_candidates theo cặp + khớp config với cạnh theo port (mục 1, 1b)
    [stage 4] scoring từng cạnh (mục 4/4a)
    [stage 5] trạng thái: Q1 count=0 -> NOT_OBSERVED · Q2 cạnh chỉ toàn nhiễu -> nhánh
              nhiễu · Q3 UNKNOWN · Q4 hysteresis (mục 5, 5b)
    [stage 6] kiểm selector cho STRONG (mục 3b) · scope (mục 8) · lưu SQLite (mục 10b)

Đây là nơi DUY NHẤT "biết" toàn bộ thứ tự nghiệp vụ — mọi module khác đều là hàm thuần,
dễ test riêng lẻ. `baseline.py` (B1/B2) dùng lại `prepare_snapshot`, `build_observed_result`
và `build_potential_only_results` — chỉ thay đúng phần quyết định trạng thái.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from . import storage
from .aggregation import TrafficAggregate, aggregate_series
from .candidates import Candidate, EdgeConfigMatch, discover_candidates, group_by_port, match_config_to_edges
from .classification import classify_all
from .config import DEFAULT_CONFIG, ScoringConfig
from .dns_external import classify_scope
from .schema import (
    ConfigEvidenceRecord,
    DependencyEdgeResult,
    DependencyStatus,
    IdentityKey,
    Scope,
    TrafficClass,
    TrafficSeries,
)
from .scoring import CONFIG_MATCH_STRENGTH, compute_features, score_edge
from .selector_validation import MockSelectorValidator, SelectorValidator
from .status import (
    StatusDecision,
    compute_unstable_classification,
    derive_status_noise_only,
    derive_status_observed,
    derive_status_potential_only,
    downgrade_to_manual_selector,
)


@dataclass
class PreparedSnapshot:
    candidates: dict
    traffic_aggregates: dict[IdentityKey, TrafficAggregate]
    dns_aggregates: dict[IdentityKey, TrafficAggregate]


def prepare_snapshot(
    traffic_series: list[TrafficSeries],
    config_evidence: list[ConfigEvidenceRecord],
    config: ScoringConfig = DEFAULT_CONFIG,
) -> PreparedSnapshot:
    """Stage 1-3, dùng chung cho Tier 1 và B1/B2 (mục 13: "phần không đổi")."""
    classify_all(traffic_series, config)
    dns_series = [s for s in traffic_series if s.traffic_class == TrafficClass.DNS]
    normal_series = [s for s in traffic_series if s.traffic_class != TrafficClass.DNS]
    traffic_aggregates = aggregate_series(normal_series)
    dns_aggregates = aggregate_series(dns_series)
    candidates = discover_candidates(traffic_aggregates, config_evidence)
    return PreparedSnapshot(candidates, traffic_aggregates, dns_aggregates)


# ---------------------------------------------------------------------------
# Dựng kết quả
# ---------------------------------------------------------------------------


def _potential_identity_key(ev: ConfigEvidenceRecord) -> IdentityKey:
    """identity_key cho dòng chỉ có config. Đích chưa phân giải -> canonical "raw:<host>"
    (mỗi host lạ 1 dòng riêng, Tier 2 thấy được host), namespace đích None."""
    if ev.is_unresolved_destination:
        dest_ns, dest_svc = None, f"raw:{ev.raw_host or ''}"
    else:
        dest_ns, dest_svc = ev.destination_namespace, ev.destination_canonical_service
    return IdentityKey(
        source_namespace=ev.source_namespace,
        source_canonical_service=ev.source_canonical_service,
        source_service_account=ev.source_service_account,
        destination_namespace=dest_ns,
        destination_canonical_service=dest_svc,
        protocol=(ev.protocol or "UNKNOWN"),
        port=ev.port,
    )


def build_potential_only_results(
    records,
    snapshot_id: str,
    snapshot_time: Optional[str] = None,
) -> list[DependencyEdgeResult]:
    """Nhánh NOT_OBSERVED (mục 5) — bản ghi config KHÔNG ứng với cạnh traffic nào: cặp chỉ
    có config, hoặc config dư (port không có traffic, mục 1b). 1 kết quả cho MỖI port.
    Không qua sigmoid. `records`: list ConfigEvidenceRecord, hoặc Candidate (tương thích cũ)."""
    if isinstance(records, Candidate):
        records = records.config_evidence
    results: list[DependencyEdgeResult] = []
    for group in group_by_port(list(records)):
        clean = [e for e in group if e.config_finding.is_clean_match]
        ev = (clean or group)[0]
        decision = derive_status_potential_only([e.config_finding for e in group])
        results.append(DependencyEdgeResult(
            identity_key=_potential_identity_key(ev),
            destination_service_ref=ev.destination_service_ref,
            scope=ev.scope,
            traffic_class=TrafficClass.UNKNOWN,  # không có traffic để phân loại
            count=0,
            distinct_days=0,
            error_ratio=0.0,
            unstable=False,
            config_finding=ev.config_finding,
            score=None,
            dependency_status=decision.status,
            snapshot_id=snapshot_id,
            workload_revision=None,
            previous_status=None,  # nhánh này không dùng hysteresis (không qua sigmoid)
            unstable_classification=False,
            needs_human_review=decision.needs_human_review,
            escalate_to_tier2=decision.escalate_to_tier2,
            escalation_reason=decision.escalation_reason,
            port_source="config" if ev.port is not None else "unknown",
            flags=sorted(set(ev.flags)),
            snapshot_time=snapshot_time,
        ))
    return results


def _build_potential_only_result(cand, snapshot_id: str) -> DependencyEdgeResult:
    """Tương thích ngược: trả kết quả đầu tiên (code cũ giả định 1 kết quả/cặp)."""
    return build_potential_only_results(cand, snapshot_id)[0]


def _build_dns_result(agg: TrafficAggregate, config: ScoringConfig) -> DependencyEdgeResult:
    """mục 7 — DNS auto-allow, tách hẳn khỏi 5-state chính thức. dependency_status chỉ để
    nhất quán khi query; `dns_auto_allow=True` mới là cờ quyết định (Planner luôn mở DNS)."""
    return DependencyEdgeResult(
        identity_key=agg.identity_key,
        destination_service_ref=agg.destination_service_ref,
        scope=classify_scope(agg.identity_key, agg.destination_service_ref, agg.flags),
        traffic_class=TrafficClass.DNS,
        count=agg.count,
        distinct_days=agg.distinct_days,
        error_ratio=agg.error_ratio,
        unstable=agg.error_ratio > config.error_ratio_threshold,
        config_finding=None,
        score=None,
        dependency_status=DependencyStatus.OBSERVED_STRONG,  # informational
        snapshot_id="",
        workload_revision=agg.workload_revision,
        dns_auto_allow=True,
        port_source=agg.port_source,
        traffic_days=sorted(agg.traffic_days),
        flags=sorted(agg.flags),
    )


def build_observed_result(
    identity_key: IdentityKey,
    agg: TrafficAggregate,
    match: EdgeConfigMatch,
    decision: StatusDecision,
    score: Optional[float],
    snapshot_id: str,
    config: ScoringConfig,
    previous_status: Optional[DependencyStatus] = None,
    unstable_classification: bool = False,
    snapshot_time: Optional[str] = None,
) -> DependencyEdgeResult:
    flags = set(agg.flags) | set(match.flags)
    if agg.is_noise_only:
        flags.add("noise_only")
    if identity_key.port is None:
        # Cảnh báo sớm (chốt 22/10/2026): Tầng 1 vẫn chấm điểm bình thường — "có quan hệ
        # không" độc lập với "mở cửa nào"; port chỉ bắt buộc ở cửa ra (NEEDS_MANUAL_PORT).
        flags.add("port_unresolved")
    return DependencyEdgeResult(
        identity_key=identity_key,
        destination_service_ref=agg.destination_service_ref,
        scope=classify_scope(identity_key, agg.destination_service_ref, flags),
        traffic_class=agg.dominant_traffic_class(),
        count=agg.count,
        distinct_days=agg.distinct_days,
        error_ratio=agg.error_ratio,
        unstable=agg.error_ratio > config.error_ratio_threshold,
        config_finding=match.evidence.config_finding if match.evidence else None,
        score=score,
        dependency_status=decision.status,
        snapshot_id=snapshot_id,
        workload_revision=agg.workload_revision,
        previous_status=previous_status,
        unstable_classification=unstable_classification,
        needs_human_review=decision.needs_human_review,
        escalate_to_tier2=decision.escalate_to_tier2,
        escalation_reason=decision.escalation_reason,
        port_source=agg.port_source,
        traffic_days=sorted(agg.traffic_days),
        flags=sorted(flags),
        config_match_level=match.level,
        auto_allow_reason=decision.auto_allow_reason,
        snapshot_time=snapshot_time,
    )


def _pick_config_evidence(identity_key: IdentityKey, config_evidence_list: list[ConfigEvidenceRecord]):
    """Tương thích ngược: config đại diện cho 1 cạnh, theo đúng luật khớp mục 1b."""
    if not config_evidence_list:
        return None
    cand = Candidate(pair_key=identity_key.pair_key(), observed={identity_key: None},
                     config_evidence=sorted(config_evidence_list, key=lambda e: e.sort_key))
    return match_config_to_edges(cand).per_edge[identity_key].evidence


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_tier1(
    traffic_series: list[TrafficSeries],
    config_evidence: list[ConfigEvidenceRecord],
    snapshot_id: str,
    conn: Optional[sqlite3.Connection] = None,
    config: ScoringConfig = DEFAULT_CONFIG,
    selector_validator: Optional[SelectorValidator] = None,
    snapshot_time: Optional[str] = None,
) -> list[DependencyEdgeResult]:
    """Chạy toàn bộ Tier 1 cho MỘT snapshot. Có `conn` -> previous_status/hysteresis đọc
    từ lịch sử thật (chỉ snapshot TRƯỚC snapshot này) và kết quả được ghi SQLite; `conn=None`
    -> stateless (previous_status luôn None). `snapshot_time`: thời điểm của DỮ LIỆU (ISO),
    dùng sắp lịch sử và đếm số lần đổi trạng thái (mục 10b)."""

    selector_validator = selector_validator or MockSelectorValidator()
    prepared = prepare_snapshot(traffic_series, config_evidence, config)

    previous_status_map: dict[IdentityKey, DependencyStatus] = {}
    if conn is not None and prepared.traffic_aggregates:
        previous_status_map = storage.get_previous_status_map(
            conn, list(prepared.traffic_aggregates.keys()),
            current_snapshot_id=snapshot_id, before_snapshot_time=snapshot_time,
        )

    results: list[DependencyEdgeResult] = []

    for cand in prepared.candidates.values():
        if cand.is_observed:
            match = match_config_to_edges(cand)
            for identity_key, agg in cand.observed.items():
                m = match.per_edge[identity_key]
                finding = m.evidence.config_finding if m.evidence else None
                strength = CONFIG_MATCH_STRENGTH.get(finding, 0.0) if finding else 0.0
                features = compute_features(agg, None, strength, config, finding)
                score = score_edge(features, config)

                previous_status = previous_status_map.get(identity_key)
                if agg.is_noise_only:
                    decision = derive_status_noise_only(agg)  # Q2 — nhánh nhiễu (mục 5)
                else:
                    decision = derive_status_observed(agg, score, previous_status, config)

                recent_flips = (
                    storage.get_recent_flip_count(conn, identity_key, config.status_flip_window_days,
                                                  until_snapshot_time=snapshot_time)
                    if conn is not None else 0
                )
                result = build_observed_result(
                    identity_key, agg, m, decision, score, snapshot_id, config,
                    previous_status=previous_status,
                    unstable_classification=compute_unstable_classification(recent_flips, config),
                    snapshot_time=snapshot_time,
                )

                # mục 3b: kiểm selector CHỈ cho STRONG, trừ EXTERNAL (sinh ServiceEntry, không
                # dùng podSelector). Planner kiểm LẠI bắt buộc ngay trước khi sinh YAML.
                if result.dependency_status == DependencyStatus.OBSERVED_STRONG and result.scope != Scope.EXTERNAL:
                    validation = selector_validator.validate(result)
                    if not validation.passed:
                        manual = downgrade_to_manual_selector(validation.reason or "")
                        result.dependency_status = manual.status
                        result.needs_human_review = manual.needs_human_review
                        result.manual_selector_reason = validation.reason

                if (result.dependency_status == DependencyStatus.OBSERVED_STRONG
                        and result.count < config.low_count_review):
                    result.flags = sorted(set(result.flags) | {"low_count_strong"})

                results.append(result)

            results.extend(build_potential_only_results(match.leftovers, snapshot_id, snapshot_time))

        elif cand.is_potential_only:
            results.extend(build_potential_only_results(cand.config_evidence, snapshot_id, snapshot_time))

    for agg in prepared.dns_aggregates.values():
        dns_result = _build_dns_result(agg, config)
        dns_result.snapshot_id = snapshot_id
        dns_result.snapshot_time = snapshot_time
        results.append(dns_result)

    if conn is not None:
        storage.insert_results(conn, results)

    return results
