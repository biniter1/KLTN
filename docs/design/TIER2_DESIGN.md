# Thiết kế chi tiết — Tier 2 (AI Agent)

Tài liệu này là bản thiết kế chi tiết chính thức cho Tier 2 — tầng suy luận thứ hai và cuối cùng trong kiến trúc hai tầng (xem `PROPOSAL.md`), chỉ được gọi khi Tier 1 (`TIER1_DESIGN.md`) không tự kết luận được. Tier 2 xử lý bằng chứng **phi cấu trúc** (code, PR description, tài liệu nội bộ) mà Tier 1 và Config Evidence Collector (`CONFIG_EVIDENCE_COLLECTOR.md`) không chạm tới.

Tài liệu này thay thế toàn bộ ghi chú rời rạc trước đó về Tier 2 — là bản chốt sau nhiều vòng stress-test.

**Phân biệt với bước LLM trong Config Evidence Collector v2 (29/09)**: Collector v2 cũng gọi LLM, nhưng chỉ là **1 lần gọi trích xuất có schema** trên config K8s — không tool, không vòng lặp, không hypothesis, không kết luận dependency có thật hay không (validator tất định quyết). Tier 2 vẫn là nơi **duy nhất** có agent suy luận nhiều vòng; luồng tier vẫn một chiều Tier 1 → Tier 2.

---

## 0. Vai trò và điều kiện kích hoạt

Tier 2 được gọi bởi `Batch Orchestrator` (`ORCHESTRATION_DESIGN.md` mục 1, bước 6) khi một cạnh dependency rơi vào 1 trong 3 trạng thái:

- `OBSERVED_WEAK` — có traffic nhưng điểm thấp
- `UNKNOWN` — có traffic nhưng `traffic_class` mâu thuẫn/không rõ
- `NOT_OBSERVED` với config cũng mù (`FOUND_UNRESOLVED`/`NOT_FOUND`)

**Input**: nguyên schema evidence mục 10 của `TIER1_DESIGN.md` — không sửa, không thêm/bớt field.

**Output**: 1 trong 4 trạng thái cuối — `CONFIDENT`, `NEEDS_MONITORING`, `INSUFFICIENT_BASIS`, `CONFLICTING_EVIDENCE` — trả về `Batch Orchestrator`.

---

## 1. Kiến trúc tổng quan — "Brain"

**Brain** là thành phần trung tâm duy nhất của Tier 2 — mọi thứ khác (Model, 4 tool, Context Manager, Trace log) đều chỉ là dữ liệu hoặc hàm mà `Brain` gọi/đọc/ghi. Không có "bộ não" thứ hai nào trong hệ thống.

Chức năng cụ thể của `Brain`, không chồng lấn với bất kỳ thành phần nào khác:

1. Nhận request từ `Batch Orchestrator`, khởi tạo `State` cho 1 phiên (1 cạnh escalate).
2. Chạy Observer stop-check mỗi vòng (mục 9) — quyết định duy nhất "tiếp tục hay dừng".
3. Định tuyến `tool_calls` từ Model Output: kiểm tra whitelist/policy (mục 10), dedup, dispatch tới `Tool Registry`, xử lý lỗi tool (mục 9.3).
4. Gọi 5 hàm của `Context Manager` (mục 4) đúng thứ tự, đúng thời điểm.
5. Ghi tập trung vào 2 bảng: `agent_rounds` và `trace_log` (mục 11) — không thành phần nào khác được ghi trực tiếp vào 2 bảng này.
6. Khi dừng: gọi `Validation` → `Result Formatter` → trả `Final Result` về `Batch Orchestrator`.

**Quan hệ với `Batch Orchestrator`**: `Brain` có phạm vi nhỏ hơn hẳn — nó xử lý đúng 1 cạnh/lần, còn `Batch Orchestrator` điều phối cả 1 trial (R=15), gọi `Brain` như 1 bước trong chuỗi tuần tự của nó (`ORCHESTRATION_DESIGN.md` mục 1).

**`Agent runtime`**: 1 lớp mỏng, việc duy nhất là gọi Model với context `Brain` đã dựng — giữ tách riêng (không nhập vào `Brain`) để dễ test/đổi nhà cung cấp LLM, dù bản thân nó gần như không có logic gì.

