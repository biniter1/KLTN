"""ORCHESTRATION_DESIGN.md mục 3: "UI/API Server KHÔNG có câu lệnh nào đụng tới bảng này
trong codebase của nó — không phải 'được quyền nhưng không dùng', mà là khả năng đó không
tồn tại trong code." Test này QUÉT MÃ NGUỒN để khoá luật đó lại, không chỉ kiểm hành vi."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
ORCH = ROOT / "orchestration" / "orchestration"
API = ORCH / "api"

_WRITE_RE = re.compile(r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM|ALTER\s+TABLE|DROP\s+TABLE)\s+(\w+)")

BATCH_OWNED = {"dependency_evidence", "pipeline_run_log", "collection_quality_log", "config_raw_items",
               "config_extractions", "config_evidence", "llm_extraction_cache", "agent_rounds", "trace_log"}
API_OWNED = {"human_decisions", "agent_tool_call_log"}


def _written_tables(path: Path) -> set[str]:
    return set(_WRITE_RE.findall(path.read_text(encoding="utf-8")))


def _py_files(*dirs: Path) -> list[Path]:
    return [p for d in dirs for p in d.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize("path", _py_files(API), ids=lambda p: p.name)
def test_api_server_never_writes_batch_tables(path):
    assert not (_written_tables(path) & BATCH_OWNED), f"{path.name} ghi vào bảng của Batch Orchestrator"


def test_only_decisions_store_writes_human_decisions():
    writers = {p.name for p in _py_files(ROOT / "orchestration", ROOT / "tier1_inference" / "tier1",
                                         ROOT / "tier2_agent" / "tier2_agent")
               if "tests" not in p.parts and "human_decisions" in _written_tables(p)}
    assert writers == {"decisions_store.py"}


def test_only_gatekeeper_writes_tool_call_log():
    writers = {p.name for p in _py_files(ROOT / "orchestration") if "tests" not in p.parts
               and "agent_tool_call_log" in _written_tables(p)}
    assert writers == {"gatekeeper.py"}


@pytest.mark.parametrize("path", [p for p in _py_files(ORCH) if p.parent == ORCH], ids=lambda p: p.name)
def test_batch_side_never_writes_api_tables(path):
    assert not (_written_tables(path) & API_OWNED)


def test_api_does_not_import_batch_writers():
    for path in _py_files(API):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("insert_results", "insert_quality_log", "insert_config_evidence", "apply_tier2_result",
                          "from ..batch", "PipelineRunLog"):
            assert forbidden not in text, f"{path.name} import/gọi {forbidden}"
