# Thiết kế — Dashboard & Cơ chế Con người Can thiệp

Tài liệu này trả lời vấn đề thứ 2 cô Dung nêu (23/09/2026): xây dựng một dashboard hiển thị quá trình hoạt động của hệ thống, nơi con người có thể can thiệp/kiểm soát được. Tier 1 và Tier 2 phải hỗ trợ được việc này về mặt kiến trúc. Đây cũng là nơi hiện thực hoá bề mặt hiển thị/hành động cho vấn đề thứ 3 (Zero Trust Continuity Monitor, `MIGRATION_PLANNER_DESIGN.md` mục 2).

> **Cập nhật 08/10/2026 (v2)** — đi cùng `ORCHESTRATION_DESIGN.md` v2:
> - Can thiệp của con người được chia thành **2 nhóm**: quyết định nghiệp vụ (mục 2, giữ nguyên) và **điều khiển vận hành** (mục 2b, mới).
> - Dashboard **thay thế hoàn toàn CLI**. Danh sách màn hình ở mục 7 (mới).
> - Bổ sung các bảng vận hành (mục 3.D), khoá `attempt_id` (mục 4), và yêu cầu kiến trúc 6–7 (mục 5).
> - 2 gap ở `brain.py` (mục 3.B) đã được vá trong code.

> **Cập nhật 12/10/2026 (v2.1)** — đi cùng `CONFIG_EVIDENCE_COLLECTOR.md` v2.1:
> - Config Collector có **nhiều nguồn sinh candidate** (quy tắc công ty, regex chung, LLM bật/tắt được). Dashboard phải cho thấy **nguồn nào tìm ra gì** và cho **cấu hình** các nguồn đó (mục 2b, 3.A, 3.D, 7).
> - **Truy vết config tới gốc** cho dev: từ 1 cạnh → bằng chứng → item config → ConfigMap/file/image; xem được cả candidate bị loại kèm lý do (mục 4, 7.1).
> - Thêm yêu cầu kiến trúc 8 (mục 5).
> - Trạng thái: 📐 thiết kế, chưa code. Trước 29/12: màn hình Config Collector chỉ đọc; quy tắc công ty đọc từ file YAML, **chưa** có trình sửa trên dashboard.
> - **Traffic Collector (read traffic)** có màn hình riêng đầy đủ: bảng series theo khóa, biểu đồ, bảng chất lượng dữ liệu (mục 7.3), và màn hình **Cấu hình Traffic Collector + nguồn dữ liệu** (live Prometheus / snapshot do hệ thống chụp / snapshot người dùng tải lên — mục 7.4). Thêm bảng `traffic_series`, `traffic_daily_counts`; `data_snapshots` có thêm nguồn gốc snapshot (mục 3.A, 3.D). Hiện series **chưa được lưu bảng nào** (đường đánh giá ghi JSON, orchestrator giữ trong bộ nhớ) → phải thêm bước ghi.

**Không thuộc phạm vi tài liệu này**: vấn đề thứ 1 (baseline đánh giá Tier 1) là phương pháp đo **offline** (chạy trên R=15 trial để viết báo cáo khóa luận), không phải một phần của dashboard vận hành. Xem `TIER1_DESIGN.md` mục 13.

Không lặp lại: kiến trúc 2 tiến trình, 2 giai đoạn, 2 nguồn dữ liệu, Job runner, cơ chế dừng và các bảng chính đã chốt ở `ORCHESTRATION_DESIGN.md`. Tài liệu này **mở rộng chi tiết** phía con người:
1. toàn bộ luồng end-to-end nơi con người tham gia;
2. danh mục lý do và schema quyết định tổng quát, cùng các thao tác vận hành;
3. danh mục bảng dữ liệu đầy đủ;
4. khóa liên kết;
5. yêu cầu kiến trúc;
6. điểm mở;
7. màn hình dashboard.

---

## 0. Cách xác định phạm vi — đi từ đầu tới cuối pipeline

Thay vì thiết kế dashboard trước rồi tìm dữ liệu sau, tài liệu này đi ngược lại: liệt kê **mọi điểm trong toàn bộ pipeline (thu thập → Tier 1 → Tier 2 → enforce → giám sát liên tục) nơi cần hiển thị cho người hoặc cần người quyết định**, rồi mới suy ra bảng dữ liệu và schema cần thiết (mục 2, 3). Cách này tránh được tình trạng dashboard chỉ có 1 nút Duyệt/Từ chối chung chung, không đủ cho những loại việc rất khác nhau.

Ngoài các điểm "cần người quyết định về dependency" (nhóm 1), v2 bổ sung các điểm "người điều khiển chính chương trình đang chạy" (nhóm 2, mục 2b).

---

## 1. Sơ đồ tổng — luồng nghiệp vụ + giám sát liên tục

Lưu ý tên gọi: "Phần 1 / Phần 2" ở sơ đồ dưới là 2 phần của **luồng nghiệp vụ** (trước và sau khi enforce). Không nhầm với "Giai đoạn 1 / 2" của `ORCHESTRATION_DESIGN.md` mục 1 (thu dữ liệu trên Azure / chạy hệ thống). Toàn bộ sơ đồ dưới thuộc Giai đoạn 2 của tài liệu đó.

