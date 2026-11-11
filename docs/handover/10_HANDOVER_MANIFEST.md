# 10 — Danh mục bàn giao

## Phạm vi thông tin Claude truy cập được khi viết bộ này

| Nguồn | Truy cập | Ghi chú |
|---|---|---|
| Repo `biniter1/KLTN` (mọi nhánh) | Có | Đọc cây file, log; chạy lại test 11/11 |
| Knowledge base Claude Project "KLTN" (219 tài liệu) | Có | Đọc toàn văn 13 tài liệu thiết kế/phân tích + docx báo cáo + TIER1_DESIGN.docx |
| Bộ nhớ dự án (memory) | Có | preferences, ways-of-working, kltn-report, de-cuong-review, index, tier2 design |
| Cuộc hội thoại hiện tại (02/11–11/11) | Có, phần đầu qua bản tóm tắt | Chi tiết trước khi tóm tắt nằm trong transcript, không đọc lại toàn bộ |
| File Bin đính kèm trong chat này | Có | Kết quả v1/v2, benchmark, drawio, docx, ảnh |
| Artifact "Bản đồ Tầng 2" | Có | Đã lưu thành `docs/TIER2_MAP.html` |
| Các chat Claude khác của Bin | **Không** | Chỉ biết qua KB, memory, tóm tắt |
| VM Azure, SQLite gốc, Prometheus | **Không** (hết hạn) | — |
| Máy Bin (`D:\KLTN\...`) | **Không** | — |
| Email với cô Dung, góp ý hội đồng | **Không** trực tiếp | Chỉ qua tóm tắt trong memory |
| Toàn văn tài liệu tham khảo | **Không đọc lại** | — |

## File bàn giao

Tất cả trên nhánh `tier2-run-tooling`. Commit bàn giao: commit chứa thư mục `docs/handover/` (sau `c62e37a`).

### Bộ bàn giao
| File | Vai trò |
|---|---|
| `docs/handover/01_PROJECT_OVERVIEW.md` … `10_HANDOVER_MANIFEST.md` | Bộ này |
| `docs/BAN_GIAO.md` | Bản tóm tắt 1 trang (viết trước bộ này, cùng nội dung rút gọn) |
| `docs/README.md` | Mục lục `docs/` |

### Tài liệu thiết kế / phân tích (bản sao KB)
| File | Vai trò | Ngày cập nhật cuối trong file |
|---|---|---|
| `docs/TRANG_THAI_CHOT.md` | Nguồn chuẩn trạng thái | 30/10 |
| `docs/design/PROPOSAL.md` | Tổng quan (đã cũ nhiều mục) | 29/09 |
| `docs/design/TIER1_DESIGN.md` | Tầng 1 | 27/10 |
| `docs/design/TRAFFIC_COLLECTION_PIPELINE.md` | Thu traffic | 27/09 |
| `docs/design/CONFIG_EVIDENCE_COLLECTOR.md` | Collector v2.1 | 12/10 |
| `docs/design/TIER2_DESIGN.md` | Tầng 2 gốc (trước v3) | 29/09 |
| `docs/design/ORCHESTRATION_DESIGN.md` | Điều phối v2 | 08/10 |
| `docs/design/ORCHESTRATION_V2_DRAFT.md` | Biên bản thảo luận v2 | 08/10 |
| `docs/design/DASHBOARD_DESIGN.md` | Dashboard v2.1 | 12/10 |
| `docs/design/MIGRATION_PLANNER_DESIGN.md` | Planner | 24/09 |
| `docs/analysis/TIER1_CHANGE_PLAN.md` | Kế hoạch sửa Tầng 1 | 30/10 |
| `docs/analysis/TIER2_KET_QUA_20261102.md` | Kết quả 02/11 + benchmark 04/11 | 04/11 |
| `docs/analysis/CHUONG4_TIER1_DU_THAO.md` | Dự thảo Chương 4 (chứa số E4 đã loại) | 29/10 |
| `docs/TIER2_MAP.html` | Sơ đồ Tầng 2 theo code `556df77` + changelog | 05/11 |
| `docs/diagrams/*.drawio` (4) | Sơ đồ gốc: config_collector_4_giai_doan, tier1_4_giai_doan, read_traffic, gd1_gd2_thu_thap_gop | — |
| `docs/images/*.png` (2) | Ảnh read_traffic, Module_collector | — |

### Kết quả
| File | Commit sinh | Ghi chú |
|---|---|---|
| `results/tier1/experiment_report_v2_20261102.json` | PR #3 (02/11) | E5 |
| `results/tier1/hysteresis_ablation_v2_20261102.json` | PR #3 | E6 |
| `results/tier1/experiment_report_main_20261030.json` | `50a99bd` | E2 |
| `results/tier2_v1/*` | `e43b72b` | E7 |
| `results/tier2_v2/*` | `3dbd66d` | E8 |
| `results/tier2_benchmark/bench_v2.json`, `.log` | `cd6134f` | E9 |
| `results/tier2_benchmark/bench_v3_trace_partial.txt` | `14fce2b` | E10 (dở) |
| `infra/experiment_report.json` | trước PR #1 | E1 |
| `infra/trial_data/trial-XX.json` | xuất 08/10 | Dữ liệu cũ |
| `infra/scripts/vm_steps_tier1_tier2.sh` | — | Các bước đã chạy trên VM |

### Code
`tier1_inference/`, `tier2_agent/`, `orchestration/`, `infra/` — HEAD `tier2-run-tooling`. Chuẩn: `main` (`50a99bd`).

### Ngoài repo (Bin giữ)
| File | Ở đâu |
|---|---|
| `KLTN_23520149_HaSonBin.docx` (báo cáo) | Máy Bin, KB. **2 bản cùng tên** đã được gửi (4,8 MB và 5,2 MB) — chưa rõ bản nào mới nhất |
| `DECUONG_KLTN_23520149_HASONBIN_v2/v3/v4.docx` | Máy Bin |
| `DeCuongKLTN_23520149_HaSonBin.pdf` (đề cương đã nộp) | Không có trong phiên này |
| `TIER1_DESIGN.docx` | KB (bản 10/09, đã cũ) |
| Kết quả Tầng 2 gốc | `D:\KLTN\Project\infra\tier2_v1\`, `tier2_v2\` |

## Không truy cập được / có thể đã mất

- SQLite `kltn_tier2.sqlite3`, `kltn_tier2_v2.sqlite3` (trên VM — chỉ còn bản export trong `results/`).
- `trial_data_v2/` (trial JSON dữ liệu 20–26/10) — chỉ có trên VM.
- Bản sao Prometheus `/data` (tar 8,2 GB) — trên VM.
- File LOTO 18/10 (E3) — không có file kết quả.
- Smoke test hàng đợi (E11) — không có file.

## Thông tin còn thiếu, cần lấy từ nguồn khác

- Tiêu chí chấm của hội đồng, lịch bảo vệ chính xác.
- Cô Dung đã được báo về: đổi tên đề tài, kiến trúc 2 tầng, LLM trong Collector, mục tiêu mới (giảm FN) chưa.
- Bản docx báo cáo mới nhất là bản nào.
- Có còn giữ baseline B3 trong kế hoạch không.
- Chi tiết các cuộc chat Claude trước 02/11 không có trong KB.
- Kế hoạch hạ tầng thay Azure (cloud khác / máy cá nhân).