**Câu hỏi kiến trúc còn bỏ ngỏ, không chặn triển khai**: `Brain` chạy trong tiến trình dài hạn (`API Server`) hay được `Batch Orchestrator` (ngắn hạn) khởi tạo mỗi lần escalate? Cả 2 lựa chọn đều hoạt động đúng với thiết kế lưu trữ ở mục 11 (SQLite WAL cho phép đọc/ghi đồng thời bất kể tiến trình nào) — quyết định này chỉ ảnh hưởng nơi đặt code, không ảnh hưởng logic.

---

## 2. Bộ 4 tool — read-only, kiểm soát cả input lẫn output

```
read_source_code(source_service, keyword)
    → quét code thật + comment + Dockerfile trong thư mục source, lọc theo keyword
read_pr_descriptions(service)
    → qua GitHub API, qua gatekeeper API Server (giữ token, Agent không bao giờ thấy)
read_internal_docs(service, keyword)
    → đọc README, lọc theo keyword
read_extended_traffic_window(edge, window)
    → mở rộng cửa sổ quan sát traffic
```

Tất cả đều `scope: read-only`. Không có tool nào ghi/thực thi.

### 2.1. Kiểm soát INPUT (tham số Model tự chọn) — thực hiện tại `Tool Registry`, trước khi `Tool` chạy

- **Whitelist**: `source_service`/`service` phải khớp 1 trong các `canonical_service` mà Tier 1 đã cung cấp trong schema của đúng cạnh đang xác minh. Không khớp → từ chối thẳng, không đoán/sanitize.
- **Policy**: `keyword` giới hạn độ dài + tập ký tự cho phép. Implementation bên dưới **bắt buộc** dùng `subprocess` với danh sách argument (`subprocess.run(["grep", keyword, path])`), **không** ghép chuỗi rồi chạy qua `shell=True` — chặn path-traversal/command-injection ở tầng hệ điều hành, không phụ thuộc việc lọc `keyword` có bắt hết ký tự nguy hiểm hay không.
- Từ chối → ghi 1 dòng `status: "ERROR"` vào `trace_log` (dùng chung cơ chế lỗi tool, mục 9.3), phản hồi lý do cho Model ở vòng sau — không âm thầm bỏ qua.

### 2.2. Kiểm soát OUTPUT (kết quả tool trả về) — xem mục 4 (Context Manager)

### 2.3. Rủi ro nhiễm dữ liệu huấn luyện (đã ghi nhận, có phương án)

Online Boutique là repo public nổi tiếng, nhiều khả năng nằm trong dữ liệu huấn luyện của LLM. Với các cạnh **gốc** (không phải tự cấy), Agent có thể kết luận đúng vì "thuộc lòng" chứ không phải suy luận thật từ evidence.

**Xử lý**: chỉ tính precision/recall chính thức của R=15 trên các cạnh **tự cấy** (tên tuỳ ý, không thể nằm trong dữ liệu huấn luyện). Cạnh gốc nếu cũng escalate lên Tier 2 thì ghi nhận riêng như 1 quan sát phụ, kèm chú thích rủi ro, không gộp vào số liệu chính. Cùng quy ước này áp dụng cho bước LLM extraction của Config Collector v2 (`CONFIG_EVIDENCE_COLLECTOR.md` mục 9.2).

---

## 3. Tool Registry

Module tĩnh giữ định nghĩa 4 tool: `{name, description, input_schema, scope}`. `description` là nguồn duy nhất — `Context Manager.build()` chỉ đọc từ đây để đưa vào `available_tools`, không viết lại ở nơi khác.

---

## 4. Context Manager — 5 hàm nội bộ

**Không phải tool, không phải service riêng** — là 1 module, `Brain` gọi trực tiếp trong cùng tiến trình, Model không thấy và không thể bỏ qua.

