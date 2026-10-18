"""Lưu trữ SQLite — `trace_log` + `agent_rounds` (TIER2_DESIGN.md mục 11).

`PRAGMA journal_mode=WAL` theo đúng chốt ở `ORCHESTRATION_DESIGN.md` mục 3 —
1 writer (`Brain`) + nhiều reader (API Server phục vụ dashboard) đọc/ghi
đồng thời, không cần export/push.

NOTE (xem README "Điểm mở/diễn giải"): thiết kế liệt kê 5 field State phải
"LƯU THẬT" (mục 5) nhưng không đặt tên 1 bảng cụ thể để chứa chúng — bảng
`agent_sessions` ở đây là suy luận hợp lý để `manual_abort_flag` có chỗ ghi
bất đồng bộ từ nút bấm dashboard (mục 5, `ORCHESTRATION_DESIGN.md` mục 5),
không phải 1 bảng được đặt tên tường minh trong tài liệu gốc.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import List, Optional

from .models import EvidenceRow, ModelOutput, SessionState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    run_id TEXT PRIMARY KEY,
    edge_identity_key TEXT NOT NULL,
    session_started_at REAL NOT NULL,
    manual_abort_flag INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT 'RUNNING'
);

CREATE TABLE IF NOT EXISTS trace_log (
    evidence_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    hypothesis_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_input TEXT NOT NULL,
    tool_output TEXT,
    stance TEXT,
    stale INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error_message TEXT,
    injection_flagged INTEGER NOT NULL DEFAULT 0,
    sanitized_flagged INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    pr_number TEXT
);
CREATE INDEX IF NOT EXISTS idx_trace_log_run ON trace_log(run_id);

CREATE TABLE IF NOT EXISTS agent_rounds (
    run_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    reasoning TEXT,
    reasoning_injection_flagged INTEGER NOT NULL DEFAULT 0,
    stance_updates TEXT,
    new_hypotheses TEXT,
    action TEXT,
    system_notices TEXT,
    stopped_by TEXT,
    stop_reason TEXT,
    timestamp REAL NOT NULL,
    PRIMARY KEY (run_id, iteration)
);
"""


# Cột thêm sau khi rà soát cho dashboard (DASHBOARD_DESIGN.md mục 3.B). File SQLite tạo
# bởi bản TRƯỚC khi vá sẽ không có 2 cột này (CREATE TABLE IF NOT EXISTS không sửa bảng
# đã có) -> `append_agent_round` sẽ lỗi "no such column". `migrate()` tự bổ sung.
_ADDED_AGENT_ROUNDS_COLUMNS = (
    ("reasoning_injection_flagged", "INTEGER NOT NULL DEFAULT 0"),
    ("system_notices", "TEXT"),
)


