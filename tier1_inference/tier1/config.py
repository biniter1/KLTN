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

Giá trị mặc định lấy đúng theo TIER1_DESIGN.md mục 4, 4a, 5b, 9, 12 — đều là
"giá trị khởi tạo", chưa phải kết quả tinh chỉnh thật (mục 12: ngoài phạm vi PoC).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


@dataclass
class ScoringConfig:
    # ---- Trọng số sigmoid — mục 4. Giá trị khởi tạo thủ công (chưa train logistic
    # regression thật vì chưa có ground truth — xem scoring.py::fit_weights_logistic_regression) ----
    w_recency: float = 0.9
    w_log_volume: float = 1.1
    w_distinct_days_ratio: float = 1.0
    w_protocol_diversity: float = 0.4
    w_healthcheck_ratio: float = -1.6   # âm: càng nhiều traffic là healthcheck càng hạ điểm
    w_monitoring_ratio: float = -1.6    # âm: tương tự cho monitoring
    w_config_match_strength: float = 0.8  # feature thứ 7, mục 4a — chỉ cộng khi count>0
    bias: float = -1.2

    # ---- Cửa sổ quan sát K ngày — dùng cho recency/distinct_days_ratio ----
    window_days: int = 7

    # ---- Ngưỡng quyết định trạng thái ----
    # theta_strong (mục 4): giá trị tham chiếu chọn qua ROC, giữ lại để tài liệu hóa/báo cáo.
    # Quyết định STRONG/không-STRONG THẬT SỰ dùng theta_promote/theta_demote (mục 5b) vì đã
    # có hysteresis; theta_strong nằm giữa hai ngưỡng đó (khớp mô tả "vùng chết" bao quanh nó).
    theta_strong: float = 0.60
    theta_promote: float = 0.68
    theta_demote: float = 0.58

    # ---- count_floor (mục 4a, "câu hỏi mở, chưa chốt") ----
    # None = tắt (đúng trạng thái "chưa chốt" trong tài liệu). Đặt (min_count, min_distinct_days)
    # để bật: traffic dưới ngưỡng này bị coi tương đương count=0 khi xét OBSERVED_STRONG.
    count_floor: Optional[tuple[int, int]] = None

    # ---- unstable traffic — mục 9 ----
    error_ratio_threshold: float = 0.3

    # ---- unstable_classification — mục 5b ----
    max_status_flips_per_window: int = 3
    status_flip_window_days: int = 7

    # ---- UNKNOWN status heuristic (mục 5: "traffic_class mâu thuẫn/không rõ") ----
    # Tài liệu không cho số cụ thể; chọn: nếu tỷ trọng traffic thuộc class UNKNOWN
    # (không khớp rule phân loại nào ở mục 2) vượt ngưỡng này -> dependency_status=UNKNOWN.
    unknown_class_ratio_threshold: float = 0.5

    # ---- Danh sách/pattern dùng cho classification.py mục 2 ----
    healthcheck_path_prefixes: tuple[str, ...] = (
        "/health", "/ready", "/live", "/metrics", "/healthz", "/readyz", "/livez",
    )
    # mục 2 ghi rõ đây là "heuristic ban đầu, tinh chỉnh khi có dữ liệu thật". Prefix ở trên
    # chỉ bắt path kiểu REST (/health...). gRPC health-check chuẩn (dùng trong Online Boutique
    # và hầu hết dịch vụ gRPC) lại có path đầy đủ dạng "/grpc.health.v1.Health/Check" — không
    # khớp prefix nào ở trên. Thêm rule match theo SUBSTRING (không phải prefix) để bắt đúng
    # convention thật này khi chạy trên app nền đã chốt (mục 2 CONFIG_EVIDENCE_COLLECTOR.md).
    healthcheck_path_contains: tuple[str, ...] = ("grpc.health",)
    monitoring_source_services: tuple[str, ...] = (
        "prometheus", "grafana", "blackbox-exporter", "security-scanner", "kiali",
    )
    control_plane_namespaces: tuple[str, ...] = ("istio-system", "kube-system")
    dns_port: int = 53

    # ---- percentile dùng cho log_volume EXPECTED_MAX (mục 4) ----
    log_volume_percentile: float = 95.0

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2, default=list))

    @classmethod
    def from_json(cls, path: str | Path) -> "ScoringConfig":
        data = json.loads(Path(path).read_text())
        # tuple fields được JSON hóa thành list — convert lại
        for k in ("healthcheck_path_prefixes", "healthcheck_path_contains",
                   "monitoring_source_services", "control_plane_namespaces"):
            if k in data and data[k] is not None:
                data[k] = tuple(data[k])
        if data.get("count_floor") is not None:
            data["count_floor"] = tuple(data["count_floor"])
        return cls(**data)


DEFAULT_CONFIG = ScoringConfig()
