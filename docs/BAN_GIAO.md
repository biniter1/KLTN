# BÀN GIAO KLTN — trạng thái tới 11/11/2026

Đọc file này trước. Sau đó đọc `docs/TRANG_THAI_CHOT.md` (nguồn chuẩn tới 30/10) và `docs/analysis/TIER2_KET_QUA_20261102.md` (kết quả Tầng 2). Khi các file lệch nhau: **file này mới nhất**, rồi tới `TRANG_THAI_CHOT.md`, rồi tới tài liệu thiết kế.

## 1. Đề tài

- Hà Sơn Bin – 23520149 – UIT, GVHD ThS. Trần Thị Dung.
- Khung suy luận phụ thuộc giữa service để chuyển Kubernetes sang NetworkPolicy default-deny (Zero Trust).
- Kiến trúc 2 tầng: **Tầng 1** (traffic Istio ambient + bằng chứng cấu hình, tính điểm, 5 trạng thái) và **Tầng 2** (tác nhân AI, ReAct, 4 tool chỉ đọc). Config Evidence Collector chỉ là nguồn bằng chứng, không phải tầng.
- **Hạn nộp báo cáo: cuối tháng 01/2027.**

## 2. Mục tiêu đã đổi (05/11/2026)

Công cụ **thu hẹp phạm vi**, admin duyệt bước cuối. Không cần tự động hoàn toàn.
- **Ưu tiên giảm FN tối đa. FP cao chấp nhận được** (admin loại khi duyệt).
- Vì vậy đo thêm theo **hàng đợi review**: `queue_recall`, `queue_size`, tách allow/review, `false_drop` (commit `556df77`).

## 3. Repo và nhánh

- Repo `biniter1/KLTN`. Code chuẩn: `main`.
- `tier1-port-gate` = PR #2 (cổng port ở cửa ra, `NOISE_ONLY_REVIEW`, `port_source=config`) — **chưa merge, không merge khi Bin chưa nói**.
- `tier2-run-tooling` = PR #3 — toàn bộ việc Tầng 2 từ 02/11. HEAD có tài liệu này.
- Test tại `556df77`: tier1 418, tier2 197, infra 59, orchestration pass hết.

### Changelog PR #3

| Commit | Ngày | Nội dung |
|---|---|---|
| `1e165fc` | 02/11 | Bảng `llm_usage` (token mỗi lần gọi); script 11 xuất kết quả Tầng 2 + recall gộp |
| `4104c9b` | 02/11 | Thêm `openai` vào requirements Tầng 1 |
| `9818c99` | 02/11 | `--end-time` cho 08 export và `cli_batch` (query traffic tại mốc quá khứ) |
| `e43b72b` | 02/11 | `OpenAIModelClient`, chọn bằng `TIER2_PROVIDER`/`TIER2_MODEL` |
| `3dbd66d` | 02/11 | Đưa `identity_key` vào goal (v1 tool traffic bị từ chối 15/15) |
| `e35c9d4` | 02/11 | Harness: cạnh cấy gọi method gRPC nghiệp vụ thật, cạnh thưa theo ngày lịch (`--sparse-window-start`). **Chưa deploy** |
| `cd6134f` | 04/11 | Script 12 benchmark bằng chứng code; `build_brain_runner(traffic_provider=)` |
| `14fce2b` | 05/11 | v3: F1 polarity pro/con · F2 corroboration 2 nguồn độc lập · F3 lọc code sinh từ proto · F4 temperature 0 · F5 gọi lại 1 lần khi sai schema · F6 window ≥ 8 |
| `33861dd` | 05/11 | Kết quả "không tìm thấy" (`no_match`) là trung tính |
| `40eda52` | 05/11 | Script 12: chặn chạy lại trùng nhãn, dừng khi hết credit |
| `556df77` | 05/11 | `REJECTED_WITH_EVIDENCE` + mã `AGENT_REJECTED`; metric hàng đợi (09, 11) |

Sơ đồ chi tiết Tầng 2 theo code: `docs/TIER2_MAP.html` (mở bằng trình duyệt).

## 4. Số liệu: chính thức và tạm

