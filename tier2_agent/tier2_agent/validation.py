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
    Polarity,
    STRUCTURED_TOOLS,
    Stance,
    ToolRowStatus,
)


def derive_hypothesis_status(evidence: List[EvidenceRow]) -> HypothesisStatus:
    """5 trạng thái hypothesis, thuần rule-based (mục 7.3)."""

    ok_evidence = [e for e in evidence if e.status == ToolRowStatus.OK.value]
    # 05/11: stance của kết quả "không tìm thấy" bị bỏ qua (lớp chặn thứ 2, sau Brain)
    stances = {e.stance for e in ok_evidence if e.stance and not e.no_match}
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
    *,
    tier1_observed: bool = False,
) -> Tuple[str, str]:
    """(final_status, original_agent_status) theo mục 7.4 — bản sửa 05/11 (F1 + F2).

    F1 — hypothesis có chiều (`polarity`):
      * pro được ủng hộ + con được ủng hộ -> CONFLICTING_EVIDENCE
      * chỉ con được ủng hộ (cạnh bị bác)  -> REJECTED_WITH_EVIDENCE (đề xuất loại, admin duyệt;
        trước 05/11 chiều này trả INSUFFICIENT_BASIS)
      * chỉ pro được ủng hộ                -> ứng viên CONFIDENT, xét corroboration
      Trước đây MỌI hypothesis SUPPORTED đều tính là xác nhận cạnh, kể cả hypothesis
      phản bác ("traffic chỉ nhất thời") -> FP CONFIDENT (benchmark 04/11).

    F2 — corroboration = HAI NGUỒN ĐỘC LẬP cho cùng kết luận "cạnh là dependency":
      * nguồn runtime: Tầng 1 đã thấy traffic thật (`tier1_observed`, count>0) HOẶC
        read_extended_traffic_window được gán support trong 1 hypothesis pro;
      * nguồn tĩnh: code/tài liệu/PR được gán support trong 1 hypothesis pro.
      Đủ cả hai -> CONFIDENT; thiếu một -> NEEDS_MONITORING. Bản cũ bắt buộc bằng
      chứng traffic, mâu thuẫn với chính nhiệm vụ của Tầng 2 (cạnh traffic THƯA): 26/45
      phiên dương tìm đúng code thật bị hạ xuống NEEDS_MONITORING.

    `original_agent_status` = trạng thái trước bước corroboration (mục 11.3).
    """

    if not hypotheses:
        return FinalStatus.INSUFFICIENT_BASIS.value, FinalStatus.INSUFFICIENT_BASIS.value

    if any(h.status == HypothesisStatus.CONFLICTED for h in hypotheses):
        return FinalStatus.CONFLICTING_EVIDENCE.value, FinalStatus.CONFLICTING_EVIDENCE.value

    supported = [h for h in hypotheses if h.status == HypothesisStatus.SUPPORTED]
    pro = [h for h in supported if h.polarity != Polarity.CON.value]
    con = [h for h in supported if h.polarity == Polarity.CON.value]

    if pro and con:
        return FinalStatus.CONFLICTING_EVIDENCE.value, FinalStatus.CONFLICTING_EVIDENCE.value
    if con and not pro:
        return FinalStatus.REJECTED_WITH_EVIDENCE.value, FinalStatus.REJECTED_WITH_EVIDENCE.value
    if pro:
        original = FinalStatus.CONFIDENT.value
        runtime = tier1_observed or any(h.has_structured_support for h in pro)
        static = any(h.has_static_support for h in pro)
        if runtime and static:
            return FinalStatus.CONFIDENT.value, original
        return FinalStatus.NEEDS_MONITORING.value, original

    # Không hypothesis nào được ủng hộ (trộn PROPOSED/CONTRADICTED/INSUFFICIENT) -> chưa đủ
    # cơ sở kết luận theo chiều nào.
    return FinalStatus.INSUFFICIENT_BASIS.value, FinalStatus.INSUFFICIENT_BASIS.value
