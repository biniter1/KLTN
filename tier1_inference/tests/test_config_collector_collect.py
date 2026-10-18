"""config_collector/collect.py — bước [1] THU THẬP (CONFIG_EVIDENCE_COLLECTOR.md mục 3,
4.0) — dùng FakeK8sReader/FakeImageConfigReader, không cần cluster thật."""

from __future__ import annotations

from tier1.config_collector.collect import RawConfigItem, collect_raw_items
from tier1.config_collector.k8s_types import (
    ConfigMapInfo,
    ContainerSpec,
    EnvFromSource,
    FakeImageConfigReader,
    FakeK8sReader,
    ImageConfig,
    NullImageConfigReader,
    PodInfo,
    VolumeMount,
    VolumeSource,
)

NS = "default"


def _pod(name, containers=None, annotations=None, volumes=None, namespace=NS) -> PodInfo:
    return PodInfo(
        name=name, namespace=namespace, service_account_name="default",
        containers=containers or [], annotations=annotations or {}, volumes=volumes or [],
    )


def _find(items: list[RawConfigItem], **kwargs) -> list[RawConfigItem]:
    return [
        it for it in items
        if all(getattr(it, k) == v for k, v in kwargs.items())
    ]


# ---------------------------------------------------------------------------
# env / envFrom / secret_ref
# ---------------------------------------------------------------------------


def test_collect_env_literal():
    pod = _pod("checkoutservice", containers=[
        ContainerSpec(name="server", env={"CART_SERVICE_ADDR": "cartservice:7070"}),
    ])
    reader = FakeK8sReader(pods=[pod])
    items = collect_raw_items(reader, NullImageConfigReader())
    matches = _find(items, source_kind="env", key="CART_SERVICE_ADDR")
    assert len(matches) == 1
    assert matches[0].value == "cartservice:7070"
    assert matches[0].container == "server"


def test_collect_env_from_configmap_with_prefix():
    cm = ConfigMapInfo(name="shared-cfg", namespace=NS, data={"HOST": "redis", "PORT": "6379"})
    pod = _pod("worker", containers=[
        ContainerSpec(name="main", env_from=[EnvFromSource(configmap_name="shared-cfg", prefix="REDIS_")]),
    ])
    reader = FakeK8sReader(pods=[pod], configmaps=[cm])
    items = collect_raw_items(reader, NullImageConfigReader())
    matches = _find(items, source_kind="envFrom")
    keys = {it.key: it.value for it in matches}
    assert keys == {"REDIS_HOST": "redis", "REDIS_PORT": "6379"}
    assert matches[0].source_ref.startswith("configmap:shared-cfg/")


def test_collect_env_from_missing_configmap_skipped_safely():
    pod = _pod("worker", containers=[
        ContainerSpec(name="main", env_from=[EnvFromSource(configmap_name="does-not-exist")]),
    ])
    reader = FakeK8sReader(pods=[pod], configmaps=[])
    items = collect_raw_items(reader, NullImageConfigReader())
    assert _find(items, source_kind="envFrom") == []


def test_collect_secret_ref_count_becomes_placeholder_items():
    pod = _pod("db-client", containers=[
        ContainerSpec(name="main", env_secret_ref_count=2),
    ])
    reader = FakeK8sReader(pods=[pod])
    items = collect_raw_items(reader, NullImageConfigReader())
    matches = _find(items, source_kind="secret_ref")
    assert len(matches) == 2
    assert all(it.value == "<redacted>" for it in matches)


# ---------------------------------------------------------------------------
# command / args
# ---------------------------------------------------------------------------


def test_collect_command_and_args_as_indexed_tokens():
    pod = _pod("legacy", containers=[
        ContainerSpec(name="main", command=["sh", "-c", "until nc -z postgres 5432; do sleep 1; done"],
                      args=["--upstream=backend:8080"]),
    ])
    reader = FakeK8sReader(pods=[pod])
    items = collect_raw_items(reader, NullImageConfigReader())
    command_items = _find(items, source_kind="command")
    args_items = _find(items, source_kind="args")
    assert [it.value for it in command_items] == ["sh", "-c", "until nc -z postgres 5432; do sleep 1; done"]
    assert [it.value for it in args_items] == ["--upstream=backend:8080"]


