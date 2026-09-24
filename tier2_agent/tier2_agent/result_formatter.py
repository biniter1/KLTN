"""Result Formatter (TIER2_DESIGN.md mục 4 bảng, mục 8.2).

LUÔN chạy mỗi vòng, bất kể dừng hay tiếp tục — đây là cách dashboard xem
được tiến trình suy luận real-time (mục 8.2). Output có cùng 1 shape dù là
"Valid result" (đang tiếp tục, không persist) hay "Final Result" (đã dừng,
persist) — chỉ khác ở field `persisted`.
"""

from __future__ import annotations

from typing import Dict, Optional

from .models import HypothesisView, ModelOutput, ObserverDecision, RoundResult, SessionState
from .validation import derive_final_statuses


def format_result(
    state: SessionState,
    views: Dict[str, HypothesisView],
    iteration: int,
    decision: ObserverDecision,
    model_output: ModelOutput,
) -> RoundResult:
    hyp_list = list(views.values())
    final_status, original_status = derive_final_statuses(hyp_list)

    return RoundResult(
        run_id=state.run_id,
        iteration=iteration,
        hypotheses=hyp_list,
        final_status=final_status,
        original_agent_status=original_status,
        action=model_output.action,
        stopped=decision.stop,
        stopped_by=decision.stopped_by,
        stop_reason=decision.stop_reason,
        persisted=decision.stop,
        reasoning=model_output.reasoning,
    )
