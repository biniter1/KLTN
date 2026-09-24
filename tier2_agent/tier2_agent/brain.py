"""`Brain` — thành phần trung tâm duy nhất của Tier 2 (TIER2_DESIGN.md mục 1).

Mọi thứ khác (Model, 4 tool, Context Manager, Trace log) chỉ là dữ liệu/hàm
mà Brain gọi/đọc/ghi. Không có "bộ não" thứ hai nào trong hệ thống.

Triển khai đúng pipeline 2 giai đoạn mỗi vòng (mục 8.1/8.2):

  Giai đoạn A: Builder① -> normalize -> format -> gọi Model
  Giai đoạn B: áp stance_updates -> Builder② -> Observer -> Validation
               -> Result Formatter -> rẽ nhánh dừng/tiếp tục

Brain xử lý ĐÚNG 1 cạnh/lần (1 phiên = 1 escalation record). Vòng đời dài
hơn (điều phối cả 1 trial R=15) là việc của Batch Orchestrator, ngoài phạm
vi gói này — xem `orchestrator_hook.py` cho điểm nối.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from . import context_manager
from .agent_runtime import AgentRuntime
from .config import Tier2Config
from .escalation import EscalationRecord
from .exceptions import InvalidEscalationError, ToolExecutionError, ToolInputRejected, UnknownToolError
from .models import (
    EvidenceRow,
    Hypothesis,
    ModelAction,
    RoundResult,
    SessionState,
    Stance,
    ToolRowStatus,
)
from .observer import ObserverInputs, check as observer_check
from .result_formatter import format_result
from .storage import Storage
from .tool_registry import ToolRegistry
from .utils import cap_size, canonical_json_key, new_id, now_epoch

_VALID_STANCES = {s.value for s in Stance}


class Brain:
    def __init__(
        self,
        config: Tier2Config,
        tool_registry: ToolRegistry,
        storage: Storage,
        agent_runtime: AgentRuntime,
    ):
        self._config = config
        self._tools = tool_registry
        self._storage = storage
        self._runtime = agent_runtime

    def run_session(self, escalation: EscalationRecord, run_id: str) -> RoundResult:
        if not escalation.should_escalate():
            raise InvalidEscalationError(
                f"dependency_status={escalation.dependency_status!r} / "
                f"config_finding={escalation.config_finding!r} không thỏa điều kiện "
                "kích hoạt Tier 2 (TIER2_DESIGN.md mục 0)"
            )

        state = SessionState(
            run_id=run_id,
            edge_identity_key=escalation.identity_key,
            session_started_at=now_epoch(),
        )
        self._storage.create_session(state)

        hypotheses: Dict[str, Hypothesis] = {}
        consecutive_errors: Dict[tuple, int] = {}
        pending_notices: List[str] = []
        tool_names = [t.name for t in self._tools.available_tools()]

        iteration = 0
        while True:
            iteration += 1

            # ---------------- Giai đoạn A (mục 8.1) ----------------
            trace_rows = self._storage.get_trace_rows(run_id)
            known_evidence_ids = {r.evidence_id for r in trace_rows}

            views1 = context_manager.build(trace_rows, hypotheses)
            views1 = context_manager.normalize(views1, self._config)
            ctx = context_manager.format_context(
                views1,
                escalation=escalation,
                available_tools=self._tools.available_tools(),
                escalation_reason=escalation.escalation_reason,
                hypothesis_cap_reached=len(hypotheses) >= self._config.max_open_hypotheses,
                system_notices=pending_notices,
            )
            pending_notices = []

            model_output = self._runtime.call(ctx, tool_names)

            # check_injection cũng quét reasoning TRƯỚC khi ghi log (mục 4)
            inj_result = context_manager.check_injection(model_output.reasoning)
            model_output.reasoning = inj_result.text

            # ---------------- Giai đoạn B (mục 8.2) ----------------
            for su in model_output.stance_updates:
                if su.evidence_id not in known_evidence_ids:
                    pending_notices.append(
                        f"evidence_id {su.evidence_id!r} không tồn tại — stance_update bị từ chối"
                    )
                    continue
                if su.stance not in _VALID_STANCES:
                    pending_notices.append(
                        f"stance {su.stance!r} không hợp lệ cho evidence_id {su.evidence_id!r}"
                    )
                    continue
                self._storage.update_stance(su.evidence_id, su.stance)

            # Cắt trần tool_calls/vòng TRƯỚC khi xét "hypothesis mới có kèm
            # tool_calls cùng vòng không" (mục 7.1) — nếu làm ngược lại, 1
            # hypothesis mới mà tool_call duy nhất của nó bị cắt bởi trần sẽ
            # vẫn được chấp nhận dù thực tế không có tool_call nào chạy cho
            # nó trong vòng này, vi phạm đúng luật cứng mục 7.1.
            requested = model_output.tool_calls
            if len(requested) > self._config.max_tool_calls_per_round:
                overflow_n = len(requested) - self._config.max_tool_calls_per_round
                requested = requested[: self._config.max_tool_calls_per_round]
                pending_notices.append(
                    f"{overflow_n} tool_calls vượt trần {self._config.max_tool_calls_per_round}/vòng bị bỏ qua (anomaly)"
                )

            tool_call_targets = {tc.hypothesis_id for tc in requested}
            for nh in model_output.new_hypotheses:
                if len(hypotheses) >= self._config.max_open_hypotheses:
                    pending_notices.append(
                        f"hypothesis {nh.id!r} bị từ chối — đã chạm trần "
                        f"{self._config.max_open_hypotheses} hypothesis mở song song"
                    )
                    continue
                if nh.id in hypotheses:
                    pending_notices.append(f"hypothesis {nh.id!r} đã tồn tại — bỏ qua khai báo trùng")
                    continue
                if nh.id not in tool_call_targets:
                    pending_notices.append(
                        f"hypothesis {nh.id!r} bị từ chối — phải kèm tool_calls trong CÙNG vòng mở nó (mục 7.1)"
                    )
                    continue
                hypotheses[nh.id] = Hypothesis(id=nh.id, statement=nh.statement)

            dispatched_new_count = 0
            attempted_count = 0  # tool thật sự được GỌI vòng này (kể cả lỗi) — xem ghi chú Observer bên dưới
            for tc in requested:
                if tc.hypothesis_id not in hypotheses:
                    pending_notices.append(
                        f"hypothesis_id {tc.hypothesis_id!r} không tồn tại — tool_call {tc.tool_name!r} bị từ chối"
                    )
                    continue

                try:
                    self._tools.validate_input(
                        tc.tool_name,
                        tc.tool_input,
                        allowed_services=escalation.canonical_services,
                        edge_identity_key=escalation.identity_key,
                    )
                except (ToolInputRejected, UnknownToolError) as exc:
                    self._storage.append_trace_row(
                        EvidenceRow(
                            evidence_id=new_id("E"),
                            run_id=run_id,
                            iteration=iteration,
                            hypothesis_id=tc.hypothesis_id,
                            tool_name=tc.tool_name,
                            tool_input=tc.tool_input,
                            tool_output=None,
                            status=ToolRowStatus.ERROR.value,
                            error_message=str(exc),
                        )
                    )
                    pending_notices.append(f"tool_call {tc.tool_name!r} bị từ chối: {exc}")
                    continue

                dedup_key_str = canonical_json_key(tc.tool_input)
                existing = self._storage.find_dedup_row(run_id, tc.tool_name, dedup_key_str)
                if existing is not None:
                    pending_notices.append(
                        f"tool_call {tc.tool_name!r}{tc.tool_input!r} trùng lệnh đã chạy trước "
                        f"(evidence_id={existing.evidence_id}) — dùng lại kết quả cũ, không chạy lại"
                    )
                    continue

                error_key = (tc.tool_name, dedup_key_str)
                if consecutive_errors.get(error_key, 0) >= self._config.max_consecutive_tool_errors:
                    pending_notices.append(
                        f"tool_call {tc.tool_name!r}{tc.tool_input!r} đã lỗi "
                        f"{self._config.max_consecutive_tool_errors} lần liên tiếp — "
                        "needs_admin_attention=true, không cho thử lại"
                    )
                    continue

                attempted_count += 1
                try:
                    result = self._tools.execute(tc.tool_name, tc.tool_input)
                except ToolExecutionError as exc:
                    consecutive_errors[error_key] = consecutive_errors.get(error_key, 0) + 1
                    needs_admin = consecutive_errors[error_key] >= self._config.max_consecutive_tool_errors
                    self._storage.append_trace_row(
                        EvidenceRow(
                            evidence_id=new_id("E"),
                            run_id=run_id,
                            iteration=iteration,
                            hypothesis_id=tc.hypothesis_id,
                            tool_name=tc.tool_name,
                            tool_input=tc.tool_input,
                            tool_output=None,
                            status=ToolRowStatus.ERROR.value,
                            error_message=str(exc),
                        )
                    )
                    note = f"tool_call {tc.tool_name!r} lỗi: {exc}"
                    if needs_admin:
                        note += " (needs_admin_attention=true)"
                    pending_notices.append(note)
                    continue

                consecutive_errors[error_key] = 0
                sanitized_output, sanitized_flag = context_manager.sanitize(result.raw_output)
                if sanitized_flag:
                    clean_output, injection_flag = sanitized_output, False
                else:
                    inj = context_manager.check_injection(sanitized_output)
                    clean_output, injection_flag = inj.text, inj.flagged
                capped_output, truncated = cap_size(clean_output, self._config.tool_output_char_cap)

                self._storage.append_trace_row(
                    EvidenceRow(
                        evidence_id=new_id("E"),
                        run_id=run_id,
                        iteration=iteration,
                        hypothesis_id=tc.hypothesis_id,
                        tool_name=tc.tool_name,
                        tool_input=tc.tool_input,
                        tool_output=capped_output,
                        stance=None,
                        stale=result.stale,
                        status=ToolRowStatus.OK.value,
                        injection_flagged=injection_flag,
                        sanitized_flagged=sanitized_flag,
                        truncated=truncated,
                        pr_number=result.pr_number,
                    )
                )
                dispatched_new_count += 1

            # Builder② — evidence giờ có stance thật (những evidence NHẬN Ở
            # VÒNG TRƯỚC vừa được stance_updates cập nhật; evidence mới dispatch
            # vòng này vẫn stance=None, Model sẽ đánh giá ở vòng sau)
            trace_rows_2 = self._storage.get_trace_rows(run_id)
            views2 = context_manager.build(trace_rows_2, hypotheses)

            session_now = self._storage.get_session(run_id)
            observer_inputs = ObserverInputs(
                iteration_count=self._storage.iteration_count(run_id) + 1,  # round này sắp được ghi
                elapsed_ms=(now_epoch() - state.session_started_at) * 1000,
                manual_abort_flag=bool(session_now and session_now.manual_abort_flag),
                # "Cạn tool" (mục 9.1) nghĩa là KHÔNG CÒN gì để thử — 1 tool
                # thật sự được gọi nhưng lỗi vẫn là tiến triển (Model có
                # observation mới để phản ứng ở vòng sau, mục 9.3); chỉ khi
                # MỌI tool_call đều bị dedup/từ chối trước khi kịp gọi mới
                # coi là hết đường đi tiếp.
                made_progress_this_round=attempted_count > 0,
                model_action=model_output.action,
            )
            decision = observer_check(observer_inputs, self._config)

            round_result = format_result(state, views2, iteration, decision, model_output)

            self._storage.append_agent_round(
                run_id,
                iteration,
                model_output,
                stopped_by=decision.stopped_by,
                stop_reason=decision.stop_reason,
            )

            if decision.stop:
                self._storage.set_phase(run_id, "CONCLUDED")
                return round_result

            # tiếp tục vòng sau — "Valid result" sống, KHÔNG lưu, chỉ dùng để
            # hiển thị dashboard real-time (mục 8.2); vòng sau tự đọc lại
            # trace_log/hypotheses mới nhất nên không cần truyền round_result
            # tay qua vòng lặp.
