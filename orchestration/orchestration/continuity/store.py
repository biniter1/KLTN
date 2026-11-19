"""Module DUY NHẤT ghi `config_change_log` và `blocked_connection_log` (append-only).

Nạp lại cùng 1 file log nhiều lần không sinh dòng trùng: khóa `audit_id` / `event_key` là
UNIQUE và câu ghi dùng `INSERT OR IGNORE`.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Iterable

from ..run_log import utc_now_iso
from .audit import ConfigChange
from .blocked import BlockedConnection


def insert_config_changes(conn: sqlite3.Connection, changes: Iterable[ConfigChange]) -> int:
    now = utc_now_iso()
    added = 0
    for c in changes:
        cur = conn.execute(
            "INSERT OR IGNORE INTO config_change_log (change_id, audit_id, resource_kind, resource_namespace, "
            "resource_name, change_type, source, actor, exception_label, git_commit_sha, changed_at, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (f"chg_{uuid.uuid4().hex[:12]}", c.audit_id, c.resource_kind, c.resource_namespace, c.resource_name,
             c.change_type, c.source, c.actor, c.exception_label, c.changed_at, now),
        )
        added += cur.rowcount
    return added


def insert_blocked_connections(conn: sqlite3.Connection, events: Iterable[BlockedConnection]) -> int:
    now = utc_now_iso()
    added = 0
    for e in events:
        cur = conn.execute(
            "INSERT OR IGNORE INTO blocked_connection_log (event_key, flow_key, observed_at, source_namespace, "
            "source_workload, source_service_account, source_ip, destination_namespace, destination_workload, "
            "destination_ip, protocol, port, reporter, detail, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (e.event_key, e.flow_key, e.observed_at, e.source_namespace, e.source_workload,
             e.source_service_account, e.source_ip, e.destination_namespace, e.destination_workload,
             e.destination_ip, e.protocol, e.port, e.reporter, json.dumps(e.detail, ensure_ascii=False), now),
        )
        added += cur.rowcount
    return added
