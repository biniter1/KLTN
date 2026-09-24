"""Tiện ích chung: id, thời gian, entropy, cắt chuỗi.

Tách riêng để test được từng phần nhỏ độc lập (entropy, cap size) mà không
phải dựng cả Brain.
"""

from __future__ import annotations

import math
import time
import uuid
from collections import Counter
from typing import Tuple


def new_id(prefix: str) -> str:
    """Sinh id ngẫu nhiên ổn định, dùng cho evidence_id khi Model không tự đặt."""

    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_epoch() -> float:
    return time.time()


def canonical_json_key(value) -> str:
    """Chuẩn hoá `tool_input` (dict) thành 1 khoá so sánh ổn định cho dedup
    (mục 10: dedup theo (tool_name, tool_input))."""

    if isinstance(value, dict):
        items = sorted((k, canonical_json_key(v)) for k, v in value.items())
        return "{" + ",".join(f"{k}:{v}" for k, v in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json_key(v) for v in value) + "]"
    return repr(value)


def shannon_entropy(text: str) -> float:
    """Entropy Shannon (bit/ký tự) — tín hiệu nghi payload mã hoá/obfuscate
    (TIER2_DESIGN.md mục 4, 10.1: kỹ thuật nhận từ bài viết ngoài)."""

    if not text:
        return 0.0
    counts = Counter(text)
    length = len(text)
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def cap_size(text: str, max_chars: int) -> Tuple[str, bool]:
    """Cap kích thước tool_output (mục 4, mục 10 policy `tool_output size`).

    Trả (nội_dung_có_thể_bị_cắt, truncated_flag). Không phải nội dung nguy
    hiểm nên chỉ cắt + gắn cờ, không chặn hẳn (khác `sanitize`).
    """

    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True
