# [ĐÃ CHỐT 08/10/2026] Orchestration v2: 2 giai đoạn, 2 nguồn dữ liệu, dừng 2 cấp, dashboard thay CLI

> Trạng thái: **ĐÃ CHỐT VÀ ĐÃ GỘP** (08/10/2026) vào `ORCHESTRATION_DESIGN.md` (v2) và
> `claude/DASHBOARD_DESIGN.md` (v2). Hai tài liệu đó là nguồn chính thức. File này chỉ giữ lại
> làm **biên bản thảo luận** 06–08/10: vì sao chọn mỗi phương án, kèm các lệnh kiểm tra ở mục 12.
>
> Code orchestration giao ngày 06/10 (`orchestration/` trong zip) làm đúng thiết kế CŨ. Mục 11
> liệt kê những gì sẽ phải đổi.

---

## 0. Vì sao phải sửa thiết kế cũ

`ORCHESTRATION_DESIGN.md` mục 1 mô tả Batch Orchestrator làm luôn bước *"cấy kịch bản → chờ K
ngày → thu thập"*. Thực tế vận hành khác:

- Hạ tầng dùng chung (`05_deploy_shared.sh`) cấy cả 15 virtual trial **một lần**, rồi quan sát
  chung 7 ngày trên máy ảo Azure. Việc này do `infra/` lo, không do Orchestrator.
- Orchestrator chỉ có việc khi đã đủ dữ liệu.
- Cô Dung yêu cầu một dashboard cho con người theo dõi và can thiệp. Bin muốn dashboard này
  **thay thế hoàn toàn CLI**: chạy trial, dừng, chạy lại đều bấm trên giao diện.

---

## 1. Hai giai đoạn tách rời

| | Giai đoạn 1: Thu dữ liệu | Giai đoạn 2: Chạy hệ thống của đồ án |
|---|---|---|
| Ở đâu | Máy ảo Azure: kind + Istio ambient + Online Boutique + harness + Prometheus | Máy ảo Azure (nguồn live) **hoặc** laptop (nguồn snapshot), xem mục 3 |
| Ai làm | Script `infra/` 00→07 | Batch Orchestrator + API Server + Dashboard |
| Kéo dài | 7 ngày, cluster tự chạy | Vài phút mỗi trial. Phần lâu nhất là Tier 2 (gọi LLM) |
| Con người can thiệp | Không có trên dashboard. Dashboard **không** có nút dừng việc thu thập | Toàn bộ mục 5 |

Riêng phần **tính số liệu đánh giá** (precision/recall, B1/B2, CI 95%, Wilcoxon) vẫn do
`tier1.experiment` / `09_run_experiment.py` làm như cũ. Orchestrator không thay phần này.

---

## 2. Thành phần và vai trò

```
           Trình duyệt (Dashboard UI)
                    │  HTTP, polling 2–3 giây
                    ▼
┌──────────────── API Server (luôn chạy) ─────────────────┐
│ • Đọc: trạng thái, kết quả, chất lượng thu thập, trace Tier 2
│ • Human review: ghi human_decisions (append-only)
│ • Gatekeeper Tier 2: /agent-tool-calls/* (token GitHub, rate-limit, log)
│ • Job runner (MỚI): hàng đợi chạy, khởi động Batch làm TIẾN TRÌNH CON,
│   gửi SIGTERM/SIGKILL, ghi lại PID + exit code
└────────────────────┬─────────────────────────────────────┘
                     │ spawn / signal
                     ▼
     Batch Orchestrator (1 tiến trình / 1 lần chạy / 1 trial)
     scenario_check → traffic → config → Tier 1 → Tier 2 → thoát
                     │
                     ▼
            SQLite dùng chung (WAL), tách bảng theo người ghi
```

