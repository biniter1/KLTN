# Kế hoạch sửa code Tầng 1 — chốt 15/10/2026

Tổng hợp **mọi thay đổi Tầng 1 đã chốt ngày 15/10** (thiết kế chi tiết ở `TIER1_DESIGN.md`, trạng thái ở `claude/TRANG_THAI_CHOT.md` mục 3.3 và việc 15). File này trả lời: sửa file nào, sửa gì, làm theo thứ tự nào, test nào phải sửa, chạy lại ra sao.

**Cập nhật 18/10/2026 — ĐÃ CODE giai đoạn A–F + G1–G3** trên nhánh `tier1-refactor-victoria`, PR https://github.com/biniter1/KLTN/pull/1 — **đã merge vào `main` ngày 18/10/2026** (merge commit `fee1bd9`). Kèm tích hợp VictoriaMetrics + VictoriaLogs (repo LOG_METRIC) và thêm lỗi **L11** (series TCP không có query theo ngày). Bước B3 nằm trong `schema.normalize_path` + `cluster_context.py` (không có file `normalize.py` riêng). Còn lại: G2 (chạy lại 08/09/10 trên VM), G4 (fit), G5 (bảng trước/sau chính thức).

**Cập nhật 30/10/2026:**
- Commit `cac079f` (thêm `tier1/normalize.py` + `infra/experiment_report_rerun.json` từ một bản code làm lại ngoài `main`) đã được revert bằng commit `50a99bd` (`git revert`, không force push). `experiment_report_rerun.json` (P 0,941 / R 0,822) **không** được chạy từ code trên `main` — không dùng số đó.
- Code trong KB (`tier1_inference/`, `tier2_agent/`) đã được ghi đè lại bằng bản `main`.
- Test trên `main` (30/10): tier1 414, orchestration 120, tier2 180, infra 46 — xanh hết.
- PR #2 (`tier1-port-gate`, chưa merge): cổng port ở cửa ra (22/10), `NOISE_ONLY_REVIEW` (22/10), `port_source = config` (27/10). Trên nhánh này: tier1 424, orchestration 134.

**Kết quả chạy lại trên `main` ngày 30/10/2026** (commit `50a99bd`, cây mã giống hệt `fee1bd9`; 15 file trial CŨ xuất 08/10 — chưa có port/ngày; validator selector giả; `EXPERIMENT_CONFIG`) — **chưa phải số chính thức**, khớp tuyệt đối lần chạy sau PR #1:

| | Precision | Recall | F1 | Recall nhóm thưa |
|---|---|---|---|---|
| Tầng 1 | 1,000 ± 0,000 | 0,870 ± 0,015 | 0,930 ± 0,009 | 0,378 ± 0,137 |
| B1 | 0,889 ± 0,000 | 0,889 ± 0,000 | 0,889 ± 0,000 | 0,533 ± 0,038 |
| B2 | 0,885 ± 0,008 | 0,822 ± 0,013 | 0,852 ± 0,009 | 0,000 ± 0,000 |

Wilcoxon bắt cặp: Tầng 1 vs B1 — recall p = 0,025 (Tầng 1 **thấp hơn** B1), precision p = 0,0001 (Tầng 1 cao hơn); Tầng 1 vs B2 — recall p = 0,002, precision p = 0,0004 (Tầng 1 cao hơn cả hai). Ablation hysteresis (`10_hysteresis_ablation.py`) **không chạy được** trên file cũ vì thiếu `series_daily_counts` — cần chạy lại `08_export` trên VM. (Trước khi code lại: Tầng 1 0,888 / 0,852 / 0,869, recall thưa 0,256.)