```
PHẦN 1 — THU THẬP → TIER 1 → TIER 2 → ENFORCE
════════════════════════════════════════════════

[Traffic Collector] ⎫
[Config Evidence Collector] ⎬── lỗi bất kỳ module nào → STOP, hiện lỗi rõ
 (kể cả lỗi bước LLM)       ⎭    (pipeline_run_log = FAILED) → 👤 HUMAN (Chạy lại — mục 2b)
  ↑ cấu hình nguồn candidate (quy tắc công ty, bật/tắt LLM, image reader) — 👤 mục 2b
        │ (cả 2 xong, không lỗi)
        ▼
[TIER 1 — scoring + hysteresis]
        │
        ├─ NEEDS_MANUAL_SELECTOR ─────────────────────────► 👤 HUMAN (nhập selector/ipBlock tay)
        │
        ├─ UNKNOWN (traffic mâu thuẫn/không rõ) ──┐
        │                                          │
        ├─ OBSERVED_WEAK (có traffic, chưa đủ) ────┼──► [TIER 2 — AI Agent]  (xem dưới)
        │                                          │
        ├─ NOT_OBSERVED ──┬─ config mù (UNRESOLVED/NOT_FOUND) ─┘
        │                 │
        │                 └─ config sạch (STRUCTURAL/EMBEDDED) ─► 👤 HUMAN (duyệt, khỏi tốn AI)
        │
        └─ OBSERVED_STRONG ──┬─ đích KHÔNG nhạy cảm ──► migrate thẳng
                              └─ đích NHẠY CẢM (kube-system/
                                 EXTERNAL) ──► 👤 HUMAN (xem trước khi tạo YAML — mục 1.5
                                                          MIGRATION_PLANNER_DESIGN.md)

[TIER 2 — kết quả]                       (👤 có thể DỪNG 1 phiên bất kỳ lúc nào — mục 2b)
        ├─ CONFIDENT ───────────────────────────► (đồng thuận, đi tiếp như STRONG)
        ├─ CONFLICTING_EVIDENCE ────────────────► 👤 HUMAN (bằng chứng mâu thuẫn)
        ├─ NEEDS_MONITORING/INSUFFICIENT_BASIS ─► 👤 HUMAN (cạn tool, hoặc người đã dừng phiên)
        └─ tool lỗi liên tiếp (needs_admin_attention) ─► 👤 HUMAN (cảnh báo vận hành — khác nhóm trên)

        │  (mọi nhánh hội tụ về 1 policy candidate)
        ▼
[POLICY IMPACT VALIDATOR]  ← nhận CẢ policy hệ thống sinh ra LẪN policy kỹ sư network viết tay
        │
        ├─ WOULD_BREAK ─────────────────────────► 👤 HUMAN (chặn cứng, không cho apply)
        ├─ UNSUPPORTED_BY_EVIDENCE ──────────────► 👤 HUMAN (khuyến nghị, ghi đè phải kèm lý do)
        └─ COVERED ──────────────────────────────► APPLY

              ════════ ĐÃ ENFORCE (NetworkPolicy thật trên cluster) ════════


PHẦN 2 — ZERO TRUST CONTINUITY MONITOR (chạy liên tục SAU khi enforce)
════════════════════════════════════════════════════════════════════════

  Sự kiện (K8s Audit Log                    Định kỳ (chu kỳ K-ngày,
  webhook, gần thời gian thực)               tái dùng cadence Tier 1 mục 5b)
        │                              ┌──────────────┴───────────────┐
        ▼                              ▼                              ▼
  [config_change_log]         [emergency_exceptions]        [Tier 1 re-score MỌI
   mọi thay đổi config         quét expires_at < now          rule đang enforce]
   (ai, khi nào, GitOps                │                              │
    hay tay)                          hết hạn?                  rớt khỏi STRONG?
        │                        No │      │ Yes               (θ_demote)
        ▼                    (còn hợp lệ)  ▼                     No │  │ Yes
   attribute được?                    👤 HUMAN                (giữ nguyên)│
  (git_commit_sha khớp,          (gia hạn/thu hồi/                       ▼
   hoặc exception ACTIVE)?        làm vĩnh viễn)                    👤 HUMAN
   Có │        │ Không                                          (STALE_PERMISSION
      │        ▼                                                 — xem xét thu hồi)
      │   đối chiếu dependency_current_state
      │   (edge này còn traffic thật không?)
      │     Có │      │ Không
      │        ▼      ▼
      │     mức độ  mức độ CAO
      │     THẤP    ("mở toang không lý do")
      │        └───┬────┘
      │            ▼
      │       👤 HUMAN (UNATTRIBUTED_CONFIG_CHANGE)
      ▼
(ghi nhận bình thường, không cần review)
```

---

## 2. Nhóm 1 — Quyết định nghiệp vụ: danh mục `reason_code` + schema quyết định tổng quát

**Vấn đề với thiết kế gốc**: `ORCHESTRATION_DESIGN.md` bản đầu chỉ có 1 khái niệm hàng đợi (`PENDING_HUMAN_REVIEW` / `review-queue`) với 1 hành động ngầm định (`POST /decisions` duyệt/từ chối nhị phân). Thực tế có **11 lý do khác nhau** đẩy việc vào tay người, và chúng đòi hỏi **hành động khác nhau**, không chỉ duyệt/từ chối:

| `reason_code` | Nguồn | Hành động con người cần làm |
|---|---|---|
| `CLEAN_CONFIG_MATCH` | Tier 1 mục 5 (`NOT_OBSERVED` + config sạch) | Duyệt/từ chối kết luận (nhị phân). Màn hình hiện kèm **đoạn config nguyên văn** (`evidence_refs.quote`), nguồn (`source_kind`, `key`, `source_ref`) và **nguồn candidate** (`extraction_method`) để người review đọc thẳng bằng chứng (mục 7.1) |
| `NEEDS_MANUAL_SELECTOR` | Tier 1 mục 3b | Nhập selector/`ipBlock` tay |
| `HIGH_BLAST_RADIUS` | Migration Planner mục 1.5 | Duyệt/từ chối trước khi tạo YAML |
| `CONFLICTING_EVIDENCE` | Tier 2 (`CONFLICTING_EVIDENCE`) | Đọc trace suy luận AI, tự quyết |
| `AGENT_INSUFFICIENT_BASIS` | Tier 2 (`NEEDS_MONITORING`/`INSUFFICIENT_BASIS`, kể cả phiên bị người dừng tay) | Đọc trace, tự quyết (mức khẩn thấp hơn trên) |
| `TOOL_ADMIN_ATTENTION` | Tier 2 `brain.py` (`needs_admin_attention`) | Xử lý vận hành (tool hỏng). **Khác bản chất**, không phải quyết định dependency |
| `POLICY_WOULD_BREAK` | Policy Impact Validator | Sửa policy xung đột, hoặc ghi đè có lý do |
| `POLICY_UNSUPPORTED_BY_EVIDENCE` | Policy Impact Validator | Khuyến nghị: duyệt tiếp hoặc yêu cầu thêm bằng chứng |
| `UNATTRIBUTED_CONFIG_CHANGE` | Continuity Monitor mục 2.5 | Gán chủ sở hữu / thu hồi / hợp thức hoá qua GitOps |
| `EMERGENCY_EXCEPTION_EXPIRED` | Continuity Monitor mục 2.2 | Gia hạn / thu hồi / chuyển `MADE_PERMANENT` |
| `STALE_PERMISSION` | Continuity Monitor mục 2.8 | Xem xét thu hồi rule không còn traffic hỗ trợ |

