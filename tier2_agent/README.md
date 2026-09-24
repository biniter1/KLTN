# Tier 2 (AI Agent) — cài đặt tham chiếu theo `TIER2_DESIGN.md`

Gói này cài đặt Tier 2 — tầng suy luận thứ hai và cuối cùng của KLTN
(2-tier dependency inference cho Kubernetes NetworkPolicy generation), theo
đúng bản thiết kế đã chốt `TIER2_DESIGN.md` (kiến trúc "Brain"-centered, 4
tool có input+output validation, Context Manager pipeline, hypothesis-driven
reasoning, pipeline 2 giai đoạn/vòng).

Cùng chuẩn chất lượng như gói Tier 1 đã giao trước: code trước, test sau
(fixture/factory kiểu `conftest.py`), 172 unit test, ~97% coverage dòng lệnh
(`pytest.ini` bật `--cov=tier2_agent --cov-report=term-missing`).

## Chạy thử

```bash
pip install -r requirements-dev.txt
pytest
```

`requirements.txt` liệt kê 3 thư viện ngoài cần cho code chạy thật:
`anthropic` (gọi Model), `jsonschema` (ép + validate Model Output),
`requests` (gatekeeper PR description qua HTTP). Bộ test **không** cần
network hay API key thật — mọi phần gọi LLM/HTTP/Prometheus đều đi qua
interface có thể tiêm fake/scripted (xem `tests/conftest.py`).

## Cấu trúc code — ánh xạ tới TIER2_DESIGN.md

| Mục thiết kế | Nội dung | File code |
|---|---|---|
| 0 | Điều kiện kích hoạt Tier 2, escalation_reason | `tier2_agent/escalation.py` |
| 1 | Kiến trúc "Brain" | `tier2_agent/brain.py` |
| 1 | Agent runtime (lớp mỏng gọi Model) | `tier2_agent/agent_runtime.py`, `tier2_agent/llm/client.py` |
| 2, 2.1, 3 | 4 tool + input validation + Tool Registry | `tier2_agent/tool_registry.py`, `tier2_agent/tools/*.py` |
| 4 | Context Manager: `sanitize`, `check_injection`, `build`, `normalize`, `format` | `tier2_agent/security.py`, `tier2_agent/context_manager.py` |
| 5 | State | `tier2_agent/models.SessionState` (+ `agent_sessions` trong `storage.py`) |
| 6 | Model Output (schema) | `tier2_agent/llm/schemas.py`, `tier2_agent/agent_runtime.py` |
| 7 | Hypothesis-driven reasoning (5 trạng thái, 4 status cuối, ordinal reliability, evidence độc lập) | `tier2_agent/validation.py`, `tier2_agent/models.py` |
| 8.1, 8.2 | Pipeline 2 giai đoạn/vòng | `tier2_agent/brain.py` (`run_session`) |
| 9 | Điều kiện dừng, Observer, xử lý lỗi tool | `tier2_agent/observer.py`, `tier2_agent/brain.py` |
| 10 | Whitelist/Policy bảo mật | `tier2_agent/tool_registry.py` + `tier2_agent/brain.py` (dedup/cap/reference validation) |
| 11 | Lưu trữ SQLite WAL — `trace_log`, `agent_rounds` | `tier2_agent/storage.py` |
| 11.3 | Cập nhật `dependency_evidence` (điểm nối Batch Orchestrator) | `tier2_agent/orchestrator_hook.py` |
| 12 | Ngưỡng số | `tier2_agent/config.py` (`Tier2Config`) |

Test soi gương cấu trúc trên trong `tests/` (`tests/tools/`, `tests/llm/`,
`tests/test_brain_scenarios.py` cho kịch bản end-to-end).

## Cách wiring vào hệ thống thật

