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

Sửa 15/10/2026 (TIER1_DESIGN.md mục 10b):
  - tra lịch sử theo ĐỦ 7 phần khóa cạnh (bản cũ thiếu source_service_account -> 2 cạnh
    chỉ khác SA dùng chung lịch sử);
  - chỉ lấy dòng của snapshot TRƯỚC snapshot hiện tại (bản cũ không loại dòng cùng
    snapshot_id -> chạy lại 1 snapshot đọc nhầm kết quả của chính nó);
  - cột `snapshot_time` (thời điểm của DỮ LIỆU) — sắp lịch sử và đếm số lần đổi trạng thái
    theo cột này, không theo `created_at` (giờ chạy code);
  - cột mới: port_source, traffic_days, flags (JSON), config_match_level, auto_allow_reason.
"""

from __future__ import annotations

import json
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
    -- NULL được phép: cạnh potential-only FOUND_UNRESOLVED (config trỏ tên không phân giải
    -- được) có destination = None theo CONFIG_EVIDENCE_COLLECTOR.md mục 6 — trước đây
    -- NOT NULL làm insert_results lỗi đúng loại cạnh hay escalate nhất (chưa lộ ra vì
    -- experiment.py chạy stateless, không ghi SQLite).
    destination_namespace       TEXT,
    destination_canonical_service TEXT,
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

    created_at TEXT NOT NULL,

    -- Orchestration layer (ORCHESTRATION_DESIGN.md mục 1-3, DASHBOARD_DESIGN.md mục 4).
    -- NULL khi Tier 1 chạy độc lập (examples/, experiment.py) — không bắt buộc.
    run_id            TEXT,   -- trial (vd "trial-07")
    attempt_id        TEXT,   -- 1 lần chạy Batch Orchestrator cho trial đó (vd "trial-07.a2")
    identity_key      TEXT,   -- IdentityKey.as_string(), khoá nối với Tier 2/human_decisions
    tier2_session_id  TEXT,   -- agent_sessions.run_id của phiên Tier 2 xử lý cạnh này
    tier2_status          TEXT,  -- TIER2_DESIGN.md mục 11.3
    tier2_original_status TEXT,

    -- 15/10/2026 (TIER1_DESIGN.md mục 10b)
    snapshot_time       TEXT,   -- thời điểm của dữ liệu, "YYYY-MM-DD HH:MM:SS" UTC
    port_source         TEXT,
    traffic_days        TEXT,   -- JSON list ngày có traffic
    flags               TEXT,   -- JSON list cờ
    config_match_level  TEXT,   -- EXACT | PORT_ONLY | PAIR_ONLY
    auto_allow_reason   TEXT    -- VERIFIED_MONITORING | NULL
);

CREATE INDEX IF NOT EXISTS idx_dep_evidence_identity
    ON dependency_evidence (
        source_namespace, source_canonical_service,
        destination_namespace, destination_canonical_service,
        protocol, port
    );

CREATE INDEX IF NOT EXISTS idx_dep_evidence_snapshot ON dependency_evidence (snapshot_id, created_at);
"""

# Cột thêm sau (Orchestration layer) — file SQLite tạo bằng bản CŨ của module này sẽ không
# có các cột này (CREATE TABLE IF NOT EXISTS không sửa bảng đã tồn tại), nên `connect()`
# tự ALTER TABLE bổ sung. Chỉ THÊM cột, không bao giờ xoá/đổi cột cũ.
_ADDED_COLUMNS = (
    ("run_id", "TEXT"),
    ("attempt_id", "TEXT"),
    ("identity_key", "TEXT"),
    ("tier2_session_id", "TEXT"),
    ("tier2_status", "TEXT"),
    ("tier2_original_status", "TEXT"),
    ("snapshot_time", "TEXT"),
    ("port_source", "TEXT"),
    ("traffic_days", "TEXT"),
    ("flags", "TEXT"),
    ("config_match_level", "TEXT"),
    ("auto_allow_reason", "TEXT"),
)

_POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_dep_evidence_run ON dependency_evidence (run_id, attempt_id);
CREATE INDEX IF NOT EXISTS idx_dep_evidence_identity_key ON dependency_evidence (identity_key);
"""

_IDENTITY_SA_INDEX = """
CREATE INDEX IF NOT EXISTS idx_dep_evidence_identity_sa ON dependency_evidence (
    source_namespace, source_canonical_service, source_service_account,
    destination_namespace, destination_canonical_service, protocol, port, snapshot_time
);
"""


def migrate(conn: sqlite3.Connection) -> None:
    """Bổ sung cột thiếu cho file SQLite tạo từ bản cũ (idempotent)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(dependency_evidence)")}
    for name, col_type in _ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE dependency_evidence ADD COLUMN {name} {col_type}")
    conn.executescript(_POST_MIGRATION_INDEXES)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(dependency_evidence)")}
    if "source_service_account" in cols:  # bảng rất cũ (test migrate) có thể thiếu cột này
        conn.executescript(_IDENTITY_SA_INDEX)


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    return conn


