# Chương 4. TRÌNH BÀY, ĐÁNH GIÁ, BÀN LUẬN VỀ KẾT QUẢ

*(Dự thảo mục 4.1–4.4.3 và phần Tầng 1 của mục 4.5. Mục 4.4.1 (Config Evidence Collector), 4.4.4 (Tầng 2) và 4.4.5 (Migration Planner) chưa viết vì chưa chạy thực nghiệm trên 15 trial; bổ sung khi có số liệu.)*

*(Số liệu Tầng 1 ở mục 4.4.2/4.4.3 được cập nhật lần gần nhất ngày 29/10/2026, sau khi mã Tầng 1 được code lại lần thứ hai — xem mục 4.2.1. Số liệu này chạy được bằng cách nạp lại 15 file `trial_data/trial-XX.json` thật đã có sẵn và chạy qua mã mới trong môi trường cục bộ, KHÔNG cần dựng lại cụm; bộ kiểm selector vẫn là bản giả lập — xem giới hạn ở mục 4.5.2.)*

## 4.1. Hồ sơ thiết kế phần mềm

Bộ khung được thiết kế theo bốn sơ đồ UML và một lược đồ cơ sở dữ liệu, vẽ bằng công cụ UML tương ứng (cùng cách trình bày Hình 3.1–3.19) và chèn vào các vị trí dưới đây.

**Hình 4.1 — Sơ đồ use case.** Ba tác nhân: Quản trị viên hệ thống (xem dashboard, duyệt hàng đợi review, cấu hình các module thu thập), Tác nhân AI — Tầng 2 (đọc bằng chứng, gọi công cụ, kết luận), Batch Orchestrator (chạy 7 bước của một lần thực nghiệm). Use case chính: Chạy một lần thực nghiệm, Xem trạng thái một quan hệ phụ thuộc, Duyệt quan hệ cần con người xem xét, Cấu hình nguồn bằng chứng.

**Hình 4.2 — Sơ đồ thành phần.** Bốn package theo đúng cấu trúc mã nguồn (mục 4.2.1): `tier1_inference`, `tier2_agent`, `orchestration`, `infra`; `orchestration` phụ thuộc vào hai package suy luận, không có chiều ngược lại.

**Hình 4.3 — Sơ đồ triển khai.** Một cụm `kind` trên máy ảo Azure, gồm namespace `istio-system` (ztunnel, istiod), namespace `shared-eval` (Online Boutique, waypoint, các workload cấy) và Prometheus; Batch Orchestrator và API Server gọi vào cụm qua Kubernetes API và Prometheus HTTP API.

**Hình 4.4 — Sơ đồ tuần tự cho một lần chạy thực nghiệm.** Bảy bước tuần tự của Batch Orchestrator (cấy kịch bản → thu thập lưu lượng → thu thập cấu hình → gộp bằng chứng → chạy Tầng 1 → gọi Tầng 2 nếu cần → ghi kết quả), mỗi bước lỗi dẫn thẳng tới trạng thái `FAILED`.

**Lược đồ cơ sở dữ liệu.** Một file SQLite, tách bảng theo quyền ghi: `dependency_evidence` (Orchestrator ghi), `human_decisions` (API Server ghi, chỉ thêm), cùng các bảng `traffic_series`, `traffic_daily_counts`, `collection_quality_log`, `config_rule_sets` phục vụ dashboard.

## 4.2. Hiện thực hệ thống

### 4.2.1. Cấu trúc mã nguồn

Hệ thống gồm 4 package Python độc lập:

- **`tier1_inference`** — Tầng 1 (tập ứng viên, phân loại lưu lượng, gộp cạnh, định danh, tính điểm, trạng thái/hysteresis), module thu thập lưu lượng, module thu thập cấu hình, hai phương pháp đối chứng B1/B2, và bộ máy thực nghiệm dùng chung (`experiment.py`, `evaluation.py`, `trial_io.py`, `ablation.py`).
- **`tier2_agent`** — Brain (vòng ReAct), 4 công cụ chỉ đọc, Context Manager, Observer, schema đầu ra có cấu trúc.
- **`orchestration`** — Batch Orchestrator, API Server (dashboard, gatekeeper, review), lớp truy cập dữ liệu.
- **`infra`** — script dựng cụm, sinh/cấy kịch bản, xuất dữ liệu trial, chạy thực nghiệm chính thức, ablation hysteresis.

