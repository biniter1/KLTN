# 06 — Lỗi, giới hạn, nợ kỹ thuật

## 1. Lỗi chưa giải quyết

| # | Vấn đề | Ảnh hưởng | Gợi ý |
|---|---|---|---|
| I1 | Traffic cấy của harness cũ lỗi 100% (`grpcurl list` qua reflection) | Tầng 2 coi cạnh thật là "sự kiện ngắn hạn lỗi" | Đã sửa ở `e35c9d4`, **chưa deploy** |
| I2 | Lịch cạnh thưa tính từ lúc pod start (`ACTIVE_DAYS`) | Cạnh thưa lệch ngày khi pod restart | Đã sửa (`ACTIVE_DATES` theo ngày lịch), chưa deploy |
| I3 | Cạnh SPARSE_HIDDEN không có dấu vết trong code/docs/PR | 3/4 tool Tầng 2 không thể xác nhận → trần recall ~0,859 | Đề xuất "cấy code overlay" — **chờ Bin** |
| I4 | `noise_precision` luôn 0 | Không đo được H1 trên nhóm nhiễu | Đổi thành tỷ lệ cạnh nhiễu bị STRONG |
| I5 | Ground truth của cạnh nhiễu (healthcheck/monitoring) mâu thuẫn với thiết kế hàng đợi (nhiễu vẫn cần được mở) | Định nghĩa dương chưa thống nhất | **Chờ Bin quyết** |
| I6 | TCP `cartservice → redis-cart` count = 0 | FN không bao giờ lên Tầng 2 | Kiểm `reporter` TCP / dùng log ztunnel |
| I7 | Một số cặp chỉ có config không thành cạnh | FN | Cần `trial_data_v2` để kiểm |
| I8 | `08_export_trial_data.py` làm rơi `CONFIG_ONLY_UNRESOLVED` (`trial_scope.py` đúng) | Lệch giữa 2 đường | Kiểm với `pair_key` mới |
| I9 | Scrape gap chỉ xét thời điểm hiện tại, job `istio-proxy` có thể không tồn tại | Thiếu dữ liệu không bị phát hiện | — |
| I10 | `workload_revision` luôn `"latest"` | Temporal drift không dùng được | — |
| I11 | Model gán stance cho bằng chứng traffic không nhất quán (E8 trial-05 vs 03/07/08) | Kết quả không ổn định | Giai đoạn 2 thiết kế lại |
| I12 | 22/30 case benchmark khác kết quả giữa 3 lần chạy (trước temperature 0) | — | v3 đặt temperature 0, chưa đo lại |
| I13 | Danh sách reason_code PR #2 và PR #3 khác nhau | Xung đột khi merge | Hợp khi merge |
| I14 | Bìa docx: tên đề tài cũ + lỗi chính tả ("KUBERNTES", "ÁP ỤNG", "forKubernetes") | Báo cáo | Sửa khi Bin cho phép |
| I15 | Chương 3 trong docx mô tả luật corroboration cũ (CONFIDENT cần bằng chứng có cấu trúc Tầng 1) | Lệch với code v3 | Viết lại Chương 3 phần Tầng 2 |
| I16 | `CHUONG4_TIER1_DU_THAO.md` chứa số E4 đã bị loại | Dễ dùng nhầm | Chỉ dùng số E5 (hoặc E2 có ghi chú) |
| I17 | Kế hoạch 22 tuần trong đề cương cũ vượt 29/12 | Đã nén ở đề cương v2 (11/10) | Kiểm lại với hạn mới cuối 01/2027 |

## 2. Giới hạn đã biết (chấp nhận, ghi vào báo cáo)

- Ambient không truyền label tùy biến của pod nguồn → tách trial ở tầng ứng dụng.
- Không có DNS trong telemetry (UDP); DNS luôn được Planner tự mở.
- Không đọc Secret → connection string trong Secret bị bỏ sót.
- Redact chưa bắt `token=`, `apikey=`, `Authorization: Bearer`; giá trị trong `<config_item>` chưa escape.
- 2 chỗ lệch K8s ở bước merge config (`$(VAR)` với envFrom / image ENV) — chờ Bin quyết.
- 15 trial không độc lập (31% cặp trùng). Online Boutique nhỏ.
- Rule-based injection detection có recall thấp.
- LLM có thể "thuộc" Online Boutique → số chính thức chỉ trên cạnh cấy.
- Config đọc ngày 02/11, traffic của 20–26/10 (lệch thời gian ở E5/E7/E8).