**Schema `human_decisions` tổng quát hoá**: thay cho giả định ngầm "chỉ duyệt/từ chối". Vẫn append-only, vẫn CHỈ API Server ghi, không đổi nguyên tắc gốc.

```jsonc
{
  "decision_id": "dec_0231",
  "reason_code": "UNATTRIBUTED_CONFIG_CHANGE",   // 1 trong bảng trên
  "target_ref": { /* identity_key (+ run_id tuỳ chọn), hoặc resource_kind+namespace+name, tuỳ loại */ },
  "decided_by": "...",
  "decided_at": "...",
  "action": "APPROVE" | "REJECT" | "SUBMIT_SELECTOR" | "EXTEND_EXCEPTION" |
             "MAKE_PERMANENT" | "ATTRIBUTE" | "REVOKE" | "OVERRIDE_WITH_JUSTIFICATION",
  "payload": { /* tuỳ action — vd selector, hạn mới, ghi chú lý do ghi đè */ }
}
```

`target_ref` có `run_id` thì quyết định chỉ áp cho trial đó. Không có thì áp cho mọi trial cùng `identity_key`.

`TOOL_ADMIN_ATTENTION` hiển thị ở mục riêng "Cảnh báo vận hành", tách khỏi review-queue nghiệp vụ, vì đây là báo lỗi hạ tầng (tool hỏng) chứ không phải quyết định về một dependency.

---

## 2b. Nhóm 2 — Điều khiển vận hành (MỚI v2)

Đây là can thiệp vào **chính chương trình đang chạy**, không phải quyết định về dependency. Nhóm này **không** ghi vào `human_decisions` mà ghi vào `run_jobs` / `run_control` / `orchestration_settings` / `config_rule_sets` (mục 3.D). Cơ chế chi tiết ở `ORCHESTRATION_DESIGN.md` mục 7–8.

| Thao tác | Khi nào dùng | Cơ chế |
|---|---|---|
| **Chạy** (1 / nhiều / cả 15 trial) | Thay cho gõ CLI | Job runner xếp hàng, chạy tuần tự, mỗi lúc 1 lần chạy |
| **Huỷ** job chưa chạy | Đổi ý trước khi tới lượt | Bỏ khỏi hàng đợi |
| **Dừng** cả lần chạy (≈ SIGTERM) | Chạy không đúng ý, muốn dừng sạch | Dừng tại điểm kiểm tra gần nhất, ghi CANCELLED |
| **Dừng ngay** cả lần chạy (≈ SIGKILL) | Treo, hoặc đang đốt tiền API không chờ được | Kill tức thì, Job runner ghi KILLED |
| **Dừng 1 phiên Tier 2** | AI suy luận lòng vòng ở 1 cạnh | Bật `manual_abort_flag`, Brain dừng cuối vòng, các cạnh khác chạy tiếp. Chỉ có cấp "Dừng" |
| **Chạy lại** | Attempt FAILED / CANCELLED / KILLED, hoặc đổi nguồn live → snapshot | Tạo attempt mới |
| **Chụp snapshot** | Một lần tại mốc T, trước khi Prometheus xoá dữ liệu cũ | Job riêng, ghi `data_snapshots` (`origin = SYSTEM_CAPTURED`) |
| **(v2.1) Tải lên snapshot** | Dữ liệu được thu ở nơi khác (máy khác, cluster khác, bản sao lưu cũ), hoặc Prometheus gốc đã mất dữ liệu | Người dùng tải file lên; hệ thống kiểm tra định dạng + hash trước khi nhận; ghi `data_snapshots` (`origin = USER_UPLOADED`). Snapshot đã nhận là **chỉ đọc**, không sửa |
| **(v2.1) Cấu hình Traffic Collector** | Đổi Prometheus URL, cửa sổ ngày, namespace, nguồn dữ liệu mặc định | Lưu phiên bản mới vào `orchestration_settings`, chỉ áp cho attempt tạo sau đó |
| **Đánh dấu đã xử lý cảnh báo** | Sau khi sửa hạ tầng (vd token GitHub) | Ghi `human_decisions` reason `TOOL_ADMIN_ATTENTION` |
| **(v2.1) Cấu hình Config Collector** | Đổi nguồn candidate: bật/tắt LLM, bật/tắt regex chung, chọn bộ quy tắc công ty, chế độ đọc image | Lưu phiên bản mới vào `orchestration_settings`; quy tắc công ty lưu phiên bản mới vào `config_rule_sets`. **Chỉ áp cho attempt tạo sau đó**, attempt cũ giữ nguyên cấu hình lúc chạy (ghi trong `pipeline_run_log.detail`) |

Không có thao tác dừng việc **thu thập 7 ngày** trên Azure (giai đoạn 1). Phần đó là hạ tầng K8s, nằm ngoài dashboard.

---

## 3. Danh mục bảng dữ liệu

### A. Tier 1 — 2 module thu thập + "trái tim"

