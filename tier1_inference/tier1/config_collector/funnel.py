"""
Config Evidence Collector v2 — bước [3] PHỄU LỌC (bảo thủ) + CHUẨN HOÁ
(CONFIG_EVIDENCE_COLLECTOR.md mục 4.2).

"Phễu lọc là chỗ nguy hiểm nhất của pipeline" (mục 4.2) — mục đích cả module là giảm
false negative; lọc quá tay ở ĐÂY lại tạo chính loại lỗi nguy hiểm nhất (false negative
IM LẶNG). Nguyên tắc: LỌC THEO HÌNH DẠNG GIÁ TRỊ, không lọc theo tên biến. Chỉ bỏ 5 loại
chắc chắn vô nghĩa: EMPTY, BOOLEAN, BINARY, SECRET_REF, và OVERRIDDEN (bị `merge.py`
bước 2 ghi đè). KHÔNG bỏ: số trần, đường dẫn, chuỗi ngắn, tên biến lạ (mục 4.2 nói rõ).

Mọi item, kể cả bị loại, đều có mặt trong kết quả của `build_funnel_items` (với
`funnel_status` khác "KEPT") — module này KHÔNG được phép âm thầm xoá bất cứ thứ gì,
vì mục 6 `config_raw_items` và mục 9.1 "funnel loss" cần thấy TOÀN BỘ, không chỉ phần
còn sống sót.

Redact credential TRƯỚC khi dữ liệu rời cluster tới API LLM bên ngoài (mục 4.2):
  - userinfo trong URL: `scheme://user:pass@host` -> `scheme://user:****@host`
  - `password=`/`pwd=` trong connection string dạng key=value
`redacted_value` là bản DUY NHẤT gửi cho LLM và DUY NHẤT dùng để grounding (mục 4.4a)
— "kiểm tra nguyên văn chạy trên đúng văn bản LLM đã thấy", không phải trên giá trị gốc.

Chunk theo RANH GIỚI ITEM — không bao giờ cắt giữa 1 item, kể cả item nhiều dòng (nội
dung file ConfigMap) — "để quote nguyên văn vẫn kiểm được" (mục 4.2). Đơn vị gọi LLM là
1 workload; không gộp nhiều workload vào 1 chunk dù còn dư chỗ (ranh giới rõ ràng, đơn
giản hoá cache theo mục 4.3 — `extract.py` cache theo từng chunk).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .merge import EffectiveConfigItem

DEFAULT_CHUNK_CHAR_LIMIT = 8000  # mục 4.2 — "ngưỡng khởi tạo ~8000 ký tự/lần gọi, đo lại ở pilot"

_BOOLEAN_VALUES = {"true", "false", "yes", "no", "on", "off"}

# mục 4.2 chuẩn hoá — redact credential trước khi rời cluster.
_USERINFO_PATTERN = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^:/@\s]+):(?P<pass>[^@/\s]+)@")
_KV_SECRET_PATTERN = re.compile(r"(?i)\b(password|pwd)=([^&\s;]+)")


def _redact(value: str) -> str:
    value = _USERINFO_PATTERN.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:****@", value)
    value = _KV_SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=****", value)
    return value


@dataclass(frozen=True)
class FunnelItem:
    """1 item sau bước [3] — mọi item (kể cả bị loại) đều có mặt ở đây."""

    item_id: str
    workload: str
    namespace: str
    container: str
    container_type: str
    source_kind: str
    key: str
    value: str  # giá trị gốc (sau merge.py), CHƯA redact — dùng khi truy vết/debug nội bộ
    redacted_value: str  # giá trị ĐÃ redact — DUY NHẤT đưa cho LLM + DUY NHẤT dùng grounding
    funnel_status: str  # "KEPT" | "EMPTY" | "BOOLEAN" | "BINARY" | "SECRET_REF" | "OVERRIDDEN"
    source_ref: Optional[str]
    rendered_block: str  # text "<config_item ...>...</config_item>" đã redact — rỗng nếu không KEPT


def _classify(item: EffectiveConfigItem) -> str:
    if item.status == "OVERRIDDEN":
        return "OVERRIDDEN"
    if item.source_kind == "secret_ref":
        return "SECRET_REF"
    if item.is_binary:
        return "BINARY"
    if item.value == "":
        return "EMPTY"
    if item.value.strip().lower() in _BOOLEAN_VALUES:
        return "BOOLEAN"
    return "KEPT"


def _render_block(item_id: str, item: EffectiveConfigItem, redacted_value: str) -> str:
    return (
        f'<config_item id="{item_id}" workload="{item.workload}" container="{item.container}" '
        f'source="{item.source_kind}" key="{item.key}">\n{redacted_value}\n</config_item>'
    )


def build_funnel_items(effective_items: list[EffectiveConfigItem]) -> list[FunnelItem]:
    """mục 4.2 bước [3]a — phân loại + redact + gán `item_id`. Sắp xếp ỔN ĐỊNH theo
    (workload, container, source_kind, key) TRƯỚC khi gán `item_id`, để hash
    `sha256(prompt_version|model_id|chunk)` ở `extract.py` tái lập được qua các lần
    chạy khác nhau (mục 1.2 nguyên tắc 8, mục 4.2 "sắp xếp ổn định")."""
    ordered = sorted(effective_items, key=lambda it: (it.workload, it.container, it.source_kind, it.key))

    result: list[FunnelItem] = []
    for idx, item in enumerate(ordered):
        item_id = f"i{idx:04d}"
        status = _classify(item)
        redacted_value = _redact(item.value) if status == "KEPT" else ""
        rendered_block = _render_block(item_id, item, redacted_value) if status == "KEPT" else ""
        result.append(
            FunnelItem(
                item_id=item_id, workload=item.workload, namespace=item.namespace,
                container=item.container, container_type=item.container_type,
                source_kind=item.source_kind, key=item.key, value=item.value,
                redacted_value=redacted_value, funnel_status=status,
                source_ref=item.source_ref, rendered_block=rendered_block,
            )
        )
    return result


@dataclass(frozen=True)
class Chunk:
    """1 lần gọi LLM (mục 4.3) — luôn thuộc đúng 1 workload (mục 4.2 "đơn vị gọi LLM
    là 1 workload"). `chunk_index`/`chunk_count` chỉ để log/debug."""

    workload: str
    namespace: str
    chunk_index: int
    chunk_count: int
    items: tuple[FunnelItem, ...]  # chỉ item KEPT, đúng thứ tự xuất hiện trong `text`
    text: str  # nối `rendered_block` của `items` bằng "\n" — sẵn sàng nhúng vào prompt


def chunk_items_by_workload(
    items: list[FunnelItem], char_limit: int = DEFAULT_CHUNK_CHAR_LIMIT
) -> list[Chunk]:
    """mục 4.2 bước [3]b — chia chunk theo RANH GIỚI ITEM, không bao giờ cắt giữa 1
    item (kể cả item nhiều dòng), "để quote nguyên văn vẫn kiểm được". Một item đơn lẻ
    dài hơn `char_limit` vẫn tạo thành 1 chunk riêng nguyên vẹn — thà vượt ngưỡng còn
    hơn cắt đôi quote."""
    kept = [it for it in items if it.funnel_status == "KEPT"]

    by_workload: dict[tuple[str, str], list[FunnelItem]] = {}
    for it in kept:
        by_workload.setdefault((it.namespace, it.workload), []).append(it)

    chunks: list[Chunk] = []
    for (namespace, workload), workload_items in by_workload.items():
        groups: list[list[FunnelItem]] = []
        current: list[FunnelItem] = []
        current_len = 0
        for it in workload_items:
            block_len = len(it.rendered_block)
            added_len = block_len + (1 if current else 0)  # "\n" nối giữa các block
            if current and current_len + added_len > char_limit:
                groups.append(current)
                current = [it]
                current_len = block_len
            else:
                current_len += added_len
                current.append(it)
        if current:
            groups.append(current)

        for idx, group in enumerate(groups):
            chunks.append(
                Chunk(
                    workload=workload, namespace=namespace, chunk_index=idx, chunk_count=len(groups),
                    items=tuple(group), text="\n".join(it.rendered_block for it in group),
                )
            )
    return chunks