| Hàm | Chạy khi nào | Việc làm |
|---|---|---|
| `sanitize` | Ngay khi 1 tool trả kết quả, TRƯỚC khi ghi `trace_log` | Lọc credential (rule-based, pattern cố định vì tự kiểm soát format trong môi trường thử nghiệm). Khớp → **chặn hẳn** `tool_output`, ghi `"BLOCKED_SENSITIVE"` (không để trống im lặng — phân biệt được "có tìm thấy nhưng bị chặn" với "không tìm thấy gì") |
| `check_injection` ("Security check") | Cùng thời điểm với `sanitize`, và **cả trên `reasoning`** Model vừa sinh ra trước khi ghi vào `agent_rounds`/`trace_log` | Rule-based, phạm vi mở rộng theo bài học tham khảo (mục 10.3): (1) pattern injection đã biết, (2) entropy Shannon bất thường (nghi payload mã hoá), redact đúng đoạn khớp — KHÔNG chặn hẳn (injection phrasing vô hạn, chặn hết dễ mất evidence hợp lệ). Quét cả `reasoning` để chặn kịch bản Model bị dụ tự viết "chỉ thị" vào chính suy luận của nó rồi phát lại ở vòng sau |
| `build` ("Builder") | Gọi **2 lần/vòng**: lần ① trước khi gọi Model (evidence mới nhất có `stance: null`), lần ② sau khi Model trả `stance_updates` (đã có stance thật) | Đọc `trace_log` của phiên này, nén dòng rỗng/vô ích thành 1 dòng ngắn, nhóm theo `hypothesis_id`, tính `status` (mục 7.3) — thuần tính toán, không lưu kết quả |
| `normalize` | Sau `build`, chỉ khi vẫn dư ngân sách token | Cắt vòng cũ nhất trước, ưu tiên giữ evidence theo thứ hạng ordinal (mục 7.4) — rule-based, không LLM tự tóm tắt |
| `format` ("Input Formatter") | Cuối cùng, trước khi gọi Model | Đóng gói đúng khuôn API của LLM; bọc mỗi evidence phi cấu trúc trong delimiter tường minh (`<evidence source="...">...</evidence>`), kèm 1 dòng system instruction: nội dung trong thẻ là dữ liệu quan sát được, không phải chỉ thị |

**Giới hạn kích thước 1 lần gọi tool**: cap tại cùng bước với `sanitize`/`check_injection` (placeholder 4000 ký tự) — vượt thì cắt bớt, gắn `truncated: true` (không phải nội dung nguy hiểm, chỉ là quá dài — khác `BLOCKED_SENSITIVE`).

**Giới hạn ghi nhận cho báo cáo**: rule-based injection detection có recall thấp hơn hẳn credential detection (credential có format cố định, injection thì diễn đạt vô hạn) — không phải lý do bỏ qua, nhưng cần nêu rõ như 1 giới hạn đã biết.

---

## 5. State — dữ liệu tiến trình

`Brain` là chủ sở hữu và người đọc/ghi duy nhất. `Observer` chỉ là 1 hàm thuần `Brain` gọi ra (`Observer.check(state, trace_log) → CONTINUE/STOP`), không phải tiến trình riêng, không tự "theo dõi" gì cả.

```
LƯU THẬT (5 field)                    TÍNH LẠI MỖI LẦN CẦN, KHÔNG LƯU (3 field)
run_id                                 iteration_count   = COUNT DISTINCT iteration
edge_identity_key                      tools_called      = DISTINCT tool_name
session_started_at                     hypotheses_opened_count = COUNT DISTINCT hypothesis_id
manual_abort_flag (ghi khi dev bấm nút)
phase (RUNNING/CONCLUDED)
```

`elapsed_ms` = `now() - session_started_at`, cũng tính live. Lý do không lưu 3 field kia: tránh 2 nguồn dữ liệu (bảng gốc + số đếm riêng) có thể lệch nhau nếu 1 lần ghi thất bại giữa chừng — nhất quán với cách `dependency_current_state` được thiết kế thành VIEW, không phải bảng lưu sẵn (`ORCHESTRATION_DESIGN.md` mục 3).

---

## 6. Model Output — cấu trúc I/O bắt buộc mỗi vòng

Ép bằng structured output/JSON schema của API LLM — không hy vọng Model tự giác tuân thủ định dạng.

```jsonc
{
  "reasoning": "...",                                  // luôn có
  "stance_updates": [                                  // gán cho evidence NHẬN Ở VÒNG TRƯỚC
    {"evidence_id": "E7", "stance": "support"}
  ],
  "new_hypotheses": [                                  // tuỳ chọn — nếu có, BẮT BUỘC
    {"id": "H3", "statement": "..."}                   // kèm tool_calls cho nó NGAY vòng này
  ],
  "action": "call_tool",                               // hoặc "conclude"
  "tool_calls": [                                       // tối đa 4/vòng (mục 10), có thể
    {"tool_name": "...", "tool_input": {...},           // nhắm vào hypothesis MỚI hoặc CŨ
     "hypothesis_id": "H2"}
  ]
}
```

