import pytest

from tier2_agent.exceptions import ToolExecutionError
from tier2_agent.tools.pr_descriptions import (
    CachingGatekeeperClient,
    make_http_gatekeeper_fetch,
    make_pr_descriptions_tool,
)


def test_filters_prs_by_service_source_prefix(source_tree):
    pr_map = {
        "frontend": [
            {
                "number": 42,
                "title": "Add payment call",
                "body": "gọi payment mỗi tuần",
                "changed_files": [f"{source_tree['frontend']}/main.py"],
            },
            {
                "number": 7,
                "title": "Unrelated PR",
                "body": "không liên quan",
                "changed_files": [f"{source_tree['payment']}/main.py"],
            },
        ]
    }
    gatekeeper = CachingGatekeeperClient(lambda service: pr_map.get(service, []))
    tool = make_pr_descriptions_tool(gatekeeper, source_tree)
    result = tool({"service": "frontend"})
    assert "PR #42" in result.raw_output
    assert "PR #7" not in result.raw_output
    assert result.pr_number == "42"


def test_no_matching_pr_returns_friendly_message(source_tree):
    gatekeeper = CachingGatekeeperClient(lambda service: [])
    tool = make_pr_descriptions_tool(gatekeeper, source_tree)
    result = tool({"service": "frontend"})
    assert "Không có PR" in result.raw_output
    assert result.pr_number is None


def test_caching_gatekeeper_fetches_once_per_service():
    calls = []

    def _fetch(service):
        calls.append(service)
        return []

    client = CachingGatekeeperClient(_fetch)
    client.fetch_pr_descriptions("frontend")
    client.fetch_pr_descriptions("frontend")
    client.fetch_pr_descriptions("payment")
    assert calls == ["frontend", "payment"]


def test_reset_cache_clears_between_trials():
    calls = []

    def _fetch(service):
        calls.append(service)
        return []

    client = CachingGatekeeperClient(_fetch)
    client.fetch_pr_descriptions("frontend")
    client.reset_cache()
    client.fetch_pr_descriptions("frontend")
    assert calls == ["frontend", "frontend"]


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload=None, exc=None):
        self._payload = payload
        self._exc = exc
        self.last_call = None

    def post(self, url, json, timeout):
        self.last_call = (url, json, timeout)
        if self._exc:
            raise self._exc
        return _FakeResponse(self._payload)


def test_http_gatekeeper_fetch_calls_expected_route():
    session = _FakeSession(payload={"pull_requests": [{"number": 1, "title": "t", "body": "b", "changed_files": []}]})
    fetch = make_http_gatekeeper_fetch("http://api-server.local", session=session)
    result = fetch("frontend")
    assert result[0]["number"] == 1
    url, payload, _timeout = session.last_call
    assert url == "http://api-server.local/agent-tool-calls/read_pr_descriptions"
    assert payload == {"service": "frontend"}


def test_http_gatekeeper_fetch_wraps_errors():
    session = _FakeSession(exc=ConnectionError("no route to host"))
    fetch = make_http_gatekeeper_fetch("http://api-server.local", session=session)
    with pytest.raises(ToolExecutionError):
        fetch("frontend")
