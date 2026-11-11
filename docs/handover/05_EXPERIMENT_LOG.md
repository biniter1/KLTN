# 05 — Nhật ký thí nghiệm

Mọi số dưới đây từ lần chạy thật. Số nào không chính thức thì ghi rõ. Không bỏ thí nghiệm thất bại.

## Thiết lập chung

- **Dataset**: Online Boutique (~11 service) + harness ~83 workload trong namespace `shared-eval`, 15 trial ảo (seed `base_seed = 20260101`, `infra/scenarios/scenario_pool.py`). Mỗi trial cấy kịch bản khác nhau: cạnh thưa, cạnh chỉ có config (sạch / UNRESOLVED `-nonexistent`), nhiễu healthcheck/monitoring (prometheus, grafana, blackbox-exporter).
- **Ground truth**: `infra/ground_truth/`, biết trước vì tự cấy.
- **Metric**: precision, recall, F1, recall nhóm thưa (`distinct_days ≤ 2`), `noise_precision` (lỗi định nghĩa — luôn 0); từ `556df77`: `queue_recall`, `queue_size`, allow/review, `false_drop`. Mean ± CI 95% trên 15 trial, Wilcoxon bắt cặp.
- **Baseline**: B1 `count > 0` là STRONG; B2 `count ≥ 20 AND distinct_days ≥ 3`. B3 chưa có.

---

## E1. Tầng 1 lần đầu (trước code lại 15/10) — dữ liệu 08/10

File: `infra/experiment_report.json` (trên repo).

| | P | R | F1 | Recall thưa |
|---|---|---|---|---|
| Tầng 1 | 0,888 | 0,852 | 0,869 | 0,256 |
| B1 | 0,889 | 0,889 | 0,889 | 0,533 |
| B2 | 0,885 | 0,822 | 0,852 | 0,000 |

Recall Tầng 1 < B1 (p≈0,008), > B2 (p≈0,007). Giới hạn: port luôn 0, không có series TCP, `distinct_days` lấy max, validator giả.

## E2. Tầng 1 sau PR #1 — dữ liệu 08/10, validator giả (chạy 18/10, lặp lại trên `main` 30/10)

File: `results/tier1/experiment_report_main_20261030.json`. **Không chính thức** (file trial cũ, validator giả).

| | P | R | F1 | Recall thưa |
|---|---|---|---|---|
| Tầng 1 | 1,000 ± 0,000 | 0,870 ± 0,015 | 0,930 ± 0,009 | 0,378 ± 0,137 |
| B1 | 0,889 ± 0,000 | 0,889 ± 0,000 | 0,889 ± 0,000 | 0,533 ± 0,038 |
| B2 | 0,885 ± 0,008 | 0,822 ± 0,013 | 0,852 ± 0,009 | 0,000 ± 0,000 |

Wilcoxon: T1 vs B1 recall p≈0,025 (T1 thấp hơn), precision p≈0,0001; T1 vs B2 recall p≈0,002, precision p≈0,0004. Đây là số đang có trong Chương 4 file docx.

## E3. Fit trọng số LOTO (18/10) — KẾT QUẢ ÂM

Leave-One-Trial-Out trên 15 file trial 08/10. Kết quả: P 1,000 · R 0,889 · F1 0,941 · thưa 0,533 — recall **trùng B1 cả 15/15 trial**.
5 kiểm tra: 31% cặp (21/67) trùng nguyên giữa 15 trial; permutation AUC ≈ 0,496 ± 0,117 (code không rò nhãn); chỉ healthcheck/monitoring_ratio đã AUC 1,000; `config_match_strength` phương sai 0; 10 random_state std 0.
→ Quyết định giữ trọng số đặt tay. Không có file kết quả trên repo (script chạy tay ngày 18/10). **[TODO]** Bảng LOTO trong docx là bản mã 18/10.

## E4. Lần chạy "29/10" bị loại — KẾT QUẢ KHÔNG HỢP LỆ

`infra/experiment_report_rerun.json`: P 0,941 / R 0,822 / thưa 0,871, recall trùng B2 15/15. Không chạy từ code `main` → revert (`50a99bd`). `docs/analysis/CHUONG4_TIER1_DU_THAO.md` vẫn chứa số này — **không dùng**.

## E5. Tầng 1 trên dữ liệu 20–26/10, validator thật (02/11)

- Cấu hình: `--end-time 2026-10-26T23:59:59Z`, Prometheus lưu trữ `localhost:9091` (dựng từ bản sao `/data` 02/11 04:23 UTC, retention 3650d, ~7,39–7,41 triệu request/ngày), `KubernetesSelectorValidator` (cụm thật, chỉ đọc), Collector `gpt-4o-mini` (82 config record). 107/107 series `port_source = inferred_single_port`.
- File: `results/tier1/experiment_report_v2_20261102.json`. **Đây là số Tầng 1 mới nhất chạy thật.**

| | P | R | F1 | Recall thưa |
|---|---|---|---|---|
| Tầng 1 | 1,000 ± 0,000 | 0,833 ± 0,000 | 0,909 ± 0,000 | 0,000 |
| B1 | 0,886 ± 0,002 | 0,863 ± 0,016 | 0,874 ± 0,009 | 0,267 ± 0,143 |
| B2 | 0,882 ± 0,000 | 0,833 ± 0,000 | 0,857 ± 0,000 | 0,000 |

Wilcoxon: T1 vs B1 recall p=0,0047 (B1 cao hơn), precision p=0,00045; T1 vs B2 recall p=1,0 (bằng nhau mọi trial), precision p=0,00011.
Lưu ý: config, selector, source đọc từ trạng thái cụm 02/11, traffic của 20–26/10.

