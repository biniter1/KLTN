"""Validation — suy luận trạng thái hypothesis (mục 7.3) và 4 status cuối
(mục 7.4), thuần rule-based, không LLM.

Được gọi 2 nơi:
  - `Context Manager.build` (Builder ①/②) dùng `derive_hypothesis_status`
    để tính `HypothesisView.status` mỗi lần nhóm lại evidence.
  - `Brain`, sau Builder②, dùng `derive_final_statuses` để suy ra
    `(final_status, original_agent_status)` — chạy MỌI vòng, không chỉ lúc
    kết luận (mục 8.2).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Tuple

from .models import (
    EvidenceRow,
    FinalStatus,
    Hypothesis,
    HypothesisStatus,
    HypothesisView,
    STRUCTURED_TOOLS,
    Stance,
    ToolRowStatus,
)


def derive_hypothesis_status(evidence: List[EvidenceRow]) -> HypothesisStatus:
    """5 trạng thái hypothesis, thuần rule-based (mục 7.3)."""

    ok_evidence = [e for e in evidence if e.status == ToolRowStatus.OK.value]
    stances = {e.stance for e in ok_evidence if e.stance}
    has_support = Stance.SUPPORT.value in stances
    has_contradict = Stance.CONTRADICT.value in stances

    if has_support and has_contradict:
        return HypothesisStatus.CONFLICTED
    if has_support:
        return HypothesisStatus.SUPPORTED
    if has_contradict:
        return HypothesisStatus.CONTRADICTED
    if evidence:
        # Đã gọi tool liên quan (kể cả toàn ERROR / toàn chưa có stance) mà
        # không thấy gì cả 2 chiều -> INSUFFICIENT, bao gồm cả trường hợp
        # open-world "không tool nào chứng minh được" (mục 7.3).
        return HypothesisStatus.INSUFFICIENT
    return HypothesisStatus.PROPOSED


def distinct_evidence_sources(evidence: List[EvidenceRow]) -> int:
    """Đếm số NGUỒN độc lập (mục 7.5): PR description + source code cùng trỏ
    1 PR number tính là 1 nguồn, không phải 2. Chỉ dùng để hiển thị/thứ hạng,
    không bao giờ dùng để tính ra 1 con số confidence (mục 7.5)."""

    ok_evidence = [e for e in evidence if e.status == ToolRowStatus.OK.value]
    groups = set()
    for e in ok_evidence:
        if e.tool_name in ("read_pr_descriptions", "read_source_code") and e.pr_number:
            groups.add(("pr", e.pr_number))
        else:
            groups.add(("evidence", e.evidence_id))
    return len(groups)


def derive_final_statuses(
    hypotheses: List[HypothesisView],
) -> Tuple[str, str]:
    """(final_status, original_agent_status) theo mục 7.4.

    `original_agent_status` = trạng thái sẽ có nếu KHÔNG áp luật corroboration
    (mục 11.3: "tier2_original_status — trước khi bị corroboration hạ cấp,
    nếu có"). Model Output (mục 6) không có field tự báo status riêng, nên
    "trước khi hạ cấp" chính là kết quả naive trước bước gạn lọc bằng chứng
    cấu trúc — xem README phần diễn giải.
    """

    if not hypotheses:
        return FinalStatus.INSUFFICIENT_BASIS.value, FinalStatus.INSUFFICIENT_BASIS.value

    if any(h.status == HypothesisStatus.CONFLICTED for h in hypotheses):
        return FinalStatus.CONFLICTING_EVIDENCE.value, FinalStatus.CONFLICTING_EVIDENCE.value

    if all(h.status == HypothesisStatus.INSUFFICIENT for h in hypotheses):
        return FinalStatus.INSUFFICIENT_BASIS.value, FinalStatus.INSUFFICIENT_BASIS.value

    supported = [h for h in hypotheses if h.status == HypothesisStatus.SUPPORTED]
    if supported:
        original = FinalStatus.CONFIDENT.value
        if any(h.has_structured_support for h in supported):
            return FinalStatus.CONFIDENT.value, original
        return FinalStatus.NEEDS_MONITORING.value, original

    # Còn lại: trộn PROPOSED/CONTRADICTED/INSUFFICIENT, không có SUPPORTED
    # hay CONFLICTED nào -> chưa đủ cơ sở kết luận.
    return FinalStatus.INSUFFICIENT_BASIS.value, FinalStatus.INSUFFICIENT_BASIS.value
