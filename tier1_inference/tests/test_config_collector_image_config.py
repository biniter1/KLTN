"""image_config.py — đọc ENV/ENTRYPOINT/CMD của image (mục 3.2) bằng Registry API v2 /
crictl trong node kind. HTTP và subprocess đều giả lập, không cần mạng/docker."""

from __future__ import annotations

import json

import pytest

from tier1.config_collector import image_config as ic
from tier1.config_collector.k8s_types import NullImageConfigReader

CONFIG_BLOB = {"architecture": "amd64", "os": "linux",
               "config": {"Env": ["PATH=/usr/bin", "REDIS_ADDR=redis-cart:6379"],
                          "Entrypoint": ["/src/server"], "Cmd": ["--port=8080"]}}


# ---------------------------------------------------------------------------
# parse_image_ref
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ref, expected", [
    ("redis", ("registry-1.docker.io", "library/redis", "latest")),
    ("redis:7.2-alpine", ("registry-1.docker.io", "library/redis", "7.2-alpine")),
    ("curlimages/curl:8.10.1", ("registry-1.docker.io", "curlimages/curl", "8.10.1")),
    ("docker.io/library/busybox@sha256:" + "a" * 64, ("registry-1.docker.io", "library/busybox", "sha256:" + "a" * 64)),
    ("us-central1-docker.pkg.dev/google-samples/microservices-demo/frontend:v0.10.1",
     ("us-central1-docker.pkg.dev", "google-samples/microservices-demo/frontend", "v0.10.1")),
    ("localhost:5000/team/app:dev", ("localhost:5000", "team/app", "dev")),
    ("ghcr.io/org/app:1@sha256:" + "d" * 64, ("ghcr.io", "org/app", "sha256:" + "d" * 64)),
])
def test_parse_image_ref(ref, expected):
    r = ic.parse_image_ref(ref)
    assert (r.registry, r.repository, r.reference) == expected


def test_parse_bare_image_id_returns_none():
    assert ic.parse_image_ref("sha256:" + "e" * 64) is None


# ---------------------------------------------------------------------------
# RegistryImageConfigReader — HTTP giả
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """Mô phỏng Docker Hub: 401 + token ẩn danh, multi-arch index, blob redirect sang CDN."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if url.startswith("https://auth.example/token"):
            return 200, {}, json.dumps({"token": "T"}).encode()
        if url.startswith("https://cdn.example/"):
            assert "Authorization" not in headers  # không lộ token sang host khác
            return 200, {}, json.dumps(CONFIG_BLOB).encode()
        if headers.get("Authorization") != "Bearer T":
            return 401, {"www-authenticate": 'Bearer realm="https://auth.example/token",service="registry.docker.io",'
                                             'scope="repository:library/app:pull"'}, b""
        if url.endswith("/manifests/1.0"):
            return 200, {}, json.dumps({"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
                {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
                {"digest": "sha256:att", "platform": {"os": "unknown", "architecture": "unknown"}},
                {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
            ]}).encode()
        if url.endswith("/manifests/sha256:amd"):
            return 200, {}, json.dumps({"config": {"digest": "sha256:cfg"}, "layers": []}).encode()
        if url.endswith("/blobs/sha256:cfg"):
            return 307, {"location": "https://cdn.example/blob?sig=x"}, b""
        return 404, {}, b""


def test_registry_reader_token_index_platform_and_redirect():
    fake = _FakeRegistry()
    reader = ic.RegistryImageConfigReader(arch="amd64", http_get=fake)
    cfg = reader.get_image_config("app:1.0")
    assert cfg.env == {"PATH": "/usr/bin", "REDIS_ADDR": "redis-cart:6379"}
    assert cfg.entrypoint == ["/src/server"] and cfg.cmd == ["--port=8080"]
    n = len(fake.calls)
    assert reader.get_image_config("app:1.0") == cfg  # cache: không gọi lại
    assert len(fake.calls) == n


def test_registry_reader_missing_platform_or_404_returns_none():
    fake = _FakeRegistry()
    assert ic.RegistryImageConfigReader(arch="s390x", http_get=fake).get_image_config("app:1.0") is None
    assert ic.RegistryImageConfigReader(arch="amd64", http_get=fake).get_image_config("app:nope") is None


def test_registry_reader_network_error_is_best_effort():
    def boom(url, headers):
        raise OSError("network unreachable")

    assert ic.RegistryImageConfigReader(http_get=boom).get_image_config("redis:7") is None


# ---------------------------------------------------------------------------
# KindNodeImageConfigReader — subprocess giả
# ---------------------------------------------------------------------------


def test_kind_node_reader_uses_crictl_on_nodes():
    calls = []

    def runner(argv):
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return 0, "kind-control-plane\nkind-worker\n"
        if argv[2] == "kind-control-plane":
            return 1, ""  # image không có trên node này
        return 0, json.dumps({"status": {"id": "sha256:x"}, "info": {"imageSpec": CONFIG_BLOB}})

    reader = ic.KindNodeImageConfigReader(runner=runner)
    cfg = reader.get_image_config("kltn/harness:dev")
    assert cfg.env["REDIS_ADDR"] == "redis-cart:6379"
    assert ["docker", "exec", "kind-worker", "crictl", "inspecti", "-o", "json", "kltn/harness:dev"] in calls


def test_kind_node_reader_no_docker_returns_none():
    reader = ic.KindNodeImageConfigReader(runner=lambda argv: (127, ""))
    assert reader.get_image_config("x:1") is None


# ---------------------------------------------------------------------------
# Chain + lựa chọn mặc định
# ---------------------------------------------------------------------------


class _Static:
    def __init__(self, mapping):
        self.mapping, self.calls = mapping, 0

    def get_image_config(self, ref):
        self.calls += 1
        return self.mapping.get(ref)


def test_chain_falls_through_caches_and_counts():
    a, b = _Static({}), _Static({"x:1": ic.ImageConfig(env={"K": "v"})})
    chain = ic.ChainImageConfigReader([a, b])
    assert chain.get_image_config("x:1").env == {"K": "v"}
    assert chain.get_image_config("x:1").env == {"K": "v"}
    assert chain.get_image_config("y:1") is None
    assert (a.calls, b.calls) == (2, 2)
    assert chain.resolved_refs == {"x:1"} and chain.unresolved_refs == {"y:1"}


@pytest.mark.parametrize("value, expected", [
    ("none", NullImageConfigReader),
    ("auto", ic.ChainImageConfigReader),
    ("registry", ic.ChainImageConfigReader),
    ("kind", ic.ChainImageConfigReader),
])
def test_default_image_reader_from_env(monkeypatch, value, expected):
    monkeypatch.setenv(ic.IMAGE_READER_ENV, value)
    assert isinstance(ic.default_image_reader(), expected)


def test_default_image_reader_is_auto_when_unset(monkeypatch):
    monkeypatch.delenv(ic.IMAGE_READER_ENV, raising=False)
    r = ic.default_image_reader()
    assert isinstance(r, ic.ChainImageConfigReader)
    assert [type(x).__name__ for x in r._readers] == ["KindNodeImageConfigReader", "RegistryImageConfigReader"]


def test_default_image_reader_invalid_value(monkeypatch):
    monkeypatch.setenv(ic.IMAGE_READER_ENV, "skopeo")
    with pytest.raises(ValueError):
        ic.default_image_reader()
