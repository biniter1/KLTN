"""
ScoringConfig — TOÀN BỘ số/ngưỡng/danh sách có thể tinh chỉnh của Tier 1 nằm ở ĐÂY,
không rải rác trong logic (classification.py, scoring.py, status.py chỉ ĐỌC config,
không hardcode).

Đây là điểm "flex" đầu tiên trong 3 điểm được trình bày ở README.md:
  1) Tách config khỏi logic (file này)               -> tinh chỉnh threshold/weight
     không cần sửa code, có thể load từ JSON, có thể có nhiều bộ config khác nhau
     cho các bối cảnh khác nhau (môi trường thử nghiệm khác nhau, cluster khác nhau).
  2) Domain-neutral output (schema.py)                -> tách suy luận khỏi sinh policy.
  3) Pluggable SelectorValidator (selector_validation.py) -> điểm cắm thay đổi cách
     validate trước khi enforce, không đụng vào phần suy luận.

Mọi ngưỡng ở đây là GIÁ TRỊ KHỞI TẠO (chốt 15/10/2026): con số chính thức chọn khi chạy
đánh giá — fit trọng số bằng hồi quy logistic, chọn θ trên ROC của tập fit, đặt
θ_promote/θ_demote = θ ± 0,05 (TIER1_DESIGN.md mục 5b, 13).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ScoringConfig:
    # ---- Trọng số sigmoid — mục 4. Giá trị khởi tạo thủ công (chưa fit) ----
    w_recency: float = 0.9
    w_log_volume: float = 1.1
    w_distinct_days_ratio: float = 1.0
    w_healthcheck_ratio: float = -1.6   # âm: càng nhiều traffic là healthcheck càng hạ điểm
    w_monitoring_ratio: float = -1.6    # âm: tương tự cho monitoring
    w_config_match_strength: float = 0.8  # mục 4a — chỉ cộng khi count>0
    # Bỏ `protocol_diversity` (15/10): protocol nằm trong khóa cạnh nên đặc trưng luôn = 0,5,
    # chỉ như cộng hằng 0,4·0,5 = 0,2. Bias −1,2 → −1,0 bù đúng phần đó (điểm không đổi).
    bias: float = -1.0

    # ---- Config dạng 3 đặc trưng 0/1 — chỉ dùng sau khi fit (mục 4a, chốt 15/10) ----
    # None = chưa fit -> dùng `w_config_match_strength × config_match_strength`.
    w_is_structural: Optional[float] = None
    w_is_embedded: Optional[float] = None
    w_is_unresolved: Optional[float] = None

    # ---- Cửa sổ quan sát K ngày — dùng cho recency/distinct_days_ratio ----
    window_days: int = 7

    # ---- log_volume (mục 4, chốt 15/10): mẫu số CỐ ĐỊNH, không tính lại theo snapshot ----
    # Lấy 1 lần = phân vị 95 count các cạnh của lần xuất dữ liệu 08/10/2026 (67 cạnh của
    # shared-eval, gộp mọi trial: 1.674.596 request / 7 ngày), rồi đóng băng. Đổi khi đổi
    # môi trường; KHÔNG để mỗi snapshot tự tính (điểm 1 cạnh sẽ đổi khi cạnh khác đổi).
    volume_reference: float = 1_674_596.0

    # ---- Ngưỡng quyết định trạng thái (giữ giá trị khởi tạo, chọn lại khi đánh giá) ----
    theta_strong: float = 0.60
    theta_promote: float = 0.68
    theta_demote: float = 0.58

    # ---- count_floor (mục 4a) — CHỐT 15/10: KHÔNG dùng (giữ None) ----
    # 1 request thật + config trỏ đúng đích = 2 nguồn độc lập xác nhận; đặt sàn hại đúng
    # nhóm cạnh thưa cần cứu. Dashboard đánh dấu cạnh STRONG có count < low_count_review.
    count_floor: Optional[tuple[int, int]] = None
    low_count_review: int = 3

    # ---- unstable traffic — mục 9 ----
    error_ratio_threshold: float = 0.3

    # ---- unstable_classification — mục 5b ----
    max_status_flips_per_window: int = 3
    status_flip_window_days: int = 7

    # ---- UNKNOWN status — mục 5 (Q3) ----
    unknown_class_ratio_threshold: float = 0.5

    # ---- Phân loại — mục 2.2 ----
    # DNS: Service đích (ns/name) là DNS của cluster, hoặc port 53.
    dns_services: tuple[str, ...] = ("kube-system/kube-dns",)
    dns_port: int = 53
    control_plane_namespaces: tuple[str, ...] = ("istio-system", "kube-system")

    # MONITORING đã xác minh: (namespace/service_account) — so với SA tách từ SPIFFE
    # principal (mTLS, không giả được). Hỗ trợ wildcard kiểu fnmatch. Collector còn tự
    # phát hiện thêm từ cluster (cluster_context.py) và gắn cờ `verified_monitoring`.
    # Mặc định CHẶT: chỉ Prometheus addon của Istio. Monitoring khác (vmagent của
    # LOG_METRIC...) được collector tự phát hiện từ cluster (cluster_context.py). Thí nghiệm
    # dùng EXPERIMENT_CONFIG bên dưới (phương án a).
    verified_monitoring_identities: tuple[str, ...] = ("istio-system/prometheus",)
    # Tên tự khai (canonical) — chỉ dùng để gắn cờ `claimed_monitoring`, KHÔNG trừ điểm.
    monitoring_names: tuple[str, ...] = (
        "prometheus", "vmagent", "grafana", "blackbox-exporter", "security-scanner", "kiali",
    )

    # HEALTHCHECK 3 tầng (mục 2.2, chốt 15/10): (1) path từ probe trong pod spec — collector
    # gắn cờ `probe_path`; (2) danh sách mặc định NGẮN dưới đây; (3) đội vận hành tự thêm
    # vào `healthcheck_extra_paths` (YAML/JSON config). So theo segment cuối / nguyên văn,
    # KHÔNG theo tiền tố (tránh `/status-report`, `/livestream`...).
    healthcheck_last_segments: tuple[str, ...] = (
        "health", "healthz", "readyz", "livez", "ready", "live", "metrics",
    )
    # CHỈ gRPC health chuẩn. gRPC reflection (grpc.reflection.v1[alpha].ServerReflection)
    # KHÔNG nằm mặc định: harness thí nghiệm sinh traffic dependency cấy bằng `grpcurl list`
    # qua reflection (infra/scenarios/render_manifests.py) -> trong thí nghiệm reflection là
    # traffic THẬT (ground truth True). Gắn nó là healthcheck làm recall tụt 0,852 -> 0,789
    # trên 15 trial (đo 18/10/2026). Môi trường thật coi reflection là công cụ debug thì thêm
    # 2 path đó vào `healthcheck_extra_paths`.
    healthcheck_grpc_services: tuple[str, ...] = ("grpc.health.v1.health",)
    healthcheck_extra_paths: tuple[str, ...] = ()

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2, default=list))

    @classmethod
    def from_json(cls, path: str | Path) -> "ScoringConfig":
        data = json.loads(Path(path).read_text())
        known = cls.__dataclass_fields__
        data = {k: v for k, v in data.items() if k in known}  # bỏ khóa cũ (vd w_protocol_diversity)
        for k, f in known.items():
            if k in data and data[k] is not None and str(f.type).startswith("tuple"):
                data[k] = tuple(data[k])
        return cls(**data)


DEFAULT_CONFIG = ScoringConfig()

# Thí nghiệm R=15 (phương án a, chốt 15/10/2026): harness nhiễu monitoring
# (infra/scenarios/scenario_pool.py::_MONITORING_SOURCES) chạy pod trong namespace dùng chung
# với SA trùng tên nguồn giả lập -> khai báo 3 danh tính này là "monitoring đã xác minh".
# Báo cáo phải ghi rõ: đây là cấu hình THÍ NGHIỆM, không phải mặc định sản phẩm.
EXPERIMENT_MONITORING_SOURCES = ("prometheus", "grafana", "blackbox-exporter")
EXPERIMENT_CONFIG = ScoringConfig(
    verified_monitoring_identities=DEFAULT_CONFIG.verified_monitoring_identities
    + tuple(f"*/{name}" for name in EXPERIMENT_MONITORING_SOURCES),
)
