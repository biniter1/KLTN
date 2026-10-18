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


def pod_canonical_service(labels: dict, owner_name: str | None, pod_name: str) -> str:
    """Canonical service của 1 pod — ĐÚNG thứ tự Istio tự suy ra (và cùng thứ tự
    `config_collector/identity.py` dùng): `service.istio.io/canonical-name` ->
    `app.kubernetes.io/name` -> `app` -> tên workload.

    Sửa lỗi 15/10/2026: bản cũ chỉ đọc nhãn `security.istio.io/canonical-name` (sai tên,
    và phần lớn pod không gắn nhãn canonical tường minh) -> trên cluster thật mọi cạnh
    STRONG đều rớt NEEDS_MANUAL_SELECTOR."""
    labels = labels or {}
    explicit = labels.get("service.istio.io/canonical-name") or labels.get("security.istio.io/canonical-name")
    if explicit:
        return explicit
    for key in ("app.kubernetes.io/name", "app"):
        if labels.get(key):
            return labels[key]
    workload = owner_name or pod_name
    template_hash = labels.get("pod-template-hash")
    if template_hash and workload.endswith(f"-{template_hash}"):
        workload = workload[: -(len(template_hash) + 1)]  # ReplicaSet "frontend-7d9f" -> Deployment "frontend"
    return workload


class KubernetesSelectorValidator:
    """Hiện thực thật bằng thư viện `kubernetes` chính thức (đọc thẳng kubeconfig, không
    subprocess kubectl). CHỈ ĐỌC Service/Pod — cùng quyền với Config Collector.

    Đọc Service + Pod của mỗi namespace MỘT LẦN cho mỗi instance (15/10/2026) rồi dùng chung
    cho mọi cạnh — vẫn là dữ liệu đọc TẠI THỜI ĐIỂM kiểm, không phải cache cũ: tạo instance
    mới cho mỗi lần chạy (Tier 1 báo sớm, Planner kiểm lại bắt buộc trước khi sinh YAML).
    Bản cũ: mỗi cạnh liệt kê lại mọi Service và pod của từng Service.

    Truyền `core_api` để test (fake) — không cần kubeconfig.
    """

    def __init__(self, kubeconfig_path: str | None = None, core_api=None):
        self._kubeconfig_path = kubeconfig_path
        self._client = core_api
        self._ns_cache: dict[str, tuple[list, list]] = {}

    def _ensure_client(self):
        if self._client is not None:
            return
        from kubernetes import client, config as k8s_config  # import lazy

        if self._kubeconfig_path:
            k8s_config.load_kube_config(config_file=self._kubeconfig_path)
        else:
            k8s_config.load_kube_config()
        self._client = client.CoreV1Api()

    def _namespace_state(self, namespace: str) -> tuple[list, list]:
        if namespace not in self._ns_cache:
            self._ensure_client()
            services = self._client.list_namespaced_service(namespace).items
            pods = self._client.list_namespaced_pod(namespace).items
            self._ns_cache[namespace] = (list(services), list(pods))
        return self._ns_cache[namespace]

    @staticmethod
    def _matches(selector: dict, pod) -> bool:
        labels = pod.metadata.labels or {}
        return all(labels.get(k) == v for k, v in selector.items())

    def validate(self, edge: DependencyEdgeResult) -> ValidationResult:
        ref: Optional[ServiceRef] = edge.destination_service_ref
        if ref is None:
            return ValidationResult(
                passed=False,
                reason="Không có destination_service_ref — không thể tra Service thật.",
            )

        try:
            services, pods = self._namespace_state(ref.service_namespace)
        except Exception as e:  # pragma: no cover - phụ thuộc cluster thật
            return ValidationResult(passed=False, reason=f"Không đọc được Service/Pod: {e}")

        # Bước 1: Service.spec.selector
        svc = next((x for x in services if x.metadata.name == ref.service_name), None)
        if svc is None:
            return ValidationResult(passed=False, reason=f"Không tìm thấy Service '{ref.service_name}'.")
        selector = svc.spec.selector or {}
        if not selector:
            return ValidationResult(
                passed=False,
                reason="Service không có spec.selector (dùng Endpoints thủ công) — "
                       "dùng ipBlock thay vì podSelector, bắt buộc review thủ công (mục 3b).",
            )
        label_selector = ",".join(f"{k}={v}" for k, v in selector.items())

        # Bước 2: tập pod mà selector trả về không rỗng
        matched = [p for p in pods if self._matches(selector, p)]
        if not matched:
            return ValidationResult(passed=False, reason=f"Selector '{label_selector}' không khớp pod nào hiện tại.")
        matched_names = {p.metadata.name for p in matched}

        # Bước 3: overlap — Service khác cùng namespace chọn trùng pod?
        overlapping: list[str] = []
        for other in services:
            if other.metadata.name == ref.service_name:
                continue
            other_selector = other.spec.selector or {}
            if not other_selector:
                continue
            if matched_names & {p.metadata.name for p in pods if self._matches(other_selector, p)}:
                overlapping.append(other.metadata.name)
        if overlapping:
            return ValidationResult(
                passed=False,
                reason=f"Selector giao với Service khác trong cùng namespace: {overlapping} "
                       "— selector không đủ đặc thù.",
                pod_count=len(matched_names),
                overlapping_services=overlapping,
            )

        # Bước 4: canonical của từng pod khớp phải đúng workload đã quan sát traffic
        expected = edge.identity_key.destination_canonical_service
        for p in matched:
            owners = p.metadata.owner_references or []
            canonical = pod_canonical_service(p.metadata.labels or {}, owners[0].name if owners else None, p.metadata.name)
            if canonical != expected:
                return ValidationResult(
                    passed=False,
                    reason=(
                        f"Pod '{p.metadata.name}' khớp selector nhưng canonical ('{canonical}') "
                        f"không khớp workload đã quan sát traffic ('{expected}')."
                    ),
                    pod_count=len(matched_names),
                )

        return ValidationResult(passed=True, pod_count=len(matched_names), overlapping_services=[])