Model **kết hợp trong cùng 1 lần gọi**: đánh giá evidence vừa nhận (`stance_updates`) + quyết định hành động tiếp theo (`action`/`tool_calls`) + đề xuất hypothesis mới nếu cần — không tách thành 2 lượt riêng (rẻ hơn, đúng bản chất ReAct: mỗi vòng tái đánh giá toàn bộ evidence tích luỹ).

Field `confidence` số (LLM tự khai) đã bị **loại bỏ hoàn toàn** — không có công thức nào chứng minh được độ tin cậy của 1 con số tự chấm; trạng thái cuối suy ra thuần từ rule (mục 7).

---

## 7. Hypothesis-driven reasoning

### 7.1. Candidate → Hypothesis → Evidence

Thay vì để Model tự do "tìm kiếm xung quanh", nó phải tường minh hoá: *"tôi đang nghi ngờ điều gì, và bằng chứng nào ủng hộ/phản bác điều đó"*.

**Catalog khởi điểm mềm** theo `escalation_reason` (mục 8.1) — chỉ là gợi ý, không phải giới hạn cứng. Model được tự đề xuất hypothesis ngoài catalog; không gian hành động thực tế vẫn bị giới hạn tự nhiên bởi việc chỉ có đúng 4 tool tồn tại để kiểm chứng bất kỳ hypothesis nào.

### 7.2. Evidence — cấu trúc lưu

```jsonc
{
  "evidence_id": "E7", "run_id": "...", "iteration": 3,
  "hypothesis_id": "H2", "tool_name": "read_pr_descriptions",
  "tool_input": {...}, "tool_output": "...",
  "stance": null,        // null lúc mới ghi, UPDATE theo evidence_id khi Model báo lại
  "stale": false,        // xem 7.5
  "status": "OK"         // hoặc "ERROR" — bị loại khỏi dedup lookup
}
```

### 7.3. 5 trạng thái hypothesis — thuần rule-based

```
SUPPORTED     = có ≥1 evidence stance=support, không có contradict
CONTRADICTED  = có ≥1 evidence stance=contradict, không có support
CONFLICTED    = có CẢ HAI  → map thẳng vào status cuối CONFLICTING_EVIDENCE
INSUFFICIENT  = đã gọi tool liên quan, không thấy gì cả 2 chiều
                (bao gồm cả trường hợp không tool nào trong 4 cái có thể
                 chứng minh được hypothesis đó — đúng giới hạn open-world,
                 không phải lỗi hệ thống)
PROPOSED      = chưa có evidence nào
```

### 7.4. Suy ra 4 status cuối của cạnh — rule cố định, không LLM

```
Bất kỳ hypothesis nào CONFLICTED                            → CONFLICTING_EVIDENCE
Mọi hypothesis đều INSUFFICIENT                              → INSUFFICIENT_BASIS
≥1 hypothesis SUPPORTED + có ≥1 evidence CẤU TRÚC (Tier 1)   → CONFIDENT
  (corroboration rule — áp per-hypothesis, không phải 1 lần cho cả kết luận)
≥1 hypothesis SUPPORTED nhưng CHỈ evidence phi cấu trúc      → NEEDS_MONITORING
```

Giữ lại cả `original_agent_status` (Model tự báo) lẫn `final_status` (sau rule) trong record cuối — không chỉ giữ 1.

### 7.5. Thứ hạng độ tin cậy — ORDINAL, không phải số

```
traffic (read_extended_traffic_window)  >  PR description  >
source code thật  >  comment/docs (tụt hạng thêm nếu stale=true)
```

Chỉ dùng để quyết `normalize` cắt gì trước và thứ tự hiển thị cho người review — **không bao giờ** dùng để tính ra 1 con số confidence.

**Staleness**: `read_source_code`/`read_internal_docs` mang kèm ngày commit cuối (git blame/log), so với ngày snapshot traffic của Tier 1 — cũ hơn nhiều (placeholder >1 năm) → `stale: true`.

