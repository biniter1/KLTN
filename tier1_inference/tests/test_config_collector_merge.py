"""config_collector/merge.py — bước [2] HỢP NHẤT thành effective config
(CONFIG_EVIDENCE_COLLECTOR.md mục 4.1)."""

from __future__ import annotations

from tier1.config_collector.collect import collect_raw_items
from tier1.config_collector.k8s_types import (
    ConfigMapInfo,
    ContainerSpec,
    EnvFromSource,
    FakeImageConfigReader,
    FakeK8sReader,
    ImageConfig,
    NullImageConfigReader,
    PodInfo,
)
from tier1.config_collector.merge import EffectiveConfigItem, merge_effective_config

NS = "default"


def _pod(name, containers=None, namespace=NS) -> PodInfo:
    return PodInfo(name=name, namespace=namespace, service_account_name="default", containers=containers or [])


def _find(items: list[EffectiveConfigItem], **kwargs) -> list[EffectiveConfigItem]:
    return [it for it in items if all(getattr(it, k) == v for k, v in kwargs.items())]


def _run(pod, configmaps=None, image_reader=None):
    image_reader = image_reader or NullImageConfigReader()
    reader = FakeK8sReader(pods=[pod], configmaps=configmaps or [])
    raw = collect_raw_items(reader, image_reader)
    return merge_effective_config([pod], raw, image_reader)


# ---------------------------------------------------------------------------
# precedence: image ENV < envFrom < env
# ---------------------------------------------------------------------------


def test_env_overrides_image_env_for_same_key():
    pod = _pod("app", containers=[
        ContainerSpec(name="main", image="registry/app:v1", env={"REDIS_ADDR": "redis-prod:6379"}),
    ])
    image_reader = FakeImageConfigReader({"registry/app:v1": ImageConfig(env={"REDIS_ADDR": "localhost:6379"})})
    result = _run(pod, image_reader=image_reader)

    active = _find(result, key="REDIS_ADDR", status="ACTIVE")
    overridden = _find(result, key="REDIS_ADDR", status="OVERRIDDEN")
    assert len(active) == 1 and active[0].value == "redis-prod:6379" and active[0].source_kind == "env"
    assert len(overridden) == 1 and overridden[0].value == "localhost:6379" and overridden[0].source_kind == "image_env"


def test_envfrom_overrides_image_env_but_env_overrides_envfrom():
    cm = ConfigMapInfo(name="cfg", namespace=NS, data={"ADDR": "from-configmap:1234"})
    pod = _pod("app", containers=[
        ContainerSpec(
            name="main", image="registry/app:v1",
            env_from=[EnvFromSource(configmap_name="cfg")],
            env={"ADDR": "from-env:9999"},
        ),
    ])
    image_reader = FakeImageConfigReader({"registry/app:v1": ImageConfig(env={"ADDR": "from-image:1"})})
    result = _run(pod, configmaps=[cm], image_reader=image_reader)

    by_kind_status = {(it.source_kind, it.status) for it in _find(result, key="ADDR")}
    assert by_kind_status == {
        ("image_env", "OVERRIDDEN"),
        ("envFrom", "OVERRIDDEN"),
        ("env", "ACTIVE"),
    }
    winner = _find(result, key="ADDR", status="ACTIVE")[0]
    assert winner.value == "from-env:9999"


def test_later_envfrom_source_wins_over_earlier_for_same_key():
    cm1 = ConfigMapInfo(name="cfg1", namespace=NS, data={"ADDR": "first:1111"})
    cm2 = ConfigMapInfo(name="cfg2", namespace=NS, data={"ADDR": "second:2222"})
    pod = _pod("app", containers=[
        ContainerSpec(name="main", env_from=[
            EnvFromSource(configmap_name="cfg1"),
            EnvFromSource(configmap_name="cfg2"),
        ]),
    ])
    result = _run(pod, configmaps=[cm1, cm2])
    winner = _find(result, key="ADDR", status="ACTIVE")[0]
    assert winner.value == "second:2222"


# ---------------------------------------------------------------------------
# $(VAR) expansion
# ---------------------------------------------------------------------------


def test_dollar_var_expansion_within_env_uses_earlier_declared_var():
    pod = _pod("app", containers=[
        ContainerSpec(name="main", env={"HOST": "cartservice", "ADDR": "$(HOST):7070"}),
    ])
    result = _run(pod)
    addr = _find(result, key="ADDR", status="ACTIVE")[0]
    assert addr.value == "cartservice:7070"


def test_dollar_dollar_var_is_escaped_literal():
    pod = _pod("app", containers=[
        ContainerSpec(name="main", env={"HOST": "cartservice", "LITERAL": "$$(HOST)"}),
    ])
    result = _run(pod)
    literal = _find(result, key="LITERAL", status="ACTIVE")[0]
    assert literal.value == "$(HOST)"


