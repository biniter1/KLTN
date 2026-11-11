# Thiết kế — Coordination Layer (Batch Orchestrator + API Server)

Tài liệu này mô tả **ai điều phối** Tier 1, Config Evidence Collector và Tier 2 (AI Agent) chạy với nhau như thế nào: module nào chạy khi nào, chuyện gì xảy ra khi một module lỗi giữa chừng, con người theo dõi, dừng, chạy lại và review ra sao mà không làm nghẽn toàn bộ hệ thống.

> **Cập nhật 08/10/2026 (v2).** Bin đã chốt sau buổi thảo luận 06–08/10 (biên bản đầy đủ ở `claude/ORCHESTRATION_V2_DRAFT.md`). Các thay đổi chính so với bản 29/09:
> - Tách rõ **2 giai đoạn**: thu dữ liệu trên Azure, rồi chạy hệ thống (mục 1). Batch Orchestrator **không** còn bước "cấy kịch bản / chờ K ngày".
> - **2 nguồn dữ liệu** cho Batch: `live` (ghim mốc T) và `snapshot` (phát lại). Đổi nguồn bằng tay, theo từng lần chạy (mục 2).
> - **Dashboard thay thế CLI**: API Server có thêm **Job runner** để khởi động Batch như tiến trình con (mục 7).
> - **Dừng 2 cấp** (giống SIGTERM / SIGKILL) **× 2 phạm vi** (cả lần chạy / 1 phiên Tier 2) (mục 8).
> - API key nằm trong file `.env` riêng, API Server chỉ truyền đường dẫn file (mục 9).
> - Mỗi lần chạy là 1 **attempt** (`trial-07.a2`). Kết quả chính thức của 1 trial là attempt SUCCESS mới nhất (mục 3, 4).

---

## 0. Hai loại tiến trình khác nhau — không gộp làm một

Dễ nhầm nếu nghĩ "bộ não điều phối" là một tiến trình duy nhất làm hết mọi việc. Thực ra cần tách hai loại tiến trình có vòng đời khác hẳn nhau:

```
Batch Orchestrator                          API Server
- Ngắn hạn, chạy xong thì thoát             - Dài hạn, luôn sống
- 1 tiến trình = 1 lần chạy (attempt)       - Lắng nghe request bất kỳ lúc nào
  cho đúng 1 trial                          - Phục vụ dashboard, human review,
- Không lắng nghe request nào; chỉ nhận       gatekeeper cho Tier 2, và Job runner
  signal (SIGTERM/SIGKILL) từ Job runner      (khởi động Batch, gửi signal)
```

Không thể ghép hai thứ này làm một tiến trình: một cái "chạy xong rồi tắt", một cái "phải sống mãi để nhận request" là hai mô hình vận hành xung đột nhau.

Từ v2, **API Server là bên khởi động Batch** (thay cho việc người tự gõ CLI). Batch vẫn là tiến trình riêng, vẫn tự ghi bảng của mình, và API Server không ghi thay. Điểm chung của hai bên là cùng đọc/ghi **một file SQLite** (mục 4).

---

## 1. Hai giai đoạn tách rời

| | Giai đoạn 1 — Thu dữ liệu | Giai đoạn 2 — Chạy hệ thống của đồ án |
|---|---|---|
| Ở đâu | Máy ảo Azure: kind + Istio ambient + Online Boutique + harness + Prometheus | Máy ảo Azure (nguồn live) **hoặc** laptop (nguồn snapshot), xem mục 2 |
| Ai làm | Script `infra/` 00→07 (hạ tầng dùng chung `05_deploy_shared.sh`: cấy cả 15 virtual trial **một lần**) | Batch Orchestrator + API Server + Dashboard |
| Kéo dài | 7 ngày, cluster tự chạy | Vài phút mỗi trial; phần lâu nhất là Tier 2 (gọi LLM) |
| Con người can thiệp | Không có trên dashboard. Dashboard **không** có nút dừng việc thu thập | Toàn bộ mục 6–8 + `DASHBOARD_DESIGN.md` |