**Độc lập nguồn**: evidence từ `read_pr_descriptions` và `read_source_code` cùng trỏ về 1 PR number → tính là 1 nguồn, không phải 2 (tránh đếm trùng khi corroborate).

### 7.6. Giới hạn số hypothesis mở song song

Enforce bằng code (`Brain`), không phải nhắc trong system prompt: chạm ngưỡng (placeholder = 4, mục 12) → loại bỏ hẳn lựa chọn "mở hypothesis mới" khỏi schema hành động hợp lệ của vòng đó, Model không có cách nào chọn được, dù muốn.

---

## 8. Pipeline 2 giai đoạn mỗi vòng

### 8.1. Giai đoạn A — thu thập & Model đánh giá

```
Tool trả kết quả thô
  → Input Check (mục 2.1, tại Tool Registry — TRƯỚC bước này nếu input không hợp lệ)
  → sanitize + check_injection (mục 4)
  → ghi trace_log (evidence_id mới, stance: null)
  → Builder ① (đọc trace_log, nhóm theo hypothesis)
  → normalize → format (bọc <evidence> tag)
  → gọi Model
  → nhận Model Output
```

`goal` đưa vào Model gồm: schema escalate gốc của Tier 1 + `escalation_reason` — 1 bảng tra cứu thuần code, không cần LLM:

```
NOT_OBSERVED  → nghi CronJob chu kỳ dài / evidence đã lỗi thời / dependency tạo động lúc runtime
OBSERVED_WEAK → nghi chu kỳ ngắn hơn cửa sổ K ngày, hoặc nhánh lỗi hiếm
UNKNOWN       → không phải vấn đề tần suất, mà là vấn đề PHÂN LOẠI/Ý NGHĨA
```

### 8.2. Giai đoạn B — cập nhật, quyết định, xuất kết quả

```
Model Output → Brain
  → UPDATE trace_log.stance theo từng evidence_id trong stance_updates
    (xác thực evidence_id/hypothesis_id có THẬT SỰ tồn tại trước khi áp dụng —
     không tồn tại thì từ chối, phản hồi lại cho Model ở vòng sau, không âm thầm bỏ qua)
  → Builder ② (cùng hàm với Builder ①, gọi lại — evidence giờ có stance thật)
  → Observer: đủ chưa?
      • policy: iteration/timeout/hypothesis cap/manual_abort đã chạm ngưỡng?
      • hoặc Model tự báo action="conclude"?
  → Validation (corroboration rule per-hypothesis — LUÔN chạy, bất kể dừng hay tiếp tục)
  → Result Formatter (LUÔN chạy)
  → rẽ nhánh CHỈ ở bước cuối cùng:
      • Observer nói DỪNG → cùng kết quả này lưu thành Final Result,
        trả về Batch Orchestrator
      • Observer nói TIẾP TỤC → kết quả này là "Valid result" — sống,
        KHÔNG lưu, vừa hiển thị dashboard real-time vừa làm evidence_so_far
        cho vòng kế tiếp (quay lại 8.1)
```

**Điểm mấu chốt**: `Validation`/`Result Formatter` chạy **mọi vòng**, không chỉ lúc kết luận — đây là cách dashboard xem được tiến trình suy luận theo thời gian thực mà không cần thêm cơ chế export nào (mục 11.3).

---

## 9. Điều kiện dừng

### 9.1. Phân loại

```
Policy thuần (không cần ngữ nghĩa) → Iteration cap · Timeout · Manual abort · Cạn tool
Semantic (cần Model tự báo)         → action="conclude" + qua được corroboration
```

### 9.2. Observer — wrapper bắt buộc, không phải tool

Model không biết `Observer` tồn tại, không có cách nào "gọi" hay "bỏ qua" nó — nó chạy vô điều kiện mỗi vòng.

### 9.3. Xử lý lỗi tool call

```
Tool lỗi (timeout/network/không đọc được)
  → ghi trace_log: {tool_name, tool_input, status:"ERROR", error_message}
    (dòng này KHÔNG tính vào dedup lookup — retry phải thực sự chạy lại)
  → đưa lỗi vào context như 1 observation bình thường, Model tự quyết vòng sau
  → CÙNG (tool_name, tool_input) lỗi LẦN 2 liên tiếp
    → không cho thử lần 3, gắn needs_admin_attention=true, nổi bật trên dashboard
```

