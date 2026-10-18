import sqlite3

import pytest

from tier2_agent.models import RoundResult
from tier2_agent.orchestrator_hook import apply_tier2_result


def _result(final="CONFIDENT", original="CONFIDENT"):
    return RoundResult(
        run_id="run1",
        iteration=3,
        hypotheses=[],
        final_status=final,
        original_agent_status=original,
        action="conclude",
        stopped=True,
        stopped_by="model",
        stop_reason=None,
        persisted=True,
        reasoning="done",
    )


def test_raises_when_table_missing_and_not_allowed_to_create(make_escalation):
    conn = sqlite3.connect(":memory:")
    escalation = make_escalation()
    with pytest.raises(RuntimeError):
        apply_tier2_result(conn, escalation, "run1", _result())


def test_creates_minimal_table_and_updates_matching_row(make_escalation):
    conn = sqlite3.connect(":memory:")
    escalation = make_escalation()
    conn.execute(
        "CREATE TABLE dependency_evidence (run_id TEXT, source_namespace TEXT, "
        "source_canonical_service TEXT, source_service_account TEXT, "
        "destination_namespace TEXT, destination_canonical_service TEXT, "
        "protocol TEXT, port INTEGER)"
    )
    conn.execute(
        "INSERT INTO dependency_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "run1",
            escalation.source_namespace,
            escalation.source_canonical_service,
            escalation.source_service_account,
            escalation.destination_namespace,
            escalation.destination_canonical_service,
            escalation.protocol,
            escalation.port,
        ),
    )
    conn.commit()

    affected = apply_tier2_result(conn, escalation, "run1", _result(final="NEEDS_MONITORING", original="CONFIDENT"))
    assert affected == 1

    row = conn.execute(
        "SELECT tier2_status, tier2_original_status FROM dependency_evidence WHERE run_id='run1'"
    ).fetchone()
    assert row == ("NEEDS_MONITORING", "CONFIDENT")


def test_create_if_missing_builds_minimal_schema(make_escalation):
    conn = sqlite3.connect(":memory:")
    escalation = make_escalation()
    affected = apply_tier2_result(conn, escalation, "run1", _result(), create_if_missing=True)
    # Không có dòng dependency_evidence nào tồn tại từ trước -> 0 dòng bị ảnh hưởng,
    # nhưng KHÔNG raise (bảng đã được tạo).
    assert affected == 0
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "dependency_evidence" in tables


def test_does_not_update_row_for_a_different_edge(make_escalation):
    conn = sqlite3.connect(":memory:")
    escalation = make_escalation()
    conn.executescript(
        "CREATE TABLE dependency_evidence (run_id TEXT, source_namespace TEXT, "
        "source_canonical_service TEXT, source_service_account TEXT, "
        "destination_namespace TEXT, destination_canonical_service TEXT, "
        "protocol TEXT, port INTEGER);"
    )
    conn.execute(
        "INSERT INTO dependency_evidence VALUES ('run1', 'default', 'OTHER', 'other-sa', 'default', 'payment', 'TCP', 9999)"
    )
    conn.commit()

    affected = apply_tier2_result(conn, escalation, "run1", _result())
    assert affected == 0


# ---------------------------------------------------------------------------
# Tích hợp Batch Orchestrator: port NULL, attempt_id, tier2_session_id
# ---------------------------------------------------------------------------


def _table_with_rows(escalation, rows):
    """rows: list (run_id, attempt_id, port)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE dependency_evidence (run_id TEXT, attempt_id TEXT, source_namespace TEXT, "
        "source_canonical_service TEXT, source_service_account TEXT, "
        "destination_namespace TEXT, destination_canonical_service TEXT, "
        "protocol TEXT, port INTEGER)"
    )
    for run_id, attempt_id, port in rows:
        conn.execute(
            "INSERT INTO dependency_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, attempt_id, escalation.source_namespace, escalation.source_canonical_service,
             escalation.source_service_account, escalation.destination_namespace,
             escalation.destination_canonical_service, escalation.protocol, port),
        )
    conn.commit()
    return conn


def test_null_port_edge_is_matched(make_escalation):
    """Cạnh potential-only (NOT_OBSERVED + config mù) hay có port=None — trước đây
    `port = ?` với NULL khớp 0 dòng."""
    escalation = make_escalation(
        dependency_status="NOT_OBSERVED", config_finding="FOUND_UNRESOLVED", port=None, protocol="UNKNOWN",
    )
    conn = _table_with_rows(escalation, [("trial-07", "trial-07.a1", None)])
    assert apply_tier2_result(conn, escalation, "trial-07", _result()) == 1


def test_attempt_id_limits_update_to_current_attempt(make_escalation):
    escalation = make_escalation()
    conn = _table_with_rows(
        escalation, [("trial-07", "trial-07.a1", escalation.port), ("trial-07", "trial-07.a2", escalation.port)]
    )
    affected = apply_tier2_result(
        conn, escalation, "trial-07", _result(final="CONFLICTING_EVIDENCE"),
        attempt_id="trial-07.a2", tier2_session_id="trial-07.a2#abc",
    )
    assert affected == 1
    rows = conn.execute(
        "SELECT attempt_id, tier2_status, tier2_session_id FROM dependency_evidence ORDER BY attempt_id"
    ).fetchall()
    assert rows == [("trial-07.a1", None, None), ("trial-07.a2", "CONFLICTING_EVIDENCE", "trial-07.a2#abc")]
