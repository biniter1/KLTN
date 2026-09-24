import pytest

from tier2_agent.config import Tier2Config
from tier2_agent.exceptions import ToolExecutionError, ToolInputRejected, UnknownToolError
from tier2_agent.tool_registry import ToolRegistry, ToolResult

ALLOWED = frozenset({"frontend", "payment"})
EDGE_KEY = "default|frontend|frontend-sa|default|payment|TCP|8080"


@pytest.fixture
def registry():
    cfg = Tier2Config()
    impls = {
        "read_source_code": lambda ti: ToolResult(raw_output="ok source"),
        "read_pr_descriptions": lambda ti: ToolResult(raw_output="ok pr"),
        "read_internal_docs": lambda ti: ToolResult(raw_output="ok docs"),
        "read_extended_traffic_window": lambda ti: ToolResult(raw_output="ok traffic"),
    }
    return ToolRegistry(cfg, impls)


def test_available_tools_lists_all_4_with_descriptions(registry):
    tools = registry.available_tools()
    names = {t.name for t in tools}
    assert names == {
        "read_source_code",
        "read_pr_descriptions",
        "read_internal_docs",
        "read_extended_traffic_window",
    }
    assert all(t.description for t in tools)
    assert all(t.scope == "read-only" for t in tools)


def test_unknown_tool_name_rejected(registry):
    with pytest.raises(UnknownToolError):
        registry.validate_input(
            "read_everything", {}, allowed_services=ALLOWED, edge_identity_key=EDGE_KEY
        )


def test_source_service_must_match_canonical_service(registry):
    with pytest.raises(ToolInputRejected):
        registry.validate_input(
            "read_source_code",
            {"source_service": "not-a-real-service", "keyword": "payment"},
            allowed_services=ALLOWED,
            edge_identity_key=EDGE_KEY,
        )


def test_valid_source_code_input_accepted(registry):
    registry.validate_input(
        "read_source_code",
        {"source_service": "frontend", "keyword": "payment"},
        allowed_services=ALLOWED,
        edge_identity_key=EDGE_KEY,
    )


def test_empty_keyword_rejected(registry):
    with pytest.raises(ToolInputRejected):
        registry.validate_input(
            "read_internal_docs",
            {"service": "frontend", "keyword": ""},
            allowed_services=ALLOWED,
            edge_identity_key=EDGE_KEY,
        )


def test_non_dict_tool_input_rejected(registry):
    with pytest.raises(ToolInputRejected):
        registry.validate_input(
            "read_internal_docs",
            "not-a-dict",  # type: ignore[arg-type]
            allowed_services=ALLOWED,
            edge_identity_key=EDGE_KEY,
        )


def test_keyword_too_long_rejected(registry):
    with pytest.raises(ToolInputRejected):
        registry.validate_input(
            "read_internal_docs",
            {"service": "frontend", "keyword": "a" * 200},
            allowed_services=ALLOWED,
            edge_identity_key=EDGE_KEY,
        )


@pytest.mark.parametrize("bad_keyword", ["rm -rf /; echo", "a`whoami`", "a|b", "a$(id)", "a>b", "a<b", "a~b"])
def test_keyword_bad_charset_rejected(registry, bad_keyword):
    with pytest.raises(ToolInputRejected):
        registry.validate_input(
            "read_internal_docs",
            {"service": "frontend", "keyword": bad_keyword},
            allowed_services=ALLOWED,
            edge_identity_key=EDGE_KEY,
        )


def test_keyword_path_like_chars_allowed_by_charset_but_harmless():
    """`.` và `/` được PHÉP trong charset (tìm path/keyword kỹ thuật) — an
    toàn không phải vì charset chặn hết, mà vì keyword chỉ được dùng làm
    PATTERN cho grep qua subprocess list-args, không bao giờ ghép vào path
    thật (mục 2.1: "không phụ thuộc việc lọc keyword có bắt hết ký tự nguy
    hiểm hay không"). Test thực thi thật ở tests/tools/test_source_code.py
    xác nhận keyword dạng path-traversal không thoát khỏi thư mục root.
    """

    cfg = Tier2Config()
    registry = ToolRegistry(cfg, {})
    registry.validate_input(
        "read_internal_docs",
        {"service": "frontend", "keyword": "../../etc/passwd"},
        allowed_services=ALLOWED,
        edge_identity_key=EDGE_KEY,
    )


def test_traffic_window_edge_must_match_session_identity_key(registry):
    with pytest.raises(ToolInputRejected):
        registry.validate_input(
            "read_extended_traffic_window",
            {"edge": "some-other-edge", "window": 14},
            allowed_services=ALLOWED,
            edge_identity_key=EDGE_KEY,
        )
    registry.validate_input(
        "read_extended_traffic_window",
        {"edge": EDGE_KEY, "window": 14},
        allowed_services=ALLOWED,
        edge_identity_key=EDGE_KEY,
    )


@pytest.mark.parametrize("window", [0, -1, 91, 1.5, True])
def test_traffic_window_window_must_be_bounded_int(registry, window):
    with pytest.raises(ToolInputRejected):
        registry.validate_input(
            "read_extended_traffic_window",
            {"edge": EDGE_KEY, "window": window},
            allowed_services=ALLOWED,
            edge_identity_key=EDGE_KEY,
        )


def test_execute_dispatches_to_implementation(registry):
    result = registry.execute("read_source_code", {"source_service": "frontend", "keyword": "x"})
    assert result.raw_output == "ok source"


def test_execute_unknown_tool_raises(registry):
    with pytest.raises(UnknownToolError):
        registry.execute("read_everything", {})


def test_execute_wraps_unexpected_exception_as_tool_execution_error():
    cfg = Tier2Config()

    def _boom(_ti):
        raise ValueError("boom")

    registry = ToolRegistry(cfg, {"read_source_code": _boom})
    with pytest.raises(ToolExecutionError):
        registry.execute("read_source_code", {"source_service": "frontend", "keyword": "x"})


def test_execute_reraises_tool_execution_error_unwrapped():
    cfg = Tier2Config()

    def _boom(_ti):
        raise ToolExecutionError("network down")

    registry = ToolRegistry(cfg, {"read_source_code": _boom})
    with pytest.raises(ToolExecutionError, match="network down"):
        registry.execute("read_source_code", {"source_service": "frontend", "keyword": "x"})
