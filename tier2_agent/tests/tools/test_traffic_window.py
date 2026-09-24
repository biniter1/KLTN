import pytest

from tier2_agent.exceptions import ToolExecutionError
from tier2_agent.tools.traffic_window import make_traffic_window_tool


class _Provider:
    def __init__(self, response=None, exc=None):
        self.response = response or {"count": 20, "distinct_days": 5, "error_ratio": 0.1}
        self.exc = exc
        self.calls = []

    def fetch_extended_window(self, edge, window_days):
        self.calls.append((edge, window_days))
        if self.exc:
            raise self.exc
        return self.response


def test_returns_summary_text_with_numbers():
    provider = _Provider()
    tool = make_traffic_window_tool(provider)
    result = tool({"edge": "e1", "window": 30})
    assert "count=20" in result.raw_output
    assert "distinct_days=5" in result.raw_output
    assert provider.calls == [("e1", 30)]


def test_provider_error_becomes_tool_execution_error():
    provider = _Provider(exc=TimeoutError("prometheus timeout"))
    tool = make_traffic_window_tool(provider)
    with pytest.raises(ToolExecutionError):
        tool({"edge": "e1", "window": 30})
