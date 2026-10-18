"""
Config Evidence Collector v2 — bước [1] THU THẬP (CONFIG_EVIDENCE_COLLECTOR.md mục 3, 4.0).

Đọc toàn bộ nguồn liệt kê ở mục 3.2, cho MỌI workload (pod template — mục 3.1) trong
1 hoặc toàn bộ namespace, gắn provenance đầy đủ. Bước này KHÔNG áp bất kỳ quy tắc ưu
tiên/ghi đè nào (đó là việc của `merge.py` bước [2]), KHÔNG lọc gì theo nội dung (đó là
việc của `funnel.py` bước [3]), KHÔNG gọi K8s/Istio API để xác thực (đó là việc của
`validate.py` bước [5]). Đơn vị là 1 `RawConfigItem`: (workload, container, source_kind,
key, value thô).

Nguyên tắc bước này: LẤY NHIỀU NHẤT CÓ THỂ — mọi thứ đọc được đều thành item (kể cả
giá trị rỗng, placeholder), để funnel quyết định và ĐẾM được cái gì bị loại.

`env` (đọc theo `ContainerSpec.env_entries`, đúng thứ tự khai báo):
  - `value`                       -> item "env", giá trị literal (thiếu value = "").
  - `valueFrom.configMapKeyRef`   -> item "env", THAY bằng giá trị thật trong ConfigMap
                                     (source_ref = "configmap:<tên>/<key>"). ConfigMap/key
                                     không tồn tại -> giá trị "" (funnel đếm EMPTY), source_ref
                                     ghi "(missing)" — không âm thầm bỏ.
  - `valueFrom.fieldRef`          -> item "env", placeholder "<fieldRef:status.podIP>"
    `valueFrom.resourceFieldRef`     (mục 3.2: giá trị chỉ có lúc runtime, giữ TÊN TRƯỜNG,
                                     không phải địa chỉ đích). Giữ placeholder để biến khác
                                     dùng $(POD_IP) vẫn thấy được nó trỏ về đâu (merge.py).
  - `valueFrom.secretKeyRef`      -> item "secret_ref" (mục 3.4, không đọc giá trị).
Container cũ/Fake không có `env_entries` -> đi đường legacy (`env` dict + đếm
`env_secret_ref_count`), giữ nguyên hành vi trước.

Secret (mục 3.4): KHÔNG đọc giá trị thật (không có RBAC `secrets`). Mọi tham chiếu —
`secretKeyRef`, `envFrom.secretRef`, volume `secret`, nguồn secret trong `projected` —
thành item placeholder "<redacted>" để `funnel.py` loại với lý do SECRET_REF và ĐẾM được
trong `collection_quality_log` (mục 6).

ConfigMap mount dạng file: `volumes.configMap`, `projected.sources[].configMap`, `items`,
`subPath` (mục 3.2).

Image config: chỉ `ENV` của image tạo raw item ở đây (`source_kind="image_env"`).
`ENTRYPOINT`/`CMD` của image KHÔNG tạo raw item — không phải "giá trị" theo nghĩa địa
chỉ độc lập, mà là thành phần của command/args hiệu lực; `merge.py` đọc thẳng từ
`ImageConfig` để ghép với `command`/`args` của Pod theo đúng mục 4.1. Tra theo digest
đang chạy trước, tag trong spec sau (`k8s_types.lookup_image_config`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .k8s_types import (
    ConfigMapInfo,
    ContainerSpec,
    EnvEntry,
    ImageConfigReader,
    K8sReader,
    PodInfo,
    VolumeSource,
    lookup_image_config,
)

SECRET_PLACEHOLDER = "<redacted>"


@dataclass(frozen=True)
class RawConfigItem:
    """1 item cấu hình thô, TRƯỚC khi hợp nhất (mục 4.0 bước [1]).

    `source_kind` dùng nội bộ bước 1–3: "env" | "envFrom" | "secret_ref" | "command" |
    "args" | "configmap_file" | "image_env" | "annotation". Khớp với
    `EvidenceRef.source_kind` (schema.py) cho các loại còn sống sót tới evidence cuối
    (secret_ref luôn bị funnel loại trước khi tới đó).

    `source_ref` của item "env": None = literal `value:` (merge.py chỉ expand $(VAR)
    cho loại này — đúng K8s: valueFrom không bị expand); khác None = valueFrom.
    """

    workload: str
    namespace: str
    container: str  # "" = gắn với cả Pod, không riêng container nào (annotation)
    container_type: str  # "app" | "init" | "pod"
    source_kind: str
    key: str
    value: str
    source_ref: Optional[str] = None  # tên ConfigMap/key gốc, đường dẫn file mount, image ref...
    is_binary: bool = False  # mục 3.4 funnel BINARY — không coi nội dung là text được


def _configmap_lookup(configmaps: list[ConfigMapInfo]) -> dict[tuple[str, str], ConfigMapInfo]:
    return {(cm.namespace, cm.name): cm for cm in configmaps}


def _item(pod: PodInfo, container: ContainerSpec, source_kind: str, key: str, value: str,
          source_ref: Optional[str] = None, is_binary: bool = False) -> RawConfigItem:
    return RawConfigItem(
        workload=pod.name, namespace=pod.namespace, container=container.name,
        container_type=container.container_type, source_kind=source_kind, key=key, value=value,
        source_ref=source_ref, is_binary=is_binary,
    )


def _env_entry_item(
    pod: PodInfo, container: ContainerSpec, entry: EnvEntry, cm_index: dict[tuple[str, str], ConfigMapInfo],
) -> RawConfigItem:
    if entry.kind == "value":
        return _item(pod, container, "env", entry.name, entry.value)

    if entry.kind == "configMapKeyRef":
        ref = f"configmap:{entry.configmap_name}/{entry.key}"
        cm = cm_index.get((pod.namespace, entry.configmap_name or ""))
        if cm is None or entry.key not in cm.data:
            # ConfigMap/key không tồn tại (optional, hoặc cấu hình lỗi thật của cluster):
            # vẫn ghi lại để đếm được, giá trị rỗng -> funnel EMPTY.
            is_binary = cm is not None and entry.key in cm.binary_keys
            return _item(pod, container, "env", entry.name, "", source_ref=f"{ref} (missing)", is_binary=is_binary)
        return _item(pod, container, "env", entry.name, cm.data[entry.key], source_ref=ref)

    if entry.kind == "fieldRef":
        return _item(pod, container, "env", entry.name, f"<fieldRef:{entry.field_path}>",
                     source_ref=f"downward:fieldRef/{entry.field_path}")

    if entry.kind == "resourceFieldRef":
        return _item(pod, container, "env", entry.name, f"<resourceFieldRef:{entry.resource}>",
                     source_ref=f"downward:resourceFieldRef/{entry.resource}")

    if entry.kind == "secretKeyRef":
        return _item(pod, container, "secret_ref", entry.name, SECRET_PLACEHOLDER, source_ref="secret")

    return _item(pod, container, "env", entry.name, "<valueFrom:unknown>", source_ref="valueFrom:unknown")


def _collect_env_items(
    pod: PodInfo, container: ContainerSpec, cm_index: dict[tuple[str, str], ConfigMapInfo]
) -> list[RawConfigItem]:
    items: list[RawConfigItem] = []

    if container.env_entries:
        for entry in container.env_entries:
            items.append(_env_entry_item(pod, container, entry, cm_index))
    else:
        # Đường legacy (Fake/test cũ không điền env_entries) — hành vi giữ nguyên.
        for key, value in container.env.items():
            items.append(_item(pod, container, "env", key, value))

    for ef in container.env_from:
        cm = cm_index.get((pod.namespace, ef.configmap_name))
        if cm is None:
            # ConfigMap không tồn tại (xoá sau khi tạo Deployment, hoặc lỗi cấu hình
            # thật của cluster) -- bỏ qua an toàn, không phải lỗi của collector.
            continue
        for cm_key, cm_value in cm.data.items():
            items.append(_item(
                pod, container, "envFrom", f"{ef.prefix}{cm_key}", cm_value,
                source_ref=f"configmap:{cm.name}/{cm_key}", is_binary=cm_key in cm.binary_keys,
            ))

    if not container.env_entries:
        for i in range(container.env_secret_ref_count):
            items.append(_item(pod, container, "secret_ref", f"<secretKeyRef#{i}>", SECRET_PLACEHOLDER, source_ref="secret"))

    for i, _secret_name in enumerate(container.env_from_secret_refs):
        items.append(_item(pod, container, "secret_ref", f"<envFrom.secretRef#{i}>", SECRET_PLACEHOLDER, source_ref="secret"))

    return items


def _collect_command_args(pod: PodInfo, container: ContainerSpec) -> list[RawConfigItem]:
    items: list[RawConfigItem] = []
    for i, token in enumerate(container.command):
        items.append(_item(pod, container, "command", f"[{i}]", token))
    for i, token in enumerate(container.args):
        items.append(_item(pod, container, "args", f"[{i}]", token))
    return items


def _configmap_sources(vol: VolumeSource) -> list[tuple[str, Optional[dict[str, str]]]]:
    sources: list[tuple[str, Optional[dict[str, str]]]] = []
    if vol.configmap_name is not None:
        sources.append((vol.configmap_name, vol.configmap_items))
    sources.extend((p.configmap_name, p.items) for p in vol.projected_configmaps)
    return sources


def _collect_configmap_file_items(
    pod: PodInfo, cm_index: dict[tuple[str, str], ConfigMapInfo]
) -> list[RawConfigItem]:
    """mục 3.2 "ConfigMap mount dạng file" — gắn theo TỪNG container thực sự mount nó
    (qua `volumeMounts`), không gắn chung chung ở cấp Pod, để provenance đủ chi tiết
    cho `evidence_refs.container` (mục 6). `subPath` (mount đúng 1 file) được dịch về
    key gốc qua `items` nếu có. Volume `projected` có thể gộp NHIỀU ConfigMap."""
    items: list[RawConfigItem] = []
    volumes_by_name = {v.name: v for v in pod.volumes}

    for container in pod.containers:
        for mount in container.volume_mounts:
            vol = volumes_by_name.get(mount.name)
            if vol is None:
                continue

            for i, _secret_name in enumerate(vol.secret_names):
                items.append(_item(
                    pod, container, "secret_ref", f"<volume.secret:{vol.name}#{i}>", SECRET_PLACEHOLDER,
                    source_ref=f"secret -> {mount.mount_path}",
                ))

            sources = _configmap_sources(vol)
            single_source = len(sources) == 1
            for cm_name, cm_items in sources:
                cm = cm_index.get((pod.namespace, cm_name))
                if cm is None:
                    continue
                all_keys = set(cm.data.keys()) | cm.binary_keys

                if mount.sub_path is not None:
                    path_to_key = {path: key for key, path in (cm_items or {}).items()}
                    key = path_to_key.get(mount.sub_path, mount.sub_path)
                    if key not in all_keys and not single_source:
                        continue  # subPath thuộc nguồn khác trong projected
                    keys = [key]
                elif cm_items is not None:
                    keys = list(cm_items.keys())
                else:
                    keys = sorted(all_keys)

                for key in keys:
                    is_binary = key in cm.binary_keys
                    value = "" if is_binary else cm.data.get(key, "")
                    dest_name = (cm_items or {}).get(key, key)
                    items.append(_item(
                        pod, container, "configmap_file", key, value,
                        source_ref=f"configmap:{cm.name}/{key} -> {mount.mount_path}/{dest_name}",
                        is_binary=is_binary,
                    ))
    return items


def _collect_image_env_items(
    pod: PodInfo, container: ContainerSpec, image_reader: ImageConfigReader
) -> list[RawConfigItem]:
    image_config, used_ref = lookup_image_config(image_reader, container)
    if image_config is None:
        return []
    return [
        _item(pod, container, "image_env", key, value, source_ref=f"image:{used_ref}")
        for key, value in image_config.env.items()
    ]


def _collect_annotation_items(pod: PodInfo) -> list[RawConfigItem]:
    return [
        RawConfigItem(
            workload=pod.name, namespace=pod.namespace, container="",
            container_type="pod", source_kind="annotation", key=key, value=value,
        )
        for key, value in pod.annotations.items()
    ]


def collect_raw_items(
    reader: K8sReader,
    image_reader: ImageConfigReader,
    namespace: Optional[str] = None,
) -> list[RawConfigItem]:
    """Thu thập TOÀN BỘ raw item cho mọi workload trong `namespace` (hoặc mọi
    namespace nếu `namespace=None`) — mục 3.2, 4.0 bước [1]. Thứ tự trả về giữ nguyên
    thứ tự khai báo gốc theo từng container (quan trọng cho `merge.py` khi phân xử
    "nguồn sau thắng nguồn trước khi trùng key" trong `envFrom`/`env`, mục 4.1)."""
    pods = reader.list_pods(namespace)
    configmaps = reader.list_configmaps(namespace)
    cm_index = _configmap_lookup(configmaps)

    items: list[RawConfigItem] = []
    for pod in pods:
        items.extend(_collect_annotation_items(pod))
        items.extend(_collect_configmap_file_items(pod, cm_index))
        for container in pod.containers:
            items.extend(_collect_env_items(pod, container, cm_index))
            items.extend(_collect_command_args(pod, container))
            items.extend(_collect_image_env_items(pod, container, image_reader))
    return items