Các nguyên tắc cũ được **giữ nguyên**:
- 2 loại tiến trình có vòng đời khác nhau.
- Mỗi bên chỉ ghi bảng của mình.
- `human_decisions` chỉ thêm, không sửa/xoá.
- Lỗi ở bất kỳ bước nào thì hủy cả lần chạy, không fallback âm thầm.

Thay đổi duy nhất về kiến trúc: **API Server là bên khởi động Batch** (thay cho việc người tự
gõ CLI). Batch vẫn là tiến trình riêng và vẫn tự ghi bảng của mình.

---

## 3. Hai nguồn dữ liệu cho Batch Orchestrator

### 3.1 Nguồn `live`: đọc trực tiếp cluster Azure
- Đọc Prometheus và K8s API của cluster đang chạy.
- **Mọi query Prometheus được ghim tại mốc T** (mục 3.4), không tính tại "bây giờ". Nếu không
  ghim, `increase(...[7d])` chạy lúc demo sẽ ra cửa sổ khác lúc đánh giá, và kết quả demo
  không khớp báo cáo.

### 3.2 Nguồn `snapshot`: phát lại bản ghi đã chụp
- Có một thao tác **"Chụp snapshot"** (một nút trên dashboard, chạy như một job), thực hiện
  một lần tại mốc T khi cluster còn khỏe. Nó lưu dữ liệu **THÔ**:
  - Từng câu PromQL và câu trả lời thô tương ứng, tại mốc T.
  - Object K8s thô: pod, service, endpoints, configmap, virtualservice, serviceentry.
  - Cửa sổ traffic mở rộng cho tool `read_extended_traffic_window` của Tier 2: **14 và 30
    ngày** (đã chốt).
  - PR GitHub mà gatekeeper lấy về, cho các service của Online Boutique.
  - File manifest: mốc T, thời điểm chụp, namespace, hash từng file.
- Nguồn snapshot **phát lại** đúng các câu trả lời đó. Từ Config Collector (LLM dùng lại cache),
  Tier 1, Tier 2 trở đi, **hai nguồn chạy cùng một đường code**. Chỉ khác chỗ lấy dữ liệu đầu vào.
- Lý do không dùng file JSON của `08_export`: file đó đã xử lý sẵn (TrafficSeries,
  ConfigEvidenceRecord), nên sẽ bỏ qua bước Config Collector và LLM. Khi đó hai nguồn chạy hai
  đường code khác nhau.
- Giới hạn (chấp nhận được): ở nguồn snapshot, nếu Tier 2 xin một cửa sổ không được chụp sẵn
  (ví dụ 21 ngày), tool trả lỗi "không có dữ liệu". Brain đã có sẵn cơ chế xử lý lỗi tool
  (TIER2_DESIGN.md mục 9.3).

### 3.3 Luật đổi nguồn
- **Đổi bằng tay** (đã chốt). Không tự động.
- Đổi ở mức **cả lần chạy**. **Không bao giờ trộn hai nguồn trong cùng một lần chạy** (cùng lý
  do Config Collector không tự fallback sang regex).
- Mỗi lần chạy ghi `data_source` (`live` | `snapshot`) và `snapshot_id`. Dashboard luôn hiển
  thị nguồn này.
- Kịch bản demo: lần chạy live bị FAILED → bấm "Chạy lại bằng snapshot" → tạo lần chạy mới, đọc
  toàn bộ từ snapshot.

### 3.4 Mốc T
- **T = thời điểm harness pod đầu tiên bắt đầu chạy + 7 ngày** (window_days), làm tròn lên đầu
  giờ kế tiếp.
- T được tính **một lần**, lưu vào cấu hình chung, và dùng chung cho cả 15 trial cùng mọi lần
  chạy lại. Sửa được trên màn hình cấu hình nếu cần.
- `scenario_check` (nguồn live) kiểm tra hiện tại ≥ T. Chưa tới T thì báo FAILED, kèm số giờ
  còn thiếu.

