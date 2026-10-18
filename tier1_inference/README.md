# Tier 1 — Inference Layer (code)

Implement theo `TIER1_DESIGN.md` (Project KLTN). Đây là tầng suy luận duy nhất trước
Tier 2 (AI Agent): tự kết luận được khi đủ tin cậy (`OBSERVED_STRONG`), còn lại escalate.

## Chạy thử (không cần cluster/Prometheus thật)

```bash
cd tier1_inference
python3 examples/run_example.py
```

In ra kết quả 2 lần chạy (snapshot) trên dữ liệu giả lập phong cách Online Boutique,
cộng phần so sánh minh họa hysteresis (mục 5b). Không cần cài thêm gì — lõi pipeline
chỉ dùng thư viện chuẩn Python. `pip install -r requirements.txt` chỉ cần khi dùng
`scoring.fit_weights_logistic_regression()` hoặc `selector_validation.KubernetesSelectorValidator`.

## Chạy unit test

```bash
cd tier1_inference
pip install pytest pytest-cov   # chỉ cần cho test, lõi pipeline không cần
python3 -m pytest -q                                   # 181 test
python3 -m pytest --cov=tier1 --cov-report=term-missing # kèm coverage (~93%)
```

181 test, phủ toàn bộ 13 module (`schema` gián tiếp qua các module dùng nó). Phần
chưa cover chủ yếu là các lớp gọi API thật cần hạ tầng thật để chạy — không test được
trong môi trường không có cluster/Prometheus: `selector_validation.KubernetesSelectorValidator`,
`config_collector.KubernetesReader`, `traffic_collector.HttpPrometheusClient` (bản
Fake/Mock dùng cho phần còn lại được test đầy đủ) — cùng vài nhánh guard hiếm khi chạy
(`config.py` `to_json`/`from_json`, một số early-return phòng thủ).

## Nguồn dữ liệu traffic: Prometheus hoặc VictoriaMetrics + VictoriaLogs

Từ 18/10/2026 module đọc traffic dùng được stack lưu trữ HA của repo **LOG_METRIC**
(vm-vl-ha-lab: 2 vmagent active-active → vminsert → 3 vmstorage RF=2; vlagent DaemonSet gửi
đôi vào vlogs-a/vlogs-b; một điểm đọc duy nhất là **vmauth**):

| Nguồn | Client | URL ví dụ | Dùng để |
|---|---|---|---|
| Prometheus | `HttpPrometheusClient` | `http://localhost:9090` | bộ đếm Istio (như cũ) |
| VictoriaMetrics | `VictoriaMetricsClient` | `http://vmauth:8427/select/0/prometheus` | cùng PromQL; **từ chối kết quả `isPartial`** (thiếu vmstorage → thiếu cạnh → policy chặn nhầm) trừ khi `allow_partial=True` |
| VictoriaLogs | `log_collector.HttpVictoriaLogsClient` | `http://vmauth:8427` | access log ztunnel → **port thật** của cạnh + cạnh TCP không qua waypoint |

Chọn backend metrics: `make_metrics_client(url, "victoriametrics")` hoặc env
`TRAFFIC_METRICS_BACKEND=victoriametrics`. `collect_traffic(..., log_client=..., context=...)`
gộp 2 nguồn (log thấy đúng 1 port cho cạnh → `port_source = observed_log`; cạnh chỉ thấy qua
log → thêm series TCP mới), rồi `cluster_context` suy port còn thiếu từ Service spec. Script:
`08_export_trial_data.py --metrics-backend victoriametrics --logs-url ...`,
`orchestration.cli_batch --metrics-backend victoriametrics --logs-url ...`.

⚠ Phần đọc VictoriaLogs **chưa kiểm chứng trên cluster thật**: tên field log ztunnel và bộ lọc
luồng `ZTUNNEL_STREAM_FILTER` cần xác nhận trên VM (xem docstring `log_collector.py`).

## Cấu trúc code ↔ mục trong TIER1_DESIGN.md

