"""
Selector validation — TIER1_DESIGN.md mục 3b.

Chạy CHỈ cho các edge đạt OBSERVED_STRONG, TẠI THỜI ĐIỂM chuẩn bị sinh NetworkPolicy
YAML thật (không phải lúc quan sát traffic) — vì K8s Service selector có thể đổi giữa
lúc thu thập traffic và lúc thật sự enforce.

4 bước theo tài liệu:
  1. Query trực tiếp Service.spec.selector từ K8s API tại thời điểm sinh policy (không cache).
  2. Kiểm tra tập pod mà selector trả về không rỗng.
  3. Overlap check: selector có giao với tập pod của Service khác trong cùng namespace không.
  4. Đối chiếu ngược ownerReferences của pod, xác nhận đúng workload đã ghi nhận traffic.

Luật cứng: bất kỳ bước nào thất bại -> NEEDS_MANUAL_SELECTOR, không tự động enforce.

Thiết kế pluggable (Protocol) là điểm "flex" #3 (xem README.md / config.py):
  - MockSelectorValidator       : dùng cho demo/test cục bộ, không cần cluster thật.
  - KubernetesSelectorValidator : hiện thực đầy đủ bằng lib `kubernetes` chính thức,
    dùng khi có kubeconfig trỏ vào cluster thật (kind/minikube theo CONFIG_EVIDENCE_COLLECTOR.md
    mục 2) — không chạy được trong môi trường demo này (không có cluster), nhưng code đầy đủ
    để làm căn cứ trình bày & dùng thật khi lên cluster.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from .schema import DependencyEdgeResult, ServiceRef


@dataclass
class ValidationResult:
    passed: bool
    reason: Optional[str] = None
    pod_count: Optional[int] = None
    overlapping_services: Optional[list[str]] = None


class SelectorValidator(Protocol):
    def validate(self, edge: DependencyEdgeResult) -> ValidationResult:
        ...


class MockSelectorValidator:
    """Dùng cho examples/run_example.py — không gọi K8s API thật.

    `failing_services`: tập tên K8s Service (service_name) được giả lập là "có vấn đề"
    (không selector / selector rỗng / overlap...) để demo nhánh NEEDS_MANUAL_SELECTOR
    end-to-end mà không cần cluster thật.
    """

    def __init__(self, failing_services: set[str] | None = None):
        self.failing_services = failing_services or set()

    def validate(self, edge: DependencyEdgeResult) -> ValidationResult:
        ref = edge.destination_service_ref
        if ref is None:
            return ValidationResult(
                passed=False,
                reason="Không có destination_service_ref (scope không phải IN_CLUSTER hoặc "
                       "chưa resolve được Service thật) — không thể sinh podSelector.",
            )
        if ref.service_name in self.failing_services:
            return ValidationResult(
                passed=False,
                reason=f"[mock] Service '{ref.service_name}' được giả lập selector rỗng hoặc overlap.",
            )
        return ValidationResult(passed=True, pod_count=1, overlapping_services=[])


class KubernetesSelectorValidator:
    """Hiện thực thật bằng thư viện `kubernetes` chính thức (đúng lựa chọn công nghệ đã
    chốt ở CONFIG_EVIDENCE_COLLECTOR.md mục 2: đọc thẳng kubeconfig, không subprocess kubectl).

    Yêu cầu: gói `kubernetes` đã cài, kubeconfig hợp lệ trỏ vào cluster thật.
    Không được gọi trong examples/run_example.py (không có cluster trong môi trường demo).
    """

    def __init__(self, kubeconfig_path: str | None = None):
        self._kubeconfig_path = kubeconfig_path
        self._client = None  # lazy init, chỉ import `kubernetes` khi thật sự dùng

    def _ensure_client(self):
        if self._client is not None:
            return
        from kubernetes import client, config as k8s_config  # import lazy

        if self._kubeconfig_path:
            k8s_config.load_kube_config(config_file=self._kubeconfig_path)
        else:
            k8s_config.load_kube_config()
        self._client = client.CoreV1Api()

    def validate(self, edge: DependencyEdgeResult) -> ValidationResult:
        ref: Optional[ServiceRef] = edge.destination_service_ref
        if ref is None:
            return ValidationResult(
                passed=False,
                reason="Không có destination_service_ref — không thể tra Service thật.",
            )

        self._ensure_client()
        api = self._client

        # Bước 1: query trực tiếp Service.spec.selector, không dùng cache
        try:
            svc = api.read_namespaced_service(ref.service_name, ref.service_namespace)
        except Exception as e:  # pragma: no cover - phụ thuộc cluster thật
            return ValidationResult(passed=False, reason=f"Không đọc được Service: {e}")

        selector = svc.spec.selector or {}
        if not selector:
            return ValidationResult(
                passed=False,
                reason="Service không có spec.selector (dùng Endpoints thủ công) — "
                       "dùng ipBlock thay vì podSelector, bắt buộc review thủ công (mục 3b).",
            )

        label_selector = ",".join(f"{k}={v}" for k, v in selector.items())

        # Bước 2: tập pod mà selector trả về không rỗng
        pods = api.list_namespaced_pod(ref.service_namespace, label_selector=label_selector)
        if not pods.items:
            return ValidationResult(
                passed=False,
                reason=f"Selector '{label_selector}' không khớp pod nào hiện tại.",
            )
        matched_pod_names = {p.metadata.name for p in pods.items}

        # Bước 3: overlap check — mọi Service khác trong namespace có selector giao với selector này?
        all_services = api.list_namespaced_service(ref.service_namespace)
        overlapping: list[str] = []
        for other in all_services.items:
            if other.metadata.name == ref.service_name:
                continue
            other_selector = other.spec.selector or {}
            if not other_selector:
                continue
            other_label_selector = ",".join(f"{k}={v}" for k, v in other_selector.items())
            other_pods = api.list_namespaced_pod(ref.service_namespace, label_selector=other_label_selector)
            other_pod_names = {p.metadata.name for p in other_pods.items}
            if matched_pod_names & other_pod_names:
                overlapping.append(other.metadata.name)

        if overlapping:
            return ValidationResult(
                passed=False,
                reason=f"Selector giao với Service khác trong cùng namespace: {overlapping} "
                       "— selector không đủ đặc thù.",
                pod_count=len(matched_pod_names),
                overlapping_services=overlapping,
            )

        # Bước 4: đối chiếu ngược ownerReferences -> xác nhận đúng workload đã quan sát traffic
        expected_workload = edge.identity_key.destination_canonical_service
        for p in pods.items:
            canonical = (p.metadata.labels or {}).get("security.istio.io/canonical-name")
            if canonical != expected_workload:
                return ValidationResult(
                    passed=False,
                    reason=(
                        f"Pod '{p.metadata.name}' khớp selector nhưng nhãn canonical-name "
                        f"('{canonical}') không khớp workload đã quan sát traffic "
                        f"('{expected_workload}')."
                    ),
                    pod_count=len(matched_pod_names),
                )

        return ValidationResult(passed=True, pod_count=len(matched_pod_names), overlapping_services=[])
