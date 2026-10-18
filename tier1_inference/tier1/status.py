"""
Suy ra dependency_status (5 trạng thái) — TIER1_DESIGN.md mục 5 và 5b.

Hai nhánh tách biệt hoàn toàn (đúng "Nhánh riêng cho NOT_OBSERVED... không đi qua lại
công thức sigmoid"):

  - derive_status_observed()      : cặp có traffic (count>0) -> áp hysteresis (mục 5b)
  - derive_status_potential_only(): cặp KHÔNG có traffic (count=0), chỉ có config evidence
                                     -> KHÔNG bao giờ chạm sigmoid, rẽ theo config_finding (mục 5)

NEEDS_MANUAL_SELECTOR không quyết định ở đây — nó là kết quả của selector_validation.py
chạy SAU khi đã có OBSERVED_STRONG (mục 3b: "Validation bắt buộc tại thời điểm sinh YAML").
status.py chỉ cung cấp downgrade_to_manual_selector() để pipeline.py gọi khi cần.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .aggregation import TrafficAggregate
from .config import ScoringConfig, DEFAULT_CONFIG
from .schema import ConfigFinding, DependencyStatus, TrafficClass


@dataclass
class StatusDecision:
    status: DependencyStatus
    escalate_to_tier2: bool
    escalation_reason: Optional[str]
    needs_human_review: bool = False
    auto_allow_reason: Optional[str] = None


# Escalation reason lookup — tham chiếu TIER2_DESIGN.md mục 8.2 (bảng escalation_reason)
ESCALATION_REASON = {
    DependencyStatus.OBSERVED_WEAK: (
        "OBSERVED_WEAK: có traffic thật nhưng score dưới theta_promote — "
        "chưa đủ tin cậy để Tier 1 tự kết luận"
    ),
    DependencyStatus.UNKNOWN: (
        "UNKNOWN: traffic_class của edge không rõ ràng/mâu thuẫn — "
        "Tier 1 không đủ căn cứ phân loại là BUSINESS hay noise"
    ),
    "NOT_OBSERVED_BLIND": (
        "NOT_OBSERVED: không có traffic (count=0) và Config Evidence Collector cũng "
        "không tìm được tham chiếu nào (FOUND_UNRESOLVED/NOT_FOUND) — mù hoàn toàn cả "
        "hai nguồn bằng chứng"
    ),
}


def _below_count_floor(agg: TrafficAggregate, config: ScoringConfig) -> bool:
    """mục 4a — count_floor tùy chọn (mặc định tắt): traffic quá thấp coi tương đương count=0
    khi xét OBSERVED_STRONG. Đây là 'câu hỏi mở, chưa chốt' trong tài liệu -> implement optional,
    off-by-default, để không tự ý áp một ngưỡng chưa được xác nhận."""
    if config.count_floor is None:
        return False
    min_count, min_distinct_days = config.count_floor
    return agg.count < min_count or agg.distinct_days < min_distinct_days


def is_class_ambiguous(agg: TrafficAggregate, config: ScoringConfig = DEFAULT_CONFIG) -> bool:
    """Heuristic cho 'traffic_class mâu thuẫn/không rõ' (mục 5) — tài liệu không cho số cụ
    thể, chọn: tỷ trọng traffic KHÔNG khớp rule phân loại nào (UNKNOWN) vượt ngưỡng cấu hình."""
    return agg.class_ratio(TrafficClass.UNKNOWN) >= config.unknown_class_ratio_threshold


def apply_hysteresis(
    score: float,
    previous_status: Optional[DependencyStatus],
    config: ScoringConfig = DEFAULT_CONFIG,
) -> DependencyStatus:
    """Ngưỡng kép bất đối xứng — mục 5b.

        Nếu đang KHÔNG phải STRONG: chỉ chuyển STRONG khi score >= theta_promote
        Nếu đang LÀ STRONG:         chỉ bị hạ khỏi STRONG khi score < theta_demote
        score trong [theta_demote, theta_promote) -> giữ nguyên trạng thái hiện tại (vùng chết)

    Chỉ trả OBSERVED_STRONG hoặc OBSERVED_WEAK (NEEDS_MANUAL_SELECTOR áp sau, ở pipeline).
    """
    was_strong = previous_status == DependencyStatus.OBSERVED_STRONG

    if was_strong:
        is_strong_now = score >= config.theta_demote
    else:
        is_strong_now = score >= config.theta_promote

    return DependencyStatus.OBSERVED_STRONG if is_strong_now else DependencyStatus.OBSERVED_WEAK


def derive_status_observed(
    agg: TrafficAggregate,
    score: float,
    previous_status: Optional[DependencyStatus],
    config: ScoringConfig = DEFAULT_CONFIG,
) -> StatusDecision:
    """Cặp có traffic thật (count>0). Luật cứng mục 4a: count>0 là điều kiện CẦN cho
    OBSERVED_STRONG (đã đảm bảo bởi việc hàm này chỉ được gọi khi count>0) — nhưng
    KHÔNG phải điều kiện ĐỦ: vẫn phải qua hysteresis + không được ambiguous class."""

    if is_class_ambiguous(agg, config):
        return StatusDecision(
            status=DependencyStatus.UNKNOWN,
            escalate_to_tier2=True,
            escalation_reason=ESCALATION_REASON[DependencyStatus.UNKNOWN],
        )

    effective_count_zero = _below_count_floor(agg, config)
    if effective_count_zero:
        # Dưới count_floor -> coi như chưa từng có traffic đáng kể, không cho lên STRONG
        # dù score cao (vì score có thể bị config_match_strength "cân" hộ — mục 4a lo ngại).
        capped_status = apply_hysteresis(min(score, config.theta_demote - 0.01), previous_status, config)
    else:
        capped_status = apply_hysteresis(score, previous_status, config)

    if capped_status == DependencyStatus.OBSERVED_STRONG:
        return StatusDecision(
            status=DependencyStatus.OBSERVED_STRONG,
            escalate_to_tier2=False,
            escalation_reason=None,
        )

    return StatusDecision(
        status=DependencyStatus.OBSERVED_WEAK,
        escalate_to_tier2=True,
        escalation_reason=ESCALATION_REASON[DependencyStatus.OBSERVED_WEAK],
    )


def derive_status_noise_only(agg: TrafficAggregate) -> StatusDecision:
    """Cạnh chỉ toàn nhiễu (mục 5 nhánh nhiễu, chốt 15/10): có traffic nhưng không có
    request BUSINESS nào. Traffic nhiễu VẪN là traffic thật cần được cho phép (chặn
    Prometheus = mất giám sát, chặn healthcheck = service bị coi là chết), nhưng gọi
    Tier 2 thì tốn chi phí AI chỉ để kết luận lại "đây là healthcheck":

      - toàn MONITORING đã xác minh  -> Planner tự mở (auto_allow_reason=VERIFIED_MONITORING)
      - còn lại (healthcheck, control plane, monitoring chưa xác minh) -> người duyệt

    Trạng thái giữ OBSERVED_WEAK (không tính là "tự kết luận" khi đánh giá), không escalate."""
    only_verified_monitoring = (
        agg.class_counts.get(TrafficClass.MONITORING, 0) == agg.count
        and "claimed_monitoring" not in agg.flags
    )
    if only_verified_monitoring:
        return StatusDecision(
            status=DependencyStatus.OBSERVED_WEAK,
            escalate_to_tier2=False,
            escalation_reason=None,
            needs_human_review=False,
            auto_allow_reason="VERIFIED_MONITORING",
        )
    return StatusDecision(
        status=DependencyStatus.OBSERVED_WEAK,
        escalate_to_tier2=False,
        escalation_reason=None,
        needs_human_review=True,
    )


def derive_status_potential_only(
    config_findings: list[ConfigFinding],
) -> StatusDecision:
    """Cặp KHÔNG có traffic (count=0), chỉ tồn tại nhờ config evidence (C_potential) — mục 5.

    Không đi qua sigmoid. Rẽ nhánh THUẦN TÚY theo config_finding:
      - FOUND_STRUCTURAL/FOUND_EMBEDDED sạch -> hàng đợi review người, KHÔNG escalate Tier 2,
        KHÔNG tự enforce.
      - FOUND_UNRESOLVED/NOT_FOUND -> escalate Tier 2 (mù cả traffic lẫn config).
    """
    has_clean_match = any(f.is_clean_match for f in config_findings)

    if has_clean_match:
        return StatusDecision(
            status=DependencyStatus.NOT_OBSERVED,
            escalate_to_tier2=False,
            escalation_reason=None,
            needs_human_review=True,
        )

    return StatusDecision(
        status=DependencyStatus.NOT_OBSERVED,
        escalate_to_tier2=True,
        escalation_reason=ESCALATION_REASON["NOT_OBSERVED_BLIND"],
        needs_human_review=False,
    )


def compute_unstable_classification(recent_flip_count: int, config: ScoringConfig = DEFAULT_CONFIG) -> bool:
    """mục 5b: >=3 lần đổi trạng thái / 7 ngày dù đã có hysteresis -> gắn cờ cho người review.
    recent_flip_count do storage.py tính từ lịch sử previous_status theo thời gian
    (pipeline chạy 1-2 lần trong demo này nên giá trị này thường là 0 — cơ chế vẫn đầy đủ
    để dùng khi chạy nhiều snapshot thật)."""
    return recent_flip_count >= config.max_status_flips_per_window


def downgrade_to_manual_selector(reason: str) -> StatusDecision:
    """Gọi từ pipeline.py khi OBSERVED_STRONG không qua được validation mục 3b."""
    return StatusDecision(
        status=DependencyStatus.NEEDS_MANUAL_SELECTOR,
        escalate_to_tier2=False,
        escalation_reason=None,
        needs_human_review=True,
    )
