"""
Config Evidence Collector v2 — ghi 5 bảng SQLite mục 6 (CONFIG_EVIDENCE_COLLECTOR.md),
CÙNG 1 file SQLite với Tier 1 (`tier1/storage.py`, mục 2 "ghi vào cùng 1 file SQLite với
Tier 1"). Chỉ Batch Orchestrator (tức `pipeline.py::collect_config_evidence` trong
package này) ghi vào các bảng ở đây.

APPEND-ONLY, cùng tinh thần `tier1/storage.py` — mỗi lần chạy pipeline (mỗi trial) chèn
thêm dòng mới, không UPDATE đè, để giữ lịch sử phục vụ mục 9 (đo từng tầng pipeline qua
nhiều trial, so sánh baseline regex vs LLM, v.v).

`SqliteExtractionCache` implement đúng `Protocol ExtractionCache` (`extract.py`) — cho
phép cache LLM BỀN qua nhiều lần gọi `collect_config_evidence()` khác nhau (mục 2:
"config của service gốc Online Boutique... chỉ gọi 1 lần" cho toàn bộ 15 trial), thay vì
chỉ cache trong 1 tiến trình như `InMemoryExtractionCache`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..schema import ConfigEvidenceRecord, EvidenceRef, ServiceRef
from .extract import LLMReference
from .funnel import FunnelItem
from .validate import EvidenceGroup, ValidatedReference

SCHEMA = """
CREATE TABLE IF NOT EXISTS config_raw_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    workload        TEXT NOT NULL,
    namespace       TEXT NOT NULL,
    container       TEXT NOT NULL,
    container_type  TEXT NOT NULL,
    source_kind     TEXT NOT NULL,
    item_key        TEXT NOT NULL,
    value           TEXT NOT NULL,
    source_ref      TEXT,
    funnel_status   TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_config_raw_items_run ON config_raw_items (run_id);