| Bảng | Trạng thái | Nội dung |
|---|---|---|
| `pipeline_run_log` | Có | Trạng thái từng bước của mỗi **attempt**: `RUNNING` lúc bắt đầu, rồi `SUCCESS` / `FAILED` / `CANCELLED` + `error_message` + `detail`. Dòng `trial` RUNNING ghi `data_source`, `snapshot_id`, T. **(v2.1)** Dòng `config_collector` ghi thêm cấu hình đã dùng: nguồn candidate bật, `rule_set_version`, `prompt_version`, `model_id`, chế độ image reader |
| `collection_quality_log` | Có (dạng ngang) | Xem schema dưới |
| `config_raw_items` | Có (`CONFIG_EVIDENCE_COLLECTOR.md` mục 6) | Mọi item config sau hợp nhất, kể cả bị ghi đè/bị phễu loại, kèm lý do và `source_ref`. Trả lời được "vì sao dependency X không có trong C_potential" |
| `config_extractions` | Có (`CONFIG_EVIDENCE_COLLECTOR.md` mục 6) | Mọi candidate (từ mọi nguồn) + **(v2.1)** `extraction_method` + kết quả validation (kể cả `REJECTED_UNGROUNDED`, `DROP_SELF`) + cờ parse. Người xem thấy được mỗi nguồn đề xuất gì và validator giữ/loại gì |
| `llm_extraction_cache` | Có (`CONFIG_EVIDENCE_COLLECTOR.md` mục 4.3) | Request/response thô của LLM theo `cache_key`. Không hiển thị mặc định, chỉ để truy vết/tái lập |
| `config_rule_sets` | **(v2.1 📐)** | Bộ quy tắc công ty theo phiên bản (mục 3.D) |
| `traffic_series` | **(v2.1 📐)** Batch ghi sau bước traffic_collector | 1 dòng / series / attempt: `run_id`, `attempt_id`, khóa series (7 phần: source ns/canonical/SA, dest ns/canonical/SA, protocol + port + path), `destination_service_ref`, `count`, `error_count`, `distinct_days`, `last_seen_days_ago`, `workload_revision`, `kept_for_trial` (sau khi tách theo `pair_key`), `data_source` + `snapshot_id`. Ghi **cả** series bị loại khi tách trial (cờ `kept_for_trial = 0`) để truy vết |
| `traffic_daily_counts` | **(v2.1 📐)** | 1 dòng / series / ngày trong cửa sổ: `day_index` (0 = gần T nhất), `count`, `error_count`. Nguồn cho biểu đồ theo ngày. Lấy từ 7 query `@` theo ngày (hiện chỉ có cho HTTP) |
| `dependency_evidence` | Có (`TIER1_DESIGN.md` mục 10) + cột `run_id`, `attempt_id`, `identity_key`, `tier2_session_id`, `tier2_status`, `tier2_original_status` | "Trái tim" Tier 1: mọi feature, score, status. Dashboard hiện **nguyên schema**, không tóm tắt bớt |
| `dependency_current_state` (VIEW) | Có | Evidence của attempt SUCCESS mới nhất JOIN quyết định người mới nhất. Bảng tổng cho màn hình chính |

**Vì sao cần `collection_quality_log`**: cả 2 tài liệu collector đều có bước "kiểm tra chất lượng", nhưng cần một chỗ lưu số đếm tổng hợp cho mỗi attempt. Cụ thể là `TRAFFIC_COLLECTION_PIPELINE.md` mục 2 (kiểm tra `up{job="istio-proxy"}`) và `CONFIG_EVIDENCE_COLLECTOR.md` mục 4 (pipeline 5 bước: cần biết mỗi bước giữ/loại bao nhiêu, đây cũng là số liệu cho mục 9 của tài liệu đó).

Dạng **ngang**: 1 dòng cho mỗi (`run_id`, `attempt_id`, `module`). Cột riêng của module kia để NULL. Nếu file SQLite cũ còn bảng dạng dọc (`metric`, `value`), bảng đó được đổi tên thành `collection_quality_log_legacy`, không xoá.

```jsonc
{
  "run_id": "trial-07",
  "attempt_id": "trial-07.a1",
  "module": "traffic_collector",          // hoặc "config_collector"

  // riêng traffic_collector
  "scrape_gap_detected": false,
  "scrape_gap_targets": [],
  "series_total": 412,                     // trước khi tách trial (namespace dùng chung)
  "series_kept_for_trial": 38,             // sau khi tách theo pair_key
  "series_http": 412,                      // (v2.1) theo protocol — lộ lỗi "không có series TCP"
  "series_tcp": 0,                         // (v2.1)
  "series_port_zero": 412,                 // (v2.1) lộ lỗi "port luôn bằng 0"
  "series_with_errors": 7,                 // (v2.1) có error_count > 0
  "queries_run": 11,                       // (v2.1) số PromQL đã chạy (live) / đã phát lại (snapshot)
  "window_days": 7,                        // (v2.1)

  // riêng config_collector — đếm theo từng bước pipeline
  "workloads_scanned": 14,
  "workloads_not_running": 2,              // (v2.1) template replicas=0 / CronJob chưa chạy
  "images_resolved": 11,                   // (v2.1) đọc được image config
  "images_unresolved": 1,                  // (v2.1) không đọc được (registry private, mạng...)
  "items_collected": 212,                  // [1] sau thu thập
  "items_overridden": 9,                   // [2] bị ghi đè khi hợp nhất
  "items_dropped_by_funnel": {"EMPTY": 6, "BOOLEAN": 11, "BINARY": 0, "SECRET_REF": 2},  // [3]
  "chunks_sent": 16,                       // [4c]
  "llm_calls": 3,                          // kể cả lần retry do sai schema
  "llm_cache_hits": 13,
  "refs_extracted": 41,                    // tổng candidate (mọi nguồn, trước khi gộp trùng)
  "candidates_by_method": {"llm": 38, "regex_generic": 29, "company_rules": 0},  // (v2.1)
  "refs_rejected_ungrounded": 1,           // [5a] hallucination bị chặn
  "refs_parse_mismatch": 1,                // (v2.1) [5a2] host/port đề xuất khác kết quả parse
  "refs_rejected_invalid_host": 0,
  "refs_dropped_loopback": 2,              // [5b]
  "refs_dropped_self": 3,                  // [5f]
  "candidates_found": 24,                  // FOUND_STRUCTURAL + FOUND_EMBEDDED
  "candidates_found_unresolved": 5,
  "candidates_external": 2,
  "evidence_only_by_method": {"llm": 3, "regex_generic": 1, "company_rules": 0},  // (v2.1) cạnh chỉ 1 nguồn tìm ra
  "evidence_total": 31,                    // trước khi tách trial
  "evidence_kept_for_trial": 9             // sau khi tách theo pair_key
}
```

### B. Tier 2 — Brain, suy luận, tool, security

