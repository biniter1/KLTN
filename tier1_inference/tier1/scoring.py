"""
Scoring — TIER1_DESIGN.md mục 4 và 4a (cập nhật 15/10/2026).

    score = sigmoid(bias + w1·recency + w2·log_volume + w3·distinct_days_ratio
                   + w5·healthcheck_ratio + w6·monitoring_ratio
                   [+ w7·config_match_strength, chỉ khi count>0 — mục 4a])

Thay đổi 15/10:
  - Bỏ `protocol_diversity` (luôn = 0,5 vì protocol nằm trong khóa cạnh); bias −1,2 → −1,0.
  - `log_volume` chia cho `config.volume_reference` CỐ ĐỊNH (không tính lại theo snapshot).
  - Sau khi fit: config thành 3 đặc trưng 0/1 (`is_structural`, `is_embedded`,
    `is_unresolved`) — `score_edge` dùng chúng khi `config.w_is_structural` đã có giá trị.

Luật cứng (mục 4a): config_match_strength CHỈ cộng khi cạnh đã có traffic (count>0). Với
count=0, hàm này không được gọi — nhánh NOT_OBSERVED hoàn toàn tách biệt.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

from .aggregation import TrafficAggregate
from .config import ScoringConfig, DEFAULT_CONFIG
from .schema import ConfigFinding, TrafficClass


# Ánh xạ config_finding -> config_match_strength trong [0,1].
# Tài liệu (CONFIG_EVIDENCE_COLLECTOR.md mục 6) nói rõ config_finding là nhãn NHỊ PHÂN
# ("đã xác nhận tồn tại hay chưa"), không phải một dải xác suất -- nên việc lượng hóa
# thành số cụ thể để nhét vào sigmoid là một lựa chọn thiết kế CỦA TIER 1 (mục 4a),
# không phải giá trị đã có sẵn trong tài liệu Collector. Ghi rõ ở đây để dễ tinh chỉnh/
# bảo vệ khi trình bày.
CONFIG_MATCH_STRENGTH: dict[ConfigFinding, float] = {
    ConfigFinding.FOUND_STRUCTURAL: 1.0,   # đối chiếu K8s Service thật, sạch, không conflict
    ConfigFinding.FOUND_EMBEDDED: 1.0,     # tương tự, chỉ khác evidence_source
    ConfigFinding.FOUND_UNRESOLVED: 0.3,   # có cú pháp giống địa chỉ nhưng chưa đối chiếu được
    ConfigFinding.NOT_FOUND: 0.0,
}


@dataclass
class ScoringFeatures:
    recency: float
    log_volume: float
    distinct_days_ratio: float
    healthcheck_ratio: float
    monitoring_ratio: float
    config_match_strength: float  # 0.0 nếu không có config evidence khớp cạnh này
    # 3 đặc trưng 0/1 của config (dùng sau khi fit — mục 4a)
    is_structural: float = 0.0
    is_embedded: float = 0.0
    is_unresolved: float = 0.0


def sigmoid(x: float) -> float:
    # Chặn overflow khi x rất âm/dương (weights khởi tạo tay có thể đẩy x ra xa 0)
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def percentile_95(values: list[int]) -> float:
    """Phân vị 95 — dùng để lấy `ScoringConfig.volume_reference` MỘT LẦN từ dữ liệu rồi
    đóng băng (mục 4, chốt 15/10); không còn gọi theo từng snapshot trong pipeline.
    Triển khai thủ công (không phụ thuộc numpy) bằng nội suy tuyến tính kiểu 'linear'
    (giống mặc định numpy.percentile) để dễ kiểm chứng độc lập khi trình bày."""
    if not values:
        return 1.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    rank = 0.95 * (len(s) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(s[lo])
    frac = rank - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def compute_features(
    agg: TrafficAggregate,
    expected_max_count: float | None = None,
    config_match_strength: float = 0.0,
    config: ScoringConfig = DEFAULT_CONFIG,
    config_finding: ConfigFinding | None = None,
) -> ScoringFeatures:
    """Tính đặc trưng cho MỘT cạnh đã có traffic.

    `expected_max_count`: chỉ để tương thích chữ ký cũ — None (mặc định) dùng
    `config.volume_reference` cố định (chốt 15/10). Truyền số thì dùng số đó."""

    K = config.window_days
    reference = expected_max_count if expected_max_count is not None else config.volume_reference

    recency = 1.0 - min(agg.last_seen_days_ago / K, 1.0) if K > 0 else 0.0
    recency = max(0.0, recency)

    log_volume = min(
        math.log(agg.count + 1) / math.log(reference + 1), 1.0
    ) if reference > 0 else 0.0

    distinct_days_ratio = min(agg.distinct_days / K, 1.0) if K > 0 else 0.0

    healthcheck_ratio = agg.class_ratio(TrafficClass.HEALTHCHECK)
    monitoring_ratio = agg.class_ratio(TrafficClass.MONITORING)

    return ScoringFeatures(
        recency=recency,
        log_volume=log_volume,
        distinct_days_ratio=distinct_days_ratio,
        healthcheck_ratio=healthcheck_ratio,
        monitoring_ratio=monitoring_ratio,
        config_match_strength=config_match_strength,
        is_structural=1.0 if config_finding == ConfigFinding.FOUND_STRUCTURAL else 0.0,
        is_embedded=1.0 if config_finding == ConfigFinding.FOUND_EMBEDDED else 0.0,
        is_unresolved=1.0 if config_finding == ConfigFinding.FOUND_UNRESOLVED else 0.0,
    )


def score_edge(features: ScoringFeatures, config: ScoringConfig = DEFAULT_CONFIG) -> float:
    """Áp công thức sigmoid mục 4 / 4a với trọng số từ config (điểm flex #1 — xem config.py)."""
    x = (
        config.bias
        + config.w_recency * features.recency
        + config.w_log_volume * features.log_volume
        + config.w_distinct_days_ratio * features.distinct_days_ratio
        + config.w_healthcheck_ratio * features.healthcheck_ratio
        + config.w_monitoring_ratio * features.monitoring_ratio
    )
    if config.w_is_structural is not None:
        # Đã fit: config dạng 3 đặc trưng 0/1 (mục 4a, chốt 15/10)
        x += (
            config.w_is_structural * features.is_structural
            + (config.w_is_embedded or 0.0) * features.is_embedded
            + (config.w_is_unresolved or 0.0) * features.is_unresolved
        )
    else:
        x += config.w_config_match_strength * features.config_match_strength
    return sigmoid(x)


# ---------------------------------------------------------------------------
# Huấn luyện trọng số bằng logistic regression — mục 4.
# CHƯA gọi trong pipeline mặc định (chưa có ground truth thật). Giữ lại đầy đủ để dùng
# ở giai đoạn đánh giá (R=15 runs, TRAFFIC_COLLECTION_PIPELINE.md mục 6) khi đã có nhãn
# thật (dependency đã cấy = 1, health check/monitoring/cặp ngẫu nhiên = 0).
# ---------------------------------------------------------------------------

def fit_weights_logistic_regression(
    feature_rows: list[ScoringFeatures],
    labels: list[int],
    k_folds: int = 5,
    base_config: ScoringConfig = DEFAULT_CONFIG,
) -> tuple[ScoringConfig, dict]:
    """
    sklearn.linear_model.LogisticRegression + L2 regularization + K-fold cross-validation
    (k=5 mặc định), đúng phương pháp mô tả ở mục 4. Trả về (config đã cập nhật weights,
    report gồm accuracy/precision/recall trung bình qua các fold — để chọn theta_strong
    qua ROC như tài liệu yêu cầu, KHÔNG làm ở đây, làm ở bước đánh giá riêng).

    KHÔNG tự động dùng dataset fit trọng số để báo cáo precision/recall cuối (mục 4:
    "tách bạch tuyệt đối tập fit trọng số với tập báo cáo precision/recall cuối cùng") —
    người gọi hàm này chịu trách nhiệm tách train/test ở tầng ngoài trước khi gọi.
    """
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_validate

    X = np.array([
        [f.recency, f.log_volume, f.distinct_days_ratio,
         f.healthcheck_ratio, f.monitoring_ratio,
         f.is_structural, f.is_embedded, f.is_unresolved]
        for f in feature_rows
    ])
    y = np.array(labels)

    # L2 là penalty mặc định của LogisticRegression (mục 4: "L2 regularization") — không
    # truyền penalty="l2" tường minh để tránh FutureWarning ở sklearn >= 1.8 (tham số đó
    # đang bị deprecate, dự kiến gỡ bỏ ở 1.10, thay bằng l1_ratio/C).
    clf = LogisticRegression(solver="lbfgs", max_iter=1000, C=1.0)

    cv = StratifiedKFold(n_splits=k_folds, shuffle=True, random_state=42)
    cv_results = cross_validate(
        clf, X, y, cv=cv,
        scoring=["accuracy", "precision", "recall", "roc_auc"],
        return_estimator=False,
    )

    clf.fit(X, y)  # fit lại trên toàn bộ tập train (sau khi đã CV để đánh giá độ ổn định)
    w = clf.coef_[0]
    bias = clf.intercept_[0]

    new_config = dataclasses.replace(
        base_config,
        w_recency=float(w[0]),
        w_log_volume=float(w[1]),
        w_distinct_days_ratio=float(w[2]),
        w_healthcheck_ratio=float(w[3]),
        w_monitoring_ratio=float(w[4]),
        w_is_structural=float(w[5]),
        w_is_embedded=float(w[6]),
        w_is_unresolved=float(w[7]),
        bias=float(bias),
    )

    report = {
        "cv_accuracy_mean": float(np.mean(cv_results["test_accuracy"])),
        "cv_precision_mean": float(np.mean(cv_results["test_precision"])),
        "cv_recall_mean": float(np.mean(cv_results["test_recall"])),
        "cv_roc_auc_mean": float(np.mean(cv_results["test_roc_auc"])),
    }
    return new_config, report
