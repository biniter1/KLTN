#!/usr/bin/env python3
"""Ablation hysteresis (TIER1_DESIGN.md mục 13.4) — 7 snapshot cắt từ 7 ngày dữ liệu đã thu
(chốt 15/10/2026). Cần file trial xuất bằng 08_export_trial_data.py bản mới (có
`series_daily_counts`); file cũ thì chạy lại 08_export trên snapshot Prometheus đã chụp.

Dùng:
    python3 10_hysteresis_ablation.py ../trial_data/trial-01.json ../trial_data/trial-07.json \\
        --tier1-path ../../tier1_inference --out ../hysteresis_ablation.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("trial_files", nargs="+", help="1–2 file trial JSON (mục 13.4)")
    parser.add_argument("--tier1-path", default=str(Path(__file__).resolve().parent.parent.parent / "tier1_inference"))
    parser.add_argument("--snapshots", type=int, default=7)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    sys.path.insert(0, str(Path(args.tier1_path).resolve()))
    from tier1.ablation import run_hysteresis_ablation
    from tier1.trial_io import load_trial

    out = {}
    for f in args.trial_files:
        loaded = load_trial(f)
        if loaded.export_format < 2:
            raise SystemExit(f"LỖI: '{f}' xuất trước 18/10/2026, không có series_daily_counts — chạy lại 08_export.")
        t = loaded.trial
        report = run_hysteresis_ablation(t.traffic_series, loaded.daily_counts, t.config_evidence,
                                         trial_id=t.trial_id, n_snapshots=args.snapshots)
        summary = report.summary()
        out[t.trial_id] = {"days": report.days, **summary,
                           "flips_hysteresis": report.hysteresis.flips(),
                           "flips_single_threshold": report.single_threshold.flips()}
        print(f"[{t.trial_id}] {len(report.days)} snapshot ({report.days[0]} .. {report.days[-1]})")
        for name in ("hysteresis", "single_threshold"):
            s = summary[name]
            print(f"  {name:17s} cạnh={s['edges']:3d}  tổng lần đổi={s['total_flips']:3d}  "
                  f"cạnh có đổi={s['edges_with_flip']:3d}  tối đa/cạnh={s['max_flips_per_edge']}")
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False))
        print(f"Đã ghi: {args.out}")


if __name__ == "__main__":
    main()