| Bảng | Trạng thái | Nội dung |
|---|---|---|
| `agent_sessions` | Có | State phiên (5 field mục 5 `TIER2_DESIGN.md` + `manual_abort_flag`). `run_id` của phiên = `<attempt_id>#<hash identity_key>` |
| `agent_rounds` | Có, **đã thêm 2 cột** | Xem dưới |
| `trace_log` | Có, rất đầy đủ | `tool_name/tool_input/tool_output/status/error_message/injection_flagged/sanitized_flagged/truncated`. Đã đúng ý "tool call + kết quả + security check", đã liên kết sẵn qua `(run_id, iteration, hypothesis_id, evidence_id)` |
| Context Manager (`build`/`normalize`/`format`) | Không cần bảng riêng | Hàm **thuần túy** (deterministic) trên dữ liệu đã có ở `trace_log` + `agent_rounds`. Dashboard replay lại được bất cứ lúc nào để debug; lưu thêm chỉ tốn chỗ mà không thêm thông tin |

**2 gap phát hiện khi đối chiếu `tier2_agent/tier2_agent/brain.py` — ĐÃ VÁ trong code:**

1. **Cờ injection trên reasoning của Model từng bị vứt bỏ.** Code chỉ giữ `inj_result.text`, mất `inj_result.flagged`. Đã thêm cột `agent_rounds.reasoning_injection_flagged` (bool).
2. **`pending_notices`** (lý do hypothesis bị từ chối, tool_call bị dedup, lỗi liên tiếp...) từng chỉ dùng để nhắc Model ở vòng sau, không lưu cho người xem. Đã thêm cột `agent_rounds.system_notices` (JSON array), ghi đúng các notice sinh ra **trong** vòng đó.

File SQLite tạo trước bản vá được tự bổ sung 2 cột này khi mở (migration).

### C. Zero Trust Continuity Monitor + Policy Impact Validator (chưa code)

| Bảng | Nội dung | Lớp nào |
|---|---|---|
| `config_change_log` | Mọi thay đổi config (`MIGRATION_PLANNER_DESIGN.md` mục 2.4) | Lớp A: toàn bộ lịch sử, kể cả đã attributed |
| `emergency_exceptions` | Ngoại lệ khẩn cấp đang mở, hạn dùng, trạng thái (mục 2.2) | Lớp A (đọc) + Lớp B (khi hết hạn) |
| Impact Report (Policy Impact Validator) | Kết quả kiểm tra 1 policy trước khi apply (mục 1.3) | Lớp B: `WOULD_BREAK` luôn cần người ngay |

### D. Điều khiển vận hành (MỚI v2)

| Bảng | Người ghi | Nội dung |
|---|---|---|
| `run_jobs` | API Server (Job runner), append theo sự kiện | job_id, run_id, attempt_id, PID, trạng thái `QUEUED / RUNNING / EXITED / KILLED / CANCELLED_BEFORE_START / LOST`, exit code, người bấm, thời điểm |
| `run_control` | API Server, append-only | Mỗi lần bấm Dừng / Dừng ngay / Huỷ / Dừng phiên: ai, lúc nào, cấp (`graceful` / `force`), phạm vi (`run` / `tier2_session`), lý do. Là bằng chứng audit cho thao tác vận hành |
| `orchestration_settings` | API Server, append theo phiên bản | Cấu hình chung (Prometheus URL, kubeconfig, namespace, owner_map, ground truth, source-root, mốc T, **đường dẫn** `.env`). **(v2.1)** Thêm khối `traffic_collector`: `default_data_source` (`live` / `snapshot`), `snapshot_id` mặc định, `prometheus_url`, `window_days` (mặc định 7), `namespaces`, `reporter` (`waypoint`), `query_timeout_s`, mốc T. Thêm khối `config_collector`: `sources_enabled` (`llm`, `regex_generic`, `company_rules`), `rule_set_version` đang dùng, `llm_provider`, `llm_model` (nên là snapshot cố định), `prompt_version`, `image_reader` (`auto`/`kind`/`registry`/`crane`/`none`). Lấy bản mới nhất |
| `config_rule_sets` | **(v2.1 📐)** API Server, append theo phiên bản | `rule_set_version`, nội dung quy tắc có cấu trúc (YAML/JSON: `key_pattern` + `value_type`, cặp `host_key`/`port_key`), người lưu, thời điểm, kết quả kiểm tra hợp lệ. **Không nhận regex thô** (tránh ReDoS, tránh viết sai). Trước 29/12: nạp từ file YAML, chưa sửa trên giao diện |
| `data_snapshots` | Job "Chụp snapshot" (phía Batch), hoặc API Server khi người dùng tải lên | snapshot_id, T, thời điểm chụp, đường dẫn, hash manifest. **(v2.1)** Thêm: `origin` (`SYSTEM_CAPTURED` / `USER_UPLOADED`), người tải lên, `window_days`, nguồn Prometheus gốc (URL/ghi chú), kết quả kiểm tra (`VALID` / `INVALID` + lý do), danh sách query có trong snapshot |
| `agent_tool_call_log` | API Server (gatekeeper), append-only | Mọi lời gọi tool của Tier 2 qua gatekeeper, kể cả bị từ chối / rate-limit |

---

## 4. Liên kết — khóa nối các bảng

```
Trial / lần chạy: run_id (trial, vd trial-07) + attempt_id (trial-07.a2)
            pipeline_run_log, collection_quality_log, dependency_evidence, run_jobs đều có cả 2.
            Kết quả chính thức của 1 trial = attempt có dòng trial SUCCESS mới nhất.
Tier 1:  identity_key (chuỗi ns|svc|sa|dest_ns|dest_svc|protocol|port) + run_id/attempt_id
Traffic Collector (v2.1): traffic_series (run_id, attempt_id, khóa series) → gộp lên
            identity_key = dependency_evidence.identity_key (bấm 1 cạnh ở Tier 1 → xem mọi
            series tạo nên nó); traffic_daily_counts → traffic_series; data_source +
            snapshot_id → data_snapshots
Config Collector: run_id (= attempt_id) + item_id (config_raw_items ↔ config_extractions ↔
            config_evidence.evidence_refs) — truy ngược từ 1 cạnh trong dependency_evidence
            về đúng dòng config nguyên văn đã sinh ra nó.
            (v2.1) item_id duy nhất trong cả lần chạy: "<namespace>/i0003" (hiện đánh số lại
            theo từng namespace — chỉ an toàn khi 1 lần chạy quét 1 namespace).
            (v2.1) evidence_refs mang luôn source_ref + extraction_method → hiện được nguồn gốc
            và nguồn candidate mà không cần JOIN thêm.
            (v2.1) rule_set_version → config_rule_sets; prompt_version + model_id →
            llm_extraction_cache.
Tier 2:  run_id của phiên = <attempt_id>#<hash identity_key> (1 phiên = 1 cạnh escalate)
            → iteration → hypothesis_id / evidence_id
Nối 2 tầng: dependency_evidence.tier2_session_id → agent_sessions.run_id;
            agent_sessions.edge_identity_key = dependency_evidence.identity_key
Quyết định người: human_decisions.target_identity_key (+ target_run_id tuỳ chọn)
            khớp dependency_evidence.identity_key → không mất khi chạy lại trial
Vận hành: run_jobs.attempt_id ↔ pipeline_run_log.attempt_id; run_control → job_id / phiên Tier 2
Continuity Monitor: (resource_kind, namespace, name) — đối chiếu chéo với
            dependency_current_state bằng destination_namespace/canonical_service
            tương ứng, để biết 1 thay đổi có khớp 1 cạnh dependency đã biết không.
```

