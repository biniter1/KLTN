"""
Baseline đánh giá component-level — TIER1_DESIGN.md mục 13.

Câu hỏi nghiên cứu (mục 13, nguyên văn): "khác biệt precision/recall của Tier 1 có
thật sự đến từ logistic regression + hysteresis, hay phần lớn giá trị đã nằm sẵn ở
việc hợp nhất C_observed ∪ C_potential?" — nên B1/B2 CHỈ thay phần scoring/hysteresis
(mục 4, 5b), giữ nguyên 100% candidate discovery (mục 1), identity resolution (mục 3),
và luật escalation cho nhánh potential-only (mục 5) so với Tier 1 thật.

- **B1** (mục 13.1, baseline ĐỐI NGOẠI — mô phỏng Kubescape/Otterize dạng đơn giản):
  `count > 0` → `OBSERVED_STRONG` thẳng, bỏ qua hoàn toàn sigmoid/hysteresis/traffic_class.
  Vì `C_observed` (mục 1, `candidates.py`) đã CHỈ gồm cặp `count > 0` theo định nghĩa,
  một candidate "observed" dưới B1 LUÔN LUÔN là `OBSERVED_STRONG` — không có nhánh nào khác.

- **B2** (mục 13.2, ABLATION NỘI BỘ — ngưỡng cứng cũ của chính đồ án, KHÔNG đại diện
  công cụ bên ngoài nào): `count >= 20 AND distinct_days >= 3` → `OBSERVED_STRONG` với
  `score` gán cứng `0.9`; ngược lại xử lý NHƯ NOT_OBSERVED/escalate theo đúng mục 5
  (`status.derive_status_potential_only`), dùng `config_finding` của chính cặp đó nếu
  Config Evidence Collector có bắt được (rỗng nếu không).

Cả 2: cặp CHỈ có trong C_potential (chưa từng có traffic) dùng lại NGUYÊN VẸN
`pipeline._build_potential_only_result` — mục 13.1/13.2 nói rõ "phần không đổi này
không làm nhiễu phép so sánh". KHÔNG chạy selector validation (mục 3b, `NEEDS_MANUAL_SELECTOR`)
— đó là bước validate trước khi sinh YAML thật, nằm ngoài câu hỏi "scoring/hysteresis tạo
ra khác biệt gì" mà mục 13 đặt ra; B1/B2 không có khái niệm NEEDS_MANUAL_SELECTOR.

NOTE — điểm mở/diễn giải: mục 13.3 nói "Metric: precision, recall, F1" nhưng không nêu
chính xác điều gì được tính là "dự đoán dương" trong số 5 trạng thái đầu ra. Vì nghiên
cứu ở đây hẹp vào riêng quyết định `OBSERVED_STRONG` (biến DUY NHẤT khác nhau giữa B1/B2/
Tier 1 thật ở cấp component, đúng câu hỏi nghiên cứu nêu ở đầu mục 13), `evaluation.py`
định nghĩa "dự đoán dương" = `dependency_status == OBSERVED_STRONG`. Các trạng thái escalate
(WEAK/UNKNOWN/NOT_OBSERVED) không tính là dương hay âm ở phép so sánh này, vì cả 3 biến thể
đều đẩy chúng cho bước xử lý SAU (Tier 2/con người), không phải kết luận cuối của component.
"""

from __future__ import annotations

from typing import Literal

from .aggregation import TrafficAggregate, aggregate_series
from .candidates import Candidate, discover_candidates
from .classification import classify_all
from .config import DEFAULT_CONFIG, ScoringConfig
from .dns_external import classify_scope, is_dns_edge
from .pipeline import _build_dns_result, _build_potential_only_result, _pick_config_evidence
from .schema import (
    ConfigEvidenceRecord,
    DependencyEdgeResult,
    DependencyStatus,
    TrafficSeries,
)
from .status import StatusDecision, derive_status_potential_only

Variant = Literal["B1", "B2"]


