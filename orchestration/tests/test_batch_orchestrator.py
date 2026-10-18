"""BatchOrchestrator end-to-end (ORCHESTRATION_DESIGN.md mục 1-3) — Prometheus/K8s/LLM/Tier 2
đều là bản giả, mọi module Tier 1/Collector/Tier 2 bên trong là code THẬT."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from orchestration.batch import BatchOrchestrator, tier2_session_id
from tier1.config_collector.extract import LLMExtractionError
from tier2_agent.escalation import EscalationRecord

from .conftest import NOW, NS, PerfectExtractionLLM, ScriptedTier2Runner, make_prometheus, read_rows


def _events(db_path, attempt_id):
    return [(r["step"], r["status"]) for r in read_rows(
        db_path, "SELECT step, status FROM pipeline_run_log WHERE attempt_id = ? ORDER BY id", (attempt_id,))]


def _evidence(db_path, attempt_id):
    return read_rows(db_path, "SELECT * FROM dependency_evidence WHERE attempt_id = ? ORDER BY id", (attempt_id,))


# ---------------------------------------------------------------------------
# Luồng thành công
# ---------------------------------------------------------------------------


def test_success_runs_every_step_in_order_with_running_then_success(make_settings, make_deps):
    settings = make_settings()
    outcome = BatchOrchestrator(settings, make_deps()).run()

    assert outcome.status == "SUCCESS"
    assert outcome.attempt_id == "trial-01.a1"
    assert _events(settings.db_path, "trial-01.a1") == [
        ("trial", "RUNNING"),
        ("scenario_check", "RUNNING"), ("scenario_check", "SUCCESS"),
        ("traffic_collector", "RUNNING"), ("traffic_collector", "SUCCESS"),
        ("config_collector", "RUNNING"), ("config_collector", "SUCCESS"),
        ("tier1", "RUNNING"), ("tier1", "SUCCESS"),
        ("tier2", "RUNNING"), ("tier2", "SUCCESS"),
        ("trial", "SUCCESS"),
    ]


def test_running_status_visible_to_other_process_while_step_in_progress(make_settings, make_deps, infra_files):
    """DASHBOARD_DESIGN.md mục 5 điểm 1 — lúc Tier 2 đang chạy, 1 kết nối KHÁC (như API
    Server) phải thấy trial + tier2 = RUNNING, không im lặng tới khi xong."""
    seen = {}

    class _Spy(ScriptedTier2Runner):
        def run(self, escalation_dict, session_id):
            other = sqlite3.connect(str(infra_files["db_path"]))
            seen.update(dict(other.execute(
                "SELECT step, status FROM pipeline_step_status WHERE attempt_id = 'trial-01.a1'").fetchall()))
            other.close()
            return super().run(escalation_dict, session_id)

    BatchOrchestrator(make_settings(), make_deps(tier2_runner=_Spy())).run()
    assert seen["trial"] == "RUNNING"
    assert seen["tier2"] == "RUNNING"
    assert seen["tier1"] == "SUCCESS"


def test_evidence_rows_tagged_with_run_attempt_and_identity_key(make_settings, make_deps):
    settings = make_settings()
    BatchOrchestrator(settings, make_deps()).run()
    rows = _evidence(settings.db_path, "trial-01.a1")
    assert rows and all(r["run_id"] == "trial-01" for r in rows)
    assert all(r["identity_key"] and r["identity_key"].count("|") == 6 for r in rows)
    assert all(r["snapshot_id"] == "trial-01.a1" for r in rows)


def test_traffic_and_config_split_by_pair_key_excludes_other_trials_edges(make_settings, make_deps):
    settings = make_settings()
    BatchOrchestrator(settings, make_deps()).run()
    pairs = {(r["source_canonical_service"], r["destination_canonical_service"])
             for r in _evidence(settings.db_path, "trial-01.a1")}
    assert ("frontend", "cartservice") in pairs            # cạnh thật OB — mọi trial
    assert ("adservice", "paymentservice") in pairs        # cạnh cấy của trial-01
    assert ("currencyservice", "cartservice") in pairs     # CONFIG_ONLY_CLEAN của trial-01
    assert ("shippingservice", "emailservice") not in pairs  # cạnh cấy của trial-02


def test_config_only_unresolved_edge_reaches_tier1_and_is_escalated(make_settings, make_deps):
    """FOUND_UNRESOLVED có destination=None — phải khớp lại qua raw_host '<dest>-nonexistent'
    (trial_scope.py), nếu không kịch bản CONFIG_ONLY_UNRESOLVED biến mất khỏi mọi trial.
    Từ 15/10 Tầng 1 ghi đích dạng "raw:<host>" (mỗi host lạ 1 dòng riêng — TIER1_DESIGN.md mục 1b)."""
    runner = ScriptedTier2Runner()
    settings = make_settings()
    BatchOrchestrator(settings, make_deps(tier2_runner=runner)).run()
    unresolved = [r for r in _evidence(settings.db_path, "trial-01.a1") if r["config_finding"] == "FOUND_UNRESOLVED"]
    assert len(unresolved) == 1
    assert unresolved[0]["source_canonical_service"] == "paymentservice"
    assert unresolved[0]["destination_canonical_service"] == "raw:adservice-nonexistent"
    assert unresolved[0]["destination_namespace"] is None
    assert unresolved[0]["escalate_to_tier2"] == 1
    assert unresolved[0]["tier2_status"] == "CONFIDENT"
    assert any(d["destination_canonical_service"] == "raw:adservice-nonexistent" for d, _sid in runner.calls)


def test_quality_log_has_one_wide_row_per_module_with_trial_split_counts(make_settings, make_deps):
    settings = make_settings()
    BatchOrchestrator(settings, make_deps()).run()
    rows = {r["module"]: r for r in read_rows(
        settings.db_path, "SELECT * FROM collection_quality_log WHERE attempt_id = 'trial-01.a1'")}
    assert set(rows) == {"traffic_collector", "config_collector"}
    t = rows["traffic_collector"]
    assert (t["series_total"], t["series_kept_for_trial"], t["scrape_gap_detected"]) == (4, 3, 0)
    c = rows["config_collector"]
    assert c["evidence_total"] == 2 and c["evidence_kept_for_trial"] == 2
    assert c["candidates_found"] == 1 and c["candidates_found_unresolved"] == 1
    assert c["llm_calls"] >= 1 and c["llm_cache_hits"] == 0
    assert set(json.loads(c["items_dropped_by_funnel"])) == {"EMPTY", "BOOLEAN", "BINARY", "SECRET_REF"}


def test_second_trial_reuses_llm_cache_but_rescans_config(make_settings, make_deps):
    """Mục 1: quét lại config TỪ ĐẦU mỗi trial, nhưng chunk giống hệt không gọi lại LLM."""
    BatchOrchestrator(make_settings("trial-01"), make_deps()).run()
    llm = PerfectExtractionLLM()
    settings = make_settings("trial-02")
    assert BatchOrchestrator(settings, make_deps(llm_client=llm)).run().status == "SUCCESS"
    q = read_rows(settings.db_path, "SELECT * FROM collection_quality_log WHERE attempt_id = 'trial-02.a1' "
                                    "AND module = 'config_collector'")[0]
    assert llm.calls == 0 and q["llm_calls"] == 0 and q["llm_cache_hits"] == q["chunks_sent"] > 0
    raw = read_rows(settings.db_path, "SELECT COUNT(*) AS n FROM config_raw_items WHERE run_id = 'trial-02.a1'")
    assert raw[0]["n"] > 0  # vẫn thu thập lại toàn bộ item, chỉ bỏ qua lời gọi LLM


# ---------------------------------------------------------------------------
# scenario_check — "cấy + chờ K ngày" ở hạ tầng dùng chung = chỉ kiểm tra
# ---------------------------------------------------------------------------


def test_fails_when_observation_window_not_yet_elapsed(make_settings, make_deps):
    settings = make_settings()
    deps = make_deps(deployment_clock=lambda _ns: NOW - timedelta(days=3))
    outcome = BatchOrchestrator(settings, deps).run()
    assert outcome.status == "FAILED" and outcome.failed_step == "scenario_check"
    assert "3.0 ngày" in outcome.error_message
    assert _events(settings.db_path, outcome.attempt_id)[-2:] == [("scenario_check", "FAILED"), ("trial", "FAILED")]


def test_allow_short_window_runs_anyway_and_records_it(make_settings, make_deps):
    settings = make_settings(allow_short_window=True)
    outcome = BatchOrchestrator(settings, make_deps(deployment_clock=lambda _ns: NOW - timedelta(hours=2))).run()
    assert outcome.status == "SUCCESS"
    detail = read_rows(settings.db_path, "SELECT detail FROM pipeline_run_log WHERE step = 'scenario_check' "
                                         "AND status = 'SUCCESS'")[0]["detail"]
    assert json.loads(detail)["short_window_allowed"] is True


def test_fails_when_deployment_time_unknown(make_settings, make_deps):
    outcome = BatchOrchestrator(make_settings(), make_deps(deployment_clock=lambda _ns: None)).run()
    assert outcome.failed_step == "scenario_check"


def test_fails_when_ground_truth_missing(make_settings, make_deps):
    outcome = BatchOrchestrator(make_settings("trial-09"), make_deps()).run()
    assert outcome.failed_step == "scenario_check"
    assert "ground truth" in outcome.error_message


# ---------------------------------------------------------------------------
# Luật cứng mục 2 — lỗi bất kỳ bước nào -> hủy cả trial, không chạy tiếp âm thầm
# ---------------------------------------------------------------------------


def test_llm_error_fails_config_collector_and_tier1_never_runs(make_settings, make_deps):
    settings = make_settings()
    deps = make_deps(llm_client=PerfectExtractionLLM(fail_with=LLMExtractionError("quota hết")))
    outcome = BatchOrchestrator(settings, deps).run()
    assert outcome.status == "FAILED" and outcome.failed_step == "config_collector"
    steps = [s for s, _ in _events(settings.db_path, outcome.attempt_id)]
    assert "tier1" not in steps
    assert _evidence(settings.db_path, outcome.attempt_id) == []


def test_no_regex_fallback_when_llm_fails(make_settings, make_deps):
    settings = make_settings()
    BatchOrchestrator(settings, make_deps(llm_client=PerfectExtractionLLM(fail_with=LLMExtractionError("x")))).run()
    methods = read_rows(settings.db_path, "SELECT DISTINCT extraction_method AS m FROM config_evidence")
    assert {r["m"] for r in methods} <= {"llm"}


def test_prometheus_error_fails_traffic_step_with_message(make_settings, make_deps):
    class _Broken:
        def query(self, promql, time=None):
            raise ConnectionError("prometheus port-forward đã chết")

    outcome = BatchOrchestrator(make_settings(), make_deps(prometheus_client=_Broken())).run()
    assert outcome.failed_step == "traffic_collector"
    assert "ConnectionError" in outcome.error_message


def test_scrape_gap_is_flagged_but_not_fatal_by_default(make_settings, make_deps):
    settings = make_settings()
    outcome = BatchOrchestrator(settings, make_deps(prometheus_client=make_prometheus(gap_targets=["10.0.0.7"]))).run()
    assert outcome.status == "SUCCESS"
    q = read_rows(settings.db_path, "SELECT * FROM collection_quality_log WHERE module = 'traffic_collector'")[0]
    assert q["scrape_gap_detected"] == 1 and json.loads(q["scrape_gap_targets"]) == ["10.0.0.7"]


def test_scrape_gap_fatal_when_requested(make_settings, make_deps):
    outcome = BatchOrchestrator(make_settings(fail_on_scrape_gap=True),
                                make_deps(prometheus_client=make_prometheus(gap_targets=["x"]))).run()
    assert outcome.failed_step == "traffic_collector"


def test_tier2_error_fails_trial_and_attempt_hidden_from_current_state(make_settings, make_deps):
    settings = make_settings()
    outcome = BatchOrchestrator(settings, make_deps(tier2_runner=ScriptedTier2Runner(fail_on_call=1))).run()
    assert outcome.status == "FAILED" and outcome.failed_step == "tier2"
    assert _evidence(settings.db_path, outcome.attempt_id)  # đã ghi 1 phần — giữ để truy vết
    assert read_rows(settings.db_path, "SELECT * FROM dependency_current_state") == []


# ---------------------------------------------------------------------------
# Tier 2
# ---------------------------------------------------------------------------


def test_each_escalated_edge_gets_its_own_session_linked_back(make_settings, make_deps):
    runner = ScriptedTier2Runner(by_destination={"shippingservice": "CONFLICTING_EVIDENCE"})
    settings = make_settings()
    BatchOrchestrator(settings, make_deps(tier2_runner=runner)).run()
    session_ids = [sid for _d, sid in runner.calls]
    assert len(session_ids) == 2 and len(set(session_ids)) == 2
    escalated = [r for r in _evidence(settings.db_path, "trial-01.a1") if r["escalate_to_tier2"]]
    for row in escalated:
        assert row["tier2_session_id"] == tier2_session_id("trial-01.a1", row["identity_key"])
    weak = next(r for r in escalated if r["destination_canonical_service"] == "shippingservice")
    assert weak["tier2_status"] == "CONFLICTING_EVIDENCE"
    non_escalated = [r for r in _evidence(settings.db_path, "trial-01.a1") if not r["escalate_to_tier2"]]
    assert all(r["tier2_status"] is None for r in non_escalated)


def test_escalation_dict_is_valid_tier1_section_10_schema(make_settings, make_deps):
    runner = ScriptedTier2Runner()
    BatchOrchestrator(make_settings(), make_deps(tier2_runner=runner)).run()
    for d, sid in runner.calls:
        record = EscalationRecord.from_dict(d)
        assert record.should_escalate()
        assert sid.endswith(tier2_session_id("x", record.identity_key).split("#")[1])


def test_skip_tier2_is_recorded_explicitly(make_settings, make_deps):
    settings = make_settings()
    outcome = BatchOrchestrator(settings, make_deps(tier2_runner=None)).run()
    assert outcome.status == "SUCCESS"
    detail = json.loads(read_rows(settings.db_path, "SELECT detail FROM pipeline_run_log WHERE step = 'tier2' "
                                                    "AND status = 'SUCCESS'")[0]["detail"])
    assert detail["skipped"] is True and detail["escalated"] == 2
    assert all(r["tier2_status"] is None for r in _evidence(settings.db_path, outcome.attempt_id))


def test_real_brain_writes_rounds_into_same_db(make_settings, make_deps, infra_files):
    """Brain thật (ScriptedModelClient) ghi agent_sessions/agent_rounds/trace_log vào CÙNG
    file SQLite; session_id trỏ ngược đúng cạnh (DASHBOARD_DESIGN.md mục 4)."""
    from tier2_agent.agent_runtime import AgentRuntime
    from tier2_agent.brain import Brain
    from tier2_agent.config import Tier2Config
    from tier2_agent.llm.client import ScriptedModelClient
    from tier2_agent.storage import Storage
    from tier2_agent.tool_registry import ToolRegistry
    from tier2_agent.tools.traffic_window import make_traffic_window_tool

    from orchestration.wiring import BrainTier2Runner, PrometheusTrafficWindowProvider

    def script(ctx, tool_names):
        if not ctx.hypotheses:
            return {"reasoning": "kiểm cửa sổ dài hơn", "action": "call_tool", "stance_updates": [],
                    "new_hypotheses": [{"id": "H1", "statement": "chu kỳ dài"}],
                    "tool_calls": [{"tool_name": "read_extended_traffic_window",
                                    "tool_input": {"edge": _edge[0], "window": 14},
                                    "hypothesis_id": "H1"}]}
        return {"reasoning": "xong", "action": "conclude", "stance_updates": [], "tool_calls": []}

    _edge: list = []

    class _Runner(BrainTier2Runner):
        def run(self, escalation_dict, session_id):
            _edge[:] = [EscalationRecord.from_dict(escalation_dict).identity_key]
            return super().run(escalation_dict, session_id)

    config = Tier2Config(max_iterations=4, session_timeout_s=999999)
    registry = ToolRegistry(config, {"read_extended_traffic_window": make_traffic_window_tool(
        PrometheusTrafficWindowProvider(make_prometheus(), [NS]))})
    brain = Brain(config, registry, Storage(str(infra_files["db_path"])),
                  AgentRuntime(ScriptedModelClient(script_fn=script)))
    settings = make_settings()
    outcome = BatchOrchestrator(settings, make_deps(tier2_runner=_Runner(brain))).run()

    assert outcome.status == "SUCCESS", outcome.error_message
    for row in [r for r in _evidence(settings.db_path, outcome.attempt_id) if r["escalate_to_tier2"]]:
        session = read_rows(settings.db_path, "SELECT * FROM agent_sessions WHERE run_id = ?", (row["tier2_session_id"],))
        assert session and session[0]["edge_identity_key"] == row["identity_key"]
        rounds = read_rows(settings.db_path, "SELECT * FROM agent_rounds WHERE run_id = ?", (row["tier2_session_id"],))
        assert len(rounds) == 2
        assert "reasoning_injection_flagged" in rounds[0] and rounds[0]["system_notices"] is not None
        assert row["tier2_status"] is not None


def test_traffic_window_provider_aggregates_one_edge():
    from orchestration.wiring import PrometheusTrafficWindowProvider

    from .conftest import TRAFFIC

    long_traffic = [(s, d, p, total, days) for s, d, p, total, days in TRAFFIC]
    provider = PrometheusTrafficWindowProvider(make_prometheus(long_traffic, window_days=14), [NS],
                                               now=lambda: NOW)
    stats = provider.fetch_extended_window(
        f"{NS}|adservice|adservice|{NS}|paymentservice|GRPC|50051", 14)
    assert stats["count"] == 7000 and stats["distinct_days"] == 7 and stats["protocol_diversity"] == 1
    assert provider.fetch_extended_window("không|khớp|gì|cả|x|GRPC|1", 14)["count"] == 0


# ---------------------------------------------------------------------------
# Chạy lại + tính độc lập giữa các trial
# ---------------------------------------------------------------------------


def test_rerun_creates_new_attempt_and_current_state_uses_latest_success(make_settings, make_deps):
    settings = make_settings()
    BatchOrchestrator(settings, make_deps()).run()
    second = BatchOrchestrator(settings, make_deps(tier2_runner=ScriptedTier2Runner("INSUFFICIENT_BASIS"))).run()
    assert second.attempt_id == "trial-01.a2"
    current = read_rows(settings.db_path, "SELECT DISTINCT attempt_id FROM dependency_current_state")
    assert [r["attempt_id"] for r in current] == ["trial-01.a2"]
    assert len(_evidence(settings.db_path, "trial-01.a1")) == len(_evidence(settings.db_path, "trial-01.a2"))


def test_failed_rerun_keeps_previous_success_as_official(make_settings, make_deps):
    settings = make_settings()
    BatchOrchestrator(settings, make_deps()).run()
    BatchOrchestrator(settings, make_deps(deployment_clock=lambda _ns: None)).run()
    current = read_rows(settings.db_path, "SELECT DISTINCT attempt_id FROM dependency_current_state")
    assert [r["attempt_id"] for r in current] == ["trial-01.a1"]


def test_stateless_default_trials_do_not_share_hysteresis(make_settings, make_deps):
    """Hạ tầng dùng chung: trial-02 có CÙNG identity_key với trial-01 cho cạnh thật OB —
    stateless (mặc định) thì previous_status của trial-02 luôn None."""
    BatchOrchestrator(make_settings("trial-01"), make_deps()).run()
    settings = make_settings("trial-02")
    BatchOrchestrator(settings, make_deps()).run()
    assert all(r["previous_status"] is None for r in _evidence(settings.db_path, "trial-02.a1"))


def test_stateful_uses_only_own_trial_lineage(make_settings, make_deps):
    BatchOrchestrator(make_settings("trial-02"), make_deps()).run()   # lịch sử của trial KHÁC
    settings = make_settings("trial-01", stateful_tier1=True)
    BatchOrchestrator(settings, make_deps()).run()
    first = _evidence(settings.db_path, "trial-01.a1")
    real_edge = next(r for r in first if r["destination_canonical_service"] == "cartservice"
                     and r["source_canonical_service"] == "frontend")
    assert real_edge["previous_status"] is None  # không đọc lịch sử trial-02

    BatchOrchestrator(settings, make_deps()).run()
    second = _evidence(settings.db_path, "trial-01.a2")
    real_edge2 = next(r for r in second if r["identity_key"] == real_edge["identity_key"])
    assert real_edge2["previous_status"] == real_edge["dependency_status"]