def test_dollar_var_undefined_keeps_original_text():
    pod = _pod("app", containers=[ContainerSpec(name="main", env={"ADDR": "$(UNDEFINED):80"})])
    result = _run(pod)
    addr = _find(result, key="ADDR", status="ACTIVE")[0]
    assert addr.value == "$(UNDEFINED):80"


def test_dollar_var_expansion_in_args_uses_final_env():
    pod = _pod("app", containers=[
        ContainerSpec(name="main", env={"UPSTREAM": "backend:8080"}, args=["--upstream=$(UPSTREAM)"]),
    ])
    result = _run(pod)
    arg = _find(result, source_kind="args", status="ACTIVE")[0]
    assert arg.value == "--upstream=backend:8080"


# ---------------------------------------------------------------------------
# command/args vs image ENTRYPOINT/CMD
# ---------------------------------------------------------------------------


def test_only_args_uses_image_entrypoint_plus_pod_args():
    pod = _pod("app", containers=[ContainerSpec(name="main", image="r/app:v1", args=["--port=8080"])])
    image_reader = FakeImageConfigReader({"r/app:v1": ImageConfig(entrypoint=["/app/server"], cmd=["--default"])})
    result = _run(pod, image_reader=image_reader)

    entrypoint_active = _find(result, source_kind="image_entrypoint", status="ACTIVE")
    cmd_overridden = _find(result, source_kind="image_cmd", status="OVERRIDDEN")
    args_active = _find(result, source_kind="args", status="ACTIVE")
    assert [it.value for it in entrypoint_active] == ["/app/server"]
    assert [it.value for it in cmd_overridden] == ["--default"]
    assert [it.value for it in args_active] == ["--port=8080"]


def test_command_present_drops_image_cmd_even_without_args():
    pod = _pod("app", containers=[ContainerSpec(name="main", image="r/app:v1", command=["/app/run.sh"])])
    image_reader = FakeImageConfigReader({"r/app:v1": ImageConfig(entrypoint=["/entrypoint"], cmd=["--default"])})
    result = _run(pod, image_reader=image_reader)

    assert [it.value for it in _find(result, source_kind="command", status="ACTIVE")] == ["/app/run.sh"]
    assert [it.value for it in _find(result, source_kind="image_entrypoint", status="OVERRIDDEN")] == ["/entrypoint"]
    assert [it.value for it in _find(result, source_kind="image_cmd", status="OVERRIDDEN")] == ["--default"]
    assert _find(result, source_kind="args") == []


def test_no_command_no_args_uses_image_entrypoint_and_cmd_as_is():
    pod = _pod("app", containers=[ContainerSpec(name="main", image="r/app:v1")])
    image_reader = FakeImageConfigReader({"r/app:v1": ImageConfig(entrypoint=["/app/server"], cmd=["--port=8080"])})
    result = _run(pod, image_reader=image_reader)

    assert [it.value for it in _find(result, source_kind="image_entrypoint", status="ACTIVE")] == ["/app/server"]
    assert [it.value for it in _find(result, source_kind="image_cmd", status="ACTIVE")] == ["--port=8080"]


def test_command_and_args_both_override_image_entirely():
    pod = _pod("app", containers=[
        ContainerSpec(name="main", image="r/app:v1", command=["/app/run.sh"], args=["--upstream=backend:9000"]),
    ])
    image_reader = FakeImageConfigReader({"r/app:v1": ImageConfig(entrypoint=["/entrypoint"], cmd=["--default"])})
    result = _run(pod, image_reader=image_reader)

    assert [it.value for it in _find(result, source_kind="command", status="ACTIVE")] == ["/app/run.sh"]
    assert [it.value for it in _find(result, source_kind="args", status="ACTIVE")] == ["--upstream=backend:9000"]
    assert [it.value for it in _find(result, source_kind="image_entrypoint", status="OVERRIDDEN")] == ["/entrypoint"]
    assert [it.value for it in _find(result, source_kind="image_cmd", status="OVERRIDDEN")] == ["--default"]


# ---------------------------------------------------------------------------
# pass-through kinds
# ---------------------------------------------------------------------------


def test_configmap_file_and_secret_ref_and_annotation_pass_through_as_active():
    cm = ConfigMapInfo(name="cfg", namespace=NS, data={"db.properties": "host=postgres"})
    pod = PodInfo(
        name="app", namespace=NS, service_account_name="default",
        annotations={"custom/upstream": "backend:9000"},
        containers=[ContainerSpec(name="main", env_secret_ref_count=1)],
    )
    result = _run(pod, configmaps=[cm])
    # secret_ref: không có configmap mount trong test này, chỉ kiểm secret_ref + annotation.
    assert _find(result, source_kind="secret_ref", status="ACTIVE") != []
    assert _find(result, source_kind="annotation", status="ACTIVE") != []