Riêng phần **tính số liệu đánh giá** (precision/recall, B1/B2, CI 95%, Wilcoxon) vẫn do `tier1.experiment` / `infra/scripts/09_run_experiment.py` làm như cũ. Orchestrator không thay phần này (`TIER1_DESIGN.md` mục 13).

**Phân biệt với kiến trúc triển khai thật** (ngoài phạm vi PoC, hướng mở rộng): mô hình trên chỉ đúng cho **PoC/harness đánh giá** (R=15, mỗi trial một kịch bản kiểm soát được). Một hệ thống production sẽ thu traffic **liên tục**, đóng snapshot định kỳ (ví dụ mỗi 7 ngày), và nếu một chu kỳ lỗi thì dùng tạm snapshot gần nhất thay vì chặn hệ thống. Cách này **không áp dụng được cho R=15**: mỗi trial cố ý mang một kịch bản khác nhau, dùng nhầm snapshot của trial khác sẽ làm sai kết quả đo, không phải "ứng phó lỗi an toàn".

---

## 2. Hai nguồn dữ liệu cho Batch Orchestrator

### 2.1 Nguồn `live` — đọc trực tiếp cluster Azure
- Đọc Prometheus và K8s API của cluster đang chạy.
- **Mọi query Prometheus được ghim tại mốc T** (mục 2.4), không tính tại "bây giờ". Nếu không ghim, `increase(...[7d])` chạy lúc demo sẽ ra cửa sổ khác lúc đánh giá, và kết quả demo không khớp báo cáo.

### 2.2 Nguồn `snapshot` — phát lại bản ghi đã chụp
- Thao tác **"Chụp snapshot"** (một job trên dashboard) chạy **một lần** tại mốc T khi cluster còn khỏe, và lưu dữ liệu **thô**:
  - Từng câu PromQL và câu trả lời thô của Prometheus, tại T.
  - Object K8s thô: pod, service, endpoints, configmap, virtualservice, serviceentry.
  - Cửa sổ traffic mở rộng cho tool `read_extended_traffic_window` của Tier 2: **14 và 30 ngày**.
  - PR GitHub mà gatekeeper lấy về, cho các service của Online Boutique.
  - Manifest: mốc T, thời điểm chụp, namespace, hash từng file.
- Nguồn snapshot **phát lại** đúng các câu trả lời đó. Từ Config Collector (LLM dùng lại cache) → Tier 1 → Tier 2, **hai nguồn chạy cùng một đường code**, chỉ khác chỗ lấy dữ liệu đầu vào.
- Không dùng file JSON của `08_export` làm snapshot, vì file đó đã xử lý sẵn (TrafficSeries, ConfigEvidenceRecord). Dùng nó thì nguồn snapshot bỏ qua bước Config Collector và LLM, tức hai nguồn chạy hai đường code khác nhau.
- Giới hạn chấp nhận được: ở nguồn snapshot, nếu Tier 2 xin một cửa sổ không được chụp sẵn (ví dụ 21 ngày), tool trả lỗi "không có dữ liệu". Brain đã có cơ chế xử lý lỗi tool (`TIER2_DESIGN.md` mục 9.3).

### 2.3 Luật đổi nguồn
- **Đổi bằng tay**, không tự động.
- Đổi ở mức **cả lần chạy**. **Không bao giờ trộn hai nguồn trong cùng một lần chạy**, cùng lý do Config Collector không tự fallback sang regex.
- Mỗi lần chạy ghi `data_source` (`live` | `snapshot`) và `snapshot_id`. Dashboard luôn hiển thị nguồn này.
- Kịch bản demo: lần chạy live FAILED → bấm "Chạy lại bằng snapshot" → tạo attempt mới, đọc toàn bộ từ snapshot.

### 2.4 Mốc T
- **T = thời điểm harness pod đầu tiên bắt đầu chạy + 7 ngày** (`window_days`), làm tròn lên đầu giờ kế tiếp.
- T được tính **một lần**, lưu vào cấu hình chung, dùng chung cho cả 15 trial và mọi lần chạy lại. Sửa được trên màn hình cấu hình.