Tầng 1 được recode toàn bộ ngày 18/10/2026 theo các quyết định thiết kế chốt ngày 15/10, sửa 11 lỗi phát hiện khi chạy thật trên cụm — ví dụ thứ tự luật phân loại khiến traffic Prometheus thật không bao giờ được gắn MONITORING, hay lưu lượng health check qua gRPC reflection bị tính nhầm là nhiễu trong phạm vi thí nghiệm.

Tầng 1 được code lại lần thứ hai, hoàn tất ngày 29/10/2026, theo `TIER1_CHANGE_PLAN.md`: sửa lại thứ tự và cách xác định traffic health check (dựa trên probe thật của K8s thay vì suy đoán theo path), gộp cạnh theo nguyên tắc hợp nhất (union) giữa các nguồn bằng chứng, khớp cổng chặt hơn khi chọn ứng viên, và thiết kế lại công thức tính điểm (dùng một `volume_reference` cố định thay cho percentile tính riêng theo từng snapshot). Số liệu ở mục 4.4.2/4.4.3 phản ánh bản mã sau lần code lại thứ hai này, không phải bản 18/10.

### 4.2.2. Kiểm thử

**Bảng 4.1. Số lượng kiểm thử đơn vị theo package**

| Package | Số kiểm thử | Pass |
| --- | --- | --- |
| `tier1_inference` | 412 | 412 |
| `orchestration` | 120 | 120 |
| `tier2_agent` | 180 | 180 |
| `infra` | 46 | 46 |
| **Tổng** | **758** | 758 |

*Nguồn: hàng `tier1_inference` chạy lại và xác nhận trực tiếp ngày 29/10/2026 trên bản mã sau lần code lại thứ hai (không còn ca bị skip như bản 18/10). Ba hàng còn lại giữ nguyên số liệu từ lần chạy pytest trên `git archive origin/main`, PR #1, 18/10/2026 — ba package này không thuộc phạm vi lần code lại thứ hai nên chưa được xác nhận lại trong lần cập nhật này.*

## 4.3. Triển khai thực nghiệm

Thực nghiệm chạy trên một cụm `kind` dựng trên máy ảo Azure, Istio cài ở chế độ ambient (ztunnel + waypoint theo namespace), Prometheus dùng addon mẫu đi kèm Istio để thu số liệu.

Vì ứng dụng nền (Online Boutique, khoảng 11 service) quá nhỏ để dựng 15 cụm riêng trong phạm vi PoC, đề tài dùng một namespace `shared-eval` duy nhất chứa Online Boutique cộng khoảng 83 workload do harness tự sinh, đóng vai 15 "trial ảo" — mỗi trial một kịch bản cấy khác nhau (quan hệ thưa, quan hệ chỉ có trong cấu hình, lưu lượng health check gây nhiễu). Việc tách dữ liệu theo trial dựa vào `pair_key` và `owner_map` ở tầng ứng dụng, không dựa vào nhãn Prometheus, vì chế độ ambient của Istio không truyền được nhãn tùy biến của pod nguồn vào số liệu. Lưu lượng được thu đủ 7 ngày, dữ liệu xuất ngày 08/10/2026 thành 15 file `trial_data/trial-XX.json`, mỗi file gồm chuỗi lưu lượng, bằng chứng cấu hình và đáp án đúng đã biết trước vì đề tài tự cấy.

Một số lỗi phát hiện khi thu thập trên cụm thật, đã sửa trong lần recode 18/10: nhãn `destination_port` không tồn tại trong `istio_requests_total` nên cổng luôn đọc ra 0 — sửa bằng cách suy cổng từ `Service` spec; không thu được số liệu TCP do giá trị `reporter` của metric TCP khác giả định ban đầu; tập ngày có traffic bị rút gọn thành số đếm ở bản thu thập đầu, làm sai lệch tỷ lệ ngày đều đặn.