**Điều chỉnh khi code (18/10):**
- gRPC reflection **bỏ khỏi** danh sách healthcheck mặc định: harness thí nghiệm sinh traffic dependency cấy bằng `grpcurl` qua reflection → trong thí nghiệm reflection là traffic thật; giữ trong danh sách làm recall tụt 0,852 → 0,789. Môi trường thật thêm qua `healthcheck_extra_paths`.
- `EXPERIMENT_CONFIG` (phương án a) khai cả 3 nguồn nhiễu monitoring của harness: `*/prometheus`, `*/grafana`, `*/blackbox-exporter`. `DEFAULT_CONFIG` chỉ tin `istio-system/prometheus` (+ tự phát hiện Prometheus/vmagent từ cluster).
- Đích config chưa phân giải ghi `destination_canonical_service = "raw:<host>"` (namespace đích None) để mỗi host là 1 dòng riêng cả trong SQLite và Tier 2.
- Batch Orchestrator giữ lịch sử theo `snapshot_id = attempt_id` (không truyền `snapshot_time`).
- Ablation 7 snapshot dùng dữ liệu cộng dồn ngày 1..k nên điểm thường tăng dần → số lần đổi trạng thái đo được sẽ thấp; ghi là giới hạn trong báo cáo.

Ký hiệu: ⚠ = lỗi thật trong code hiện tại · 📐 = thay đổi thiết kế · cỡ việc S (nhỏ, vài chục dòng) / M (một file, logic mới) / L (nhiều file hoặc cần dữ liệu cluster).

Thuật ngữ (dùng thống nhất): **bộ đếm** (counter Prometheus, 1 tổ hợp nhãn) → **series** (1 API giữa 2 service: nguồn, đích, protocol, port, path) → **cạnh** (1 luật mạng: nguồn, SA, đích, protocol, port — `identity_key`) → **cặp** (chỗ hẹn với config: nguồn, đích — `pair_key`).

---

## 1. Danh sách lỗi thật (⚠)

| # | Lỗi | File | Hậu quả | Cỡ |
|---|---|---|---|---|
| L1 | Kiểm selector bước 4 đọc nhãn `security.istio.io/canonical-name` (sai tên; pod thường không gắn nhãn canonical) | `selector_validation.py` | Trên cluster thật **mọi cạnh STRONG → NEEDS_MANUAL_SELECTOR**. Chưa lộ vì đánh giá dùng validator giả | S |
| L2 | Tra `previous_status` chỉ so 6/7 phần khóa (thiếu SA nguồn) | `storage.py` | 2 cạnh chỉ khác SA dùng chung lịch sử hysteresis | S |
| L3 | Tra `previous_status` không loại dòng cùng `snapshot_id` | `storage.py` | Chạy lại cùng snapshot → đọc nhầm kết quả của chính nó → kết quả khác lần đầu | S |
| L4 | Config UNRESOLVED có `pair_key = (ns, svc, None, None)` | `schema.py` | Mọi đích chưa phân giải của 1 nguồn dồn vào 1 ô → chỉ 1 đích ra kết quả, các đích khác **mất** | S |
| L5 | Config ở port không có traffic, trong cặp đã có traffic, không sinh kết quả | `pipeline.py` | Mất dependency nhánh hiếm (vd redis sentinel 26379) — hại recall | M |
| L6 | Cạnh chỉ toàn nhiễu (healthcheck/monitoring) → OBSERVED_WEAK → escalate Tier 2 | `status.py`, `pipeline.py` | Tốn chi phí AI cho cạnh đã biết là nhiễu | S |
| L7 | Thứ tự luật phân loại: MONITORING đứng sau CONTROL_PLANE và HEALTHCHECK | `classification.py` | Prometheus thật (ở `istio-system`, gọi `/metrics`) không bao giờ được gắn MONITORING | S |
| L8 | `distinct_days` khi gộp cạnh lấy max thay vì hợp ngày | `aggregation.py`, `traffic_collector.py` | Cạnh nhiều path thưa bị tính thiếu ngày → hại recall nhóm thưa | M |
| L9 | Port traffic luôn = 0 (nhãn `destination_port` vắng) | `traffic_collector.py` | Không khớp port với config; policy không hẹp được tới port | M |
| L10 | `protocol_diversity` luôn = 0,5 (protocol nằm trong khóa cạnh) | `scoring.py` | Đặc trưng vô nghĩa | S |
| L11 | Series TCP không có query theo ngày (phát hiện 18/10) | `traffic_collector.py` | `distinct_days = 0`, `last_seen = 9999` → cạnh TCP (redis...) luôn bị chấm thấp | S |