### 2.5 ⚠ Ràng buộc thời gian lưu của Prometheus
- Prometheus mặc định giữ dữ liệu **15 ngày** (`--storage.tsdb.retention.time`). Addon mẫu của Istio có thể dùng `emptyDir`, tức pod Prometheus restart là mất dữ liệu.
- Hệ quả:
  - Nguồn live chỉ dùng được trong cửa sổ retention.
  - **Snapshot phải chụp trước khi dữ liệu cũ bị xoá**, tức trong khoảng từ T tới hết retention tính từ lúc bắt đầu thu.
- **Không restart hay sửa Prometheus khi đang thu dữ liệu.** Kiểm tra chỉ bằng lệnh đọc (`claude/ORCHESTRATION_V2_DRAFT.md` mục 12).

---

## 3. Batch Orchestrator — luồng 1 lần chạy (attempt)

```
Mỗi lần chạy cho 1 virtual trial (vd trial-07) = 1 attempt mới (trial-07.a1, .a2, ...)

scenario_check     live: hiện tại ≥ T; có ground truth + owner_map của trial
                   snapshot: snapshot tồn tại, hash khớp manifest; có ground truth + owner_map
   ▼  ◆ điểm kiểm tra dừng
traffic_collector  đọc traffic tại T (live/snapshot) từ namespace dùng chung
                   → tách phần của trial theo pair_key + owner_map
   ▼  ◆
config_collector   quét lại config TỪ ĐẦU (live/snapshot) → hợp nhất → phễu lọc
                   → LLM extraction (cache theo hash) → validation → tách theo trial
   ▼  ◆
tier1              candidate discovery + scoring, STATELESS mặc định → ghi dependency_evidence
   ▼  ◆
tier2              với từng cạnh Tier 1 escalate:  ◆  → 1 phiên Brain riêng
                   (trong Brain: mỗi vòng suy luận đọc manual_abort_flag)
                   → ghi tier2_status vào đúng dòng của attempt này
   ▼
trial = SUCCESS | FAILED | CANCELLED   → tiến trình thoát
```

Ghi chú:
- **Tuần tự, không gọi chéo**: Tier 1 chỉ đọc dữ liệu đã được điền sẵn từ các bước trước.
- **"Cache LLM" không mâu thuẫn với "quét lại mới"**: việc quét luôn chạy lại từ đầu; cache chỉ tránh gọi lại LLM cho chunk config có nội dung giống hệt (cùng hash).
- **Tách trial trong namespace dùng chung**: 1 record thuộc trial X khi `pair_key` là cạnh thật của Online Boutique, **hoặc** X nằm trong `owner_map[pair_key]`. Riêng record FOUND_UNRESOLVED (đích = None) được khớp lại qua `raw_host == "<dest>-nonexistent"`, đúng quy ước của `render_manifests.py`.
- **Tier 1 stateless mặc định**: 15 trial chung namespace có cùng identity_key cho các cạnh thật. Nếu dùng hysteresis trên toàn bảng, trial sau sẽ đọc `previous_status` của trial trước và phá tính độc lập của R=15. Tuỳ chọn `stateful` chỉ dùng cho bộ demo, giới hạn lịch sử trong chính trial đó.
- **Phiên Tier 2**: id = `<attempt_id>#<hash identity_key>`, vì 1 phiên = 1 cạnh và `agent_sessions.run_id` là khoá chính. `dependency_evidence.tier2_session_id` trỏ ngược về phiên.

---

## 4. Cấu trúc lưu trữ — tách bảng theo người ghi, không tách theo credential

**Vấn đề**: UI (ghi quyết định người, ghi lệnh vận hành) và Batch (ghi và tính lại evidence) cùng đụng vào một nguồn dữ liệu. Cần đảm bảo bên này không ghi đè phần của bên kia. Quan trọng hơn, nếu Batch chạy lại sau khi người đã duyệt một cạnh, quyết định của người **không được mất**.

