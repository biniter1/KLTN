from tier2_agent.models import EvidenceRow, ModelOutput, NewHypothesis, SessionState, StanceUpdate


def _row(**kwargs):
    base = dict(
        evidence_id="E1",
        run_id="run1",
        iteration=1,
        hypothesis_id="H1",
        tool_name="read_source_code",
        tool_input={"source_service": "frontend", "keyword": "x"},
        tool_output="found",
        stance=None,
        stale=False,
        status="OK",
    )
    base.update(kwargs)
    return EvidenceRow(**base)


def test_create_and_get_session(storage):
    state = SessionState(run_id="run1", edge_identity_key="key1", session_started_at=100.0)
    storage.create_session(state)
    fetched = storage.get_session("run1")
    assert fetched.run_id == "run1"
    assert fetched.edge_identity_key == "key1"
    assert fetched.manual_abort_flag is False
    assert fetched.phase == "RUNNING"


def test_get_session_missing_returns_none(storage):
    assert storage.get_session("nope") is None


def test_set_manual_abort(storage):
    storage.create_session(SessionState(run_id="run1", edge_identity_key="k", session_started_at=1.0))
    storage.set_manual_abort("run1", True)
    assert storage.get_session("run1").manual_abort_flag is True


def test_set_phase(storage):
    storage.create_session(SessionState(run_id="run1", edge_identity_key="k", session_started_at=1.0))
    storage.set_phase("run1", "CONCLUDED")
    assert storage.get_session("run1").phase == "CONCLUDED"


def test_append_and_get_trace_rows_roundtrip(storage):
    row = _row(tool_input={"a": 1, "b": [1, 2]})
    storage.append_trace_row(row)
    rows = storage.get_trace_rows("run1")
    assert len(rows) == 1
    assert rows[0].tool_input == {"a": 1, "b": [1, 2]}
    assert rows[0].evidence_id == "E1"


def test_update_stance_updates_existing_row(storage):
    storage.append_trace_row(_row(evidence_id="E1", stance=None))
    updated = storage.update_stance("E1", "support")
    assert updated is True
    rows = storage.get_trace_rows("run1")
    assert rows[0].stance == "support"


def test_update_stance_returns_false_for_missing_id(storage):
    assert storage.update_stance("GHOST", "support") is False


def test_evidence_exists(storage):
    storage.append_trace_row(_row(evidence_id="E1"))
    assert storage.evidence_exists("E1") is True
    assert storage.evidence_exists("GHOST") is False


def test_dedup_lookup_finds_matching_ok_row(storage):
    storage.append_trace_row(
        _row(evidence_id="E1", tool_name="read_source_code", tool_input={"source_service": "frontend", "keyword": "x"})
    )
    from tier2_agent.utils import canonical_json_key

    key = canonical_json_key({"source_service": "frontend", "keyword": "x"})
    found = storage.find_dedup_row("run1", "read_source_code", key)
    assert found is not None
    assert found.evidence_id == "E1"


def test_dedup_lookup_excludes_error_rows(storage):
    storage.append_trace_row(
        _row(evidence_id="E1", status="ERROR", tool_input={"source_service": "frontend", "keyword": "x"})
    )
    from tier2_agent.utils import canonical_json_key

    key = canonical_json_key({"source_service": "frontend", "keyword": "x"})
    found = storage.find_dedup_row("run1", "read_source_code", key)
    assert found is None


def test_append_agent_round_and_live_counts(storage):
    mo = ModelOutput(
        reasoning="r",
        action="call_tool",
        stance_updates=[StanceUpdate(evidence_id="E1", stance="support")],
        new_hypotheses=[NewHypothesis(id="H1", statement="s")],
        tool_calls=[],
    )
    storage.append_agent_round("run1", 1, mo, stopped_by=None, stop_reason=None)
    rows = storage.get_agent_rounds("run1")
    assert len(rows) == 1
    assert rows[0]["reasoning"] == "r"
    assert storage.iteration_count("run1") == 1


def test_tools_called_and_hypotheses_opened_count(storage):
    storage.append_trace_row(_row(evidence_id="E1", hypothesis_id="H1", tool_name="read_source_code"))
    storage.append_trace_row(_row(evidence_id="E2", hypothesis_id="H2", tool_name="read_internal_docs"))
    assert storage.tools_called("run1") == {"read_source_code", "read_internal_docs"}
    assert storage.hypotheses_opened_count("run1") == 2


def test_wal_mode_enabled(storage):
    mode = storage._conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert mode.lower() == "wal"