### 3.5 ⚠ Ràng buộc thời gian lưu dữ liệu của Prometheus (cần kiểm tra GẤP)
- Prometheus mặc định chỉ giữ dữ liệu **15 ngày** (`--storage.tsdb.retention.time`). Nếu cluster
  dùng addon mẫu của Istio, storage có thể là `emptyDir`: **pod Prometheus restart là mất sạch
  dữ liệu**.
- Hệ quả:
  - Nguồn live chỉ dùng được trong khoảng ~15 ngày kể từ lúc bắt đầu thu. Demo vào tháng
    12 bằng live là không được, trừ khi đã tăng retention từ trước.
  - **Snapshot phải được chụp trước khi dữ liệu cũ bị xoá.** Với cách tính mốc T ở trên, cần
    chụp trong khoảng từ T tới ~ngày bắt đầu + 15 ngày.
- **KHÔNG restart hay sửa Prometheus khi đang thu dữ liệu**, vì có thể mất 7 ngày dữ liệu. Chỉ
  kiểm tra bằng lệnh đọc (mục 12).

---

## 4. Luồng một lần chạy (Batch Orchestrator)

```
scenario_check   live: hiện tại ≥ T, có ground truth + owner_map
                 snapshot: snapshot tồn tại, hash khớp manifest, có ground truth + owner_map
   ▼  [điểm kiểm tra dừng]
traffic_collector  đọc traffic tại T (live/snapshot) → tách phần của trial theo pair_key
   ▼  [điểm kiểm tra]
config_collector   quét config (live/snapshot) → LLM (cache) → validate → tách theo trial
   ▼  [điểm kiểm tra]
tier1              stateless (mặc định) → ghi dependency_evidence
   ▼  [điểm kiểm tra]
tier2              với từng cạnh escalate:  [điểm kiểm tra]  → 1 phiên Brain
                   (trong Brain: mỗi vòng suy luận đọc manual_abort_flag)
   ▼
trial SUCCESS / FAILED / CANCELLED
```

Bước nào lỗi thì cả lần chạy FAILED, giống thiết kế cũ. Trạng thái mới `CANCELLED` được giải
thích ở mục 6.

---

## 5. Các trường hợp con người can thiệp

### 5.1 Nhóm 1: Quyết định nghiệp vụ (đã có, giữ nguyên `DASHBOARD_DESIGN.md` mục 2)

| Nguồn | Ví dụ `reason_code` | Người làm gì |
|---|---|---|
| Tier 2 | `CONFLICTING_EVIDENCE`, `AGENT_INSUFFICIENT_BASIS` | Đọc trace suy luận, tự quyết |
| Tier 1 | `CLEAN_CONFIG_MATCH`, `NEEDS_MANUAL_SELECTOR` | Duyệt / từ chối / nhập selector |
| Đích nhạy cảm | `HIGH_BLAST_RADIUS` | Duyệt trước khi tạo policy |
| Sau này | `POLICY_*`, Continuity Monitor | Ghi đè, gia hạn, thu hồi… |

Với R=15: không cần người ngồi duyệt trong lúc đo. "Rơi vào hàng đợi review" chỉ được đếm như
một loại kết quả (giữ nguyên ORCH mục 4).

### 5.2 Nhóm 2: Điều khiển vận hành (MỚI)

| Hành động | Mô tả |
|---|---|
| Chạy | Chọn 1 trial / nhiều trial / cả 15, chọn nguồn dữ liệu và tuỳ chọn (bỏ Tier 2, stateful…) |
| Hàng đợi | Các trial chạy **lần lượt, mỗi lúc một cái**. Huỷ được trial chưa tới lượt |
| Dừng / Dừng ngay | Mục 6 |
| Chạy lại | Trên trial FAILED / CANCELLED / KILLED, bằng cùng nguồn hoặc đổi nguồn |
| Chụp snapshot | Mục 3.2 |
| Xử lý cảnh báo vận hành | `TOOL_ADMIN_ATTENTION`, ví dụ tool lỗi liên tiếp vì token GitHub hết hạn |

