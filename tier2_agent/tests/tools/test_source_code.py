import time

import pytest

from tier2_agent.exceptions import ToolExecutionError
from tier2_agent.tools.source_code import make_source_code_tool


def test_finds_matching_keyword(source_tree):
    tool = make_source_code_tool(source_tree, stale_threshold_days=365)
    result = tool({"source_service": "frontend", "keyword": "call_payment"})
    assert "call_payment" in result.raw_output


def test_no_match_returns_friendly_message_not_error(source_tree):
    tool = make_source_code_tool(source_tree, stale_threshold_days=365)
    result = tool({"source_service": "frontend", "keyword": "totally_absent_symbol"})
    assert "Không tìm thấy" in result.raw_output


def test_unknown_service_raises_tool_execution_error(source_tree):
    tool = make_source_code_tool(source_tree, stale_threshold_days=365)
    with pytest.raises(ToolExecutionError):
        tool({"source_service": "not-configured", "keyword": "x"})


def test_extracts_pr_number_from_matched_comment(source_tree):
    tool = make_source_code_tool(source_tree, stale_threshold_days=365)
    result = tool({"source_service": "frontend", "keyword": "call_payment"})
    assert result.pr_number == "42"


def test_stale_true_when_commit_older_than_threshold(source_tree):
    far_future = time.time() + 400 * 86400  # đứng ở tương lai xa để commit "cũ" > ngưỡng
    tool = make_source_code_tool(
        source_tree, stale_threshold_days=365, reference_epoch=far_future
    )
    result = tool({"source_service": "frontend", "keyword": "call_payment"})
    assert result.stale is True


def test_not_stale_when_commit_recent(source_tree):
    tool = make_source_code_tool(
        source_tree, stale_threshold_days=365, reference_epoch=time.time()
    )
    result = tool({"source_service": "frontend", "keyword": "call_payment"})
    assert result.stale is False


def test_stale_false_when_commit_lookup_unknown(source_tree):
    tool = make_source_code_tool(
        source_tree,
        stale_threshold_days=365,
        reference_epoch=time.time() + 400 * 86400,
        commit_lookup=lambda path: None,
    )
    result = tool({"source_service": "frontend", "keyword": "call_payment"})
    assert result.stale is False


def test_path_traversal_looking_keyword_does_not_escape_root(source_tree):
    """`keyword` chỉ được dùng làm PATTERN cho grep (subprocess list-args),
    KHÔNG BAO GIỜ ghép vào đường dẫn — 1 keyword trông giống path traversal
    vẫn chỉ là 1 chuỗi tìm kiếm vô hại, không đọc được file ngoài root."""

    tool = make_source_code_tool(source_tree, stale_threshold_days=365)
    result = tool({"source_service": "frontend", "keyword": "../../../../etc/passwd"})
    assert "Không tìm thấy" in result.raw_output


def test_keyword_with_spaces_is_treated_literally_not_as_shell(source_tree):
    """Không dùng shell=True nên keyword có khoảng trắng vẫn chỉ là 1 pattern
    grep bình thường, không bị tách thành nhiều argument/command."""

    tool = make_source_code_tool(source_tree, stale_threshold_days=365)
    result = tool({"source_service": "frontend", "keyword": "def call_payment"})
    assert "call_payment" in result.raw_output