**Giải pháp**: tách bảng theo đúng bên ghi. SQLite không có khái niệm cấp quyền theo client như S3 Access Point, nên nguyên lý đó được mô phỏng bằng cấu trúc bảng thay vì bằng xác thực:

```
CHỈ Batch Orchestrator ghi
  dependency_evidence        (+ run_id, attempt_id, identity_key, tier2_session_id, tier2_*)
  pipeline_run_log           (RUNNING → SUCCESS | FAILED | CANCELLED, append theo sự kiện)
  collection_quality_log     (1 dòng / attempt / module — DASHBOARD_DESIGN.md mục 3.A)
  config_raw_items, config_extractions, config_evidence, llm_extraction_cache
  agent_sessions, agent_rounds, trace_log   (Tier 2 Brain chạy bên trong Batch)
  data_snapshots             (job "Chụp snapshot")
  → API Server KHÔNG có câu lệnh ghi nào vào các bảng này. Không phải "được quyền nhưng
    không dùng", mà là khả năng đó không tồn tại trong code. Có test quét mã nguồn khoá lại.
    Ngoại lệ duy nhất có chủ đích: bật agent_sessions.manual_abort_flag, ghi qua chính
    tier2_agent.storage (mục 8.2).

CHỈ API Server ghi — APPEND-ONLY (trigger SQLite chặn UPDATE/DELETE)
  human_decisions            quyết định nghiệp vụ (DASHBOARD_DESIGN.md mục 2)
  run_jobs                   Job runner: QUEUED → RUNNING → EXITED | KILLED | LOST ...
  run_control                mỗi lần bấm Dừng / Dừng ngay / Huỷ: ai, lúc nào, cấp, phạm vi, lý do
  orchestration_settings     cấu hình chung theo phiên bản (mục 7)
  agent_tool_call_log        log gatekeeper Tier 2 (mục 10)

VIEW (chỉ đọc, tính lúc query)
  latest_successful_attempt  = attempt có dòng trial SUCCESS mới nhất của mỗi trial
  dependency_current_state   = dependency_evidence của attempt đó
                               JOIN human_decisions mới nhất theo identity_key
                               (quyết định có run_id thì chỉ áp cho trial đó; không có thì
                               áp mọi trial)
```

**Lợi ích trực tiếp**: chạy lại một trial là tạo attempt mới, không ghi đè attempt cũ. Quyết định người khớp theo `identity_key` nên **vẫn còn nguyên** sau khi chạy lại, không cần logic merge để "cứu" quyết định cũ. Attempt FAILED / CANCELLED / KILLED vẫn giữ dữ liệu để truy vết, nhưng **không bao giờ lọt vào view**.

**Trạng thái hiển thị của 1 attempt được tính lúc đọc** từ `pipeline_run_log` (Batch ghi) + `run_jobs` (API ghi). Ví dụ KILLED đến từ `run_jobs`, vì tiến trình bị kill không kịp ghi gì. Không bên nào phải ghi đè dữ liệu của bên kia.

**Vận hành**: `PRAGMA journal_mode=WAL` cho phép đọc và ghi đồng thời. Dashboard đọc real-time qua kết nối read-only trong lúc Batch, kể cả Tier 2, đang ghi.

---

## 5. Xử lý lỗi giữa lần chạy

**Luật cứng**: bất kỳ bước nào (scenario_check, Traffic Collector, Config Collector, Tier 1, Tier 2) lỗi giữa chừng thì **toàn bộ attempt bị hủy**. `pipeline_run_log` ghi bước đó FAILED kèm `error_message`, và dòng `trial` FAILED kèm bước lỗi. **Không bao giờ** để Tier 1 chạy tiếp âm thầm với dữ liệu thiếu. Kết quả bị nhiễu mà không có cờ báo nguy hiểm hơn nhiều so với việc thấy rõ "trial-07 FAILED ở config_collector, cần chạy lại".

Lỗi của bước LLM bên trong Collector (API lỗi, hoặc output sai schema sau 1 lần retry) tính là `config_collector` FAILED. **Không tự fallback sang parser regex**, vì làm vậy trộn 2 phương pháp trích xuất trong cùng một bộ số liệu và làm hỏng phép so sánh LLM vs baseline regex (`CONFIG_EVIDENCE_COLLECTOR.md` mục 4.3, 9.2).

