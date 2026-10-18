"""
Config Evidence Collector v2 — bước [2] HỢP NHẤT thành "effective config"
(CONFIG_EVIDENCE_COLLECTOR.md mục 4.1). Tất định — không gọi LLM, không gọi K8s/Istio
API xác thực (đó là `validate.py` bước 5).

Quy tắc (nguyên văn mục 4.1):

- Biến môi trường: `image ENV` < `envFrom` (nguồn sau thắng nguồn trước khi trùng key)
  < `env` (thắng tất cả).
- Lệnh: `command` ghi đè `ENTRYPOINT` của image (và bỏ luôn `CMD` của image dù có
  `args` hay không). `args` ghi đè `CMD` của image. Chỉ có `args` (không `command`)
  -> dùng `ENTRYPOINT` của image + `args`. Không có gì cả -> `ENTRYPOINT` + `CMD` của
  image chạy nguyên bản.
- Expand `$(VAR)`: CHỈ trong `env.value` literal (dùng đúng các biến "env" đã khai báo
  trước đó trong CÙNG container — không dùng envFrom/image làm ngữ cảnh), và trong
  `command`/`args` (dùng toàn bộ effective env cuối cùng của container, vì command/args
  chạy SAU khi environment đã dựng xong). `$$(VAR)` là escape -> giữ nguyên literal
  `$(VAR)`. Biến không tồn tại -> giữ nguyên chuỗi gốc. Giá trị lấy từ `valueFrom`
  (configMapKeyRef / fieldRef...) KHÔNG bị expand (đúng K8s), nhưng vẫn làm ngữ cảnh
  cho biến khai báo sau — vd `POD_IP` (fieldRef) rồi `ADDR=http://$(POD_IP):8080`.
- KHÔNG xoá giá trị bị ghi đè: vẫn trả về trong kết quả với `status="OVERRIDDEN"`, chỉ
  không gửi cho LLM (funnel.py bước 3 sẽ loại chúng, nhưng vẫn lưu vào
  `config_raw_items` để truy vết/đo funnel loss — mục 6, 9.1).
- `secret_ref` từ `env.valueFrom.secretKeyRef` (key = tên biến thật): tham gia chuỗi
  ghi đè ở tầng `env` (1 biến secretKeyRef cùng tên ghi đè biến envFrom, đúng K8s).
  `secret_ref` khác (envFrom.secretRef, volume secret) và `configmap_file`: không nằm
  trong chuỗi ghi đè env, luôn `status="ACTIVE"`, đi thẳng qua.
- `annotation`: gắn ở cấp Pod (không riêng container), cũng luôn `status="ACTIVE"`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Optional

from .collect import RawConfigItem
from .k8s_types import ContainerSpec, ImageConfig, ImageConfigReader, PodInfo, lookup_image_config

_ENV_TIER = {"image_env": 0, "envFrom": 1, "env": 2}

_VAR_PATTERN = re.compile(r"\$\$\(([A-Za-z_][A-Za-z0-9_]*)\)|\$\(([A-Za-z_][A-Za-z0-9_]*)\)")


@dataclass(frozen=True)
class EffectiveConfigItem:
    """1 item SAU hợp nhất (mục 4.0 bước [2]) — `status="OVERRIDDEN"` nghĩa là bị một
    nguồn ưu tiên cao hơn ghi đè, KHÔNG dùng để trích xuất (funnel.py loại), nhưng vẫn
    giữ lại để truy vết (mục 4.1 "không xóa giá trị bị ghi đè")."""

    workload: str
    namespace: str
    container: str
    container_type: str
    source_kind: str
    key: str
    value: str  # đã qua $(VAR) expansion nếu áp dụng
    status: str  # "ACTIVE" | "OVERRIDDEN"
    source_ref: Optional[str] = None
    is_binary: bool = False


def _expand_vars(value: str, resolved: dict[str, str]) -> str:
    def repl(m: "re.Match[str]") -> str:
        if m.group(1) is not None:
            # $$(VAR) escape -> literal $(VAR), không tra cứu giá trị.
            return f"$({m.group(1)})"
        name = m.group(2)
        return resolved.get(name, f"$({name})")

    return _VAR_PATTERN.sub(repl, value)


def _to_effective(item: RawConfigItem, status: str) -> EffectiveConfigItem:
    return EffectiveConfigItem(
        workload=item.workload, namespace=item.namespace, container=item.container,
        container_type=item.container_type, source_kind=item.source_kind, key=item.key,
        value=item.value, status=status, source_ref=item.source_ref, is_binary=item.is_binary,
    )


def _is_env_secret(it: RawConfigItem) -> bool:
    """secret_ref sinh từ env.valueFrom.secretKeyRef với key = tên biến thật (collect.py
    đường env_entries). Placeholder legacy "<secretKeyRef#i>" / envFrom / volume không tính."""
    return it.source_kind == "secret_ref" and it.source_ref == "secret" and not it.key.startswith("<")


def _env_tier(it: RawConfigItem) -> int:
    return _ENV_TIER["env"] if _is_env_secret(it) else _ENV_TIER[it.source_kind]


def _merge_env(own_items: list[RawConfigItem]) -> tuple[list[EffectiveConfigItem], dict[str, str]]:
    """Hợp nhất `image_env`/`envFrom`/`env` của 1 container. Trả về danh sách effective
    item (ACTIVE + OVERRIDDEN) và map tên-biến -> giá trị effective cuối cùng (chỉ các
    biến ACTIVE), dùng làm ngữ cảnh expand `$(VAR)` cho command/args."""
    env_items = [it for it in own_items if it.source_kind in _ENV_TIER or _is_env_secret(it)]

    # Expand $(VAR) cho tier "env" theo đúng thứ tự khai báo, chỉ dùng các biến "env"
    # đã resolve trước đó trong cùng container (mục 4.1).
    resolved_env_tier: dict[str, str] = {}
    expanded_items: list[RawConfigItem] = []
    for it in env_items:
        if it.source_kind == "env":
            # chỉ literal `value:` (source_ref None) mới bị expand; valueFrom giữ nguyên
            value = _expand_vars(it.value, resolved_env_tier) if it.source_ref is None else it.value
            resolved_env_tier[it.key] = value
            expanded_items.append(replace(it, value=value))
        else:
            expanded_items.append(it)

    groups: dict[str, list[tuple[int, int, RawConfigItem]]] = {}
    for idx, it in enumerate(expanded_items):
        groups.setdefault(it.key, []).append((_env_tier(it), idx, it))

    result: list[EffectiveConfigItem] = []
    final_env: dict[str, str] = {}
    for key, candidates in groups.items():
        candidates.sort(key=lambda c: (c[0], c[1]))
        winner = candidates[-1][2]
        for _tier, _idx, it in candidates:
            status = "ACTIVE" if it is winner else "OVERRIDDEN"
            result.append(_to_effective(it, status))
        if winner.source_kind != "secret_ref":
            final_env[key] = winner.value

    return result, final_env


def _materialize_image_tokens(
    pod: PodInfo, container: ContainerSpec, tokens: list[str], source_kind: str, status: str,
    image_ref: Optional[str],
) -> list[EffectiveConfigItem]:
    return [
        EffectiveConfigItem(
            workload=pod.name, namespace=pod.namespace, container=container.name,
            container_type=container.container_type, source_kind=source_kind,
            key=f"[{i}]", value=token, status=status, source_ref=f"image:{image_ref}",
        )
        for i, token in enumerate(tokens)
    ]


def _merge_command_args(
    pod: PodInfo,
    container: ContainerSpec,
    own_items: list[RawConfigItem],
    image_config: Optional[ImageConfig],
    final_env: dict[str, str],
    image_ref: Optional[str] = None,
) -> list[EffectiveConfigItem]:
    image_ref = image_ref or container.image
    result: list[EffectiveConfigItem] = []
    command_items = [it for it in own_items if it.source_kind == "command"]
    args_items = [it for it in own_items if it.source_kind == "args"]
    has_command = bool(command_items)
    has_args = bool(args_items)

    # ENTRYPOINT: command của Pod ghi đè nếu có; nếu không, ENTRYPOINT của image chạy
    # nguyên bản (materialize để LLM vẫn thấy, vì nó thật sự được thực thi).
    if has_command:
        for it in command_items:
            result.append(_to_effective(replace(it, value=_expand_vars(it.value, final_env)), "ACTIVE"))
        if image_config and image_config.entrypoint:
            result.extend(_materialize_image_tokens(
                pod, container, image_config.entrypoint, "image_entrypoint", "OVERRIDDEN", image_ref,
            ))
    elif image_config and image_config.entrypoint:
        result.extend(_materialize_image_tokens(
            pod, container, image_config.entrypoint, "image_entrypoint", "ACTIVE", image_ref,
        ))

    # CMD: args của Pod ghi đè nếu có. Có command (dù có args hay không) luôn bỏ CMD
    # của image (đúng mục 4.1). Không command, không args -> CMD của image chạy làm
    # args hiệu lực.
    if has_args:
        for it in args_items:
            result.append(_to_effective(replace(it, value=_expand_vars(it.value, final_env)), "ACTIVE"))
        if image_config and image_config.cmd:
            result.extend(_materialize_image_tokens(
                pod, container, image_config.cmd, "image_cmd", "OVERRIDDEN", image_ref,
            ))
    elif has_command:
        if image_config and image_config.cmd:
            result.extend(_materialize_image_tokens(
                pod, container, image_config.cmd, "image_cmd", "OVERRIDDEN", image_ref,
            ))
    else:
        if image_config and image_config.cmd:
            result.extend(_materialize_image_tokens(
                pod, container, image_config.cmd, "image_cmd", "ACTIVE", image_ref,
            ))

    return result


def merge_effective_config(
    pods: list[PodInfo],
    raw_items: list[RawConfigItem],
    image_reader: ImageConfigReader,
) -> list[EffectiveConfigItem]:
    """Bước [2] HỢP NHẤT (mục 4.1). Nhận `raw_items` từ `collect.py` cộng với `pods`/
    `image_reader` (cần truy cập trực tiếp `ImageConfig.entrypoint`/`cmd`, vốn KHÔNG có
    raw item riêng — xem docstring `collect.py`)."""
    by_container: dict[tuple[str, str, str], list[RawConfigItem]] = {}
    pod_level: dict[tuple[str, str], list[RawConfigItem]] = {}
    for it in raw_items:
        if it.container == "":
            pod_level.setdefault((it.namespace, it.workload), []).append(it)
        else:
            by_container.setdefault((it.namespace, it.workload, it.container), []).append(it)

    result: list[EffectiveConfigItem] = []
    for pod in pods:
        for container in pod.containers:
            own_items = by_container.get((pod.namespace, pod.name, container.name), [])

            env_items, final_env = _merge_env(own_items)
            result.extend(env_items)

            image_config, image_ref = lookup_image_config(image_reader, container)
            result.extend(_merge_command_args(pod, container, own_items, image_config, final_env, image_ref))

            # configmap_file/secret_ref (không phải secretKeyRef của env): không tham
            # gia chuỗi ghi đè env, đi thẳng qua.
            result.extend(
                _to_effective(it, "ACTIVE")
                for it in own_items
                if it.source_kind in ("configmap_file", "secret_ref") and not _is_env_secret(it)
            )

        for it in pod_level.get((pod.namespace, pod.name), []):
            result.append(_to_effective(it, "ACTIVE"))

    return result