## 2. Thứ tự làm (giai đoạn A → G)

Nguyên tắc: sửa lỗi độc lập trước; thay đổi **dữ liệu vào** trước thay đổi **logic dùng dữ liệu đó**; chấm điểm sau cùng vì phụ thuộc mọi thứ phía trên; fit trọng số chỉ làm sau khi xong A–F.

### A. Lỗi độc lập, sửa ngay (không phụ thuộc gì) — cỡ tổng S

| Việc | File | Nội dung |
|---|---|---|
| A1 (L1) | `selector_validation.py` | Bước 4: suy canonical của pod theo thứ tự Istio `service.istio.io/canonical-name` → `app.kubernetes.io/name` → `app` → tên workload; tách hàm dùng chung với Config Collector (`config_collector/identity.py`) |
| A2 (L2, L3) | `storage.py` | Điều kiện tra lịch sử thêm `source_service_account`; chỉ lấy dòng có `snapshot_id` khác snapshot hiện tại và `snapshot_time` < snapshot hiện tại; index thêm SA |
| A3 (L4) | `schema.py` | `ConfigEvidenceRecord.pair_key()`: đích chưa phân giải → `(ns, svc, "raw", raw_host)` |
| A4 (L6) | `status.py`, `pipeline.py` | Nhánh cạnh chỉ toàn nhiễu (không series BUSINESS): monitoring đã xác minh → `auto_allow_reason = VERIFIED_MONITORING`; còn lại → `needs_human_review`, không escalate. Trước khi có danh tính xác minh (giai đoạn C), tạm coi mọi cạnh toàn nhiễu → người duyệt |

Kiểm sau A: `trial_scope.py` và `08_export_trial_data.py` vẫn xử lý đúng `pair_key` mới (việc 8 backlog: `08_export` đang làm rơi `CONFIG_ONLY_UNRESOLVED`).

### B. Dữ liệu vào: series mới — cỡ M–L

| Việc | File | Nội dung |
|---|---|---|
| B1 (L8) | `traffic_collector.py` | Giữ tập ngày có traffic thay vì chỉ đếm; ghi số request từng ngày vào bảng `traffic_daily_counts` (backlog 14, có thể tách làm sau) |
| B2 | `schema.py` | `TrafficSeries`: `port: int \| None`, `port_source`, `traffic_days: list[str]`, `distinct_days = len(traffic_days)`, `flags: set[str]`. Tương thích file JSON cũ: thiếu `traffic_days` → giữ `distinct_days` cũ, cờ `legacy_days` |
| B3 (L9) | mới: `tier1/normalize.py` | Chuẩn hóa trước phân loại: `path_norm` + segments (viết thường, cắt query, bỏ `/` cuối); suy port từ Service spec qua `destination_service_ref` (1 port → `inferred_single_port`; nhiều port → khớp `appProtocol`/tên `grpc-*`/`http-*` → `inferred_protocol_match`; không được → `None` + `unknown`). Dùng index cluster của Collector (`validate.build_cluster_snapshot`) |
| B4 | vận hành | Thêm `tagOverrides` `destination_port` vào Telemetry **chỉ sau khi chụp snapshot Prometheus** (backlog 10) |

### C. Phân loại — cỡ M (C3 cần sửa Collector)