```jsonc
// pipeline_run_log — append theo sự kiện, mỗi bước 2 dòng (RUNNING lúc bắt đầu + dòng kết thúc)
{
  "run_id": "trial-07",
  "attempt_id": "trial-07.a2",
  "step": "config_collector",   // trial | scenario_check | traffic_collector | config_collector | tier1 | tier2
  "status": "FAILED",           // RUNNING | SUCCESS | FAILED | CANCELLED
  "error_message": "...",
  "detail": { },                // JSON tuỳ bước; dòng trial RUNNING có data_source, snapshot_id, T
  "timestamp": "..."
}
```

Ghi cả RUNNING lúc bắt đầu, không chỉ lúc kết thúc, để dashboard phản ánh đúng "đang chạy tới đâu" thay vì im lặng cho tới khi xong (`DASHBOARD_DESIGN.md` mục 5 điểm 1). Retry **không tự động**: người bấm "Chạy lại" trên dashboard (mục 7).

---

## 6. Hàng đợi review con người — không chặn cạnh khác, không dùng cơ chế "chờ"

**Sai lầm cần tránh**: thiết kế pipeline block chờ người duyệt xong mới chạy tiếp. Khi đó một cạnh cần review kéo theo mọi cạnh khác (kể cả cạnh đã `OBSERVED_STRONG`, hoàn toàn đủ tin cậy) bị treo chung, biến một hệ thống lẽ ra tự động phần lớn thành nghẽn bởi khâu con người chậm nhất.

**Giải pháp**: tái dùng khái niệm "migration wave / exception queue" có sẵn từ thiết kế ban đầu, không phát minh cơ chế mới:

```
Với mỗi cạnh dependency (xử lý độc lập, không đồng bộ với nhau):

    OBSERVED_STRONG          → đích KHÔNG nhạy cảm → vào wave migrate
                                (ưu tiên theo confidence × blast radius)
                              → đích NHẠY CẢM (blast-radius override,
                                MIGRATION_PLANNER_DESIGN.md mục 1.5)
                                → PENDING_HUMAN_REVIEW (reason_code
                                  HIGH_BLAST_RADIUS), review TRƯỚC KHI
                                  tạo YAML — không tự động dù confidence cao
    PENDING_HUMAN_REVIEW     → vào exception queue, GIỮ NGUYÊN trạng thái hiện tại
                                (không enforce, không chặn cạnh khác) cho tới khi
                                người quyết định
    Escalate Tier 2 (AI)     → tương tự, chờ kết quả AI, không chặn cạnh khác
```

Không có tiến trình nào "đứng chờ" theo nghĩa kỹ thuật. Hàng đợi review được **suy ra lúc query** từ `dependency_current_state`, không có bảng hàng đợi riêng phải đồng bộ. Cạnh `PENDING_HUMAN_REVIEW` chỉ đơn giản **bị loại khỏi wave hiện tại**. Khi người quyết định xong (ghi `human_decisions`), lần chạy **Wave Planner** tiếp theo (một bước riêng, đọc lại `dependency_current_state` mới nhất) sẽ tự nhặt cạnh đó vào một wave sau, không chèn ngược vào wave đang chạy. Zero Trust Continuity Monitor cũng tái dùng cơ chế "đọc lại trạng thái mới nhất mỗi chu kỳ" này (`STALE_PERMISSION`, `MIGRATION_PLANNER_DESIGN.md` mục 2.8).

**Riêng R=15 (đánh giá cho khóa luận)**: không cần người thật ngồi duyệt trong lúc đo. Ground truth đã biết trước, nên "rơi vào `PENDING_HUMAN_REVIEW`" chỉ cần **đếm làm một loại kết quả**. Màn hình review có hành động duyệt thật dùng để **demo cơ chế**, không lẫn vào vòng đo R=15.

---

## 7. Job runner — dashboard thay thế CLI