## E6. Ablation hysteresis (02/11)

trial-01, trial-02, 7 snapshot cắt ngày 1..k. File: `results/tier1/hysteresis_ablation_v2_20261102.json`. Kết quả: cả hysteresis và ngưỡng đơn **0 lần đổi trạng thái** trên 18 cạnh → không phân biệt được.

## E7. Tầng 2 v1 trên 15 trial (02/11) — THẤT BẠI DO LỖI TÍCH HỢP

Commit `e43b72b`, model `gpt-4.1-2025-04-14`. File: `results/tier2_v1/`.
- 19 cạnh escalate (14 trial; trial-14 không có), **19/19 INSUFFICIENT_BASIS**. TP/FP/FN/TN = 0/0/8/11.
- `read_extended_traffic_window` bị từ chối 15/15 lần: tool đòi chuỗi `identity_key`, goal không có.
- 53 lời gọi LLM, 72.067 token vào (không cache), 12.032 cache, 14.900 ra.
- Recall gộp 0,833 (= Tầng 1).

## E8. Tầng 2 v2 trên 15 trial (02/11)

Commit `3dbd66d` (chỉ thêm `goal["identity_key"]`). File: `results/tier2_v2/`.
- 1 CONFIDENT · 2 NEEDS_MONITORING (gốc CONFIDENT) · 16 INSUFFICIENT. TP/FP/FN/TN = 1/0/7/11. 10 lời gọi traffic thành công.
- 63 lời gọi LLM, 106.257 vào, 29.184 cache, 17.579 ra.
- Recall gộp 0,837 ± 0,008 vs Tầng 1 0,833: p = 0,317 (chỉ trial-05 đổi 15/18 → 16/18). **Không có ý nghĩa.** Precision 1,0.
- Trần lý thuyết: dù đúng cả 8 cạnh dương được escalate, recall gộp ≈ 0,859 — đa số FN của Tầng 1 không bao giờ được escalate.
- Danh sách cạnh sai + lý do: `results/tier2_v2/tier2_wrong.json`, `docs/analysis/TIER2_KET_QUA_20261102.md` mục 4. TP trial-05 **không đáng tin** (stance mâu thuẫn với lập luận). Corroboration chặn 1 FP (trial-06, nhầm `genproto/`).

## E9. Thí nghiệm 1 — benchmark bằng chứng code, v2 logic (04/11)

Script `infra/scripts/12_tier2_code_benchmark.py`, commit `cd6134f`, `gpt-4.1-2025-04-14`, 3 lần/case, 90 phiên (89 hợp lệ, 1 crash schema `'neutral'`).
Dương: 15 cạnh thật của Online Boutique. Âm: 15 cặp không gọi nhau (seed 20261104). Mọi case cùng hồ sơ escalate (OBSERVED_WEAK, 534 req, 1 ngày, lỗi 0%), cùng traffic tool, không config → chỉ code/docs/PR phân biệt. **Không phải số 15 trial.**

| Tính dương | TP | FP | FN | TN | P | R |
|---|---|---|---|---|---|---|
| CONFIDENT | 5 | 4 | 40 | 40 | 0,556 | 0,111 |
| Gốc CONFIDENT | 31 | 11 | 14 | 33 | 0,738 | 0,689 |

317 lời gọi LLM, ~602 nghìn token vào, ~94 nghìn ra. 22/30 case khác kết quả giữa 3 lần chạy.
Lỗi Tầng 2 rút ra: (1) giả thuyết không có chiều → cả 4 FP; (2) corroboration bắt buộc traffic → 26/45 phiên dương bị hạ; (3) nhầm code sinh từ proto (8 phiên), output cắt 4000 ký tự bị file sinh lấn; (4) không cố định temperature; (5) stance ngoài schema làm crash.
Dataset: cạnh SPARSE không có bằng chứng code (8/8 có 0 file không sinh tự động); traffic cấy lỗi 100% vì `grpcurl list` đi qua reflection (chỉ shippingservice đăng ký); `ACTIVE_DAYS` tính từ lúc pod start.
→ Kết luận: **cả dataset lẫn Tầng 2 đều có lỗi.** File: `results/tier2_benchmark/bench_v2.json`, `.log`.

## E10. Benchmark v3 (05/11) — DỞ

Commit `14fce2b`. Lần 1: IntegrityError (trùng session id) + 39 phiên 429 (hết credit) → sửa script `40eda52`. Chạy được 50/90 phiên.
- Kết quả trên 50 phiên: recall 14/29, FP 0/21 (v2 trên cùng tập: 3/29).
- 10/10 phiên dương CONFLICTING do kết quả "không tìm thấy" bị gán contradict → sửa `33861dd`.
- Chấm lại offline 50 phiên với no_match trung tính (replay khớp kết quả thật 50/50): 22/29, FP 0. **Không chính thức** — chưa chạy lại thật.
- File: `results/tier2_benchmark/bench_v3_trace_partial.txt`.

## E11. Smoke test metric hàng đợi (05/11) — KHÔNG CHÍNH THỨC

09 trên dữ liệu 08/10 cũ: queue recall Tầng 1 0,944 · B1 0,889 · B2 0,822; admin phải duyệt thêm ~2,3 cạnh/trial. Không có file trên repo.

---

## Bằng chứng gốc nằm ngoài repo

- Máy Bin: `D:\KLTN\Project\infra\tier2_v1\`, `tier2_v2\`.
- VM Azure (đã hết hạn — **có thể mất**): `~/kltn-repo/infra/tier2_results*/`, `kltn_tier2.sqlite3`, `kltn_tier2_v2.sqlite3`, `trial_data_v2/`, bản sao Prometheus 8,2 GB.
