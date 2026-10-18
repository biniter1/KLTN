"""
Config Evidence Collector — BASELINE REGEX (v1, 26/09/2026) — CONFIG_EVIDENCE_COLLECTOR.md
mục 9.2, 10.

**ĐÂY KHÔNG CÒN LÀ ĐƯỜNG CHÍNH.** Từ 29/09/2026, đường chính là pipeline LLM-assisted ở
`config_collector/pipeline.py` (xem `CONFIG_EVIDENCE_COLLECTOR.md` v2). File này — bộ parser
rule-based 3 cấp cú pháp của v1 — được GIỮ LẠI NGUYÊN VẸN, không xoá, để làm **baseline so
sánh recall/precision** với pipeline LLM trên cùng ground truth R=15 trial (mục 9.2): chạy cả
2 trên cùng dữ liệu, kiểm định Wilcoxon signed-rank trên 15 cặp recall và 15 cặp precision.

Trả lời câu hỏi: "theo cấu hình đã khai báo trong cluster, service này có tham chiếu
tới service khác không" — không chạy traffic, không đo đạc, chỉ đọc chữ (env, envFrom,
command, args, ConfigMap) rồi đối chiếu ngược với Service/Endpoints thật.

Batch, một lần (mục 2) — không watch/controller. Chạy từ ngoài cluster qua kubeconfig,
dùng thư viện `kubernetes` chính thức, không subprocess `kubectl` (đúng lựa chọn công
nghệ đã chốt).

Thiết kế pluggable (Protocol) đúng tinh thần `selector_validation.py`:
  - FakeK8sReader        : dùng cho test/demo, không cần cluster thật — tự dựng
    danh sách Pod/Service/ConfigMap/Endpoints trong bộ nhớ.
  - KubernetesReader     : hiện thực đầy đủ bằng lib `kubernetes` chính thức, đọc
    kubeconfig thật. Không chạy được trong môi trường demo này (không có cluster),
    nhưng code đầy đủ để dùng thật khi lên cluster (kind/minikube theo mục 2).

Pipeline giải mã (mục 4), brick-by-brick:
    1. parse_candidate_addresses  — tách địa chỉ theo 3 cấp cú pháp
    2. _looks_like_address        — lọc cú pháp thô (LOG_LEVEL=info, TIMEOUT=30 loại)
    3. pair_host_port_vars        — ghép cặp HOST/PORT, ADDR/PORT theo hậu tố đã biết
    4. _dedupe_candidates         — khử trùng envFrom vs env liệt kê riêng
    5. _remove_self_references    — loại candidate trỏ về chính source
    6. _resolve_against_cluster   — xác nhận ngược với Service/Endpoints thật
    7. _canonicalize_identity     — đọc nhãn security.istio.io/canonical-name + serviceAccountName

NOTE — điểm mở/diễn giải: mục 4 bước 2 mô tả "LOG_LEVEL=info, TIMEOUT=30 bị loại [ở
bước lọc cú pháp]", nhưng về mặt cú pháp thuần túy một token như "info" (chỉ chữ cái)
không thể phân biệt với một tên Service hợp lệ (ví dụ "redis") chỉ bằng regex — sự phân
biệt thật sự chỉ có được ở bước 6 (khớp danh sách Service thật hay không). Cách hiện
thực ở đây: bước 2 chỉ loại các token KHÔNG THỂ là một DNS label hợp lệ (có khoảng
trắng, ký tự đặc biệt ngoài '-'/'.', thuần số, hoặc quá dài >253 ký tự theo RFC 1123) —
phần còn lại (bao gồm cả "info") được giữ làm candidate cấp 3 và bị loại thật sự ở
bước 6 nếu không khớp Service nào (rơi vào NOT_FOUND, không gây hại — nguyên tắc bất
đối xứng an toàn mục 4 cuối tài liệu).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol
from urllib.parse import urlparse

from ..schema import ConfigEvidenceRecord, ConfigFinding, Scope

# ---------------------------------------------------------------------------
# Shape dữ liệu K8s tối giản — chỉ giữ đúng field pipeline cần, không map 1:1
# toàn bộ object client thật (tránh phụ thuộc cứng vào lib `kubernetes` ở tầng
# thuật toán, chỉ KubernetesReader mới cần import lib đó).
# ---------------------------------------------------------------------------


@dataclass
class ContainerSpec:
    name: str
    env: dict[str, str] = field(default_factory=dict)  # chỉ literal value (bỏ qua valueFrom Secret/field-ref)
    env_from_configmap_names: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)


@dataclass
class PodInfo:
    name: str
    namespace: str
    service_account_name: str
    labels: dict[str, str] = field(default_factory=dict)
    containers: list[ContainerSpec] = field(default_factory=list)

    @property
    def canonical_service(self) -> Optional[str]:
        """mục 7c — ĐỌC ĐÚNG nhãn Istio, không lấy thẳng tên Pod/Deployment."""
        return self.labels.get("security.istio.io/canonical-name")


@dataclass
class ServiceInfo:
    name: str
    namespace: str
    selector: Optional[dict[str, str]] = None  # None/rỗng = không có selector (mục 3, case đặc biệt)
    cluster_ip: Optional[str] = "10.0.0.1"  # "None" (chuỗi) = headless — không dùng trong PoC (mục 3)


@dataclass
class ConfigMapInfo:
    name: str
    namespace: str
    data: dict[str, str] = field(default_factory=dict)


@dataclass
class EndpointsInfo:
    """Service không có selector + Endpoints/EndpointSlice gán tay (mục 3, case giữ lại)."""

    service_name: str
    namespace: str
    addresses: list[str] = field(default_factory=list)  # IP hoặc hostname trỏ ra ngoài cluster


class K8sReader(Protocol):
    def list_pods(self, namespace: Optional[str] = None) -> list[PodInfo]: ...
    def list_services(self, namespace: Optional[str] = None) -> list[ServiceInfo]: ...
    def list_configmaps(self, namespace: Optional[str] = None) -> list[ConfigMapInfo]: ...
    def list_endpoints(self, namespace: Optional[str] = None) -> list[EndpointsInfo]: ...


class FakeK8sReader:
    """Dùng cho test/demo — không gọi K8s API thật. Nhận thẳng danh sách object đã
    dựng sẵn trong bộ nhớ, filter theo namespace giống hành vi API thật."""

    def __init__(
        self,
        pods: list[PodInfo] | None = None,
        services: list[ServiceInfo] | None = None,
        configmaps: list[ConfigMapInfo] | None = None,
        endpoints: list[EndpointsInfo] | None = None,
    ):
        self._pods = pods or []
        self._services = services or []
        self._configmaps = configmaps or []
        self._endpoints = endpoints or []

    def list_pods(self, namespace: Optional[str] = None) -> list[PodInfo]:
        return [p for p in self._pods if namespace is None or p.namespace == namespace]

    def list_services(self, namespace: Optional[str] = None) -> list[ServiceInfo]:
        return [s for s in self._services if namespace is None or s.namespace == namespace]

    def list_configmaps(self, namespace: Optional[str] = None) -> list[ConfigMapInfo]:
        return [c for c in self._configmaps if namespace is None or c.namespace == namespace]

    def list_endpoints(self, namespace: Optional[str] = None) -> list[EndpointsInfo]:
        return [e for e in self._endpoints if namespace is None or e.namespace == namespace]


class KubernetesReader:
    """Hiện thực thật bằng thư viện `kubernetes` chính thức (mục 2: đọc thẳng
    kubeconfig, không subprocess kubectl). Không chạy được trong môi trường demo
    này (không có cluster) — code đầy đủ để dùng thật khi lên cluster."""

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

    @staticmethod
    def _extract_env(container) -> dict[str, str]:  # pragma: no cover - phụ thuộc cluster thật
        env: dict[str, str] = {}
        for e in container.env or []:
            if e.value is not None:  # bỏ qua valueFrom (Secret/fieldRef) — ngoài phạm vi (mục 3 collector)
                env[e.name] = e.value
        return env

    @staticmethod
    def _extract_env_from_configmap_names(container) -> list[str]:  # pragma: no cover
        names = []
        for ef in container.env_from or []:
            if ef.config_map_ref is not None:
                names.append(ef.config_map_ref.name)
        return names

    def list_pods(self, namespace: Optional[str] = None) -> list[PodInfo]:  # pragma: no cover
        self._ensure_client()
        api = self._client
        raw = api.list_namespaced_pod(namespace) if namespace else api.list_pod_for_all_namespaces()
        result = []
        for p in raw.items:
            containers = [
                ContainerSpec(
                    name=c.name,
                    env=self._extract_env(c),
                    env_from_configmap_names=self._extract_env_from_configmap_names(c),
                    command=list(c.command or []),
                    args=list(c.args or []),
                )
                for c in (p.spec.containers or [])
            ]
            result.append(
                PodInfo(
                    name=p.metadata.name,
                    namespace=p.metadata.namespace,
                    service_account_name=p.spec.service_account_name or "default",
                    labels=dict(p.metadata.labels or {}),
                    containers=containers,
                )
            )
        return result

    def list_services(self, namespace: Optional[str] = None) -> list[ServiceInfo]:  # pragma: no cover
        self._ensure_client()
        api = self._client
        raw = api.list_namespaced_service(namespace) if namespace else api.list_service_for_all_namespaces()
        return [
            ServiceInfo(
                name=s.metadata.name,
                namespace=s.metadata.namespace,
                selector=dict(s.spec.selector) if s.spec.selector else None,
                cluster_ip=s.spec.cluster_ip,
            )
            for s in raw.items
        ]

    def list_configmaps(self, namespace: Optional[str] = None) -> list[ConfigMapInfo]:  # pragma: no cover
        self._ensure_client()
        api = self._client
        raw = api.list_namespaced_config_map(namespace) if namespace else api.list_config_map_for_all_namespaces()
        return [
            ConfigMapInfo(name=c.metadata.name, namespace=c.metadata.namespace, data=dict(c.data or {}))
            for c in raw.items
        ]

    def list_endpoints(self, namespace: Optional[str] = None) -> list[EndpointsInfo]:  # pragma: no cover
        self._ensure_client()
        api = self._client
        raw = api.list_namespaced_endpoints(namespace) if namespace else api.list_endpoints_for_all_namespaces()
        result = []
        for e in raw.items:
            addrs: list[str] = []
            for subset in (e.subsets or []):
                for a in (subset.addresses or []):
                    addrs.append(a.ip)
            result.append(EndpointsInfo(service_name=e.metadata.name, namespace=e.metadata.namespace, addresses=addrs))
        return result


# ---------------------------------------------------------------------------
# Bước 1-2 — parse + lọc cú pháp (mục 4)
# ---------------------------------------------------------------------------


@dataclass
class ParsedAddress:
    raw_value: str
    host: Optional[str]
    port: Optional[int]
    port_unknown: bool
    evidence_source: str  # "env" | "envFrom" | "command" | "args" | "configmap"
    source_key: str  # tên biến/khóa gốc (để pairing HOST/PORT nếu cần)


_DNS_LABEL_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$")
_FQDN_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$")
_HOST_PORT_RE = re.compile(r"^([a-zA-Z0-9][a-zA-Z0-9\-\.]*):(\d{1,5})$")


def _looks_like_address(token: str) -> bool:
    """Lọc cú pháp thô (bước 2). Loại: rỗng, có khoảng trắng, thuần số, quá dài,
    hoặc không khớp cú pháp FQDN/DNS-label nào. Xem NOTE ở docstring module —
    phân biệt ngữ nghĩa sâu hơn (LOG_LEVEL=info) để bước 6 (khớp Service thật) xử lý."""
    if not token or " " in token or "\t" in token:
        return False
    if token.isdigit():
        return False
    if len(token) > 253:
        return False
    return bool(_FQDN_RE.match(token))


def parse_candidate_addresses(
    raw_kv: dict[str, str],
    evidence_source: str,
) -> list[ParsedAddress]:
    """Bước 1+2 — với MỌI giá trị (không đoán trước tên biến "có khả năng" chứa địa
    chỉ), thử tách theo 3 cấp cú pháp; loại các giá trị không khớp cú pháp địa chỉ nào."""
    results: list[ParsedAddress] = []
    for key, value in raw_kv.items():
        value = (value or "").strip()
        if not value:
            continue

        # Cấp 1 — có scheme đầy đủ: "http://productcatalogservice:3550[/path]"
        if "://" in value:
            parsed = urlparse(value)
            host = parsed.hostname
            if host and _looks_like_address(host):
                results.append(
                    ParsedAddress(
                        raw_value=value, host=host, port=parsed.port,
                        port_unknown=parsed.port is None,
                        evidence_source=evidence_source, source_key=key,
                    )
                )
            continue

        # Cấp 2 — chỉ host:port: "productcatalogservice:3550"
        m = _HOST_PORT_RE.match(value)
        if m:
            host, port_str = m.group(1), m.group(2)
            if _looks_like_address(host) and 0 < int(port_str) <= 65535:
                results.append(
                    ParsedAddress(
                        raw_value=value, host=host, port=int(port_str),
                        port_unknown=False, evidence_source=evidence_source, source_key=key,
                    )
                )
            continue

        # Cấp 3 — chỉ tên trần: "productcatalogservice"
        if _looks_like_address(value):
            results.append(
                ParsedAddress(
                    raw_value=value, host=value, port=None,
                    port_unknown=True, evidence_source=evidence_source, source_key=key,
                )
            )

    return results


# ---------------------------------------------------------------------------
# Bước 3 — ghép cặp HOST/PORT tách biệt (mục 4)
# ---------------------------------------------------------------------------

_HOST_SUFFIXES = ("_HOST", "_ADDR")
_PORT_SUFFIX = "_PORT"


def pair_host_port_vars(env: dict[str, str]) -> dict[str, str]:
    """Ghép "<PREFIX>_HOST"/"<PREFIX>_ADDR" với "<PREFIX>_PORT" (đúng tiền tố, không
    gộp tùy tiện theo tiền tố chung chung như DB_HOST + DB_MAX_CONNECTIONS) thành 1
    giá trị "host:port" duy nhất, gắn dưới khóa "<PREFIX>_HOST" (giữ nguyên khóa host
    làm định danh cặp). Trả về env ĐÃ ghép cặp CỘNG với các biến không tham gia ghép
    cặp nào (giữ nguyên để bước 1-2 parse độc lập)."""
    result: dict[str, str] = {}
    consumed_port_keys: set[str] = set()

    for key, value in env.items():
        matched_suffix = next((s for s in _HOST_SUFFIXES if key.endswith(s)), None)
        if matched_suffix is None:
            continue
        prefix = key[: -len(matched_suffix)]
        port_key = f"{prefix}{_PORT_SUFFIX}"
        if port_key in env:
            result[key] = f"{value}:{env[port_key]}"
            consumed_port_keys.add(port_key)
        else:
            result[key] = value

    for key, value in env.items():
        if key in result or key in consumed_port_keys:
            continue
        if any(key.endswith(s) for s in _HOST_SUFFIXES):
            continue  # đã xử lý ở vòng trên (có thể không ghép được cặp)
        result[key] = value

    return result


# ---------------------------------------------------------------------------
# Bước 4 — khử trùng envFrom vs env liệt kê riêng
# ---------------------------------------------------------------------------


def _dedupe_candidates(candidates: list[ParsedAddress]) -> list[ParsedAddress]:
    """envFrom (đổ toàn bộ ConfigMap) có thể trùng key+value với env liệt kê riêng —
    giữ 1 bản, ưu tiên nguồn "env" (tường minh hơn) khi trùng."""
    seen: dict[tuple, ParsedAddress] = {}
    priority = {"env": 0, "command": 1, "args": 1, "configmap": 2, "envFrom": 3}
    for c in candidates:
        dedupe_key = (c.host, c.port, c.raw_value)
        existing = seen.get(dedupe_key)
        if existing is None or priority.get(c.evidence_source, 9) < priority.get(existing.evidence_source, 9):
            seen[dedupe_key] = c
    return list(seen.values())


# ---------------------------------------------------------------------------
# Bước 5 — loại tự tham chiếu
# ---------------------------------------------------------------------------


def _remove_self_references(candidates: list[ParsedAddress], own_names: set[str]) -> list[ParsedAddress]:
    """own_names: mọi tên mà chính source pod có thể tự xưng (tên Pod, tên (các)
    Service chọn trúng pod này, canonical_service) — loại candidate trỏ về chính nó
    (biến kiểu SERVICE_NAME dùng để log/tag, không phải để gọi)."""
    return [c for c in candidates if c.host not in own_names]


# ---------------------------------------------------------------------------
# Bước 6 — xác nhận ngược với K8s (Service/Endpoints thật)
# ---------------------------------------------------------------------------


@dataclass
class ResolvedCandidate:
    parsed: ParsedAddress
    finding: ConfigFinding
    scope: Scope
    matched_service: Optional[ServiceInfo] = None
    matched_endpoints: Optional[EndpointsInfo] = None


def _resolve_against_cluster(
    candidates: list[ParsedAddress],
    services_by_name: dict[str, ServiceInfo],
    endpoints_by_name: dict[str, EndpointsInfo],
) -> list[ResolvedCandidate]:
    resolved: list[ResolvedCandidate] = []
    for c in candidates:
        # Host có thể là FQDN "svc.namespace.svc.cluster.local" hoặc bare "svc" — lấy
        # thành phần đầu tiên làm tên Service để tra (đủ cho phạm vi PoC, một namespace).
        short_name = c.host.split(".")[0] if c.host else None

        svc = services_by_name.get(short_name) if short_name else None
        if svc is not None:
            finding = (
                ConfigFinding.FOUND_STRUCTURAL
                if c.port is not None and not c.port_unknown
                else ConfigFinding.FOUND_EMBEDDED
            )
            resolved.append(ResolvedCandidate(parsed=c, finding=finding, scope=Scope.IN_CLUSTER, matched_service=svc))
            continue

        eps = endpoints_by_name.get(short_name) if short_name else None
        if eps is not None:
            # Service không selector + Endpoints thủ công — case đặc biệt giữ lại (mục 3):
            # dùng để phát hiện dependency ra ngoài cluster "giả trang" thành Service nội bộ.
            resolved.append(
                ResolvedCandidate(parsed=c, finding=ConfigFinding.FOUND_EMBEDDED, scope=Scope.EXTERNAL, matched_endpoints=eps)
            )
            continue

        # Không khớp Service nội bộ nào — thử coi là địa chỉ ngoài (có dấu chấm, không
        # thuộc danh sách Service nội bộ) => EXTERNAL/UNKNOWN thay vì NOT_FOUND thẳng.
        if c.host and "." in c.host:
            resolved.append(ResolvedCandidate(parsed=c, finding=ConfigFinding.FOUND_UNRESOLVED, scope=Scope.EXTERNAL))
        else:
            # Không khớp gì (kể cả trùng tên tình cờ) -> GIỮ LẠI, gắn cờ, KHÔNG xóa,
            # KHÔNG coi là bằng chứng phủ định (luật cứng mục 1).
            resolved.append(ResolvedCandidate(parsed=c, finding=ConfigFinding.FOUND_UNRESOLVED, scope=Scope.UNKNOWN))

    return resolved


# ---------------------------------------------------------------------------
# Bước 7 — chuẩn hóa identity (source lẫn destination)
# ---------------------------------------------------------------------------


def _find_service_pods(service: ServiceInfo, pods_by_namespace: dict[str, list[PodInfo]]) -> list[PodInfo]:
    """mục 7a-b: Service -> spec.selector -> tập pod khớp."""
    if not service.selector:
        return []
    candidates = pods_by_namespace.get(service.namespace, [])
    return [p for p in candidates if service.selector.items() <= p.labels.items()]


def _canonicalize_from_pod(pod: PodInfo) -> tuple[Optional[str], Optional[str]]:
    """mục 7c: đọc TRỰC TIẾP nhãn security.istio.io/canonical-name + serviceAccountName
    từ pod thật — không lấy thẳng tên Service/Deployment."""
    return pod.canonical_service, pod.service_account_name


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def collect_config_evidence(
    reader: K8sReader,
    snapshot_id: str,
    namespaces: Optional[list[str]] = None,
    collected_at: Optional[str] = None,
) -> list[ConfigEvidenceRecord]:
    """Điều phối toàn bộ pipeline mục 4-7, trả về danh sách `ConfigEvidenceRecord`
    (schema mục 6) — input trực tiếp cho `candidates.discover_candidates()` bên Tier 1
    (C_potential, mục 1 TIER1_DESIGN.md)."""
    collected_at = collected_at or datetime.now(timezone.utc).isoformat()

    all_pods: list[PodInfo] = []
    all_services: list[ServiceInfo] = []
    all_configmaps: list[ConfigMapInfo] = []
    all_endpoints: list[EndpointsInfo] = []

    if namespaces:
        for ns in namespaces:
            all_pods.extend(reader.list_pods(ns))
            all_services.extend(reader.list_services(ns))
            all_configmaps.extend(reader.list_configmaps(ns))
            all_endpoints.extend(reader.list_endpoints(ns))
    else:
        all_pods = reader.list_pods()
        all_services = reader.list_services()
        all_configmaps = reader.list_configmaps()
        all_endpoints = reader.list_endpoints()

    services_by_name = {s.name: s for s in all_services}
    endpoints_by_name = {e.service_name: e for e in all_endpoints}
    configmaps_by_key = {(c.namespace, c.name): c for c in all_configmaps}
    pods_by_namespace: dict[str, list[PodInfo]] = {}
    for p in all_pods:
        pods_by_namespace.setdefault(p.namespace, []).append(p)

    # Cache resolve identity theo Service name để không lặp lại bước 7a-b cho mỗi candidate
    _identity_cache: dict[tuple[str, str], tuple[Optional[str], Optional[str]]] = {}

    def _resolve_identity_for_service_name(service_name: str, namespace: str) -> tuple[Optional[str], Optional[str]]:
        cache_key = (namespace, service_name)
        if cache_key in _identity_cache:
            return _identity_cache[cache_key]
        svc = services_by_name.get(service_name)
        result: tuple[Optional[str], Optional[str]] = (None, None)
        if svc is not None:
            matched_pods = _find_service_pods(svc, pods_by_namespace)
            if matched_pods:
                result = _canonicalize_from_pod(matched_pods[0])
        _identity_cache[cache_key] = result
        return result

    records: list[ConfigEvidenceRecord] = []

    for pod in all_pods:
        # own_names (bước 5): tên Pod + tên mọi Service chọn trúng pod này + canonical_service
        own_names = {pod.name}
        if pod.canonical_service:
            own_names.add(pod.canonical_service)
        for svc in all_services:
            if svc.namespace == pod.namespace and svc.selector and svc.selector.items() <= pod.labels.items():
                own_names.add(svc.name)

        raw_kv: dict[str, str] = {}
        source_map: dict[str, str] = {}  # key -> evidence_source, dùng lại sau khi pair_host_port_vars làm phẳng nguồn

        for container in pod.containers:
            for k, v in container.env.items():
                raw_kv[k] = v
                source_map[k] = "env"
            for cm_name in container.env_from_configmap_names:
                cm = configmaps_by_key.get((pod.namespace, cm_name))
                if cm:
                    for k, v in cm.data.items():
                        raw_kv.setdefault(k, v)
                        source_map.setdefault(k, "envFrom")
            for i, tok in enumerate(container.command):
                raw_kv[f"__command_{container.name}_{i}"] = tok
                source_map[f"__command_{container.name}_{i}"] = "command"
            for i, tok in enumerate(container.args):
                raw_kv[f"__args_{container.name}_{i}"] = tok
                source_map[f"__args_{container.name}_{i}"] = "args"

        paired = pair_host_port_vars(raw_kv)

        all_candidates: list[ParsedAddress] = []
        for key, value in paired.items():
            src = source_map.get(key, "env")
            all_candidates.extend(parse_candidate_addresses({key: value}, evidence_source=src))

        all_candidates = _dedupe_candidates(all_candidates)
        all_candidates = _remove_self_references(all_candidates, own_names)
        resolved = _resolve_against_cluster(all_candidates, services_by_name, endpoints_by_name)

        source_canonical, source_sa = _canonicalize_from_pod(pod)
        if source_canonical is None:
            # Pod chưa được Istio admission gắn nhãn (chưa inject sidecar) — không đủ
            # định danh chuẩn để ghi evidence có ý nghĩa cho Tier 1 (mục 3 TIER1_DESIGN.md
            # cần canonical_service khớp giữa traffic và config evidence).
            continue

        for rc in resolved:
            dest_short_name = rc.parsed.host.split(".")[0] if rc.parsed.host else rc.parsed.host
            dest_canonical: Optional[str] = None
            dest_sa: Optional[str] = None
            dest_namespace = pod.namespace

            if rc.matched_service is not None:
                dest_namespace = rc.matched_service.namespace
                dest_canonical, dest_sa = _resolve_identity_for_service_name(rc.matched_service.name, dest_namespace)

            # Không resolve được canonical đích thật (chưa có pod chạy / chưa inject
            # sidecar / EXTERNAL/UNKNOWN) -> vẫn giữ evidence, dùng tên parse được làm
            # canonical tạm (đúng nguyên tắc open-world: không xóa, gắn cờ).
            if dest_canonical is None:
                dest_canonical = dest_short_name or rc.parsed.raw_value

            records.append(
                ConfigEvidenceRecord(
                    source_namespace=pod.namespace,
                    source_canonical_service=source_canonical,
                    source_service_account=source_sa or pod.service_account_name,
                    destination_namespace=dest_namespace,
                    destination_canonical_service=dest_canonical,
                    destination_service_account=dest_sa,
                    protocol=None,  # Collector không tách được protocol tầng ứng dụng (mục 6: null hợp lệ)
                    port=rc.parsed.port,
                    port_unknown=rc.parsed.port_unknown,
                    config_finding=rc.finding,
                    evidence_source=rc.parsed.evidence_source,
                    scope=rc.scope,
                    snapshot_id=snapshot_id,
                    collected_at=collected_at,
                )
            )

    return records