---

## 6. Dừng: 2 cấp, 2 phạm vi

### 6.1 Dừng cả lần chạy

| Cấp | Cơ chế | Kết quả |
|---|---|---|
| **Dừng** (giống SIGTERM) | API Server gửi `SIGTERM` cho tiến trình Batch. Batch bắt signal, bật cờ "đã yêu cầu dừng", và dừng tại **điểm kiểm tra gần nhất** (mục 4). Nếu đang trong Tier 2, Batch đồng thời bật `manual_abort_flag` của phiên đang chạy để Brain dừng ở cuối vòng hiện tại | Batch tự ghi `pipeline_run_log`: bước đang chạy + `trial` = **CANCELLED**, kèm người bấm và lý do |
| **Dừng ngay** (giống SIGKILL) | API Server gửi `SIGKILL`. Tiến trình chết tức thì | Batch không kịp ghi gì, nên **API Server ghi vào bảng của nó** (`run_jobs`, mục 9): `KILLED` + thời điểm + người bấm + lý do. Dashboard hiển thị KILLED |

- **Độ trễ của "Dừng"**: phải chờ lời gọi bên ngoài đang chạy (1 lời gọi LLM, 1 query
  Prometheus, 1 tool của Tier 2) xong mới tới điểm kiểm tra. Thường vài giây, tệ nhất khoảng
  30 giây (timeout của tool/LLM). Lâu hơn mức đó thì dùng "Dừng ngay".
- **Dashboard tự gợi ý**: bấm "Dừng" mà sau N giây (mặc định 60) vẫn chưa thấy CANCELLED thì
  hiện nút "Dừng ngay". Không tự động kill.
- Ở cả 2 cấp, dữ liệu dở dang của lần chạy **bị ẩn khỏi view `dependency_current_state`** (giữ
  lại để truy vết). Kết quả chính thức vẫn là lần chạy SUCCESS gần nhất, nếu có.

### 6.2 Dừng một phiên Tier 2 (bỏ qua 1 cạnh, chạy tiếp các cạnh khác)

- Cơ chế: API Server bật `manual_abort_flag` của phiên đó (cơ chế đã có trong Tier 2, mục 5 /
  9.1). Brain kiểm tra cờ ở cuối mỗi vòng và kết thúc phiên với
  `stopped_by = observer_policy`, `stop_reason = "manual abort"`.
- Cạnh đó vẫn nhận trạng thái Tier 2 tính từ những gì đã có tới lúc dừng (thường là
  `INSUFFICIENT_BASIS`), nên **tự rơi vào hàng đợi review** `AGENT_INSUFFICIENT_BASIS`. Hợp lý:
  người đã chủ động dừng thì người quyết định. Dashboard hiển thị nhãn "dừng tay" để phân biệt
  với trường hợp AI tự cạn bằng chứng.
- **⚠ Điều chỉnh so với lúc thảo luận: phạm vi này CHỈ có cấp "Dừng", không có "Dừng ngay".**
  Lý do kỹ thuật: mọi phiên Tier 2 chạy chung trong một tiến trình Batch. Signal chỉ tác động
  lên cả tiến trình. Python không có cách an toàn để "giết" một phần đang chạy bên trong một
  tiến trình. Muốn dừng ngay một phiên thì phải "Dừng ngay" cả lần chạy (6.1).

---

## 7. Job runner trong API Server

- Hàng đợi tuần tự, **mỗi lúc chỉ 1 lần chạy**. Lý do: tránh tranh ghi SQLite, tránh đốt quota
  LLM song song, và log dễ đọc.
- Khởi động Batch bằng tiến trình con:
  `python -m orchestration.cli_batch --run-id … --data-source … --env-file <đường dẫn>`
  với tham số lấy từ cấu hình chung (mục 10) cộng với lựa chọn trên màn hình Chạy.