def test_collect_init_container_marked_as_init_type():
    pod = _pod("app", containers=[
        ContainerSpec(name="wait-for-db", container_type="init", command=["sh", "-c", "until nc -z db 5432; do sleep 1; done"]),
        ContainerSpec(name="server", container_type="app"),
    ])
    reader = FakeK8sReader(pods=[pod])
    items = collect_raw_items(reader, NullImageConfigReader())
    init_items = _find(items, container="wait-for-db", source_kind="command")
    assert len(init_items) == 3
    assert all(it.container_type == "init" for it in init_items)


# ---------------------------------------------------------------------------
# ConfigMap file mount
# ---------------------------------------------------------------------------


def test_collect_configmap_file_mount_all_keys():
    cm = ConfigMapInfo(name="app-cfg", namespace=NS, data={"db.properties": "host=postgres\nport=5432\n"})
    pod = _pod("app", containers=[
        ContainerSpec(
            name="main",
            volume_mounts=[VolumeMount(name="cfg", mount_path="/etc/app")],
        ),
    ], volumes=[VolumeSource(name="cfg", configmap_name="app-cfg")])
    reader = FakeK8sReader(pods=[pod], configmaps=[cm])
    items = collect_raw_items(reader, NullImageConfigReader())
    matches = _find(items, source_kind="configmap_file", key="db.properties")
    assert len(matches) == 1
    assert "postgres" in matches[0].value
    assert matches[0].container == "main"


def test_collect_configmap_file_mount_sub_path_single_key():
    cm = ConfigMapInfo(name="app-cfg", namespace=NS, data={"a": "va", "b": "vb"})
    pod = _pod("app", containers=[
        ContainerSpec(
            name="main",
            volume_mounts=[VolumeMount(name="cfg", mount_path="/etc/app/b.conf", sub_path="b")],
        ),
    ], volumes=[VolumeSource(name="cfg", configmap_name="app-cfg")])
    reader = FakeK8sReader(pods=[pod], configmaps=[cm])
    items = collect_raw_items(reader, NullImageConfigReader())
    matches = _find(items, source_kind="configmap_file")
    assert len(matches) == 1
    assert matches[0].key == "b"
    assert matches[0].value == "vb"


def test_collect_configmap_file_binary_key_has_no_text_value():
    cm = ConfigMapInfo(name="bin-cfg", namespace=NS, data={}, binary_keys=frozenset({"cert.bin"}))
    pod = _pod("app", containers=[
        ContainerSpec(name="main", volume_mounts=[VolumeMount(name="cfg", mount_path="/etc/app")]),
    ], volumes=[VolumeSource(name="cfg", configmap_name="bin-cfg")])
    reader = FakeK8sReader(pods=[pod], configmaps=[cm])
    items = collect_raw_items(reader, NullImageConfigReader())
    matches = _find(items, source_kind="configmap_file", key="cert.bin")
    assert len(matches) == 1
    assert matches[0].is_binary is True
    assert matches[0].value == ""


# ---------------------------------------------------------------------------
# image config / annotations
# ---------------------------------------------------------------------------


def test_collect_image_env_items():
    pod = _pod("app", containers=[ContainerSpec(name="main", image="registry/app:v1")])
    reader = FakeK8sReader(pods=[pod])
    image_reader = FakeImageConfigReader({"registry/app:v1": ImageConfig(env={"REDIS_ADDR": "localhost:6379"})})
    items = collect_raw_items(reader, image_reader)
    matches = _find(items, source_kind="image_env", key="REDIS_ADDR")
    assert len(matches) == 1
    assert matches[0].value == "localhost:6379"


def test_collect_image_env_items_no_image_config_available():
    pod = _pod("app", containers=[ContainerSpec(name="main", image="registry/app:v1")])
    reader = FakeK8sReader(pods=[pod])
    items = collect_raw_items(reader, FakeImageConfigReader({}))
    assert _find(items, source_kind="image_env") == []


def test_collect_annotation_items_at_pod_level():
    pod = _pod("app", containers=[ContainerSpec(name="main")], annotations={"custom/upstream": "backend:9000"})
    reader = FakeK8sReader(pods=[pod])
    items = collect_raw_items(reader, NullImageConfigReader())
    matches = _find(items, source_kind="annotation", key="custom/upstream")
    assert len(matches) == 1
    assert matches[0].container == ""
    assert matches[0].container_type == "pod"