| Việc | File | Nội dung |
|---|---|---|
| C1 (L7) | `classification.py` | Thứ tự mới: DNS → MONITORING (đã xác minh) → CONTROL_PLANE (nguồn **hoặc** đích) → gắn cờ `claimed_monitoring` nếu chỉ trùng tên → HEALTHCHECK → BUSINESS → UNKNOWN |
| C2 | `classification.py`, `config.py` | DNS theo `destination_service_ref ∈ dns_services` (mặc định `kube-system/kube-dns`) hoặc port 53. MONITORING theo `(namespace, SA)` ∈ `verified_monitoring`; tập này lập mỗi lần chạy: tự phát hiện từ `prometheus_url` (Service → selector → pod → ns + SA) + label `app.kubernetes.io/name=prometheus` + khai báo trong config (thí nghiệm khai SA `prometheus` của harness — phương án a) |
| C3 | `config_collector/k8s_types.py`, `collect.py` | Collector đọc thêm `livenessProbe`/`readinessProbe`/`startupProbe` (`httpGet.path`, `grpc`) của từng container, xuất ra `probe_paths[workload]` cho Tầng 1 |
| C4 | `classification.py`, `config.py` | HEALTHCHECK 3 tầng: path từ probe của **đúng service đích** (so `path_norm`) · danh sách mặc định ngắn (`/health`, `/healthz`, `/readyz`, `/livez`, `/ready`, `/live`, `/metrics`; gRPC `grpc.health.v1.Health`; **gRPC reflection đã bỏ khỏi danh sách mặc định 18/10**, xem "Điều chỉnh khi code") · đội vận hành tự thêm (YAML). Tầng 2/3 so theo **segment cuối** hoặc bằng nguyên văn; gRPC so tên service (segment đầu) |

### D. Gộp cạnh và khớp config — cỡ M

| Việc | File | Nội dung |
|---|---|---|
| D1 (L8) | `aggregation.py` | Hợp `traffic_days`, `distinct_days = len(...)`; hợp `flags` |
| D2 (L5) | `candidates.py`, `pipeline.py` (`_pick_config_evidence`) | Mỗi bản ghi config (sắp theo `item_id`) so với các cạnh của cặp: EXACT (cùng port, protocol trùng hoặc config không ghi) / PORT_ONLY (cùng port, lệch protocol → cờ `protocol_mismatch`) / PAIR_ONLY (không so được port) / **config dư** (port đã biết, mọi cạnh có port đã biết, không trùng) → kết quả NOT_OBSERVED riêng cho port đó |
| D3 | `pipeline.py` | Mỗi cạnh chọn 1 config: mức khớp > finding sạch > `item_id` nhỏ nhất; so SA → cờ `sa_mismatch`; ghi `config_match_level`. Cặp chỉ config → 1 kết quả NOT_OBSERVED **mỗi port** |

### E. Chấm điểm — cỡ S

| Việc | File | Nội dung |
|---|---|---|
| E1 (L10) | `scoring.py`, `config.py` | Bỏ `protocol_diversity`; bias −1,2 → −1,0 (điểm không đổi về toán) |
| E2 | `scoring.py`, `config.py`, `pipeline.py` | `log_volume` chia `volume_reference` cố định trong config (lấy 1 lần = phân vị 95 count các cạnh của snapshot đầu, rồi đóng băng); bỏ tính `EXPECTED_MAX` theo snapshot |
| E3 | `scoring.py::fit_weights_logistic_regression` | Bỏ cột `protocol_diversity`; config thành 3 cột 0/1 `is_structural`, `is_embedded`, `is_unresolved` |
| — | `config.py` | `count_floor` giữ `None` (chốt không dùng). Mọi ngưỡng giữ giá trị khởi tạo cho tới lúc chạy đánh giá |

### F. Scope, DNS, lưu trữ — cỡ S–M

| Việc | File | Nội dung |
|---|---|---|
| F1 | `dns_external.py` | `classify_scope`: PassthroughCluster/BlackHoleCluster → EXTERNAL (`external_unregistered`) → host khớp ServiceEntry (index Collector) → service_ref → dấu chấm → UNKNOWN. Cần `TrafficSeries` mang thêm `destination_service_name` thô |
| F2 | `pipeline.py`, `dns_external.py` | Bỏ `is_dns_edge`; tách DNS theo `traffic_class == DNS` |
| F3 | `storage.py`, `schema.py` | Cột mới: `snapshot_time`, `port_source`, `traffic_days` (JSON), `flags` (JSON), `config_match_level`, `auto_allow_reason`; đếm số lần đổi trạng thái theo `snapshot_time`. Đối chiếu patch `CHANGES_existing_modules.patch` (backlog 7) trước khi sửa `storage.py` |
| F4 | `selector_validation.py` | Đọc Service + Pod 1 lần mỗi namespace mỗi lần kiểm, dùng chung cho mọi cạnh |