```python
from tier2_agent.brain import Brain
from tier2_agent.agent_runtime import AgentRuntime
from tier2_agent.llm.client import AnthropicModelClient
from tier2_agent.tool_registry import ToolRegistry
from tier2_agent.storage import Storage
from tier2_agent.escalation import EscalationRecord
from tier2_agent.orchestrator_hook import apply_tier2_result
from tier2_agent.tools.source_code import make_source_code_tool
from tier2_agent.tools.internal_docs import make_internal_docs_tool
from tier2_agent.tools.pr_descriptions import CachingGatekeeperClient, make_http_gatekeeper_fetch, make_pr_descriptions_tool
from tier2_agent.tools.traffic_window import make_traffic_window_tool

config = Tier2Config()  # hoặc override sau khi có số liệu pilot (mục 12)

service_dirs = {"frontend": "/checkout/online-boutique/src/frontend", ...}
gatekeeper = CachingGatekeeperClient(make_http_gatekeeper_fetch("http://api-server:8080"))
# Batch Orchestrator: gọi gatekeeper.reset_cache() khi bắt đầu 1 trial MỚI (mục 2)

registry = ToolRegistry(config, {
    "read_source_code": make_source_code_tool(service_dirs, stale_threshold_days=config.stale_threshold_days),
    "read_internal_docs": make_internal_docs_tool(service_dirs, stale_threshold_days=config.stale_threshold_days),
    "read_pr_descriptions": make_pr_descriptions_tool(gatekeeper, service_dirs),
    "read_extended_traffic_window": make_traffic_window_tool(your_prometheus_provider),
})

storage = Storage("tier2.sqlite3")  # cùng file SQLite WAL với Batch Orchestrator/API Server
runtime = AgentRuntime(AnthropicModelClient(api_key=os.environ["ANTHROPIC_API_KEY"]))
brain = Brain(config, registry, storage, runtime)

escalation = EscalationRecord.from_dict(tier1_escalation_schema_dict)  # nguyên schema mục 10, không sửa
result = brain.run_session(escalation, run_id=trial_run_id)

apply_tier2_result(dependency_evidence_conn, escalation, trial_run_id, result)  # mục 11.3
```

## Điểm mở / diễn giải (thiết kế không đặc tả đủ chi tiết để code)

Toàn bộ các điểm dưới đây được đánh dấu bằng `NOTE` ngay tại chỗ trong code
(`grep -rn "Điểm mở" tier2_agent/` để xem lại nhanh), liệt kê lại đây để dễ
đối chiếu khi bảo vệ khóa luận:

1. **Whitelist `source_service`/`service` — phạm vi cạnh hay toàn bộ Tier 1
   cung cấp?** (mục 13, câu hỏi treo). Đã chọn phương án CHẶT: chỉ 2
   canonical_service của đúng cạnh đang xác minh
   (`escalation.py::EscalationRecord.canonical_services`). An toàn hơn,
   và không cần thông tin gì ngoài escalation record.
2. **Bảng lưu 5 field State "LƯU THẬT" (mục 5)** — thiết kế liệt kê field
   nhưng không đặt tên 1 bảng cụ thể. Đã thêm bảng `agent_sessions`
   (`storage.py`) để `manual_abort_flag` có chỗ ghi bất đồng bộ từ nút bấm
   dashboard.
3. **Thuật toán phát hiện "cạn tool" (mục 9.1)** — không có đặc tả chính
   xác. Đã diễn giải: 1 vòng mà Model xin `call_tool` nhưng KHÔNG có tool
   nào thật sự được GỌI (mọi yêu cầu đều bị dedup/từ chối trước khi tới
   bước thực thi) thì coi là hết đường đi tiếp (`observer.py`). Một tool
   được gọi nhưng LỖI vẫn tính là có tiến triển — Model cần thấy lỗi đó để
   quyết định retry/pivot (mục 9.3), tách bạch với "không gọi được gì cả".