- Nằm trong API Server. Nhận lệnh từ dashboard: **Chạy** (1 / nhiều / cả 15 trial, chọn nguồn dữ liệu và tuỳ chọn như bỏ Tier 2, stateful…), **Huỷ** job chưa chạy, **Chạy lại** attempt FAILED / CANCELLED / KILLED (cùng nguồn hoặc đổi nguồn), **Chụp snapshot**.
- Hàng đợi tuần tự, **mỗi lúc chỉ 1 lần chạy**: tránh tranh ghi SQLite, tránh đốt quota LLM song song, và log dễ đọc.
- Khởi động Batch bằng tiến trình con:
  `python -m orchestration.cli_batch --run-id … --data-source live|snapshot --env-file <đường dẫn>`.
  Tham số lấy từ `orchestration_settings`: Prometheus URL, kubeconfig, namespace, owner_map, ground truth, source-root, mốc T, đường dẫn `.env`.
- Ghi `run_jobs` theo sự kiện: job_id, run_id, attempt_id (khi Batch báo về), PID, trạng thái (`QUEUED / RUNNING / EXITED / KILLED / CANCELLED_BEFORE_START / LOST`), exit code, người bấm, thời điểm.
- **API Server restart khi Batch đang chạy**: lúc khởi động lại, kiểm tra PID trong `run_jobs`. PID còn sống thì tiếp tục theo dõi. PID đã chết mà `pipeline_run_log` vẫn RUNNING thì ghi `LOST` để người xem quyết định. Đây là cách xử lý trường hợp "treo ở RUNNING mãi".

---

## 8. Dừng — 2 cấp × 2 phạm vi

Chỉ dừng **phần xử lý** của giai đoạn 2 (đọc dữ liệu, Tier 1, Tier 2). Không có nút dừng việc thu thập 7 ngày của giai đoạn 1. Tier 1 chạy dưới 1 giây, nên trong thực tế nút Dừng chủ yếu dùng cho Tier 2: AI suy luận lòng vòng, tốn tiền API, hoặc làm điều ngoài ý muốn.

### 8.1 Dừng cả lần chạy

| Cấp | Cơ chế | Kết quả |
|---|---|---|
| **Dừng** (giống SIGTERM) | Job runner gửi `SIGTERM` cho tiến trình Batch. Batch bắt signal, bật cờ "đã yêu cầu dừng", và dừng tại **điểm kiểm tra ◆ gần nhất** (mục 3). Nếu đang trong Tier 2, Batch bật luôn `manual_abort_flag` của phiên đang chạy để Brain dừng ở cuối vòng hiện tại | Batch tự ghi `pipeline_run_log`: bước đang chạy + `trial` = **CANCELLED**, kèm người bấm và lý do lấy từ `run_control` |
| **Dừng ngay** (giống SIGKILL) | Job runner gửi `SIGKILL`; tiến trình chết tức thì | Batch không kịp ghi gì → Job runner ghi `run_jobs` = **KILLED** (thời điểm, người bấm, lý do) |

- **Độ trễ của "Dừng"**: phải chờ lời gọi bên ngoài đang chạy xong (1 lời gọi LLM, 1 query Prometheus, 1 tool Tier 2). Thường vài giây, tệ nhất ~30 giây (timeout). Sau **60 giây** chưa thấy CANCELLED thì dashboard **gợi ý** nút "Dừng ngay", không tự động kill.
- Ở cả 2 cấp, dữ liệu dở dang **bị ẩn khỏi `dependency_current_state`** (giữ lại để truy vết). Kết quả chính thức vẫn là attempt SUCCESS gần nhất, nếu có.

### 8.2 Dừng 1 phiên Tier 2 (bỏ qua 1 cạnh, chạy tiếp các cạnh khác)