### G. Đánh giá và chạy lại — cỡ M

| Việc | Nội dung |
|---|---|
| G1 | Đường đánh giá dùng `KubernetesSelectorValidator` thật (chỉ đọc) thay `MockSelectorValidator` |
| G2 | Chạy lại `08_export_trial_data.py` (query lại snapshot Prometheus, **không cần thu lại 7 ngày**) → có `traffic_days`, port suy ra; rồi `09` |
| G3 | Script ablation hysteresis: 7 snapshot cắt từ 7 ngày (snapshot k = ngày 1..k), chạy đường có SQLite, 1–2 trial, đo số lần đổi trạng thái |
| G4 | Fit trọng số (backlog 4) → chọn θ qua ROC trên tập fit, θ_promote/θ_demote = θ ± 0,05 → báo cáo trên tập còn lại; chỉ số chính recall Tầng 1 + Tầng 2 |
| G5 | Bảng so sánh trước/sau sửa (precision, recall, recall nhóm thưa, số cạnh gọi Tier 2) — để báo cáo giải thích được mỗi thay đổi đóng góp gì |

### Phụ thuộc

```
A (lỗi độc lập) ──────────────────────────────────────────┐
B (series mới) ──► C (phân loại) ──► D (gộp + khớp) ──► E (chấm điểm) ──► G
                     ▲ C3 cần Collector đọc probe          F (scope/lưu trữ) ─┘
```

## 3. Test phải sửa / thêm

| File test | Vì sao |
|---|---|
| `tests/conftest.py` | `make_series` / `make_identity_key`: `port` cho phép `None`, thêm `traffic_days`, `port_source`, `flags` |
| `test_classification.py` | Đang kiểm thứ tự cũ (DNS > CONTROL_PLANE > HEALTHCHECK > MONITORING; "prometheus + /health → HEALTHCHECK") — viết lại theo thứ tự mới; thêm test `claimed_monitoring`, healthcheck theo segment (`/cart/123/status` ✗, `/api/v1/healthz` ✓, `/livestream` ✗), probe path |
| `test_aggregation.py` | Hợp ngày (2 series khác ngày → 2), file cũ thiếu `traffic_days` |
| `test_candidates.py` | `raw:host` tách ô; config dư → NOT_OBSERVED riêng; EXACT/PORT_ONLY/PAIR_ONLY; `sa_mismatch` |
| `test_scoring.py` | Bỏ `protocol_diversity`; `volume_reference` cố định (thêm cạnh lớn không đổi điểm cạnh khác) |
| `test_status.py` | Nhánh cạnh chỉ toàn nhiễu không escalate |
| `test_storage.py` | Lịch sử theo đủ 7 phần khóa; chạy lại cùng snapshot ra cùng kết quả |
| `test_selector_validation.py` | Pod không có nhãn canonical tường minh vẫn qua bước 4 (suy từ `app`) |
| `test_dns_external.py` | Scope PassthroughCluster/ServiceEntry |
| `test_pipeline.py` | End-to-end với ví dụ 6 cạnh + 2 config dư trong `TIER1_DESIGN.md` mục 1b |

## 4. Ảnh hưởng tới báo cáo

- Số liệu `experiment_report.json` hiện có được tính **trước** các sửa đổi này, với ngưỡng đơn 0,68 (đường đánh giá không chạy hysteresis), validator giả, port = 0, `distinct_days` lấy max. Báo cáo phải dùng số liệu **sau G2** hoặc ghi rõ phiên bản.
- Không hứa trước chiều hướng thay đổi của recall/precision — chỉ báo cáo sau khi chạy G5.
- Các lỗi L1–L10 là nội dung tốt cho phần "phân tích và cải tiến" của báo cáo: mỗi lỗi có nguyên nhân, cách phát hiện (đối chiếu code với dữ liệu thật), cách sửa.
