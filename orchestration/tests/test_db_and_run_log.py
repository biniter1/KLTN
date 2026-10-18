"""db.py (lược đồ dùng chung, append-only, view) + run_log.py (RUNNING -> SUCCESS/FAILED)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from orchestration.db import REASON_CODES, ensure_schema, open_db
from orchestration.run_log import PipelineRunLog, StepFailed, next_attempt_id


def _decision(conn, decision_id="dec_1", reason="CLEAN_CONFIG_MATCH", action="APPROVE"):
    conn.execute(
        "INSERT INTO human_decisions (decision_id, reason_code, target_ref, target_identity_key, decided_by, "
        "decided_at, action) VALUES (?, ?, '{}', 'k', 'bin', '2026-10-06T00:00:00', ?)",
        (decision_id, reason, action))


def test_schema_has_all_tables_and_views(db):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    assert {"dependency_evidence", "pipeline_run_log", "human_decisions", "collection_quality_log",
            "config_raw_items", "config_extractions", "config_evidence", "llm_extraction_cache",
            "agent_sessions", "agent_rounds", "trace_log", "agent_tool_call_log",
            "dependency_current_state", "pipeline_step_status", "latest_successful_attempt"} <= names


def test_ensure_schema_idempotent_and_wal(db):
    ensure_schema(db)
    ensure_schema(db)
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_agent_rounds_has_dashboard_columns(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(agent_rounds)")}
    assert {"reasoning_injection_flagged", "system_notices"} <= cols


def test_human_decisions_is_append_only(db):
    _decision(db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE human_decisions SET action = 'REJECT'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM human_decisions")
    assert db.execute("SELECT action FROM human_decisions").fetchone()[0] == "APPROVE"


def test_agent_tool_call_log_is_append_only(db):
    db.execute("INSERT INTO agent_tool_call_log (tool_name, request_json, status, created_at) "
               "VALUES ('read_pr_descriptions', '{}', 'OK', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM agent_tool_call_log")


@pytest.mark.parametrize("reason", REASON_CODES)
def test_all_eleven_reason_codes_accepted(db, reason):
    _decision(db, decision_id=f"d_{reason}", reason=reason)


def test_unknown_reason_code_and_action_rejected_by_check_constraint(db):
    with pytest.raises(sqlite3.IntegrityError):
        _decision(db, reason="APPROVE_EVERYTHING")
    with pytest.raises(sqlite3.IntegrityError):
        _decision(db, decision_id="d2", action="DELETE_EDGE")


def test_pipeline_run_log_rejects_unknown_step_or_status(db):
    log = PipelineRunLog(db, "trial-01", "trial-01.a1")
    with pytest.raises(sqlite3.IntegrityError):
        log.record("deploy", "RUNNING")
    with pytest.raises(sqlite3.IntegrityError):
        log.record("tier1", "DONE")


def test_step_success_writes_running_then_success_with_detail(db):
    log = PipelineRunLog(db, "trial-01", "trial-01.a1")
    with log.step("tier1") as ctx:
        ctx.detail["edges"] = 5
    rows = db.execute("SELECT status, detail FROM pipeline_run_log ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["RUNNING", "SUCCESS"]
    assert json.loads(rows[1]["detail"]) == {"edges": 5}


def test_step_exception_becomes_failed_and_stepfailed(db):
    log = PipelineRunLog(db, "trial-01", "trial-01.a1")
    with pytest.raises(StepFailed) as exc_info:
        with log.step("config_collector"):
            raise ValueError("LLM trả sai schema")
    assert exc_info.value.step == "config_collector"
    row = db.execute("SELECT status, error_message, detail FROM pipeline_run_log ORDER BY id DESC").fetchone()
    assert row["status"] == "FAILED"
    assert row["error_message"] == "ValueError: LLM trả sai schema"
    assert "traceback" in json.loads(row["detail"])


def test_step_view_shows_latest_status_per_step(db):
    log = PipelineRunLog(db, "trial-01", "trial-01.a1")
    log.record("tier1", "RUNNING")
    assert db.execute("SELECT status FROM pipeline_step_status WHERE step='tier1'").fetchone()[0] == "RUNNING"
    log.record("tier1", "SUCCESS")
    assert db.execute("SELECT status FROM pipeline_step_status WHERE step='tier1'").fetchone()[0] == "SUCCESS"


def test_next_attempt_id_increments_per_trial(db):
    assert next_attempt_id(db, "trial-01") == "trial-01.a1"
    PipelineRunLog(db, "trial-01", "trial-01.a1").record("trial", "RUNNING")
    PipelineRunLog(db, "trial-02", "trial-02.a1").record("trial", "RUNNING")
    assert next_attempt_id(db, "trial-01") == "trial-01.a2"


def test_open_db_on_old_file_migrates_tier2_and_quality_log(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(str(path))
    old.executescript(
        "CREATE TABLE agent_rounds (run_id TEXT NOT NULL, iteration INTEGER NOT NULL, reasoning TEXT, "
        "stance_updates TEXT, new_hypotheses TEXT, action TEXT, stopped_by TEXT, stop_reason TEXT, "
        "timestamp REAL NOT NULL, PRIMARY KEY (run_id, iteration));"
        "CREATE TABLE collection_quality_log (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, "
        "metric TEXT NOT NULL, value REAL NOT NULL, created_at TEXT NOT NULL);")
    old.close()
    conn = open_db(path)
    assert "system_notices" in {r[1] for r in conn.execute("PRAGMA table_info(agent_rounds)")}
    assert "module" in {r[1] for r in conn.execute("PRAGMA table_info(collection_quality_log)")}
