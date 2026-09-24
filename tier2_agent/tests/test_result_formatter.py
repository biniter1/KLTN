from tier2_agent.models import (
    ModelOutput,
    ObserverDecision,
    SessionState,
    StoppedBy,
)
from tier2_agent.result_formatter import format_result


def test_persisted_true_only_when_observer_stops():
    state = SessionState(run_id="r1", edge_identity_key="k", session_started_at=0.0)
    mo = ModelOutput(reasoning="r", action="call_tool")

    continuing = format_result(state, {}, 1, ObserverDecision(False), mo)
    assert continuing.persisted is False
    assert continuing.stopped is False

    stopped = format_result(
        state, {}, 2, ObserverDecision(True, StoppedBy.MODEL.value, None), mo
    )
    assert stopped.persisted is True
    assert stopped.stopped is True
    assert stopped.stopped_by == StoppedBy.MODEL.value


def test_result_carries_reasoning_and_final_status_empty_hypotheses():
    state = SessionState(run_id="r1", edge_identity_key="k", session_started_at=0.0)
    mo = ModelOutput(reasoning="lý do kết luận", action="conclude")
    result = format_result(state, {}, 1, ObserverDecision(True, StoppedBy.MODEL.value, None), mo)
    assert result.reasoning == "lý do kết luận"
    assert result.final_status == "INSUFFICIENT_BASIS"
    assert result.run_id == "r1"
