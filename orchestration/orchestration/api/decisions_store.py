"""Module DUY NHẤT trong toàn codebase có câu INSERT vào `human_decisions`.

Append-only 3 lớp: (1) không có câu UPDATE/DELETE nào ở đây; (2) trigger SQLite chặn
UPDATE/DELETE (db.py); (3) test quét mã nguồn đảm bảo không module nào khác ghi bảng này
(tests/test_write_boundaries.py).
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from ..run_log import utc_now_iso
from .review import NormalizedTarget


def insert_decision(conn: sqlite3.Connection, *, reason_code: str, target_ref: dict,
                    target: NormalizedTarget, decided_by: str, action: str, payload: dict) -> dict:
    row = {
        "decision_id": f"dec_{uuid.uuid4().hex[:16]}",
        "reason_code": reason_code,
        "target_ref": json.dumps(target_ref, ensure_ascii=False, sort_keys=True),
        "target_identity_key": target.identity_key,
        "target_run_id": target.run_id,
        "target_resource": target.resource,
        "decided_by": decided_by,
        "decided_at": utc_now_iso(),
        "action": action,
        "payload": json.dumps(payload, ensure_ascii=False, sort_keys=True),
    }
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO human_decisions ({cols}) VALUES ({', '.join('?' for _ in row)})",
                 tuple(row.values()))
    return row


def migrate_decision_checks(conn: sqlite3.Connection) -> bool:
    """SQLite không ALTER được CHECK: file DB tạo trước khi thêm reason_code/action mới
    (vd NEEDS_MANUAL_PORT, SUBMIT_PORT — 22/10/2026) giữ CHECK cũ và từ chối quyết định mới.
    Dựng lại bảng với CHECK mới, GIỮ NGUYÊN mọi dòng (chỉ đổi ràng buộc, không sửa dữ liệu),
    rồi chạy lại DDL để tạo lại index + trigger append-only. Trả True nếu đã dựng lại.

    Đặt ở đây (không ở db.py) để giữ luật "chỉ decisions_store ghi human_decisions"."""
    from ..db import ACTIONS, ORCHESTRATION_SCHEMA, REASON_CODES

    found = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='human_decisions'").fetchone()
    if found is None:
        return False
    sql = found[0] or ""
    if all(f"'{c}'" in sql for c in REASON_CODES) and all(f"'{a}'" in sql for a in ACTIONS):
        return False
    body = ORCHESTRATION_SCHEMA.split("CREATE TABLE IF NOT EXISTS human_decisions", 1)[1]
    create = "CREATE TABLE human_decisions" + body.split(");", 1)[0] + ");"
    conn.execute("BEGIN")
    try:
        # View tham chiếu bảng: RENAME sẽ viết lại view sang tên _old rồi hỏng khi DROP -> bỏ
        # view trước, DDL cuối hàm tạo lại.
        conn.execute("DROP VIEW IF EXISTS dependency_current_state")
        conn.execute("ALTER TABLE human_decisions RENAME TO human_decisions_old")
        conn.execute(create)
        conn.execute("INSERT INTO human_decisions SELECT * FROM human_decisions_old")
        conn.execute("DROP TABLE human_decisions_old")  # kéo theo index/trigger cũ
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.executescript(ORCHESTRATION_SCHEMA)  # tạo lại index + trigger (IF NOT EXISTS)
    return True