CREATE TABLE IF NOT EXISTS llm_extraction_cache (
    cache_key       TEXT PRIMARY KEY,
    prompt_version  TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    chunk_text      TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_extractions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL,
    item_id           TEXT NOT NULL,
    quote             TEXT NOT NULL,
    host              TEXT,
    port              INTEGER,
    scheme            TEXT,
    port_item_id      TEXT,
    port_quote        TEXT,
    validation_status TEXT NOT NULL,
    unresolved_reason TEXT,
    external_kind     TEXT,
    flags             TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_config_extractions_run ON config_extractions (run_id);

CREATE TABLE IF NOT EXISTS config_evidence (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                          TEXT NOT NULL,
    snapshot_id                     TEXT NOT NULL,
    source_namespace                TEXT NOT NULL,
    source_canonical_service        TEXT NOT NULL,
    source_service_account          TEXT NOT NULL,
    destination_namespace           TEXT,
    destination_canonical_service   TEXT,
    destination_service_account     TEXT,
    destination_service_name        TEXT,
    destination_service_namespace   TEXT,
    raw_host                        TEXT,
    protocol                        TEXT,
    service_port                    INTEGER,
    target_port                     INTEGER,
    port_unknown                    INTEGER NOT NULL DEFAULT 0,
    config_finding                  TEXT NOT NULL,
    unresolved_reason                TEXT,
    scope                           TEXT NOT NULL,
    external_kind                   TEXT,
    flags                           TEXT,
    evidence_refs                   TEXT NOT NULL,
    extraction_method               TEXT NOT NULL,
    prompt_version                  TEXT,
    model_id                        TEXT,
    collected_at                    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_config_evidence_run ON config_evidence (run_id);
CREATE INDEX IF NOT EXISTS idx_config_evidence_snapshot ON config_evidence (snapshot_id);

CREATE TABLE IF NOT EXISTS collection_quality_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_collection_quality_log_run ON collection_quality_log (run_id);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# config_raw_items
# ---------------------------------------------------------------------------


def insert_raw_items(conn: sqlite3.Connection, run_id: str, items: list[FunnelItem]) -> None:
    """mục 6 — "Mọi item sau bước 2, kể cả bị ghi đè/bị loại, kèm provenance +
    funnel_status". Lưu `value` GỐC (chưa redact) để truy vết nội bộ — bảng này không
    rời khỏi máy Orchestrator, không có vấn đề lộ credential ra ngoài như `extract.py`."""
    ts = _now()
    rows = [
        (run_id, it.item_id, it.workload, it.namespace, it.container, it.container_type,
         it.source_kind, it.key, it.value, it.source_ref, it.funnel_status, ts)
        for it in items
    ]
    conn.executemany(
        """INSERT INTO config_raw_items (
            run_id, item_id, workload, namespace, container, container_type,
            source_kind, item_key, value, source_ref, funnel_status, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# llm_extraction_cache — SqliteExtractionCache implement Protocol ExtractionCache
# ---------------------------------------------------------------------------


class SqliteExtractionCache:
    """Cache LLM BỀN qua nhiều lần chạy `collect_config_evidence()` khác nhau (mục 2,
    mục 4.3) — backing bằng bảng `llm_extraction_cache`. Implement đúng Protocol
    `ExtractionCache` của `extract.py` (duck typing, không import ngược)."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get(self, cache_key: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT response_json FROM llm_extraction_cache WHERE cache_key=?", (cache_key,)
        ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def set(self, cache_key: str, prompt_version: str, model_id: str, chunk_text: str, response: dict) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO llm_extraction_cache
               (cache_key, prompt_version, model_id, chunk_text, response_json, created_at)
               VALUES (?,?,?,?,?,?)""",
            (cache_key, prompt_version, model_id, chunk_text, json.dumps(response), _now()),
        )
        self._conn.commit()


# ---------------------------------------------------------------------------
# config_extractions
# ---------------------------------------------------------------------------


def insert_extractions(conn: sqlite3.Connection, run_id: str, validated: list[ValidatedReference]) -> None:
    """mục 6 — "Mọi reference LLM trả về + kết quả validation" — dùng đo precision/
    hallucination (mục 9.1 "ungrounded rate"). Lưu CẢ record bị REJECTED_*/DROP_*, không
    chỉ các record sống sót tới `config_evidence`."""
    ts = _now()
    rows = []
    for v in validated:
        ref: LLMReference = v.reference
        rows.append((
            run_id, ref.item_id, ref.quote, ref.host, ref.port, ref.scheme,
            ref.port_item_id, ref.port_quote, v.status, v.unresolved_reason,
            v.external_kind, json.dumps(list(v.flags)), ts,
        ))
    conn.executemany(
        """INSERT INTO config_extractions (
            run_id, item_id, quote, host, port, scheme, port_item_id, port_quote,
            validation_status, unresolved_reason, external_kind, flags, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# config_evidence — bản cuối, xây ConfigEvidenceRecord cho Tier 1
# ---------------------------------------------------------------------------


def build_config_evidence_records(
    groups: list[EvidenceGroup],
    snapshot_id: str,
    collected_at: str,
    run_id: str,
    prompt_version: str,
    model_id: str,
) -> list[ConfigEvidenceRecord]:
    """Lắp `EvidenceGroup` (đầu ra `validate.py::finalize_evidence`, bước 5g) thành
    `ConfigEvidenceRecord` (schema.py, giao diện KHÔNG ĐỔI với Tier 1). `destination_*`
    = None đúng khi FOUND_UNRESOLVED không phân giải được identity đích thật (mục 6 —
    "null nếu FOUND_UNRESOLVED không phân giải được"; `schema.py::ConfigEvidenceRecord`
    đã nới kiểu `Optional[str]` cho 2 field này để phản ánh đúng mục 6, xem comment ở đó)."""
    records: list[ConfigEvidenceRecord] = []
    for g in groups:
        records.append(
            ConfigEvidenceRecord(
                source_namespace=g.source.namespace,
                source_canonical_service=g.source.canonical_service,
                source_service_account=g.source.service_account,
                destination_namespace=g.destination.namespace if g.destination else None,
                destination_canonical_service=g.destination.canonical_service if g.destination else None,
                destination_service_account=g.destination.service_account if g.destination else None,
                protocol=g.protocol,
                port=g.target_port,
                port_unknown=g.port_unknown,
                config_finding=g.config_finding,
                evidence_source=(g.evidence_refs[0].source_kind if g.evidence_refs else "unknown"),
                scope=g.scope,
                snapshot_id=snapshot_id,
                collected_at=collected_at,
                destination_service_ref=g.destination_service_ref,
                raw_host=g.raw_host,
                service_port=g.service_port,
                unresolved_reason=g.unresolved_reason,
                external_kind=g.external_kind,
                flags=g.flags,
                evidence_refs=g.evidence_refs,
                extraction_method="llm",
                prompt_version=prompt_version,
                model_id=model_id,
                run_id=run_id,
            )
        )
    return records


def insert_config_evidence(conn: sqlite3.Connection, run_id: str, records: list[ConfigEvidenceRecord]) -> None:
    rows = []
    for r in records:
        ref: Optional[ServiceRef] = r.destination_service_ref
        rows.append((
            run_id, r.snapshot_id, r.source_namespace, r.source_canonical_service, r.source_service_account,
            r.destination_namespace, r.destination_canonical_service, r.destination_service_account,
            ref.service_name if ref else None, ref.service_namespace if ref else None,
            r.raw_host, r.protocol, r.service_port, r.port, int(r.port_unknown),
            r.config_finding.value, r.unresolved_reason, r.scope.value, r.external_kind,
            json.dumps(list(r.flags)), json.dumps([_evidence_ref_to_dict(e) for e in r.evidence_refs]),
            r.extraction_method, r.prompt_version, r.model_id, r.collected_at,
        ))
    conn.executemany(
        """INSERT INTO config_evidence (
            run_id, snapshot_id, source_namespace, source_canonical_service, source_service_account,
            destination_namespace, destination_canonical_service, destination_service_account,
            destination_service_name, destination_service_namespace,
            raw_host, protocol, service_port, target_port, port_unknown,
            config_finding, unresolved_reason, scope, external_kind, flags, evidence_refs,
            extraction_method, prompt_version, model_id, collected_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()


def _evidence_ref_to_dict(ref: EvidenceRef) -> dict:
    return {"item_id": ref.item_id, "source_kind": ref.source_kind, "container": ref.container,
            "key": ref.key, "quote": ref.quote}


# ---------------------------------------------------------------------------
# collection_quality_log
# ---------------------------------------------------------------------------


def insert_quality_log(conn: sqlite3.Connection, run_id: str, counts: dict[str, float]) -> None:
    """mục 6 — "Số đếm tổng hợp của trial" cho `DASHBOARD_DESIGN.md` mục 3. `counts` là
    1 dict phẳng tên-chỉ-số -> giá trị, ví dụ {"raw_items_total": 42, "funnel_kept": 30,
    "llm_calls": 3, "cache_hits": 1, "ungrounded_total": 2, "evidence_total": 5}."""
    ts = _now()
    rows = [(run_id, metric, float(value), ts) for metric, value in counts.items()]
    conn.executemany(
        "INSERT INTO collection_quality_log (run_id, metric, value, created_at) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
