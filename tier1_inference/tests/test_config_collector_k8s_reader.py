"""KubernetesReader (k8s_types.py) — duyệt workload template (mục 3.1) + đọc env/volume
(mục 3.2). Dùng object model THẬT của thư viện `kubernetes` (V1Deployment, V1Pod...) với
API giả trả sẵn danh sách — không cần cluster."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

k8s = pytest.importorskip("kubernetes.client")

from tier1.config_collector.collect import collect_raw_items  # noqa: E402
from tier1.config_collector.k8s_types import KubernetesReader, NullImageConfigReader  # noqa: E402

NS = "shop"


# ---------------------------------------------------------------------------
# API giả
# ---------------------------------------------------------------------------


class _FakeApi:
    """Mỗi method `list_namespaced_x(ns)` / `list_x_for_all_namespaces()` trả object có
    `.items`, lọc theo namespace như API thật."""

    def __init__(self, **lists):
        self._lists = lists  # tên loại -> list object

    def __getattr__(self, name):
        for kind, objs in self._lists.items():
            if name == f"list_namespaced_{kind}":
                return lambda ns, _o=objs: SimpleNamespace(items=[o for o in _o if o.metadata.namespace == ns])
            if name == f"list_{kind}_for_all_namespaces":
                return lambda _o=objs: SimpleNamespace(items=list(_o))
        if name == "list_namespace":
            return lambda: SimpleNamespace(items=[])
        raise AttributeError(name)


def _meta(name, owner=None, labels=None):
    refs = None
    if owner is not None:
        kind, oname = owner
        refs = [k8s.V1OwnerReference(api_version="v1", kind=kind, name=oname, uid="u", controller=True)]
    return k8s.V1ObjectMeta(name=name, namespace=NS, owner_references=refs, labels=labels)


def _template(app, containers, volumes=None, sa=None, annotations=None):
    return k8s.V1PodTemplateSpec(
        metadata=k8s.V1ObjectMeta(labels={"app": app}, annotations=annotations),
        spec=k8s.V1PodSpec(containers=containers, volumes=volumes, service_account_name=sa),
    )


def _c(name="main", image="busybox:1.36", env=None, env_from=None, command=None, mounts=None):
    return k8s.V1Container(name=name, image=image, env=env, env_from=env_from, command=command, volume_mounts=mounts)


def _reader(deployments=(), statefulsets=(), daemonsets=(), replicasets=(), jobs=(), cronjobs=(),
            pods=(), configmaps=(), endpoints=()):
    apps = _FakeApi(deployment=list(deployments), stateful_set=list(statefulsets),
                    daemon_set=list(daemonsets), replica_set=list(replicasets))
    batch = _FakeApi(job=list(jobs), cron_job=list(cronjobs))
    core = _FakeApi(pod=list(pods), config_map=list(configmaps), endpoints=list(endpoints), service=[])
    return KubernetesReader(core_api=core, apps_api=apps, batch_api=batch, custom_api=object())


def _deployment(name, replicas_ready, containers=None, **kw):
    return k8s.V1Deployment(
        metadata=_meta(name),
        spec=k8s.V1DeploymentSpec(selector=k8s.V1LabelSelector(match_labels={"app": name}),
                                  template=_template(name, containers or [_c()], **kw)),
        status=k8s.V1DeploymentStatus(ready_replicas=replicas_ready),
    )


def _cronjob(name, active=None, containers=None):
    return k8s.V1CronJob(
        metadata=_meta(name),
        spec=k8s.V1CronJobSpec(schedule="0 3 1 * *", job_template=k8s.V1JobTemplateSpec(
            spec=k8s.V1JobSpec(template=_template(name, containers or [_c()])))),
        status=k8s.V1CronJobStatus(active=active),
    )


def _rs(name, owner=None, ready=1):
    return k8s.V1ReplicaSet(
        metadata=_meta(name, owner=owner),
        spec=k8s.V1ReplicaSetSpec(selector=k8s.V1LabelSelector(match_labels={"app": name}),
                                  template=_template(name, [_c()])),
        status=k8s.V1ReplicaSetStatus(replicas=ready, ready_replicas=ready),
    )


def _pod(name, owner=None, phase="Running", image_id=None, labels=None):
    statuses = None
    if image_id:
        statuses = [k8s.V1ContainerStatus(name="main", image="busybox:1.36", image_id=image_id,
                                          ready=True, restart_count=0)]
    return k8s.V1Pod(
        metadata=_meta(name, owner=owner, labels=labels),
        spec=k8s.V1PodSpec(containers=[_c()]),
        status=k8s.V1PodStatus(phase=phase, container_statuses=statuses),
    )


# ---------------------------------------------------------------------------
# mục 3.1 — duyệt workload template
# ---------------------------------------------------------------------------


def test_deployment_with_zero_replicas_is_still_listed_as_template():
    reader = _reader(deployments=[_deployment("legacy-report", replicas_ready=None)])
    pods = reader.list_pods(NS)
    assert [(p.name, p.workload_kind, p.is_running) for p in pods] == [("legacy-report", "Deployment", False)]


def test_cronjob_never_run_is_listed_from_job_template():
    reader = _reader(cronjobs=[_cronjob("monthly-billing")])
    pods = reader.list_pods(NS)
    assert [(p.name, p.workload_kind, p.is_running) for p in pods] == [("monthly-billing", "CronJob", False)]
    assert pods[0].labels == {"app": "monthly-billing"}


def test_cronjob_currently_running_is_running():
    reader = _reader(cronjobs=[_cronjob("monthly-billing", active=[k8s.V1ObjectReference(name="j1")])])
    assert reader.list_pods(NS)[0].is_running is True


def test_all_workload_kinds_listed_once_no_pod_duplicates():
    dep = _deployment("frontend", replicas_ready=2)
    rs_of_dep = _rs("frontend-7d9f", owner=("Deployment", "frontend"))
    rs_old_revision = _rs("frontend-5c4b", owner=("Deployment", "frontend"), ready=0)
    standalone_rs = _rs("old-rs")
    sts = k8s.V1StatefulSet(
        metadata=_meta("redis"),
        spec=k8s.V1StatefulSetSpec(selector=k8s.V1LabelSelector(match_labels={"app": "redis"}),
                                   service_name="redis", template=_template("redis", [_c()])),
        status=k8s.V1StatefulSetStatus(replicas=1, ready_replicas=1),
    )
    ds = k8s.V1DaemonSet(
        metadata=_meta("node-agent"),
        spec=k8s.V1DaemonSetSpec(selector=k8s.V1LabelSelector(match_labels={"app": "node-agent"}),
                                 template=_template("node-agent", [_c()])),
        status=k8s.V1DaemonSetStatus(current_number_scheduled=0, desired_number_scheduled=0,
                                     number_misscheduled=0, number_ready=0),
    )
    cj = _cronjob("monthly-billing")
    job_of_cj = k8s.V1Job(metadata=_meta("monthly-billing-2901", owner=("CronJob", "monthly-billing")),
                          spec=k8s.V1JobSpec(template=_template("monthly-billing", [_c()])),
                          status=k8s.V1JobStatus(active=0))
    standalone_job = k8s.V1Job(metadata=_meta("db-migrate"),
                               spec=k8s.V1JobSpec(template=_template("db-migrate", [_c()])),
                               status=k8s.V1JobStatus(active=1))
    pods = [
        _pod("frontend-7d9f-aaaaa", owner=("ReplicaSet", "frontend-7d9f")),
        _pod("frontend-7d9f-bbbbb", owner=("ReplicaSet", "frontend-7d9f")),
        _pod("redis-0", owner=("StatefulSet", "redis")),
        _pod("db-migrate-xyz", owner=("Job", "db-migrate")),
        _pod("debug-shell", phase="Pending"),                                 # Pod trần
        _pod("operator-managed-1", owner=("Rollout", "canary")),            # controller lạ
    ]
    reader = _reader(deployments=[dep], statefulsets=[sts], daemonsets=[ds],
                     replicasets=[rs_of_dep, rs_old_revision, standalone_rs],
                     jobs=[job_of_cj, standalone_job], cronjobs=[cj], pods=pods)

    got = {(p.name, p.workload_kind, p.is_running) for p in reader.list_pods(NS)}
    assert got == {
        ("frontend", "Deployment", True),          # 2 pod + 2 ReplicaSet -> đúng 1 template
        ("old-rs", "ReplicaSet", True),
        ("redis", "StatefulSet", True),
        ("node-agent", "DaemonSet", False),
        ("monthly-billing", "CronJob", False),      # Job con của CronJob không lặp lại
        ("db-migrate", "Job", True),
        ("debug-shell", "Pod", False),
        ("operator-managed-1", "Pod", True),        # không bỏ sót pod do CRD quản
    }


def test_name_collision_between_kinds_is_disambiguated():
    reader = _reader(deployments=[_deployment("worker", 1)], cronjobs=[_cronjob("worker")])
    assert sorted(p.name for p in reader.list_pods(NS)) == ["worker", "worker-cronjob"]


def test_running_image_digest_attached_to_template_container():
    dep = _deployment("frontend", 1)
    rs = _rs("frontend-7d9f", owner=("Deployment", "frontend"))
    pod = _pod("frontend-7d9f-aaaaa", owner=("ReplicaSet", "frontend-7d9f"),
               image_id="docker.io/library/busybox@sha256:" + "a" * 64)
    reader = _reader(deployments=[dep], replicasets=[rs], pods=[pod])
    container = reader.list_pods(NS)[0].containers[0]
    assert container.image == "busybox:1.36"
    assert container.image_id == "docker.io/library/busybox@sha256:" + "a" * 64


def test_endpoints_pod_refs_are_mapped_to_workload_names():
    dep = _deployment("legacy-db", 1)
    rs = _rs("legacy-db-6f", owner=("Deployment", "legacy-db"))
    pod = _pod("legacy-db-6f-zzz", owner=("ReplicaSet", "legacy-db-6f"))
    ep = k8s.V1Endpoints(
        metadata=k8s.V1ObjectMeta(name="db-manual", namespace=NS),
        subsets=[k8s.V1EndpointSubset(addresses=[k8s.V1EndpointAddress(
            ip="10.244.1.7", target_ref=k8s.V1ObjectReference(kind="Pod", name="legacy-db-6f-zzz", namespace=NS))])],
    )
    reader = _reader(deployments=[dep], replicasets=[rs], pods=[pod], endpoints=[ep])
    [got] = reader.list_endpoints(NS)
    assert got.pod_refs == [(NS, "legacy-db")]
    assert got.addresses == ["10.244.1.7"]


# ---------------------------------------------------------------------------
# mục 3.2 — env đọc đủ + đúng thứ tự, volume secret/projected
# ---------------------------------------------------------------------------


def _env_container():
    V = k8s.V1EnvVar
    VF = k8s.V1EnvVarSource
    return _c(env=[
        V(name="POD_IP", value_from=VF(field_ref=k8s.V1ObjectFieldSelector(field_path="status.podIP"))),
        V(name="CART_ADDR", value_from=VF(config_map_key_ref=k8s.V1ConfigMapKeySelector(name="addr-cfg", key="cart"))),
        V(name="DB_PASS", value_from=VF(secret_key_ref=k8s.V1SecretKeySelector(name="db", key="pw"))),
        V(name="MEM", value_from=VF(resource_field_ref=k8s.V1ResourceFieldSelector(resource="limits.memory"))),
        V(name="SELF_URL", value="http://$(POD_IP):8080"),
        V(name="EMPTY_ONE"),
    ], env_from=[
        k8s.V1EnvFromSource(config_map_ref=k8s.V1ConfigMapEnvSource(name="shared")),
        k8s.V1EnvFromSource(secret_ref=k8s.V1SecretEnvSource(name="creds")),
    ])


def test_env_entries_read_in_declaration_order_with_all_valuefrom_kinds():
    reader = _reader(deployments=[_deployment("checkout", 1, containers=[_env_container()])])
    c = reader.list_pods(NS)[0].containers[0]
    assert [(e.name, e.kind) for e in c.env_entries] == [
        ("POD_IP", "fieldRef"), ("CART_ADDR", "configMapKeyRef"), ("DB_PASS", "secretKeyRef"),
        ("MEM", "resourceFieldRef"), ("SELF_URL", "value"), ("EMPTY_ONE", "value"),
    ]
    assert c.env_entries[0].field_path == "status.podIP"
    assert (c.env_entries[1].configmap_name, c.env_entries[1].key) == ("addr-cfg", "cart")
    assert c.env_from_secret_refs == ["creds"]
    assert [ef.configmap_name for ef in c.env_from] == ["shared"]
    # field legacy vẫn điền đúng
    assert c.env == {"SELF_URL": "http://$(POD_IP):8080", "EMPTY_ONE": ""}
    assert c.env_secret_ref_count == 1


def test_end_to_end_collect_resolves_configmapkeyref_and_keeps_fieldref_placeholder():
    cm = k8s.V1ConfigMap(metadata=k8s.V1ObjectMeta(name="addr-cfg", namespace=NS), data={"cart": "cartservice:7070"})
    reader = _reader(deployments=[_deployment("checkout", 0, containers=[_env_container()])], configmaps=[cm])
    items = collect_raw_items(reader, NullImageConfigReader(), NS)
    env = {it.key: (it.source_kind, it.value) for it in items if it.source_kind in ("env", "secret_ref")}
    assert env["CART_ADDR"] == ("env", "cartservice:7070")
    assert env["POD_IP"] == ("env", "<fieldRef:status.podIP>")
    assert env["MEM"] == ("env", "<resourceFieldRef:limits.memory>")
    assert env["DB_PASS"] == ("secret_ref", "<redacted>")
    assert env["<envFrom.secretRef#0>"] == ("secret_ref", "<redacted>")
    assert env["EMPTY_ONE"] == ("env", "")


def test_secret_and_projected_volumes():
    vols = [
        k8s.V1Volume(name="tls", secret=k8s.V1SecretVolumeSource(secret_name="tls-cert")),
        k8s.V1Volume(name="bundle", projected=k8s.V1ProjectedVolumeSource(sources=[
            k8s.V1VolumeProjection(config_map=k8s.V1ConfigMapProjection(name="cfg-a")),
            k8s.V1VolumeProjection(config_map=k8s.V1ConfigMapProjection(
                name="cfg-b", items=[k8s.V1KeyToPath(key="db", path="conf/db.properties")])),
            k8s.V1VolumeProjection(secret=k8s.V1SecretProjection(name="api-key")),
            k8s.V1VolumeProjection(service_account_token=k8s.V1ServiceAccountTokenProjection(path="token")),
        ])),
    ]
    mounts = [k8s.V1VolumeMount(name="tls", mount_path="/tls"), k8s.V1VolumeMount(name="bundle", mount_path="/etc/app")]
    cms = [
        k8s.V1ConfigMap(metadata=k8s.V1ObjectMeta(name="cfg-a", namespace=NS), data={"upstream": "backend:9000"}),
        k8s.V1ConfigMap(metadata=k8s.V1ObjectMeta(name="cfg-b", namespace=NS), data={"db": "host=postgres", "unused": "x"}),
    ]
    dep = _deployment("app", 1, containers=[_c(mounts=mounts)], volumes=vols)
    reader = _reader(deployments=[dep], configmaps=cms)
    items = collect_raw_items(reader, NullImageConfigReader(), NS)

    files = {it.key: (it.value, it.source_ref) for it in items if it.source_kind == "configmap_file"}
    assert files == {
        "upstream": ("backend:9000", "configmap:cfg-a/upstream -> /etc/app/upstream"),
        "db": ("host=postgres", "configmap:cfg-b/db -> /etc/app/conf/db.properties"),
    }
    secrets = [it for it in items if it.source_kind == "secret_ref"]
    assert len(secrets) == 2  # tls-cert + api-key, không đọc giá trị
    assert all(it.value == "<redacted>" for it in secrets)


def test_probes_are_read_for_healthcheck_classification():
    """15/10: Collector đọc liveness/readiness/startup probe — Tầng 1 dùng làm tầng 1 của
    HEALTHCHECK (path health THẬT của từng service)."""
    c = k8s.V1Container(
        name="server", image="busybox:1.36",
        readiness_probe=k8s.V1Probe(http_get=k8s.V1HTTPGetAction(path="/_healthz", port=8080)),
        liveness_probe=k8s.V1Probe(grpc=k8s.V1GRPCAction(port=7070, service="")),
    )
    reader = _reader(deployments=[_deployment("frontend", 1, containers=[c])])
    pod = next(p for p in reader.list_pods(NS) if p.name == "frontend")
    probes = {pr.kind: pr for pr in pod.containers[0].probes}
    assert probes["readiness"].http_path == "/_healthz" and probes["readiness"].port == 8080
    assert probes["liveness"].is_grpc and probes["liveness"].port == 7070
