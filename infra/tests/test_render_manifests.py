"""render_manifests.py — sinh YAML K8s cho 1 trial từ TrialScenario (scenario_pool.py).

Vì sandbox không có cluster thật, các test này CHỈ kiểm tra tính đúng đắn có thể xác
minh tĩnh (structural): YAML parse được, tên tài nguyên hợp lệ RFC 1123, số lượng
Deployment đúng, nhãn/identity đúng, và (quan trọng nhất) cạnh NOISE_HEALTHCHECK thực sự
sinh ra 1 lệnh HTTP mang đúng path health-check — KHÔNG kiểm chứng được hành vi thật trên
Envoy/Prometheus (xem docstring module).
"""

import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scenarios"))

from render_manifests import _k8s_safe_name, _validate_k8s_name, render, render_shared  # noqa: E402
from scenario_pool import ScenarioKind, generate_shared_trials, generate_trial  # noqa: E402

_RFC1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _docs(namespace="trial-01", seed=1):
    scenario = generate_trial(namespace, seed=seed)
    text = render(scenario, namespace=namespace)
    return scenario, list(yaml.safe_load_all(text))


def test_render_produces_valid_yaml_documents():
    _, docs = _docs()
    # 1 Namespace + 1 ConfigMap + 6 Deployment (1 mỗi ScenarioKind)
    assert len(docs) == 8
    kinds = [d["kind"] for d in docs]
    assert kinds[0] == "Namespace"
    assert kinds[1] == "ConfigMap"
    assert kinds[2:] == ["Deployment"] * 6


def test_every_resource_name_is_rfc1123_valid():
    _, docs = _docs()
    for d in docs:
        name = d["metadata"]["name"]
        assert _RFC1123.match(name), name


def test_render_rejects_namespace_with_underscore():
    scenario = generate_trial("trial_01", seed=1)
    with pytest.raises(ValueError, match="RFC 1123"):
        render(scenario, namespace="trial_01")


def test_k8s_safe_name_truncates_and_strips_trailing_hyphen():
    # Chuỗi được cắt ở đúng vị trí ngay sau '-' -> phải bỏ '-' thừa ở cuối.
    long_name = "a" * 60 + "-" + "b" * 10
    safe = _k8s_safe_name(long_name, max_len=61)
    assert len(safe) <= 61
    assert not safe.endswith("-")


def test_deployment_names_are_unique_within_a_trial():
    _, docs = _docs()
    deployment_names = [d["metadata"]["name"] for d in docs if d["kind"] == "Deployment"]
    assert len(deployment_names) == len(set(deployment_names))


def _deployment_by_kind(docs, kind: ScenarioKind):
    return next(
        d for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["labels"]["kltn/scenario-kind"] == kind.value
    )


def test_each_deployment_impersonates_its_declared_source():
    scenario, docs = _docs()
    deployments = {d["metadata"]["labels"]["kltn/scenario-kind"]: d for d in docs if d["kind"] == "Deployment"}
    for edge in scenario.injected_edges:
        dep = deployments[edge.kind.value]
        pod_labels = dep["spec"]["template"]["metadata"]["labels"]
        assert pod_labels["service.istio.io/canonical-name"] == edge.source
        assert pod_labels["test-harness"] == "true"
        assert pod_labels["injection-id"] == scenario.trial_id


