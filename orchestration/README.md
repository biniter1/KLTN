# Orchestration layer — Batch Orchestrator + API Server

Cài đặt `ORCHESTRATION_DESIGN.md`, áp luôn các cập nhật của `DASHBOARD_DESIGN.md` (29/9):
`human_decisions` tổng quát (11 `reason_code`), `pipeline_run_log` ghi cả `RUNNING`,
`collection_quality_log` dạng ngang theo mục 3.A, 2 cột mới của `agent_rounds`.

Cùng chuẩn với `tier1_inference/` và `tier2_agent/`: test kiểu fixture/factory
(`tests/conftest.py`), không cần cluster, Prometheus, LLM hay GitHub thật. Hiện có
**120 test, coverage khoảng 93%**. Phần chưa cover là 2 CLI và các lớp gọi hạ tầng thật.

```bash
cd orchestration
pip install -r requirements-dev.txt
python3 -m pytest
```

Bố cục repo giả định: `orchestration/` nằm cùng cấp với `tier1_inference/`,
`tier2_agent/`, `infra/`. `orchestration/_paths.py` tự thêm các thư mục đó vào
`sys.path`. Nếu bố cục khác, đặt `KLTN_TIER1_PATH`, `KLTN_TIER2_PATH`,
`KLTN_SCENARIOS_PATH`.

## Chạy thật (sau khi đủ 7 ngày quan sát)

```bash
# Terminal 1 — API Server (tiến trình dài hạn)
export GITHUB_TOKEN="..."            # tuỳ chọn; không có thì GitHub giới hạn 60 req/giờ
python3 -m orchestration.cli_api --db ../kltn.sqlite3 --port 8080

# Terminal 2 — Batch Orchestrator, mỗi trial 1 tiến trình
kubectl -n istio-system port-forward svc/prometheus 9090:9090 &
export OPENAI_API_KEY="..."          # provider Config Collector (CONFIG_COLLECTOR_LLM_PROVIDER)
export ANTHROPIC_API_KEY="..."       # Tier 2 — bỏ qua nếu --skip-tier2
for i in $(seq -w 1 15); do
  python3 -m orchestration.cli_batch --run-id trial-$i --namespace shared-eval \
      --db ../kltn.sqlite3 --source-root ~/src/microservices-demo --api-server http://localhost:8080
done
```

Exit code: `0` = SUCCESS, `1` = FAILED (lý do ghi trong `pipeline_run_log`). Các cờ hay dùng:

| Cờ | Ý nghĩa |
|---|---|
| `--skip-tier2` | Chỉ chạy tới Tier 1. Bước `tier2` vẫn ghi `SUCCESS` kèm `detail.skipped=true`, không im lặng |
| `--stateful` | Tier 1 dùng hysteresis theo lịch sử của chính trial đó (bộ demo). Mặc định là stateless |
| `--allow-short-window` | Chỉ để thử pipeline: bỏ qua kiểm tra đã đủ `--window-days` |
| `--deployed-at <ISO>` | Tự khai thời điểm deploy. Mặc định đọc `startTime` nhỏ nhất của pod `test-harness=true` |
| `--fail-on-scrape-gap` | Hủy trial khi có scrape gap. Mặc định chỉ ghi cờ vào `collection_quality_log` |
| `--image-config` | Đọc ENV/ENTRYPOINT/CMD của image qua `crane` |

## Ánh xạ thiết kế → code

| Thiết kế | Code |
|---|---|
| ORCH mục 1 — luồng 1 trial | `orchestration/batch.py` (`BatchOrchestrator.run`) |
| ORCH mục 2 — hủy trial khi lỗi, `pipeline_run_log` RUNNING→SUCCESS/FAILED | `run_log.py`, `batch.py` |
| ORCH mục 3 — tách bảng theo người ghi, WAL, view `dependency_current_state` | `db.py`, `tests/test_write_boundaries.py` |
| ORCH mục 4 — hàng đợi không chặn cạnh khác | `api/readers.py::review_queue` (suy ra lúc query, không có tiến trình chờ) |
| ORCH mục 6 — các route | `api/app.py` |
| ORCH mục 7 — gatekeeper Tier 2 | `api/gatekeeper.py` |
| DASH mục 2 — 11 reason_code + action + payload | `api/review.py`, `api/decisions_store.py` |
| DASH mục 3.A — `collection_quality_log` | `tier1/config_collector/store.py` (bảng), `batch.py` (ghi) |
| DASH mục 3.B — 2 cột `agent_rounds` | đã có sẵn trong snapshot, bổ sung migration (`tier2_agent/storage.py::migrate`) |
| DASH mục 4 — khoá liên kết | `identity_key`, `run_id`/`attempt_id`, `tier2_session_id` (xem dưới) |

