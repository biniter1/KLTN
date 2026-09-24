"""Tier2Config — các ngưỡng số, giá trị khởi tạo (TIER2_DESIGN.md mục 12).

Tất cả giá trị ở đây là "placeholder, chờ đo thật" theo đúng ghi chú trong
thiết kế — không phải giá trị đã kiểm định. `Tier2Config` là 1 dataclass bất
biến (frozen) để một phiên Brain không thể vô tình đổi ngưỡng giữa chừng.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tier2Config:
    # --- mục 12: bảng ngưỡng số, giá trị khởi tạo ---
    max_iterations: int = 8
    tool_call_timeout_s: float = 30.0
    session_timeout_s: float = 300.0  # 5 phút
    normalize_token_budget_ratio: float = 0.70
    max_open_hypotheses: int = 4
    max_tool_calls_per_round: int = 4
    max_consecutive_tool_errors: int = 2  # lỗi lần 2 liên tiếp -> không cho thử lần 3
    tool_output_char_cap: int = 4000

    # --- mục 2.1: policy cho tham số keyword ---
    keyword_max_length: int = 128
    # Chỉ cho chữ/số, khoảng trắng và một số ký tự an toàn trong tìm kiếm
    # văn bản (- _ . / cho path/keyword kỹ thuật). Không cho phép các ký tự
    # thường dùng trong path traversal hay command injection (; | & $ ` > < \ ~).
    keyword_allowed_charset: str = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 _.-/"
    )

    # --- mục 4: ước lượng ngân sách token (đơn giản hoá, không phụ thuộc
    # tokenizer thật của 1 nhà cung cấp cụ thể — xem README về giới hạn này) ---
    context_window_tokens: int = 200_000
    chars_per_token_estimate: float = 4.0

    # --- mục 7.5: staleness — evidence source-code/docs cũ hơn ngưỡng này
    # (so với ngày snapshot traffic của Tier 1) bị đánh dấu stale=true ---
    stale_threshold_days: int = 365

    @property
    def token_budget(self) -> int:
        return int(self.context_window_tokens * self.normalize_token_budget_ratio)
