"""
Ablation hysteresis — TIER1_DESIGN.md mục 13.4 (chốt 15/10/2026: N = 7 snapshot cắt từ chính
7 ngày dữ liệu đã thu, không thu thêm).

Snapshot k (k = 1..N) chỉ thấy dữ liệu từ ngày 1 tới ngày k của cửa sổ: count = tổng số
request các ngày ≤ k (lấy từ `series_daily_counts` mà 08_export ghi), traffic_days = các
ngày ≤ k có traffic, last_seen tính từ cuối ngày k. Chạy `run_tier1` lần lượt k = 1..N trên
CÙNG 1 file SQLite (previous_status đọc được từ snapshot trước), so 2 biến thể:

  - "hysteresis":      θ_promote / θ_demote như config
  - "single_threshold": θ_promote = θ_demote = θ_strong (ngưỡng đơn)

Metric: số lần đổi trạng thái của mỗi cạnh qua N snapshot (đúng định nghĩa
`unstable_classification`), KHÔNG phải precision/recall — hysteresis không sửa độ chính xác
của 1 lần quan sát mà sửa độ ổn định qua nhiều lần.
"""

from __future__ import annotations

import copy
import dataclasses
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from . import storage
from .config import EXPERIMENT_CONFIG, ScoringConfig
from .pipeline import run_tier1
from .schema import TrafficSeries


def slice_series(series: list[TrafficSeries], daily_counts: list[dict], until_day: str) -> list[TrafficSeries]:
    """Cắt danh sách series về những gì đã thấy tính tới hết ngày `until_day` (YYYY-MM-DD)."""
    end = date.fromisoformat(until_day)
    out: list[TrafficSeries] = []
    for s, daily in zip(series, daily_counts):
        if not daily:
            continue
        kept = {d: c for d, c in daily.items() if d <= until_day and c > 0}
        if not kept:
            continue
        t = copy.deepcopy(s)
        t.traffic_class = None
        t.traffic_days = sorted(kept)
        t.distinct_days = len(kept)
        total = sum(daily.values()) or 1
        t.count = int(round(sum(kept.values())))
        t.error_count = int(round(s.error_count * (sum(kept.values()) / total)))
        t.last_seen_days_ago = float((end - date.fromisoformat(t.traffic_days[-1])).days)
        out.append(t)
    return out


@dataclass
class VariantRun:
    statuses: dict[str, list[str]] = field(default_factory=dict)  # identity_key -> trạng thái theo snapshot

    def flips(self) -> dict[str, int]:
        return {k: sum(1 for a, b in zip(v, v[1:]) if a != b) for k, v in self.statuses.items()}


@dataclass
class AblationReport:
    days: list[str]
    hysteresis: VariantRun
    single_threshold: VariantRun

    def summary(self) -> dict:
        def _s(run: VariantRun) -> dict:
            flips = run.flips()
            return {
                "edges": len(flips),
                "total_flips": sum(flips.values()),
                "edges_with_flip": sum(1 for v in flips.values() if v > 0),
                "max_flips_per_edge": max(flips.values(), default=0),
            }

        return {"snapshots": len(self.days), "hysteresis": _s(self.hysteresis),
                "single_threshold": _s(self.single_threshold)}


def _run_variant(series, daily, config_evidence, days, config: ScoringConfig, trial_id: str) -> VariantRun:
    conn: sqlite3.Connection = storage.connect(":memory:")
    run = VariantRun()
    for k, day in enumerate(days, start=1):
        snap = slice_series(series, daily, day)
        results = run_tier1(snap, config_evidence, f"{trial_id}-d{k}", conn=conn, config=config,
                            snapshot_time=f"{day}T23:59:59Z")
        for r in results:
            if r.count == 0:
                continue  # dòng chỉ có config không qua hysteresis
            run.statuses.setdefault(r.identity_key.as_string(), [None] * (k - 1)).append(r.dependency_status.value)
        for v in run.statuses.values():
            if len(v) < k:
                v.append(None)  # cạnh chưa/không còn thấy ở snapshot này
    conn.close()
    return run


def run_hysteresis_ablation(
    series: list[TrafficSeries],
    daily_counts: list[dict],
    config_evidence: list,
    trial_id: str = "trial",
    config: ScoringConfig = EXPERIMENT_CONFIG,
    n_snapshots: int = 7,
) -> AblationReport:
    all_days = sorted({d for daily in daily_counts for d in daily})
    if not all_days:
        raise ValueError("Không có series_daily_counts — cần file trial xuất bằng 08_export từ 18/10/2026 trở đi.")
    days = all_days[-n_snapshots:]
    single = dataclasses.replace(config, theta_promote=config.theta_strong, theta_demote=config.theta_strong)
    return AblationReport(
        days=days,
        hysteresis=_run_variant(series, daily_counts, config_evidence, days, config, trial_id),
        single_threshold=_run_variant(series, daily_counts, config_evidence, days, single, trial_id),
    )
