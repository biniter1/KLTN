"""Fixture/factory dùng chung — cùng phong cách `conftest.py` đã dùng cho
bộ test Tier 1 (factory hàm trả record/tool tuỳ chỉnh theo tham số, thay vì
1 đống fixture cố định)."""

from __future__ import annotations

import subprocess
import time

import pytest

from tier2_agent.config import Tier2Config
from tier2_agent.escalation import EscalationRecord
from tier2_agent.llm.client import ScriptedModelClient
from tier2_agent.storage import Storage
from tier2_agent.tool_registry import ToolRegistry
from tier2_agent.tools.internal_docs import make_internal_docs_tool
from tier2_agent.tools.pr_descriptions import CachingGatekeeperClient, make_pr_descriptions_tool
from tier2_agent.tools.source_code import make_source_code_tool
from tier2_agent.tools.traffic_window import make_traffic_window_tool


# ---------------------------------------------------------------------------
# Escalation record factory (TIER1_DESIGN.md mục 10)
# ---------------------------------------------------------------------------


def _base_escalation_dict(**overrides) -> dict:
    data = {
        "source_namespace": "default",
        "source_canonical_service": "frontend",
        "source_service_account": "frontend-sa",
        "destination_namespace": "default",
        "destination_canonical_service": "payment",
        "destination_service_ref": {"service_name": "payment", "service_namespace": "default"},
        "protocol": "TCP",
        "port": 8080,
        "scope": "IN_CLUSTER",
        "traffic_class": "BUSINESS",
        "count": 5,
        "distinct_days": 2,
        "error_ratio": 0.0,
        "unstable": False,
        "config_finding": None,
        "score": 0.42,
        "dependency_status": "OBSERVED_WEAK",
        "snapshot_id": "snap_test",
        "workload_revision": "sha256:test",
    }
    data.update(overrides)
    return data


@pytest.fixture
def escalation_dict_factory():
    return _base_escalation_dict


@pytest.fixture
def make_escalation():
    def _make(**overrides) -> EscalationRecord:
        return EscalationRecord.from_dict(_base_escalation_dict(**overrides))

    return _make


# ---------------------------------------------------------------------------
# Config / Storage
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_config() -> Tier2Config:
    """Config với ngưỡng nhỏ để test hội tụ nhanh, không đợi timeout thật."""

    return Tier2Config(
        max_iterations=4,
        session_timeout_s=999999,  # vô hiệu hoá timeout thời gian trong test thường
        max_open_hypotheses=2,
        max_tool_calls_per_round=2,
        max_consecutive_tool_errors=2,
        tool_output_char_cap=4000,
        context_window_tokens=200_000,
        normalize_token_budget_ratio=0.70,
    )


@pytest.fixture
def storage(tmp_path):
    db_path = str(tmp_path / "tier2_test.sqlite3")
    s = Storage(db_path)
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Cây source thật (git repo nhỏ) cho read_source_code / read_internal_docs
# ---------------------------------------------------------------------------


def _run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def source_tree(tmp_path):
    """Tạo 2 service giả: frontend, payment — mỗi service có source + README,
    khởi tạo git repo thật để test staleness (mục 7.5) qua `git log`."""

    root = tmp_path / "services"
    services = {}
    for name, content in [
        ("frontend", "def call_payment():  # gọi payment qua http, xem PR #42\n    pass\n"),
        ("payment", "def handle():\n    return 'ok'\n"),
    ]:
        svc_dir = root / name
        svc_dir.mkdir(parents=True)
        (svc_dir / "main.py").write_text(content, encoding="utf-8")
        (svc_dir / "README.md").write_text(f"# {name}\n\nGọi payment (dependency ẩn).\n", encoding="utf-8")
        (svc_dir / "Dockerfile").write_text("FROM python:3.11\n", encoding="utf-8")

        _run(["git", "init", "-q"], cwd=str(svc_dir))
        _run(["git", "config", "user.email", "test@example.com"], cwd=str(svc_dir))
        _run(["git", "config", "user.name", "Test"], cwd=str(svc_dir))
        _run(["git", "add", "."], cwd=str(svc_dir))
        _run(["git", "commit", "-q", "-m", "init"], cwd=str(svc_dir))

        services[name] = str(svc_dir)
    return services


# ---------------------------------------------------------------------------
# Fake gatekeeper / traffic provider
# ---------------------------------------------------------------------------


class FakeTrafficProvider:
    def __init__(self, response: dict | None = None, error: Exception | None = None):
        self.response = response or {"count": 12, "distinct_days": 4, "error_ratio": 0.05}
        self.error = error
        self.calls = []

    def fetch_extended_window(self, edge: str, window_days: int) -> dict:
        self.calls.append((edge, window_days))
        if self.error:
            raise self.error
        return self.response


def make_fake_pr_fetch(pr_map: dict):
    """pr_map: service -> list[dict PR]. Trả callable(service) -> list[dict]."""

    calls = []

    def _fetch(service: str):
        calls.append(service)
        return pr_map.get(service, [])

    _fetch.calls = calls
    return _fetch


@pytest.fixture
def tool_registry_factory(source_tree, fast_config):
    def _make(
        *,
        pr_map: dict | None = None,
        traffic_provider=None,
        gatekeeper_fetch=None,
        stale_threshold_days: int = 365,
        reference_epoch: float | None = None,
        commit_lookup=None,
    ) -> ToolRegistry:
        traffic_provider = traffic_provider or FakeTrafficProvider()
        fetch_fn = gatekeeper_fetch or make_fake_pr_fetch(pr_map or {})
        gatekeeper = CachingGatekeeperClient(fetch_fn)

        kwargs = dict(
            stale_threshold_days=stale_threshold_days,
            reference_epoch=reference_epoch if reference_epoch is not None else time.time(),
        )
        if commit_lookup is not None:
            kwargs["commit_lookup"] = commit_lookup

        implementations = {
            "read_source_code": make_source_code_tool(source_tree, **kwargs),
            "read_internal_docs": make_internal_docs_tool(source_tree, **kwargs),
            "read_pr_descriptions": make_pr_descriptions_tool(gatekeeper, source_tree),
            "read_extended_traffic_window": make_traffic_window_tool(traffic_provider),
        }
        return ToolRegistry(fast_config, implementations), gatekeeper, traffic_provider

    return _make


# ---------------------------------------------------------------------------
# Model Output raw-dict builder (dùng cho ScriptedModelClient)
# ---------------------------------------------------------------------------


def model_output(
    *,
    reasoning: str = "reasoning...",
    action: str = "call_tool",
    stance_updates=None,
    new_hypotheses=None,
    tool_calls=None,
) -> dict:
    out = {
        "reasoning": reasoning,
        "action": action,
        "stance_updates": stance_updates or [],
        "tool_calls": tool_calls or [],
    }
    if new_hypotheses is not None:
        out["new_hypotheses"] = new_hypotheses
    return out


@pytest.fixture
def model_output_factory():
    return model_output


@pytest.fixture
def scripted_client_factory():
    def _make(script):
        return ScriptedModelClient(script=list(script))

    return _make