def test_noise_healthcheck_harness_sends_a_real_http_healthcheck_path():
    """Bug đã sửa: trước đây destination của NOISE_HEALTHCHECK có thể là 1 service
    GRPC/TCP, khiến lệnh sinh ra (grpcurl/nc) hoàn toàn bỏ qua `edge.path` -- traffic
    thật sự không mang path health-check nào, nên classification.py (chỉ nhận diện
    HEALTHCHECK qua `series.path`) sẽ không bao giờ gắn đúng nhãn. Cố định destination
    = "frontend" (HTTP) để lệnh thật sự là 1 curl mang đúng path."""
    scenario, docs = _docs()
    dep = _deployment_by_kind(docs, ScenarioKind.NOISE_HEALTHCHECK)
    env = {e["name"]: e["value"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    hc_edge = next(e for e in scenario.injected_edges if e.kind == ScenarioKind.NOISE_HEALTHCHECK)
    assert env["CALL_CMD"].startswith("curl ")
    assert hc_edge.path in env["CALL_CMD"]
    assert "frontend" in env["CALL_CMD"]


def test_traffic_generating_images_are_shell_capable():
    """Bug đã sửa (CHẠY THẬT trên máy Bin, 2026-09-27, trial-01): "fullstorydev/grpcurl:latest"
    (Docker Hub) build FROM scratch (xác nhận qua Dockerfile chính thức), CHỈ có
    /bin/grpcurl + ca-certificates -- KHÔNG có /bin/sh. wrapper.sh (vòng lặp lịch chạy theo
    ACTIVE_DAYS, xem WRAPPER_SCRIPT) cần "sh" để chạy -> pod nào cấy vào dest GRPC bị
    CrashLoopBackOff/RunContainerError ngay khi start (đúng 3/6 harness pod của trial-01 bị
    lỗi này khi Bin tự chạy). Đổi sang "ghcr.io/fullstorydev/grpcurl:latest-alpine" (build
    từ alpine:3, có sẵn sh + grpcurl trên PATH -- xác nhận qua chính workflow release CI của
    dự án grpcurl, .github/workflows/release.yml, publish CẢ 2 target "scratch"/"alpine")."""
    _, docs = _docs()
    for dep in docs:
        if dep.get("kind") != "Deployment":
            continue
        container = dep["spec"]["template"]["spec"]["containers"][0]
        if container["command"] == ["sh", "-c", "sleep infinity"]:
            continue  # CONFIG_ONLY_* -- không sinh traffic, không cần image có sh
        image = container["image"]
        assert image != "fullstorydev/grpcurl:latest", (
            f"{dep['metadata']['name']}: dùng ảnh scratch không có shell -- wrapper.sh sẽ CrashLoopBackOff"
        )
        call_cmd = next((e["value"] for e in container["env"] if e["name"] == "CALL_CMD"), "")
        if "grpcurl" in call_cmd:
            assert image == "ghcr.io/fullstorydev/grpcurl:latest-alpine"


def test_config_only_edges_have_no_traffic_generation_command():
    _, docs = _docs()
    for kind in (ScenarioKind.CONFIG_ONLY_CLEAN, ScenarioKind.CONFIG_ONLY_UNRESOLVED):
        dep = _deployment_by_kind(docs, kind)
        container = dep["spec"]["template"]["spec"]["containers"][0]
        assert container["command"] == ["sh", "-c", "sleep infinity"]
        # Không có traffic thật -> không cần mount wrapper script.
        assert "volumeMounts" not in container
        assert "volumes" not in dep["spec"]["template"]["spec"]


def test_sparse_hidden_active_days_env_matches_scenario():
    scenario, docs = _docs()
    dep = _deployment_by_kind(docs, ScenarioKind.SPARSE_HIDDEN)
    env = {e["name"]: e["value"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    edge = next(e for e in scenario.injected_edges if e.kind == ScenarioKind.SPARSE_HIDDEN)
    expected_days = ",".join(str(d) for d in range(edge.active_days_out_of_window))
    assert env["ACTIVE_DAYS"] == expected_days


def test_namespace_has_ambient_dataplane_label_not_sidecar_injection():
    """Đổi từ sidecar sang ambient (ztunnel + waypoint) — xem lý do ở
    scripts/03_install_istio.sh. Namespace phải mang nhãn `istio.io/dataplane-mode:
    ambient`, KHÔNG còn nhãn `istio-injection: enabled` cũ (nhãn cũ vô hại nhưng không
    còn ý nghĩa gì với ambient, giữ lại dễ gây hiểu lầm là còn dùng sidecar)."""
    _, docs = _docs()
    ns = docs[0]
    assert ns["kind"] == "Namespace"
    assert ns["metadata"]["labels"]["istio.io/dataplane-mode"] == "ambient"
    assert "istio-injection" not in ns["metadata"]["labels"]


def test_render_is_deterministic_for_same_scenario():
    scenario = generate_trial("trial-07", seed=7)
    text_a = render(scenario, namespace="trial-07")
    text_b = render(scenario, namespace="trial-07")
    assert text_a == text_b


# ---------------------------------------------------------------------------
# render_shared() — kiến trúc "dùng chung hạ tầng" (05_deploy_shared.sh)
# ---------------------------------------------------------------------------


def _shared_docs(n_trials=15, seed=1, namespace="shared-eval"):
    scenario = generate_shared_trials(n_trials=n_trials, base_seed=seed, namespace=namespace)
    text = render_shared(scenario, service_account_hint="{source}")
    return scenario, list(yaml.safe_load_all(text))


def test_render_shared_produces_1_namespace_1_configmap_n_deployments():
    scenario, docs = _shared_docs()
    kinds = [d["kind"] for d in docs]
    assert kinds[0] == "Namespace"
    assert kinds[1] == "ConfigMap"
    assert kinds[2:] == ["Deployment"] * len(scenario.shared_edges)
    assert len(scenario.shared_edges) < 6 * 15  # có chia sẻ (NOISE_HEALTHCHECK/MONITORING)


def test_render_shared_namespace_is_ambient_and_marked_shared():
    _, docs = _shared_docs()
    ns = docs[0]
    assert ns["kind"] == "Namespace"
    assert ns["metadata"]["labels"]["istio.io/dataplane-mode"] == "ambient"
    assert ns["metadata"]["labels"]["kltn/shared-infra"] == "true"


def test_render_shared_every_deployment_name_is_rfc1123_and_unique():
    _, docs = _shared_docs()
    names = [d["metadata"]["name"] for d in docs if d["kind"] == "Deployment"]
    assert len(names) == len(set(names))
    for name in names:
        assert _RFC1123.match(name), name


def test_render_shared_deployment_count_matches_shared_edges_not_6_times_n_trials():
    """Bằng chứng trực tiếp cho cơ chế khử trùng lặp: số Deployment THẬT phải khớp
    đúng `len(scenario.shared_edges)`, không phải `6 * n_trials` (một số cạnh
    NOISE_HEALTHCHECK/NOISE_MONITORING được nhiều virtual trial dùng chung)."""
    scenario, docs = _shared_docs(n_trials=15, seed=1)
    n_deployments = sum(1 for d in docs if d["kind"] == "Deployment")
    assert n_deployments == len(scenario.shared_edges)
    assert n_deployments < 90


def test_render_shared_rejects_invalid_namespace():
    scenario = generate_shared_trials(n_trials=3, base_seed=1, namespace="shared_eval")
    with pytest.raises(ValueError, match="RFC 1123"):
        render_shared(scenario)


def test_render_shared_is_deterministic_for_same_scenario():
    scenario = generate_shared_trials(n_trials=15, base_seed=1)
    text_a = render_shared(scenario)
    text_b = render_shared(scenario)
    assert text_a == text_b
