"""Lược đồ SQLite DÙNG CHUNG — 1 file, WAL, tách bảng theo NGƯỜI GHI (ORCHESTRATION_DESIGN.md mục 3).

    Bảng                       | Người ghi DUY NHẤT          | Kiểu ghi
    ---------------------------|-----------------------------|---------------------------
    dependency_evidence        | Batch Orchestrator (Tier 1  | append theo attempt; Tier 2
                               |  + orchestrator_hook)       |  UPDATE 3 cột tier2_* đúng
                               |                             |  attempt hiện tại
    config_raw_items,          | Batch Orchestrator (Config  | append
    config_extractions,        |  Collector)                 |
    config_evidence,           |                             |
    llm_extraction_cache       |                             |
    collection_quality_log     | Batch Orchestrator          | append
    pipeline_run_log           | Batch Orchestrator          | append (RUNNING -> SUCCESS/FAILED)
    agent_sessions/agent_rounds| Batch Orchestrator (Tier 2  | Tier 2 tự quản; API Server
      /trace_log               |  Brain)                     |  chỉ bật manual_abort_flag
    human_decisions            | API Server                  | APPEND-ONLY (trigger chặn
                               |                             |  UPDATE/DELETE)
    agent_tool_call_log        | API Server (gatekeeper)     | APPEND-ONLY (trigger)

`ensure_schema()` chỉ chạy DDL (CREATE ... IF NOT EXISTS / ALTER ADD COLUMN) — không ghi
dữ liệu — nên cả 2 tiến trình đều gọi được lúc khởi động mà không vi phạm luật trên.
Quyền GHI DỮ LIỆU vào từng bảng chỉ tồn tại trong code của đúng 1 bên — xem test
`tests/test_write_boundaries.py` (quét mã nguồn, không phải "được phép nhưng không dùng").
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import _paths  # noqa: F401

PIPELINE_STEPS = ("trial", "scenario_check", "traffic_collector", "config_collector", "tier1", "tier2")
RUN_STATUSES = ("RUNNING", "SUCCESS", "FAILED")

# DASHBOARD_DESIGN.md mục 2 — 11 reason_code + 2 mã port (chốt 22/10/2026: cổng port ở cửa ra).
REASON_CODES = (
    "CLEAN_CONFIG_MATCH",
    "NEEDS_MANUAL_SELECTOR",
    "HIGH_BLAST_RADIUS",
    "CONFLICTING_EVIDENCE",
    "AGENT_INSUFFICIENT_BASIS",
    "TOOL_ADMIN_ATTENTION",
    "POLICY_WOULD_BREAK",
    "POLICY_UNSUPPORTED_BY_EVIDENCE",
    "UNATTRIBUTED_CONFIG_CHANGE",
    "EMERGENCY_EXCEPTION_EXPIRED",
    "STALE_PERMISSION",
    "NEEDS_MANUAL_PORT",
    "PORT_CONFLICT",
)

ACTIONS = (
    "APPROVE",
    "REJECT",
    "SUBMIT_SELECTOR",
    "EXTEND_EXCEPTION",
    "MAKE_PERMANENT",
    "ATTRIBUTE",
    "REVOKE",
    "OVERRIDE_WITH_JUSTIFICATION",
    "SUBMIT_PORT",
)


def _in_list(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


ORCHESTRATION_SCHEMA = f"""
-- ORCHESTRATION_DESIGN.md mục 2 + DASHBOARD_DESIGN.md mục 5 điểm 1: ghi CẢ RUNNING lúc bắt
-- đầu, rồi 1 dòng SUCCESS/FAILED lúc kết thúc (append, không UPDATE dòng RUNNING) — trạng
-- thái hiện tại của 1 bước = dòng mới nhất của (attempt_id, step).
CREATE TABLE IF NOT EXISTS pipeline_run_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,                 -- trial, vd "trial-07"
    attempt_id    TEXT NOT NULL,                 -- 1 lần chạy Batch Orchestrator, vd "trial-07.a2"
    step          TEXT NOT NULL CHECK (step IN ({_in_list(PIPELINE_STEPS)})),
    status        TEXT NOT NULL CHECK (status IN ({_in_list(RUN_STATUSES)})),
    error_message TEXT,
    detail        TEXT,                          -- JSON tuỳ bước (số đếm, cờ skipped...)
    timestamp     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pipeline_run_log_run ON pipeline_run_log (run_id, attempt_id, step);

-- DASHBOARD_DESIGN.md mục 2 — schema quyết định TỔNG QUÁT, append-only.
CREATE TABLE IF NOT EXISTS human_decisions (
    decision_id          TEXT PRIMARY KEY,
    reason_code          TEXT NOT NULL CHECK (reason_code IN ({_in_list(REASON_CODES)})),
    target_ref           TEXT NOT NULL,          -- JSON nguyên văn client gửi
    target_identity_key  TEXT,                   -- tách ra để JOIN (cạnh dependency)
    target_run_id        TEXT,                   -- NULL = áp cho mọi trial
    target_resource      TEXT,                   -- "kind/namespace/name" (Continuity Monitor)
    decided_by           TEXT NOT NULL,
    decided_at           TEXT NOT NULL,
    action               TEXT NOT NULL CHECK (action IN ({_in_list(ACTIONS)})),
    payload              TEXT NOT NULL DEFAULT '{{}}'
);
CREATE INDEX IF NOT EXISTS idx_human_decisions_target ON human_decisions (target_identity_key, target_run_id);
CREATE INDEX IF NOT EXISTS idx_human_decisions_resource ON human_decisions (target_resource);