- Ghi vào `run_jobs` (bảng của API Server): job_id, run_id, attempt_id (khi Batch báo về), PID,
  trạng thái job (`QUEUED / RUNNING / EXITED / KILLED / CANCELLED_BEFORE_START`), exit code,
  người bấm, thời điểm.
- **API Server restart khi Batch đang chạy**: lúc khởi động lại, kiểm tra PID trong `run_jobs`.
  - PID còn sống: tiếp tục theo dõi.
  - PID đã chết mà `pipeline_run_log` vẫn ở RUNNING: đánh dấu job `LOST` để người xem quyết.
  Đây là cách xử lý trường hợp "treo ở RUNNING mãi" đã ghi trong README 06/10.

---

## 8. API key (đã chốt phương án b)

- Mọi API key (provider LLM của Config Collector, Anthropic cho Tier 2) nằm trong **một file
  `.env` riêng**, quyền đọc `600`, do Bin tự tạo trên máy chạy.
- API Server **chỉ truyền đường dẫn** file cho tiến trình con. API Server không đọc file, không
  log nội dung, không có route nào trả nội dung file. Tiến trình Batch tự đọc file khi khởi động.
- `GITHUB_TOKEN` là ngoại lệ: thuộc về **API Server** (gatekeeper), đặt trong môi trường của
  API Server. Agent không bao giờ thấy token này.
- Ghi chú trung thực khi bảo vệ: trên cùng 1 máy, cùng 1 user, đây là **kỷ luật code**, không
  phải cách ly thật. Cách ly thật (2 user/2 máy/secret manager) là hướng mở rộng.

---

## 9. Bảng dữ liệu: thêm và sửa

| Bảng | Người ghi | Thay đổi |
|---|---|---|
| `pipeline_run_log` | Batch | Thêm status `CANCELLED`. Dòng `trial` RUNNING ghi thêm `data_source`, `snapshot_id`, `T` vào `detail` |
| `run_jobs` (MỚI) | API Server | Mục 7. Append theo sự kiện (QUEUED → RUNNING → …), không UPDATE |
| `run_control` (MỚI) | API Server | Mỗi lần bấm Dừng / Dừng ngay / Huỷ: ai, lúc nào, lý do, cấp, phạm vi. Append-only. Là bằng chứng audit cho thao tác vận hành |
| `data_snapshots` (MỚI) | Job "Chụp snapshot" (phía Batch) | snapshot_id, T, thời điểm chụp, đường dẫn, hash manifest |
| `orchestration_settings` (MỚI) | API Server | Cấu hình chung (mục 10). Append theo phiên bản, lấy bản mới nhất |
| `agent_sessions.manual_abort_flag` | API Server bật | Đã có. Vẫn ghi qua `tier2_agent.storage` như code 06/10 |

Trạng thái hiển thị của một lần chạy được **tính lúc đọc** từ `pipeline_run_log` + `run_jobs`
(ví dụ KILLED đến từ `run_jobs`). Không bên nào phải ghi đè dữ liệu của bên kia.

---

## 10. Dashboard: các màn hình

| Màn hình | Nội dung | Nút bấm |
|---|---|---|
| Tổng quan | 15 trial: lần chạy gần nhất, trạng thái, nguồn dữ liệu, bước đang chạy (realtime) | Chạy cả 15 |
| Chạy | Chọn trial, nguồn (live/snapshot), tuỳ chọn | Chạy, Thêm vào hàng đợi |
| Hàng đợi / đang chạy | Job đang chạy và job chờ, tiến độ từng bước | Dừng, Dừng ngay, Huỷ |
| Lịch sử trial | Mọi lần chạy của 1 trial, so sánh kết quả | Chạy lại (cùng nguồn / đổi nguồn) |
| Traffic Collector | `collection_quality_log` (traffic), scrape gap | — |
| Config Collector | Item bị phễu loại, reference LLM trích, kết quả validate, đoạn config nguyên văn | — |
| Tier 1 | Từng cạnh: feature, score, status, lý do escalate | — |
| Tier 2 | Từng phiên: từng vòng suy luận, hypothesis, tool đã gọi, cờ injection, system notices | Dừng phiên |
| Review | Hàng đợi theo `reason_code` | Duyệt / Từ chối / Nhập selector / … |
| Cảnh báo vận hành | Trial lỗi, job LOST, `TOOL_ADMIN_ATTENTION`, log gatekeeper bị từ chối | Đánh dấu đã xử lý |
| Snapshot | Danh sách snapshot, T, hash, còn đủ file không | Chụp snapshot |
| Cấu hình | Prometheus URL, kubeconfig, namespace, owner_map, ground truth, source-root, mốc T, đường dẫn `.env` (chỉ đường dẫn) | Lưu |

