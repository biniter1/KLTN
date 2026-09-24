"""Ngoại lệ dùng nội bộ Tier 2.

Mọi từ chối (input không hợp lệ, tham chiếu không tồn tại, lỗi tool) đều đi
qua các exception có tên rõ ràng ở đây — Brain bắt và biến thành phản hồi
tường minh cho Model ở vòng sau, không bao giờ nuốt lỗi âm thầm (nguyên tắc
xuyên suốt TIER2_DESIGN.md).
"""


class Tier2Error(Exception):
    """Lớp gốc cho mọi lỗi Tier 2."""


class InvalidEscalationError(Tier2Error):
    """Escalation record không thỏa điều kiện kích hoạt Tier 2 (mục 0)."""


class ToolInputRejected(Tier2Error):
    """Input của 1 tool call bị Tool Registry từ chối (mục 2.1)."""


class ToolExecutionError(Tier2Error):
    """1 tool chạy lỗi (timeout/network/không đọc được) — mục 9.3."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class ReferenceValidationError(Tier2Error):
    """`evidence_id`/`hypothesis_id` Model tham chiếu không tồn tại (mục 10)."""


class UnknownToolError(Tier2Error):
    """Tên tool không nằm trong 4 giá trị whitelist (mục 10)."""
