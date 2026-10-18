"""
Config Evidence Collector v2 — ĐIỀU PHỐI TOÀN BỘ pipeline (mục 4.0 bước 1-5) + ghi
SQLite (mục 6, `store.py`). Đây là entry point DUY NHẤT bên ngoài package này cần gọi.

Giữ TÊN HÀM giống hệt v1 (`collect_config_evidence`, trước ở `tier1/config_collector.py`,
nay `regex_baseline.py`) để `infra/scripts/08_export_trial_data.py` KHÔNG phải sửa cách
import (`from tier1.config_collector import KubernetesReader, collect_config_evidence`
vẫn đúng — `KubernetesReader` re-export ở `__init__.py`). Lệnh gọi CŨ:

    collect_config_evidence(k8s_reader, snapshot_id=f"{trial_id}-snapshot", namespaces=[trial_id])

vẫn chạy được NGUYÊN VẸN — mọi tham số mới (`llm_client`, `cache`, `image_reader`,
`prompt_version`, `run_id`, `db_path`) đều optional với default hợp lý (xem docstring
`collect_config_evidence`). Khác biệt THẬT SỰ so với v1: bây giờ cần `OPENAI_API_KEY`
trong môi trường chạy (Orchestrator, không phải cluster) để `OpenAILLMClient` mặc định
hoạt động — mục 11 điểm mở đã đổi provider 4 lần trong thực tế (2026-09/10): Anthropic
(tốn phí, VAT phát sinh bất ngờ lúc nạp) -> Gemini free tier (bug "prepayment credits
depleted" + 503 quá tải) -> Groq free tier (giới hạn token/ngày thấp, cạn nhanh) ->
OpenAI (tốn phí, chốt dùng vì 3 cái trên đều không đủ ổn định cho lần chạy thật sau 7
ngày quan sát). Cả 3 client kia (`AnthropicLLMClient`/`GeminiLLMClient`/`GroqLLMClient`)
vẫn còn nguyên trong `extract.py` — đổi qua lại bằng biến môi trường
`CONFIG_COLLECTOR_LLM_PROVIDER`, không cần sửa code (xem docstring `collect_config_evidence`).

**Không fallback sang regex khi LLM lỗi** (mục 4.3) — `ExtractionFailed`/
`LLMExtractionError` từ `extract.py` bay THẲNG LÊN từ hàm này, KHÔNG bắt/nuốt ở đây.
Người gọi (`08_export_trial_data.py` / `ORCHESTRATION_DESIGN.md` mục 2) coi exception
này là "bước config_collector = FAILED -> hủy cả trial".
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..schema import ConfigEvidenceRecord
from . import store
from .collect import collect_raw_items
from .extract import (
    DEFAULT_PROMPT_VERSION,
    AnthropicLLMClient,
    ExtractionCache,
    GeminiLLMClient,
    GroqLLMClient,
    InMemoryExtractionCache,
    LLMClient,
    OpenAILLMClient,
    extract_all,
)
from .funnel import build_funnel_items, chunk_items_by_workload
from .image_config import default_image_reader
from .k8s_types import ImageConfigReader, K8sReader
from .merge import merge_effective_config
from .validate import ValidatedReference, build_cluster_snapshot, finalize_evidence, validate_reference

# mục 11 điểm mở: 4 provider pluggable đã thử trong thực tế (2026-09/10) -- Anthropic
# (tốn phí, nạp tiền phát sinh VAT ~54% bất ngờ), Gemini free tier (bug "prepayment
# credits depleted" + 503 quá tải kéo dài), Groq free tier (giới hạn token/ngày thấp,
# cạn nhanh), OpenAI (tốn phí, user chốt dùng sau khi cả 3 cái trên đều không ổn). Chọn
# qua biến môi trường CONFIG_COLLECTOR_LLM_PROVIDER để đổi provider KHÔNG cần sửa
# code/redeploy lại -- đúng bài học rút ra sau 4 lần phải gửi lại code chỉ vì đổi
# provider/model_id.
_LLM_PROVIDER_FACTORIES = {
    "openai": OpenAILLMClient,
    "gemini": GeminiLLMClient,
    "anthropic": AnthropicLLMClient,
    "groq": GroqLLMClient,
}
DEFAULT_LLM_PROVIDER = "openai"


def _default_llm_client() -> LLMClient:
    provider = os.environ.get("CONFIG_COLLECTOR_LLM_PROVIDER", DEFAULT_LLM_PROVIDER).strip().lower()
    factory = _LLM_PROVIDER_FACTORIES.get(provider)
    if factory is None:
        raise ValueError(
            f"CONFIG_COLLECTOR_LLM_PROVIDER='{provider}' không hợp lệ -- phải là 1 trong "
            f"{sorted(_LLM_PROVIDER_FACTORIES)} (hoặc bỏ trống để dùng mặc định "
            f"'{DEFAULT_LLM_PROVIDER}')."
        )
    return factory()


def collect_config_evidence(
    reader: K8sReader,
    snapshot_id: str,
    namespaces: Optional[list[str]] = None,
    collected_at: Optional[str] = None,
    *,
    image_reader: Optional[ImageConfigReader] = None,
    llm_client: Optional[LLMClient] = None,
    cache: Optional[ExtractionCache] = None,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    run_id: Optional[str] = None,
    db_path: Optional[str | Path] = None,
) -> list[ConfigEvidenceRecord]:
    """Điều phối pipeline mục 4 bước [1]-[5] cho 1 trial, trả về `list[ConfigEvidenceRecord]`
    — input trực tiếp cho `candidates.discover_candidates()` bên Tier 1 (C_potential).

    Tham số GIỮ NGUYÊN từ v1: `reader`, `snapshot_id`, `namespaces`, `collected_at`.

    Tham số MỚI (đều optional, default hợp lý để lệnh gọi cũ vẫn chạy không sửa gì):
      - `image_reader`: mặc định `image_config.default_image_reader()` — chọn theo biến
        môi trường `CONFIG_COLLECTOR_IMAGE_READER` ("auto" mặc định = crictl trong node
        kind rồi tới Registry API v2 | "kind" | "registry" | "crane" | "none"). Đọc
        ENV/ENTRYPOINT/CMD của image (mục 3.2), best-effort: không đọc được thì bỏ qua
        tầng image, không lỗi. Test suite đặt "none" (tests/conftest.py).
      - `llm_client`: mặc định chọn theo biến môi trường `CONFIG_COLLECTOR_LLM_PROVIDER`
        ("openai" | "gemini" | "anthropic" | "groq", mặc định "openai" nếu không đặt) rồi
        tự tạo client tương ứng (đọc API key riêng của provider đó từ env — mục 2 "API
        key giữ ở Orchestrator, không nằm trong cluster"). Đổi provider KHÔNG cần sửa
        code, chỉ cần `export CONFIG_COLLECTOR_LLM_PROVIDER=groq` (+ API key tương ứng)
        trên máy chạy. Test truyền `FakeLLMClient` để không gọi API thật.
      - `cache`: mặc định `InMemoryExtractionCache()` (chỉ sống trong 1 lần gọi hàm này).
        Muốn cache BỀN qua nhiều lần `collect_config_evidence()` khác nhau (mục 2 "config
        của service gốc Online Boutique... chỉ gọi 1 lần" cho cả 15 trial) thì truyền
        `db_path` (hàm tự tạo `store.SqliteExtractionCache` backing bởi SQLite) hoặc tự
        truyền `cache=` riêng.
      - `prompt_version`: mặc định `extract.DEFAULT_PROMPT_VERSION` — PHẢI cố định trước
        khi chạy R=15 (mục 4.3), không đổi giữa các lần gọi trong cùng 1 đợt thí nghiệm.
      - `run_id`: mặc định = `snapshot_id` nếu không truyền riêng (dùng làm khoá nhóm ở
        `collection_quality_log`/`config_raw_items`/`config_extractions`, mục 6).
      - `db_path`: truyền vào thì MỞ file SQLite này và ghi đủ 5 bảng mục 6 (append-only,
        `store.py`). Không truyền thì KHÔNG ghi gì xuống đĩa — chỉ trả `ConfigEvidenceRecord`
        trong bộ nhớ (dry-run / test).

    Validation (bước 5) luôn nhìn TOÀN CẢNH cụm (mọi Service/Endpoints/VirtualService/
    ServiceEntry, không giới hạn theo `namespaces`) để phân giải đích đúng theo đúng ngữ
    nghĩa DNS thật của K8s — khác v1 (vốn giới hạn luôn cả phần xác thực theo
    `namespaces`). Đây là lựa chọn CÓ CHỦ ĐÍCH, không phải sơ suất: nguồn thu thập
    (workload nào bị quét) vẫn giới hạn đúng theo `namespaces`, chỉ phần "đích nằm ở
    đâu" được nhìn rộng hơn cho đúng.
    """
    collected_at = collected_at or datetime.now(timezone.utc).isoformat()
    run_id = run_id or snapshot_id
    image_reader = image_reader or default_image_reader()
    llm_client = llm_client or _default_llm_client()

    conn: Optional[sqlite3.Connection] = None
    if db_path is not None:
        conn = store.connect(db_path)
        if cache is None:
            cache = store.SqliteExtractionCache(conn)
    if cache is None:
        cache = InMemoryExtractionCache()

    snapshot = build_cluster_snapshot(reader)

    all_funnel_items = []
    all_validated: list[ValidatedReference] = []
    chunk_count = 0

    namespace_list = namespaces if namespaces else [None]
    for ns in namespace_list:
        raw_items = collect_raw_items(reader, image_reader, ns)
        pods = reader.list_pods(ns)
        pods_by_workload = {p.name: p for p in pods}

        effective_items = merge_effective_config(pods, raw_items, image_reader)
        funnel_items = build_funnel_items(effective_items)
        all_funnel_items.extend(funnel_items)
        funnel_items_by_id = {it.item_id: it for it in funnel_items}

        chunks = chunk_items_by_workload(funnel_items)
        chunk_count += len(chunks)
        chunk_results = extract_all(chunks, llm_client, prompt_version, cache)

        for chunk, references in chunk_results:
            source_pod = pods_by_workload.get(chunk.workload)
            if source_pod is None:
                continue  # không nên xảy ra (chunk luôn sinh từ pod đã quét) -- bỏ qua an toàn
            for ref in references:
                all_validated.extend(validate_reference(ref, funnel_items_by_id, source_pod, snapshot))

    groups = finalize_evidence(all_validated)
    model_id = getattr(llm_client, "model_id", "unknown")
    records = store.build_config_evidence_records(groups, snapshot_id, collected_at, run_id, prompt_version, model_id)

    if conn is not None:
        store.insert_raw_items(conn, run_id, all_funnel_items)
        store.insert_extractions(conn, run_id, all_validated)
        store.insert_config_evidence(conn, run_id, records)
        store.insert_quality_log(conn, run_id, _quality_counts(all_funnel_items, all_validated, chunk_count, records))

    return records


def _quality_counts(
    funnel_items, validated: list[ValidatedReference], chunk_count: int, records: list[ConfigEvidenceRecord],
) -> dict[str, float]:
    """mục 6 `collection_quality_log` / mục 9.1 — số đếm tổng hợp 1 trial."""
    counts: dict[str, float] = {
        "raw_items_total": len(funnel_items),
        "chunks_total": chunk_count,
        "extractions_total": len(validated),
        "evidence_total": len(records),
    }
    for status in ("KEPT", "EMPTY", "BOOLEAN", "BINARY", "SECRET_REF", "OVERRIDDEN"):
        counts[f"funnel_{status.lower()}"] = sum(1 for it in funnel_items if it.funnel_status == status)
    for status in ("REJECTED_UNGROUNDED", "REJECTED_INVALID_HOST", "DROP_LOOPBACK", "DROP_SELF"):
        counts[status.lower()] = sum(1 for v in validated if v.status == status)
    for finding in ("FOUND_STRUCTURAL", "FOUND_EMBEDDED", "FOUND_UNRESOLVED"):
        counts[finding.lower()] = sum(1 for r in records if r.config_finding.value == finding)
    return counts