## Các quyết định đã chốt khi code

1. **Bước "cấy + chờ K ngày" chỉ là bước kiểm tra (`scenario_check`).** Hạ tầng dùng chung
   đã cấy cả 15 virtual trial một lần. Orchestrator chỉ kiểm tra 3 điều: có ground truth,
   có owner_map, và đã đủ `window_days` kể từ lúc harness pod đầu tiên chạy. Sau đó thu
   traffic/config của namespace chung và tách phần của trial theo `pair_key` + `owner_map`,
   dùng đúng luật của `08_export_trial_data.py::split_shared_collection`.
2. **Tier 2 do Batch Orchestrator gọi** (mục 1 bước 6). Mỗi cạnh escalate chạy 1 phiên
   Brain. API Server chỉ làm gatekeeper cho tool call của Tier 2.
3. **Tier 1 mặc định stateless**, giống `tier1.experiment.run_trial`. Ở hạ tầng dùng chung,
   15 trial có cùng `identity_key` cho các cạnh thật của Online Boutique. Nếu dùng
   `conn` thật, trial sau sẽ đọc `previous_status` của trial trước. `--stateful` giới hạn
   lịch sử trong đúng trial đó, bằng cách chép sang 1 DB tạm trong RAM nên không phải sửa
   Tier 1.
4. **`collection_quality_log` dạng ngang**, mỗi `(run_id, attempt_id, module)` 1 dòng, cột
   đúng tên mục 3.A. Có thêm 4 cột ngoài 3.A: `series_total`/`series_kept_for_trial`,
   `evidence_total`/`evidence_kept_for_trial` (số trước và sau khi tách trial), và
   `refs_rejected_invalid_host`. Nếu file SQLite cũ còn bảng dạng dọc, bảng đó được đổi
   tên thành `collection_quality_log_legacy`, không bị xoá.
5. **`attempt_id`** (`trial-07.a2`): mỗi lần chạy lại 1 trial là 1 attempt mới, không ghi đè
   attempt cũ. View `dependency_current_state` chỉ lấy attempt SUCCESS mới nhất của mỗi
   trial. Attempt FAILED vẫn được giữ để truy vết nhưng không bao giờ lọt vào view.
   Quyết định của người khớp theo `identity_key` (và tùy chọn theo `run_id`), nên vẫn còn
   khi trial chạy lại. Có test chứng minh điều này.
   - Với 5 bảng của Config Collector, cột `run_id` chứa **attempt_id**, vì các bảng đó không
     có cột attempt riêng.
6. **Session id Tier 2** = `<attempt_id>#<sha1(identity_key)[:12]>`. Lý do:
   `agent_sessions.run_id` là PRIMARY KEY và mỗi cạnh là 1 phiên.
   `dependency_evidence.tier2_session_id` trỏ ngược về phiên đó.
7. **Append-only có 3 lớp**: không có câu UPDATE/DELETE nào trong code; trigger SQLite chặn
   UPDATE/DELETE trên `human_decisions` và `agent_tool_call_log`; test quét mã nguồn đảm bảo
   chỉ `decisions_store.py` ghi `human_decisions`, chỉ `gatekeeper.py` ghi log gatekeeper, và
   `api/` không có câu ghi nào vào bảng của Batch. Các route đọc dùng kết nối `mode=ro`.
8. **Quy tắc tạo hàng đợi review** (`api/review.py::edge_review_reasons`):
   - `NEEDS_MANUAL_SELECTOR` → `NEEDS_MANUAL_SELECTOR`.
   - `NOT_OBSERVED` + config sạch → `CLEAN_CONFIG_MATCH`, kèm `quote`/`key` nguyên văn.
   - Tier 2 `CONFLICTING_EVIDENCE` → `CONFLICTING_EVIDENCE`.
   - Tier 2 `NEEDS_MONITORING` hoặc `INSUFFICIENT_BASIS` → `AGENT_INSUFFICIENT_BASIS`.
   - (`OBSERVED_STRONG` hoặc Tier 2 `CONFIDENT`) + đích thuộc `kube-system` hoặc
     `scope=EXTERNAL` → `HIGH_BLAST_RADIUS`.
   - DNS auto-allow không bao giờ vào hàng đợi.
   - `TOOL_ADMIN_ATTENTION` nằm ở route riêng `/ops-alerts`. Nó được phát hiện khi 1 tool
     lỗi liên tiếp ≥ 2 lần trong `trace_log`.

## Các lỗi tích hợp đã sửa trong code có sẵn (kèm test)

