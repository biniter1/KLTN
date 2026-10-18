"""
Kiểu dữ liệu K8s tối giản + reader pluggable — dùng chung cho toàn bộ pipeline v2
(CONFIG_EVIDENCE_COLLECTOR.md mục 3). Chỉ giữ đúng field pipeline cần, không map 1:1
toàn bộ object client thật (tránh phụ thuộc cứng vào lib `kubernetes` ở tầng thuật
toán — đúng nguyên tắc đã áp dụng ở regex_baseline.py/selector_validation.py).

Đơn vị thu thập là WORKLOAD (pod template), không phải pod đang chạy (mục 3.1) — nên
"PodInfo" ở đây thực ra đại diện cho 1 pod template (CronJob chưa chạy/replicas=0 vẫn
phải xuất hiện). Tên giữ "PodInfo" để tương thích ngược với `regex_baseline.py`.

Hai Protocol pluggable, đúng tinh thần `selector_validation.py`:
  - K8sReader          : Fake cho test, KubernetesReader cho cluster thật.
  - ImageConfigReader  : Fake cho test; bản thật ở `image_config.py` (Registry API v2 /
    crictl trong node kind / crane) — đọc ENV/ENTRYPOINT/CMD, KHÔNG pull filesystem.

Cập nhật 2026-10-11 (sửa mục 1.5 "code khác thiết kế"):
  - `KubernetesReader.list_pods` duyệt ĐỦ pod template của Deployment / StatefulSet /
    DaemonSet / ReplicaSet độc lập / Job độc lập / CronJob (`jobTemplate`) + Pod trần
    (không controller) — kể cả `replicas=0` và CronJob chưa từng chạy (mục 3.1).
  - Env đọc THEO ĐÚNG THỨ TỰ KHAI BÁO thành `env_entries` (value / configMapKeyRef /
    fieldRef / resourceFieldRef / secretKeyRef) — mục 3.2.
  - `envFrom.secretRef`, volume `secret`, `projected` (configMap + secret) được ghi nhận.
  - `image_id` (digest thật của image đang chạy, lấy từ containerStatuses) để tra image
    config theo digest (mục 3.2 "theo digest").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


# ---------------------------------------------------------------------------
# Container / Pod template
# ---------------------------------------------------------------------------


@dataclass
class EnvFromSource:
    configmap_name: str
    prefix: str = ""  # mục 3.2: envFrom.configMapRef — tính cả prefix


@dataclass
class EnvEntry:
    """1 phần tử `env:` của container, GIỮ ĐÚNG THỨ TỰ khai báo (quan trọng cho
    $(VAR) expansion ở merge.py — chỉ biến khai báo TRƯỚC mới dùng được).

    kind:
      - "value"            : `value:` literal (thiếu `value` = chuỗi rỗng, đúng K8s)
      - "configMapKeyRef"  : `valueFrom.configMapKeyRef` -> collect.py thay bằng giá trị thật
      - "fieldRef"         : Downward API (`status.podIP`, `spec.nodeName`...) -> placeholder
      - "resourceFieldRef" : Downward API (`limits.memory`...) -> placeholder
      - "secretKeyRef"     : KHÔNG đọc giá trị (mục 3.4) -> item SECRET_REF đếm được
      - "unknown"          : valueFrom loại khác (phiên bản K8s mới hơn) -> placeholder
    """

    name: str
    kind: str = "value"
    value: str = ""
    configmap_name: Optional[str] = None
    key: Optional[str] = None
    optional: bool = False
    field_path: Optional[str] = None
    resource: Optional[str] = None
    container_name: Optional[str] = None


@dataclass
class VolumeMount:
    name: str  # khớp với Volume.name cùng Pod
    mount_path: str
    sub_path: Optional[str] = None  # mount đúng 1 key làm 1 file (không phải cả thư mục)


@dataclass
class ContainerSpec:
    name: str
    container_type: str = "app"  # "app" | "init" (mục 3.2 — initContainers hay chứa script chờ dependency)
    image: Optional[str] = None  # tham chiếu image trong spec (tag hoặc digest)
    env: dict[str, str] = field(default_factory=dict)  # LEGACY: chỉ literal value (dùng khi env_entries rỗng)
    env_secret_ref_count: int = 0  # LEGACY: số secretKeyRef (dùng khi env_entries rỗng)
    env_from: list[EnvFromSource] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    volume_mounts: list[VolumeMount] = field(default_factory=list)
    # --- mới 2026-10-11 ---
    env_entries: list[EnvEntry] = field(default_factory=list)  # có -> collect.py dùng cái này, bỏ qua env/env_secret_ref_count
    env_from_secret_refs: list[str] = field(default_factory=list)  # tên Secret trong envFrom.secretRef (chỉ tên, không đọc giá trị)
    image_id: Optional[str] = None  # digest image ĐANG CHẠY (containerStatuses.imageID) — None nếu chưa có pod

    # Tương thích ngược: code cũ (regex_baseline.py) dùng `env_from_configmap_names:
    # list[str]`. Thuộc tính này KHÔNG tồn tại ở đây nữa (dùng `env_from` thay thế) —
    # xem `regex_baseline.py` (module riêng, không đụng tới k8s_types.py này).


@dataclass
class ConfigMapProjection:
    """1 nguồn configMap bên trong volume `projected`."""

    configmap_name: str
    items: Optional[dict[str, str]] = None  # key -> path (None = mọi key, giữ nguyên tên)


@dataclass
class VolumeSource:
    """Volume cấp Pod — mục 3.2: ConfigMap mount dạng file (trực tiếp hoặc qua
    `projected`). `items=None` nghĩa là mount TOÀN BỘ key của ConfigMap, mỗi key
    thành 1 file cùng tên; `items` khác None là map {key: đường dẫn file con}.
    Volume `secret` / nguồn secret trong `projected`: chỉ giữ TÊN Secret (mục 3.4)."""

    name: str
    configmap_name: Optional[str] = None
    configmap_items: Optional[dict[str, str]] = None  # key gốc -> path (None = giữ nguyên tên key)
    projected_configmaps: list[ConfigMapProjection] = field(default_factory=list)
    secret_names: list[str] = field(default_factory=list)


@dataclass
class PodInfo:
    """Đại diện 1 WORKLOAD pod template (mục 3.1), không phải 1 pod instance đang chạy."""

    name: str
    namespace: str
    service_account_name: str
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)  # mục 3.2
    containers: list[ContainerSpec] = field(default_factory=list)
    volumes: list[VolumeSource] = field(default_factory=list)
    is_running: bool = True  # False = chỉ có pod template (CronJob/replicas=0), không pod thật đang chạy (mục 4.4c)
    workload_kind: Optional[str] = None  # "Deployment" | "StatefulSet" | ... | "Pod" (None ở Fake/test cũ)

    @property
    def canonical_service(self) -> Optional[str]:
        """mục 4.4e — ưu tiên nhãn Istio, nếu thiếu áp fallback chain Istio tự dùng:
        app.kubernetes.io/name -> app -> None (identity.py sẽ rơi tiếp về tên workload)."""
        return (
            self.labels.get("service.istio.io/canonical-name")
            or self.labels.get("security.istio.io/canonical-name")  # tên nhãn cũ v1, giữ tương thích
        )


# ---------------------------------------------------------------------------
# Service / Endpoints / ConfigMap / Istio
# ---------------------------------------------------------------------------


@dataclass
class ServicePortInfo:
    port: int
    target_port: int  # mục 4.4d — PHẢI có, dùng sinh NetworkPolicy (không phải service_port)
    name: Optional[str] = None
    protocol: str = "TCP"
    app_protocol: Optional[str] = None


@dataclass
class ServiceInfo:
    name: str
    namespace: str
    selector: Optional[dict[str, str]] = None  # None/rỗng = không có selector (mục 3, case đặc biệt)
    cluster_ip: Optional[str] = "10.0.0.1"  # "None" (chuỗi) = headless — không dùng trong PoC (mục 3)
    service_type: str = "ClusterIP"  # "ClusterIP" | "ExternalName" | ...
    external_name: Optional[str] = None  # service_type == "ExternalName"
    ports: list[ServicePortInfo] = field(default_factory=list)


@dataclass
class ConfigMapInfo:
    name: str
    namespace: str
    data: dict[str, str] = field(default_factory=dict)
    binary_keys: frozenset[str] = frozenset()  # mục 3.4 funnel BINARY — key nào là binaryData


@dataclass
class EndpointsInfo:
    """Service không có selector + Endpoints/EndpointSlice gán tay (mục 3, case giữ lại)."""

    service_name: str
    namespace: str
    addresses: list[str] = field(default_factory=list)  # IP hoặc hostname trỏ ra ngoài cluster
    # (namespace, TÊN WORKLOAD) — KubernetesReader đã đổi tên pod -> tên workload sở hữu
    # nó, để khớp đúng `PodInfo.name` (vốn là tên workload, không phải tên pod).
    pod_refs: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class VirtualServiceInfo:
    """Rút gọn tối đa cho mục 4.4c nhánh VirtualService: chỉ cần hosts + 1 đích đơn
    giản hoá ("theo route destination 1 bước rồi phân giải lại", không mô phỏng toàn
    bộ ngữ nghĩa traffic-splitting/subset của Istio — ngoài phạm vi PoC)."""

    name: str
    namespace: str
    hosts: list[str] = field(default_factory=list)
    destination_host: Optional[str] = None  # route đầu tiên trỏ tới (short name hoặc FQDN)


@dataclass
class ServiceEntryInfo:
    name: str
    namespace: str
    hosts: list[str] = field(default_factory=list)


@dataclass
class ImageConfig:
    """"Dấu vết Dockerfile" của 1 image — mục 3.2. `entrypoint`/`cmd` dùng ở merge.py
    bước 2 (ENTRYPOINT/CMD bị ghi đè bởi command/args của Pod); `env` dùng làm tầng
    thấp nhất trong effective config."""

    entrypoint: list[str] = field(default_factory=list)
    cmd: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


class K8sReader(Protocol):
    def list_pods(self, namespace: Optional[str] = None) -> list[PodInfo]: ...
    def list_services(self, namespace: Optional[str] = None) -> list[ServiceInfo]: ...
    def list_configmaps(self, namespace: Optional[str] = None) -> list[ConfigMapInfo]: ...
    def list_endpoints(self, namespace: Optional[str] = None) -> list[EndpointsInfo]: ...
    def list_virtualservices(self, namespace: Optional[str] = None) -> list[VirtualServiceInfo]: ...
    def list_serviceentries(self, namespace: Optional[str] = None) -> list[ServiceEntryInfo]: ...
    def list_namespaces(self) -> list[str]: ...


class ImageConfigReader(Protocol):
    def get_image_config(self, image_ref: str) -> Optional[ImageConfig]: ...


def image_refs_for(container: ContainerSpec) -> list[str]:
    """Thứ tự tra image config: digest đang chạy (`image_id`, đúng cái thật sự chạy —
    mục 3.2 "theo digest") rồi mới tới tham chiếu trong spec (`image`, có thể là tag,
    dùng khi workload chưa có pod nào: replicas=0 / CronJob chưa chạy)."""
    refs: list[str] = []
    for ref in (container.image_id, container.image):
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def lookup_image_config(
    image_reader: ImageConfigReader, container: ContainerSpec,
) -> tuple[Optional[ImageConfig], Optional[str]]:
    """Trả (ImageConfig, ref đã dùng) — (None, None) nếu không đọc được bằng ref nào.
    collect.py và merge.py dùng CHUNG hàm này để 2 bước luôn thấy cùng 1 image config."""
    for ref in image_refs_for(container):
        cfg = image_reader.get_image_config(ref)
        if cfg is not None:
            return cfg, ref
    return None, None


# ---------------------------------------------------------------------------
# Fake (test/demo) — không gọi API thật
# ---------------------------------------------------------------------------


class FakeK8sReader:
    """Dùng cho test/demo — không gọi K8s API thật. Nhận thẳng danh sách object đã
    dựng sẵn trong bộ nhớ, filter theo namespace giống hành vi API thật."""

    def __init__(
        self,
        pods: list[PodInfo] | None = None,
        services: list[ServiceInfo] | None = None,
        configmaps: list[ConfigMapInfo] | None = None,
        endpoints: list[EndpointsInfo] | None = None,
        virtualservices: list[VirtualServiceInfo] | None = None,
        serviceentries: list[ServiceEntryInfo] | None = None,
        namespaces: list[str] | None = None,
    ):
        self._pods = pods or []
        self._services = services or []
        self._configmaps = configmaps or []
        self._endpoints = endpoints or []
        self._virtualservices = virtualservices or []
        self._serviceentries = serviceentries or []
        # mục 4.4c: "<a>.<b> chỉ coi là <svc>.<ns> khi <b> là namespace có thật" — mặc
        # định suy ra từ namespace của mọi object đã cho, cho tiện viết test; truyền
        # tay `namespaces=` khi cần kiểm tra đúng namespace KHÔNG tồn tại.
        self._namespaces = namespaces if namespaces is not None else sorted(
            {p.namespace for p in self._pods}
            | {s.namespace for s in self._services}
            | {c.namespace for c in self._configmaps}
            | {e.namespace for e in self._endpoints}
        )

    def list_pods(self, namespace: Optional[str] = None) -> list[PodInfo]:
        return [p for p in self._pods if namespace is None or p.namespace == namespace]

    def list_services(self, namespace: Optional[str] = None) -> list[ServiceInfo]:
        return [s for s in self._services if namespace is None or s.namespace == namespace]

    def list_configmaps(self, namespace: Optional[str] = None) -> list[ConfigMapInfo]:
        return [c for c in self._configmaps if namespace is None or c.namespace == namespace]

    def list_endpoints(self, namespace: Optional[str] = None) -> list[EndpointsInfo]:
        return [e for e in self._endpoints if namespace is None or e.namespace == namespace]

    def list_virtualservices(self, namespace: Optional[str] = None) -> list[VirtualServiceInfo]:
        return [v for v in self._virtualservices if namespace is None or v.namespace == namespace]

    def list_serviceentries(self, namespace: Optional[str] = None) -> list[ServiceEntryInfo]:
        return [v for v in self._serviceentries if namespace is None or v.namespace == namespace]

    def list_namespaces(self) -> list[str]:
        return list(self._namespaces)


class FakeImageConfigReader:
    def __init__(self, images: dict[str, ImageConfig] | None = None):
        self._images = images or {}

    def get_image_config(self, image_ref: str) -> Optional[ImageConfig]:
        return self._images.get(image_ref)


class NullImageConfigReader:
    """Luôn trả None — pipeline bỏ qua tầng image ENV/ENTRYPOINT/CMD mà không lỗi.
    Từ 2026-10-11 KHÔNG còn là mặc định của `collect_config_evidence` (mặc định mới:
    `image_config.default_image_reader()`, chọn theo biến môi trường
    CONFIG_COLLECTOR_IMAGE_READER). Chỉ dùng khi đặt CONFIG_COLLECTOR_IMAGE_READER=none
    (test suite tự đặt, xem tests/conftest.py)."""

    def get_image_config(self, image_ref: str) -> Optional[ImageConfig]:
        return None


# ---------------------------------------------------------------------------
# Thật — thư viện `kubernetes` chính thức.
# ---------------------------------------------------------------------------

# Loại controller mà Pod con của nó KHÔNG được liệt kê riêng (đã có pod template ở
# workload cha). Pod có controller KHÁC (CRD của operator, Node với static pod...) hoặc
# không có controller -> liệt kê như 1 workload "Pod" riêng, để KHÔNG BỎ SÓT gì.
_POD_PARENT_KINDS = frozenset({"ReplicaSet", "StatefulSet", "DaemonSet", "Job"})


def _controller_ref(metadata):
    for ref in (getattr(metadata, "owner_references", None) or []):
        if getattr(ref, "controller", False):
            return ref
    return None


def _items(raw) -> list:
    return list(getattr(raw, "items", None) or [])


class KubernetesReader:
    """Đọc kubeconfig thật (mục 2: không subprocess kubectl). Các API Istio
    (VirtualService/ServiceEntry) đọc qua CustomObjectsApi, best-effort — nếu CRD
    chưa cài hoặc RBAC thiếu quyền, trả về [] thay vì crash cả lần quét (một nguồn
    bổ sung không nên làm hỏng toàn bộ pipeline chỉ vì thiếu 1 CRD).

    Workload (Deployment/StatefulSet/...) thì KHÔNG best-effort: thiếu quyền `list` thì
    exception bay lên (trial FAILED) — thà fail to rõ ràng còn hơn âm thầm bỏ sót 1
    loại workload (false negative im lặng, đúng loại lỗi module này tồn tại để tránh).

    RBAC cần (ClusterRole read-only): `list` trên pods, services, endpoints,
    configmaps, namespaces (core); deployments, statefulsets, daemonsets, replicasets
    (apps); jobs, cronjobs (batch); virtualservices, serviceentries (networking.istio.io).
    Không cần `secrets`.

    Các tham số `core_api`/`apps_api`/`batch_api`/`custom_api` chỉ để test truyền API
    giả vào (không cần cluster); dùng thật thì bỏ trống.
    """

    def __init__(
        self,
        kubeconfig_path: str | None = None,
        *,
        core_api=None,
        apps_api=None,
        batch_api=None,
        custom_api=None,
    ):
        self._kubeconfig_path = kubeconfig_path
        self._client = core_api
        self._apps_api = apps_api
        self._batch_api = batch_api
        self._custom_api = custom_api

    def _ensure_client(self):
        if self._client is not None and self._apps_api is not None and self._batch_api is not None:
            return
        from kubernetes import client, config as k8s_config

        if self._kubeconfig_path:
            k8s_config.load_kube_config(config_file=self._kubeconfig_path)
        else:
            k8s_config.load_kube_config()
        self._client = self._client or client.CoreV1Api()
        self._apps_api = self._apps_api or client.AppsV1Api()
        self._batch_api = self._batch_api or client.BatchV1Api()
        self._custom_api = self._custom_api or client.CustomObjectsApi()

    @staticmethod
    def _list(api, namespaced_fn: str, all_fn: str, namespace: Optional[str]) -> list:
        if namespace:
            return _items(getattr(api, namespaced_fn)(namespace))
        return _items(getattr(api, all_fn)())

    # ---------------- container / volume ----------------

    @staticmethod
    def _extract_env_entries(container) -> list[EnvEntry]:
        entries: list[EnvEntry] = []
        for e in container.env or []:
            vf = getattr(e, "value_from", None)
            if vf is None:
                entries.append(EnvEntry(name=e.name, kind="value", value=e.value or ""))
            elif getattr(vf, "config_map_key_ref", None) is not None:
                ref = vf.config_map_key_ref
                entries.append(EnvEntry(
                    name=e.name, kind="configMapKeyRef", configmap_name=ref.name, key=ref.key,
                    optional=bool(getattr(ref, "optional", False)),
                ))
            elif getattr(vf, "secret_key_ref", None) is not None:
                entries.append(EnvEntry(name=e.name, kind="secretKeyRef"))
            elif getattr(vf, "field_ref", None) is not None:
                entries.append(EnvEntry(name=e.name, kind="fieldRef", field_path=vf.field_ref.field_path))
            elif getattr(vf, "resource_field_ref", None) is not None:
                rfr = vf.resource_field_ref
                entries.append(EnvEntry(
                    name=e.name, kind="resourceFieldRef", resource=rfr.resource,
                    container_name=getattr(rfr, "container_name", None),
                ))
            else:
                entries.append(EnvEntry(name=e.name, kind="unknown"))
        return entries

    @staticmethod
    def _extract_env_from(container) -> tuple[list[EnvFromSource], list[str]]:
        configmaps: list[EnvFromSource] = []
        secrets: list[str] = []
        for ef in container.env_from or []:
            if getattr(ef, "config_map_ref", None) is not None:
                configmaps.append(EnvFromSource(configmap_name=ef.config_map_ref.name, prefix=ef.prefix or ""))
            elif getattr(ef, "secret_ref", None) is not None:
                secrets.append(ef.secret_ref.name)
        return configmaps, secrets

    @staticmethod
    def _extract_volume_mounts(container) -> list[VolumeMount]:
        return [
            VolumeMount(name=vm.name, mount_path=vm.mount_path, sub_path=vm.sub_path or None)
            for vm in (container.volume_mounts or [])
        ]

    def _extract_container(self, c, container_type: str, image_id: Optional[str] = None) -> ContainerSpec:
        entries = self._extract_env_entries(c)
        env_from, env_from_secrets = self._extract_env_from(c)
        return ContainerSpec(
            name=c.name,
            container_type=container_type,
            image=c.image,
            # 2 field legacy vẫn điền cho ai còn đọc chúng; collect.py dùng env_entries.
            env={en.name: en.value for en in entries if en.kind == "value"},
            env_secret_ref_count=sum(1 for en in entries if en.kind == "secretKeyRef"),
            env_from=env_from,
            command=list(c.command or []),
            args=list(c.args or []),
            volume_mounts=self._extract_volume_mounts(c),
            env_entries=entries,
            env_from_secret_refs=env_from_secrets,
            image_id=image_id,
        )

    @staticmethod
    def _extract_volumes(volumes) -> list[VolumeSource]:
        result = []
        for v in volumes or []:
            if getattr(v, "config_map", None) is not None:
                cm = v.config_map
                items = {it.key: it.path for it in cm.items} if cm.items else None
                result.append(VolumeSource(name=v.name, configmap_name=cm.name, configmap_items=items))
            elif getattr(v, "secret", None) is not None:
                result.append(VolumeSource(name=v.name, secret_names=[v.secret.secret_name or ""]))
            elif getattr(v, "projected", None) is not None:
                projections: list[ConfigMapProjection] = []
                secret_names: list[str] = []
                for src in (v.projected.sources or []):
                    if getattr(src, "config_map", None) is not None:
                        cm = src.config_map
                        items = {it.key: it.path for it in cm.items} if cm.items else None
                        projections.append(ConfigMapProjection(configmap_name=cm.name, items=items))
                    elif getattr(src, "secret", None) is not None:
                        secret_names.append(src.secret.name or "")
                    # downwardAPI / serviceAccountToken / clusterTrustBundle: không chứa
                    # địa chỉ dependency -> bỏ qua.
                if projections or secret_names:
                    result.append(VolumeSource(
                        name=v.name, projected_configmaps=projections, secret_names=secret_names,
                    ))
        return result

    def _pod_info(
        self, name: str, namespace: str, kind: str, template, is_running: bool,
        image_ids: dict[str, str],
    ) -> PodInfo:
        """`template` là V1PodTemplateSpec (workload) hoặc chính V1Pod (Pod trần) —
        cả 2 đều có `.metadata` + `.spec` (V1PodSpec)."""
        meta = template.metadata
        spec = template.spec
        containers = [
            self._extract_container(c, "app", image_ids.get(c.name)) for c in (spec.containers or [])
        ]
        containers += [
            self._extract_container(c, "init", image_ids.get(c.name)) for c in (spec.init_containers or [])
        ]
        return PodInfo(
            name=name,
            namespace=namespace,
            service_account_name=(spec.service_account_name or getattr(spec, "service_account", None) or "default"),
            labels=dict((meta.labels if meta else None) or {}),
            annotations=dict((meta.annotations if meta else None) or {}),
            containers=containers,
            volumes=self._extract_volumes(spec.volumes),
            is_running=is_running,
            workload_kind=kind,
        )

    # ---------------- workload inventory ----------------

    @staticmethod
    def _owner_workload(pod, rs_owner: dict, job_owner: dict) -> tuple[str, str]:
        """Pod thật -> (kind, tên) workload sở hữu nó, khớp đúng cách `list_pods` đặt
        tên. Pod -> ReplicaSet -> Deployment; Pod -> Job -> CronJob."""
        ns = pod.metadata.namespace
        ctrl = _controller_ref(pod.metadata)
        if ctrl is None or ctrl.kind not in _POD_PARENT_KINDS:
            return "Pod", pod.metadata.name
        if ctrl.kind == "ReplicaSet":
            parent = rs_owner.get((ns, ctrl.name))
            if parent is not None and parent.kind == "Deployment":
                return "Deployment", parent.name
            return "ReplicaSet", ctrl.name
        if ctrl.kind == "Job":
            parent = job_owner.get((ns, ctrl.name))
            if parent is not None and parent.kind == "CronJob":
                return "CronJob", parent.name
            return "Job", ctrl.name
        return ctrl.kind, ctrl.name  # StatefulSet / DaemonSet

    def _inventory(self, namespace: Optional[str]) -> tuple[list[PodInfo], dict[tuple[str, str], str]]:
        """Trả (danh sách workload template, map (ns, tên pod thật) -> tên workload)."""
        self._ensure_client()
        apps, batch, core = self._apps_api, self._batch_api, self._client

        deployments = self._list(apps, "list_namespaced_deployment", "list_deployment_for_all_namespaces", namespace)
        statefulsets = self._list(apps, "list_namespaced_stateful_set", "list_stateful_set_for_all_namespaces", namespace)
        daemonsets = self._list(apps, "list_namespaced_daemon_set", "list_daemon_set_for_all_namespaces", namespace)
        replicasets = self._list(apps, "list_namespaced_replica_set", "list_replica_set_for_all_namespaces", namespace)
        jobs = self._list(batch, "list_namespaced_job", "list_job_for_all_namespaces", namespace)
        cronjobs = self._list(batch, "list_namespaced_cron_job", "list_cron_job_for_all_namespaces", namespace)
        pods = self._list(core, "list_namespaced_pod", "list_pod_for_all_namespaces", namespace)

        rs_owner = {(rs.metadata.namespace, rs.metadata.name): _controller_ref(rs.metadata) for rs in replicasets}
        job_owner = {(j.metadata.namespace, j.metadata.name): _controller_ref(j.metadata) for j in jobs}

        # digest image đang chạy, theo (ns, kind, workload) -> {container: imageID}
        image_ids: dict[tuple[str, str, str], dict[str, str]] = {}
        pod_owner: dict[tuple[str, str], tuple[str, str]] = {}
        for p in pods:
            kind, wname = self._owner_workload(p, rs_owner, job_owner)
            pod_owner[(p.metadata.namespace, p.metadata.name)] = (kind, wname)
            status = getattr(p, "status", None)
            statuses = list(getattr(status, "container_statuses", None) or [])
            statuses += list(getattr(status, "init_container_statuses", None) or [])
            bucket = image_ids.setdefault((p.metadata.namespace, kind, wname), {})
            for cs in statuses:
                if getattr(cs, "image_id", None):
                    bucket.setdefault(cs.name, cs.image_id)

        # (kind, obj, template, is_running) theo đúng thứ tự ưu tiên giữ tên gốc khi trùng
        entries: list[tuple[str, object, object, bool]] = []
        for d in deployments:
            entries.append(("Deployment", d, d.spec.template, (getattr(d.status, "ready_replicas", None) or 0) > 0))
        for s in statefulsets:
            entries.append(("StatefulSet", s, s.spec.template, (getattr(s.status, "ready_replicas", None) or 0) > 0))
        for ds in daemonsets:
            entries.append(("DaemonSet", ds, ds.spec.template, (getattr(ds.status, "number_ready", None) or 0) > 0))
        for rs in replicasets:
            ctrl = _controller_ref(rs.metadata)
            if ctrl is not None and ctrl.kind == "Deployment":
                continue  # ReplicaSet do Deployment quản (kể cả revision cũ) -> đã có template ở Deployment
            entries.append(("ReplicaSet", rs, rs.spec.template, (getattr(rs.status, "ready_replicas", None) or 0) > 0))
        for j in jobs:
            ctrl = _controller_ref(j.metadata)
            if ctrl is not None and ctrl.kind == "CronJob":
                continue  # Job do CronJob sinh ra -> đã có template ở CronJob
            entries.append(("Job", j, j.spec.template, (getattr(j.status, "active", None) or 0) > 0))
        for cj in cronjobs:
            active = getattr(cj.status, "active", None) if cj.status is not None else None
            entries.append(("CronJob", cj, cj.spec.job_template.spec.template, bool(active)))
        for p in pods:
            if pod_owner[(p.metadata.namespace, p.metadata.name)][0] != "Pod":
                continue
            phase = getattr(p.status, "phase", None) if p.status is not None else None
            entries.append(("Pod", p, p, phase == "Running"))

        result: list[PodInfo] = []
        final_name: dict[tuple[str, str, str], str] = {}
        taken: set[tuple[str, str]] = set()
        for kind, obj, template, running in entries:
            ns, name = obj.metadata.namespace, obj.metadata.name
            out_name = name
            if (ns, out_name) in taken:
                # Hiếm: 2 workload KHÁC loại trùng tên trong 1 namespace. Pipeline khoá
                # theo tên workload, nên đổi tên cái sau thay vì để 2 cái đè nhau.
                out_name = f"{name}-{kind.lower()}"
            taken.add((ns, out_name))
            final_name[(ns, kind, name)] = out_name
            result.append(self._pod_info(
                out_name, ns, kind, template, running, image_ids.get((ns, kind, name), {}),
            ))

        pod_to_workload = {
            key: final_name.get((key[0], kind, wname), wname) for key, (kind, wname) in pod_owner.items()
        }
        return result, pod_to_workload

    def list_pods(self, namespace: Optional[str] = None) -> list[PodInfo]:
        """mục 3.1 — MỌI pod template: Deployment / StatefulSet / DaemonSet / ReplicaSet
        độc lập / Job độc lập / CronJob (`jobTemplate`) / Pod trần, KỂ CẢ replicas=0 và
        CronJob chưa từng chạy. `PodInfo.name` = tên workload (không phải tên pod ngẫu
        nhiên kiểu `frontend-7d9f6c-abcde`), `is_running` theo status của workload:
        Deployment/StatefulSet/ReplicaSet `readyReplicas>0`, DaemonSet `numberReady>0`,
        Job `active>0`, CronJob `status.active` khác rỗng, Pod `phase=Running`."""
        return self._inventory(namespace)[0]

    def list_services(self, namespace: Optional[str] = None) -> list[ServiceInfo]:
        self._ensure_client()
        result = []
        for s in self._list(self._client, "list_namespaced_service", "list_service_for_all_namespaces", namespace):
            ports = [
                ServicePortInfo(
                    port=p.port,
                    target_port=(p.target_port if isinstance(p.target_port, int) else p.port),
                    name=p.name,
                    protocol=p.protocol or "TCP",
                    app_protocol=getattr(p, "app_protocol", None),
                )
                for p in (s.spec.ports or [])
            ]
            result.append(
                ServiceInfo(
                    name=s.metadata.name,
                    namespace=s.metadata.namespace,
                    selector=dict(s.spec.selector) if s.spec.selector else None,
                    cluster_ip=s.spec.cluster_ip,
                    service_type=s.spec.type or "ClusterIP",
                    external_name=getattr(s.spec, "external_name", None),
                    ports=ports,
                )
            )
        return result

    def list_configmaps(self, namespace: Optional[str] = None) -> list[ConfigMapInfo]:
        self._ensure_client()
        return [
            ConfigMapInfo(
                name=c.metadata.name,
                namespace=c.metadata.namespace,
                data=dict(c.data or {}),
                binary_keys=frozenset((c.binary_data or {}).keys()),
            )
            for c in self._list(self._client, "list_namespaced_config_map", "list_config_map_for_all_namespaces", namespace)
        ]

    def list_endpoints(self, namespace: Optional[str] = None) -> list[EndpointsInfo]:
        self._ensure_client()
        raw = self._list(self._client, "list_namespaced_endpoints", "list_endpoints_for_all_namespaces", namespace)
        pod_to_workload: Optional[dict] = None
        result = []
        for e in raw:
            addrs: list[str] = []
            pod_refs: list[tuple[str, str]] = []
            for subset in (e.subsets or []):
                for a in (subset.addresses or []):
                    addrs.append(a.ip)
                for a in list(subset.addresses or []) + list(getattr(subset, "not_ready_addresses", None) or []):
                    tr = getattr(a, "target_ref", None)
                    if tr is not None and tr.kind == "Pod":
                        if pod_to_workload is None:
                            pod_to_workload = self._inventory(namespace)[1]
                        pns = tr.namespace or e.metadata.namespace
                        ref = (pns, pod_to_workload.get((pns, tr.name), tr.name))
                        if ref not in pod_refs:
                            pod_refs.append(ref)
            result.append(
                EndpointsInfo(
                    service_name=e.metadata.name, namespace=e.metadata.namespace,
                    addresses=addrs, pod_refs=pod_refs,
                )
            )
        return result

    def list_virtualservices(self, namespace: Optional[str] = None) -> list[VirtualServiceInfo]:  # pragma: no cover
        self._ensure_client()
        try:
            if namespace:
                raw = self._custom_api.list_namespaced_custom_object(
                    "networking.istio.io", "v1beta1", namespace, "virtualservices"
                )
            else:
                raw = self._custom_api.list_cluster_custom_object(
                    "networking.istio.io", "v1beta1", "virtualservices"
                )
        except Exception:
            # CRD chưa cài / RBAC thiếu quyền -- nguồn bổ sung, không chặn pipeline chính.
            return []
        result = []
        for item in raw.get("items", []):
            meta = item.get("metadata", {})
            spec = item.get("spec", {})
            dest_host = None
            for http_route in spec.get("http", []) or []:
                routes = http_route.get("route", []) or []
                if routes:
                    dest_host = routes[0].get("destination", {}).get("host")
                    break
            result.append(
                VirtualServiceInfo(
                    name=meta.get("name", ""), namespace=meta.get("namespace", ""),
                    hosts=list(spec.get("hosts", []) or []), destination_host=dest_host,
                )
            )
        return result

    def list_serviceentries(self, namespace: Optional[str] = None) -> list[ServiceEntryInfo]:  # pragma: no cover
        self._ensure_client()
        try:
            if namespace:
                raw = self._custom_api.list_namespaced_custom_object(
                    "networking.istio.io", "v1beta1", namespace, "serviceentries"
                )
            else:
                raw = self._custom_api.list_cluster_custom_object(
                    "networking.istio.io", "v1beta1", "serviceentries"
                )
        except Exception:
            return []
        result = []
        for item in raw.get("items", []):
            meta = item.get("metadata", {})
            spec = item.get("spec", {})
            result.append(
                ServiceEntryInfo(
                    name=meta.get("name", ""), namespace=meta.get("namespace", ""),
                    hosts=list(spec.get("hosts", []) or []),
                )
            )
        return result

    def list_namespaces(self) -> list[str]:
        self._ensure_client()
        return [ns.metadata.name for ns in _items(self._client.list_namespace())]


class CraneImageConfigReader:
    """Đọc ENV/ENTRYPOINT/CMD của image qua `crane config <ref>` (subprocess CLI,
    KHÔNG pull filesystem/layer — mục 3.2). Cần `crane` cài sẵn; không có thì trả None.
    Từ 2026-10-11 có thêm `image_config.RegistryImageConfigReader` (Python thuần, không
    cần cài CLI) và `image_config.KindNodeImageConfigReader` (image nạp bằng `kind
    load`) — reader này giữ lại như 1 lựa chọn (CONFIG_COLLECTOR_IMAGE_READER=crane).
    Cache theo image_ref trong tiến trình."""

    def __init__(self):
        self._cache: dict[str, Optional[ImageConfig]] = {}

    def get_image_config(self, image_ref: str) -> Optional[ImageConfig]:  # pragma: no cover
        if image_ref in self._cache:
            return self._cache[image_ref]
        result = self._fetch(image_ref)
        self._cache[image_ref] = result
        return result

    @staticmethod
    def _fetch(image_ref: str) -> Optional[ImageConfig]:  # pragma: no cover
        import json
        import shutil
        import subprocess

        if shutil.which("crane") is None:
            return None
        try:
            proc = subprocess.run(
                ["crane", "config", image_ref], capture_output=True, text=True, timeout=30, check=True,
            )
            raw = json.loads(proc.stdout)
        except Exception:
            return None
        return image_config_from_oci_config(raw)


def image_config_from_oci_config(raw: dict) -> Optional[ImageConfig]:
    """Blob config OCI/Docker (`{"config": {"Env": [...], "Entrypoint": [...], "Cmd":
    [...]}}`) -> ImageConfig. Dùng chung cho mọi reader (registry/crictl/crane)."""
    if not isinstance(raw, dict):
        return None
    cfg = raw.get("config")
    if not isinstance(cfg, dict):
        cfg = raw.get("Config") if isinstance(raw.get("Config"), dict) else {}
    env_list = cfg.get("Env") or []
    env = dict(kv.split("=", 1) for kv in env_list if isinstance(kv, str) and "=" in kv)
    return ImageConfig(
        entrypoint=list(cfg.get("Entrypoint") or []),
        cmd=list(cfg.get("Cmd") or []),
        env=env,
    )