- API Server bật `manual_abort_flag` của phiên đó, ghi qua `tier2_agent.storage` (cơ chế có sẵn, `TIER2_DESIGN.md` mục 5, 9.1). Brain kiểm tra cờ ở cuối mỗi vòng và kết thúc phiên với `stopped_by = observer_policy`, `stop_reason = "manual abort"`.
- Cạnh đó nhận trạng thái Tier 2 tính từ những gì có tới lúc dừng (thường là `INSUFFICIENT_BASIS`), nên **tự vào hàng đợi review** `AGENT_INSUFFICIENT_BASIS`. Người đã chủ động dừng thì người quyết định. Dashboard gắn nhãn "dừng tay".
- **Chỉ có cấp "Dừng", không có "Dừng ngay"**: mọi phiên Tier 2 chạy chung trong một tiến trình Batch, signal tác động lên cả tiến trình, và Python không có cách an toàn để giết riêng một phần đang chạy bên trong. Muốn dừng ngay một phiên thì phải "Dừng ngay" cả lần chạy (8.1).

---

## 9. API key và bí mật

- Mọi API key của **Batch** (provider LLM của Config Collector, Anthropic cho Tier 2) nằm trong **một file `.env` riêng**, quyền `600`, do Bin tự tạo trên máy chạy. API Server **chỉ truyền đường dẫn** file cho tiến trình con: không đọc file, không log nội dung, không có route nào trả nội dung file.
- `GITHUB_TOKEN` thuộc về **API Server** (gatekeeper, mục 10), đặt trong môi trường của API Server. Agent không bao giờ thấy token này.
- Config gửi ra API LLM bên ngoài đã được redact credential và không bao giờ chứa Secret (`CONFIG_EVIDENCE_COLLECTOR.md`).
- Ghi chú trung thực: trên cùng 1 máy, cùng 1 user, đây là **kỷ luật code**, không phải cách ly thật. Cách ly thật (2 user / 2 máy / secret manager) là hướng mở rộng (mục 13).

---

## 10. API Server — các vai trò trong một service

Một API Server duy nhất, không tách thành nhiều service, để giữ đơn giản đúng quy mô PoC (nhất quán với các quyết định giảm phạm vi trước đó: không watch/controller, không đóng gói K8s Job, không train model, không đọc Secret). Các vai trò tách theo route và bảng nội bộ:

```
Đọc (dashboard)
  GET  /dependencies, /dependencies/{id}      trạng thái hiện tại + chi tiết 1 cạnh (config nguyên văn, phiên Tier 2)
  GET  /pipeline-runs, /pipeline-runs/{run}   các attempt, bước đang chạy / lỗi
  GET  /collection-quality                    số đếm chất lượng thu thập
  GET  /agent-sessions/{id}                   vòng suy luận + trace của 1 phiên Tier 2
  GET  /snapshots, /settings, /jobs           snapshot, cấu hình, hàng đợi job

Human review (nghiệp vụ — DASHBOARD_DESIGN.md mục 2)
  GET  /review-queue, /ops-alerts, /reason-codes
  POST /decisions                             reason_code + action + payload (append-only)

Điều khiển vận hành (Job runner — mục 7, 8)
  POST /jobs                                  chạy 1 / nhiều trial (nguồn, tuỳ chọn)
  POST /jobs/{id}/cancel                      huỷ job chưa chạy
  POST /jobs/{id}/stop   {level: graceful|force, reason}   Dừng / Dừng ngay
  POST /agent-sessions/{id}/abort  {reason}   dừng 1 phiên Tier 2
  POST /snapshots                             chụp snapshot tại T
  PUT  /settings                              ghi phiên bản cấu hình mới

Gatekeeper Tier 2
  POST /agent-tool-calls/*                    mục 11

Chưa làm (chờ Migration Planner)
  POST /policy-impact-check                   MIGRATION_PLANNER_DESIGN.md mục 1
```

**Truy cập**: API Server chỉ nghe `localhost`. Chạy trên Azure (nguồn live) thì vào qua `ssh -L 8080:localhost:8080 …`. Chạy trên laptop (nguồn snapshot) thì mở thẳng. Không làm đăng nhập ở PoC. `decided_by` / người bấm do client khai.

---

## 11. Vì sao không "xác thực nội bộ giữa các bước pipeline"