| File | Lỗi | Sửa |
|---|---|---|
| `tier1/storage.py` | Không có `run_id`, nên `orchestrator_hook` `UPDATE ... WHERE run_id` sẽ lỗi `no such column` | Thêm `run_id`, `attempt_id`, `identity_key`, `tier2_session_id`, `tier2_*` + `migrate()` cho file cũ |
| `tier1/storage.py` | `destination_*` là NOT NULL, nên cạnh FOUND_UNRESOLVED (dest = None) không ghi được | Cho phép NULL. Chưa lộ trước đây vì `experiment.py` không ghi SQLite |
| `tier1/schema.py` | — | Thêm `IdentityKey.as_string()`, cùng định dạng với Tier 2 |
| `tier2_agent/escalation.py` | `identity_key` gặp `TypeError` khi dest = None | `str()` từng phần |
| `tier2_agent/orchestrator_hook.py` | `port = ?` không khớp được NULL; chạy lại trial thì UPDATE dính cả attempt cũ | Dùng `IS ?`, thêm `attempt_id`, `tier2_session_id` |
| `tier2_agent/tools/pr_descriptions.py` | So `startswith` đường dẫn tuyệt đối cục bộ với `changed_files` tương đối của GitHub, nên mọi PR thật đều bị lọc mất | Khớp thêm theo đường dẫn tương đối tính từ gốc repo (≥ 2 thành phần) |
| `tier2_agent/storage.py` | File cũ thiếu 2 cột mục 3.B | `migrate()` |
| `tier1/config_collector/pipeline.py` | Chưa đếm `llm_calls`/`llm_cache_hits` | Bọc client/cache để đếm. Thêm tham số `attempt_id`, `write_quality_log`, `stats_out`. Lệnh gọi cũ của `08_export` vẫn chạy nguyên vẹn |

## Điểm mở / diễn giải (cần Bin quyết)

1. **`TOOL_ADMIN_ATTENTION` dùng action nào?** Mục 2 chỉ ghi "xử lý vận hành", còn enum
   action không có ACKNOWLEDGE. Hiện dùng `APPROVE` với nghĩa "đã xử lý" và bắt buộc
   `payload.note`.
2. **Bảng action cho từng reason_code** (`api/review.py::REASONS`) là phần diễn giải từ cột
   "Hành động con người cần làm" của bảng mục 2. Xem nhanh qua `GET /reason-codes`.
3. **5 reason_code chưa có module sinh ra**: `POLICY_*` (Policy Impact Validator),
   `UNATTRIBUTED_CONFIG_CHANGE`, `EMERGENCY_EXCEPTION_EXPIRED`, `STALE_PERMISSION`
   (Continuity Monitor). Schema và `POST /decisions` đã nhận đủ, nhưng `/review-queue` chưa
   tự sinh 5 loại này. `POST /policy-impact-check` và các bảng `config_change_log`,
   `emergency_exceptions` chưa làm vì Migration Planner chưa được code.
4. **CONFIG_ONLY_UNRESOLVED bị `08_export_trial_data.py::split_shared_collection` loại khỏi
   mọi trial.** Record FOUND_UNRESOLVED có dest = None, nên `pair_key` không khớp owner_map.
   Orchestrator đã thêm luật khớp lại qua `raw_host == "<dest>-nonexistent"` (đúng quy ước
   của `render_manifests.py`). Script 08 **chưa sửa**. Điều này không ảnh hưởng số liệu
   precision/recall của Tier 1, vì cặp đó có ground truth = False và Tier 1 không dự đoán
   nó là dương. Nhưng Tier 2 sẽ không thấy kịch bản này nếu chỉ dùng 08.
5. **Tier 2 không biết hostname không phân giải được.** Schema mục 10 của Tier 1 không có
   `raw_host`, nên escalation của cạnh FOUND_UNRESOLVED chỉ có `destination = None`. Ngoài
   ra, Tier 1 gộp mọi ref FOUND_UNRESOLVED của cùng 1 nguồn thành 1 candidate
   `(ns, src, None, None)`.
6. **`read_extended_traffic_window` gọi thẳng Prometheus từ Orchestrator**, không đi qua
   gatekeeper (README Tier 2, điểm mở 5).
7. **Chưa có xác thực người review.** `decided_by` do client tự khai (phạm vi PoC).
8. **Bước bị kill giữa chừng** (Ctrl+C, máy ảo reboot) sẽ đứng ở `RUNNING` mãi. Dashboard
   nên hiển thị kèm `started_at` để người xem tự nhận ra.

## Chưa kiểm chứng trên hạ tầng thật

`cli_batch.main`, `kubernetes_deployment_clock`, `GitHubPRSource`, `build_brain_runner` là
phần nối hạ tầng thật. Từng khối bên trong đều đã có test, nhưng việc chạy end-to-end trên
cluster Azure, Prometheus, GitHub và Anthropic thật thì chưa. API Server đã khởi động bằng
uvicorn thật và gọi thử `/health`, `/agent-tool-calls/read_pr_descriptions`.