| Số | Nguồn | Trạng thái |
|---|---|---|
| Tầng 1 P 1,000 · R 0,833 · F1 0,909; B1 R 0,863; B2 R 0,833 | `results/tier1/experiment_report_v2_20261102.json` (dữ liệu 20–26/10, validator thật) | **Chạy thật**, dùng được |
| Tầng 1+2 v1: R 0,833 (19 cạnh escalate, 19 INSUFFICIENT) | `results/tier2_v1/` (commit `e43b72b`) | Chạy thật, có lỗi tích hợp đã ghi |
| Tầng 1+2 v2: R 0,837, p=0,317 so với Tầng 1 (1 TP, 0 FP) | `results/tier2_v2/` (commit `3dbd66d`) | Chạy thật, dùng được |
| Hysteresis ablation: 0 lần đổi trạng thái cả 2 cơ chế | `results/tier1/hysteresis_ablation_v2_20261102.json` | Chạy thật, không phân biệt được |
| Benchmark code v2: CONFIDENT P 0,556 R 0,111; gốc CONFIDENT P 0,738 R 0,689 | `results/tier2_benchmark/bench_v2.*` (commit `cd6134f`) | Chạy thật, **không phải số 15 trial** |
| Benchmark v3: 50/90 phiên, recall 14/29, FP 0/21 | `results/tier2_benchmark/bench_v3_trace_partial.txt` | Dở (hết credit) |
| Chấm lại offline 50 phiên với no_match trung tính: 22/29, FP 0 | tính lại từ vết | **Không phải số chính thức** |
| Smoke test hàng đợi trên dữ liệu 08/10 cũ: queue recall Tầng 1 0,944 · B1 0,889 · B2 0,822 | chạy thử 09 | **Không chính thức** |
| Tầng 1 trên `main` 30/10 (P 1,000 R 0,870) | `results/tier1/experiment_report_main_20261030.json` | Dữ liệu cũ + validator giả, tạm |

Trần recall gộp trên dataset hiện tại ≈ **0,859**: phần lớn FN của Tầng 1 không bao giờ được escalate; cạnh cấy không có dấu vết trong code/docs/PR.

## 5. Kết luận đã rút ra

- "Lỗi do dataset hay Tầng 2?" → **cả hai** (thí nghiệm 1, 04/11).
  - Dataset: cạnh SPARSE không có bằng chứng code (8/8 có 0 file không sinh tự động); traffic cấy lỗi 100% vì `grpcurl list` đi qua reflection mà chỉ shippingservice đăng ký.
  - Tầng 2: giả thuyết không có chiều, corroboration bắt buộc traffic, nhầm code proto, không cố định temperature, crash schema — đã sửa ở v3.
- Không được viết "Tầng 1 tốt hơn baseline". Chỉ "so sánh và phân tích".

## 6. Việc tiếp theo (theo thứ tự)

1. **Thiết kế lại giai đoạn 2 của Tầng 2** (đang dở): (A) thu bằng chứng cố định rồi model chấm, hay (B) agentic với giả thuyết cố định.
2. Chạy benchmark chính thức (cần credit OpenAI hoặc model miễn phí), chạy lại v2 trên cùng model để so.
3. Dựng lại cụm (Azure hết hạn), deploy harness mới với `--sparse-window-start`, thu 7 ngày, chạy lại 15 trial.
4. Viết lại Chương 3 phần Tầng 2 và Chương 4 phần kết quả.

### Câu hỏi đang chờ Bin

- Ground truth cho cạnh nhiễu (healthcheck/monitoring) tính dương hay âm — đang mâu thuẫn với thiết kế hàng đợi.
- Thay phương án "nửa cạnh thưa có env" bằng "cấy code" (overlay) không.
- Các vấn đề Tầng 1 đã thấy: định nghĩa dương quá chặt (đã xử lý bằng metric hàng đợi); TCP tới redis-cart count 0; vài cặp chỉ có config không thành cạnh (cần `trial_data_v2`).

## 7. Môi trường đã dùng

- VM Azure (kind + Istio ambient + Online Boutique + harness ~83 workload trong `shared-eval`, 15 trial ảo, seed 20260101). **Subscription đã hết hạn.**
- Prometheus lưu trữ dựng lại từ bản sao `/data` (tar 8,2 GB, chụp 02/11 04:23 UTC) trên cổng 9091, retention 3650d. Cửa sổ 20–26/10: `--end-time 2026-10-26T23:59:59Z`.
- Config Collector: `gpt-4o-mini`. Tầng 2: `gpt-4.1-2025-04-14` (không có Anthropic key).
- Các bước chạy trên VM: `infra/scripts/vm_steps_tier1_tier2.sh`. Dùng tmux; đặt key bằng `read -rs VAR && export VAR`.

## 8. Quy tắc làm việc của Bin (bắt buộc)

- Không bịa hay giả lập kết quả Tầng 2. Mọi số từ lần chạy thật; không chạy được thì nói rõ vì sao.
- Không yêu cầu dán API key/token vào chat. Chỉ hướng dẫn `export TÊN_BIẾN=...` để Bin tự đặt.
- Không push, không merge PR #2, không sửa `main` khi Bin chưa nói.
- Không chắc thì hỏi trước.
- Không chia sẻ `.env`, API key, kubeconfig.
- Không restart/sửa Prometheus hay dữ liệu `shared-eval`.
- Không sửa file nộp (docx) khi chưa hỏi.
- Trước khi code: liệt kê file + thay đổi, chờ Bin duyệt. Trả lời ngắn, rõ, không vòng vo. Sau mỗi thay đổi cập nhật sơ đồ Tầng 2 và changelog.

## 9. An toàn

- Key OpenAI từng bị dán vào chat (02/11) → phải revoke.
- Khóa SSH `infra/KLTN_key.pem` từng nằm trong lịch sử git public → phải thay.
- Xóa GitHub token đã tạo cho VM sau khi dùng.