Hysteresis (hai ngưỡng promote/demote 0,68/0,58) chưa được thực nghiệm kiểm chứng trong đường đánh giá này vì mỗi trial chỉ có một snapshot, không phải một chuỗi thời gian nhiều lần đo; số liệu ở mục 4.4 dùng ngưỡng đơn 0,68. Ablation riêng cho hysteresis, cắt từ 7 ngày đã thu thành nhiều snapshot, là việc còn lại.

15 file `trial_data/trial-XX.json` xuất ngày 08/10/2026 không cần thu thập lại để lấy số liệu ở mục 4.4.2: mỗi file đã đóng băng đủ chuỗi lưu lượng, bằng chứng cấu hình và đáp án đúng của trial đó, nên việc chạy lại Tầng 1 trên bản mã mới chỉ là nạp lại các file này và chạy qua `tier1.experiment.run_experiment()`, không cần truy cập cụm.

## 4.4. Kết quả thực nghiệm

### 4.4.1. Module thu thập bằng chứng cấu hình

Module đã hiện thực và có kiểm thử đơn vị (mục 4.2.2), nhưng chưa được đánh giá định lượng (recall, precision, tỷ lệ bịa bị chặn) trên 15 trial thật so với bộ phân tích theo luật. Bổ sung khi có số liệu.

### 4.4.2. Tầng 1 so với các phương pháp đối chứng

Bảng 4.2 so sánh Tầng 1 (trọng số đặt tay) với B1 (có traffic là tin) và B2 (ngưỡng cứng), chạy trên 15 file `trial_data/trial-XX.json` thật, qua bản mã Tầng 1 sau lần code lại thứ hai (29/10/2026 — mục 4.2.1). Mỗi chỉ số là trung bình ± khoảng tin cậy 95% trên 15 trial; khác biệt kiểm định bằng Wilcoxon bắt cặp. B1 và B2 không đổi logic giữa hai lần code lại, nên số liệu hai phương pháp này trùng khớp với lần chạy 18/10 — dùng để đối chiếu rằng dữ liệu nạp đúng, chỉ Tầng 1 thay đổi.

**Bảng 4.2. Tầng 1 so với B1, B2 — trọng số đặt tay, 15 trial thật (bản mã 29/10/2026)**

| Phương pháp | Precision | Recall | F1 | Recall nhóm thưa |
| --- | --- | --- | --- | --- |
| Tầng 1 | 0,941 ± 0,009 | 0,822 ± 0,013 | 0,877 ± 0,009 | 0,871 ± 0,013 |
| B1 | 0,889 ± 0,000 | 0,889 ± 0,000 | 0,889 ± 0,000 | 0,533 ± 0,038 |
| B2 | 0,885 ± 0,008 | 0,822 ± 0,013 | 0,852 ± 0,009 | 0,000 ± 0,000 |

**Bảng 4.3. Kiểm định Wilcoxon bắt cặp, Tầng 1 so với từng đối chứng (n = 15)**

| So sánh | p-value |
| --- | --- |
| Precision, Tầng 1 vs B1 | ≈ 0,00035 |
| Recall, Tầng 1 vs B1 | ≈ 0,00029 |
| Precision, Tầng 1 vs B2 | ≈ 0,00035 |
| Recall, Tầng 1 vs B2 | 1,000 (giống tuyệt đối cả 15/15 trial) |

Tầng 1 đạt precision cao hơn cả B1 và B2 một cách có ý nghĩa thống kê (p ≈ 0,0004 với cả hai), nhờ phân loại lưu lượng loại bỏ nhiễu health check/monitoring khỏi bằng chứng — đúng hướng giả thiết 1. Recall tổng thể của Tầng 1 thấp hơn B1 một cách có ý nghĩa thống kê (B1 coi mọi traffic là bằng chứng nên recall cao tuyệt đối nhưng không lọc nhiễu), nhưng **trùng tuyệt đối với B2 trên cả 15/15 trial (p = 1,000)** — khác với lần chạy 18/10, khi recall Tầng 1 còn cao hơn B2 có ý nghĩa thống kê. Đây là thay đổi do lần code lại thứ hai, cần xem lại bước gộp cạnh/phân loại để hiểu vì sao tập cạnh được Tầng 1 "nhớ được" lại khớp đúng tập của B2 ở mọi trial, trước khi dùng con số này để khẳng định bất kỳ điều gì — xem thêm mục 4.5.2.

