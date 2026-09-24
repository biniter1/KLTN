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
python3 -m pytest -q                                   # 95 test
python3 -m pytest --cov=tier1 --cov-report=term-missing # kèm coverage (~90%)
```

95 test, phủ toàn bộ 8 module lõi (`schema` gián tiếp qua các module dùng nó). Phần
chưa cover chủ yếu là `selector_validation.KubernetesSelectorValidator` (cần cluster
K8s thật để chạy, không test được trong môi trường không có cluster — `MockSelectorValidator`
dùng cho phần còn lại được test đầy đủ) và vài nhánh guard hiếm khi chạy (`config.py`
`to_json`/`from_json`, một số early-return phòng thủ).

## Cấu trúc code ↔ mục trong TIER1_DESIGN.md

| File | Mục thiết kế | Nội dung |
|---|---|---|
| `tier1/schema.py` | mục 2, 3, 3b, 5, 8, 10 | Toàn bộ kiểu dữ liệu: Enum trạng thái, `IdentityKey`, `TrafficSeries`, `ConfigEvidenceRecord`, `DependencyEdgeResult` |
| `tier1/config.py` | mục 4, 4a, 5b, 9, 12 | `ScoringConfig` — mọi trọng số/ngưỡng/danh sách, tách khỏi logic (điểm flex #1) |
| `tier1/classification.py` | mục 2 | `classify_series()` — traffic_class theo rule, chạy ở cấp series |
| `tier1/aggregation.py` | (chuẩn bị dữ liệu, không đặt tên riêng trong tài liệu) | Gộp series → `TrafficAggregate` theo `identity_key` |
| `tier1/candidates.py` | mục 1 | `discover_candidates()` — C = C_observed ∪ C_potential |
| `tier1/scoring.py` | mục 4, 4a | Công thức sigmoid, `percentile_95` (EXPECTED_MAX), `fit_weights_logistic_regression()` (chưa dùng — cần ground truth thật) |
| `tier1/status.py` | mục 5, 5b | 5-state, hysteresis bất đối xứng, nhánh NOT_OBSERVED tách biệt khỏi sigmoid |
| `tier1/dns_external.py` | mục 7, 8 | DNS auto-allow, phân loại scope IN_CLUSTER/EXTERNAL/UNKNOWN |
| `tier1/selector_validation.py` | mục 3b | 4 bước validate trước khi cho enforce; `SelectorValidator` pluggable (điểm flex #3) |
| `tier1/storage.py` | mục 10, 11 (TIER2_DESIGN.md) | SQLite WAL, bảng `dependency_evidence`, previous_status cho hysteresis |
| `tier1/pipeline.py` | toàn bộ | `run_tier1()` — nối các bước theo đúng thứ tự |
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
- **`scope` IN_CLUSTER/EXTERNAL/UNKNOWN cho traffic** (mục 8): có `destination_service_ref`
  → IN_CLUSTER; không có + tên có dấu chấm (giống FQDN) → EXTERNAL; còn lại → UNKNOWN.
  Xem `dns_external.py::classify_scope`.
- **Selector validation (mục 3b) không áp dụng cho scope=EXTERNAL** — vì mục 8 nói rõ
  EXTERNAL sinh `ServiceEntry`, không phải NetworkPolicy/podSelector — validate nhầm sẽ
  luôn fail oan (không có `destination_service_ref`). scope=UNKNOWN vẫn cho qua validate
  (fail đúng nghĩa, cần người xem).
- **HEALTHCHECK path rule mở rộng cho gRPC** (mục 2): prefix `/health*` trong tài liệu chỉ
  bắt path kiểu REST. gRPC health-check chuẩn (dùng trong Online Boutique và hầu hết dịch vụ
  gRPC) có path đầy đủ dạng `/grpc.health.v1.Health/Check`, không khớp prefix nào — thêm rule
  match theo substring `"grpc.health"` (`config.py::healthcheck_path_contains`) để bắt đúng.
  Phát hiện ra nhờ chính unit test (`tests/test_aggregation.py`) khi dữ liệu giả lập dùng path
  gRPC thật không được phân loại như kỳ vọng — đúng tinh thần mục 2 "heuristic ban đầu, tinh
  chỉnh khi có dữ liệu thật".
- **`previous_status` lưu là status SAU CÙNG (post-validation)** — nếu một edge từng đạt
  STRONG về điểm số nhưng bị hạ xuống `NEEDS_MANUAL_SELECTOR` do validation mục 3b, lần
  chạy sau `previous_status` đọc được là `NEEDS_MANUAL_SELECTOR`, không phải `STRONG` —
  nghĩa là hysteresis (mục 5b, vốn chỉ nói về `OBSERVED_STRONG`) coi edge đó "chưa từng
  STRONG" và đòi hỏi lại `score >= theta_promote` để xét enforce, dù bản chất traffic vẫn
  mạnh suốt. Đây là lựa chọn có chủ đích (an toàn hơn: một vấn đề selector chưa được người
  xử lý không nên tự động "quên đi" chỉ vì traffic vẫn cao), không phải bug.

## Ngoài phạm vi (khớp mục 12 TIER1_DESIGN.md)

Không có ở đây (và không nên có ở giai đoạn này): identity resolution thật từ K8s API
(giả định traffic/config evidence đưa vào đã được collector resolve sẵn — xem
`TRAFFIC_COLLECTION_PIPELINE.md`, `CONFIG_EVIDENCE_COLLECTOR.md`), sinh NetworkPolicy YAML
thật (việc của Migration Planner, tiêu thụ `DependencyEdgeResult`), huấn luyện trọng số
bằng dữ liệu thật (`fit_weights_logistic_regression` đã viết sẵn nhưng chưa gọi).