def _identity_where_clause() -> str:
    # `IS ?` cho cột có thể NULL (đích None của cạnh chỉ có config, port None) — `= NULL`
    # trong SQL luôn sai.
    return (
        "source_namespace=? AND source_canonical_service=? AND source_service_account=? "
        "AND destination_namespace IS ? AND destination_canonical_service IS ? "
        "AND protocol=? AND port IS ?"
    )


def _identity_params(key: IdentityKey) -> tuple:
    return (
        key.source_namespace,
        key.source_canonical_service,
        key.source_service_account,
        key.destination_namespace,
        key.destination_canonical_service,
        key.protocol,
        key.port,
    )


def normalize_time(value: str | None) -> str | None:
    """ISO 8601 bất kỳ -> "YYYY-MM-DD HH:MM:SS" UTC (so chuỗi được với datetime() SQLite)."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


_ROW_TIME = "COALESCE(snapshot_time, created_at)"


def get_previous_status_map(
    conn: sqlite3.Connection,
    candidate_keys: list[IdentityKey],
    current_snapshot_id: str | None = None,
    before_snapshot_time: str | None = None,
) -> dict[IdentityKey, DependencyStatus]:
    """Với mỗi identity_key, lấy dependency_status của dòng GẦN NHẤT thuộc snapshot TRƯỚC
    snapshot hiện tại — previous_status cho hysteresis (mục 5b). Loại mọi dòng cùng
    `current_snapshot_id` và (nếu có `before_snapshot_time`) mọi dòng có thời điểm dữ liệu
    không sớm hơn. Cạnh mới -> không có trong kết quả (= "không phải STRONG")."""
    clauses = [_identity_where_clause()]
    extra: list = []
    if current_snapshot_id is not None:
        clauses.append("snapshot_id != ?")
        extra.append(current_snapshot_id)
    before = normalize_time(before_snapshot_time)
    if before is not None:
        clauses.append(f"{_ROW_TIME} < ?")
        extra.append(before)
    where = " AND ".join(clauses)

    result: dict[IdentityKey, DependencyStatus] = {}
    for key in candidate_keys:
        row = conn.execute(
            f"""SELECT dependency_status FROM dependency_evidence
                WHERE {where}
                ORDER BY {_ROW_TIME} DESC, id DESC LIMIT 1""",
            _identity_params(key) + tuple(extra),
        ).fetchone()
        if row is not None:
            result[key] = DependencyStatus(row[0])
    return result


def get_recent_flip_count(
    conn: sqlite3.Connection,
    key: IdentityKey,
    window_days: int,
    until_snapshot_time: str | None = None,
) -> int:
    """Đếm số lần dependency_status ĐỔI giữa các dòng liên tiếp của cùng identity_key
    trong `window_days` ngày tính tới `until_snapshot_time` (thời điểm dữ liệu, mặc định
    là bây giờ) — unstable_classification (mục 5b)."""
    until = normalize_time(until_snapshot_time)
    if until is None:
        window_sql = f"{_ROW_TIME} >= datetime('now', ?)"
        params = _identity_params(key) + (f"-{window_days} days",)
    else:
        window_sql = f"{_ROW_TIME} >= datetime(?, ?) AND {_ROW_TIME} < ?"
        params = _identity_params(key) + (until, f"-{window_days} days", until)
    rows = conn.execute(
        f"""SELECT dependency_status FROM dependency_evidence
            WHERE {_identity_where_clause()} AND {window_sql}
            ORDER BY {_ROW_TIME} ASC, id ASC""",
        params,
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
    *,
    run_id: str | None = None,
    attempt_id: str | None = None,
) -> None:
    """`run_id`/`attempt_id` (tuỳ chọn) — Batch Orchestrator truyền vào để gắn mọi dòng
    với đúng trial + lần chạy (ORCHESTRATION_DESIGN.md mục 1-3). `identity_key` dạng chuỗi
    luôn được ghi (không phụ thuộc 2 tham số này)."""
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
            run_id, attempt_id, k.as_string(),
            normalize_time(r.snapshot_time), r.port_source,
            json.dumps(list(r.traffic_days)), json.dumps(list(r.flags)),
            r.config_match_level, r.auto_allow_reason,
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
            created_at,
            run_id, attempt_id, identity_key,
            snapshot_time, port_source, traffic_days, flags, config_match_level, auto_allow_reason
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()


def fetch_latest_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM dependency_evidence WHERE snapshot_id=? ORDER BY id ASC", (snapshot_id,)
    )
    return cur.fetchall()