---

## 5. Yêu cầu kiến trúc (rút ra từ toàn bộ mục 0–4)

1. **Ghi tăng dần, không ghi dồn cuối**. Mỗi tool_call/round được ghi ngay lúc xảy ra (đã đúng ở Tier 2). Cùng nguyên tắc cho `pipeline_run_log`: ghi cả trạng thái `RUNNING` lúc bắt đầu, không chỉ lúc xong, để dashboard phản ánh đúng "đang chạy" thay vì im lặng cho tới khi xong.
2. **Không được tính rồi vứt**. Mọi flag/notice tính ra ở bất kỳ đâu trong pipeline đều phải có chỗ lưu. Ví dụ: 2 gap mục 3.B (đã vá); ở Config Collector, mọi item bị phễu loại và mọi candidate bị validator loại đều có dòng log kèm lý do.
3. **Khóa liên kết nhất quán** trên mọi bảng (mục 4), để dashboard JOIN thẳng mà không cần logic ánh xạ thủ công.
4. **WAL + đọc không chặn ghi**. Dashboard đọc real-time qua kết nối read-only trong lúc Batch (kể cả Brain) đang ghi.
5. **Lưu nguồn, replay phần biến đổi thuần túy**. Không lưu riêng output của Context Manager (`build`/`normalize`/`format`), tránh phình schema không cần thiết. Ngược lại, output của LLM (cả Config Collector lẫn Tier 2) **không** phải biến đổi thuần túy nên **phải** lưu nguyên văn.
6. **(v2) Dừng phải có điểm kiểm tra và luôn để lại dấu vết.**
   - Batch kiểm tra yêu cầu dừng giữa các bước, giữa từng cạnh Tier 2, và mỗi vòng của Brain.
   - Mọi lần dừng đều có người bấm + lý do (`run_control`) và trạng thái kết quả: CANCELLED do Batch ghi, KILLED do Job runner ghi.
   - Attempt dở dang không bao giờ lọt vào `dependency_current_state`.
7. **(v2) Mỗi attempt ghi rõ nguồn dữ liệu** (`data_source`, `snapshot_id`, mốc T). Không trộn 2 nguồn trong 1 attempt, và dashboard luôn hiển thị nguồn.
8. **(v2.1) Mỗi bằng chứng config truy được tới gốc và tới nguồn candidate.** Từ 1 cạnh, dashboard phải hiện được (không cần truy vấn tay): item config nguyên văn (đã redact), `source_ref` (ConfigMap/key, file mount, image ref), `extraction_method`, cấu hình Collector của attempt đó (`rule_set_version`, `prompt_version`, `model_id`). **Không bao giờ hiển thị giá trị Secret** (Collector không đọc) hay nội dung `.env`.
9. **(v2.1) Dữ liệu traffic phải được lưu ở mức series** (`traffic_series`, `traffic_daily_counts`), không chỉ số đếm tổng. Không lưu thì dashboard không hiện được bảng/biểu đồ, và không giải thích được vì sao 1 cạnh có score như vậy. Snapshot tải lên phải qua kiểm tra trước khi dùng, và không bao giờ bị sửa sau khi nhận.

---

## 6. Điểm mở / diễn giải

- Tiêu chí `destination_sensitivity` (`HIGH_BLAST_RADIUS`) hiện chỉ dùng 2/3 tiêu chí khả dĩ: `kube-system` hoặc `scope=EXTERNAL`, bỏ RBAC-đọc-Secret. Xem `MIGRATION_PLANNER_DESIGN.md` mục 1.5 và mục 4.
- Cổng chặn trước khi thay đổi (preventive admission gate qua ValidatingAdmissionWebhook): cân nhắc và không làm ở PoC, lý do đầy đủ ở `MIGRATION_PLANNER_DESIGN.md` mục 4.
- N (số snapshot liên tiếp) cho ablation hysteresis: xem `TIER1_DESIGN.md` mục 13.4, chưa chốt giá trị.
- Ngưỡng thời gian khớp `git_commit_sha` với `config_change_log` khi có độ trễ GitOps controller: chưa có giá trị placeholder, cần đo thực nghiệm (`MIGRATION_PLANNER_DESIGN.md` mục 4).
- **(v2) Action cho `TOOL_ADMIN_ATTENTION`**: enum action không có ACKNOWLEDGE. Code đang diễn giải `APPROVE` = "đã xử lý xong", bắt buộc `payload.note`. Chờ Bin xác nhận.
- **(v2) Bảng action hợp lệ cho từng reason_code** (code: `api/review.py`, xem nhanh qua `GET /reason-codes`) là diễn giải từ cột "Hành động con người cần làm" ở mục 2. Chờ Bin rà.
- **(v2) 5 reason_code của Policy Impact Validator / Continuity Monitor**: schema và `POST /decisions` đã nhận đủ, nhưng hàng đợi chưa tự sinh vì 2 module đó chưa có code.
- **(v2.1) Trình sửa quy tắc công ty trên giao diện**: sau 29/12. Trước đó nạp từ YAML, dashboard chỉ hiển thị bộ quy tắc + phiên bản đang dùng.
- **(v2.1) Có cần "thử quy tắc" (dry-run) trước khi lưu không**: tức chạy bộ quy tắc mới trên config của attempt gần nhất và hiện số candidate tìm thêm/mất đi. Hữu ích nhưng chưa chốt.

