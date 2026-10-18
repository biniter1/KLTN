"""ablation.py — 7 snapshot cắt từ 7 ngày (TIER1_DESIGN.md mục 13.4)."""

from tier1.ablation import run_hysteresis_ablation, slice_series
from tier1.config import ScoringConfig

from .conftest import make_series

DAYS = [f"2026-10-0{d}" for d in range(2, 9)]


def test_slice_series_keeps_only_days_up_to_k():
    s = make_series(count=30, traffic_days=["2026-10-02", "2026-10-05"])
    out = slice_series([s], [{"2026-10-02": 10, "2026-10-05": 20}], "2026-10-04")
    assert len(out) == 1
    t = out[0]
    assert (t.count, t.traffic_days, t.distinct_days, t.last_seen_days_ago) == (10, ["2026-10-02"], 1, 2.0)
    assert slice_series([s], [{"2026-10-05": 20}], "2026-10-04") == []


def test_variants_differ_where_score_is_between_thresholds():
    """Điểm nằm giữa θ_strong (0,60) và θ_promote (0,68): ngưỡng đơn cho STRONG ngay,
    hysteresis phải đợi điểm đạt 0,68 (khó lên). Snapshot cộng dồn ngày 1..k nên điểm thường
    tăng dần — số lần đổi trạng thái đo được sẽ thấp (giới hạn của cách cắt 7 ngày, ghi vào báo cáo)."""
    daily = {d: (50 if i % 2 == 0 else 30) for i, d in enumerate(DAYS)}
    s = make_series(count=sum(daily.values()), traffic_days=DAYS)
    rep = run_hysteresis_ablation([s], [daily], [], trial_id="t", config=ScoringConfig(volume_reference=20000))
    hyst = next(iter(rep.hysteresis.statuses.values()))
    single = next(iter(rep.single_threshold.statuses.values()))
    assert rep.summary()["snapshots"] == 7 and len(hyst) == len(single) == 7
    assert hyst[0] == "OBSERVED_WEAK" and single[0] == "OBSERVED_STRONG"
    assert hyst[-1] == single[-1] == "OBSERVED_STRONG"