**Cân nhắc và bác bỏ**: thêm một lớp xác thực giữa Config Collector → Traffic Collector → Tier 1 nghe có vẻ khớp chủ đề Zero Trust, nhưng thực chất là **hình thức, không phải bảo mật thật**. Cả các module này chạy trong cùng 1 tiến trình Python, dùng chung 1 `kubeconfig`, dưới cùng 1 user. Không có ranh giới tin cậy thật nào giữa các lời gọi hàm nội bộ để mà "xác thực".

**Ranh giới thật sự có ý nghĩa** nằm giữa **Tier 1 + Config Collector** (phạm vi hẹp, RBAC read-only, mọi quyết định có hệ quả đều tất định) và **Tier 2 / AI Agent** (LLM có khả năng gọi tool, đọc bằng chứng phi cấu trúc, có nguy cơ evidence injection: comment code cố tình viết sai lệch để đánh lừa AI). API Server đóng vai trò **gatekeeper** cho lời gọi tool ra ngoài của Tier 2:
- allowlist tool + whitelist service;
- input schema chặt;
- rate-limit;
- giữ token thật;
- log **mọi** lời gọi vào `agent_tool_call_log`, kể cả bị từ chối hoặc bị rate-limit.

Tool `read_extended_traffic_window` gọi thẳng Prometheus (nguồn live) hoặc snapshot từ phía Batch, không qua gatekeeper (`TIER2_DESIGN.md`, điểm mở về gatekeeper cho tool traffic).

**Config Collector v2 cũng gọi LLM, vì sao vẫn nằm phía "hẹp" của ranh giới**: bước LLM trong Collector không có tool, không vòng lặp, chỉ trả JSON theo schema. Mọi reference nó trả về đều bị validator tất định kiểm lại trước khi thành bằng chứng. Dù bị prompt injection qua config, hậu quả tệ nhất chỉ là thêm hoặc bớt reference: thêm thì bị validator chặn, bớt thì đường traffic vẫn độc lập (`CONFIG_EVIDENCE_COLLECTOR.md` mục 5.2). Không cần gatekeeper riêng cho bước này.

---

## 12. UI — tham chiếu

UI gồm Lớp A (theo dõi, chỉ đọc), Lớp B (human review, có ghi và audit), và từ v2 thêm **Lớp C (điều khiển vận hành: chạy / dừng / chạy lại / snapshot / cấu hình)**. Danh sách màn hình, nút bấm và yêu cầu realtime: xem `DASHBOARD_DESIGN.md` mục 7.

---

## 13. Ngoài phạm vi PoC / hướng mở rộng

- Kiến trúc production thật (thu thập liên tục, snapshot định kỳ, dùng bản gần nhất khi lỗi). Mô tả ở mục 1, không build cho PoC.
- Tách API Server thành nhiều service theo mức độ rủi ro (ví dụ cô lập hẳn gatekeeper AI Agent hoặc Job runner).
- Cách ly bí mật thật: user riêng, secret manager (mục 9).
- Đăng nhập / phân quyền người dùng trên dashboard.
- Retry tự động cho attempt FAILED. Hiện người bấm "Chạy lại".
- Chạy song song nhiều trial. Hiện tuần tự, mỗi lúc 1 lần chạy.
- Cổng chặn trước khi thay đổi config (preventive admission gate), xem `MIGRATION_PLANNER_DESIGN.md` mục 4.
- LLM tự host cho bước trích xuất của Config Collector (không gửi config ra API bên ngoài).

---

## 14. Điểm mở

1. **Số liệu chính thức của chương Evaluation**: hiện là 09 (Tier 1 vs B1/B2). Có thêm số liệu "Tier 1 + Tier 2" tính từ DB của Orchestrator không?
2. **Hạn chót chụp snapshot**: chờ kết quả kiểm tra retention / storage / giờ harness bắt đầu chạy trên Azure (mục 2.5).
3. `08_export_trial_data.py::split_shared_collection` đang làm rơi kịch bản CONFIG_ONLY_UNRESOLVED (record đích = None không khớp owner_map). Không ảnh hưởng số liệu Tier 1, nhưng nên sửa cho nhất quán với Orchestrator.