Ở nhóm quan hệ thưa, Tầng 1 đạt recall 0,871 — cao hơn cả B1 (0,533) và B2 (0,000, bỏ sót toàn bộ nhóm này) — đúng hướng giả thiết 2, và là một đảo chiều so với lần chạy 18/10 (khi Tầng 1 chỉ đạt 0,378 ở nhóm này, thấp hơn B1). Vì đảo chiều này khá lớn, nên đọc cùng với điểm trên: nhiều khả năng cùng một thay đổi ở bước gộp cạnh/tính điểm vừa làm tăng recall nhóm thưa vừa làm recall tổng thể trùng với B2 — cần xem lại code để xác nhận trước khi đưa vào luận văn như một kết quả chắc chắn.

Theo thiết kế, chỉ số so sánh chính thức với B1 phải là recall Tầng 1 + Tầng 2 gộp lại, vì các quan hệ Tầng 1 không tự kết luận được chuyển tiếp lên Tầng 2 xử lý, không bị bỏ. Số liệu Tầng 2 trên 15 trial chưa chạy (mục 4.4.4), nên Bảng 4.2 hiện chỉ phản ánh riêng Tầng 1, chưa phải số liệu so sánh đầy đủ.

### 4.4.3. Trọng số học được và độ ổn định

Bên cạnh trọng số đặt tay, đề tài thử suy luận trọng số bằng hồi quy logistic trên dữ liệu thật, dùng phương pháp Leave-One-Trial-Out: với mỗi trial, trọng số được fit trên 14 trial còn lại rồi áp dụng để chạy Tầng 1 trên trial bị giữ lại, lặp cho cả 15 trial. Bảng 4.4 so sánh kết quả.

*Lưu ý: Bảng 4.4 chạy trên bản mã Tầng 1 trước lần code lại thứ hai (bản 18/10), chưa được lặp lại trên bản mã 29/10 dùng ở Bảng 4.2. Vì vậy hàng "Trọng số đặt tay" dưới đây KHÔNG khớp với Bảng 4.2 ở trên — đây là việc còn lại trước khi đưa phần so sánh LOTO vào luận văn như số liệu chính thức.*

**Bảng 4.4. Tầng 1 với trọng số học (LOTO) so với trọng số đặt tay (bản mã 18/10, chưa chạy lại)**

| Phương pháp | Precision | Recall | F1 | Recall nhóm thưa |
| --- | --- | --- | --- | --- |
| Trọng số LOTO | 1,000 ± 0,000 | 0,889 ± 0,000 | 0,941 ± 0,000 | 0,533 ± 0,038 |
| Trọng số đặt tay (bản 18/10) | 1,000 ± 0,000 | 0,870 ± 0,015 | 0,930 ± 0,009 | 0,378 ± 0,137 |

Trọng số học cho số liệu cao hơn, nhưng recall nhóm thưa trùng tuyệt đối với B1 trên cả 15/15 trial — dấu hiệu cần kiểm tra thêm trước khi tin dùng. Năm kiểm tra bổ sung (trùng cạnh giữa các trial, permutation test, feature ablation, phương sai đặc trưng cấu hình, độ ổn định qua nhiều random seed) cho thấy nguyên nhân: 15 trial dùng chung một hạ tầng (`shared-eval`) nên khoảng 31% cặp dịch vụ lặp lại y nguyên giữa các trial, và gần như toàn bộ sức phân biệt trong mẫu nằm ở bước phân loại nhiễu (một quy tắc cứng, không học) chứ không phải ở trọng số hồi quy logistic — permutation test cho AUC ≈ 0,5 (loại khả năng lỗi code/rò rỉ nhãn), nhưng chỉ hai đặc trưng `healthcheck_ratio`/`monitoring_ratio` đã cho AUC = 1,000, bằng đúng khi dùng đủ sáu đặc trưng.

