#!/usr/bin/env python3
"""
Chạy thử Tier 1 end-to-end với dữ liệu giả lập (simulate_data.py), KHÔNG cần cluster
/ Prometheus thật — đúng yêu cầu "ví dụ chạy thử với dữ liệu giả lập".

Chạy 2 "snapshot" liên tiếp trên CÙNG một SQLite DB để minh họa:
  - Toàn bộ 5 trạng thái + DNS auto-allow + NEEDS_MANUAL_SELECTOR (run1)
  - Cơ chế hysteresis bất đối xứng mục 5b (so sánh run1 vs run2)

Cách chạy:
    cd /home/claude/tier1_inference
    python3 examples/run_example.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tier1 import storage
from tier1.config import DEFAULT_CONFIG
from tier1.pipeline import run_tier1
from tier1.selector_validation import MockSelectorValidator

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from simulate_data import build_snapshot  # noqa: E402


COL_WIDTHS = (28, 22, 9, 9, 8, 24, 10, 22)
HEADERS = (
    "source", "destination", "proto/port", "count", "score",
    "dependency_status", "escalate?", "ghi chú",
)


def _fmt_row(*cols) -> str:
    return " | ".join(str(c).ljust(w) for c, w in zip(cols, COL_WIDTHS))


def print_report(results, title: str) -> None:
    print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")
    print(_fmt_row(*HEADERS))
    print("-" * 130)
    for r in sorted(results, key=lambda x: (x.identity_key.source_canonical_service, x.identity_key.destination_canonical_service)):
        k = r.identity_key
        note = []
        if r.dns_auto_allow:
            note.append("DNS auto-allow")
        if r.needs_human_review:
            note.append("cần review người")
        if r.escalate_to_tier2:
            note.append("-> Tier2 AI Agent")
        if r.manual_selector_reason:
            note.append("selector FAIL")
        if r.unstable:
            note.append("unstable(error cao)")
        proto_port = f"{k.protocol}/{k.port}"
        score_str = f"{r.score:.3f}" if r.score is not None else "-"
        print(_fmt_row(
            f"{k.source_namespace}/{k.source_canonical_service}",
            f"{k.destination_namespace}/{k.destination_canonical_service}",
            proto_port,
            r.count,
            score_str,
            r.dependency_status.value,
            "yes" if r.escalate_to_tier2 else "no",
            ", ".join(note) if note else "-",
        ))
    print()


def main() -> None:
    db_path = os.path.join(tempfile.gettempdir(), "tier1_demo.sqlite3")
    if os.path.exists(db_path):
        os.remove(db_path)  # chạy demo từ đầu mỗi lần, tránh lẫn dữ liệu cũ
    conn = storage.connect(db_path)
    print(f"[i] SQLite demo DB: {db_path}")

    # redis-cart bị giả lập selector "hỏng" (mục 3b) để demo nhánh NEEDS_MANUAL_SELECTOR
    validator = MockSelectorValidator(failing_services={"redis-cart"})

    # ---- Run 1 ----
    traffic1, config_ev1, snap1 = build_snapshot("run1")
    results1 = run_tier1(
        traffic1, config_ev1, snap1, conn=conn,
        config=DEFAULT_CONFIG, selector_validator=validator,
    )
    print_report(results1, f"RUN 1 — snapshot_id={snap1}")

    # ---- Run 2 (demo hysteresis mục 5b) ----
    traffic2, config_ev2, snap2 = build_snapshot("run2")
    results2 = run_tier1(
        traffic2, config_ev2, snap2, conn=conn,
        config=DEFAULT_CONFIG, selector_validator=validator,
    )
    print_report(results2, f"RUN 2 — snapshot_id={snap2}")

    # ---- So sánh hysteresis cho 2 edge tiêu biểu ----
    print("=" * 100)
    print("So sánh hysteresis (mục 5b) giữa run1 -> run2")
    print("=" * 100)
    for dest in ("productcatalogservice", "currencyservice"):
        r1 = next(r for r in results1 if r.identity_key.destination_canonical_service == dest)
        r2 = next(r for r in results2 if r.identity_key.destination_canonical_service == dest)
        print(
            f"  frontend -> {dest:24s} run1: score={r1.score:.3f} status={r1.dependency_status.value:16s}"
            f"   run2: score={r2.score:.3f} status={r2.dependency_status.value:16s}"
            f"   previous_status(run2)={r2.previous_status.value if r2.previous_status else None}"
        )
    print(
        "\nGiải thích: productcatalogservice ở run2 có score tụt nhưng vẫn còn >= theta_demote "
        f"({DEFAULT_CONFIG.theta_demote}) nên GIỮ NGUYÊN OBSERVED_STRONG nhờ previous_status=STRONG "
        "(không bị 'nhấp nháy' dù traffic dao động) — đúng thiết kế hysteresis bất đối xứng.\n"
        "currencyservice ở run2 tăng nhẹ traffic nhưng vẫn dưới theta_promote "
        f"({DEFAULT_CONFIG.theta_promote}) nên TIẾP TỤC là OBSERVED_WEAK (chưa đủ để được 'thăng hạng')."
    )

    conn.close()


if __name__ == "__main__":
    main()
