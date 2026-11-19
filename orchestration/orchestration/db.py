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
    emergency_exceptions       | API Server (exceptions_store| APPEND-ONLY (trigger) — khai
                               |  .py)                       |  báo; trạng thái suy từ quyết
                               |                             |  định + thời gian (không sửa dòng)
    config_change_log,         | Continuity Monitor          | APPEND-ONLY (trigger), khử
    blocked_connection_log     |  (continuity/store.py)      |  trùng theo auditID / event_key

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

# DASHBOARD_DESIGN.md mục 2 — 11 reason_code + AGENT_REJECTED (05/11).
REASON_CODES = (
    "CLEAN_CONFIG_MATCH",
    "NEEDS_MANUAL_SELECTOR",
    "HIGH_BLAST_RADIUS",
    "CONFLICTING_EVIDENCE",
    "AGENT_INSUFFICIENT_BASIS",
    "AGENT_REJECTED",
    "TOOL_ADMIN_ATTENTION",
    "POLICY_WOULD_BREAK",
    "POLICY_UNSUPPORTED_BY_EVIDENCE",
    "UNATTRIBUTED_CONFIG_CHANGE",
    "EMERGENCY_EXCEPTION_EXPIRED",
    "STALE_PERMISSION",
    # 19/11 (báo cáo mục 3.10.4): kết nối bị chặn sau khi áp dụng -> người quản trị quyết:
    # hợp lệ (APPROVE + exception_id đã mở) hay sự cố bảo mật (REJECT + incident_note).
    "BLOCKED_CONNECTION",
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


# MIGRATION_PLANNER_DESIGN.md mục 2.2-2.4 + báo cáo mục 3.10.4-3.10.5 (chốt 19/11).
CONTINUITY_SCHEMA = """
-- Sổ đăng ký ngoại lệ khẩn cấp (Bảng 3.9). APPEND-ONLY: chỉ ghi lời khai báo. Trạng thái
-- (ACTIVE / EXPIRED / REVOKED / MADE_PERMANENT) và hạn hiện hành SUY RA lúc đọc từ
-- expires_at + quyết định của người (human_decisions, target_resource =
-- 'emergency_exception/<id>') + thời điểm hiện tại -> không cần tiến trình nào UPDATE.
CREATE TABLE IF NOT EXISTS emergency_exceptions (
    exception_id             TEXT PRIMARY KEY,
    requested_by             TEXT NOT NULL,
    reason                   TEXT NOT NULL,
    ticket                   TEXT NOT NULL,
    source_namespace         TEXT NOT NULL,
    source_service           TEXT NOT NULL,
    source_service_account   TEXT,
    destination_namespace    TEXT NOT NULL,
    destination_service      TEXT NOT NULL,
    protocol                 TEXT NOT NULL,
    port                     INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    source_pod_selector      TEXT NOT NULL,       -- JSON matchLabels
    destination_pod_selector TEXT NOT NULL,       -- JSON matchLabels
    resource_kind            TEXT NOT NULL,       -- 'NetworkPolicy'
    resource_name            TEXT NOT NULL,       -- tên policy ngoại lệ (ở ns nguồn + ns đích)
    blocked_flow_key         TEXT,                -- kết nối bị chặn đã dẫn tới ngoại lệ này
    created_at               TEXT NOT NULL,
    expires_at               TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS emergency_exceptions_no_update
BEFORE UPDATE ON emergency_exceptions
BEGIN SELECT RAISE(ABORT, 'emergency_exceptions là append-only: gia hạn/đóng = ghi quyết định MỚI'); END;

CREATE TRIGGER IF NOT EXISTS emergency_exceptions_no_delete
BEFORE DELETE ON emergency_exceptions
BEGIN SELECT RAISE(ABORT, 'emergency_exceptions là append-only'); END;

-- Mọi thay đổi chính sách mạng thật, đọc từ Kubernetes Audit Log (mục 2.3-2.4).
CREATE TABLE IF NOT EXISTS config_change_log (
    change_id          TEXT PRIMARY KEY,
    audit_id           TEXT NOT NULL UNIQUE,      -- auditID của sự kiện -> nạp lại không trùng
    resource_kind      TEXT NOT NULL,
    resource_namespace TEXT NOT NULL,
    resource_name      TEXT NOT NULL,
    change_type        TEXT NOT NULL CHECK (change_type IN ('CREATE', 'UPDATE', 'DELETE')),
    source             TEXT NOT NULL CHECK (source IN ('GITOPS', 'IMPERATIVE')),
    actor              TEXT NOT NULL,
    exception_label    TEXT,                      -- nhãn kltn.zt/exception-id trong requestObject
    git_commit_sha     TEXT,
    changed_at         TEXT NOT NULL,
    detected_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_config_change_resource
    ON config_change_log (resource_kind, resource_namespace, resource_name);

CREATE TRIGGER IF NOT EXISTS config_change_log_no_update
BEFORE UPDATE ON config_change_log
BEGIN SELECT RAISE(ABORT, 'config_change_log là append-only'); END;

CREATE TRIGGER IF NOT EXISTS config_change_log_no_delete
BEFORE DELETE ON config_change_log
BEGIN SELECT RAISE(ABORT, 'config_change_log là append-only'); END;

-- Kết nối bị chặn sau khi áp dụng chính sách (mục 3.10.4) — flow log của CNI hoặc log ztunnel.
CREATE TABLE IF NOT EXISTS blocked_connection_log (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key              TEXT NOT NULL UNIQUE,  -- băm nội dung sự kiện -> nạp lại không trùng
    flow_key               TEXT NOT NULL,         -- 'ns|workload|ns|workload|proto|port'
    observed_at            TEXT NOT NULL,
    source_namespace       TEXT,
    source_workload        TEXT,
    source_service_account TEXT,
    source_ip              TEXT,
    destination_namespace  TEXT,
    destination_workload   TEXT,
    destination_ip         TEXT,
    protocol               TEXT,
    port                   INTEGER,
    reporter               TEXT NOT NULL,         -- cilium | calico | ztunnel | generic
    detail                 TEXT,                  -- JSON: lý do chặn, policy, ...
    ingested_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocked_flow ON blocked_connection_log (flow_key, observed_at);

CREATE TRIGGER IF NOT EXISTS blocked_connection_log_no_update
BEFORE UPDATE ON blocked_connection_log
BEGIN SELECT RAISE(ABORT, 'blocked_connection_log là append-only'); END;

CREATE TRIGGER IF NOT EXISTS blocked_connection_log_no_delete
BEFORE DELETE ON blocked_connection_log
BEGIN SELECT RAISE(ABORT, 'blocked_connection_log là append-only'); END;
"""


def _migrate_human_decisions_check(conn: sqlite3.Connection) -> None:
    """File DB cũ có CHECK(reason_code IN ...) thiếu reason_code mới -> dựng lại bảng, chép
    nguyên dữ liệu (không mất, không sửa quyết định nào), dựng lại trigger append-only.
    SQLite không ALTER được CHECK nên đây là cách duy nhất."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='human_decisions'").fetchone()
    if row is None or all(f"'{c}'" in row[0] for c in REASON_CODES):
        return
    new_sql = ORCHESTRATION_SCHEMA.split("CREATE TABLE IF NOT EXISTS human_decisions (", 1)[1].split(");", 1)[0]
    conn.execute("BEGIN")
    try:
        # RENAME viết lại tham chiếu trong view -> bỏ view trước, ORCHESTRATION_SCHEMA dựng lại ngay sau
        conn.execute("DROP VIEW IF EXISTS dependency_current_state")
        conn.execute("DROP TRIGGER IF EXISTS human_decisions_no_update")
        conn.execute("DROP TRIGGER IF EXISTS human_decisions_no_delete")
        conn.execute("ALTER TABLE human_decisions RENAME TO human_decisions_pre_migration")
        conn.execute(f"CREATE TABLE human_decisions ({new_sql})")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(human_decisions_pre_migration)")]
        col_list = ", ".join(cols)
        conn.execute(f"INSERT INTO human_decisions ({col_list}) SELECT {col_list} FROM human_decisions_pre_migration")
        conn.execute("DROP TABLE human_decisions_pre_migration")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


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
    _migrate_human_decisions_check(conn)
    conn.executescript(ORCHESTRATION_SCHEMA)
    conn.executescript(CONTINUITY_SCHEMA)


def open_db(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(db_path)
    ensure_schema(conn)
    return conn