Vòng bị lỗi vẫn tính vào `iteration_count`.

### 9.4. Phân biệt lý do dừng (bổ sung vào `agent_rounds`, xem mục 11.2)

```
stopped_by: "model" | "observer_policy"
stop_reason: chỉ điền khi stopped_by="observer_policy" (vd "max_iterations exceeded")
```

---

## 10. Bảo mật tại `Brain` — Whitelist vs Policy

| Loại | Mục | Enforce |
|---|---|---|
| **Whitelist** | Tool name | Enum 4 giá trị cố định trong JSON schema |
| | `source_service`/`service` | Khớp `canonical_service` của Tier 1 cho đúng cạnh |
| | Scope tool | Phải `read-only` |
| | Domain mạng | Chỉ `api.github.com`, qua gatekeeper |
| **Policy** | `keyword` | Giới hạn dài/ký tự + subprocess list-args |
| | `tool_output` size | Cap, vượt thì truncate + flag, không chặn |
| | `len(tool_calls)`/vòng | ≤4 — vượt thì lấy 4 đầu, log bất thường |
| | Hypothesis cap | Loại khỏi action schema khi chạm ngưỡng |
| | Iteration/timeout/manual abort | Ép dừng |
| | Retry limit | 2 lần liên tiếp thì dừng thử |
| | Dedup | `(tool_name, tool_input)` đã có → tái dùng |
| | `evidence_id`/`hypothesis_id` tham chiếu | Phải tồn tại thật, không thì từ chối |
| | Sanitize / Security check | Xem mục 4 |
| | Corroboration | Xem mục 7.4 |

**Framing cho phần lập luận của khóa luận**: đây là Zero Trust áp lên chính Agent, không chỉ lên NetworkPolicy nó sinh ra — *deny-by-default* (mọi whitelist trên đều là allowlist, không phải blocklist) và *never-trust-always-verify* (mọi output của Model đều bị `Brain` xác minh trước khi có hiệu lực, Model không bao giờ tự thực thi trực tiếp).

### 10.1. Phòng thủ 2 lớp chống prompt injection

```
Lớp 1 — Security check (mục 4): chặn pattern đã biết + entropy bất thường
        Giới hạn thật: recall thấp, diễn đạt khéo có thể lọt
Lớp 2 — Corroboration rule (mục 7.4): không cho 1 evidence đơn lẻ tự quyết CONFIDENT
        KHÔNG phát hiện được evidence giả — nhưng GIỚI HẠN THIỆT HẠI:
        dù Lớp 1 bỏ sót, tệ nhất chỉ rơi về NEEDS_MONITORING (vào hàng đợi
        người), không bao giờ tự động enforce sai
```

### 10.2. Đã tham khảo 1 bài viết ngoài về chống prompt injection cho AI agent — phần lớn không áp dụng được

Bài viết nhắm vào coding agent (thực thi shell, đọc file tuỳ ý, gọi mạng tự do) — mô hình đe doạ nguy hiểm hơn hẳn Tier 2. Đã **từ chối**: sandbox thực thi (Docker/Firecracker — Tier 2 không thực thi code), output validator cho shell/import nguy hiểm (không có tool nào như vậy), dùng 1 LLM thứ 2 để tự phân loại SAFE/INJECTION (cùng rủi ro với `confidence` tự khai — phi tất định, tốn thêm). Đã **nhận** 3 kỹ thuật rẻ, nhất quán với triết lý rule-based: bọc delimiter (mục 4), entropy Shannon (mục 4), negative-prompting lặp lại trong system instruction. **Cảnh báo**: đoạn bài viết nói về "cộng đồng developer Trung Quốc" (Zhihu, quy định MIIT, thư viện GitHub với số sao rất cụ thể) đọc giống nội dung filler SEO không kiểm chứng được — không nên trích dẫn vào khóa luận.

---

## 11. Lưu trữ

Không phát sinh queue hay storage trung gian nào — toàn bộ nằm trong cùng 1 file SQLite (`PRAGMA journal_mode=WAL`, đã chốt ở `ORCHESTRATION_DESIGN.md` mục 3), cho phép 1 writer (`Brain`) + nhiều reader (API Server phục vụ dashboard) đọc/ghi đồng thời — không cần export/push, dashboard chỉ `SELECT` trực tiếp bất cứ lúc nào.