---

## 7. Màn hình dashboard (MỚI v2 — dashboard thay thế CLI)

| Màn hình | Nội dung | Nút bấm |
|---|---|---|
| Tổng quan | 15 trial: attempt gần nhất, trạng thái, nguồn dữ liệu, bước đang chạy (realtime) | Chạy cả 15 |
| Chạy | Chọn trial, nguồn (live / snapshot), tuỳ chọn (bỏ Tier 2, stateful…) | Chạy, Thêm vào hàng đợi |
| Hàng đợi / đang chạy | Job đang chạy + job chờ, tiến độ từng bước, PID | Dừng, Dừng ngay (gợi ý sau 60 giây), Huỷ |
| Lịch sử trial | Mọi attempt của 1 trial, nguồn dữ liệu, cấu hình Collector đã dùng, so sánh kết quả | Chạy lại (cùng nguồn / đổi nguồn) |
| Traffic Collector | **(v2.1 mở rộng, xem 7.3)** 4 tab: Bảng series · Biểu đồ · Chất lượng dữ liệu · Nguồn & truy vết | Xuất CSV |
| Config Collector | **(v2.1 mở rộng, xem 7.1)** 4 tab: Phễu · Candidate & xác thực · Truy vết cạnh · Đóng góp theo nguồn | — |
| Tier 1 | Từng cạnh: feature, score, status, lý do escalate. Bấm cạnh → mở tab "Truy vết cạnh" của Config Collector | — |
| Tier 2 | Từng phiên: từng vòng suy luận, hypothesis, tool đã gọi, cờ injection, system notices, nhãn "dừng tay" | Dừng phiên |
| Review (Lớp B) | Hàng đợi theo `reason_code`, kèm bằng chứng (config nguyên văn + nguồn gốc + nguồn candidate, trace Tier 2) | Duyệt / Từ chối / Nhập selector / … (theo mục 2) |
| Cảnh báo vận hành | Attempt FAILED, job LOST, `TOOL_ADMIN_ATTENTION`, lời gọi gatekeeper bị từ chối, **(v2.1)** image không đọc được config | Đánh dấu đã xử lý |
| Snapshot | Danh sách snapshot, **nguồn gốc** (hệ thống chụp / người dùng tải lên), mốc T, cửa sổ ngày, hash, kết quả kiểm tra, đủ file chưa, cảnh báo hạn retention của Prometheus | Chụp snapshot, **Tải lên snapshot** |
| **Cấu hình Traffic Collector** (v2.1) | Xem 7.4 | Lưu (tạo phiên bản mới), Kiểm tra kết nối Prometheus |
| Cấu hình | Prometheus URL, kubeconfig, namespace, owner_map, ground truth, source-root, mốc T, **đường dẫn** `.env` (không bao giờ hiển thị nội dung) | Lưu |
| **Cấu hình Collector** (v2.1) | Xem 7.2 | Lưu (tạo phiên bản mới) |

**Realtime**: polling 2–3 giây qua API Server (kết nối read-only, WAL). Không cần WebSocket ở quy mô PoC.

**Truy cập**: API Server chỉ nghe `localhost`. Trên Azure thì vào qua SSH tunnel; trên laptop thì mở thẳng. Không làm đăng nhập ở PoC (`ORCHESTRATION_DESIGN.md` mục 10).

### 7.1. (v2.1) Màn hình Config Collector — chi tiết

Mục tiêu: dev/người review **theo dõi được** Collector đã thấy gì, đề xuất gì, giữ/loại gì và vì sao — không cần đọc SQLite.

| Tab | Hiển thị | Nguồn dữ liệu |
|---|---|---|
| **Phễu** | Số đếm từng bước: workload quét (đang chạy / chỉ template), item thu, bị ghi đè, bị phễu loại theo lý do, chunk, lời gọi LLM, cache hit, image đọc được / không. Bấm một con số → danh sách item tương ứng (key, giá trị đã redact, `source_ref`, lý do) | `collection_quality_log`, `config_raw_items` |
| **Candidate & xác thực** | Mọi candidate: workload/container/key, `quote`, host:port đề xuất, host:port sau parse, **nguồn** (`llm` / `regex_generic` / `company_rules`), kết quả (`FOUND_*` / `REJECTED_*` / `DROP_*`), lý do, cờ. Lọc theo nguồn, theo kết quả; mặc định hiện cả candidate bị loại | `config_extractions` |
| **Truy vết cạnh** | Chọn 1 cạnh (từ Tier 1 hoặc tìm theo service) → chuỗi: cạnh → `config_finding` + cờ → từng `evidence_ref` (item_id, container, key, `quote` tô sáng trong giá trị đầy đủ) → `source_ref` (ConfigMap `<tên>/<key>`, file `<đường dẫn>`, image `<ref>`) → nguồn candidate → cấu hình Collector của attempt (`rule_set_version`, `prompt_version`, `model_id`). Nút xem response LLM thô (từ `llm_extraction_cache`) | `config_evidence`, `config_raw_items`, `config_extractions`, `llm_extraction_cache`, `pipeline_run_log` |
| **Đóng góp theo nguồn** | Mỗi nguồn đề xuất bao nhiêu, bao nhiêu được xác nhận, bao nhiêu bị loại (theo lý do), và **cạnh chỉ 1 nguồn tìm ra** (vd "LLM tìm thêm 3 cạnh mà quy tắc/regex bỏ sót"). Giúp tổ chức quyết định có đáng bật LLM không | `collection_quality_log.candidates_by_method`, `evidence_only_by_method`, `config_extractions` |

Quy tắc hiển thị: chỉ hiển thị **giá trị đã redact** (bản đã gửi LLM); không có giá trị Secret nào để hiển thị (Collector không đọc). Placeholder `<redacted>` / `<fieldRef:...>` hiển thị đúng như vậy.

### 7.2. (v2.1) Màn hình Cấu hình Collector

