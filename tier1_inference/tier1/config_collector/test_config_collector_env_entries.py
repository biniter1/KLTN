"""collect.py + merge.py với `env_entries` (thứ tự khai báo + valueFrom) — mục 3.2, 4.1."""

from __future__ import annotations

from tier1.config_collector.collect import collect_raw_items
from tier1.config_collector.k8s_types import (
    ConfigMapInfo,
    ContainerSpec,
    EnvEntry,
    EnvFromSource,
    FakeImageConfigReader,
    FakeK8sReader,
    ImageConfig,
    NullImageConfigReader,
    PodInfo,
)
from tier1.config_collector.merge import merge_effective_config

NS = "default"


def _run(containers, configmaps=(), image_reader=None):
    pod = PodInfo(name="app", namespace=NS, service_account_name="default", containers=containers)
    image_reader = image_reader or NullImageConfigReader()
    reader = FakeK8sReader(pods=[pod], configmaps=list(configmaps))
    raw = collect_raw_items(reader, image_reader)
    return raw, merge_effective_config([pod], raw, image_reader)


def _get(items, **kw):
    return [it for it in items if all(getattr(it, k) == v for k, v in kw.items())]


def test_configmapkeyref_replaced_by_real_value_with_provenance():
    cm = ConfigMapInfo(name="addr", namespace=NS, data={"cart": "cartservice:7070"})
    raw, _ = _run([ContainerSpec(name="main", env_entries=[
        EnvEntry(name="CART_ADDR", kind="configMapKeyRef", configmap_name="addr", key="cart"),
    ])], configmaps=[cm])
    [it] = _get(raw, key="CART_ADDR")
    assert (it.source_kind, it.value, it.source_ref) == ("env", "cartservice:7070", "configmap:addr/cart")


def test_configmapkeyref_missing_is_kept_as_empty_not_dropped():
    raw, _ = _run([ContainerSpec(name="main", env_entries=[
        EnvEntry(name="X", kind="configMapKeyRef", configmap_name="nope", key="k", optional=True),
    ])])
    [it] = _get(raw, key="X")
    assert it.value == "" and it.source_ref == "configmap:nope/k (missing)"


def test_fieldref_placeholder_flows_into_later_var_expansion():
    _, eff = _run([ContainerSpec(name="main", env_entries=[
        EnvEntry(name="HOST_IP", kind="fieldRef", field_path="status.hostIP"),
        EnvEntry(name="OTEL_ENDPOINT", value="http://$(HOST_IP):4317"),
    ])])
    assert _get(eff, key="HOST_IP")[0].value == "<fieldRef:status.hostIP>"
    assert _get(eff, key="OTEL_ENDPOINT")[0].value == "http://<fieldRef:status.hostIP>:4317"


def test_valuefrom_value_is_not_expanded_but_literal_is():
    cm = ConfigMapInfo(name="c", namespace=NS, data={"k": "$(A)-raw"})
    _, eff = _run([ContainerSpec(name="main", env_entries=[
        EnvEntry(name="A", value="alpha"),
        EnvEntry(name="FROM_CM", kind="configMapKeyRef", configmap_name="c", key="k"),
        EnvEntry(name="LIT", value="$(A)-lit"),
    ])], configmaps=[cm])
    assert _get(eff, key="FROM_CM")[0].value == "$(A)-raw"  # K8s không expand valueFrom
    assert _get(eff, key="LIT")[0].value == "alpha-lit"


def test_expansion_only_sees_vars_declared_earlier():
    _, eff = _run([ContainerSpec(name="main", env_entries=[
        EnvEntry(name="ADDR", value="$(HOST):80"),
        EnvEntry(name="HOST", value="backend"),
    ])])
    assert _get(eff, key="ADDR")[0].value == "$(HOST):80"


def test_secretkeyref_with_same_name_overrides_envfrom_value():
    cm = ConfigMapInfo(name="shared", namespace=NS, data={"DB_URL": "postgres://db:5432/app"})
    _, eff = _run([ContainerSpec(
        name="main",
        env_from=[EnvFromSource(configmap_name="shared")],
        env_entries=[EnvEntry(name="DB_URL", kind="secretKeyRef")],
    )], configmaps=[cm])
    statuses = {(it.source_kind, it.status) for it in _get(eff, key="DB_URL")}
    assert statuses == {("envFrom", "OVERRIDDEN"), ("secret_ref", "ACTIVE")}


def test_envfrom_secretref_counted_as_secret_ref():
    raw, eff = _run([ContainerSpec(name="main", env_from_secret_refs=["creds", "tokens"])])
    assert len(_get(raw, source_kind="secret_ref")) == 2
    assert len(_get(eff, source_kind="secret_ref", status="ACTIVE")) == 2


def test_legacy_env_dict_ignored_when_env_entries_present():
    raw, _ = _run([ContainerSpec(
        name="main", env={"A": "1"}, env_secret_ref_count=3,
        env_entries=[EnvEntry(name="A", value="1")],
    )])
    assert len(_get(raw, key="A")) == 1
    assert _get(raw, source_kind="secret_ref") == []


def test_image_lookup_prefers_running_digest_then_spec_tag():
    digest = "r/app@sha256:" + "b" * 64
    reader = FakeImageConfigReader({
        digest: ImageConfig(env={"FROM": "digest"}, entrypoint=["/d"]),
        "r/app:v1": ImageConfig(env={"FROM": "tag"}),
    })
    raw, eff = _run([ContainerSpec(name="main", image="r/app:v1", image_id=digest)], image_reader=reader)
    [it] = _get(raw, source_kind="image_env")
    assert it.value == "digest" and it.source_ref == f"image:{digest}"
    assert _get(eff, source_kind="image_entrypoint")[0].source_ref == f"image:{digest}"

    raw2, _ = _run([ContainerSpec(name="main", image="r/app:v1", image_id="sha256:" + "c" * 64)], image_reader=reader)
    assert _get(raw2, source_kind="image_env")[0].value == "tag"  # digest không đọc được -> rơi về tag