Realtime bằng polling 2–3 giây, đọc qua kết nối read-only (WAL cho phép đọc trong lúc Batch ghi).
Không cần WebSocket ở quy mô PoC.

---

## 11. So với code giao ngày 06/10: những gì sẽ đổi

| Phần | Thay đổi |
|---|---|
| `batch.py` | Thêm lớp "nguồn dữ liệu" (live ghim T / snapshot phát lại). Thêm điểm kiểm tra dừng + xử lý SIGTERM. Thêm trạng thái CANCELLED. `scenario_check` kiểm tra theo nguồn |
| Mới | Công cụ "Chụp snapshot" (ghi lại câu trả lời thô của Prometheus/K8s/GitHub) và bộ phát lại |
| `api/` | Thêm job runner, các route Chạy/Dừng/Huỷ/Chạy lại/Chụp snapshot/Cấu hình, các bảng `run_jobs`, `run_control`, `orchestration_settings` |
| Mới | Dashboard UI (giao diện web) |
| `08_export_trial_data.py` | Vẫn dùng cho 09 (số liệu Tier 1). Lỗi làm rơi CONFIG_ONLY_UNRESOLVED cần sửa (không ảnh hưởng số liệu Tier 1, nhưng nên nhất quán) |
| Giữ nguyên | Tách bảng theo người ghi, human_decisions append-only, gatekeeper, 11 reason_code, các sửa lỗi tích hợp Tier 1/Tier 2 |

---

## 12. Việc Bin cần kiểm tra ngay trên Azure (chỉ lệnh ĐỌC, không sửa gì)

```bash
# Thời gian lưu dữ liệu của Prometheus (không có cờ này = mặc định 15 ngày)
kubectl -n istio-system get deploy prometheus -o yaml | grep -n "retention"
# Storage là emptyDir (mất khi pod restart) hay PersistentVolume?
kubectl -n istio-system get deploy prometheus -o yaml | grep -n -A3 "volumes:"
# Harness pod đầu tiên bắt đầu chạy lúc nào (để tính mốc T)
kubectl -n shared-eval get pods -l test-harness=true \
  -o custom-columns=NAME:.metadata.name,START:.status.startTime --sort-by=.status.startTime | head -3
```

---

## 13. Điểm mở

Đã chốt ngày 08/10:
- **Truy cập dashboard (câu C)**: API Server chỉ nghe `localhost`. Chạy trên Azure (nguồn live)
  thì vào qua `ssh -L 8080:localhost:8080 …`. Chạy trên laptop (nguồn snapshot) thì mở thẳng.
  Không làm đăng nhập.
- **Dừng 1 phiên Tier 2 chỉ có cấp "Dừng"** (mục 6.2). Chấp nhận giới hạn này.
- Ngưỡng gợi ý "Dừng ngay": **60 giây**.

Còn mở:
1. **Số liệu chính thức của chương Evaluation lấy từ đâu?** Hiện là 09 (Tier 1 vs B1/B2).
   Có cần thêm số liệu "Tier 1 + Tier 2" tính từ DB của Orchestrator không?
2. Hạn chót chụp snapshot: chờ kết quả 3 lệnh ở mục 12.