def migrate(conn: sqlite3.Connection) -> None:
    """Idempotent — chỉ THÊM cột thiếu, không đổi/xoá cột cũ."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(agent_rounds)")}
    for name, col_type in _ADDED_AGENT_ROUNDS_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE agent_rounds ADD COLUMN {name} {col_type}")


class Storage:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        migrate(self._conn)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- agent_sessions (State, mục 5) ---

    def create_session(self, state: SessionState) -> None:
        self._conn.execute(
            "INSERT INTO agent_sessions (run_id, edge_identity_key, session_started_at, "
            "manual_abort_flag, phase) VALUES (?, ?, ?, ?, ?)",
            (
                state.run_id,
                state.edge_identity_key,
                state.session_started_at,
                int(state.manual_abort_flag),
                state.phase,
            ),
        )
        self._conn.commit()

    def get_session(self, run_id: str) -> Optional[SessionState]:
        row = self._conn.execute(
            "SELECT * FROM agent_sessions WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return SessionState(
            run_id=row["run_id"],
            edge_identity_key=row["edge_identity_key"],
            session_started_at=row["session_started_at"],
            manual_abort_flag=bool(row["manual_abort_flag"]),
            phase=row["phase"],
        )

    def set_manual_abort(self, run_id: str, flag: bool = True) -> None:
        """Ghi bất đồng bộ khi dashboard bấm nút (mục 9.1: manual abort)."""

        self._conn.execute(
            "UPDATE agent_sessions SET manual_abort_flag = ? WHERE run_id = ?",
            (int(flag), run_id),
        )
        self._conn.commit()

    def set_phase(self, run_id: str, phase: str) -> None:
        self._conn.execute(
            "UPDATE agent_sessions SET phase = ? WHERE run_id = ?", (phase, run_id)
        )
        self._conn.commit()

    # --- trace_log (mục 11.1) ---

    def append_trace_row(self, row: EvidenceRow) -> None:
        self._conn.execute(
            "INSERT INTO trace_log (evidence_id, run_id, iteration, hypothesis_id, "
            "tool_name, tool_input, tool_output, stance, stale, status, error_message, "
            "injection_flagged, sanitized_flagged, truncated, pr_number) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.evidence_id,
                row.run_id,
                row.iteration,
                row.hypothesis_id,
                row.tool_name,
                json.dumps(row.tool_input, ensure_ascii=False),
                row.tool_output,
                row.stance,
                int(row.stale),
                row.status,
                row.error_message,
                int(row.injection_flagged),
                int(row.sanitized_flagged),
                int(row.truncated),
                row.pr_number,
            ),
        )
        self._conn.commit()

    def update_stance(self, evidence_id: str, stance: str) -> bool:
        cur = self._conn.execute(
            "UPDATE trace_log SET stance = ? WHERE evidence_id = ?", (stance, evidence_id)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def evidence_exists(self, evidence_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM trace_log WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        return row is not None

    def get_trace_rows(self, run_id: str) -> List[EvidenceRow]:
        rows = self._conn.execute(
            "SELECT * FROM trace_log WHERE run_id = ? ORDER BY iteration ASC", (run_id,)
        ).fetchall()
        return [
            EvidenceRow(
                evidence_id=r["evidence_id"],
                run_id=r["run_id"],
                iteration=r["iteration"],
                hypothesis_id=r["hypothesis_id"],
                tool_name=r["tool_name"],
                tool_input=json.loads(r["tool_input"]),
                tool_output=r["tool_output"],
                stance=r["stance"],
                stale=bool(r["stale"]),
                status=r["status"],
                error_message=r["error_message"],
                injection_flagged=bool(r["injection_flagged"]),
                sanitized_flagged=bool(r["sanitized_flagged"]),
                truncated=bool(r["truncated"]),
                pr_number=r["pr_number"],
            )
            for r in rows
        ]

    def find_dedup_row(self, run_id: str, tool_name: str, tool_input_key: str) -> Optional[EvidenceRow]:
        """Tra `trace_log` theo (tool_name, tool_input) đã chuẩn hoá, loại
        dòng status=ERROR (mục 10: dedup, retry phải thực sự chạy lại)."""

        for row in self.get_trace_rows(run_id):
            if row.tool_name != tool_name or row.status == "ERROR":
                continue
            from .utils import canonical_json_key

            if canonical_json_key(row.tool_input) == tool_input_key:
                return row
        return None

    # --- agent_rounds (mục 11.2) ---

    def append_agent_round(
        self,
        run_id: str,
        iteration: int,
        model_output: ModelOutput,
        *,
        stopped_by: Optional[str],
        stop_reason: Optional[str],
        reasoning_injection_flagged: bool = False,
        system_notices: Optional[List[str]] = None,
    ) -> None:
        """reasoning_injection_flagged/system_notices — DASHBOARD_DESIGN.md mục 3
        (Category B): trước đây `check_injection(model_output.reasoning)` chỉ dùng
        `.text` để làm sạch reasoning, `.flagged` bị bỏ luôn (không cách nào biết
        SAU NÀY một round từng bị flag injection hay không); tương tự,
        `pending_notices` chỉ dùng để nhắc round SAU trong context, chưa bao giờ
        được lưu lại cho chính round đã sinh ra chúng. 2 cột này lấp đúng 2 khoảng
        trống đó — không đổi hành vi runtime của Brain, chỉ thêm ghi log."""
        self._conn.execute(
            "INSERT INTO agent_rounds (run_id, iteration, reasoning, "
            "reasoning_injection_flagged, stance_updates, new_hypotheses, action, "
            "system_notices, stopped_by, stop_reason, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                iteration,
                model_output.reasoning,
                int(reasoning_injection_flagged),
                json.dumps(
                    [su.__dict__ for su in model_output.stance_updates], ensure_ascii=False
                ),
                json.dumps(
                    [nh.__dict__ for nh in model_output.new_hypotheses], ensure_ascii=False
                ),
                model_output.action,
                json.dumps(system_notices or [], ensure_ascii=False),
                stopped_by,
                stop_reason,
                time.time(),
            ),
        )
        self._conn.commit()

    def get_agent_rounds(self, run_id: str) -> List[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM agent_rounds WHERE run_id = ? ORDER BY iteration ASC", (run_id,)
        ).fetchall()

    # --- live counts (mục 5: TÍNH LẠI mỗi lần cần, không lưu) ---

    def iteration_count(self, run_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT iteration) AS c FROM agent_rounds WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return row["c"] or 0

    def tools_called(self, run_id: str) -> set:
        rows = self._conn.execute(
            "SELECT DISTINCT tool_name FROM trace_log WHERE run_id = ?", (run_id,)
        ).fetchall()
        return {r["tool_name"] for r in rows}

    def hypotheses_opened_count(self, run_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT hypothesis_id) AS c FROM trace_log WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return row["c"] or 0
