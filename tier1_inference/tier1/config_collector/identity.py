"""
Config Evidence Collector v2 — bước [5e] CHUẨN HOÁ IDENTITY
(CONFIG_EVIDENCE_COLLECTOR.md mục 4.4e). Tách riêng khỏi `validate.py` vì CÙNG MỘT
logic fallback chain được dùng cho cả source lẫn destination.

Thứ tự fallback canonical_service (mục 4.4e — đúng thứ tự Istio ambient tự suy ra khi
thiếu nhãn canonical riêng của nó):

    service.istio.io/canonical-name   (PodInfo.canonical_service đã gồm luôn tên nhãn
                                        cũ security.istio.io/canonical-name, xem k8s_types.py)
    -> app.kubernetes.io/name
    -> app
    -> tên workload (PodInfo.name — đơn vị thu thập là pod template, mục 3.1, nên
       CHÍNH LÀ tên workload, không phải tên 1 pod instance ngẫu nhiên)
"""

from __future__ import annotations

from dataclasses import dataclass

from .k8s_types import PodInfo


def canonical_service_of(pod: PodInfo) -> str:
    return (
        pod.canonical_service
        or pod.labels.get("app.kubernetes.io/name")
        or pod.labels.get("app")
        or pod.name
    )


def service_account_of(pod: PodInfo) -> str:
    return pod.service_account_name or "default"


@dataclass(frozen=True)
class WorkloadIdentity:
    namespace: str
    canonical_service: str
    service_account: str
    workload_name: str  # chỉ debug/log -- định danh chính thức là canonical_service

    def is_same_as(self, other: "WorkloadIdentity") -> bool:
        """mục 4.4f — tự tham chiếu: cùng namespace + cùng canonical_service (KHÔNG so
        service_account, vì 2 workload khác nhau vẫn có thể vô tình cùng 1 SA)."""
        return self.namespace == other.namespace and self.canonical_service == other.canonical_service


def resolve_identity(pod: PodInfo) -> WorkloadIdentity:
    return WorkloadIdentity(
        namespace=pod.namespace,
        canonical_service=canonical_service_of(pod),
        service_account=service_account_of(pod),
        workload_name=pod.name,
    )


def distinct_identities(pods: list[PodInfo]) -> list[WorkloadIdentity]:
    """Gộp các pod CÙNG canonical_service thành 1 identity (replicas của cùng 1
    workload — bình thường, không phải overlap). Trả về NHIỀU identity chỉ khi selector
    thật sự khớp nhiều workload KHÁC NHAU — đúng trường hợp `selector_overlap` mục 4.4e."""
    seen: dict[tuple[str, str], WorkloadIdentity] = {}
    for pod in pods:
        ident = resolve_identity(pod)
        seen.setdefault((ident.namespace, ident.canonical_service), ident)
    return list(seen.values())
