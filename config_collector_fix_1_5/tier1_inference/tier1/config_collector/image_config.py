"""
Đọc "dấu vết Dockerfile" của image (ENV / ENTRYPOINT / CMD) — CONFIG_EVIDENCE_COLLECTOR.md
mục 3.2. KHÔNG pull layer/filesystem, chỉ đọc manifest + blob config (vài KB/image).

3 reader thật + 1 chuỗi kết hợp:

  KindNodeImageConfigReader  `docker exec <node kind> crictl inspecti -o json <ref>` —
                             đọc đúng image ĐÃ CÓ trên node (kể cả image tự build nạp
                             bằng `kind load`, vốn không có trên registry công khai).
                             Không cần mạng.
  RegistryImageConfigReader  Python thuần, gọi thẳng Registry HTTP API v2 (Docker Hub,
                             ghcr.io, gcr.io, *.pkg.dev, quay.io...) với token ẩn danh —
                             dùng cho workload CHƯA có pod nào (replicas=0, CronJob chưa
                             chạy) nên image chưa từng được kéo về node. Manifest list /
                             OCI index -> chọn đúng nền tảng linux/<arch>.
  CraneImageConfigReader     (k8s_types.py) `crane config` — lựa chọn thay thế nếu thích.

  ChainImageConfigReader     thử lần lượt, ref nào đọc được thì dừng; cache theo ref
                             (mục 2: "config service gốc... chỉ gọi 1 lần"); đếm
                             resolved/unresolved để báo cáo (không âm thầm mất).

Chọn reader mặc định qua biến môi trường CONFIG_COLLECTOR_IMAGE_READER (giống cách chọn
LLM provider — đổi không cần sửa code):
  auto (mặc định) = Chain[kind-node, registry] | kind | registry | crane | none

Giới hạn đã biết: chỉ registry cho phép pull ẩn danh (public). Registry private cần
credential -> trả None (không crash), ref đó nằm trong `unresolved_refs` để báo cáo.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from .k8s_types import (
    CraneImageConfigReader,
    ImageConfig,
    ImageConfigReader,
    NullImageConfigReader,
    image_config_from_oci_config,
)

log = logging.getLogger(__name__)

IMAGE_READER_ENV = "CONFIG_COLLECTOR_IMAGE_READER"
DEFAULT_IMAGE_READER = "auto"


# ---------------------------------------------------------------------------
# Phân tích image reference
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageRef:
    registry: str  # host API thật (docker.io -> registry-1.docker.io)
    repository: str  # vd "library/redis", "google-samples/microservices-demo/frontend"
    reference: str  # tag hoặc digest "sha256:..."


_DIGEST_RE = re.compile(r"^[a-z0-9]+(?:[.+_-][a-z0-9]+)*:[0-9a-fA-F]{32,}$")


def parse_image_ref(ref: str) -> Optional[ImageRef]:
    """`redis:7` -> docker.io/library/redis:7; `ghcr.io/a/b@sha256:..`; `localhost:5000/x`.
    Trả None cho ID trần (`sha256:abc...`) — không có repository để gọi registry."""
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.startswith("docker-pullable://"):
        ref = ref[len("docker-pullable://"):]
    if _DIGEST_RE.match(ref):
        return None
    name, digest = (ref.split("@", 1) + [None])[:2] if "@" in ref else (ref, None)
    tag = None
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = name.rsplit(":", 1)
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, repo = parts[0], "/".join(parts[1:])
    else:
        registry, repo = "docker.io", name
    if registry in ("docker.io", "index.docker.io", "registry-1.docker.io"):
        registry = "registry-1.docker.io"
        if "/" not in repo:
            repo = f"library/{repo}"
    if not repo:
        return None
    return ImageRef(registry=registry, repository=repo, reference=digest or tag or "latest")


def _default_arch() -> str:
    env_arch = os.environ.get("CONFIG_COLLECTOR_IMAGE_ARCH")
    if env_arch:
        return env_arch
    machine = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)


# ---------------------------------------------------------------------------
# Registry HTTP API v2
# ---------------------------------------------------------------------------

HttpGet = Callable[[str, dict], tuple[int, dict, bytes]]
"""(url, headers) -> (status, headers lower-case, body). KHÔNG tự follow redirect."""

_MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # tự xử lý redirect để KHÔNG gửi kèm Authorization sang CDN/S3


def _urllib_get(timeout: float) -> HttpGet:
    opener = urllib.request.build_opener(_NoRedirect())

    def get(url: str, headers: dict) -> tuple[int, dict, bytes]:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as e:  # 3xx/4xx/5xx đều về đây khi không follow
            return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""

    return get


class RegistryImageConfigReader:
    """Registry HTTP API v2 + token ẩn danh (Docker Hub, GHCR, GCR, Artifact Registry,
    Quay...). Luôn best-effort: lỗi mạng/401/404 -> None, không crash pipeline."""

    MAX_BLOB_BYTES = 4 * 1024 * 1024  # config blob thực tế vài KB; chặn trên cho an toàn

    def __init__(
        self,
        *,
        os_name: str = "linux",
        arch: Optional[str] = None,
        timeout: float = 20.0,
        http_get: Optional[HttpGet] = None,
    ):
        self._os = os_name
        self._arch = arch or _default_arch()
        self._get = http_get or _urllib_get(timeout)
        self._tokens: dict[tuple[str, str], str] = {}
        self._cache: dict[str, Optional[ImageConfig]] = {}

    def get_image_config(self, image_ref: str) -> Optional[ImageConfig]:
        if image_ref in self._cache:
            return self._cache[image_ref]
        try:
            result = self._fetch(image_ref)
        except Exception as e:  # mạng/JSON hỏng... -> best-effort
            log.warning("image config: registry lỗi với %s: %s", image_ref, e)
            result = None
        self._cache[image_ref] = result
        return result

    # --- HTTP + auth ---

    def _request(self, ref: ImageRef, url: str, accept: Optional[str] = None) -> tuple[int, dict, bytes]:
        headers = {"User-Agent": "kltn-config-collector/1"}
        if accept:
            headers["Accept"] = accept
        token = self._tokens.get((ref.registry, ref.repository))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        status, resp_headers, body = self._get(url, headers)

        if status == 401 and not token:
            token = self._fetch_token(ref, resp_headers.get("www-authenticate", ""))
            if token:
                self._tokens[(ref.registry, ref.repository)] = token
                headers["Authorization"] = f"Bearer {token}"
                status, resp_headers, body = self._get(url, headers)

        hops = 0
        while status in (301, 302, 303, 307, 308) and hops < 5:
            location = resp_headers.get("location")
            if not location:
                break
            location = urllib.parse.urljoin(url, location)
            same_host = urllib.parse.urlparse(location).netloc == urllib.parse.urlparse(url).netloc
            redirect_headers = dict(headers)
            if not same_host:
                redirect_headers.pop("Authorization", None)  # URL ký sẵn của CDN/S3
            url = location
            status, resp_headers, body = self._get(url, redirect_headers)
            hops += 1
        return status, resp_headers, body

    def _fetch_token(self, ref: ImageRef, www_authenticate: str) -> Optional[str]:
        if not www_authenticate.lower().startswith("bearer"):
            return None  # Basic auth = registry private -> ngoài phạm vi (không giữ credential)
        params = dict(re.findall(r'(\w+)="([^"]*)"', www_authenticate))
        realm = params.get("realm")
        if not realm:
            return None
        query = {"scope": params.get("scope") or f"repository:{ref.repository}:pull"}
        if params.get("service"):
            query["service"] = params["service"]
        status, _h, body = self._get(f"{realm}?{urllib.parse.urlencode(query)}", {"User-Agent": "kltn-config-collector/1"})
        if status != 200:
            return None
        data = json.loads(body.decode("utf-8"))
        return data.get("token") or data.get("access_token")

    # --- manifest -> config blob ---

    def _manifest(self, ref: ImageRef, reference: str) -> Optional[dict]:
        url = f"https://{ref.registry}/v2/{ref.repository}/manifests/{reference}"
        status, _h, body = self._request(ref, url, accept=_MANIFEST_ACCEPT)
        if status != 200:
            log.warning("image config: manifest %s/%s:%s -> HTTP %s", ref.registry, ref.repository, reference, status)
            return None
        return json.loads(body.decode("utf-8"))

    def _pick_platform(self, index: dict) -> Optional[str]:
        candidates = []
        for m in index.get("manifests", []) or []:
            plat = m.get("platform") or {}
            if plat.get("os") == self._os and plat.get("architecture") == self._arch:
                candidates.append(m)
        if not candidates:
            return None
        # ưu tiên bản không variant hoặc variant chuẩn (arm64/v8)
        candidates.sort(key=lambda m: (m.get("platform", {}).get("variant") not in (None, "v8"),))
        return candidates[0].get("digest")

    def _fetch(self, image_ref: str) -> Optional[ImageConfig]:
        ref = parse_image_ref(image_ref)
        if ref is None:
            return None
        manifest = self._manifest(ref, ref.reference)
        if manifest is None:
            return None
        if "manifests" in manifest:  # manifest list / OCI index (multi-arch)
            digest = self._pick_platform(manifest)
            if digest is None:
                log.warning("image config: %s không có bản %s/%s", image_ref, self._os, self._arch)
                return None
            manifest = self._manifest(ref, digest)
            if manifest is None:
                return None
        config_digest = (manifest.get("config") or {}).get("digest")
        if not config_digest:
            return None  # schema v1 cũ / artifact không phải image
        url = f"https://{ref.registry}/v2/{ref.repository}/blobs/{config_digest}"
        status, _h, body = self._request(ref, url)
        if status != 200 or len(body) > self.MAX_BLOB_BYTES:
            log.warning("image config: blob %s -> HTTP %s", config_digest, status)
            return None
        return image_config_from_oci_config(json.loads(body.decode("utf-8")))


# ---------------------------------------------------------------------------
# crictl trong node kind
# ---------------------------------------------------------------------------

Runner = Callable[[list[str]], tuple[int, str]]
"""argv -> (returncode, stdout). Mặc định subprocess; test truyền giả."""


def _subprocess_runner(timeout: float) -> Runner:
    def run(argv: list[str]) -> tuple[int, str]:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, proc.stdout

    return run


class KindNodeImageConfigReader:
    """`docker exec <node> crictl inspecti -o json <ref>` trên các node kind (mục 3.2:
    "image tự build nạp bằng `kind load` thì đọc qua container runtime trong node kind").
    Không có `docker` / không có node kind / image chưa có trên node -> None."""

    def __init__(
        self,
        *,
        cluster_name: Optional[str] = None,
        nodes: Optional[list[str]] = None,
        runner: Optional[Runner] = None,
        timeout: float = 20.0,
    ):
        self._cluster_name = cluster_name or os.environ.get("KIND_CLUSTER_NAME")
        self._nodes = nodes
        self._runner = runner
        self._timeout = timeout
        self._cache: dict[str, Optional[ImageConfig]] = {}

    def _run(self, argv: list[str]) -> tuple[int, str]:
        if self._runner is None:
            if shutil.which(argv[0]) is None:
                return 127, ""
            self._runner = _subprocess_runner(self._timeout)
        try:
            return self._runner(argv)
        except Exception as e:
            log.warning("image config: lệnh %s lỗi: %s", argv[:3], e)
            return 1, ""

    def _discover_nodes(self) -> list[str]:
        if self._nodes is not None:
            return self._nodes
        label = "io.x-k8s.kind.cluster" + (f"={self._cluster_name}" if self._cluster_name else "")
        code, out = self._run(["docker", "ps", "--filter", f"label={label}", "--format", "{{.Names}}"])
        self._nodes = [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []
        return self._nodes

    def get_image_config(self, image_ref: str) -> Optional[ImageConfig]:
        if image_ref in self._cache:
            return self._cache[image_ref]
        result = None
        for node in self._discover_nodes():
            code, out = self._run(["docker", "exec", node, "crictl", "inspecti", "-o", "json", image_ref])
            if code != 0 or not out.strip():
                continue
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                continue
            spec = (data.get("info") or {}).get("imageSpec")
            if isinstance(spec, dict):
                result = image_config_from_oci_config(spec)
                if result is not None:
                    break
        self._cache[image_ref] = result
        return result


# ---------------------------------------------------------------------------
# Chuỗi + lựa chọn mặc định
# ---------------------------------------------------------------------------


class ChainImageConfigReader:
    """Thử lần lượt từng reader; cache kết quả theo ref; đếm để báo cáo."""

    def __init__(self, readers: list[ImageConfigReader]):
        self._readers = list(readers)
        self._cache: dict[str, Optional[ImageConfig]] = {}
        self.resolved_refs: set[str] = set()
        self.unresolved_refs: set[str] = set()

    def get_image_config(self, image_ref: str) -> Optional[ImageConfig]:
        if image_ref in self._cache:
            return self._cache[image_ref]
        result = None
        for reader in self._readers:
            result = reader.get_image_config(image_ref)
            if result is not None:
                break
        self._cache[image_ref] = result
        (self.resolved_refs if result is not None else self.unresolved_refs).add(image_ref)
        return result


def default_image_reader() -> ImageConfigReader:
    """Reader mặc định của `collect_config_evidence` khi không truyền `image_reader`."""
    choice = os.environ.get(IMAGE_READER_ENV, DEFAULT_IMAGE_READER).strip().lower() or DEFAULT_IMAGE_READER
    if choice == "none":
        return NullImageConfigReader()
    if choice == "kind":
        return ChainImageConfigReader([KindNodeImageConfigReader()])
    if choice == "registry":
        return ChainImageConfigReader([RegistryImageConfigReader()])
    if choice == "crane":
        return ChainImageConfigReader([CraneImageConfigReader()])
    if choice == "auto":
        return ChainImageConfigReader([KindNodeImageConfigReader(), RegistryImageConfigReader()])
    raise ValueError(
        f"{IMAGE_READER_ENV}='{choice}' không hợp lệ -- phải là 1 trong "
        f"['auto', 'kind', 'registry', 'crane', 'none'] (bỏ trống = 'auto')."
    )
