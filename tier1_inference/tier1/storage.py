"""
SQLite storage — bảng `dependency_evidence`, schema bám sát TIER1_DESIGN.md mục 10
(+ TIER2_DESIGN.md mục 11.3: đây chính là bảng mà Tier 2 sau này UPDATE thêm cột
tier2_status/tier2_original_status, không phải bảng mới).

WAL mode (PRAGMA journal_mode=WAL) — nhất quán với quyết định đã chốt bên thiết kế
Tier 2 (đọc/ghi đồng thời không cần message queue).

Bảng được thiết kế APPEND-ONLY: mỗi lần chạy pipeline (mỗi snapshot) chèn thêm dòng mới
cho mỗi edge, không UPDATE đè lên dòng cũ. Điều này cho:
  - previous_status cho hysteresis (mục 5b): lấy dòng gần nhất TRƯỚC snapshot hiện tại
    của cùng identity_key.
  - unstable_classification (mục 5b): đếm số lần đổi status trong cửa sổ N ngày gần nhất
    bằng cách quét lịch sử các dòng trước đó — không cần bảng phụ.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import ScoringConfig, DEFAULT_CONFIG
from .schema import (
    ConfigFinding,
    DependencyEdgeResult,
    DependencyStatus,
    IdentityKey,
    Scope,
    ServiceRef,
    TrafficClass,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS dependency_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- identity_key (mục 3)
    source_namespace            TEXT NOT NULL,
    source_canonical_service    TEXT NOT NULL,
    source_service_account      TEXT NOT NULL,
    destination_namespace       TEXT NOT NULL,
    destination_canonical_service TEXT NOT NULL,
    protocol                    TEXT NOT NULL,
    port                        INTEGER,

    -- destination_service_ref (mục 3b)
    destination_service_name      TEXT,
    destination_service_namespace TEXT,

    scope           TEXT NOT NULL,
    traffic_class   TEXT NOT NULL,

    count           INTEGER NOT NULL,
    distinct_days   INTEGER NOT NULL,
    error_ratio     REAL NOT NULL,
    unstable        INTEGER NOT NULL DEFAULT 0,

    config_finding  TEXT,

    score            REAL,
    dependency_status TEXT NOT NULL,

    snapshot_id       TEXT NOT NULL,
    workload_revision TEXT,

    -- bookkeeping nội bộ Tier 1 (không thuộc schema mục 10 gốc)
    previous_status          TEXT,
    unstable_classification  INTEGER NOT NULL DEFAULT 0,
    needs_human_review       INTEGER NOT NULL DEFAULT 0,
    escalate_to_tier2        INTEGER NOT NULL DEFAULT 0,
    escalation_reason        TEXT,
    manual_selector_reason   TEXT,
    dns_auto_allow           INTEGER NOT NULL DEFAULT 0,

    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dep_evidence_identity
    ON dependency_evidence (
        source_namespace, source_canonical_service,
        destination_namespace, destination_canonical_service,
        protocol, port
    );

CREATE INDEX IF NOT EXISTS idx_dep_evidence_snapshot ON dependency_evidence (snapshot_id, created_at);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _identity_where_clause() -> str:
    return (
        "source_namespace=? AND source_canonical_service=? "
        "AND destination_namespace=? AND destination_canonical_service=? "
        "AND protocol=? AND (port IS ? OR port=?)"
    )


def _identity_params(key: IdentityKey) -> tuple:
    return (
        key.source_namespace,
        key.source_canonical_service,
        key.destination_namespace,
        key.destination_canonical_service,
        key.protocol,
        key.port,
        key.port,
    )


def get_previous_status_map(
    conn: sqlite3.Connection,
    candidate_keys: list[IdentityKey],
) -> dict[IdentityKey, DependencyStatus]:
    """Với mỗi identity_key, lấy dependency_status của dòng GẦN NHẤT đã lưu trước đó
    (nếu có) — dùng làm previous_status cho hysteresis (mục 5b). Không giả định các
    edge đã tồn tại; edge mới hoàn toàn -> previous_status=None (mặc định là "không
    phải STRONG", đúng nghĩa "chưa từng được thăng hạng")."""
    result: dict[IdentityKey, DependencyStatus] = {}
    for key in candidate_keys:
        row = conn.execute(
            f"""SELECT dependency_status FROM dependency_evidence
                WHERE {_identity_where_clause()}
                ORDER BY created_at DESC, id DESC LIMIT 1""",
            _identity_params(key),
        ).fetchone()
        if row is not None:
            result[key] = DependencyStatus(row[0])
    return result


def get_recent_flip_count(
    conn: sqlite3.Connection,
    key: IdentityKey,
    window_days: int,
) -> int:
    """Đếm số lần dependency_status ĐỔI giữa các dòng liên tiếp của cùng identity_key
    trong `window_days` gần nhất — dùng cho unstable_classification (mục 5b)."""
    rows = conn.execute(
        f"""SELECT dependency_status, created_at FROM dependency_evidence
            WHERE {_identity_where_clause()}
              AND created_at >= datetime('now', ?)
            ORDER BY created_at ASC, id ASC""",
        _identity_params(key) + (f"-{window_days} days",),
    ).fetchall()

    flips = 0
    for i in range(1, len(rows)):
        if rows[i][0] != rows[i - 1][0]:
            flips += 1
    return flips


def insert_results(
    conn: sqlite3.Connection,
    results: list[DependencyEdgeResult],
    run_timestamp: str | None = None,
) -> None:
    # Format "YYYY-MM-DD HH:MM:SS" — tương thích trực tiếp với hàm datetime('now', ...)
    # của SQLite (dùng trong get_recent_flip_count để so sánh cửa sổ ngày).
    ts = run_timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for r in results:
        k = r.identity_key
        ref = r.destination_service_ref
        rows.append((
            k.source_namespace, k.source_canonical_service, k.source_service_account,
            k.destination_namespace, k.destination_canonical_service,
            k.protocol, k.port,
            ref.service_name if ref else None,
            ref.service_namespace if ref else None,
            r.scope.value, r.traffic_class.value,
            r.count, r.distinct_days, r.error_ratio, int(r.unstable),
            r.config_finding.value if r.config_finding else None,
            r.score, r.dependency_status.value,
            r.snapshot_id, r.workload_revision,
            r.previous_status.value if r.previous_status else None,
            int(r.unstable_classification), int(r.needs_human_review),
            int(r.escalate_to_tier2), r.escalation_reason, r.manual_selector_reason,
            int(r.dns_auto_allow),
            ts,
        ))

    conn.executemany(
        """INSERT INTO dependency_evidence (
            source_namespace, source_canonical_service, source_service_account,
            destination_namespace, destination_canonical_service,
            protocol, port,
            destination_service_name, destination_service_namespace,
            scope, traffic_class,
            count, distinct_days, error_ratio, unstable,
            config_finding,
            score, dependency_status,
            snapshot_id, workload_revision,
            previous_status, unstable_classification, needs_human_review,
            escalate_to_tier2, escalation_reason, manual_selector_reason,
            dns_auto_allow,
            created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()


def fetch_latest_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM dependency_evidence WHERE snapshot_id=? ORDER BY id ASC", (snapshot_id,)
    )
    return cur.fetchall()
