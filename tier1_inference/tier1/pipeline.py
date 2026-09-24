"""
Orchestrator — nối toàn bộ các bước thành `run_tier1()`, đúng thứ tự mô tả xuyên suốt
TIER1_DESIGN.md:

    classify (mục 2)
      -> tách DNS (mục 7)
      -> aggregate lên cấp edge
      -> discover_candidates (mục 1)
      -> với mỗi candidate:
           observed  -> scoring (mục 4/4a) -> hysteresis (mục 5b) -> selector validation (mục 3b)
           potential -> nhánh NOT_OBSERVED thuần túy (mục 5), không qua sigmoid
      -> persist SQLite (mục 10/11)

Đây là nơi DUY NHẤT "biết" toàn bộ thứ tự nghiệp vụ — mọi module khác (scoring,
status, classification...) đều là hàm thuần túy không phụ thuộc lẫn nhau, dễ test
riêng lẻ và dễ thay thế từng phần (điểm flex).
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from . import storage
from .aggregation import TrafficAggregate, aggregate_series
from .candidates import discover_candidates
from .classification import classify_all
from .config import ScoringConfig, DEFAULT_CONFIG
from .dns_external import classify_scope, is_dns_edge
from .schema import (
    ConfigEvidenceRecord,
    DependencyEdgeResult,
    DependencyStatus,
    IdentityKey,
    Scope,
    TrafficClass,
    TrafficSeries,
)
from .scoring import CONFIG_MATCH_STRENGTH, compute_features, percentile_95, score_edge
from .selector_validation import MockSelectorValidator, SelectorValidator
from .status import (
    compute_unstable_classification,
    derive_status_observed,
    derive_status_potential_only,
    downgrade_to_manual_selector,
)


def _pick_config_evidence(
    identity_key: IdentityKey,
    config_evidence_list: list[ConfigEvidenceRecord],
) -> Optional[ConfigEvidenceRecord]:
    """Chọn 1 config evidence 'đại diện' cho identity_key cụ thể (đã có protocol/port,
    vì đây là nhánh observed) trong danh sách config evidence của cả CẶP (source,dest)
    -- vì C_potential (mục 1) chỉ khớp theo cặp, không theo protocol/port.

    Ưu tiên: khớp đúng protocol+port trước, rồi ưu tiên finding "sạch"
    (FOUND_STRUCTURAL/FOUND_EMBEDDED) trước FOUND_UNRESOLVED/NOT_FOUND."""
    if not config_evidence_list:
        return None

    exact = [
        ev for ev in config_evidence_list
        if ev.protocol and ev.protocol.upper() == identity_key.protocol.upper()
        and ev.port == identity_key.port
    ]
    pool = exact if exact else config_evidence_list

    clean = [ev for ev in pool if ev.config_finding.is_clean_match]
    chosen_pool = clean if clean else pool
    return chosen_pool[0]


def _pick_config_evidence_for_potential(
    config_evidence_list: list[ConfigEvidenceRecord],
) -> ConfigEvidenceRecord:
    """Cho nhánh potential-only (mục 5): ưu tiên finding sạch nhất để đại diện cho cả cặp."""
    clean = [ev for ev in config_evidence_list if ev.config_finding.is_clean_match]
    return clean[0] if clean else config_evidence_list[0]


def _build_dns_result(
    agg: TrafficAggregate,
    config: ScoringConfig,
) -> DependencyEdgeResult:
    """mục 7 — DNS auto-allow, tách hẳn khỏi 5-state chính thức. Vẫn ghi dependency_status
    (bắt buộc theo schema) để nhất quán khi query, nhưng dns_auto_allow=True mới là cờ
    quyết định enforcement thật (luôn allow, không phụ thuộc dependency_status)."""
    return DependencyEdgeResult(
        identity_key=agg.identity_key,
        destination_service_ref=agg.destination_service_ref,
        scope=classify_scope(agg.identity_key, agg.destination_service_ref),
        traffic_class=TrafficClass.DNS,
        count=agg.count,
        distinct_days=agg.distinct_days,
        error_ratio=agg.error_ratio,
        unstable=agg.error_ratio > config.error_ratio_threshold,
        config_finding=None,
        score=None,
        dependency_status=DependencyStatus.OBSERVED_STRONG,  # informational — xem docstring
        snapshot_id="",  # gán ở run_tier1 sau khi biết snapshot_id
        workload_revision=agg.workload_revision,
        dns_auto_allow=True,
    )


def run_tier1(
    traffic_series: list[TrafficSeries],
    config_evidence: list[ConfigEvidenceRecord],
    snapshot_id: str,
    conn: Optional[sqlite3.Connection] = None,
    config: ScoringConfig = DEFAULT_CONFIG,
    selector_validator: Optional[SelectorValidator] = None,
) -> list[DependencyEdgeResult]:
    """Chạy toàn bộ Tier 1 cho MỘT snapshot. Nếu `conn` được truyền vào, kết quả được
    persist vào SQLite và previous_status/hysteresis được đọc từ lịch sử thật; nếu
    `conn=None`, pipeline chạy "stateless" (previous_status luôn None — hữu ích cho
    unit test một snapshot đơn lẻ)."""

    selector_validator = selector_validator or MockSelectorValidator()

    # 1) Phân loại traffic_class ở cấp series (mục 2)
    classify_all(traffic_series, config)

    # 2) Tách DNS ra khỏi luồng scoring chính (mục 7)
    dns_series = [s for s in traffic_series if is_dns_edge(s.identity_key(), config)]
    normal_series = [s for s in traffic_series if not is_dns_edge(s.identity_key(), config)]

    # 3) Gộp lên cấp edge
    traffic_aggregates = aggregate_series(normal_series)
    dns_aggregates = aggregate_series(dns_series)

    # 4) discover_candidates — C = C_observed ∪ C_potential (mục 1)
    candidates = discover_candidates(traffic_aggregates, config_evidence)

    # 5) previous_status cho hysteresis (mục 5b) — chỉ cần cho edge có traffic thật
    previous_status_map: dict[IdentityKey, DependencyStatus] = {}
    if conn is not None and traffic_aggregates:
        previous_status_map = storage.get_previous_status_map(conn, list(traffic_aggregates.keys()))

    # 6) EXPECTED_MAX cho log_volume — percentile_95 trên TOÀN snapshot (mục 4), không tính DNS
    expected_max_count = percentile_95([agg.count for agg in traffic_aggregates.values()])

    results: list[DependencyEdgeResult] = []

    for pair_key, cand in candidates.items():
        if cand.is_observed:
            for identity_key, agg in cand.observed.items():
                ev = _pick_config_evidence(identity_key, cand.config_evidence)
                config_finding = ev.config_finding if ev else None
                config_match_strength = CONFIG_MATCH_STRENGTH.get(config_finding, 0.0) if config_finding else 0.0

                features = compute_features(agg, expected_max_count, config_match_strength, config)
                score = score_edge(features, config)

                previous_status = previous_status_map.get(identity_key)
                decision = derive_status_observed(agg, score, previous_status, config)

                recent_flips = (
                    storage.get_recent_flip_count(conn, identity_key, config.status_flip_window_days)
                    if conn is not None else 0
                )
                unstable_classification = compute_unstable_classification(recent_flips, config)

                result = DependencyEdgeResult(
                    identity_key=identity_key,
                    destination_service_ref=agg.destination_service_ref,
                    scope=classify_scope(identity_key, agg.destination_service_ref),
                    traffic_class=agg.dominant_traffic_class(),
                    count=agg.count,
                    distinct_days=agg.distinct_days,
                    error_ratio=agg.error_ratio,
                    unstable=agg.error_ratio > config.error_ratio_threshold,
                    config_finding=config_finding,
                    score=score,
                    dependency_status=decision.status,
                    snapshot_id=snapshot_id,
                    workload_revision=agg.workload_revision,
                    previous_status=previous_status,
                    unstable_classification=unstable_classification,
                    needs_human_review=decision.needs_human_review,
                    escalate_to_tier2=decision.escalate_to_tier2,
                    escalation_reason=decision.escalation_reason,
                )

                # mục 3b: validation CHỈ chạy cho OBSERVED_STRONG, ngay trước khi "cho phép enforce".
                # Loại trừ scope=EXTERNAL: mục 8 nói rõ EXTERNAL sinh ServiceEntry, không phải
                # NetworkPolicy podSelector, nên khái niệm "selector rỗng/overlap" của mục 3b không
                # áp dụng — validate nhầm sẽ luôn fail (không có destination_service_ref) và hạ oan
                # xuống NEEDS_MANUAL_SELECTOR. scope=UNKNOWN vẫn cho qua validate (sẽ fail đúng nghĩa
                # do thiếu service_ref) vì đó là ambiguity thật cần người xem, không phải lỗi thiết kế.
                if result.dependency_status == DependencyStatus.OBSERVED_STRONG and result.scope != Scope.EXTERNAL:
                    validation = selector_validator.validate(result)
                    if not validation.passed:
                        manual = downgrade_to_manual_selector(validation.reason or "")
                        result.dependency_status = manual.status
                        result.needs_human_review = manual.needs_human_review
                        result.manual_selector_reason = validation.reason

                results.append(result)

        elif cand.is_potential_only:
            ev = _pick_config_evidence_for_potential(cand.config_evidence)
            decision = derive_status_potential_only([e.config_finding for e in cand.config_evidence])

            # identity_key gần đúng: protocol/port có thể chưa biết (mục 1: C_potential chỉ có (source,dest))
            identity_key = IdentityKey(
                source_namespace=ev.source_namespace,
                source_canonical_service=ev.source_canonical_service,
                source_service_account=ev.source_service_account,
                destination_namespace=ev.destination_namespace,
                destination_canonical_service=ev.destination_canonical_service,
                protocol=(ev.protocol or "UNKNOWN"),
                port=ev.port,
            )

            result = DependencyEdgeResult(
                identity_key=identity_key,
                destination_service_ref=None,  # chưa có traffic -> chưa cần resolve Service thật (mục 3b chỉ chạy cho STRONG)
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
            )
            results.append(result)

    # DNS — auto-allow, ghi nhận riêng (mục 7)
    for agg in dns_aggregates.values():
        dns_result = _build_dns_result(agg, config)
        dns_result.snapshot_id = snapshot_id
        results.append(dns_result)

    if conn is not None:
        storage.insert_results(conn, results)

    return results