## 3. Nợ kỹ thuật

- Config Collector v2.1 phần lớn chưa code, nhưng Chương 3 docx đã mô tả như đã có (ba nguồn candidate, Parse, …) → phải code hoặc sửa lời văn.
- Orchestration v2 + dashboard chưa code; Chương 4 docx đã nhắc bảng `traffic_series`, `config_rule_sets`.
- Không lưu series ở bảng nào (đường đánh giá ghi JSON).
- `infra/README.md` còn mô tả WSL2; `PROPOSAL.md` mục 5, 7, 8, 9 đã cũ (xem `docs/TRANG_THAI_CHOT.md` mục 8).
- `docs/design/TIER2_DESIGN.md` mô tả trước v3; v3 chỉ có trong `TIER2_MAP.html` và tài liệu này.
- Thư mục `infra/KLTN_backup/`, `infra/kq.json`, `infra/scripts/log.txt` trên repo chưa rõ vai trò — chưa kiểm.

## 4. Lỗi từng xảy ra và cách sửa

| Lỗi | Sửa |
|---|---|
| L1: validator đọc nhãn sai `security.istio.io/canonical-name` → mọi STRONG thành NEEDS_MANUAL_SELECTOR trên cụm thật | Suy canonical theo thứ tự Istio (PR #1) |
| L2/L3: tra `previous_status` thiếu SA; đọc nhầm chính snapshot | Đủ 7 phần khóa, chỉ snapshot trước (PR #1) |
| L4: UNRESOLVED dồn 1 ô | `raw:<host>` |
| L5: config ở port khác bị mất | NOT_OBSERVED riêng |
| L6: cạnh toàn nhiễu gọi Tầng 2 | Nhánh riêng |
| L7: MONITORING không bao giờ được gắn | Đổi thứ tự luật |
| L8: `distinct_days` lấy max | Hợp ngày |
| L9: port luôn 0 | `port=None` + suy từ Service |
| L10: `protocol_diversity` hằng 0,5 | Bỏ, bù bias |
| L11: TCP không có query theo ngày | Thêm query |
| Commit `cac079f` từ code ngoài `main` | Revert `50a99bd` |
| Khóa SSH `infra/KLTN_key.pem` nằm trong repo public | Gỡ file (`7a6dfcb`), **vẫn còn trong lịch sử git** → phải thay khóa |
| Tầng 2 v1: tool traffic bị từ chối 15/15 | `3dbd66d` |
| Thiếu `openai` trong requirements | `4104c9b` |
| Benchmark: IntegrityError trùng session, 429 hết credit | `40eda52` |
| `no_match` bị gán contradict | `33861dd` |
| JSONDecodeError khi Prometheus lưu trữ đang replay WAL | Chờ `/-/ready` |
| Lỗi thao tác VM: `export OPENAI_API_KEY="..."` chạy nguyên văn → 401; dán nhiều dòng làm `read -rs` nuốt dòng sau; SSH rớt giết tiến trình; Ctrl-b bị VS Code chiếm | Dùng `read -rs`, gõ từng dòng, tmux, mở cửa sổ SSH riêng |
| Test (Claude sửa nhầm): trùng tên helper `_shared_docs`, assert đặt sai chỗ, case keyword | Đã sửa trước khi commit |

## 5. Rủi ro tái diễn

- AI làm thay đổi mà Bin không theo dõi được → luôn liệt kê trước, cập nhật changelog.
- Số liệu từ nhiều bản mã/dữ liệu khác nhau lẫn vào báo cáo → luôn ghi commit + file + ngày.
- Credit LLM hết giữa chừng → script đã dừng an toàn; ước lượng chi phí trước khi chạy.
- Mất dữ liệu khi VM/cloud hết hạn → sao lưu SQLite, `trial_data_v2`, bản sao Prometheus về máy Bin.
- Key/secret lộ trong chat hoặc repo.