| Khu vực | Nội dung | Ghi chú |
|---|---|---|
| **Nguồn candidate** | 3 công tắc: `LLM`, `Regex chung`, `Quy tắc công ty`. Tắt LLM = chạy hoàn toàn tất định, không gửi dữ liệu ra ngoài (môi trường air-gapped / chính sách công ty) | Ít nhất 1 nguồn phải bật |
| **LLM** | Provider, model (gợi ý ghim snapshot cố định), `prompt_version` (chỉ đọc) | Đổi model/prompt làm cache cũ không còn dùng được — hiện cảnh báo |
| **Quy tắc công ty** | Danh sách quy tắc có cấu trúc: `key_pattern` (glob) + `value_type` (`host` / `host_port` / `url`); cặp `host_key` + `port_key`. Phiên bản đang dùng + lịch sử phiên bản | **Không nhận regex thô.** Trước 29/12: chỉ đọc, nạp từ YAML |
| **Image config** | Chế độ: `auto` (node kind → registry) / `kind` / `registry` / `crane` / `none` | |

Lưu = tạo phiên bản mới (`orchestration_settings`, `config_rule_sets`), chỉ áp cho attempt tạo sau đó. Attempt cũ luôn hiển thị cấu hình **lúc nó chạy**.

### 7.3. (v2.1) Màn hình Traffic Collector (read traffic) — chi tiết

Mục tiêu: thấy được Prometheus đã cho ra những series nào, mỗi khóa có bao nhiêu traffic, phân bố theo thời gian ra sao, và dữ liệu có đáng tin không — trước khi Tier 1 dùng nó.

| Tab | Hiển thị | Nguồn dữ liệu |
|---|---|---|
| **Bảng series** | Mỗi dòng 1 series theo khóa: source (ns / canonical / SA) → dest (ns / canonical / SA), protocol, port, path, `count`, `error_count`, tỉ lệ lỗi, `distinct_days`, `last_seen_days_ago`, `kept_for_trial`. Lọc theo source/dest/protocol/trial; sắp xếp theo count. **Nhóm theo cạnh** (gộp series cùng `identity_key`) để thấy 1 cạnh gồm những path nào. Bấm 1 dòng → biểu đồ theo ngày của series đó | `traffic_series`, `traffic_daily_counts` |
| **Biểu đồ** | (1) Cột theo ngày (7 ngày) cho series/cạnh đang chọn — thấy ngay traffic thưa hay đều. (2) Top N cạnh theo `count` (cột ngang, thang log vì chênh lệch rất lớn). (3) Phân bố `distinct_days` (bao nhiêu cạnh xuất hiện 1, 2, … 7 ngày) — nhóm thưa nằm ở đây. (4) Tỉ lệ lỗi theo cạnh. (5) Ma trận source × dest (ô = count) cho cái nhìn toàn cảnh | `traffic_series`, `traffic_daily_counts` |
| **Chất lượng dữ liệu** | Scrape gap (target nào, lúc nào); số series HTTP / TCP; % series `port = 0`; series bị loại khi tách trial; cảnh báo nổi bật cho **lỗi đã biết** (port luôn 0, không có series TCP, `workload_revision` luôn `latest`) để không ai đọc nhầm số liệu | `collection_quality_log` (traffic), `traffic_series` |
| **Nguồn & truy vết** | Attempt này lấy dữ liệu từ đâu: `live` (Prometheus URL, mốc T) hay `snapshot` (snapshot_id, nguồn gốc hệ thống chụp / tải lên, hash). Danh sách PromQL đã chạy/phát lại kèm thời gian và số dòng kết quả. Bấm 1 series → xem dòng kết quả thô của Prometheus tạo ra nó | `pipeline_run_log`, `data_snapshots`, snapshot |

Liên kết: ở màn hình Tier 1, bấm 1 cạnh → mở tab Bảng series đã lọc đúng cạnh đó (và tab Truy vết cạnh của Config Collector) — thấy cả 2 nguồn bằng chứng của cạnh cạnh nhau.

### 7.4. (v2.1) Màn hình Cấu hình Traffic Collector + nguồn dữ liệu

| Khu vực | Nội dung | Ghi chú |
|---|---|---|
| **Nguồn dữ liệu mặc định** | `live` (đọc thẳng Prometheus, ghim mốc T) hoặc `snapshot` (chọn 1 snapshot trong danh sách) | Màn hình "Chạy" vẫn cho đổi nguồn cho từng lần chạy |
| **Prometheus (live)** | URL, `window_days` (mặc định 7), namespace cần thu, `reporter` (mặc định `waypoint`), timeout mỗi query. Nút **Kiểm tra kết nối**: gọi `up` và 1 query mẫu, báo số target và có scrape gap không | Không lưu mật khẩu/token trong bảng; nếu Prometheus cần xác thực thì chỉ lưu **đường dẫn** file bí mật, như `.env` |
| **Snapshot do hệ thống chụp** | Nút Chụp snapshot (tại mốc T): chạy toàn bộ query và lưu **câu trả lời thô** + manifest (T, window, danh sách query, hash từng file). Cảnh báo khi dữ liệu Prometheus sắp hết hạn retention (mặc định 15 ngày) | Đã có trong thiết kế v2 (`ORCHESTRATION_DESIGN.md`) |
| **Snapshot người dùng tải lên** | Tải lên 1 file nén chứa manifest + câu trả lời thô theo **đúng định dạng snapshot của hệ thống**. Hệ thống kiểm tra: đúng cấu trúc, đủ query bắt buộc, hash khớp manifest, T và window hợp lệ. Hợp lệ → vào danh sách với `origin = USER_UPLOADED`; không hợp lệ → báo lý do, không nhận | Chỉ nhận dữ liệu (JSON), không chạy code trong file; giới hạn dung lượng. Dùng khi thu ở máy/cluster khác hoặc khôi phục bản sao lưu |

**Vì sao cho tải lên:** hệ thống legacy thật thường không cho công cụ kết nối thẳng vào Prometheus production; đội vận hành tự xuất dữ liệu rồi đưa vào. Tải lên cũng là đường khôi phục khi Prometheus của môi trường thí nghiệm mất dữ liệu (emptyDir, hết retention).

**Phạm vi trước 29/12:** bảng series + biểu đồ theo ngày + bảng chất lượng dữ liệu + chọn nguồn live/snapshot (do hệ thống chụp). Tải lên snapshot và ma trận source × dest làm nếu còn thời gian.
