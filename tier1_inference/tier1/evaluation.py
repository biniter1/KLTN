"""
Đánh giá — TIER1_DESIGN.md mục 13.3: precision/recall/F1, `mean ± CI 95%` trên R=15
lần chạy, kiểm định bắt cặp Wilcoxon signed-rank (Tier 1 vs B1, Tier 1 vs B2).

NOTE — điểm mở/diễn giải (xem thêm docstring `baseline.py`): "dự đoán dương" ở đây
= `dependency_status == OBSERVED_STRONG`, so khớp theo `pair_key` (source_ns,
source_svc, dest_ns, dest_svc) — không phân biệt protocol/port, vì ground truth của
một kịch bản cấy thường phát biểu ở mức "service A phụ thuộc service B", đúng grain
mà `config_evidence`/`ConfigEvidenceRecord.pair_key()` đã dùng xuyên suốt.

Quy ước khi mẫu số = 0 (không có dự đoán dương, hoặc ground truth không có dương nào):
trả `0.0` thay vì chia cho 0 — theo đúng mặc định `zero_division=0` của scikit-learn,
không phải suy luận riêng của module này.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Optional

from .schema import DependencyEdgeResult, DependencyStatus

PairKey = tuple[str, str, str, str]
GroundTruth = dict[PairKey, bool]


@dataclass
class Metrics:
    precision: float
    recall: float
    f1: float
    tp: int
    fp: int
    fn: int


def predicted_positive_pairs(results: list[DependencyEdgeResult]) -> set[PairKey]:
    """"Dự đoán dương" = OBSERVED_STRONG — xem NOTE ở docstring module."""
    return {
        r.identity_key.pair_key()
        for r in results
        if r.dependency_status == DependencyStatus.OBSERVED_STRONG
    }


def precision_recall_f1(results: list[DependencyEdgeResult], ground_truth: GroundTruth) -> Metrics:
    predicted = predicted_positive_pairs(results)
    actual_positive = {pk for pk, is_real in ground_truth.items() if is_real}

    tp = len(predicted & actual_positive)
    fp = len(predicted - actual_positive)
    fn = len(actual_positive - predicted)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return Metrics(precision=precision, recall=recall, f1=f1, tp=tp, fp=fp, fn=fn)


def filter_ground_truth(
    ground_truth: GroundTruth,
    results: list[DependencyEdgeResult],
    predicate,
) -> GroundTruth:
    """Lọc ground truth xuống 1 lát cắt (mục 13.3: recall trên traffic thưa, precision
    trên HEALTHCHECK/MONITORING) — `predicate(result) -> bool` áp trên các `results` có
    `pair_key` khớp; cặp không xuất hiện trong `results` (không quan sát được gì) bị bỏ
    qua khỏi lát cắt vì không có cách nào biết nó thuộc nhóm nào."""
    matching_pairs = {r.identity_key.pair_key() for r in results if predicate(r)}
    return {pk: v for pk, v in ground_truth.items() if pk in matching_pairs}


@dataclass
class ConfidenceInterval:
    mean: float
    half_width: float
    low: float
    high: float


def mean_ci95(values: list[float]) -> ConfidenceInterval:
    """`mean ± CI 95%` qua phân phối t (không phải xấp xỉ chuẩn) — đúng thực hành thống
    kê cho cỡ mẫu nhỏ (R=15, mục 13.3/`PROPOSAL.md` mục 7)."""
    if not values:
        raise ValueError("mean_ci95: values rỗng")

    n = len(values)
    mean = statistics.fmean(values)

    if n < 2:
        return ConfidenceInterval(mean=mean, half_width=0.0, low=mean, high=mean)

    stdev = statistics.stdev(values)
    if stdev == 0.0:
        return ConfidenceInterval(mean=mean, half_width=0.0, low=mean, high=mean)

    from scipy.stats import t as t_dist  # import lazy — chỉ cần khi thật sự tính CI

    sem = stdev / math.sqrt(n)
    t_crit = t_dist.ppf(0.975, df=n - 1)
    half_width = t_crit * sem
    return ConfidenceInterval(mean=mean, half_width=half_width, low=mean - half_width, high=mean + half_width)


@dataclass
class WilcoxonResult:
    statistic: Optional[float]
    p_value: Optional[float]
    note: Optional[str] = None


def wilcoxon_signed_rank(tier1_values: list[float], baseline_values: list[float]) -> WilcoxonResult:
    """Kiểm định bắt cặp — mục 13.3: Tier 1 vs B1 (hoặc B2) trên ĐÚNG CÙNG R=15 trial,
    dùng Wilcoxon signed-rank thay vì t-test 2 mẫu độc lập (sai giả định bắt cặp)."""
    if len(tier1_values) != len(baseline_values):
        raise ValueError("wilcoxon_signed_rank: 2 danh sách phải cùng độ dài (bắt cặp theo trial)")
    if len(tier1_values) == 0:
        return WilcoxonResult(statistic=None, p_value=None, note="Không có dữ liệu (0 trial)")

    if all(a == b for a, b in zip(tier1_values, baseline_values)):
        # scipy.stats.wilcoxon raise lỗi khi mọi hiệu số = 0 (không có gì để xếp hạng) —
        # về mặt thống kê đây là "không có khác biệt" (p=1.0), không phải lỗi dữ liệu.
        return WilcoxonResult(statistic=0.0, p_value=1.0, note="Mọi cặp giá trị giống hệt nhau")

    from scipy.stats import wilcoxon  # import lazy — chỉ cần khi thật sự kiểm định

    statistic, p_value = wilcoxon(tier1_values, baseline_values)
    return WilcoxonResult(statistic=float(statistic), p_value=float(p_value))
