"""Module DUY NHẤT ghi `emergency_exceptions` (append-only: chỉ ghi lời khai báo).

Gia hạn / đóng / hợp thức hóa KHÔNG sửa dòng này mà ghi quyết định mới qua
`decisions_store.insert_decision` (reason_code EMERGENCY_EXCEPTION_EXPIRED) — xem
`exceptions.effective_state`.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from ..run_log import utc_now_iso
from .exceptions import resource_name


def insert_exception(conn: sqlite3.Connection, rec: dict) -> dict:
    exception_id = f"exc_{uuid.uuid4().hex[:12]}"
    row = {
        "exception_id": exception_id,
        "requested_by": rec["requested_by"],
        "reason": rec["reason"],
        "ticket": rec["ticket"],
        "source_namespace": rec["source_namespace"],
        "source_service": rec["source_service"],
        "source_service_account": rec.get("source_service_account"),
        "destination_namespace": rec["destination_namespace"],
        "destination_service": rec["destination_service"],
        "protocol": rec["protocol"],
        "port": rec["port"],
        "source_pod_selector": json.dumps(rec["source_pod_selector"], sort_keys=True),
        "destination_pod_selector": json.dumps(rec["destination_pod_selector"], sort_keys=True),
        "resource_kind": "NetworkPolicy",
        "resource_name": resource_name(exception_id),
        "blocked_flow_key": rec.get("blocked_flow_key"),
        "created_at": rec.get("created_at") or utc_now_iso(),
        "expires_at": rec["expires_at"],
    }
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO emergency_exceptions ({cols}) VALUES ({', '.join('?' for _ in row)})",
                 tuple(row.values()))
    return row
