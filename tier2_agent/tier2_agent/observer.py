"""Observer — wrapper bắt buộc, KHÔNG PHẢI tool (TIER2_DESIGN.md mục 9.2).

`Observer.check(state, trace_log) -> CONTINUE/STOP` là 1 hàm thuần —
`check()` dưới đây nhận sẵn các số liệu đã tính (Brain là nơi tính từ
`Storage`, xem `models.LiveCounts`) để module này không phụ thuộc trực tiếp
vào SQLite, dễ test bằng số liệu giả lập.

Thứ tự ưu tiên khi nhiều điều kiện cùng đúng 1 lúc (chỉ ảnh hưởng nhãn
`stopped_by`/`stop_reason` hiển thị, không ảnh hưởng việc CÓ dừng hay
không): policy cứng (manual abort, iteration cap, timeout) được ưu tiên
trước "cạn tool", rồi mới tới model tự `conclude` — nhất quán với nguyên
tắc "Observer chạy vô điều kiện, không phụ thuộc Model đề xuất gì" (mục 9.2).
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Tier2Config
from .models import ModelAction, ObserverDecision, StoppedBy


@dataclass
class ObserverInputs:
    iteration_count: int
    elapsed_ms: float
    manual_abort_flag: bool
    made_progress_this_round: bool
    model_action: str


def check(inputs: ObserverInputs, config: Tier2Config) -> ObserverDecision:
    if inputs.manual_abort_flag:
        return ObserverDecision(True, StoppedBy.OBSERVER_POLICY.value, "manual abort")

    if inputs.iteration_count >= config.max_iterations:
        return ObserverDecision(True, StoppedBy.OBSERVER_POLICY.value, "max_iterations exceeded")

    if inputs.elapsed_ms >= config.session_timeout_s * 1000:
        return ObserverDecision(True, StoppedBy.OBSERVER_POLICY.value, "session timeout exceeded")

    if inputs.model_action == ModelAction.CALL_TOOL.value and not inputs.made_progress_this_round:
        # "Cạn tool" (mục 9.1) — NOTE (README "Điểm mở/diễn giải"): thiết kế
        # không đặc tả thuật toán chính xác để phát hiện "hết tool để gọi".
        # Cài đặt này diễn giải: 1 vòng mà Model xin gọi tool nhưng KHÔNG có
        # tool_call mới nào thật sự được dispatch (mọi yêu cầu đều bị
        # dedup/từ chối) nghĩa là vòng lặp không thể tiến triển thêm.
        return ObserverDecision(
            True, StoppedBy.OBSERVER_POLICY.value, "no new tool calls available (tools exhausted)"
        )

    if inputs.model_action == ModelAction.CONCLUDE.value:
        return ObserverDecision(True, StoppedBy.MODEL.value, None)

    return ObserverDecision(False)