Vì 15 trial không đủ độc lập để trọng số học tổng quát hóa tốt ra môi trường mới, đề tài giữ trọng số đặt tay làm mặc định sản phẩm (Bảng 4.2); việc fit trọng số bằng dữ liệu thật cần nhiều trial độc lập hơn, đưa vào Chương 6. Đề tài cũng xem xét và loại phương án dùng LLM sinh thêm dữ liệu để tăng cỡ mẫu: các đặc trưng ở đây là số đo thật từ cluster, không có cách nào một LLM biết được giá trị thật của chúng, nên dùng số liệu LLM bịa ra sẽ vi phạm nguyên tắc không bịa dữ liệu mà đề tài giữ xuyên suốt.

## 4.5. Bàn luận

### 4.5.1. Đối chiếu với các giả thiết (phần liên quan Tầng 1)

Giả thiết 1 (precision cao hơn B1 trên nhóm nhiễu) được xác nhận ở mức tổng thể (Bảng 4.2, 4.3); phần đo riêng trên nhóm nhiễu cần chỉ số `noise_precision` đang có lỗi định nghĩa (luôn bằng 0 cho cả ba phương pháp vì lát cắt nhiễu trong 15 trial không có cạnh ground truth dương — cần sửa thành tỷ lệ cạnh nhiễu bị kết luận STRONG rồi đo lại). Giả thiết 2 (tính điểm liên tục cho recall cao hơn ngưỡng cứng B2 trên nhóm thưa) được xác nhận có ý nghĩa thống kê, và còn rõ hơn ở lần chạy 29/10 (0,871 so với 0,000 của B2). Giả thiết 3 và 4 (Tầng 2, module thu thập cấu hình) chưa đối chiếu được, cần số liệu mục 4.4.1 và 4.4.4.

### 4.5.2. Giới hạn của thực nghiệm

- Ứng dụng mẫu nhỏ (khoảng 11 dịch vụ gốc) và kịch bản tự cấy trên hạ tầng dùng chung, nên kết quả chưa đại diện cho hệ thống vận hành thực tế.
- 15 trial dùng chung một hạ tầng nên không hoàn toàn độc lập (khoảng 31% cặp dịch vụ lặp lại giữa các trial) — không ảnh hưởng tính hợp lệ của Bảng 4.2, nhưng giới hạn khả năng tổng quát hóa của phần có học từ dữ liệu (mục 4.4.3).
- Bảng 4.2/4.3 dùng bộ kiểm selector giả lập (`MockSelectorValidator`), chưa chạy với bộ kiểm thật (`KubernetesSelectorValidator`) trên cụm thật — số liệu chính thức cần chạy lại trên VM với bộ kiểm thật, bằng cách truyền instance đó vào `run_tier1()` (hiện mặc định dùng bản giả lập nếu không truyền gì).
- Số liệu Bảng 4.2/4.3 là lần chạy lại thứ hai (29/10), sau lần code lại thứ hai của Tầng 1; Bảng 4.4 (LOTO) thì vẫn là số của bản mã 18/10 — hai bảng tạm thời không cùng một bản mã, cần chạy lại Bảng 4.4 trên bản mã mới trước khi dùng cả hai bảng như số liệu chính thức cuối cùng.
- Recall Tầng 1 trùng tuyệt đối với B2 trên cả 15/15 trial ở lần chạy 29/10 (Bảng 4.3) là một kết quả chưa giải thích được — cần xem lại code phân loại/gộp cạnh để xác nhận đây là hệ quả hợp lý của thay đổi thiết kế (ví dụ hai phương pháp hội tụ về đúng cùng một tập cạnh được giữ sau khi lọc nhiễu), không phải một lỗi, trước khi trình bày số liệu này như một phát hiện trong luận văn.
- Chưa có số liệu Tầng 2 và module thu thập cấu hình trên cùng 15 trial, nên chưa thể báo cáo recall gộp Tầng 1 + Tầng 2 — chỉ số so sánh chính thức với B1 theo thiết kế.
- Hysteresis chưa được thực nghiệm kiểm chứng; số liệu hiện dùng ngưỡng đơn.
- Chế độ ambient của Istio không hỗ trợ đọc nhãn nguồn tùy biến, nên việc tách dữ liệu 15 trial dựa vào namespace dùng chung và `pair_key`/`owner_map` ở tầng ứng dụng.
