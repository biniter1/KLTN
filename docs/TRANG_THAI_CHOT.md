# TRẠNG THÁI CHỐT KLTN — nguồn chuẩn duy nhất

**Chốt ngày 11/10/2026, cập nhật 12/10/2026** (Config Collector v2.1, dashboard v2.1), **15/10/2026** (Tầng 1: thiết kế lại 6 stage), **18/10/2026** (Tầng 1 đã code + VictoriaMetrics/VictoriaLogs, PR #1 **đã merge vào main**; 2 điểm lệch khi code đã chốt; đã thử fit trọng số bằng hồi quy logistic trên 15 trial thật, kết luận giữ trọng số đặt tay — xem mục 3.3), **22/10 và 27/10/2026** (cổng port ở cửa ra, `NOISE_ONLY_REVIEW`, `port_source = config` — PR #2, chưa merge) **và 30/10/2026** (revert `cac079f`, đồng bộ code KB từ `main`, chạy lại thí nghiệm trên `main`). File này là nguồn chuẩn cho toàn bộ đồ án.
- Khi file này lệch với tài liệu thiết kế khác trong knowledge base, **file này đúng**.
- Nội dung được đối chiếu với **code thật** (`tier1_inference/`, `tier2_agent/`, `orchestration/`, `infra/`) và **dữ liệu thật** (`infra/trial_data/`, `infra/experiment_report.json`).
- **Code chuẩn là nhánh `main` của repo `biniter1/KLTN`.** Code trong KB chỉ là bản sao (đồng bộ lại từ `main` ngày 30/10/2026); khi lệch, `main` đúng.
- Đề cương, báo cáo và slide đều phải viết theo file này. Có thay đổi thì sửa file này trước.

Ký hiệu trạng thái: ✅ đã có code và đã chạy · 🟡 đã có code, còn lỗi hoặc chưa chạy thật · 📐 mới có thiết kế, chưa có code · ❌ không làm.

---

## 1. Thông tin đề tài

| | |
|---|---|
| Tên tiếng Việt | Khung triển khai chính sách mạng theo kiến trúc Zero Trust cho hệ thống Kubernetes sử dụng tác nhân trí tuệ nhân tạo |
| Tên tiếng Anh | A Zero Trust Architecture-based network policy deployment framework for Kubernetes using AI agents |
| Ghi chú tên | Sửa theo góp ý của người nhận xét ("sửa nguyên lý Zero Trust"). Cần báo cô Dung trước khi nộp. |
| GVHD | ThS. Trần Thị Dung |
| Thời gian | 07/09/2026 – 29/12/2026 (16 tuần) |
| Sinh viên | Hà Sơn Bin – 23520149 – NT505.R11.ANTT |

## 2. Bài toán và vai trò của Zero Trust

**Bài toán gốc:** hệ thống Kubernetes chạy lâu năm có nhiều service, tài liệu thiếu, nên không dám bật NetworkPolicy mặc định từ chối (default-deny) vì sợ chặn nhầm làm sập hệ thống. Framework tự suy luận phụ thuộc giữa các service, kèm độ tin cậy, để di chuyển sang NetworkPolicy một cách an toàn.

Zero Trust xuất hiện ở **2 vai trò**:
1. **Đích đến của hệ thống được di chuyển**: default-deny, phân đoạn vi mô, đặc quyền tối thiểu đến mức port (NIST SP 800-207). Ở trạng thái default-deny, bỏ sót một phụ thuộc thì service sập, còn đoán thừa thì mở rộng bề mặt tấn công. Đây chính là lý do bài toán khó.
2. **Áp lên chính tác nhân AI (Tầng 2)**: không tin đầu ra của mô hình, chỉ cho dùng công cụ chỉ đọc trong danh sách cho phép, gatekeeper giữ thông tin xác thực, mọi hành động ghi nhật ký kiểm toán, chống prompt injection, kết luận suy ra bằng luật.

Phần **giám sát sau triển khai** (mục 3.7) giúp tính Zero Trust không bị suy giảm theo thời gian.

**Phạm vi:** chỉ phân đoạn mạng giữa các service và kiểm soát tác nhân AI. Không làm định danh người dùng hay trạng thái thiết bị.

## 3. Kiến trúc và trạng thái từng phần

Kiến trúc **2 tầng**:
- Tầng 1 tổng hợp và tính điểm.
- Tầng 2 là tác nhân AI.

Config Evidence Collector **không phải một tầng**: nó chỉ cấp bằng chứng cho Tầng 1.

### 3.1 Thu thập lưu lượng — 🟡
- Istio **ambient**: ztunnel (L4, DaemonSet, viết bằng Rust, không phải Envoy) và waypoint (L7, Envoy, 1 waypoint/namespace).
- Prometheus dùng addon mẫu của Istio. Nó quét các bộ đếm (counter); hệ thống **không đọc nội dung gói tin**. (Từ 18/10 có thêm đường đọc access log ztunnel qua VictoriaLogs — xem dưới.)
- `tier1/traffic_collector.py::collect_traffic()` chạy các query sau:
  - `increase(istio_requests_total{reporter="waypoint"}[7d])`;
  - cùng query đó nhưng chỉ lấy mã lỗi 5xx;
  - `istio_tcp_connections_opened_total`;
  - 7 query theo ngày cho HTTP/gRPC và (từ 18/10) cho TCP, từ đó tính `traffic_days`, `distinct_days` và `last_seen_days_ago`.
- Label tùy chỉnh duy nhất là `request_path` (file `telemetry-custom-labels.yaml`). Label `test_harness`/`injection_id` **không hoạt động dưới ambient** (giới hạn của Istio).
- **Không có DNS** trong dữ liệu: DNS đi UDP 53, ztunnel chỉ đo TCP, waypoint chỉ đo HTTP/gRPC.
- Chỗ lưu dữ liệu:
  - Đường đánh giá: file JSON `infra/trial_data/trial-XX.json` (do `08_export_trial_data.py` sinh).
  - Đường orchestrator: SQLite.
- Việc phân loại nhiễu **không** diễn ra lúc thu thập, mà ở Tầng 1.
- **Lỗi đã biết trên dữ liệu đã thu (08/10)** (xem mục 7): port luôn bằng 0 (nhãn `destination_port` vắng hẳn trong metric), không thu được series TCP nào, kiểm tra scrape gap có thể không đáng tin, `workload_revision` luôn là `"latest"`, tập ngày có traffic bị bỏ khi đổi sang `TrafficSeries` (chỉ giữ số đếm). Code đã sửa (18/10) nhưng 15 file trial hiện có vẫn là bản xuất cũ.
- **ĐÃ CHỐT (15/10), ĐÃ CODE (18/10, PR #1):** mỗi series giữ `traffic_days` + `distinct_days`; số request theo ngày trả ra `CollectionResult.daily_counts` (08_export ghi `series_daily_counts`). Sửa lỗi TCP không có query theo ngày; port vắng → None.
- **VictoriaMetrics + VictoriaLogs (18/10, PR #1, 🟡 chưa chạy thật):** stack repo LOG_METRIC (vmauth là điểm đọc duy nhất). `VictoriaMetricsClient` đọc `.../select/0/prometheus`, **từ chối kết quả `isPartial`**. `log_collector.py` đọc access log ztunnel từ VictoriaLogs → cạnh TCP có **port thật** (`port_source = observed_log`) + cạnh không qua waypoint. Nối vào `08_export` (`--metrics-backend`, `--logs-url`) và `cli_batch`. Cần xác nhận trên VM tên field log ztunnel và bộ lọc luồng.

### 3.2 Config Evidence Collector (v2) — ✅
- Các bước: thu thập config theo pod template → hợp nhất effective config → phễu lọc → **LLM trích xuất (OpenAI gpt-4o-mini)** → xác thực tất định (grounding + K8s/Istio API).
- Không đọc Secret.
- Có cache LLM theo hash để kết quả tái lập được.
- Baseline regex (code v1) vẫn giữ trong `config_collector/regex_baseline.py`.
- **Sửa 12/10 (đã có trên `main`, chưa chạy trên VM):**
  - Bước thu thập quét đủ workload template (Deployment, StatefulSet, DaemonSet, ReplicaSet, Job, CronJob, Pod trần), kể cả `replicas=0` và CronJob chưa chạy.
  - Đọc `configMapKeyRef` (thay bằng giá trị thật), `fieldRef`/`resourceFieldRef` (placeholder), `envFrom.secretRef`, volume `secret`/`projected`.
  - Đọc image config bằng crictl trong node kind, rồi tới Registry API v2 (file `image_config.py`, biến môi trường `CONFIG_COLLECTOR_IMAGE_READER`).
  - **Cần làm tiếp:** chạy thử trên VM; thu lại config evidence cho 15 trial; chạy lại `08_export` và `09`.
- **ĐÃ CHỐT (12/10) — v2.1, 📐 chưa code** (chi tiết: `CONFIG_EVIDENCE_COLLECTOR.md` v2.1):
  - **Nhiều nguồn sinh candidate, một bước xác thực chung:** quy tắc công ty (nhập dạng có cấu trúc, không nhận regex thô, có `rule_set_version`) + regex chung theo hình dạng giá trị + LLM (bật/tắt được; tắt = chạy hoàn toàn tất định cho môi trường không cho gửi dữ liệu ra ngoài). Mỗi candidate ghi `extraction_method`. Đây **không** phải fallback: khi LLM bật mà lỗi thì trial vẫn FAILED.
  - **Bước Parse tất định** sau grounding: code tự tách host/port từ câu trích, so với cái LLM điền; nhãn STRUCTURAL/EMBEDDED tính theo địa chỉ đã parse.
  - **Index tra cứu** cho ảnh chụp cluster (dict, inverted index cho selector, trie cho host wildcard của ServiceEntry).
  - **Truy vết:** `evidence_refs` thêm `source_ref` + `extraction_method`; `item_id` duy nhất trong cả lần chạy.
  - **Port:** hỗ trợ `targetPort` dạng tên và khớp port theo tên.
  - **Phân vai xác thực:** Collector trả lời "trích có đúng không" (grounding + parse) và "trỏ tới cái gì có thật" (cluster index; không thấy ≠ sai). "Có thật sự gọi không" là việc của Tầng 1 + Tầng 2. Lỗi bỏ sót đo bằng recall và giảm bằng nhiều nguồn candidate. **Tầng 2 không phải lưới an toàn cho config bị sót** (cạnh không có trong C thì agent không được gọi tới).
  - Không làm: "chế độ khám phá" cho Tầng 2; LLM gắn "loại dependency".
  - **Thêm (15/10):** Collector đọc thêm `livenessProbe`/`readinessProbe`/`startupProbe` (`httpGet.path`, `grpc`) của container để Tầng 1 dùng làm danh sách healthcheck (mục 3.3). Phía collector cluster đã gắn cờ `probe_path` (18/10); phần đọc probe trong `config_collector` vẫn thuộc v2.1.

### 3.3 Tầng 1 — Tổng hợp và tính điểm — ✅ code chốt 15/10 đã merge (PR #1, 18/10) · trọng số: 🟡 · 🟡 chưa có số liệu chính thức (chưa chạy `08_export` mới + `09 --validate-selectors` trên VM)
- **18/10:** đã code toàn bộ quyết định 15/10 (giai đoạn A–F + G1–G3 của `claude/TIER1_CHANGE_PLAN.md`), sửa 11 lỗi L1–L11. Bước B3 (chuẩn hóa path, suy port từ Service) nằm trong `schema.normalize_path` + `cluster_context.py` — **không** có file `normalize.py` riêng.
- **Chạy lại trên `main` ngày 30/10** (commit `50a99bd`, cây mã giống hệt merge PR #1 `fee1bd9`), 15 file trial CŨ, validator giả: Tầng 1 P 1,000 · R 0,870 · F1 0,930 · recall nhóm thưa 0,378 (trước khi code lại: 0,888 / 0,852 / 0,869 / 0,256) — **khớp tuyệt đối** lần chạy 18/10. Chưa chính thức. Chi tiết + Wilcoxon: mục 5. Số trong `infra/experiment_report_rerun.json` (P 0,941 / R 0,822) **không** phải từ code `main`, đã gỡ khỏi repo (revert `cac079f`) — không dùng.
- **Điều chỉnh khi code — ĐÃ CHỐT 18/10 (đồng ý, giữ nguyên):**
  - gRPC reflection bỏ khỏi healthcheck mặc định: harness thí nghiệm sinh traffic cho dependency cấy bằng `grpcurl` qua reflection, nên trong phạm vi thí nghiệm đây là traffic THẬT (ground truth dương), không phải healthcheck — gắn nhãn healthcheck làm recall tụt 0,852→0,789. Môi trường thật dùng reflection để healthcheck/debug thì tự thêm 2 path đó vào `healthcheck_extra_paths` (đã có sẵn trong `ScoringConfig`).
  - `EXPERIMENT_CONFIG` khai `*/prometheus`, `*/grafana`, `*/blackbox-exporter` là monitoring đã xác minh: CHỈ áp dụng cho cấu hình thí nghiệm (3 nguồn nhiễu monitoring do harness tự dựng, SA trùng tên nguồn giả lập), KHÔNG phải mặc định sản phẩm (mặc định sản phẩm giữ chặt, chỉ tin `istio-system/prometheus`). Báo cáo Chương 4 phải ghi rõ đây là cấu hình thí nghiệm, tách bạch khỏi default, để không bị hiểu nhầm là hạ chuẩn bảo mật mặc định.
- Tập ứng viên `C = C_observed ∪ C_potential`.
- Phân loại lưu lượng (BUSINESS / HEALTHCHECK / MONITORING / DNS / CONTROL_PLANE / UNKNOWN).
- Khóa định danh `identity_key` (7 phần, có SA nguồn).
- Điểm `score = sigmoid(...)` với **6 đặc trưng** (đã bỏ `protocol_diversity`, bias −1,0).
- Hysteresis 0,68 / 0,58.
- 5 trạng thái đầu ra. Luật cứng: `count > 0` là điều kiện bắt buộc để được `OBSERVED_STRONG`.
- **Trọng số hiện tại là giá trị đặt tay** (`tier1/config.py`). Hàm `fit_weights_logistic_regression()` đã có — xem kết luận 18/10 ngay dưới về việc vì sao CHƯA dùng làm default.
- **ĐÃ CHỐT (11/10):** sẽ fit trọng số bằng hồi quy logistic. Fit trên một phần trial, báo cáo kết quả trên phần còn lại (tách theo trial).
- **ĐÃ THỬ (18/10) — kết luận: KHÔNG thay trọng số default, chỉ đưa vào Chương 4 như phân tích bổ sung.** Dùng 15 file `trial_data/trial-XX.json` thật của Bin (xuất 08/10, trên máy Bin). Phương pháp: Leave-One-Trial-Out (fit trên 14 trial, kiểm trên trial còn lại, lặp đủ 15 lần, giữ R=15 cho số báo cáo cuối) — tái dùng nguyên `tier1.pipeline.prepare_snapshot` / `tier1.experiment.run_trial`, không viết lại logic suy luận.
  - **Kết quả LOTO:** Tier 1 (trọng số fit) P=1,000 · R=0,889 · F1=0,941 · recall nhóm thưa 0,533±0,038 — recall **trùng tuyệt đối với B1 trên cả 15/15 trial** (Wilcoxon suy biến), precision vẫn cao hơn B1/B2 có ý nghĩa (p≈0,0001 và p≈0,0004). Đối chứng lại trọng số đặt tay trên ĐÚNG 15 file này ra khớp 100% số đã báo trước đó (P=1,000/R=0,870/F1=0,930/thưa=0,378) — xác nhận script đo không lệch pha với pipeline thật.
  - **5 kiểm tra bổ sung phát hiện vấn đề nghiêm trọng hơn con số P/R đẹp:**
    1. **Trùng cạnh giữa trial:** 31% cặp service (21/67) xuất hiện Y NGUYÊN ở cả 15/15 trial, đặc trưng giống hệt từng chữ số (do dùng chung hạ tầng `shared-eval`) — 270 "mẫu" không phải 270 mẫu độc lập, cỡ mẫu thật nhỏ hơn số đó rất nhiều.
    2. **Permutation test** (xáo nhãn ngẫu nhiên, fit lại 10 lần): AUC≈0,496±0,117 — đúng kỳ vọng ~0,5, loại trừ khả năng code bị lỗi/rò rỉ nhãn.
    3. **Feature ablation:** chỉ `healthcheck_ratio` + `monitoring_ratio` đã cho AUC=1,000, bằng đúng khi dùng đủ 6 đặc trưng — toàn bộ sức phân biệt nằm ở bước phân loại nhiễu (quy tắc cứng, không học), sigmoid/trọng số gần như không đóng góp thêm trong harness này.
    4. **`config_match_strength` có phương sai = 0** trong mẫu (luôn = 1,000 cho CẢ cạnh thật và cạnh nhiễu, vì cạnh nhiễu harness — ví dụ Prometheus — cũng có config thật trỏ đúng) → đặc trưng này vô nghĩa để fit, giải thích vì sao `w_is_structural`/`w_is_embedded` đảo dấu bất ổn (gần như đối xứng nhau) ở MỌI mức regularization đã thử (C=1,0 → 0,001).
    5. CV qua 10 `random_state` khác nhau: std=0 — ổn định tuyệt đối, nhưng vì bài toán tách lớp tầm thường trong harness nhỏ/kiểm soát chặt này, không phải vì mô hình học tốt một quy luật tổng quát.
  - **Quyết định:** giữ trọng số đặt tay làm default sản phẩm trong `config.py`. Việc fit trọng số thật cần môi trường có nhiều trial ĐỘC LẬP hơn (không dùng chung 1 hạ tầng) — ghi vào hướng phát triển, không làm trong phạm vi PoC này. **KHÔNG dùng AI (LLM) sinh thêm dữ liệu để tăng cỡ mẫu** — đã xem xét và loại, vì đây là số đo thật từ cluster (Prometheus + K8s config + ground truth đã cấy), LLM không có cách nào biết giá trị thật, dùng số LLM bịa sẽ vi phạm đúng nguyên tắc "không bịa dữ liệu" mà cả dự án giữ xuyên suốt.
  - **Khung viết Chương 4/5 cho phần này:** trình bày như một phân tích/ablation bổ sung, KHÔNG phải kết quả chính; nêu đúng 5 phát hiện trên làm minh chứng đã kiểm tra kỹ phương pháp, không chỉ báo số đẹp — đúng tinh thần "so sánh và phân tích", không hứa hơn baseline (mục 5).
- **ĐÃ CHỐT (15/10) — phân loại + gộp cạnh, ✅ đã code (PR #1, 18/10)** (chi tiết: `TIER1_DESIGN.md` mục 2, 2b, 4, 6, 7):
  - **Thứ tự luật mới:** DNS → MONITORING (đã xác minh) → CONTROL_PLANE → HEALTHCHECK → BUSINESS → UNKNOWN. Bản cũ: MONITORING đứng sau CONTROL_PLANE và HEALTHCHECK nên Prometheus thật không bao giờ được gắn MONITORING.
  - **DNS:** luật tối giản (đích là `kube-system/kube-dns` hoặc port 53), chỉ để loại khỏi chấm điểm. DNS luôn được Planner tự mở. ❌ Không làm nguồn log CoreDNS (hướng phát triển).
  - **MONITORING theo danh tính đã xác minh:** so (namespace, service account) lấy từ SPIFFE principal với tập tự phát hiện từ `prometheus_url` + label `app.kubernetes.io/name=prometheus` + khai báo trong config. Chỉ trùng tên thì gắn cờ `claimed_monitoring`, không trừ điểm (chống giả tên). **Yêu cầu:** Prometheus phải chạy trong mesh. Công cụ khác (Grafana, Kiali…) là phần mở rộng.
  - **Thí nghiệm — phương án (a):** khai báo SA `prometheus` của harness nhiễu monitoring vào danh sách đã xác minh trong config thí nghiệm; báo cáo ghi rõ.
  - **HEALTHCHECK 3 tầng:** path lấy từ probe trong pod spec (chính, cờ `probe_path`) + danh sách mặc định ngắn (`/health`, `/healthz`, `/readyz`, `/livez`, `/ready`, `/live`, `/metrics`, `grpc.health.v1.Health`) + đội vận hành tự thêm (`healthcheck_extra_paths`, YAML, sau này trên dashboard). So khớp theo segment cuối, không theo tiền tố. **gRPC reflection KHÔNG nằm trong danh sách mặc định** (chốt 18/10, xem trên).
  - **Port:** `port = None` + `port_source`, thứ tự: `observed` (nhãn metric) → `observed_log` (access log ztunnel thấy đúng 1 port) → `inferred_single_port` → `inferred_protocol_match` (suy từ Service spec qua `destination_service_ref`) → `config` (27/10, xem dưới) → `unknown`. Cạnh `unknown` không được sinh "allow mọi port".
  - **Ngày:** gộp cạnh lấy **hợp** `traffic_days`, không lấy max.
  - **Bỏ đặc trưng `protocol_diversity`** (luôn bằng 0,5 vì protocol nằm trong khóa cạnh); bù **bias −1,2 → −1,0** để điểm không đổi. Còn 6 đặc trưng.
- **ĐÃ CHỐT (15/10) — khớp traffic với config, ✅ đã code (PR #1, 18/10)** (chi tiết: `TIER1_DESIGN.md` mục 1b):
  - Config chưa phân giải dùng `pair_key` có đích `raw:<host>`. **Sửa lỗi thật:** code cũ dùng `None` nên mọi đích UNRESOLVED của cùng một nguồn dồn vào 1 ô và chỉ 1 đích ra kết quả.
  - Config ở port không có traffic, trong cặp đã có traffic, sinh kết quả `NOT_OBSERVED` riêng cho port đó (trước đây bị mất, ảnh hưởng recall nhánh hiếm).
  - Ghi `config_match_level` (EXACT / PORT_ONLY / PAIR_ONLY) + cờ `protocol_mismatch`, `sa_mismatch`; chọn config đại diện tất định theo `item_id`. Bảng strength **giữ nguyên**.
  - Cặp chỉ có config: 1 kết quả `NOT_OBSERVED` cho mỗi port.
- **ĐÃ CHỐT (15/10) — chấm điểm, ✅ đã code (PR #1, 18/10)** (chi tiết: `TIER1_DESIGN.md` mục 4, 4a, 5, 13.3):
  - `log_volume` chia cho hằng số `volume_reference` cố định (lấy 1 lần từ dữ liệu rồi đóng băng: 1.674.596), không tính lại theo snapshot.
  - Khi fit: config thành 3 đặc trưng 0/1 (`is_structural`, `is_embedded`, `is_unresolved`).
  - **Không dùng `count_floor`** (1 request + config = 2 nguồn độc lập xác nhận); cạnh STRONG có count < 3 gắn cờ `low_count_strong` để dashboard đánh dấu.
  - **Cạnh chỉ toàn nhiễu** (không series BUSINESS/UNKNOWN nào): monitoring đã xác minh → Planner tự mở (`auto_allow_reason = VERIFIED_MONITORING`); còn lại → người duyệt, **không gọi Tier 2**. Sửa lỗi: bản cũ đẩy mọi cạnh nhiễu (điểm ~0,47–0,49) sang Tier 2.
  - Cạnh thưa không config không lên STRONG là **đúng thiết kế**; báo cáo lấy recall Tier 1 + Tier 2 làm chỉ số chính.
- **ĐÃ CHỐT (15/10) — trạng thái + hysteresis, ✅ đã code (PR #1, 18/10)** (chi tiết: `TIER1_DESIGN.md` mục 5b, 13.4):
  - Thứ tự quyết định: count = 0 → nhánh nhiễu → UNKNOWN → hysteresis.
  - **Đường đánh giá hiện tại không chạy hysteresis** (1 snapshot/trial, không SQLite) → số liệu hiện có là ngưỡng đơn 0,68. Ablation hysteresis: **N = 7 snapshot** cắt từ 7 ngày đã thu, 1–2 trial — script `10_hysteresis_ablation.py` đã có (18/10) nhưng **cần file trial có `series_daily_counts`** (xuất bằng `08_export` mới); 15 file hiện có không chạy được (thử 30/10).
  - Đổi định dạng khóa cạnh → chấp nhận reset lịch sử `previous_status` 1 lần.
  - Trạng thái UNKNOWN giữ làm chốt chặn (thực tế 0 trường hợp).
  - **Mọi ngưỡng tĩnh giữ giá trị khởi tạo, con số chính thức chọn khi chạy đánh giá** (θ qua ROC trên tập fit, ±0,05 cho promote/demote).
- **ĐÃ CHỐT (15/10) — kiểm selector, scope, lưu trữ, ✅ đã code (PR #1, 18/10)** (chi tiết: `TIER1_DESIGN.md` mục 3b, 7, 8, 10b):
  - ⚠ **Lỗi đã sửa:** validator thật đọc nhãn sai `security.istio.io/canonical-name` → trên cluster thật mọi cạnh STRONG sẽ thành NEEDS_MANUAL_SELECTOR. Sửa: suy canonical theo thứ tự Istio.
  - Số liệu hiện có dùng **validator giả** (`MockSelectorValidator`) → báo cáo phải ghi rõ. Đánh giá chính thức dùng validator thật (`09 --validate-selectors`, chỉ đọc). Kiểm 2 lần: Tier 1 báo sớm + Planner bắt buộc trước khi sinh YAML. Đọc Service/Pod 1 lần mỗi namespace. Selector phía nguồn do Planner kiểm.
  - Scope: PassthroughCluster/BlackHoleCluster → ServiceEntry (index Collector) → service_ref → dấu chấm → UNKNOWN.
  - DNS chỉ quyết ở bước phân loại (bỏ `is_dns_edge`).
  - ⚠ **Lỗi lưu trữ đã sửa:** tra `previous_status` thiếu SA; chạy lại cùng snapshot đọc nhầm kết quả của chính nó. Đã sửa + thêm `snapshot_time` và các cột mới.
  - Toàn bộ kế hoạch sửa code Tầng 1: `claude/TIER1_CHANGE_PLAN.md`.
- **ĐÃ CHỐT (22/10) — cổng port ở cửa ra, ✅ đã code, 🟡 PR #2 (`tier1-port-gate`) chưa merge** (chi tiết: `TIER1_DESIGN.md` mục 2.1b, 6, dòng 23):
  - Tầng 1 **không chặn** cạnh thiếu/mâu thuẫn port ở cửa vào: vẫn chấm điểm và quyết trạng thái ("có quan hệ không" độc lập với "mở cửa nào"), chỉ gắn cờ `port_unresolved`, `config_port_differs`, `multiple_ports_seen`.
  - Hàng đợi review (vai Planner) chặn **ở cửa ra**, chỉ với cạnh sắp sinh rule (STRONG, Tầng 2 CONFIDENT, NEEDS_MANUAL_SELECTOR, cạnh có `auto_allow_reason`):
    - `multiple_ports_seen`, hoặc port **suy ra** (`inferred_*`) + `config_port_differs` → `PORT_CONFLICT`;
    - port `None` → `NEEDS_MANUAL_PORT`;
    - port **thấy được** lệch config → không chặn (config lệch đã thành dòng `NOT_OBSERVED` riêng).
  - Người duyệt xử lý bằng hành động `SUBMIT_PORT` (payload `port` 1..65535 = targetPort pod đích) hoặc `REJECT`. API tự dựng lại bảng `human_decisions` cho DB cũ (giữ dữ liệu).
- **ĐÃ CHỐT (22/10) — `NOISE_ONLY_REVIEW`, ✅ đã code, 🟡 PR #2 chưa merge:** cạnh chỉ toàn nhiễu mà không phải monitoring đã xác minh (không có `auto_allow_reason`, không STRONG) vào hàng đợi người duyệt với lý do `NOISE_ONLY_REVIEW` — không tự bỏ, vì bỏ nhầm là mất quan hệ thật (false negative).
- **ĐÃ CHỐT (27/10) — lấy port từ cấu hình, ✅ đã code, 🟡 PR #2 chưa merge** (chi tiết: `TIER1_DESIGN.md` dòng 24): cạnh có traffic mà 4 cách xác định port đều thất bại, nếu bằng chứng cấu hình **sạch** của cùng cặp có **đúng 1** port (đã là targetPort) → lấy port đó, `port_source = config`, cờ `port_from_config`. ≥2 port → không đoán (cửa ra `NEEDS_MANUAL_PORT`). Khóa mới trùng cạnh đã có → giữ None, cờ `config_port_taken`. Port thấy được không bao giờ bị config ghi đè.

### 3.4 Tầng 2 — Tác nhân AI — ✅ (chưa chạy trên 15 trial)
- Kiến trúc Brain, vòng lặp ReAct, LLM là Claude (Anthropic).
- 4 công cụ chỉ đọc: mã nguồn, mô tả PR (qua gatekeeper), tài liệu nội bộ, cửa sổ lưu lượng mở rộng.
- Kiểm tra cả đầu vào lẫn đầu ra của công cụ. Context Manager gồm sanitize, check_injection, build, normalize, format.
- Suy luận theo giả thuyết với 5 trạng thái, ra 4 mức kết luận cuối: CONFIDENT / NEEDS_MONITORING / INSUFFICIENT_BASIS / CONFLICTING_EVIDENCE. Kết luận suy ra bằng luật; chỉ được CONFIDENT khi có bằng chứng có cấu trúc xác nhận.
- Có 180 unit test (trên `main`, 30/10).
- Tích hợp orchestrator (`orchestrator_hook.py`: `attempt_id`, `tier2_session_id`, khớp identity bằng `IS ?`) đã có trên `main` từ 18/10 (commit `7a6dfcb`, áp `CHANGES_existing_modules.patch`).
- **(02/11) PR #3 `tier2-run-tooling` (chưa merge):** ghi token từng lần gọi model vào bảng `llm_usage`; script `infra/scripts/11_export_tier2_results.py` (chỉ đọc SQLite) xuất kết quả từng cạnh/từng trial và tính recall gộp Tầng 1 + Tầng 2 (**chỉ `CONFIDENT` tính là dương**, chốt 02/11), kèm B1/B2, CI 95%, Wilcoxon. Tier 2 chỉ chạy được trên cụm đang sống, cần đủ 7 ngày lưu lượng liên tục tới lúc chạy.
- ❌ Không làm: Bayesian log-odds, self-consistency k=5.
- **ĐÃ CHỐT (11/10):** chạy Tầng 2 trên cả 15 trial qua orchestrator để lấy số liệu (tỷ lệ ảo giác, tỷ lệ escalate, chi phí LLM).

### 3.5 Điều phối (Orchestration)
- v1 — 🟡: code giao ngày 06/10, gồm Batch Orchestrator, API Server, gatekeeper, review, tách bảng theo người ghi. Patch `CHANGES_existing_modules.patch` **đã áp vào `main` ngày 18/10** (commit `7a6dfcb`, PR #1; sửa tay 2 hunk lệch ở `config_collector/pipeline.py`). 120 test trên `main` (134 trên nhánh PR #2).
- v2 — 📐: thiết kế đã chốt ngày 08/10 (`claude/ORCHESTRATION_V2_DRAFT.md`): dashboard thay CLI, 2 nguồn dữ liệu live/snapshot, mốc T, dừng 2 cấp, job runner. Chưa có code.

### 3.6 Migration Planner — 📐
- Ba đầu ra: kế hoạch triển khai theo đợt, hàng đợi ngoại lệ, đề xuất GitOps PR.
- Chỉ các cạnh ở mức "tin chắc" mới vào đợt. Các cạnh còn lại vào hàng đợi ngoại lệ, không chặn các đợt khác.
- **Thuật toán xếp đợt chưa thiết kế.** Đề cương chỉ ghi "theo mức độ tin cậy và phạm vi ảnh hưởng", không dùng công thức.
- **Policy Impact Validator** (= bước "mô phỏng chính sách"): suy ra tập cạnh mà policy cho phép, rồi so với `dependency_current_state` để ra verdict BLOCKED / PASSED_WITH_WARNINGS / PASSED.
- **Blast-radius override**: cạnh STRONG nhưng đích nhạy cảm (namespace hệ thống, ra ngoài cluster) vẫn phải qua người duyệt.
- **Luôn tự mở DNS** (egress tới `kube-system/kube-dns` UDP/TCP 53) cho mọi pod trong mọi đợt, không cần bằng chứng. **Tự mở cạnh monitoring đã xác minh** (Prometheus thật, đúng service account) theo cùng cơ chế. Cạnh thiếu hoặc mâu thuẫn port bị chặn ở cửa ra (`NEEDS_MANUAL_PORT` / `PORT_CONFLICT`, mục 3.3 — 22/10) để người duyệt nhập port.

### 3.7 Giám sát sau triển khai (Zero Trust Continuity Monitor) — 📐, **chỉ ở mức demo**
Thiết kế theo `claude/MIGRATION_PLANNER_DESIGN.md` mục 2 (chốt ngày 11/10):
- **`emergency_exceptions`**: sổ đăng ký thay đổi khẩn cấp, bắt buộc có hạn dùng. Một sweep định kỳ đẩy những mục đã hết hạn vào hàng đợi review. Hệ thống **cảnh báo, không tự chặn**.
- **`config_change_log` và Drift Detector**: theo dõi mọi thay đổi qua K8s Audit Log và lịch sử Git commit. Thay đổi không truy được nguồn gốc (orphan) thì báo `UNATTRIBUTED_CONFIG_CHANGE`. **Không dùng Argo CD.**
- **`STALE_PERMISSION`**: cạnh đang được enforce mà Tầng 1 hạ khỏi STRONG thì đưa vào review. Không tự thu hồi quyền.
- ❌ Không có cổng chặn trước (admission webhook, Kyverno/OPA): đây là hướng mở rộng.
- Đánh giá: 1–2 kịch bản demo (service test bị bỏ, hotfix quên đóng). **Không** đánh giá định lượng drift.

### 3.8 Dashboard — 📐
Có 2 lớp: lớp A chỉ đọc (phục vụ nghiên cứu) và lớp B review có ghi. Chi tiết trong `claude/DASHBOARD_DESIGN.md` và bản v2 ở mục 3.5.
- **v2.1 (12/10):** màn hình Config Collector có 4 tab (Phễu · Candidate & xác thực · Truy vết cạnh · Đóng góp theo nguồn), để dev tra ngược 1 cạnh về đúng item config, ConfigMap/file/image và nguồn candidate. Thêm màn hình **Cấu hình Collector** (bật/tắt nguồn, quy tắc công ty, model, image reader), bảng `config_rule_sets`, cột mới trong `collection_quality_log`. Trước 29/12: quy tắc công ty chỉ đọc từ YAML, chưa sửa trên giao diện.
- **v2.1 (12/10) — Traffic Collector:** màn hình 4 tab (Bảng series theo khóa · Biểu đồ theo ngày/top cạnh/phân bố `distinct_days` · Chất lượng dữ liệu kèm cảnh báo lỗi đã biết · Nguồn & truy vết). Màn hình **Cấu hình Traffic Collector**: nguồn mặc định live/snapshot, thông số Prometheus + nút kiểm tra kết nối, snapshot do hệ thống chụp **hoặc người dùng tải lên** (kiểm tra định dạng + hash trước khi nhận, `origin` ghi trong `data_snapshots`). Cần thêm bảng `traffic_series`, `traffic_daily_counts` (hiện series chưa được lưu bảng nào).
- **Thêm (15/10), 📐:** cột `port_source` và cờ (`claimed_monitoring`, `port_unresolved`, `port_from_config`) trên bảng series/cạnh; tab Chất lượng dữ liệu đếm cạnh theo `port_source` và liệt kê "cạnh chỉ có healthcheck"; cảnh báo `claimed_monitoring`. Danh sách healthcheck do đội vận hành thêm và danh sách monitoring đã xác minh nằm trong màn hình cấu hình (trước 29/12: chỉ đọc từ YAML).

## 4. Môi trường thực nghiệm

- **kind trên máy ảo Azure**, Istio ambient cộng waypoint, Prometheus addon.
- Mô hình **dùng chung hạ tầng** (`05_deploy_shared.sh`): 1 namespace `shared-eval` chứa 1 bản Online Boutique và khoảng 83 harness Deployment phục vụ **15 trial ảo** (`base_seed = 20260101`).
- Dữ liệu được tách theo trial bằng `pair_key` cộng `owner_map`. Không dùng label Prometheus. Cạnh gốc của Online Boutique thuộc về mọi trial.
- Đã thu đủ 7 ngày lưu lượng (dữ liệu xuất ngày 08/10/2026).
- ⚠ Cần kiểm tra retention và kiểu lưu trữ của Prometheus, rồi chụp snapshot trước khi dữ liệu bị xóa (mặc định giữ 15 ngày).
- Đề cương **chỉ mô tả định tính**, không ghi số node hay số pod.

## 5. Đánh giá

| Nội dung | Trạng thái |
|---|---|
| R=15 trial ảo, mỗi trial một kịch bản cấy ngẫu nhiên khác nhau; báo cáo trung bình ± CI 95%, kiểm định Wilcoxon bắt cặp | ✅ |
| B1: có lưu lượng là kết luận có phụ thuộc · B2: ngưỡng cứng cũ (`count ≥ 20` và `distinct_days ≥ 3`) | ✅ |
| B3: LLM một lần, dùng cùng gói bằng chứng, không có vòng lặp và không grounding | 📐 chưa code |
| Độ nhạy theo ngưỡng; calibration (chỉ cho điểm của Tầng 1); chọn θ qua ROC sau khi fit | 📐 |
| Ablation hysteresis: 7 snapshot cắt từ 7 ngày đã thu, 1–2 trial, đo số lần đổi trạng thái | 🟡 script có (18/10), chưa chạy được: cần file trial xuất lại có `series_daily_counts` |
| Tầng 2 trên 15 trial: tỷ lệ ảo giác, tỷ lệ bị grounding loại, độ ổn định, tỷ lệ escalate, chi phí | 📐 (đã chốt sẽ chạy) |
| Collector: chỉ LLM · chỉ regex chung · gộp, so với baseline regex v1 (recall, precision, tỷ lệ bịa, đóng góp riêng từng nguồn); quy tắc công ty chỉ demo | 📐 |
| Drift định lượng | ❌ |

**Kết quả mới nhất — chạy lại trên `main` ngày 30/10/2026** (commit `50a99bd` = cây mã merge PR #1; `09_run_experiment.py`, `EXPERIMENT_CONFIG`, validator giả; 15 file trial **cũ** xuất 08/10 — chưa có port/ngày) — **chưa phải số chính thức**, khớp tuyệt đối lần chạy 18/10:

| | Precision | Recall | F1 | Recall nhóm thưa |
|---|---|---|---|---|
| Tầng 1 | 1,000 ± 0,000 | 0,870 ± 0,015 | 0,930 ± 0,009 | 0,378 ± 0,137 |
| B1 | 0,889 ± 0,000 | 0,889 ± 0,000 | 0,889 ± 0,000 | 0,533 ± 0,038 |
| B2 | 0,885 ± 0,008 | 0,822 ± 0,013 | 0,852 ± 0,009 | 0,000 ± 0,000 |

- Wilcoxon bắt cặp: Tầng 1 vs B1 — recall p ≈ 0,025 (Tầng 1 **thấp hơn** B1), precision p ≈ 0,0001 (Tầng 1 cao hơn); Tầng 1 vs B2 — recall p ≈ 0,002, precision p ≈ 0,0004 (Tầng 1 cao hơn).
- `noise_precision` vẫn bằng 0 cho cả 3 phương pháp (lát cắt nhiễu không có cạnh ground truth dương — cần đổi chỉ số, backlog 9).
- File: `experiment_report_main_20261030.json` (Bin giữ; chưa commit vào repo).
- `infra/experiment_report_rerun.json` (P 0,941 / R 0,822, ghi "chạy 29/10") **không** được chạy từ code `main` → đã revert khỏi repo, **không dùng**.

**Kết quả cũ** (`experiment_report.json`, trước khi code lại các thay đổi 15/10), giữ để so trước/sau:

| | Precision | Recall | F1 | Recall nhóm thưa |
|---|---|---|---|---|
| Tầng 1 | 0,888 | 0,852 | 0,869 | 0,256 |
| B1 | 0,889 | 0,889 | 0,889 | 0,533 |
| B2 | 0,885 | 0,822 | 0,852 | 0,000 |

- Bản cũ: recall Tầng 1 thấp hơn B1 (p≈0,008), cao hơn B2 (p≈0,007).
- **Chốt 15/10:** chỉ số chính khi so với B1 là **recall Tầng 1 + Tầng 2**; recall riêng Tầng 1 chỉ để phân tích (Tầng 1 theo thiết kế không tự kết luận cạnh thưa thiếu config).
- ⇒ **Đề cương và báo cáo không được hứa "Tầng 1 tốt hơn baseline".** Chỉ ghi "so sánh và phân tích".
- **(18/10)** Đã thử fit trọng số bằng hồi quy logistic (Leave-One-Trial-Out) trên 15 trial thật — xem mục 3.3 để biết đầy đủ kết quả, 5 kiểm tra chẩn đoán (trùng cạnh giữa trial, permutation test, feature ablation...), và lý do giữ trọng số đặt tay làm default.

## 6. Kế hoạch (ĐỀ XUẤT — chờ Bin xác nhận)

| Giai đoạn | Tuần | Thời gian | Nội dung |
|---|---|---|---|
| 1 | 1–2 | 07/09 – 20/09 | Khảo sát, đề cương |
| 2 | 3–5 | 21/09 – 11/10 | Hạ tầng, thu lưu lượng, Collector, Tầng 1, Tầng 2 (code), đánh giá Tầng 1 lần đầu |
| 3 | 6–8 | 12/10 – 01/11 | Sửa phần thu thập (port, TCP), fit trọng số bằng hồi quy logistic, baseline B3, chạy Tầng 2 trên 15 trial |
| 4 | 9–11 | 02/11 – 22/11 | Migration Planner, Policy Impact Validator, giám sát sau triển khai, orchestration v2 và dashboard |
| 5 | 12–14 | 23/11 – 13/12 | Tổng hợp đánh giá, chạy các kịch bản demo |
| 6 | 15–16 | 14/12 – 29/12 | Hoàn thiện báo cáo, chuẩn bị bảo vệ |

## 7. Việc còn phải làm (backlog)

1. **Port luôn bằng 0** trong traffic series cũ: `istio_requests_total` không có label `destination_port`. **Đã code (18/10):** `port = None` + `port_source`, suy từ `destination_service_ref` + Service spec, thêm nguồn `observed_log` (VictoriaLogs) và (27/10, PR #2) `config`. Thêm tagOverride `destination.port` vào Telemetry **chỉ sau khi đã chụp snapshot** (xem việc 15).
2. **Không thu được series TCP** (ví dụ `cartservice → redis-cart`): kiểm tra giá trị `reporter` của metric TCP. Query theo ngày cho TCP đã thêm (18/10, L11).
3. Kiểm tra scrape gap chỉ xét thời điểm hiện tại, và job `istio-proxy` có thể không tồn tại.
4. **ĐÃ THỬ 18/10, kết luận giữ nguyên trọng số đặt tay làm default** — xem chi tiết mục 3.3 (Leave-One-Trial-Out trên 15 trial thật + 5 kiểm tra chẩn đoán: 31% cạnh trùng y nguyên giữa các trial, permutation test sạch, feature ablation cho thấy healthcheck/monitoring_ratio một mình đã AUC=1.0, `config_match_strength` phương sai=0 trong mẫu). Không đóng việc này hẳn — thử lại khi có thêm trial THẬT ĐỘC LẬP (không dùng chung 1 hạ tầng).
5. Viết baseline B3.
6. Chạy Tầng 2 trên 15 trial.
7. ~~Xác nhận patch `storage.py` đã áp trên máy chạy.~~ **Đã áp vào `main` ngày 18/10** (commit `7a6dfcb`). Máy chạy chỉ cần pull `main`.
8. `08_export_trial_data.py` làm rơi các cạnh `CONFIG_ONLY_UNRESOLVED` (riêng `trial_scope.py` đã xử lý đúng) — kiểm lại với `pair_key` mới khi chạy `08_export` trên VM.
9. Kiểm tra định nghĩa `noise_precision` — vẫn bằng 0 cho cả 3 phương pháp sau khi code lại (18/10, chạy lại 30/10): lát cắt nhiễu không có cạnh ground truth dương nào nên precision luôn 0; cần đổi chỉ số (vd tỷ lệ cạnh nhiễu bị kết luận STRONG).
10. Kiểm tra retention của Prometheus và chụp snapshot.
11. Migration Planner, Validator, Continuity Monitor, orchestration v2, dashboard (theo kế hoạch).
12. Config Collector v2.1 (mục 3.2): nhiều nguồn candidate + regex chung + đọc quy tắc YAML; bước Parse; index tra cứu (kể cả wildcard ServiceEntry); `source_ref`/`extraction_method` trong evidence ref; `item_id` duy nhất; `targetPort` dạng tên; **đọc probe của container** (15/10). Báo cáo Chương 3 đã mô tả thiết kế v2.1 → cần code trước khi chạy đánh giá Collector.
13. Config Collector — chờ Bin quyết: 2 chỗ lệch K8s ở bước merge (`$(VAR)` với envFrom/image ENV); mở rộng redact (`token=`, `apikey=`, `Authorization`); escape giá trị trong `<config_item>`; ghim snapshot model `gpt-4o-mini`.
14. Lưu traffic ở mức series (`traffic_series`, `traffic_daily_counts`) để dashboard hiện bảng và biểu đồ; thêm đường tải lên snapshot.
15. **Tầng 1 — toàn bộ thay đổi chốt 15/10 (mục 3.3). ĐÃ CODE + ĐÃ MERGE 18/10 (PR #1)**; thay đổi 22/10 + 27/10 nằm ở **PR #2 (`tier1-port-gate`), chưa merge — chờ Bin quyết**. Test trên `main` (30/10): tier1 414, orchestration 120, tier2 180, infra 46 — xanh. Còn:
    - ⚠ **thay khóa SSH Azure NGAY** (khóa `infra/KLTN_key.pem` từng nằm trong repo public, vẫn còn trong lịch sử git dù đã gỡ file khỏi code — coi như đã lộ, không chờ việc khác);
    - chạy pytest trên VM;
    - chạy lại `08_export` (+ VictoriaMetrics/Logs nếu dùng) để có file trial mới (port, `traffic_days`, `series_daily_counts`), rồi `09 --validate-selectors` để lấy **số liệu chính thức** (bảng P=1,000/R=0,870/F1=0,930 hiện có vẫn là **tạm**: file trial cũ + validator giả);
    - `10_hysteresis_ablation.py` (chỉ chạy được trên file trial mới);
    - kế hoạch chi tiết theo file, test và cách chạy lại: `claude/TIER1_CHANGE_PLAN.md`.

## 8. Tài liệu đã cũ — đọc kèm file này

| Tài liệu | Chỗ đã cũ |
|---|---|
| `PROPOSAL.md` | Mục 8 ("đề cương dùng 3 tầng") đã sai: đề cương nay dùng 2 tầng. Mục 5 ("Tầng 2 đang thiết kế") đã sai: đã có code. Mục 7: 4 baseline A–D đã thay bằng B1/B2/B3. Mục 9: hạ tầng dùng chung đã có code và đã chạy. Môi trường: là VM Azure, không phải local. |
| `TRAFFIC_COLLECTION_PIPELINE.md` | Ghi "Envoy trong cả ztunnel lẫn waypoint" là sai (ztunnel không phải Envoy). Ghi "lưu SQLite" chưa đủ: đường đánh giá lưu JSON. Header `X-Injected-Test` chưa kiểm chứng. Mục 3 ghi "dùng chung chưa triển khai" đã sai. Nhắc tới "DNS telemetry" là sai: telemetry không có DNS. Chưa có `traffic_days`/`port_source` (15/10), VictoriaMetrics/VictoriaLogs (18/10). |
| `TIER1_DESIGN.md` | Đã cập nhật 15/10, 18/10 (đã code), 22/10 (cổng port, `NOISE_ONLY_REVIEW`) và 27/10 (`port_source = config`). Mục 4 ghi "trọng số học bằng hồi quy logistic": đã thử, giữ trọng số đặt tay (mục 3.3). |
| `infra/README.md` | Ghi môi trường là laptop WSL2: nay chạy trên VM Azure. |
| `CONFIG_EVIDENCE_COLLECTOR.md` | Đã cập nhật lên v2.1 ngày 12/10 (model, môi trường, nhiều nguồn candidate, Parse, truy vết). Các mục đánh dấu 📐 là chưa code. Chưa ghi việc đọc probe (15/10). |
| `claude/DASHBOARD_DESIGN.md` | Đã cập nhật lên v2.1 ngày 12/10 (màn hình Config Collector, Cấu hình Collector, Traffic Collector). Phần thêm 15/10 (cột `port_source`, cờ, danh sách healthcheck/monitoring) mới ghi ở mục 3.8 file này. |
| `claude/MIGRATION_PLANNER_DESIGN.md` | Vẫn đúng. Là nguồn chuẩn cho mục 3.6–3.7. |
| Code trong KB (`tier1_inference/`, `tier2_agent/`) | Đồng bộ từ `main` ngày 30/10 (ghi đè 108 file, thêm 10 file trước đó KB chưa có: `ablation.py`, `cluster_context.py`, `log_collector.py`, `trial_io.py` + 6 file test; xóa `tier1/normalize.py`). `orchestration/` và `infra/` trên KB chưa đồng bộ. Code thay đổi của PR #2 chưa lên KB. |