CREATE TRIGGER IF NOT EXISTS human_decisions_no_update
BEFORE UPDATE ON human_decisions
BEGIN SELECT RAISE(ABORT, 'human_decisions là append-only: không UPDATE, hãy ghi 1 quyết định MỚI'); END;

CREATE TRIGGER IF NOT EXISTS human_decisions_no_delete
BEFORE DELETE ON human_decisions
BEGIN SELECT RAISE(ABORT, 'human_decisions là append-only: không DELETE'); END;

-- ORCHESTRATION_DESIGN.md mục 7 — "log đầy đủ mọi lời gọi" tool của Tier 2 qua gatekeeper,
-- kể cả lời gọi bị từ chối / bị rate-limit.
CREATE TABLE IF NOT EXISTS agent_tool_call_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name     TEXT NOT NULL,
    request_json  TEXT NOT NULL,
    caller        TEXT,
    status        TEXT NOT NULL CHECK (status IN ('OK', 'REJECTED', 'RATE_LIMITED', 'ERROR')),
    reason        TEXT,
    result_count  INTEGER,
    latency_ms    REAL,
    created_at    TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS agent_tool_call_log_no_update
BEFORE UPDATE ON agent_tool_call_log
BEGIN SELECT RAISE(ABORT, 'agent_tool_call_log là append-only'); END;

CREATE TRIGGER IF NOT EXISTS agent_tool_call_log_no_delete
BEFORE DELETE ON agent_tool_call_log
BEGIN SELECT RAISE(ABORT, 'agent_tool_call_log là append-only'); END;

-- Trạng thái hiện tại của từng (attempt, step) = dòng mới nhất.
CREATE VIEW IF NOT EXISTS pipeline_step_status AS
SELECT p.*
FROM pipeline_run_log p
WHERE p.id = (
    SELECT MAX(q.id) FROM pipeline_run_log q
    WHERE q.attempt_id = p.attempt_id AND q.step = p.step
);

-- Lần chạy THÀNH CÔNG mới nhất của mỗi trial — kết quả "chính thức" của trial đó. Attempt
-- FAILED (kể cả đã ghi một phần dependency_evidence trước khi lỗi) không bao giờ lọt vào.
CREATE VIEW IF NOT EXISTS latest_successful_attempt AS
SELECT run_id, attempt_id, timestamp AS finished_at
FROM pipeline_run_log
WHERE id IN (
    SELECT MAX(id) FROM pipeline_run_log
    WHERE step = 'trial' AND status = 'SUCCESS'
    GROUP BY run_id
);

-- ORCHESTRATION_DESIGN.md mục 3: VIEW chỉ đọc = evidence (attempt thành công mới nhất)
-- JOIN quyết định người MỚI NHẤT theo identity_key. Quyết định có target_run_id = NULL áp
-- cho mọi trial; có target_run_id thì chỉ áp cho trial đó. Chạy lại trial -> attempt mới,
-- nhưng quyết định vẫn khớp theo identity_key nên KHÔNG mất.
CREATE VIEW IF NOT EXISTS dependency_current_state AS
SELECT
    e.*,
    d.decision_id   AS decision_id,
    d.reason_code   AS decision_reason_code,
    d.action        AS decision_action,
    d.decided_by    AS decision_decided_by,
    d.decided_at    AS decision_decided_at,
    d.payload       AS decision_payload
FROM dependency_evidence e
JOIN latest_successful_attempt s
  ON e.run_id = s.run_id AND e.attempt_id = s.attempt_id
LEFT JOIN human_decisions d ON d.decision_id = (
    SELECT d2.decision_id FROM human_decisions d2
    WHERE d2.target_identity_key = e.identity_key
      AND (d2.target_run_id IS NULL OR d2.target_run_id = e.run_id)
    ORDER BY d2.decided_at DESC, d2.rowid DESC
    LIMIT 1
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Kết nối đọc/ghi, WAL, autocommit (isolation_level=None) — mỗi câu ghi commit ngay
    để tiến trình kia (Batch <-> API Server) đọc thấy real-time, không giữ transaction dài."""
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.row_factory = sqlite3.Row
    return conn


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Kết nối CHỈ ĐỌC (URI mode=ro) cho các route đọc của API Server — SQLite tự từ chối
    mọi câu ghi trên kết nối này (lớp phòng thủ thứ 2 sau việc code không có câu ghi)."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Tạo/bổ sung toàn bộ bảng + view (idempotent, chỉ DDL)."""
    from tier1 import storage as tier1_storage
    from tier1.config_collector import store as collector_store
    from tier2_agent import storage as tier2_storage

    conn.executescript(tier1_storage.SCHEMA)
    tier1_storage.migrate(conn)
    conn.executescript(collector_store.SCHEMA)
    collector_store.ensure_quality_log_table(conn)
    conn.executescript(tier2_storage._SCHEMA)
    tier2_storage.migrate(conn)
    conn.executescript(ORCHESTRATION_SCHEMA)


def open_db(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(db_path)
    ensure_schema(conn)
    return conn
