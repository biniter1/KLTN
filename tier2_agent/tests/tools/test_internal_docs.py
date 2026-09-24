import pytest

from tier2_agent.exceptions import ToolExecutionError
from tier2_agent.tools.internal_docs import make_internal_docs_tool


def test_finds_keyword_in_readme(source_tree):
    tool = make_internal_docs_tool(source_tree, stale_threshold_days=365)
    result = tool({"service": "frontend", "keyword": "payment"})
    assert "payment" in result.raw_output.lower()


def test_no_match_returns_friendly_message(source_tree):
    tool = make_internal_docs_tool(source_tree, stale_threshold_days=365)
    result = tool({"service": "frontend", "keyword": "nonexistent_topic_zzz"})
    assert "Không tìm thấy" in result.raw_output


def test_unknown_service_raises(source_tree):
    tool = make_internal_docs_tool(source_tree, stale_threshold_days=365)
    with pytest.raises(ToolExecutionError):
        tool({"service": "ghost-service", "keyword": "x"})


def test_service_without_readme_reports_clearly(source_tree, tmp_path):
    empty_dir = tmp_path / "no_docs_service"
    empty_dir.mkdir()
    services = dict(source_tree)
    services["bare"] = str(empty_dir)
    tool = make_internal_docs_tool(services, stale_threshold_days=365)
    result = tool({"service": "bare", "keyword": "anything"})
    assert "không có README" in result.raw_output
