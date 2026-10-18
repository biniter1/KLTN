"""
Experiment runner — nối `pipeline.run_tier1()` + `baseline.run_baseline_b1/b2()` +
`evaluation.py` lại thành quy trình R=15 lần chạy độc lập mô tả ở `PROPOSAL.md` mục 7
và `TIER1_DESIGN.md` mục 13.3.

Không tự thu thập traffic/config hay tự cấy kịch bản — đó là việc của
`traffic_collector.py`/`config_collector.py` (thu thập) và một test-harness ngoài
phạm vi Python package này (cấy kịch bản lên cluster thật, gắn `injection_id`, xem
`TRAFFIC_COLLECTION_PIPELINE.md` mục 3/6). Module này nhận ĐẦU VÀO đã đóng băng của
mỗi trial (`TrialInput`: traffic + config evidence + ground truth tự thiết kế cho
đúng trial đó, mục 13.3 "không dựng tập dữ liệu riêng cho baseline") và chạy cả 3
biến thể (Tier 1 thật, B1, B2) trên ĐÚNG CÙNG dữ liệu đó — đúng yêu cầu "so sánh công
bằng trong nội bộ 1 trial" (`TRAFFIC_COLLECTION_PIPELINE.md` mục 6).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .baseline import run_baseline_b1, run_baseline_b2
from .config import EXPERIMENT_CONFIG, ScoringConfig
from .evaluation import (
    ConfidenceInterval,
    GroundTruth,
    Metrics,
    WilcoxonResult,
    filter_ground_truth,
    mean_ci95,
    precision_recall_f1,
    wilcoxon_signed_rank,
)
from .pipeline import run_tier1
from .schema import ConfigEvidenceRecord, TrafficClass, TrafficSeries

# 2 lát cắt bổ sung — mục 13.3 (nguyên tắc bất đối xứng an toàn: recall > precision)
_SPARSE_TRAFFIC = lambda r: r.distinct_days <= 2  # noqa: E731 — định nghĩa "traffic thưa", mục 13.3/TIER1_DESIGN.md mục 4a
_NOISE_CLASSES = {TrafficClass.HEALTHCHECK, TrafficClass.MONITORING}
_NOISE_SLICE = lambda r: r.traffic_class in _NOISE_CLASSES  # noqa: E731


@dataclass
class TrialInput:
    """Dữ liệu ĐÃ ĐÓNG BĂNG của 1 trial (mục 6 `TRAFFIC_COLLECTION_PIPELINE.md`) — do
    collector thật hoặc `examples/simulate_data.py`-style tạo ra, cộng ground truth
    tự thiết kế cho kịch bản đã cấy ở trial này."""

    trial_id: str
    traffic_series: list[TrafficSeries]
    config_evidence: list[ConfigEvidenceRecord]
    ground_truth: GroundTruth


@dataclass
class TrialResult:
    trial_id: str
    tier1: Metrics
    b1: Metrics
    b2: Metrics
    tier1_sparse_recall: Metrics
    b1_sparse_recall: Metrics
    b2_sparse_recall: Metrics
    tier1_noise_precision: Metrics
    b1_noise_precision: Metrics
    b2_noise_precision: Metrics


def run_trial(trial: TrialInput, config: ScoringConfig = EXPERIMENT_CONFIG, selector_validator=None) -> TrialResult:
    """Chạy Tier 1 thật + B1 + B2 trên ĐÚNG CÙNG traffic/config evidence của 1 trial
    (mục 13.3) — không đọc/ghi SQLite (previous_status luôn None, đúng tinh thần R=15
    "mỗi lần 1 kịch bản khác nhau", không phải chuỗi thời gian liên tục — xem mục 13.4
    cho ablation hysteresis riêng, KHÔNG dùng hàm này)."""
    # selector_validator=None -> MockSelectorValidator (có service_ref là qua). Đánh giá chính
    # thức nên truyền KubernetesSelectorValidator thật (chỉ đọc) — TIER1_DESIGN.md mục 3b.
    tier1_results = run_tier1(trial.traffic_series, trial.config_evidence, trial.trial_id, config=config,
                              selector_validator=selector_validator)
    b1_results = run_baseline_b1(trial.traffic_series, trial.config_evidence, trial.trial_id, config=config)
    b2_results = run_baseline_b2(trial.traffic_series, trial.config_evidence, trial.trial_id, config=config)

    def _sliced(results, predicate) -> Metrics:
        gt_slice = filter_ground_truth(trial.ground_truth, results, predicate)
        return precision_recall_f1(results, gt_slice)

    return TrialResult(
        trial_id=trial.trial_id,
        tier1=precision_recall_f1(tier1_results, trial.ground_truth),
        b1=precision_recall_f1(b1_results, trial.ground_truth),
        b2=precision_recall_f1(b2_results, trial.ground_truth),
        tier1_sparse_recall=_sliced(tier1_results, _SPARSE_TRAFFIC),
        b1_sparse_recall=_sliced(b1_results, _SPARSE_TRAFFIC),
        b2_sparse_recall=_sliced(b2_results, _SPARSE_TRAFFIC),
        tier1_noise_precision=_sliced(tier1_results, _NOISE_SLICE),
        b1_noise_precision=_sliced(b1_results, _NOISE_SLICE),
        b2_noise_precision=_sliced(b2_results, _NOISE_SLICE),
    )


@dataclass
class VariantSummary:
    """CI 95% cho 1 biến thể (Tier 1/B1/B2), qua R trial."""

    precision_ci: ConfidenceInterval
    recall_ci: ConfidenceInterval
    f1_ci: ConfidenceInterval
    sparse_recall_ci: ConfidenceInterval
    noise_precision_ci: ConfidenceInterval


@dataclass
class ExperimentReport:
    n_trials: int
    tier1: VariantSummary
    b1: VariantSummary
    b2: VariantSummary

    # Kiểm định bắt cặp — mục 13.3, Tier 1 vs từng baseline, trên đúng cùng R trial
    tier1_vs_b1_recall: WilcoxonResult
    tier1_vs_b1_precision: WilcoxonResult
    tier1_vs_b2_recall: WilcoxonResult
    tier1_vs_b2_precision: WilcoxonResult

    trials: list[TrialResult] = field(default_factory=list)


def _summarize(trial_results: list[TrialResult], variant: str) -> VariantSummary:
    def _values(attr: str) -> list[float]:
        return [getattr(getattr(tr, variant), attr) for tr in trial_results]

    return VariantSummary(
        precision_ci=mean_ci95(_values("precision")),
        recall_ci=mean_ci95([getattr(tr, variant).recall for tr in trial_results]),
        f1_ci=mean_ci95([getattr(tr, variant).f1 for tr in trial_results]),
        sparse_recall_ci=mean_ci95([getattr(tr, f"{variant}_sparse_recall").recall for tr in trial_results]),
        noise_precision_ci=mean_ci95([getattr(tr, f"{variant}_noise_precision").precision for tr in trial_results]),
    )


def run_experiment(trials: list[TrialInput], config: ScoringConfig = EXPERIMENT_CONFIG,
                   selector_validator=None) -> ExperimentReport:
    """Chạy R=15 trial độc lập (hoặc bất kỳ R nào >=1 truyền vào), tổng hợp
    `mean ± CI 95%` + kiểm định Wilcoxon signed-rank Tier 1 vs B1/B2 — đúng
    `TIER1_DESIGN.md` mục 13.3. KHÔNG dùng cho ablation hysteresis (mục 13.4) —
    trial ở đây là các kịch bản ĐỘC LẬP khác nhau, không phải chuỗi thời gian trên
    cùng 1 kịch bản (xem NOTE cuối mục 13.4)."""
    if not trials:
        raise ValueError("run_experiment: cần ít nhất 1 trial")

    trial_results = [run_trial(t, config, selector_validator) for t in trials]

    tier1_recall = [tr.tier1.recall for tr in trial_results]
    tier1_precision = [tr.tier1.precision for tr in trial_results]
    b1_recall = [tr.b1.recall for tr in trial_results]
    b1_precision = [tr.b1.precision for tr in trial_results]
    b2_recall = [tr.b2.recall for tr in trial_results]
    b2_precision = [tr.b2.precision for tr in trial_results]

    return ExperimentReport(
        n_trials=len(trials),
        tier1=_summarize(trial_results, "tier1"),
        b1=_summarize(trial_results, "b1"),
        b2=_summarize(trial_results, "b2"),
        tier1_vs_b1_recall=wilcoxon_signed_rank(tier1_recall, b1_recall),
        tier1_vs_b1_precision=wilcoxon_signed_rank(tier1_precision, b1_precision),
        tier1_vs_b2_recall=wilcoxon_signed_rank(tier1_recall, b2_recall),
        tier1_vs_b2_precision=wilcoxon_signed_rank(tier1_precision, b2_precision),
        trials=trial_results,
    )
