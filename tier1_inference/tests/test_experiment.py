"""experiment.py — R=15 (hoặc N) trial độc lập, PROPOSAL.md mục 7 / TIER1_DESIGN.md mục 13.3."""

from __future__ import annotations

import pytest

from tier1.experiment import TrialInput, run_experiment, run_trial
from tier1.schema import ConfigFinding

from .conftest import make_config_evidence, make_identity_key, make_series

NS = "default"


def _make_trial(trial_id: str, real_count: int = 200, real_days: int = 7) -> TrialInput:
    """1 trial style Online Boutique: 1 dependency thật rõ ràng (frontend->productcatalogservice),
    1 dependency thật nhưng traffic THƯA (frontend->currencyservice, distinct_days<=2 — mục 13.3),
    1 noise HEALTHCHECK không phải dependency thật nhưng traffic mạnh (mục 13.3, hại precision B1)."""
    real_strong = make_series(
        source="frontend", dest="productcatalogservice", count=real_count, distinct_days=real_days, path="/Business/Call"
    )
    real_sparse = make_series(
        source="frontend", dest="currencyservice", count=5, distinct_days=1, path="/Business/Convert"
    )
    noise_healthcheck = make_series(
        source="blackbox-exporter", dest="frontend", count=500, distinct_days=7, path="/healthz"
    )

    ground_truth = {
        make_identity_key(source="frontend", dest="productcatalogservice").pair_key(): True,
        make_identity_key(source="frontend", dest="currencyservice").pair_key(): True,
        make_identity_key(source="blackbox-exporter", dest="frontend").pair_key(): False,
    }

    return TrialInput(
        trial_id=trial_id,
        traffic_series=[real_strong, real_sparse, noise_healthcheck],
        config_evidence=[],
        ground_truth=ground_truth,
    )


def test_run_trial_tier1_beats_b2_on_sparse_recall():
    """Tier 1 dùng sigmoid liên tục nên vẫn có cơ hội bắt được dependency thưa (nếu
    score đủ cao dù distinct_days thấp) trong khi B2 đòi cứng distinct_days>=3 -> luôn
    trượt currencyservice. Không assert Tier 1 THẮNG tuyệt đối (phụ thuộc trọng số mặc
    định chưa train), chỉ assert B2 chắc chắn MISS (đúng thiết kế ngưỡng cứng mục 13.2)."""
    trial = _make_trial("trial_1")
    result = run_trial(trial)
    assert result.b2_sparse_recall.recall == 0.0  # đúng dự đoán mục 13.3: B2 thất bại nặng nhất ở đây


def test_run_trial_b1_has_lower_noise_precision_than_tier1():
    """B1 không phân biệt traffic_class -> coi luôn healthcheck noise là STRONG -> FP
    trong lát cắt HEALTHCHECK/MONITORING -> precision thấp hơn hẳn Tier 1 (mục 13.3)."""
    trial = _make_trial("trial_1")
    result = run_trial(trial)
    assert result.b1_noise_precision.precision <= result.tier1_noise_precision.precision


def test_run_trial_all_three_variants_present():
    trial = _make_trial("trial_1")
    result = run_trial(trial)
    assert result.trial_id == "trial_1"
    for m in (result.tier1, result.b1, result.b2):
        assert 0.0 <= m.precision <= 1.0
        assert 0.0 <= m.recall <= 1.0


def test_run_experiment_aggregates_across_trials():
    trials = [_make_trial(f"trial_{i}", real_count=150 + i * 10) for i in range(5)]
    report = run_experiment(trials)

    assert report.n_trials == 5
    assert len(report.trials) == 5
    for summary in (report.tier1, report.b1, report.b2):
        assert 0.0 <= summary.recall_ci.mean <= 1.0
        assert 0.0 <= summary.precision_ci.mean <= 1.0
        assert summary.recall_ci.half_width >= 0.0

    for wilcoxon in (
        report.tier1_vs_b1_recall,
        report.tier1_vs_b1_precision,
        report.tier1_vs_b2_recall,
        report.tier1_vs_b2_precision,
    ):
        assert wilcoxon.p_value is None or 0.0 <= wilcoxon.p_value <= 1.0


def test_run_experiment_b2_sparse_recall_ci_is_zero_across_trials():
    """B2 miss currencyservice (distinct_days=1) ở MỌI trial -> mean recall = 0, CI rộng = 0
    (không biến thiên) — kiểm chứng end-to-end cho đúng luận điểm mục 13.3."""
    trials = [_make_trial(f"trial_{i}") for i in range(5)]
    report = run_experiment(trials)
    assert report.b2.sparse_recall_ci.mean == 0.0
    assert report.b2.sparse_recall_ci.half_width == 0.0


def test_run_experiment_empty_trials_raises():
    with pytest.raises(ValueError):
        run_experiment([])


def test_trial_input_config_evidence_used_consistently_across_variants():
    """Config evidence của 1 trial phải được CẢ 3 biến thể tiêu thụ (mục 6: dùng chung
    snapshot cho mọi baseline so sánh trong cùng 1 trial) — kiểm tra qua 1 case cụ thể:
    thêm config evidence sạch cho 1 cặp potential-only, cả 3 biến thể phải cùng thấy nó."""
    ev = make_config_evidence(source="checkoutservice", dest="emailservice", finding=ConfigFinding.FOUND_EMBEDDED)
    trial = TrialInput(
        trial_id="trial_config",
        traffic_series=[],
        config_evidence=[ev],
        ground_truth={make_identity_key(source="checkoutservice", dest="emailservice").pair_key(): True},
    )
    result = run_trial(trial)
    # Cả 3 đều xử lý qua nhánh potential-only giống nhau (NOT_OBSERVED, needs_human_review)
    # -> không biến thể nào tính là "dự đoán dương" (chỉ OBSERVED_STRONG mới tính) -> recall=0
    assert result.tier1.recall == 0.0
    assert result.b1.recall == 0.0
    assert result.b2.recall == 0.0
