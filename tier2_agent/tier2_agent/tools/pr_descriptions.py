"""`read_pr_descriptions(service)` (TIER2_DESIGN.md mục 2, 2.3).

Agent KHÔNG BAO GIỜ thấy GitHub token — mọi request đi qua gatekeeper của
API Server (`ORCHESTRATION_DESIGN.md` mục 6, route `POST
/agent-tool-calls/*`), nơi giữ token thật và áp RBAC/rate-limit/log.

`GatekeeperClient` là interface tối thiểu; `HttpGatekeeperClient` là cài đặt
thật gọi qua HTTP tới API Server. Cache kết quả theo `service_name`, sống
trong suốt 1 trial (mục 2: "API Server caches PR description results per
service_name for the duration of one trial") — người gọi (Batch
Orchestrator, ngoài phạm vi gói này) chịu trách nhiệm tạo 1
`GatekeeperClient` cho mỗi trial và gọi `reset_cache()` khi bắt đầu trial
mới, tái dùng object đó cho mọi phiên `Brain` (mỗi phiên ứng với 1 cạnh
escalate) trong cùng trial.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Protocol

from ..exceptions import ToolExecutionError
from ..tool_registry import ToolResult
from ._shared import extract_pr_numbers


class GatekeeperClient(Protocol):
    def fetch_pr_descriptions(self, service: str) -> List[dict]:
        """Trả list PR: mỗi phần tử tối thiểu có
        {"number": int, "title": str, "body": str, "changed_files": [str, ...]}.
        """
        ...


class CachingGatekeeperClient:
    """Bọc 1 hàm fetch thật (HTTP hoặc fake) với cache per-service/per-trial."""

    def __init__(self, fetch_fn: Callable[[str], List[dict]]):
        self._fetch_fn = fetch_fn
        self._cache: Dict[str, List[dict]] = {}

    def reset_cache(self) -> None:
        """Gọi khi bắt đầu 1 trial mới (mục 2: cache chỉ sống trong 1 trial)."""

        self._cache = {}

    def fetch_pr_descriptions(self, service: str) -> List[dict]:
        if service not in self._cache:
            self._cache[service] = self._fetch_fn(service)
        return self._cache[service]


def make_http_gatekeeper_fetch(
    api_server_base_url: str,
    *,
    session=None,
    timeout: float = 30.0,
) -> Callable[[str], List[dict]]:
    """Cài đặt thật — gọi `POST {base_url}/agent-tool-calls/read_pr_descriptions`
    (ORCHESTRATION_DESIGN.md mục 6). Import `requests` trễ để module này vẫn
    import được trong test không cần network."""

    def _fetch(service: str) -> List[dict]:
        import requests  # import trễ, chỉ cần khi thật sự gọi HTTP

        http = session or requests
        try:
            resp = http.post(
                f"{api_server_base_url.rstrip('/')}/agent-tool-calls/read_pr_descriptions",
                json={"service": service},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise ToolExecutionError(f"gatekeeper read_pr_descriptions lỗi: {exc}") from exc
        return data.get("pull_requests", [])

    return _fetch


def _service_source_prefix(service: str, service_dirs: Dict[str, str]) -> Optional[str]:
    root = service_dirs.get(service)
    if root is None:
        return None
    return root.rstrip("/")


def make_pr_descriptions_tool(
    gatekeeper: GatekeeperClient,
    service_dirs: Dict[str, str],
) -> Callable[[dict], ToolResult]:
    def _impl(tool_input: dict) -> ToolResult:
        service = tool_input["service"]
        prefix = _service_source_prefix(service, service_dirs)

        try:
            prs = gatekeeper.fetch_pr_descriptions(service)
        except ToolExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolExecutionError(f"read_pr_descriptions lỗi: {exc}") from exc

        # PR-to-service matching: lọc PR có file thay đổi nằm trong thư mục
        # source của service (TIER2_DESIGN.md mục 0)
        if prefix is not None:
            prs = [
                pr
                for pr in prs
                if any(f.startswith(prefix) for f in pr.get("changed_files", []))
            ]

        if not prs:
            return ToolResult(
                raw_output=f"Không có PR nào chạm tới thư mục source của {service!r}."
            )

        lines = []
        numbers = []
        for pr in prs:
            numbers.append(str(pr["number"]))
            lines.append(f"PR #{pr['number']}: {pr.get('title', '')}\n{pr.get('body', '')}")
        return ToolResult(raw_output="\n---\n".join(lines), pr_number=",".join(numbers))

    return _impl
