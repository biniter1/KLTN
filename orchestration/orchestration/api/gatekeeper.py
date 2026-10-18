"""Gatekeeper cho tool call của Tier 2 (ORCHESTRATION_DESIGN.md mục 6-7, TIER2_DESIGN.md mục 2).

Đây là ranh giới tin cậy THẬT của hệ thống: Tier 2 là LLM có quyền gọi tool, đọc bằng
chứng phi cấu trúc, có nguy cơ evidence injection -> mọi lời gọi ra ngoài (GitHub) đi qua
đây, nơi:
  - giữ token thật (`GITHUB_TOKEN` trong env của API Server — Agent không bao giờ thấy);
  - allowlist tool (hiện chỉ `read_pr_descriptions`) + allowlist service (whitelist chặt);
  - input schema chặt (đúng 1 key `service`, không nhận field lạ);
  - rate-limit cửa sổ trượt theo tool;
  - ghi `agent_tool_call_log` cho MỌI lời gọi, kể cả bị từ chối/bị rate-limit.
Module này là nơi DUY NHẤT ghi `agent_tool_call_log`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import deque
from typing import Callable, Optional, Protocol

from ..run_log import utc_now_iso

ALLOWED_TOOLS = frozenset({"read_pr_descriptions"})
PR_BODY_CHAR_CAP = 4000


class PRSource(Protocol):
    def list_pull_requests(self, service: str) -> list[dict]:
        """[{"number": int, "title": str, "body": str, "changed_files": [str, ...]}, ...]"""
        ...


class GitHubPRSource:  # pragma: no cover - gọi GitHub API thật
    """Đọc PR từ GitHub REST API. Lọc sơ bộ phía server theo thư mục service trong repo
    (`service_path_template`), Tier 2 lọc lại lần nữa phía tool (`pr_descriptions.py`).
    Danh sách file của 1 PR đã merge không đổi -> cache theo số PR trong tiến trình."""

    def __init__(self, repo: str, token: Optional[str] = None, *, max_prs: int = 30,
                 service_path_template: str = "src/{service}/", timeout: float = 20.0, session=None):
        self._repo = repo
        self._token = token
        self._max_prs = max_prs
        self._template = service_path_template
        self._timeout = timeout
        self._session = session
        self._files_cache: dict[int, list[str]] = {}

    def _get(self, url: str, params=None):
        import requests

        http = self._session or requests
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        resp = http.get(url, headers=headers, params=params, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def list_pull_requests(self, service: str) -> list[dict]:
        base = f"https://api.github.com/repos/{self._repo}"
        prs = self._get(f"{base}/pulls", {"state": "closed", "sort": "updated", "direction": "desc",
                                          "per_page": self._max_prs})
        prefix = self._template.format(service=service)
        out = []
        for pr in prs:
            number = pr["number"]
            if number not in self._files_cache:
                files = self._get(f"{base}/pulls/{number}/files", {"per_page": 100})
                self._files_cache[number] = [f["filename"] for f in files]
            changed = self._files_cache[number]
            if any(f.startswith(prefix) for f in changed):
                out.append({"number": number, "title": pr.get("title") or "", "body": pr.get("body") or "",
                            "changed_files": changed})
        return out


class SlidingWindowRateLimiter:
    def __init__(self, max_calls: int, window_s: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self._max = max_calls
        self._window = window_s
        self._clock = clock
        self._calls: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            q = self._calls.setdefault(key, deque())
            while q and now - q[0] >= self._window:
                q.popleft()
            if len(q) >= self._max:
                return False
            q.append(now)
            return True


class GatekeeperRejection(Exception):
    def __init__(self, http_status: int, status: str, reason: str):
        super().__init__(reason)
        self.http_status = http_status
        self.status = status
        self.reason = reason


def log_tool_call(conn: sqlite3.Connection, *, tool_name: str, request: object, caller: Optional[str],
                  status: str, reason: Optional[str], result_count: Optional[int], latency_ms: float) -> None:
    conn.execute(
        "INSERT INTO agent_tool_call_log (tool_name, request_json, caller, status, reason, result_count, "
        "latency_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tool_name, json.dumps(request, ensure_ascii=False, default=str)[:4000], caller, status, reason,
         result_count, round(latency_ms, 2), utc_now_iso()),
    )


class Gatekeeper:
    def __init__(self, pr_source: PRSource, allowed_services: frozenset, rate_limiter: SlidingWindowRateLimiter):
        self._pr_source = pr_source
        self._allowed_services = allowed_services
        self._limiter = rate_limiter

    def handle(self, conn: sqlite3.Connection, tool_name: str, request: object, caller: Optional[str]) -> dict:
        start = time.monotonic()
        try:
            result = self._handle(tool_name, request)
        except GatekeeperRejection as rej:
            log_tool_call(conn, tool_name=tool_name, request=request, caller=caller, status=rej.status,
                          reason=rej.reason, result_count=None, latency_ms=(time.monotonic() - start) * 1000)
            raise
        log_tool_call(conn, tool_name=tool_name, request=request, caller=caller, status="OK", reason=None,
                      result_count=len(result["pull_requests"]), latency_ms=(time.monotonic() - start) * 1000)
        return result

    def _handle(self, tool_name: str, request: object) -> dict:
        if tool_name not in ALLOWED_TOOLS:
            raise GatekeeperRejection(403, "REJECTED", f"tool {tool_name!r} không nằm trong allowlist gatekeeper")
        if not isinstance(request, dict) or set(request) != {"service"} or not isinstance(request["service"], str):
            raise GatekeeperRejection(400, "REJECTED", "body phải đúng dạng {\"service\": \"<canonical_service>\"}")
        service = request["service"]
        if service not in self._allowed_services:
            raise GatekeeperRejection(403, "REJECTED", f"service {service!r} không nằm trong whitelist")
        if not self._limiter.allow(tool_name):
            raise GatekeeperRejection(429, "RATE_LIMITED", "vượt rate-limit của gatekeeper")
        try:
            prs = self._pr_source.list_pull_requests(service)
        except Exception as exc:  # noqa: BLE001 — lỗi nguồn ngoài -> ERROR có log, trả 502
            raise GatekeeperRejection(502, "ERROR", f"{type(exc).__name__}: {exc}") from exc
        cleaned = []
        for pr in prs:
            cleaned.append({
                "number": int(pr["number"]),
                "title": str(pr.get("title", ""))[:300],
                "body": str(pr.get("body", ""))[:PR_BODY_CHAR_CAP],
                "changed_files": [str(f) for f in pr.get("changed_files", [])][:300],
            })
        return {"pull_requests": cleaned}