4. **`original_agent_status` (mục 7.4, 11.3)** — Model Output (mục 6) không
   có field tự báo status riêng. Diễn giải: đây là status sẽ có NẾU KHÔNG
   áp luật corroboration (bằng chứng cấu trúc) — tức luôn bằng `final_status`
   TRỪ trường hợp 1 hypothesis SUPPORTED chỉ có bằng chứng phi cấu trúc, khi
   đó `original_agent_status=CONFIDENT` nhưng `final_status` bị hạ xuống
   `NEEDS_MONITORING`. Khớp đúng nghĩa "trước khi bị corroboration hạ cấp".
5. **`read_extended_traffic_window` có bắt buộc qua gatekeeper API Server
   như `read_pr_descriptions` không?** Thiết kế chỉ nêu tường minh gatekeeper
   cho tool PR. Đã để `TrafficProvider` là interface pluggable
   (`tools/traffic_window.py`) — người tích hợp tự quyết định gọi thẳng
   Prometheus hay qua gatekeeper.
6. **Độ tin cậy đơn vị "token"** (mục 4, ngân sách 70% context window) —
   không dùng tokenizer thật của 1 nhà cung cấp cụ thể, ước lượng thô
   `chars_per_token_estimate` (mặc định 4). Đủ cho mục đích rule-based cắt
   evidence cũ nhất trước; nếu cần chính xác hơn, thay `_estimate_tokens`
   trong `context_manager.py` bằng tokenizer thật.
7. **`dependency_evidence` schema thật** — Tier 2 không sở hữu bảng này
   (Batch Orchestrator/Tier 1 sở hữu, `ORCHESTRATION_DESIGN.md` mục 3).
   `orchestrator_hook.py` chỉ giả định 7 cột identity (đúng tên field mục 10
   của Tier 1) + tự thêm 2 cột `tier2_status`/`tier2_original_status` nếu
   chưa có — không tự tạo bảng gốc trừ khi gọi `create_if_missing=True`
   (chỉ dành cho demo/test độc lập gói này).
8. **`read_pr_descriptions`/`read_source_code` trỏ cùng 1 PR number = 1
   nguồn** (mục 7.5) — cần biết PR number của mỗi evidence. `read_source_code`
   dò PR number bằng regex trên comment khớp keyword (`tools/_shared.py:
   extract_pr_numbers`) — heuristic đơn giản, không đảm bảo bắt hết mọi cách
   ghi chú PR trong code thật.

## Giới hạn đã biết (kế thừa từ thiết kế, không phải bug)

- Rule-based injection detection có recall thấp hơn credential detection —
  đúng như mục 4 đã nêu, đây là giới hạn CHẤP NHẬN ĐƯỢC, không phải lỗi.
- 3 cơ chế ở mục 13 (Bayesian log-odds, self-consistency k=5, migration wave
  weighting) — KHÔNG cài đặt, đúng theo "ngoài phạm vi PoC / còn treo".
- Ngưỡng số ở `config.py` là placeholder chờ đo thật (percentile-95 trên
  pilot run), không phải giá trị đã kiểm định — giống hệt tinh thần
  `θ_strong` của Tier 1.

## Coverage chưa tới 100% — lý do cụ thể (không phải bỏ sót)

`pytest` báo ~97% dòng lệnh. Phần còn lại:
- `brain.py` dòng kiểm tra `stance` không nằm trong `{support, contradict}`:
  không thể kích hoạt qua API công khai vì `agent_runtime.py` luôn ép
  `jsonschema` với `enum` cố định trước khi Model Output tới được `Brain` —
  giữ lại làm phòng thủ lớp 2 (Zero Trust: "never trust, always verify") cho
  trường hợp thay bằng 1 `ModelClient` khác không tuân thủ schema.
- Vài nhánh lỗi cấp hệ điều hành trong `tools/_shared.py` (timeout `git`,
  thiếu `grep`/`git` trong PATH) — khó tái tạo quyết định trong CI thông
  thường mà không làm hỏng môi trường test.
