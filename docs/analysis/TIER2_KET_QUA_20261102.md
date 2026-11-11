# Kết quả chạy thật Tầng 1 + Tầng 2 — 02/11/2026

Toàn bộ số liệu dưới đây lấy từ các lần chạy thật trên VM Azure ngày 02/11/2026: Batch Orchestrator chạy trên 15 trial, rồi script `11_export_tier2_results.py` đọc kết quả từ SQLite ở chế độ chỉ đọc. Không có số nào được điền tay hay giả lập.

## 1. Điều kiện chạy

| Mục | Giá trị |
|---|---|
| Nhánh / PR | `tier2-run-tooling` (PR #3), chưa merge vào `main` |
| Commit v1 | `e43b72b` (export lúc 2026-11-02T08:27:06Z) |
| Commit v2 | `3dbd66d` (export lúc 2026-11-02T09:01:00Z): chỉ thêm `goal["identity_key"]` so với v1 |
| Cửa sổ traffic | 7 ngày, 20/10–26/10/2026, `--end-time 2026-10-26T23:59:59Z` |
| Nguồn metrics | Prometheus lưu trữ (`localhost:9091`), dựng từ bản sao `/data` của Prometheus trong cụm chụp lúc 2026-11-02 04:23 UTC; retention 3650d. Mỗi ngày 20–26/10 có khoảng 7,39–7,41 triệu request |
| Config Collector | OpenAI `gpt-4o-mini`; 82 config record cho cả namespace `shared-eval` |
| Model Tầng 2 | OpenAI `gpt-4.1-2025-04-14` (`TIER2_PROVIDER=openai`), vì không có Anthropic API key |
| Validator selector | `KubernetesSelectorValidator` (cụm thật, chỉ đọc) |
| Quy ước tính dương | Tầng 1: `OBSERVED_STRONG`. Tầng 1 + 2: thêm các cạnh Tầng 2 kết luận `CONFIDENT` (`NEEDS_MONITORING` không tính) |
| Test trước khi chạy | tier1 416, tier2 187, infra 49, orchestration: tất cả pass |

Lưu ý về nguồn dữ liệu:
- Config, selector và source code được đọc từ trạng thái cụm ngày 02/11, còn traffic là của 20–26/10.
- 107/107 traffic series có `port_source = inferred_single_port`. Không series nào lấy port từ log, vì không chạy VictoriaLogs.

## 2. Tầng 1 (09_run_experiment, `experiment_report_v2.json`)

| | Precision | Recall | F1 | Recall thưa (≤2 ngày) |
|---|---|---|---|---|
| Tầng 1 | 1,000 ± 0,000 | 0,833 ± 0,000 | 0,909 ± 0,000 | 0,000 |
| B1 (count>0) | 0,886 ± 0,002 | 0,863 ± 0,016 | 0,874 ± 0,009 | 0,267 ± 0,143 |
| B2 (count≥20 và ≥3 ngày) | 0,882 ± 0,000 | 0,833 ± 0,000 | 0,857 ± 0,000 | 0,000 |

Wilcoxon (ghép cặp theo trial, n = 15):

| So sánh | p |
|---|---|
| Tầng 1 vs B1, recall | 0,0047 (B1 cao hơn) |
| Tầng 1 vs B1, precision | 0,00045 (Tầng 1 cao hơn) |
| Tầng 1 vs B2, recall | 1,0 (mọi cặp bằng nhau) |
| Tầng 1 vs B2, precision | 0,00011 (Tầng 1 cao hơn) |

**Ablation hysteresis (trial-01, trial-02; 7 snapshot):** cả hysteresis lẫn ngưỡng đơn đều có 0 lần đổi trạng thái trên 18 cạnh. Dữ liệu này không phân biệt được hai cơ chế.

## 3. Tầng 2: v1 rồi v2

### 3.1 Lỗi tích hợp phát hiện sau v1

- `read_extended_traffic_window` bị `tool_registry.validate_input` từ chối **15/15 lần**, với thông báo "edge không khớp identity_key".
- Nguyên nhân: tool đòi `edge` khớp đúng từng ký tự với chuỗi `ns|svc|sa|dns|dsvc|protocol|port`. Nhưng `goal` gửi cho model (`escalation.raw`) chỉ chứa các trường rời, không có chuỗi ghép.
- Cách sửa (commit `3dbd66d`):
  - thêm `goal["identity_key"] = escalation.identity_key` và mô tả tham số `edge`;
  - thêm test hồi quy;
  - không đổi prompt, ngưỡng hay luật dừng;
  - cách sửa không dựa vào ground truth.

Những lỗi tool sau là **đúng thiết kế**, không phải bug:
- redis-cart không có `src/` trong Online Boutique, nên `read_source_code`/`read_internal_docs` báo lỗi;
- gatekeeper trả 403 cho `read_pr_descriptions` với service redis-cart, vì redis-cart không nằm trong whitelist.

### 3.2 Kết quả tổng hợp

| | v1 | v2 |
|---|---|---|
| Cạnh escalate | 19 (14 trial; trial-14 không có cạnh nào) | 19 |
| Kết quả Tầng 2 | 19 INSUFFICIENT_BASIS | 1 CONFIDENT · 2 NEEDS_MONITORING (gốc CONFIDENT, bị luật corroboration hạ) · 16 INSUFFICIENT_BASIS |
| TP / FP / FN / TN (trên cạnh escalate) | 0 / 0 / 8 / 11 | 1 / 0 / 7 / 11 |
| Lời gọi tool `read_extended_traffic_window` thành công | 0 | 10 |
| Lời gọi LLM · token vào (không cache) · token ra | 53 · 72.067 · 14.900 | 63 · 106.257 · 17.579 |
| Token vào đọc từ cache | 12.032 | 29.184 |

Recall gộp (n = 15):

| | Precision | Recall | F1 |
|---|---|---|---|
| Tầng 1 | 1,000 | 0,833 ± 0,000 | 0,909 |
| Tầng 1 + 2 (v1) | 1,000 | 0,833 ± 0,000 | 0,909 |
| Tầng 1 + 2 (v2) | 1,000 | 0,837 ± 0,008 | 0,911 ± 0,005 |

Wilcoxon cho v2:

| So sánh | p |
|---|---|
| Tầng 1 + 2 vs Tầng 1, recall | 0,317 (chỉ 1/15 trial khác: trial-05 tăng từ 15/18 lên 16/18). Không có ý nghĩa thống kê |
| Tầng 1 + 2 vs B1, recall | 0,0082 (B1 cao hơn) |
| Tầng 1 + 2 vs B1, precision | 0,00045 (Tầng 1 + 2 cao hơn) |
| Tầng 1 + 2 vs B2, recall | 0,317 |
| Tầng 1 + 2 vs B2, precision | 0,00011 (Tầng 1 + 2 cao hơn) |

### 3.3 Trần lý thuyết của Tầng 2 trong kịch bản này

- Mỗi trial có 3 FN ở Tầng 1, nhưng chỉ khoảng 1 FN được escalate. Các FN còn lại không bao giờ lên Tầng 2: không có cả traffic lẫn config (ví dụ cartservice → redis-cart, TCP count = 0), hoặc cạnh chỉ toàn traffic nhiễu.
- Nếu Tầng 2 xác nhận đúng cả 8 cạnh GT dương được escalate, recall gộp cũng chỉ đạt khoảng **0,859**.
- Vậy nút thắt chính là **độ phủ khi escalate**, không phải bản thân Tầng 2.

## 4. Các cạnh Tầng 2 kết luận sai hoặc cần lưu ý (v2)

Tất cả là cạnh do harness cấy vào. Đặc điểm chung:
- error_ratio 1,0;
- traffic chỉ trong 1 ngày;
- khoảng 177–1068 request;
- không có dấu vết trong code, tài liệu hay PR của Online Boutique.

| Trial | Cạnh | GT | Tầng 2 (v2) | Lý do (một dòng) |
|---|---|---|---|---|
| 01 | emailservice → productcatalogservice | dương | INSUFFICIENT (FN) | Chỉ thấy stub trong `demo_pb2_grpc.py` (sinh từ proto); traffic mở rộng vẫn 1 ngày, lỗi 100% |
| 02 | productcatalogservice → shippingservice | dương | NEEDS_MONITORING, gốc CONFIDENT (FN) | Model coi `shippingServiceClient` trong `genproto/` là bằng chứng gọi thật (sai căn cứ); luật corroboration hạ vì thiếu bằng chứng traffic |
| 03 | redis-cart → checkoutservice | dương | INSUFFICIENT (FN) | redis-cart không có source/docs/PR; traffic mở rộng 1 ngày, lỗi 100% nên bị đánh giá "contradict" |
| 06 | recommendationservice → paymentservice | dương | INSUFFICIENT (FN) | Chỉ có `PaymentServiceStub` sinh từ proto; dừng do hết số vòng tối đa (8 vòng) |
| 07 | checkoutservice → recommendationservice | dương | INSUFFICIENT (FN) | Traffic mở rộng 1 ngày, lỗi 100% nên bị coi là sự kiện ngắn hạn |
| 08 | currencyservice → checkoutservice | dương | INSUFFICIENT (FN) | Không có dấu vết trong code; traffic 1 ngày, lỗi 100% |
| 09 | currencyservice → adservice | dương | INSUFFICIENT (FN) | Chỉ có định nghĩa `AdService` trong proto, không có lời gọi |
| 05 | redis-cart → paymentservice | dương | **CONFIDENT (TP)** | **Đúng nhãn nhưng không đáng tin**: phần lập luận của model kết luận cạnh là "hiệu ứng ngắn hạn", mà vẫn gán stance "support" cho cùng loại bằng chứng (534 req, 1 ngày, lỗi 100%) đã bị gán "contradict" ở trial-03/07/08 |
| 06 | shippingservice → currencyservice-nonexistent | âm | NEEDS_MONITORING, gốc CONFIDENT (TN) | Model coi `currencyServiceClient` trong `genproto/` là bằng chứng (sai căn cứ); **luật corroboration đã chặn được 1 FP** |

10 cạnh TN còn lại đều là cạnh `-nonexistent`. Với các cạnh này, tuỳ phiên model đọc source, docs, PR, và ở 3 phiên đọc thêm traffic mở rộng 30 ngày (count = 0). Tất cả đều kết luận không đủ căn cứ. Kết luận đó đúng.

## 5. Kết luận được phép viết vào báo cáo

1. Trên bộ dữ liệu này, Tầng 2 **không cải thiện recall có ý nghĩa thống kê**: 0,833 lên 0,837, p = 0,317.
2. Tầng 2 **giữ precision 1,0**: không tạo FP. Luật corroboration (CONFIDENT phải có bằng chứng traffic) chặn được 1 kết luận sai do model hiểu nhầm code sinh từ proto là lời gọi thật.
3. TP duy nhất (trial-05) có stance mâu thuẫn với chính lập luận của model. Không được trình bày nó như bằng chứng rằng Tầng 2 lập luận tốt.
4. Hai điểm yếu cụ thể:
   - model nhầm code sinh từ proto (`genproto/`, `*_pb2_grpc.py`) với lời gọi thật;
   - gán stance cho bằng chứng traffic không nhất quán.
5. Giới hạn của kịch bản đánh giá: cạnh cấy bởi harness không có dấu vết trong code, docs hay PR. Vì vậy 3/4 tool của Tầng 2 về nguyên tắc không thể xác nhận các cạnh này. Kịch bản đang đo Tầng 2 ở đúng loại cạnh mà nó khó xác nhận nhất.
6. Lỗi tích hợp ở v1 phải được ghi rõ là đã phát hiện và sửa. Kết quả v1 vẫn được giữ.

## 6. File gốc

Trên máy Bin: `D:\KLTN\Project\infra\tier2_v1\` và `D:\KLTN\Project\infra\tier2_v2\`. Trên VM: `~/kltn-repo/infra/tier2_results/`, `~/kltn-repo/infra/tier2_results_v2/`, `kltn_tier2.sqlite3` (v1) và `kltn_tier2_v2.sqlite3` (v2).

---

# Thí nghiệm 1 — benchmark bằng chứng code (chạy 04/11/2026)

**Thiết kế**
- Script `infra/scripts/12_tier2_code_benchmark.py`, commit `cd6134f`, model `gpt-4.1-2025-04-14`, mỗi case chạy 3 lần (90 phiên).
- **Dương:** 15 cạnh thật của Online Boutique, có code gọi thật.
- **Âm:** 15 cặp không gọi nhau (seed 20261104), source thường có stub sinh từ proto.
- Mọi case nhận **cùng** hồ sơ escalate (OBSERVED_WEAK, 534 req, 1 ngày, lỗi 0%), cùng kết quả tool traffic, và không có `config_finding`. Như vậy chỉ code, docs và PR phân biệt được nhãn.
- Đây **không phải** số liệu 15 trial.

**Kết quả (89 phiên hợp lệ, 1 phiên lỗi schema)**

| Cách tính dương | TP | FP | FN | TN | Precision | Recall |
|---|---|---|---|---|---|---|
| Chỉ CONFIDENT (quy ước chính) | 5 | 4 | 40 | 40 | 0,556 | 0,111 |
| Trạng thái gốc CONFIDENT (trước luật corroboration) | 31 | 11 | 14 | 33 | 0,738 | 0,689 |

- Chi phí: 317 lời gọi LLM, khoảng 602 nghìn token vào và 94 nghìn token ra.
- **Độ ổn định:** 22/30 case cho kết quả khác nhau giữa 3 lần chạy.

**Lỗi của Tầng 2 đo được (độc lập với dataset)**

1. **Giả thuyết không có chiều.**
   - Model tự đặt giả thuyết phản bác, ví dụ "traffic chỉ là sự kiện ngắn hạn".
   - Bằng chứng traffic ủng hộ giả thuyết đó nên nó được SUPPORTED. `derive_final_statuses` coi *mọi* giả thuyết SUPPORTED là xác nhận cạnh, nên ra CONFIDENT.
   - Đây là nguồn gốc của **cả 4 FP** CONFIDENT trong benchmark: lập luận của model đều nói "không phải dependency".
   - Cũng là nguồn gốc của TP trial-05 trong lần chạy 15 trial.
2. **Luật corroboration mâu thuẫn với mục đích của Tầng 2.**
   - CONFIDENT bắt buộc phải có bằng chứng traffic "support". Nhưng Tầng 2 được thiết kế chính cho cạnh traffic thưa.
   - Kết quả: 26/45 phiên dương đã tìm đúng code thật nhưng vẫn bị hạ xuống NEEDS_MONITORING.
3. **Nhầm code sinh từ proto:** 8 phiên có "support" chỉ dựa trên file sinh tự động. Thêm vào đó, output grep bị cắt ở 4000 ký tự, nên có lúc các dòng của file sinh lấn hết phần code thật.
4. **Không cố định temperature** (client OpenAI và Anthropic đều dùng mặc định), nên kết quả dao động mạnh giữa các lần chạy.
5. **Model trả stance ngoài schema** (`'neutral'`) làm cả phiên dừng với `ModelOutputSchemaError` (1/90 phiên).

**Kết luận thí nghiệm 1**
- Khi bằng chứng có thật, model tìm được nó trong khoảng 69% phiên dương.
- Nhưng **cơ chế suy ra trạng thái cuối và độ ổn định** của Tầng 2 có lỗi thiết kế thật (lỗi 1, 2, 4), độc lập với dataset.
- Câu trả lời cho câu hỏi "lỗi do dataset hay do Tầng 2": **cả hai**.
  - Dataset 15 trial không cho bằng chứng nào để xác nhận (trần recall khoảng 0,859).
  - Tầng 2 có lỗi logic trạng thái, làm sai ngay cả khi bằng chứng đầy đủ.
