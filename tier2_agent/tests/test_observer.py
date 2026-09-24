from tier2_agent.config import Tier2Config
from tier2_agent.models import StoppedBy
from tier2_agent.observer import ObserverInputs, check


def _inputs(**kwargs):
    base = dict(
        iteration_count=1,
        elapsed_ms=0,
        manual_abort_flag=False,
        made_progress_this_round=True,
        model_action="call_tool",
    )
    base.update(kwargs)
    return ObserverInputs(**base)


def test_continues_when_nothing_triggers_stop():
    cfg = Tier2Config(max_iterations=8, session_timeout_s=300)
    decision = check(_inputs(), cfg)
    assert decision.stop is False
    assert decision.stopped_by is None


def test_stops_on_manual_abort_even_if_model_wants_to_continue():
    cfg = Tier2Config()
    decision = check(_inputs(manual_abort_flag=True), cfg)
    assert decision.stop is True
    assert decision.stopped_by == StoppedBy.OBSERVER_POLICY.value
    assert decision.stop_reason == "manual abort"


def test_stops_on_max_iterations():
    cfg = Tier2Config(max_iterations=3)
    decision = check(_inputs(iteration_count=3), cfg)
    assert decision.stop is True
    assert decision.stopped_by == StoppedBy.OBSERVER_POLICY.value
    assert "max_iterations" in decision.stop_reason


def test_stops_on_session_timeout():
    cfg = Tier2Config(session_timeout_s=60)
    decision = check(_inputs(elapsed_ms=60_001), cfg)
    assert decision.stop is True
    assert "timeout" in decision.stop_reason


def test_stops_when_no_new_tool_calls_dispatched():
    cfg = Tier2Config(max_iterations=8, session_timeout_s=999999)
    decision = check(
        _inputs(model_action="call_tool", made_progress_this_round=False), cfg
    )
    assert decision.stop is True
    assert decision.stopped_by == StoppedBy.OBSERVER_POLICY.value
    assert "exhausted" in decision.stop_reason


def test_no_progress_does_not_stop_when_action_is_conclude():
    cfg = Tier2Config(max_iterations=8, session_timeout_s=999999)
    decision = check(
        _inputs(model_action="conclude", made_progress_this_round=False), cfg
    )
    assert decision.stop is True
    assert decision.stopped_by == StoppedBy.MODEL.value


def test_stops_on_model_conclude():
    cfg = Tier2Config(max_iterations=8, session_timeout_s=999999)
    decision = check(_inputs(model_action="conclude"), cfg)
    assert decision.stop is True
    assert decision.stopped_by == StoppedBy.MODEL.value
    assert decision.stop_reason is None


def test_policy_conditions_take_priority_over_model_conclude():
    cfg = Tier2Config(max_iterations=2)
    decision = check(_inputs(iteration_count=2, model_action="conclude"), cfg)
    assert decision.stopped_by == StoppedBy.OBSERVER_POLICY.value