### 11.1. `trace_log` — 1 dòng / tool call

```jsonc
{
  "evidence_id", "run_id", "iteration", "hypothesis_id",
  "tool_name", "tool_input", "tool_output", "stance", "stale",
  "status",                    // "OK" | "ERROR"
  "injection_flagged": false,  // cờ tiện tra cứu, thay vì so chuỗi tool_output
  "sanitized_flagged": false
}
```

### 11.2. `agent_rounds` (MỚI) — 1 dòng / round, LUÔN có kể cả `conclude`

Lỗ hổng đã sửa: `trace_log` chỉ ghi khi có tool call — vòng Model tự `conclude` (không gọi tool) trước đây không có chỗ nào lưu `reasoning`/quyết định của nó.

```jsonc
{
  "run_id", "iteration", "reasoning",
  "stance_updates": [...], "new_hypotheses": [...],
  "action", "stopped_by", "stop_reason", "timestamp"
}
```

Join với `trace_log` qua `(run_id, iteration)` — quan hệ 1-nhiều tự nhiên (1 round có 0-4 tool call), không phải "mỗi thành phần xử lý nội bộ 1 bảng riêng" (Sanitize/Security/Normalize không cần bảng riêng — hiệu ứng của chúng đã nằm sẵn trong `trace_log.tool_output`/cờ đi kèm).

### 11.3. Kết quả cuối — không tạo bảng mới, cập nhật vào `dependency_evidence` đã có

```
Brain trả Final Result về Batch Orchestrator (đồng bộ, giá trị trả về)
  → Batch Orchestrator UPDATE dependency_evidence, thêm 2 cột:
      tier2_status           (1 trong 4 trạng thái cuối)
      tier2_original_status  (trước khi bị corroboration hạ cấp, nếu có)
```

Không phá luật "chỉ Batch Orchestrator ghi bảng này" (`ORCHESTRATION_DESIGN.md` mục 3). `dependency_current_state` (VIEW đã có) tự động thấy 2 cột mới, không cần sửa.

### 11.4. Bảng đã có, không đổi

`pipeline_run_log`, `dependency_evidence` (gốc), `human_decisions` — theo đúng `ORCHESTRATION_DESIGN.md`.

---

## 12. Ngưỡng số — giá trị khởi tạo, chờ đo thật

| Ngưỡng | Giá trị khởi tạo |
|---|---|
| `max_iterations` | 8 |
| Timeout / tool call | 30 giây |
| Timeout / phiên | 5 phút |
| Ngân sách token cho `normalize` | 70% context window model |
| Trần hypothesis mở song song | 4 |
| `len(tool_calls)` / vòng | 4 |
| Retry limit | 2 lần liên tiếp |
| `tool_output` size cap | 4000 ký tự |

Tất cả đo lại sau khi có pilot thật, dùng percentile-95 trên số liệu quan sát được (số vòng/thời gian thực tế cần để `conclude`) — cùng phương pháp đã dùng cho `θ_strong` của Tier 1, không đoán tay.

---

## 13. Ngoài phạm vi PoC / còn treo

- **Cơ chế Bayesian log-odds, self-consistency (k=5 lần chạy độc lập), migration wave weighting** — kế thừa từ 1 phiên trước, chưa được xác nhận lại, đang tự học riêng trước khi quyết giữ hay bỏ. Cần ghi rõ trong báo cáo là "đã cân nhắc, tạm gác lại", không nên im lặng bỏ qua.
- **`source_service` whitelist theo phạm vi nào**: chỉ 2 service của đúng cạnh đang xác minh (chặt, có thể cản việc điều tra qua 1 service trung gian/proxy), hay toàn bộ service trong schema Tier 1 cung cấp — chưa quyết định.
- **Nơi `Brain` vật lý chạy** (API Server dài hạn hay Batch Orchestrator ngắn hạn) — không chặn thiết kế, chỉ ảnh hưởng lúc code.
- Không train/fine-tune model nào (nhất quán với Tier 1 và Config Collector v2 — Collector v2 dùng LLM pretrained cho đúng 1 bước trích xuất, không train) — toàn bộ Tier 2 rule-based + 1 LLM call/vòng.
