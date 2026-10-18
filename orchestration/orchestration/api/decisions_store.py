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