def _decide_observed(
    variant: Variant,
    agg: TrafficAggregate,
    config_finding,
) -> tuple[StatusDecision, float]:
    """Trả (quyết định trạng thái, score gán cho báo cáo). `agg.count > 0` LUÔN đúng ở
    đây vì hàm chỉ được gọi cho candidate `is_observed` (đảm bảo bởi `candidates.py`)."""
    if variant == "B1":
        # mục 13.1: count>0 -> STRONG thẳng, không có nhánh khác cho observed candidate.
        return (
            StatusDecision(status=DependencyStatus.OBSERVED_STRONG, escalate_to_tier2=False, escalation_reason=None),
            1.0,  # B1 không có khái niệm score liên tục — 1.0 đánh dấu "luật naive kết luận chắc"
        )

    if variant == "B2":
        meets_threshold = agg.count >= 20 and agg.distinct_days >= 3
        if meets_threshold:
            return (
                StatusDecision(status=DependencyStatus.OBSERVED_STRONG, escalate_to_tier2=False, escalation_reason=None),
                0.9,  # mục 13.2: confidence gán cứng
            )
        # Dưới ngưỡng -> "xử lý như NOT_OBSERVED/escalate theo đúng mục 5" (mục 13.2)
        findings = [config_finding] if config_finding is not None else []
        return derive_status_potential_only(findings), None

    raise ValueError(f"variant không hợp lệ: {variant!r} (chỉ nhận 'B1'/'B2')")


def _run_component_variant(
    variant: Variant,
    traffic_series: list[TrafficSeries],
    config_evidence: list[ConfigEvidenceRecord],
    snapshot_id: str,
    config: ScoringConfig = DEFAULT_CONFIG,
) -> list[DependencyEdgeResult]:
    # 1-3) Giống hệt run_tier1 (mục 2, 7) — KHÔNG phải phần đang được so sánh ở mục 13.
    classify_all(traffic_series, config)
    dns_series = [s for s in traffic_series if is_dns_edge(s.identity_key(), config)]
    normal_series = [s for s in traffic_series if not is_dns_edge(s.identity_key(), config)]
    traffic_aggregates = aggregate_series(normal_series)
    dns_aggregates = aggregate_series(dns_series)

    # 4) discover_candidates — mục 1, giữ nguyên 100%
    candidates = discover_candidates(traffic_aggregates, config_evidence)

    results: list[DependencyEdgeResult] = []

    for cand in candidates.values():
        if cand.is_observed:
            for identity_key, agg in cand.observed.items():
                ev = _pick_config_evidence(identity_key, cand.config_evidence)
                config_finding = ev.config_finding if ev else None

                decision, score = _decide_observed(variant, agg, config_finding)

                results.append(
                    DependencyEdgeResult(
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
                        # B1/B2 không dùng hysteresis (mục 13.1/13.2: "bỏ qua hoàn toàn... hysteresis")
                        previous_status=None,
                        unstable_classification=False,
                        needs_human_review=decision.needs_human_review,
                        escalate_to_tier2=decision.escalate_to_tier2,
                        escalation_reason=decision.escalation_reason,
                        # B1/B2 không có khái niệm NEEDS_MANUAL_SELECTOR (mục 3b không chạy ở đây)
                    )
                )

        elif cand.is_potential_only:
            # Y HỆT Tier 1 thật — "phần không đổi không làm nhiễu phép so sánh" (mục 13.1/13.2)
            results.append(_build_potential_only_result(cand, snapshot_id))

    for agg in dns_aggregates.values():
        dns_result = _build_dns_result(agg, config)
        dns_result.snapshot_id = snapshot_id
        results.append(dns_result)

    return results


def run_baseline_b1(
    traffic_series: list[TrafficSeries],
    config_evidence: list[ConfigEvidenceRecord],
    snapshot_id: str,
    config: ScoringConfig = DEFAULT_CONFIG,
) -> list[DependencyEdgeResult]:
    """mục 13.1 — "có traffic là tin" (naive presence rule), baseline đối ngoại."""
    return _run_component_variant("B1", traffic_series, config_evidence, snapshot_id, config)


def run_baseline_b2(
    traffic_series: list[TrafficSeries],
    config_evidence: list[ConfigEvidenceRecord],
    snapshot_id: str,
    config: ScoringConfig = DEFAULT_CONFIG,
) -> list[DependencyEdgeResult]:
    """mục 13.2 — ngưỡng cứng cũ (`count>=20 AND distinct_days>=3`), ablation nội bộ."""
    return _run_component_variant("B2", traffic_series, config_evidence, snapshot_id, config)
