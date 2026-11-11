# 04 — Nhật ký quyết định

Theo thời gian. Quyết định bị thay thế vẫn giữ, ghi rõ "ĐÃ THAY".

## Kiến trúc tổng

| Ngày | Quyết định | Căn cứ | Phương án bị loại / trade-off |
|---|---|---|---|
| 09/2026 | **3 tầng → 2 tầng**: "Tier 2 cũ" (config) thành Config Evidence Collector, AI Agent thành Tầng 2 | Config không có quyền kết luận → không phải tầng | Khung 3 tầng (từng dùng trong đề cương bản đầu và trao đổi email với cô Dung) |
| 09/2026 | `count > 0` là điều kiện cứng cho STRONG, viết trong code | Dataset nhỏ có thể học sai theo hướng nguy hiểm | Để hồi quy tự học |
| 09/2026 | 5 trạng thái thay cho có/không; "chưa thấy" ≠ "không có" | Nguyên tắc bất đối xứng | Kết luận nhị phân |
| 26/09 | Istio sidecar → **ambient** | Tài nguyên VM | Hệ quả: label tùy biến của pod nguồn không dùng được (istio#61590) → tách trial bằng `pair_key` + `owner_map` |
| 29/09 | Config Collector v2: **LLM trích xuất** kẹp giữa thu thập tất định và xác thực tất định; LLM không được biết danh sách Service | Regex không bao giờ phủ đủ dạng config, mỗi rule thiếu = FN im lặng | v1 rule-based (giữ làm baseline regex); fallback tự động sang regex (bị cấm — trộn phương pháp) |
| 02/10 | Model Collector = OpenAI `gpt-4o-mini` | temperature 0 + structured output | gpt-5.x (reasoning, không chỉnh temperature) |
| 08/10 | Orchestration v2: 2 giai đoạn, nguồn live (ghim T)/snapshot, dashboard thay CLI, dừng 2 cấp × 2 phạm vi, `.env` riêng | Yêu cầu dashboard của cô Dung | Chưa code |
| 11/10 | `TRANG_THAI_CHOT.md` là nguồn chuẩn duy nhất | Thông tin lệch giữa nhiều chat/tài liệu | — |
| 11/10 | Đề cương: kế hoạch nén trong 07/09–29/12; baseline B1 + B2 + LLM one-shot (B3); bỏ mô phỏng drift; giám sát sau triển khai chỉ demo 1–2 kịch bản | Phạm vi | 4 baseline A–D của PROPOSAL |
| 12/10 | Config Collector v2.1: nhiều nguồn candidate (company_rules, regex_generic, llm bật/tắt), Parse tất định, index, truy vết | Validation không bắt được bỏ sót → giảm bằng nhiều nguồn | Phần lớn chưa code |

## Tầng 1

| Ngày | Quyết định | Căn cứ |
|---|---|---|
| 15/10 | 24 thay đổi (bảng đầu `docs/design/TIER1_DESIGN.md`): thứ tự luật phân loại mới, MONITORING theo danh tính SPIFFE, HEALTHCHECK 3 tầng, port `None` + `port_source`, hợp ngày, bỏ `protocol_diversity` (bias −1,2 → −1,0), `raw:<host>`, config dư → NOT_OBSERVED riêng, `volume_reference` cố định, không dùng `count_floor`, cạnh toàn nhiễu không gọi Tầng 2 | Đối chiếu code với dữ liệu thật |
| 15/10 | Chỉ số chính khi so B1 = recall Tầng 1 + Tầng 2. **ĐÃ THAY** một phần (10/2026): Chương 4 chỉ báo Tầng 1 | — |
| 18/10 | gRPC reflection **bỏ khỏi** healthcheck mặc định | Trong thí nghiệm harness gọi qua reflection = traffic thật; giữ lại làm recall 0,852 → 0,789 |
| 18/10 | `EXPERIMENT_CONFIG` khai `*/prometheus`, `*/grafana`, `*/blackbox-exporter` là monitoring đã xác minh; `DEFAULT_CONFIG` chỉ tin `istio-system/prometheus` | Nhiễu harness giả lập monitoring; báo cáo phải tách bạch |
| 18/10 | **Giữ trọng số đặt tay**, không dùng trọng số fit | LOTO: 31% cặp trùng nguyên giữa 15 trial; chỉ healthcheck/monitoring_ratio đã AUC 1,0; `config_match_strength` phương sai 0 |
| 18/10 | **Không dùng LLM sinh dữ liệu** để tăng cỡ mẫu | Vi phạm nguyên tắc không bịa số đo |
| 22/10 | Cổng port ở **cửa ra**, không chặn cửa vào; `NOISE_ONLY_REVIEW` (PR #2) | Chặn cửa vào = vứt cạnh = FN |
| 27/10 | `port_source = config` khi config sạch có đúng 1 port (PR #2) | — |
| 30/10 | Revert `cac079f` (normalize.py + `experiment_report_rerun.json` P 0,941/R 0,822) | Không được chạy từ code `main` → không dùng số đó |
| 11/11 | Bin: "Tầng 1 ổn rồi, chỉ còn Tầng 2" | — |

## Tầng 2

| Ngày | Quyết định | Căn cứ / bị loại |
|---|---|---|
| 09/2026 | ReAct thuần, 1 agent, không Plan-Execute, không multi-agent, không fine-tune | Tác vụ hẹp (1 cạnh, ≤4 tool) |
| 09/2026 | Bỏ trường `confidence` tự khai; trạng thái cuối suy ra bằng luật | Con số tự chấm không chứng minh được gì |
| 09/2026 | **Gác lại**: Bayesian log-odds, self-consistency k=5, wave weighting | Chưa xác nhận lại; báo cáo ghi "đã cân nhắc, tạm gác" |
| 09/2026 | Phòng chống injection: bọc `<evidence>`, entropy Shannon, negative prompting. Loại: sandbox, validator shell, LLM thứ 2 phân loại injection | Tầng 2 không thực thi code |
| 09/2026 | Chỉ tính số chính thức trên cạnh tự cấy | LLM có thể "thuộc" Online Boutique |
| 02/11 | Làm trên nhánh mới `tier2-run-tooling` | — |
| 02/11 | Dùng dữ liệu 20–26/10 qua `--end-time` thay vì chờ 7 ngày | Phương án B |
| 02/11 | Thêm client OpenAI (`gpt-4.1-2025-04-14`) | Không có Anthropic key |
| 02/11 | Recall gộp: **chỉ `CONFIDENT` tính dương** (`NEEDS_MONITORING` không tính) | — |
| 02/11 | Sửa sâu Tầng 2 nhưng **giữ khung**; sửa cả kịch bản harness | Kết quả v1/v2 kém |
| 04/11 | Làm thí nghiệm 1 (benchmark code) để tách lỗi dataset/Tầng 2 | — |
| 05/11 | **Mục tiêu mới**: giảm FN, FP chấp nhận được, admin duyệt cuối | — |
| 05/11 | v3 F1–F6 (polarity, corroboration 2 nguồn độc lập thay cho bắt buộc traffic, lọc code sinh, temperature 0, retry schema, window > 7). **F2 thay luật đã viết ở Chương 3** (luật cũ: CONFIDENT cần bằng chứng có cấu trúc từ Tầng 1) | Benchmark 04/11 |
| 05/11 | `no_match` trung tính | 10/10 phiên dương CONFLICTING do "không tìm thấy" bị gán contradict |
| 05/11 | A+B: `REJECTED_WITH_EVIDENCE` + metric hàng đợi; logic Tầng 1 không đổi | Mục tiêu mới |
| 05/11 | **Đang dở**: tái thiết kế Tầng 2 theo từng giai đoạn; đang ở giai đoạn 2 — (A) thu bằng chứng cố định rồi model chấm vs (B) agentic với giả thuyết cố định. Claude nghiêng về B. **Chưa chốt** | — |

## Quy trình

| Ngày | Quyết định |
|---|---|
| 10/2026 | Không bao giờ yêu cầu dán key vào chat |
| 06/10, 11/10 | Không tự build/sửa file khi Bin chưa yêu cầu rõ |
| 10/2026 (ĐÃ THAY một phần) | "Không commit/push lên repo thật, giao bằng zip" → từ 30/10 Bin cho phép push khi đã nói; vẫn cấm push `main`/merge PR #2 khi chưa nói |
| 05/11 | Liệt kê file + thay đổi trước khi code, chờ duyệt; cập nhật sơ đồ Tầng 2 + changelog sau mỗi thay đổi |
| 11/11 | Chuyển dự án sang ChatGPT: chỉ tài liệu lên knowledge base (≤25 file), còn lại trên GitHub |

## Trade-off và hạn chế đã chấp nhận

- 15 trial dùng chung 1 hạ tầng → không độc lập hoàn toàn (31% cặp trùng).
- Ablation hysteresis cộng dồn ngày 1..k → điểm tăng dần, số lần đổi trạng thái thấp.
- Không đọc Secret (connection string trong Secret bị bỏ sót).
- Dashboard: không đăng nhập, polling, chỉ localhost.
- Cách ly secret trên cùng máy là kỷ luật code, không phải cách ly thật.
