#!/usr/bin/env python3
"""Nạp mọi file JSON do 08_export_trial_data.py xuất ra, dựng lại `TrialInput`, và chạy
`tier1.experiment.run_experiment()` — bước CUỐI của toàn bộ pipeline R=15 trial
(PROPOSAL.md mục 7 / TIER1_DESIGN.md mục 13): Tier 1 thật vs B1 vs B2, precision/recall/f1
mean±CI95%, 2 lát cắt bổ sung (traffic thưa, HEALTHCHECK/MONITORING), 4 phép kiểm Wilcoxon.

Dùng:
    python3 09_run_experiment.py --trial-data-dir ../trial_data --tier1-path ../../tier1_inference \\
        --out ../experiment_report.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path


def _add_tier1_to_path(tier1_path: str) -> None:
    p = Path(tier1_path).resolve()
    if not p.exists():
        raise SystemExit(f"LỖI: không tìm thấy thư mục tier1_inference tại '{p}'. Dùng --tier1-path.")
    sys.path.insert(0, str(p))


def _load_trial_input(path: Path):
    """Dựng lại `TrialInput` từ JSON — dùng chung `tier1.trial_io` (đọc được cả file cũ
    trước 15/10: port 0 -> None, thiếu traffic_days -> gộp cạnh lấy max)."""
    from tier1.trial_io import load_trial_input

    return load_trial_input(path)


def _report_to_jsonable(report) -> dict:
    def _summary(s) -> dict:
        return {
            "precision_ci": dataclasses.asdict(s.precision_ci),
            "recall_ci": dataclasses.asdict(s.recall_ci),
            "f1_ci": dataclasses.asdict(s.f1_ci),
            "sparse_recall_ci": dataclasses.asdict(s.sparse_recall_ci),
            "noise_precision_ci": dataclasses.asdict(s.noise_precision_ci),
        }

    return {
        "n_trials": report.n_trials,
        "tier1": _summary(report.tier1),
        "b1": _summary(report.b1),
        "b2": _summary(report.b2),
        "tier1_vs_b1_recall": dataclasses.asdict(report.tier1_vs_b1_recall),
        "tier1_vs_b1_precision": dataclasses.asdict(report.tier1_vs_b1_precision),
        "tier1_vs_b2_recall": dataclasses.asdict(report.tier1_vs_b2_recall),
        "tier1_vs_b2_precision": dataclasses.asdict(report.tier1_vs_b2_precision),
    }


def _print_summary(report) -> None:
    def _fmt(ci) -> str:
        return f"{ci.mean:.3f} ± {ci.half_width:.3f}"

    print(f"\n=== Kết quả R={report.n_trials} trial ===\n")
    for name, s in (("Tier 1", report.tier1), ("B1", report.b1), ("B2", report.b2)):
        print(
            f"{name:8s} precision={_fmt(s.precision_ci)}  recall={_fmt(s.recall_ci)}  "
            f"f1={_fmt(s.f1_ci)}"
        )
    print("\n--- Lát cắt traffic thưa (distinct_days<=2), recall ---")
    for name, s in (("Tier 1", report.tier1), ("B1", report.b1), ("B2", report.b2)):
        print(f"{name:8s} recall={_fmt(s.sparse_recall_ci)}")
    print("\n--- Lát cắt nhiễu HEALTHCHECK/MONITORING, precision ---")
    for name, s in (("Tier 1", report.tier1), ("B1", report.b1), ("B2", report.b2)):
        print(f"{name:8s} precision={_fmt(s.noise_precision_ci)}")
    print("\n--- Wilcoxon signed-rank (Tier 1 vs baseline, paired theo trial) ---")
    for label, w in (
        ("Tier1 vs B1, recall", report.tier1_vs_b1_recall),
        ("Tier1 vs B1, precision", report.tier1_vs_b1_precision),
        ("Tier1 vs B2, recall", report.tier1_vs_b2_recall),
        ("Tier1 vs B2, precision", report.tier1_vs_b2_precision),
    ):
        print(f"{label:24s} p={w.p_value}  ({w.note or 'ok'})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trial-data-dir", default=str(Path(__file__).resolve().parent.parent / "trial_data"))
    parser.add_argument(
        "--tier1-path",
        default=str(Path(__file__).resolve().parent.parent.parent / "tier1_inference"),
    )
    parser.add_argument("--out", default=None, help="Ghi report dạng JSON ra file (tuỳ chọn)")
    parser.add_argument(
        "--validate-selectors", action="store_true",
        help="Kiểm selector cạnh STRONG bằng KubernetesSelectorValidator THẬT (chỉ đọc Service/Pod, "
             "cần cluster còn chạy) — đánh giá chính thức nên bật (TIER1_DESIGN.md mục 3b). Mặc định "
             "dùng validator giả (có service_ref là qua) như số liệu cũ.",
    )
    parser.add_argument("--kubeconfig", default=None)
    args = parser.parse_args()

    _add_tier1_to_path(args.tier1_path)
    from tier1.experiment import run_experiment

    selector_validator = None
    if args.validate_selectors:
        from tier1.selector_validation import KubernetesSelectorValidator

        selector_validator = KubernetesSelectorValidator(kubeconfig_path=args.kubeconfig)
        print("Kiểm selector: KubernetesSelectorValidator (cluster thật, chỉ đọc)")
    else:
        print("Kiểm selector: MockSelectorValidator (GIẢ) — thêm --validate-selectors cho đánh giá chính thức")

    trial_data_dir = Path(args.trial_data_dir)
    files = sorted(trial_data_dir.glob("*.json"))
    if not files:
        raise SystemExit(f"LỖI: không thấy file nào trong '{trial_data_dir}' -- chạy 08_export_trial_data.py trước.")

    trials = [_load_trial_input(f) for f in files]
    print(f"Đã nạp {len(trials)} trial: {[t.trial_id for t in trials]}")
    legacy = sum(1 for f in files if json.loads(f.read_text()).get("export_format", 1) < 2)
    if legacy:
        print(f"!!! {legacy} file xuất TRƯỚC 15/10 (không có traffic_days/port) — số ngày của cạnh nhiều path "
              f"bị tính theo max, port không biết. Chạy lại 08_export_trial_data.py để có số đúng.")

    report = run_experiment(trials, selector_validator=selector_validator)
    _print_summary(report)

    if args.out:
        Path(args.out).write_text(json.dumps(_report_to_jsonable(report), indent=2, ensure_ascii=False))
        print(f"\nĐã ghi report JSON: {args.out}")


if __name__ == "__main__":
    main()
