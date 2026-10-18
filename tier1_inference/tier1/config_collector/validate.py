"""
Config Evidence Collector v2 — bước [5] VALIDATION — rẽ nhánh tất định
(CONFIG_EVIDENCE_COLLECTOR.md mục 4.4). Đây là nơi DUY NHẤT quyết định 1 reference LLM
trả về có "thật" hay không — "LLM chỉ đề xuất, code + cluster quyết định" (mục 1.2
nguyên tắc 4).

Thứ tự đúng mục 4.4: (a) grounding -> (b) dạng host -> (c) phân giải host qua K8s/Istio
-> (d) port -> (e) chuẩn hoá identity (`identity.py`) -> (f) tự tham chiếu -> (g) khử
trùng + gán `config_finding`.

Giới hạn PoC đã biết (ghi rõ thay vì giả vờ đã làm đủ):
  - `ResolvedDestination` nhánh "thuộc dải pod CIDR" dùng `ipaddress.is_private` làm xấp
    xỉ, KHÔNG đọc cấu hình pod CIDR thật của cluster (mục 4.4c không yêu cầu độ chính
    xác tuyệt đối ở nhánh này — bản thân nó đã là `FOUND_UNRESOLVED`/`POD_IP_UNSTABLE`,
    tức "giữ lại, không dùng làm bằng chứng phủ định").
  - Không truy ngược "pod nào sở hữu IP đó" ở nhánh POD_IP_UNSTABLE — `k8s_types.py`
    không lưu IP của pod (hợp lý: đơn vị thu thập là pod TEMPLATE, mục 3.1, có thể chưa
    có pod thật nào đang chạy), nên nhánh này luôn trả về không có identity đích, đúng
    tinh thần "giữ lại nhưng không khẳng định" của chính mục 4.4c.
  - VirtualService chỉ theo route destination đầu tiên, 1 hop rồi phân giải lại (đúng
    giới hạn đã nêu ở `k8s_types.py::VirtualServiceInfo`), không mô phỏng traffic
    splitting/subset.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Optional

from ..schema import ConfigFinding, EvidenceRef, Scope, ServiceRef
from .extract import LLMReference
from .funnel import FunnelItem
from .identity import WorkloadIdentity, distinct_identities, resolve_identity
from .k8s_types import (
    EndpointsInfo,
    K8sReader,
    PodInfo,
    ServiceEntryInfo,
    ServiceInfo,
    VirtualServiceInfo,
)

DEFAULT_CLUSTER_DOMAIN = "cluster.local"

_HOSTNAME_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


# ---------------------------------------------------------------------------
# Snapshot cụm — lấy 1 lần/trial, truyền tay cho mọi hàm (không query lại nhiều lần)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClusterSnapshot:
    pods: tuple[PodInfo, ...]
    services: tuple[ServiceInfo, ...]
    endpoints: tuple[EndpointsInfo, ...]
    virtualservices: tuple[VirtualServiceInfo, ...]
    serviceentries: tuple[ServiceEntryInfo, ...]
    namespaces: frozenset


def build_cluster_snapshot(reader: K8sReader) -> ClusterSnapshot:
    """Đọc TOÀN BỘ trạng thái cụm cần cho bước [5] đúng 1 lần/trial (mục 2 "quét lại
    từ đầu cho mỗi trial"). Không gọi lại `reader` ở bất kỳ đâu khác trong module này."""
    return ClusterSnapshot(
        pods=tuple(reader.list_pods()),
        services=tuple(reader.list_services()),
        endpoints=tuple(reader.list_endpoints()),
        virtualservices=tuple(reader.list_virtualservices()),
        serviceentries=tuple(reader.list_serviceentries()),
        namespaces=frozenset(reader.list_namespaces()),
    )


# ---------------------------------------------------------------------------
# (a) Grounding
# ---------------------------------------------------------------------------


def _port_in_text(port: int, text: str) -> bool:
    return re.search(rf"(?<!\d){port}(?!\d)", text) is not None


def _is_grounded(ref: LLMReference, funnel_items_by_id: dict[str, FunnelItem]) -> bool:
    """mục 4.4a — kiểm tra trên ĐÚNG văn bản LLM đã thấy (`redacted_value`), không phải
    giá trị gốc chưa redact."""
    item = funnel_items_by_id.get(ref.item_id)
    if item is None or item.funnel_status != "KEPT":
        return False
    if ref.quote not in item.redacted_value:
        return False
    if ref.host is not None and ref.host not in ref.quote:
        return False
    if ref.port is None:
        return True
    if _port_in_text(ref.port, ref.quote):
        return True
    if ref.port_item_id is not None and ref.port_quote is not None:
        port_item = funnel_items_by_id.get(ref.port_item_id)
        if port_item is None or port_item.funnel_status != "KEPT":
            return False
        if ref.port_quote not in port_item.redacted_value:
            return False
        return _port_in_text(ref.port, ref.port_quote)
    return False


# ---------------------------------------------------------------------------
# (b) Dạng host
# ---------------------------------------------------------------------------


def _is_valid_hostname(host: str) -> bool:
    if not host or len(host) > 253:
        return False
    labels = host.rstrip(".").split(".")
    return bool(labels) and all(_HOSTNAME_LABEL.match(label) for label in labels)


def classify_host_shape(host: str) -> str:
    """Trả "IP" | "DNS" | "INVALID" — mục 4.4b."""
    try:
        ipaddress.ip_address(host)
        return "IP"
    except ValueError:
        pass
    return "DNS" if _is_valid_hostname(host) else "INVALID"


def is_loopback_host(host: str) -> bool:
    """mục 4.4b — localhost, 127.0.0.0/8, ::1, 0.0.0.0."""
    if host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


# ---------------------------------------------------------------------------
# (c) Phân giải host
# ---------------------------------------------------------------------------


def _parse_k8s_short_name(
    host: str, source_namespace: str, known_namespaces: frozenset, cluster_domain: str = DEFAULT_CLUSTER_DOMAIN,
) -> Optional[tuple[str, str]]:
    """mục 4.4c — <svc> | <svc>.<ns> | <svc>.<ns>.svc | <svc>.<ns>.svc.<cluster-domain>.
    Trả (svc, ns) nếu khớp 1 trong 4 dạng VÀ (khi có <ns> tường minh) <ns> là namespace
    có thật trong cụm; None nếu không khớp dạng nào (validate.py thử tiếp
    VirtualService/ServiceEntry/FQDN ngoài/NO_MATCH)."""
    labels = host.rstrip(".").split(".")

    if len(labels) == 1:
        return labels[0], source_namespace

    if len(labels) == 2:
        svc, ns = labels
        return (svc, ns) if ns in known_namespaces else None

    if len(labels) == 3 and labels[2] == "svc":
        svc, ns, _ = labels
        return (svc, ns) if ns in known_namespaces else None

    domain_labels = cluster_domain.split(".")
    if len(labels) == 3 + len(domain_labels) and labels[2] == "svc" and labels[3:] == domain_labels:
        svc, ns = labels[0], labels[1]
        return (svc, ns) if ns in known_namespaces else None

    return None


def _find_service(snapshot: ClusterSnapshot, namespace: str, name: str) -> Optional[ServiceInfo]:
    return next((s for s in snapshot.services if s.namespace == namespace and s.name == name), None)


def _find_virtualservice_by_host(snapshot: ClusterSnapshot, host: str) -> Optional[VirtualServiceInfo]:
    return next((vs for vs in snapshot.virtualservices if host in vs.hosts), None)


def _find_serviceentry_by_host(snapshot: ClusterSnapshot, host: str) -> Optional[ServiceEntryInfo]:
    return next((se for se in snapshot.serviceentries if host in se.hosts), None)


@dataclass(frozen=True)
class ResolvedDestination:
    """Kết quả bước (c). `pods` rỗng = không có backend trong cluster để lấy identity
    (ServiceEntry / EXTERNAL_UNREGISTERED / NO_MATCH / NO_BACKEND / POD_IP_UNSTABLE)."""

    outcome: str  # "FOUND" | "FOUND_UNRESOLVED"
    scope: Scope
    pods: tuple[PodInfo, ...] = ()
    service: Optional[ServiceInfo] = None
    unresolved_reason: Optional[str] = None
    external_kind: Optional[str] = None
    flags: tuple[str, ...] = ()


def _resolve_service(service: ServiceInfo, snapshot: ClusterSnapshot) -> ResolvedDestination:
    if service.service_type == "ExternalName":
        return ResolvedDestination("FOUND", Scope.EXTERNAL, service=service, external_kind="EXTERNAL_NAME")

    if service.selector:
        running = [
            p for p in snapshot.pods
            if p.namespace == service.namespace and p.is_running
            and service.selector.items() <= p.labels.items()
        ]
        if running:
            return ResolvedDestination("FOUND", Scope.IN_CLUSTER, pods=tuple(running), service=service)

        template = [
            p for p in snapshot.pods
            if p.namespace == service.namespace and not p.is_running
            and service.selector.items() <= p.labels.items()
        ]
        if template:
            return ResolvedDestination(
                "FOUND", Scope.IN_CLUSTER, pods=tuple(template), service=service, flags=("dest_not_running",),
            )

        return ResolvedDestination(
            "FOUND_UNRESOLVED", Scope.IN_CLUSTER, service=service, unresolved_reason="NO_BACKEND",
        )

    # Không selector — đọc Endpoints/EndpointSlice gán tay (mục 3.4 case giữ lại).
    ep = next(
        (e for e in snapshot.endpoints if e.namespace == service.namespace and e.service_name == service.name), None,
    )
    if ep is None:
        return ResolvedDestination(
            "FOUND_UNRESOLVED", Scope.IN_CLUSTER, service=service, unresolved_reason="NO_BACKEND",
        )
    if ep.pod_refs:
        pods = [
            p for (pns, pname) in ep.pod_refs
            for p in snapshot.pods if p.namespace == pns and p.name == pname
        ]
        if pods:
            return ResolvedDestination("FOUND", Scope.IN_CLUSTER, pods=tuple(pods), service=service)
    if ep.addresses:
        return ResolvedDestination(
            "FOUND", Scope.EXTERNAL, service=service, external_kind="SELECTORLESS_ENDPOINTS",
        )
    return ResolvedDestination(
        "FOUND_UNRESOLVED", Scope.IN_CLUSTER, service=service, unresolved_reason="NO_BACKEND",
    )


def resolve_host(
    host: str,
    source_namespace: str,
    snapshot: ClusterSnapshot,
    cluster_domain: str = DEFAULT_CLUSTER_DOMAIN,
    _hop: int = 0,
) -> ResolvedDestination:
    """mục 4.4c. Giả định `host` đã qua bước (b) (không INVALID/loopback)."""
    if _hop > 3:
        # Vòng VirtualService trỏ vòng -- ngoài phạm vi mục 4.4c, chặn an toàn thay vì
        # đệ quy vô hạn.
        return ResolvedDestination("FOUND_UNRESOLVED", Scope.UNKNOWN, unresolved_reason="SERVICE_NOT_FOUND")

    shape = classify_host_shape(host)

    if shape == "IP":
        matching_service = next((s for s in snapshot.services if s.cluster_ip == host), None)
        if matching_service is not None:
            return _resolve_service(matching_service, snapshot)
        ip = ipaddress.ip_address(host)
        if ip.is_private:
            return ResolvedDestination("FOUND_UNRESOLVED", Scope.UNKNOWN, unresolved_reason="POD_IP_UNSTABLE")
        return ResolvedDestination("FOUND_UNRESOLVED", Scope.EXTERNAL, unresolved_reason="EXTERNAL_IP_UNREGISTERED")

    short = _parse_k8s_short_name(host, source_namespace, snapshot.namespaces, cluster_domain)
    if short is not None:
        svc_name, ns = short
        service = _find_service(snapshot, ns, svc_name)
        if service is not None:
            return _resolve_service(service, snapshot)
        # Tên TRẦN (1 label, không khai báo namespace) không khớp gì -> NO_MATCH (mục
        # 4.4c, bullet cuối "Tên trần, không khớp gì"). Dạng CÓ khai báo namespace rõ
        # ràng (<svc>.<ns>[.svc[.<cluster-domain>]], <ns> đã được xác nhận có thật) mà
        # vẫn không khớp Service nào -> SERVICE_NOT_FOUND (bullet "Có hậu tố
        # cluster-domain nhưng không có Service" — áp dụng luôn cho cả 3 dạng có <ns>,
        # không chỉ FQDN đủ hậu tố, vì cùng bản chất "trông như tên K8s hợp lệ nhưng rỗng").
        if len(host.rstrip(".").split(".")) == 1:
            return ResolvedDestination("FOUND_UNRESOLVED", Scope.UNKNOWN, unresolved_reason="NO_MATCH")
        return ResolvedDestination("FOUND_UNRESOLVED", Scope.UNKNOWN, unresolved_reason="SERVICE_NOT_FOUND")

    vs = _find_virtualservice_by_host(snapshot, host)
    if vs is not None and vs.destination_host:
        inner = resolve_host(vs.destination_host, source_namespace, snapshot, cluster_domain, _hop + 1)
        return ResolvedDestination(
            outcome=inner.outcome, scope=inner.scope, pods=inner.pods, service=inner.service,
            unresolved_reason=inner.unresolved_reason, external_kind=inner.external_kind,
            flags=inner.flags + ("via_virtualservice",),
        )

    se = _find_serviceentry_by_host(snapshot, host)
    if se is not None:
        return ResolvedDestination("FOUND", Scope.EXTERNAL, external_kind="SERVICE_ENTRY")

    if len(host.rstrip(".").split(".")) >= 2:
        return ResolvedDestination("FOUND_UNRESOLVED", Scope.EXTERNAL, unresolved_reason="EXTERNAL_UNREGISTERED")

    return ResolvedDestination("FOUND_UNRESOLVED", Scope.UNKNOWN, unresolved_reason="NO_MATCH")


# ---------------------------------------------------------------------------
# (d) Port
# ---------------------------------------------------------------------------


def resolve_port(
    service: Optional[ServiceInfo], given_port: Optional[int],
) -> tuple[Optional[int], Optional[int], Optional[str], bool, tuple[str, ...]]:
    """mục 4.4d. Trả (service_port, target_port, protocol, port_unknown, flags)."""
    if service is None or not service.ports:
        return given_port, given_port, None, given_port is None, ()

    if given_port is not None:
        match = next((p for p in service.ports if p.port == given_port), None)
        if match is not None:
            return given_port, match.target_port, (match.app_protocol or match.protocol), False, ()
        return given_port, given_port, None, False, ("port_mismatch",)

    if len(service.ports) == 1:
        only = service.ports[0]
        return only.port, only.target_port, (only.app_protocol or only.protocol), False, ("port_inferred",)

    return None, None, None, True, ()


# ---------------------------------------------------------------------------
# (a)-(f) hợp nhất cho 1 reference
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidatedReference:
    status: str  # REJECTED_UNGROUNDED | REJECTED_INVALID_HOST | DROP_LOOPBACK | DROP_SELF |
                 # FOUND_STRUCTURAL | FOUND_EMBEDDED | FOUND_UNRESOLVED
    reference: LLMReference
    source: WorkloadIdentity
    raw_host: str
    destination: Optional[WorkloadIdentity] = None
    destination_service_ref: Optional[ServiceRef] = None
    protocol: Optional[str] = None
    service_port: Optional[int] = None
    target_port: Optional[int] = None
    port_unknown: bool = False
    scope: Scope = Scope.UNKNOWN
    unresolved_reason: Optional[str] = None
    external_kind: Optional[str] = None
    flags: tuple[str, ...] = ()
    evidence_ref: Optional[EvidenceRef] = None


def validate_reference(
    ref: LLMReference,
    funnel_items_by_id: dict[str, FunnelItem],
    source_pod: PodInfo,
    snapshot: ClusterSnapshot,
    cluster_domain: str = DEFAULT_CLUSTER_DOMAIN,
) -> list[ValidatedReference]:
    """mục 4.4(a)-(f) cho ĐÚNG 1 reference. Trả về NHIỀU `ValidatedReference` chỉ khi
    selector khớp nhiều workload khác nhau (mục 4.4e `selector_overlap` — "sinh 1
    candidate/workload"). Khử trùng cấp toàn cục (gộp nhiều reference cùng 1 cặp) là
    việc của `finalize_evidence` (bước g), không phải hàm này."""
    source_identity = resolve_identity(source_pod)
    item = funnel_items_by_id.get(ref.item_id)
    evidence_ref = (
        EvidenceRef(item_id=item.item_id, source_kind=item.source_kind, container=item.container,
                     key=item.key, quote=ref.quote)
        if item is not None else None
    )

    if not _is_grounded(ref, funnel_items_by_id):
        return [ValidatedReference(
            status="REJECTED_UNGROUNDED", reference=ref, source=source_identity,
            raw_host=ref.host or "", evidence_ref=evidence_ref,
        )]

    host = ref.host
    shape = classify_host_shape(host)
    if shape == "INVALID":
        return [ValidatedReference(
            status="REJECTED_INVALID_HOST", reference=ref, source=source_identity,
            raw_host=host, evidence_ref=evidence_ref,
        )]
    if is_loopback_host(host):
        return [ValidatedReference(
            status="DROP_LOOPBACK", reference=ref, source=source_identity,
            raw_host=host, evidence_ref=evidence_ref,
        )]

    resolved = resolve_host(host, source_pod.namespace, snapshot, cluster_domain)

    if resolved.pods:
        identities: list[Optional[WorkloadIdentity]] = list(distinct_identities(list(resolved.pods)))
        overlap_flag = ("selector_overlap",) if len(identities) > 1 else ()
    else:
        identities = [None]
        overlap_flag = ()
    flags = resolved.flags + overlap_flag

    service_ref = (
        ServiceRef(service_name=resolved.service.name, service_namespace=resolved.service.namespace)
        if resolved.service is not None else None
    )

    results: list[ValidatedReference] = []
    for dest_identity in identities:
        if dest_identity is not None and dest_identity.is_same_as(source_identity):
            results.append(ValidatedReference(
                status="DROP_SELF", reference=ref, source=source_identity, raw_host=host,
                destination=dest_identity, evidence_ref=evidence_ref,
            ))
            continue

        service_port, target_port, protocol, port_unknown, port_flags = resolve_port(resolved.service, ref.port)

        if resolved.outcome == "FOUND_UNRESOLVED":
            status = "FOUND_UNRESOLVED"
        else:
            is_full_value = item is not None and ref.quote == item.redacted_value
            status = "FOUND_STRUCTURAL" if is_full_value else "FOUND_EMBEDDED"

        results.append(ValidatedReference(
            status=status, reference=ref, source=source_identity, raw_host=host,
            destination=dest_identity, destination_service_ref=service_ref, protocol=protocol,
            service_port=service_port, target_port=target_port, port_unknown=port_unknown,
            scope=resolved.scope, unresolved_reason=resolved.unresolved_reason,
            external_kind=resolved.external_kind, flags=flags + port_flags, evidence_ref=evidence_ref,
        ))
    return results


# ---------------------------------------------------------------------------
# (g) Khử trùng + gán nhãn
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceGroup:
    """Kết quả sau khử trùng (mục 4.4g) — sẵn sàng để `store.py` (chưa viết, task #47)
    lắp vào `ConfigEvidenceRecord`; chỉ còn thiếu field orchestration-level
    (snapshot_id, collected_at, run_id, extraction_method, prompt_version, model_id)."""

    source: WorkloadIdentity
    config_finding: ConfigFinding
    raw_host: str
    destination: Optional[WorkloadIdentity]
    destination_service_ref: Optional[ServiceRef]
    protocol: Optional[str]
    service_port: Optional[int]
    target_port: Optional[int]
    port_unknown: bool
    scope: Scope
    unresolved_reason: Optional[str]
    external_kind: Optional[str]
    flags: tuple[str, ...]
    evidence_refs: tuple[EvidenceRef, ...]


_STATUS_TO_FINDING = {
    "FOUND_STRUCTURAL": ConfigFinding.FOUND_STRUCTURAL,
    "FOUND_EMBEDDED": ConfigFinding.FOUND_EMBEDDED,
    "FOUND_UNRESOLVED": ConfigFinding.FOUND_UNRESOLVED,
}
_FINDING_RANK = {  # ưu tiên STRUCTURAL hơn EMBEDDED hơn UNRESOLVED khi gộp (mục 4.4)
    ConfigFinding.FOUND_STRUCTURAL: 2,
    ConfigFinding.FOUND_EMBEDDED: 1,
    ConfigFinding.FOUND_UNRESOLVED: 0,
}


def finalize_evidence(validated: list[ValidatedReference]) -> list[EvidenceGroup]:
    """mục 4.4g — khử trùng theo (source identity, dest identity HOẶC raw_host,
    target_port), giữ TOÀN BỘ provenance trong `evidence_refs`, gán `config_finding`
    ưu tiên STRUCTURAL > EMBEDDED > UNRESOLVED. Chỉ nhận record có status thuộc 3 nhãn
    FOUND_* — REJECTED_*/DROP_* không tạo evidence cuối (nhưng vẫn cần lưu ở
    `config_extractions` cho mục 9.1 "ungrounded rate"; việc lưu đó là của caller/
    `store.py`, không phải hàm này)."""
    groups: dict[tuple, list[ValidatedReference]] = {}
    for v in validated:
        if v.status not in _STATUS_TO_FINDING:
            continue
        dest_key = (v.destination.namespace, v.destination.canonical_service) if v.destination else ("raw", v.raw_host)
        key = (v.source.namespace, v.source.canonical_service, dest_key, v.target_port)
        groups.setdefault(key, []).append(v)

    result: list[EvidenceGroup] = []
    for members in groups.values():
        best = max(members, key=lambda v: _FINDING_RANK[_STATUS_TO_FINDING[v.status]])
        all_flags = tuple(sorted({f for m in members for f in m.flags}))
        all_refs = tuple(m.evidence_ref for m in members if m.evidence_ref is not None)
        result.append(EvidenceGroup(
            source=best.source, config_finding=_STATUS_TO_FINDING[best.status], raw_host=best.raw_host,
            destination=best.destination, destination_service_ref=best.destination_service_ref,
            protocol=best.protocol, service_port=best.service_port, target_port=best.target_port,
            port_unknown=best.port_unknown, scope=best.scope, unresolved_reason=best.unresolved_reason,
            external_kind=best.external_kind, flags=all_flags, evidence_refs=all_refs,
        ))
    return result