| File | Mục thiết kế | Nội dung |
|---|---|---|
| `tier1/schema.py` | mục 2, 3, 3b, 5, 8, 10 | Toàn bộ kiểu dữ liệu: Enum trạng thái, `IdentityKey`, `TrafficSeries`, `ConfigEvidenceRecord`, `DependencyEdgeResult` |
| `tier1/config.py` | mục 4, 4a, 5b, 9, 12 | `ScoringConfig` — mọi trọng số/ngưỡng/danh sách, tách khỏi logic (điểm flex #1) |
| `tier1/classification.py` | mục 2.2 | `classify_series()` — thứ tự DNS → MONITORING đã xác minh → CONTROL_PLANE → HEALTHCHECK (3 tầng, so theo segment) → BUSINESS → UNKNOWN; cờ `claimed_monitoring` |
| `tier1/aggregation.py` | mục 2b | Gộp series → cạnh (`identity_key`): hợp `traffic_days`, hợp cờ, `is_noise_only` |
| `tier1/candidates.py` | mục 1, 1b | Gộp theo cặp (`pair_key`, đích chưa phân giải = `("raw", host)`); `match_config_to_edges` — EXACT / PORT_ONLY / PAIR_ONLY / config dư |
| `tier1/scoring.py` | mục 4, 4a | Sigmoid 6 đặc trưng (bỏ `protocol_diversity`), `log_volume` chia `volume_reference` cố định, config 0/1 khi đã fit, `fit_weights_logistic_regression()` |
| `tier1/status.py` | mục 5, 5b | 5 trạng thái, hysteresis bất đối xứng, nhánh NOT_OBSERVED, nhánh cạnh chỉ toàn nhiễu (`derive_status_noise_only`) |
| `tier1/dns_external.py` | mục 8 | Scope: PassthroughCluster/ServiceEntry → service_ref → dấu chấm → UNKNOWN (DNS quyết ở `classification.py`) |
| `tier1/selector_validation.py` | mục 3b | 4 bước kiểm trước khi cho enforce; canonical suy theo thứ tự Istio; đọc Service/Pod 1 lần mỗi namespace |
| `tier1/storage.py` | mục 10b | SQLite WAL, bảng `dependency_evidence`; lịch sử theo đủ 7 phần khóa, chỉ snapshot TRƯỚC; `snapshot_time` |
| `tier1/pipeline.py` | toàn bộ | `run_tier1()` + lõi dùng chung với baseline (`prepare_snapshot`, `build_observed_result`, `build_potential_only_results`) |
| `tier1/cluster_context.py` | mục 2.1, 2.2, 8 | Đọc cluster 1 lần: port Service, path probe, danh tính monitoring (Prometheus/vmagent), host ServiceEntry → `annotate_series` |
| `tier1/log_collector.py` | mục 2.1b | VictoriaLogs: access log ztunnel → series TCP có port thật; `merge_log_series` |
| `tier1/trial_io.py` | — | Đọc file trial JSON (cả bản cũ trước 15/10: port 0 → None) |
| `tier1/ablation.py` | mục 13.4 | Ablation hysteresis 7 snapshot cắt từ 7 ngày (`infra/scripts/10_hysteresis_ablation.py`) |
| `tier1/config_collector/` | `CONFIG_EVIDENCE_COLLECTOR.md` | Collector v2 (LLM + xác thực tất định); 15/10 đọc thêm probe (`ProbeInfo`) |
| `tier1/traffic_collector.py` | `TRAFFIC_COLLECTION_PIPELINE.md` mục 1-6 | PromQL (`increase()`, scrape gap cả cửa sổ), 7 query theo ngày cho CẢ HTTP lẫn TCP, port None khi thiếu nhãn, `daily_counts`; client Prometheus / VictoriaMetrics |
| `tier1/baseline.py` | mục 13.1, 13.2 | `run_baseline_b1()` (naive presence rule, baseline đối ngoại) / `run_baseline_b2()` (ngưỡng cứng cũ, ablation nội bộ) — tái dùng nguyên vẹn candidate discovery + nhánh NOT_OBSERVED của `pipeline.py`, chỉ thay quyết định cho cặp có traffic |
| `tier1/evaluation.py` | mục 13.3 | `precision_recall_f1()`, `filter_ground_truth()` (2 lát cắt: traffic thưa, HEALTHCHECK/MONITORING), `mean_ci95()` (phân phối t), `wilcoxon_signed_rank()` (kiểm định bắt cặp) |
| `tier1/experiment.py` | mục 13.3, `PROPOSAL.md` mục 7 | `run_experiment()` — chạy Tier 1 thật + B1 + B2 trên đúng cùng dữ liệu mỗi trial, tổng hợp `mean ± CI 95%` + Wilcoxon qua R trial độc lập |
| `examples/simulate_data.py` | — | Dữ liệu giả lập Online Boutique, phủ đủ 5 trạng thái + DNS + EXTERNAL + NEEDS_MANUAL_SELECTOR |
| `examples/run_example.py` | — | Chạy 2 snapshot liên tiếp, in báo cáo, demo hysteresis |

## 3 điểm "flex" — căn cứ cho phần mở rộng sau này (RBAC/PSS/Kyverno...)

Xuất phát từ kết luận đã thống nhất khi thiết kế Tier 2: Tier 1 + Tier 2 đã tạo ra một
**dependency graph domain-neutral** (không gắn cứng khái niệm NetworkPolicy) — việc sinh
NetworkPolicy là việc của một consumer riêng (Migration Planner) đọc `DependencyEdgeResult`,
không phải một phần của Tier 1. 3 điểm sau là nơi cụ thể trong CODE thể hiện điều đó:

1. **`config.py` tách khỏi logic** — toàn bộ trọng số scoring, ngưỡng hysteresis,
   danh sách healthcheck/monitoring/control-plane nằm trong một `ScoringConfig` có thể
   load từ JSON (`ScoringConfig.from_json`). Áp dụng Tier 1 cho một domain khác (ví dụ
   audit log RBAC) chỉ cần một bộ config khác, không sửa `scoring.py`/`status.py`.

2. **`schema.py::DependencyEdgeResult` domain-neutral** — không có field nào tên
   `network_policy`, `podSelector`... Kết quả Tier 1 luôn là "cặp (A,B) phụ thuộc nhau
   ở mức tin cậy nào" — trung lập với loại policy sẽ sinh ra sau đó.

3. **`selector_validation.py::SelectorValidator` là một `Protocol` pluggable** —
   `MockSelectorValidator` (demo) và `KubernetesSelectorValidator` (thật, dùng lib
   `kubernetes`) cùng cắm vào `run_tier1(..., selector_validator=...)`. Đây là điểm
   trong pipeline gắn với NetworkPolicy nhiều nhất (mục 3b: podSelector/overlap check) —
   tách nó ra một interface riêng nghĩa là khi thêm domain khác (ví dụ RBAC cần validate
   ServiceAccount/Role thay vì podSelector), chỉ cần viết một `Validator` mới, không đụng
   vào `pipeline.py`.

**Không** xây một "plugin framework" tổng quát (kiểu DeepSeek Harness) ở giai đoạn này —
quyết định này đã thống nhất khi thảo luận thiết kế Tier 2: chưa có domain thứ hai thật để
validate bất kỳ interface plugin nào, xây trước sẽ là over-engineering. 3 điểm trên đủ để
trình bày "kiến trúc đã chuẩn bị sẵn chỗ nối", không phải "đã tổng quát hóa".

## Vài quyết định thiết kế KHÔNG có trong TIER1_DESIGN.md (chọn khi code, cần biết khi bảo vệ)

Tài liệu thiết kế mô tả pseudocode/công thức ở mức khái niệm; vài chỗ code phải tự quyết
định con số/rule cụ thể vì tài liệu để ngỏ. Liệt kê minh bạch để không bị hỏi bất ngờ:

- **`config_match_strength`** (mục 4a): tài liệu nói `config_finding` là nhãn nhị phân,
  không phải xác suất. Việc lượng hóa `FOUND_STRUCTURAL/EMBEDDED → 1.0`,
  `FOUND_UNRESOLVED → 0.3`, `NOT_FOUND → 0.0` để nhét vào sigmoid là lựa chọn của Tier 1
  code, xem `scoring.py::CONFIG_MATCH_STRENGTH`.
- **Ngưỡng "traffic_class mâu thuẫn/không rõ" → UNKNOWN** (mục 5): implement bằng
  `unknown_class_ratio_threshold` (mặc định 0.5) — tỷ trọng traffic không khớp rule phân
  loại nào vượt ngưỡng này thì cả edge bị đánh UNKNOWN. Xem `status.py::is_class_ambiguous`.
- **`scope` IN_CLUSTER/EXTERNAL/UNKNOWN cho traffic** (mục 8): PassthroughCluster/
  BlackHoleCluster hoặc host ServiceEntry → EXTERNAL; có `destination_service_ref` →
  IN_CLUSTER; tên có dấu chấm → EXTERNAL; còn lại → UNKNOWN. Xem `dns_external.py::classify_scope`.
- **Selector validation (mục 3b) không áp dụng cho scope=EXTERNAL** — vì mục 8 nói rõ
  EXTERNAL sinh `ServiceEntry`, không phải NetworkPolicy/podSelector — validate nhầm sẽ
  luôn fail oan (không có `destination_service_ref`). scope=UNKNOWN vẫn cho qua validate
  (fail đúng nghĩa, cần người xem).
- **HEALTHCHECK** (mục 2.2, chốt 15/10): so theo segment cuối / nguyên văn, không theo tiền
  tố; gRPC chỉ `grpc.health.v1.Health`. **gRPC reflection KHÔNG nằm mặc định**: harness thí
  nghiệm sinh traffic dependency cấy bằng `grpcurl` qua reflection (`render_manifests.py`), nên
  trong thí nghiệm reflection là traffic thật — gắn nó là healthcheck làm recall 15 trial tụt
  0,852 → 0,789 (đo 18/10). Môi trường thật thêm vào `healthcheck_extra_paths` nếu muốn.
- **`EXPERIMENT_CONFIG`** (phương án a): khai `*/prometheus`, `*/grafana`, `*/blackbox-exporter`
  là monitoring đã xác minh — đúng SA của harness nhiễu monitoring. Đánh giá (`experiment.py`) và
  Batch Orchestrator dùng config này; `DEFAULT_CONFIG` chỉ tin `istio-system/prometheus`.
- **`previous_status` lưu là status SAU CÙNG (post-validation)** — nếu một edge từng đạt
  STRONG về điểm số nhưng bị hạ xuống `NEEDS_MANUAL_SELECTOR` do validation mục 3b, lần
  chạy sau `previous_status` đọc được là `NEEDS_MANUAL_SELECTOR`, không phải `STRONG` —
  nghĩa là hysteresis (mục 5b, vốn chỉ nói về `OBSERVED_STRONG`) coi edge đó "chưa từng
  STRONG" và đòi hỏi lại `score >= theta_promote` để xét enforce, dù bản chất traffic vẫn
  mạnh suốt. Đây là lựa chọn có chủ đích (an toàn hơn: một vấn đề selector chưa được người
  xử lý không nên tự động "quên đi" chỉ vì traffic vẫn cao), không phải bug.

## Ranh giới của 2 module collector (`config_collector.py`, `traffic_collector.py`)

Cả hai đọc/viết đúng schema mà `pipeline.py` đã tiêu thụ từ trước (`ConfigEvidenceRecord`,
`TrafficSeries`) — không đổi gì ở tầng suy luận. Cả hai đều chạy được **không cần cluster/
Prometheus thật** qua bản Fake (`FakeK8sReader`, `FakePrometheusClient`, xem
`tests/test_config_collector.py`, `tests/test_traffic_collector.py`), nhưng bản THẬT
(`KubernetesReader`, `HttpPrometheusClient`) cần 2 điều kiện hạ tầng chưa có trong môi
trường demo này, ghi rõ trong docstring từng file:

- `traffic_collector.py` giả định Istio Telemetry API đã được cấu hình để export 2 nhãn
  tùy chỉnh (`test_harness` cho provenance mục 3, `request_path` cho phân loại HEALTHCHECK
  mục 2 TIER1_DESIGN.md) — Istio KHÔNG tự phơi 2 nhãn này ra `istio_requests_total` theo
  mặc định. Thiếu nhãn không làm pipeline lỗi (`is_injected=False`/`path=None`), chỉ mất
  tín hiệu tương ứng.
- `config_collector.py` không đọc `Secret` (tránh RBAC đọc Secret cluster-wide, đúng
  quyết định đã chốt mục 3 tài liệu) — dependency chỉ khai qua Secret sẽ rơi vào `NOT_FOUND`,
  không gây hại (nguyên tắc open-world).

## Ngoài phạm vi (khớp mục 12 TIER1_DESIGN.md, mục 8 CONFIG_EVIDENCE_COLLECTOR.md)

Sinh NetworkPolicy YAML thật (việc của Migration Planner, tiêu thụ `DependencyEdgeResult`),
huấn luyện trọng số bằng dữ liệu thật (`fit_weights_logistic_regression` đã viết sẵn nhưng
chưa gọi), `Secret`/`NetworkPolicy` hiện có/`Ingress`/headless Service làm nguồn bằng chứng
config (mục 3 `CONFIG_EVIDENCE_COLLECTOR.md`), DNS traffic collection (xử lý riêng, ngoài
2 module này — xem mục 7 TIER1_DESIGN.md).
