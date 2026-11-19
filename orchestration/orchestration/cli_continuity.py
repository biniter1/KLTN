"""Continuity Monitor — lượt quét định kỳ (sweep). Chạy bằng cron/systemd timer, NGOÀI cụm:

    # nạp Audit Log + log kết nối bị chặn, rồi in các việc đang mở
    python -m orchestration.cli_continuity --db ../kltn.sqlite3 \\
        --audit-log /var/log/kubernetes/audit/audit.log \\
        --blocked-log hubble.json --blocked-format cilium \\
        --victorialogs-url http://localhost:9428 --since-hours 24

Mọi nguồn đều là dữ liệu cụm ĐÃ ghi sẵn; lệnh này chỉ đọc chúng và ghi vào DB của công cụ
(`config_change_log`, `blocked_connection_log`, append-only, nạp lại không trùng).
Ngoại lệ quá hạn không cần ghi gì: trạng thái suy ra lúc đọc, nên phần báo cáo cuối luôn mới.

Exit code: 0 = không còn việc mở mức cao; 2 = còn việc mức cao (để cron/CI gửi cảnh báo).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import _paths  # noqa: F401
from .continuity.audit import DEFAULT_GITOPS_ACTORS, parse_audit_log
from .continuity.blocked import FORMATS, collect_ztunnel_denied, parse_blocked_log
from .continuity.store import insert_blocked_connections, insert_config_changes
from .db import connect_readonly, open_db


def run(args, *, logs_client=None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    conn = open_db(args.db)
    summary = {"config_changes_added": 0, "blocked_events_added": 0}
    try:
        if args.audit_log:
            gitops = tuple(args.gitops_actor) or DEFAULT_GITOPS_ACTORS
            with open(args.audit_log, encoding="utf-8") as fh:
                changes = parse_audit_log(fh, gitops_actors=gitops)
            summary["config_changes_added"] = insert_config_changes(conn, changes)
        for path, fmt in zip(args.blocked_log, args.blocked_format):
            with open(path, encoding="utf-8") as fh:
                summary["blocked_events_added"] += insert_blocked_connections(conn, parse_blocked_log(fh, fmt))
        if logs_client is not None or args.victorialogs_url:
            if logs_client is None:  # pragma: no cover - VictoriaLogs thật
                from tier1.log_collector import HttpVictoriaLogsClient
                logs_client = HttpVictoriaLogsClient(args.victorialogs_url)
            denied = collect_ztunnel_denied(logs_client, now - timedelta(hours=args.since_hours), now)
            summary["blocked_events_added"] += insert_blocked_connections(conn, denied)
    finally:
        conn.close()

    from .api.readers import continuity_review_items

    ro = connect_readonly(args.db)
    try:
        items = continuity_review_items(ro, now=now)
    finally:
        ro.close()
    summary["open_items"] = [{"reason_code": i["reason_code"], "urgency": i["urgency"],
                              "target_ref": i["target_ref"]} for i in items]
    summary["open_high"] = sum(1 for i in items if i["urgency"] == "high")
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--audit-log", help="file audit log của kube-apiserver (JSON lines)")
    p.add_argument("--gitops-actor", action="append", default=[],
                   help="tiền tố username của controller GitOps (lặp lại được); mặc định Argo CD + Flux")
    p.add_argument("--blocked-log", action="append", default=[], help="file log kết nối bị chặn (JSON lines)")
    p.add_argument("--blocked-format", action="append", default=[], choices=FORMATS,
                   help="định dạng tương ứng từng --blocked-log")
    p.add_argument("--victorialogs-url", help="lấy thêm kết nối ztunnel bị từ chối từ VictoriaLogs")
    p.add_argument("--since-hours", type=float, default=24.0)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if len(args.blocked_log) != len(args.blocked_format):
        print("mỗi --blocked-log cần đúng 1 --blocked-format", file=sys.stderr)
        return 1
    summary = run(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if summary["open_high"] else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
