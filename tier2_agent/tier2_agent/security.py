"""`sanitize` và `check_injection` — 2 trong 5 hàm của Context Manager
(TIER2_DESIGN.md mục 4). Tách file riêng vì đây là 2 hàm "bảo mật" thuần,
test độc lập dễ hơn khi không lẫn với build/normalize/format.

- `sanitize` (credential): rule-based, pattern cố định — khớp thì CHẶN HẲN
  toàn bộ tool_output, trả về "BLOCKED_SENSITIVE".
- `check_injection` ("Security check"): rule-based, REDACT đúng đoạn khớp,
  không chặn hẳn — mở rộng thêm entropy Shannon bất thường (mục 4, 10.1) và
  cũng được gọi trên `reasoning` của Model trước khi ghi log (mục 4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

from .utils import shannon_entropy

BLOCKED_SENSITIVE_MARKER = "BLOCKED_SENSITIVE"
INJECTION_REDACTION_MARKER = "[REDACTED_INJECTION]"
ENTROPY_REDACTION_MARKER = "[REDACTED_HIGH_ENTROPY]"

# --- sanitize: pattern credential cố định (môi trường thử nghiệm tự kiểm
# soát format, nên rule cứng là đủ — mục 4) ---
_CREDENTIAL_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['\"]?[A-Za-z0-9/_\-\.]{8,}['\"]?"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-_\.]{10,}"),
]

# --- check_injection: pattern chỉ thị chèn vào (prompt injection) đã biết ---
_INJECTION_PATTERNS = [
    re.compile(r"(?i)ignore (all )?(previous|prior|above) instructions"),
    re.compile(r"(?i)disregard (all )?(previous|prior|above)"),
    re.compile(r"(?i)you are now[:\s]"),
    re.compile(r"(?i)system\s*:\s*"),
    re.compile(r"(?i)new instructions?\s*:"),
    re.compile(r"(?i)reveal (your )?(system prompt|instructions)"),
    re.compile(r"(?i)always treat .*? as (sufficient|true|valid)"),
    re.compile(r"(?i)act as (if )?(you were|an?)\b"),
]

# Ngưỡng entropy — chuỗi liên tục không khoảng trắng, đủ dài, entropy cao
# nghi payload mã hoá/obfuscate (mục 4, 10.1)
_ENTROPY_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=]{24,}")
_ENTROPY_THRESHOLD_BITS_PER_CHAR = 4.5


def sanitize(tool_output: str) -> Tuple[str, bool]:
    """Trả (output_đã_lọc, sanitized_flagged).

    Khớp credential -> chặn HẲN toàn bộ tool_output, đánh dấu phân biệt được
    với "không tìm thấy gì" (mục 4).
    """

    if tool_output is None:
        return "", False
    for pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(tool_output):
            return BLOCKED_SENSITIVE_MARKER, True
    return tool_output, False


@dataclass
class InjectionCheckResult:
    text: str
    flagged: bool
    matched_known_pattern: bool
    matched_high_entropy: bool


def check_injection(text: str) -> InjectionCheckResult:
    """Redact đúng đoạn khớp injection/entropy bất thường, giữ phần còn lại
    (khác `sanitize` — không chặn hẳn vì injection phrasing vô hạn, mục 4).

    Dùng chung cho cả `tool_output` (3 tool text) lẫn `reasoning` của Model
    (mục 4: "quét cả reasoning để chặn kịch bản Model bị dụ tự viết chỉ thị
    vào chính suy luận của nó").
    """

    if not text:
        return InjectionCheckResult(text or "", False, False, False)

    redacted = text
    matched_known = False
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(redacted):
            matched_known = True
            redacted = pattern.sub(INJECTION_REDACTION_MARKER, redacted)

    matched_entropy = False

    def _entropy_sub(m: "re.Match") -> str:
        nonlocal matched_entropy
        token = m.group(0)
        if shannon_entropy(token) >= _ENTROPY_THRESHOLD_BITS_PER_CHAR:
            matched_entropy = True
            return ENTROPY_REDACTION_MARKER
        return token

    redacted = _ENTROPY_TOKEN_RE.sub(_entropy_sub, redacted)

    return InjectionCheckResult(
        text=redacted,
        flagged=matched_known or matched_entropy,
        matched_known_pattern=matched_known,
        matched_high_entropy=matched_entropy,
    )
