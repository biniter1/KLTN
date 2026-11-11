# Thiết kế chi tiết — Tier 1 (Inference Layer)

Tài liệu này thay thế `TIER1_DESIGN.md` cũ. Hai thay đổi so với bản trước:

1. **Kiến trúc tổng đổi từ 3 tầng sang 2 tầng**: "Tier 2" cũ (bằng chứng cấu hình) không có khả năng tự kết luận, nên được đổi thành module **Config Evidence Collector** — không còn là một tier, chỉ là nguồn input cho Tier 1. "Tier 3" cũ (AI Agent) đổi số thành **Tier 2**, giờ là tầng suy luận thứ hai và cuối cùng.
2. **Tách cơ chế thu thập dữ liệu ra khỏi tài liệu này**: cách vật lý thu thập traffic thật (Envoy, Prometheus, xử lý counter, provenance test) nay nằm ở `TRAFFIC_COLLECTION_PIPELINE.md`. Cách đọc/giải mã cấu hình K8s nằm ở `CONFIG_EVIDENCE_COLLECTOR.md`. Tài liệu này chỉ còn **logic suy luận**: candidate discovery, phân loại, định danh, scoring, trạng thái đầu ra, escalation.

**Cập nhật 29/09/2026**: `CONFIG_EVIDENCE_COLLECTOR.md` chuyển sang v2 (thu thập rộng hơn → LLM trích xuất → xác thực tất định với K8s/Istio API). Giao diện ra Tier 1 **giữ nguyên** (nhãn `config_finding`, `identity_key`, đổ vào `C_potential`).

**Cập nhật 22/10/2026**: cổng port ở cửa ra (dòng 23, mục 6) — nhánh `tier1-port-gate`.

**Cập nhật 18/10/2026: ĐÃ CODE** các mục dưới (nhánh `tier1-refactor-victoria`, PR #1 — `claude/TIER1_CHANGE_PLAN.md`), chưa chạy trên VM.

**Cập nhật 15/10/2026 (đã chốt)** — sửa 6 điểm yếu của bước phân loại + gộp cạnh, phát hiện khi đối chiếu code với dữ liệu thật (`infra/scripts/log.txt`):

| # | Thay đổi | Mục |
|---|---|---|
| 1 | DNS: luật tối giản theo **Service đích DNS của cluster** hoặc port 53, chỉ để loại khỏi scoring. Bỏ hướng đọc log CoreDNS khỏi phạm vi | 2.2, 7 |
| 2 | MONITORING: nhận diện theo **danh tính đã xác minh** (namespace + service account từ SPIFFE principal), xét **trước** CONTROL_PLANE. Tên khớp mà danh tính không khớp → cờ `claimed_monitoring`. Yêu cầu: workload monitoring phải nằm trong mesh | 2.2 |
| 3 | HEALTHCHECK: 3 tầng — path từ **probe khai trong pod spec** + danh sách mặc định ngắn + đội vận hành tự thêm; so khớp theo segment, không theo tiền tố | 2.2 |
| 4 | Port: `port = None` + `port_source`, suy port từ Service spec thay vì gán 0 | 2.1, 6 |
| 5 | Ngày: `traffic_days` (danh sách ngày) + `distinct_days`; khi gộp cạnh lấy **hợp** các ngày, không lấy max | 2.1, 2b |
| 6 | Bỏ đặc trưng `protocol_diversity` (luôn = 0,5), bù bias −1,2 → −1,0 | 4 |
| 7 | Khớp traffic ↔ config: config chưa phân giải dùng `pair_key` có đích `raw:<host>` (sửa lỗi dồn nhiều đích UNRESOLVED vào 1 ô) | 1b |
| 8 | Config ở port không có traffic trong cặp đã có traffic → sinh kết quả `NOT_OBSERVED` riêng cho port đó (trước đây bị mất) | 1b |
| 9 | Ghi `config_match_level` (EXACT / PORT_ONLY / PAIR_ONLY) + cờ `protocol_mismatch`, `sa_mismatch`; chọn config tất định theo `item_id`. Strength **không đổi** | 1b |
| 10 | `log_volume` chia cho hằng số `volume_reference` cố định (lấy 1 lần từ dữ liệu rồi đóng băng), không dùng phân vị 95 theo snapshot | 4 |
| 11 | Khi fit: config tách thành 3 đặc trưng 0/1 (`is_structural`, `is_embedded`, `is_unresolved`); trước khi fit giữ bảng strength cũ | 4, 4a |
| 12 | `count_floor`: chốt **không dùng**; dashboard đánh dấu cạnh STRONG có count < 3 | 4a |
| 13 | Cạnh chỉ toàn nhiễu (không series BUSINESS nào): monitoring đã xác minh → Planner tự mở; còn lại → hàng đợi người duyệt, **không gọi Tier 2** | 5 |
| 14 | Báo cáo: recall Tier 1 + Tier 2 là chỉ số chính khi so với B1; recall riêng Tier 1 để phân tích | 13.3 |
| 15 | Mọi ngưỡng tĩnh (0,68 / 0,58, ≥50% UNKNOWN, ≥3 lần đổi/7 ngày, `volume_reference`) **giữ giá trị khởi tạo**; con số chính thức chọn lúc chạy đánh giá (θ qua ROC trên tập fit, θ_promote/θ_demote = θ ± 0,05) | 5b, 13.4 |
| 16 | Ablation hysteresis: N = 7 snapshot cắt từ 7 ngày đã thu (snapshot k = dữ liệu ngày 1..k), 1–2 trial; không thu thêm | 13.4 |
| 17 | Đổi định dạng khóa cạnh (port suy ra, `raw:host`) → chấp nhận **reset lịch sử `previous_status` 1 lần**, không viết code chuyển đổi | 5b |
| 18 | ⚠ Sửa lỗi kiểm selector bước 4: suy canonical của pod theo đúng thứ tự Istio (`service.istio.io/canonical-name` → `app.kubernetes.io/name` → `app` → tên workload); code cũ đọc nhãn sai `security.istio.io/canonical-name` | 3b |
| 19 | Kiểm selector 2 lần: Tier 1 (báo sớm) + Planner ngay trước khi sinh YAML (bắt buộc); đánh giá dùng validator thật (chỉ đọc); đọc Service/Pod 1 lần mỗi namespace mỗi lần kiểm; selector phía nguồn do Planner kiểm | 3b |
| 20 | Scope theo thứ tự: `PassthroughCluster`/`BlackHoleCluster` → host khớp ServiceEntry (index Collector) → có service_ref → có dấu chấm → UNKNOWN | 8 |
| 21 | Bỏ `is_dns_edge` (port 53) trong `pipeline.py`; DNS chỉ quyết ở bước phân loại (`traffic_class == DNS`) | 7 |
| 22 | ⚠ Sửa lỗi lưu trữ: tra `previous_status` theo đủ 7 phần khóa (thêm SA) và chỉ lấy snapshot **trước** snapshot hiện tại; thêm cột `snapshot_time` để đếm số lần đổi trạng thái; thêm cột `port_source`, `traffic_days`, `flags`, `config_match_level`, `auto_allow_reason` | 10b |
| 23 | **(22/10)** Cổng port ở **cửa ra**, không chặn ở cửa vào: Tầng 1 vẫn chấm điểm cạnh thiếu/mâu thuẫn port, chỉ gắn cờ (`port_unresolved`, `config_port_differs`, `multiple_ports_seen`); hàng đợi review (vai Planner) chặn cạnh sắp sinh rule bằng `NEEDS_MANUAL_PORT` / `PORT_CONFLICT`, người duyệt `SUBMIT_PORT` | 2.1b, 6 |
| 24 | **(27/10)** Cạnh chưa có port mà bằng chứng cấu hình **sạch** của cùng cặp có **đúng 1** port (đã là targetPort) → lấy port đó, `port_source = config`, cờ `port_from_config`. ≥2 port → không đoán. Khóa mới trùng cạnh đã có → giữ None, cờ `config_port_taken`. Port thấy được không bao giờ bị config ghi đè | 1b, 2.1b |

---

## 0. Vai trò của Tier 1 trong kiến trúc hai tầng

Tier 1 là tầng suy luận **duy nhất** trước Tier 2 (AI Agent) — nó tự kết luận được khi đủ tin cậy (`OBSERVED_STRONG`), còn lại thì escalate. Nó tiêu thụ song song **hai luồng bằng chứng độc lập**:

- Bằng chứng runtime (traffic thật) — xem `TRAFFIC_COLLECTION_PIPELINE.md` để biết cách thu thập.
- Bằng chứng cấu hình (config đã khai báo) — xem `CONFIG_EVIDENCE_COLLECTOR.md` để biết cách thu thập.

Không nguồn bằng chứng nào trong hai nguồn trên tự kết luận gì cả — **Tier 1 là nơi duy nhất tổng hợp và ra quyết định**. Vai trò của Tier 1 không phải kết luận chắc chắn "dependency A→B có thật hay không", mà là tổng hợp mọi tín hiệu sẵn có thành một trong 5 trạng thái tường minh (mục 5), trong đó chỉ một trạng thái được phép tự động dẫn tới enforce.

Thứ tự xử lý trong `tier1/pipeline.py::run_tier1`:

```
TrafficSeries (từ read traffic)
  → [2.1] chuẩn hóa (path, port, ngày)
  → [2.2] phân loại traffic_class từng series
  → [2b]  gộp theo cạnh (identity_key)
  → [1]   candidate C = C_observed ∪ C_potential (gộp theo pair_key)
  → [4]   scoring → [5] trạng thái + hysteresis → selector / scope / lưu trữ
```

---

## 1. Candidate Discovery — 2 nguồn hợp thành 1 tập

**Vấn đề gốc đã sửa**: nếu candidate chỉ đến từ traffic quan sát được, một dependency có thật nhưng traffic thưa (CronJob tháng, nhánh lỗi hiếm) sẽ **không bao giờ được xem xét**, chứ không phải "bị đánh giá thấp".

```
C = C_observed ∪ C_potential

C_observed  = tập cặp (source, dest) có traffic thật trong cửa sổ K ngày
              (từ HTTP/gRPC và TCP telemetry — xem TRAFFIC_COLLECTION_PIPELINE.md;
               DNS không có trong telemetry, xem mục 7)

C_potential = tập cặp suy ra từ bằng chứng cấu hình TĨNH, không cần traffic
              (env/envFrom/command/args/initContainers, ConfigMap dạng biến và file,
               image ENV/ENTRYPOINT/CMD — đọc từ pod template nên có cả CronJob chưa
               chạy/workload replicas=0; LLM trích xuất rồi xác thực tất định với
               K8s/Istio API — xem CONFIG_EVIDENCE_COLLECTOR.md; đã resolve sẵn thành
               identity_key đầy đủ trước khi đưa vào đây)
```

Một cặp thuộc `C_potential` nhưng không có traffic → trạng thái `NOT_OBSERVED` (mục 5), không phải bị loại khỏi hệ thống. Cặp `FOUND_UNRESOLVED` chưa phân giải được đích vẫn vào `C_potential` dưới dạng `(source, raw:<host>)` — không bao giờ khớp traffic nên đi thẳng nhánh `NOT_OBSERVED` → escalate Tier 2.

```python
def discover_candidates(traffic_snapshot, config_evidence):
    c_observed = {(e.source, e.dest, e.protocol, e.port)
                  for e in traffic_snapshot if e.count > 0}
    c_potential = {(e.source, e.dest or f"raw:{e.raw_host}") for e in config_evidence}
    return c_observed | c_potential
```

## 1b. Khớp traffic với config (`candidates.py` + `pipeline.py`) — 📐 cập nhật 15/10

**Khóa gặp nhau = `pair_key` = (ns nguồn, svc nguồn, ns đích, svc đích)** — không có protocol, port, SA. Lý do: config thường không ghi protocol (`cartservice:7070` không nói là gRPC), traffic hiện không có port; khớp theo đủ 7 phần thì gần như không cặp nào gặp được. SA bỏ khỏi khóa vì SA config (pod template) và SA traffic (mTLS) chỉ cần lệch nhẹ là 2 bên không gặp — lệch SA được **ghi cờ** thay vì làm hỏng khớp.

- Config đã phân giải: `pair_key` như traffic.
- Config `FOUND_UNRESOLVED` (đích chưa phân giải): `pair_key = (ns, svc, "raw", raw_host)`. **Sửa lỗi**: code cũ dùng `(ns, svc, None, None)` → mọi đích UNRESOLVED của cùng nguồn (vd `billing-legacy.internal` và `old-ledger`) dồn vào 1 ô, chỉ 1 đích ra kết quả, đích kia mất.

**Cơ chế**: 2 vòng lặp trên cùng `dict[pair_key → Candidate]` (`setdefault`) — vòng traffic (chỉ cạnh `count > 0`) rồi vòng config. Cùng cặp → cùng ô (= phép hợp `C_observed ∪ C_potential`), O(số cạnh + số bản ghi).

**3 loại ô**:

| Loại | Xử lý |
|---|---|
| Chỉ traffic | mỗi cạnh chấm điểm, `config_match_strength = 0` |
| Traffic + config | khớp theo port (dưới), mỗi cạnh chấm điểm |
| Chỉ config | `NOT_OBSERVED`, **1 kết quả cho mỗi port** khác nhau trong config (không qua sigmoid); finding sạch → người duyệt, UNRESOLVED/NOT_FOUND → Tier 2 |

**Khớp trong ô "traffic + config"** — từng bản ghi config E (sắp theo `item_id`) so với các cạnh traffic của cặp:

| Mức | Điều kiện | Kết quả |
|---|---|---|
| `EXACT` | port E = port cạnh, và protocol E trùng hoặc E không ghi protocol | gắn vào cạnh đó |
| `PORT_ONLY` | port trùng, protocol E khác protocol cạnh | gắn vào cạnh + cờ `protocol_mismatch` |
| `PAIR_ONLY` | không so được port (port E `None`, hoặc cạnh có `port_source = unknown`) | gắn vào cạnh của cặp |
| Config dư | port E đã biết, **mọi** cạnh của cặp đều có port đã biết, không cạnh nào trùng | sinh kết quả `NOT_OBSERVED` riêng cho port của E (ví dụ config redis 6379 + sentinel 26379, traffic chỉ thấy 6379 → 26379 thành 1 dòng NOT_OBSERVED) |

Mỗi cạnh chọn **1** E đại diện: mức khớp cao nhất (EXACT > PORT_ONLY > PAIR_ONLY) → finding sạch (STRUCTURAL/EMBEDDED) trước → `item_id` nhỏ nhất (tất định, chạy lại ra cùng kết quả). Sau đó so SA của E với SA của cạnh — khác → cờ `sa_mismatch` (một danh tính khác đang dùng đường này, đáng chú ý về bảo mật).

Đầu ra mỗi cạnh: `config_finding` → `config_match_strength` (STRUCTURAL/EMBEDDED 1,0; UNRESOLVED 0,3; không có 0) — **giữ nguyên bảng strength**, chỉ thêm `config_match_level` và cờ để dashboard/Tier 2 thấy.

**Ví dụ**:

| Cặp | Traffic | Config | Kết quả |
|---|---|---|---|
| frontend → productcatalogservice | GRPC 3550 | `…:3550` (không ghi protocol) | EXACT, strength 1,0 |
| frontend → cartservice | HTTP 7070 | GRPC 7070 | PORT_ONLY + `protocol_mismatch`, strength 1,0 |
| checkoutservice → redis-cart | TCP 6379 | 6379, 26379 | 6379 EXACT; 26379 → NOT_OBSERVED riêng |
| reportjob → report-db | – | `postgres://report-db:5432` | chỉ config → NOT_OBSERVED, người duyệt |
| reportjob → raw:billing-legacy.internal | – | UNRESOLVED | NOT_OBSERVED → Tier 2 |
| reportjob → raw:old-ledger | – | UNRESOLVED | NOT_OBSERVED → Tier 2 (code cũ: mất dòng này) |

**Phụ thuộc**: mức EXACT/PORT_ONLY và "config dư" chỉ có tác dụng khi port traffic đã sửa (mục 2.1b). Với dữ liệu hiện tại (port luôn 0), mọi cạnh rơi về PAIR_ONLY và không có config dư — đúng như hành vi cũ.

---

## 2. Phân loại traffic (traffic_class)

Không phải mọi traffic giữa hai service đều phản ánh một luồng nghiệp vụ thật. Mỗi **series** (1 tổ hợp nguồn, đích, protocol, port, path) được phân loại **trước** khi gộp cạnh và tính điểm — không xóa, chỉ loại khỏi phần đóng góp tích cực vào điểm số, để vẫn phục vụ gỡ lỗi.

```
traffic_class ∈ { BUSINESS, HEALTHCHECK, MONITORING, DNS, CONTROL_PLANE, UNKNOWN }
```

**Vì sao phân loại trước khi gộp**: nhãn phải gắn ở mức path. Cùng cạnh `frontend → productcatalogservice` có cả path nghiệp vụ (`/hipstershop.ProductCatalogService/ListProducts`) lẫn path health (`/grpc.health.v1.Health/Check`); gộp trước thì mất path, không phân biệt được nữa.

### 2.1 Chuẩn hóa đầu vào (📐 15/10)

**Nhãn Prometheus → trường `TrafficSeries`:**

| Trường | Nhãn Istio | Ghi chú |
|---|---|---|
| `source_workload_namespace` | `source_workload_namespace` | |
| `source_canonical_service` | `source_canonical_service` | **tự khai** — pod tự đặt label được |
| `source_service_account` | tách từ `source_principal` (`spiffe://cluster.local/ns/<ns>/sa/<sa>`) | **đã xác minh** — Istio cấp qua mTLS; `unknown` nếu nguồn ngoài mesh |
| `destination_canonical_service`, `destination_namespace` | `destination_canonical_service`, `destination_workload_namespace` | |
| `destination_service_ref` | `destination_service_name` + `destination_service_namespace` | dùng tra Service spec |
| `protocol` | `request_protocol` (HTTP/GRPC); TCP nếu từ `istio_tcp_*` | |
| `path` | `request_path` (label tùy chỉnh) | không có ở TCP |
| `port` | `destination_port` | **nhãn vắng** trong dữ liệu đã thu |

**(a) Path**: viết thường → cắt từ `?` → bỏ `/` cuối → tách theo `/` thành `segments`.
`"/_healthz?probe=1"` → `path_norm="/_healthz"`, `segments=["_healthz"]`.

**(b) Port — không dùng 0 nữa**: `port: int | None` + `port_source`:

| `port_source` | Cách có port |
|---|---|
| `observed` | nhãn `destination_port` có trong metric (sau khi thêm tagOverride Telemetry — chỉ làm sau khi chụp snapshot) |
| `observed_log` | access log ztunnel thấy **đúng 1** port cho cặp (bước gộp metrics + log); nhiều port → giữ `None` + cờ `multiple_ports_seen` |
| `inferred_single_port` | Service đích (`destination_service_ref`) chỉ có 1 port → lấy `targetPort` |
| `inferred_protocol_match` | Service có nhiều port → chọn port có `appProtocol` / tên (`grpc-*`, `http-*`) khớp `protocol` |
| `config` | **(27/10)** 4 cách trên đều thất bại, config sạch của cặp có đúng 1 port → lấy port đó (cờ `port_from_config`); làm ở bước ghép config (`candidates.fill_port_from_config`) |
| `unknown` | không chọn được → `port=None`, gắn cờ người duyệt; **không đoán** |

Mục đích: (1) policy đầu ra cần port (mục 6) — không có port thì phải mở mọi port, trái Zero Trust; (2) đối chiếu với bằng chứng config, vốn đã có port (`targetPort`, CONFIG_EVIDENCE_COLLECTOR bước (d)). Việc tra cứu dùng **chung index cluster** của Collector. Khác bước (d) của Collector: bên đó đã có port trong chuỗi config, chỉ đổi port Service → targetPort; bên traffic chưa có port nào, phải suy từ Service.

**(c) Ngày**: read traffic chạy 7 query theo ngày. Mỗi series giữ:

```python
traffic_days:  list[str]   # ["2026-10-02", "2026-10-05"] — đã sắp xếp
distinct_days: int         # = len(traffic_days)
```

Số request theo từng ngày không đi vào Tier 1 — read traffic ghi thẳng vào bảng `traffic_daily_counts` cho dashboard (`claude/DASHBOARD_DESIGN.md` 7.3).

### 2.2 Luật phân loại — chuỗi if/return, luật đầu tiên khớp thì dừng

```python
def classify_series(s, cfg):
    if is_dns(s, cfg):                     return DNS
    if is_verified_monitoring(s, cfg):     return MONITORING
    if s.source_workload_namespace in cfg.control_plane_namespaces \
       or s.destination_namespace in cfg.control_plane_namespaces:
                                           return CONTROL_PLANE
    if s.source_canonical_service in cfg.monitoring_names:   # tên khớp, chưa xác minh
        s.flags.add("claimed_monitoring")  # chỉ gắn cờ, KHÔNG return
    if is_healthcheck(s, cfg):             return HEALTHCHECK
    if s.protocol in ("HTTP", "GRPC", "TCP"): return BUSINESS
    return UNKNOWN
```

**Vì sao thứ tự này** (lựa chọn của dự án):
- DNS đầu — đặc trưng chắc chắn nhất, và cạnh DNS do Planner tự mở (mục 7).
- MONITORING đã xác minh **trước** CONTROL_PLANE — Prometheus thật nằm ở `istio-system`; xét CONTROL_PLANE trước thì gắn nhầm (lỗi của bản cũ).
- MONITORING **trước** HEALTHCHECK — Prometheus gọi `/metrics`; xét healthcheck trước thì MONITORING không bao giờ được gắn (lỗi của bản cũ).
- BUSINESS là mặc định — nghiêng về giữ cạnh, đúng nguyên tắc ưu tiên recall.

**Luật 1 — DNS.** Khớp khi `destination_service_ref ∈ cfg.dns_services` (mặc định `kube-system/kube-dns`, tên Service chuẩn của cả kube-dns lẫn CoreDNS) **hoặc** `port == 53` (kể cả port suy ra). Trên thực tế gần như không bao giờ khớp: DNS đi UDP, ztunnel chỉ đo TCP, waypoint chỉ đo HTTP/gRPC → Prometheus không có series DNS (chỉ khớp khi DNS qua TCP, hiếm). Luật giữ lại vì không tốn gì và tránh `X → kube-dns` thành ứng viên thừa trùng với luật auto-allow của Planner.

**Luật 2 — MONITORING đã xác minh.** So `(source_workload_namespace, source_service_account)` — lấy từ SPIFFE principal, không giả được — với tập `verified_monitoring`, lập 1 lần mỗi lần chạy:
1. Tự phát hiện từ `prometheus_url` đang dùng: Service → selector → pod → (namespace, service account).
2. Workload có label `app.kubernetes.io/name=prometheus` (hoặc CR `Prometheus` của Prometheus Operator nếu có).
3. Danh sách khai báo trong config (thí nghiệm khai `trial-*/prometheus` của harness nhiễu — xem dưới).

Đường dẫn `/metrics`, `/federate`, `/stats/prometheus` hoặc annotation `prometheus.io/path` chỉ là tín hiệu phụ cho dashboard, không đủ để gắn MONITORING.

Tên khớp danh sách `monitoring_names` nhưng danh tính **không** thuộc tập → không gắn MONITORING (không bị trừ điểm), chỉ gắn cờ `claimed_monitoring` để dashboard cảnh báo. Lý do: so tên dễ bị giả — kẻ tấn công đặt label `canonical-name=prometheus` để cạnh của mình bị đánh giá thấp.

**Yêu cầu triển khai**: workload monitoring muốn được nhận là MONITORING **phải chạy trong namespace đã bật ambient mesh** (để có SPIFFE principal). Nằm ngoài mesh → `source_principal = unknown` → luật 2 không khớp → traffic của nó thành BUSINESS + `claimed_monitoring`.

**Thí nghiệm** — chọn phương án (a): harness nhiễu monitoring (`scenario_pool.py::_MONITORING_SOURCES` = prometheus, grafana, blackbox-exporter; SA trùng tên nguồn) được khai báo trong `EXPERIMENT_CONFIG` (`*/prometheus`, `*/grafana`, `*/blackbox-exporter`), vì nhiễu này được thiết kế để giả lập monitoring; báo cáo ghi rõ. `DEFAULT_CONFIG` chỉ tin `istio-system/prometheus`; Prometheus/vmagent khác được tự phát hiện từ cluster (`cluster_context.py`). Grafana, Kiali, blackbox-exporter, máy quét bảo mật: cùng cơ chế, khai báo trong config — phần mở rộng.

**Luật 3 — CONTROL_PLANE.** Namespace nguồn **hoặc** đích ∈ `{istio-system, kube-system}`.

**Luật 4 — HEALTHCHECK, 3 tầng nguồn:**

| Tầng | Nguồn | Nội dung |
|---|---|---|
| 1 (chính) | **Probe trong pod spec** — `livenessProbe` / `readinessProbe` / `startupProbe` của container đích: `httpGet.path`, `grpc` | path health **thật** của từng service, đội vận hành đã khai sẵn. Config Collector vốn đã đọc pod spec, chỉ lấy thêm phần probe |
| 2 | Danh sách mặc định **ngắn, phổ biến nhất** | `/health`, `/healthz`, `/readyz`, `/livez`, `/ready`, `/live`, `/metrics`; gRPC `grpc.health.v1.Health` (CHỈ health — xem ghi chú reflection) |
| 3 | Đội vận hành tự thêm | trang cấu hình dashboard (giống quy tắc công ty của Collector); trước 29/12 đọc từ YAML |

So khớp (sau chuẩn hóa 2.1a):
- Path từ probe (tầng 1) của **đúng service đích**: so bằng `path_norm`.
- Tầng 2/3: **segment cuối** bằng đúng từ khóa (`/api/v1/healthz` ✓, `/livestream` ✗), hoặc path bằng nguyên văn mục khai báo.
- gRPC: segment đầu (tên service) ∈ danh sách (`/grpc.health.v1.Health/Check` ✓).
- **gRPC reflection KHÔNG nằm mặc định (điều chỉnh 18/10 khi code):** harness thí nghiệm sinh traffic cho dependency cấy bằng `grpcurl` qua reflection (`infra/scenarios/render_manifests.py`) → trong thí nghiệm `/grpc.reflection.v1[alpha].ServerReflection/...` là traffic THẬT (ground truth True). Gắn là healthcheck làm recall 15 trial tụt 0,852 → 0,789. Môi trường thật coi reflection là công cụ debug thì thêm 2 path đó vào `healthcheck_extra_paths`.
- `/cart/123/status` không khớp gì → BUSINESS.

Lý do không làm danh sách dài: càng dài càng bắt nhầm path nghiệp vụ (gắn nhầm HEALTHCHECK = trừ điểm oan cạnh thật); path lạ của từng hệ thống đã có trong probe. Dashboard thống kê "cạnh chỉ có healthcheck" để rà lại.

**Luật 5–6 — BUSINESS / UNKNOWN.** Protocol ∈ HTTP/GRPC/TCP → BUSINESS; còn lại → UNKNOWN (vẫn tính vào evidence nhưng hạ độ tin).

---

## 2b. Gộp theo cạnh (aggregation) — 📐 cập nhật 15/10

Gộp mọi series (đã có nhãn) của cùng một cạnh thành 1 `TrafficAggregate`. Khóa cạnh = `identity_key` 7 phần (mục 3) — **không có path**: NetworkPolicy/AuthorizationPolicy cấp quyền theo cặp workload + port, không theo path.

Cơ chế: `dict[IdentityKey → TrafficAggregate]`, duyệt mỗi series 1 lần, O(số series).

| Trường | Cách gộp |
|---|---|
| `count`, `error_count` | cộng |
| `traffic_days` | **hợp** (union) các series |
| `distinct_days` | `len(traffic_days)` |
| `last_seen_days_ago` | min |
| `class_counts[class]` | `+= series.count` (trọng số theo lưu lượng) |
| `flags` | hợp (`claimed_monitoring`, `port_unknown`…) |
| `workload_revision` | revision mới nhất ghi đè |
| `dominant_traffic_class()` | class count lớn nhất; BUSINESS ≥ class đứng nhì → BUSINESS |

**Port trong khóa**: dùng port đã suy (2.1b). `None` (unknown) là giá trị khóa riêng, không còn trùng với port 0 thật. Cùng cạnh có cả series port đã biết lẫn unknown → 2 dòng, dòng unknown gắn cờ.

**Ví dụ (vì sao hợp ngày)**: `reportjob → reportservice` có `/export` (ngày 02/10) và `/sync` (ngày 05/10). Bản cũ lấy `max(1, 1) = 1` ngày; bản mới `{02/10} ∪ {05/10}` → **2** ngày → `distinct_days_ratio` đúng hơn cho nhóm traffic thưa (recall nhóm thưa hiện 0,256).

**Tương thích**: file `infra/trial_data/trial-XX.json` xuất trước 15/10 không có `traffic_days` → quay về cách lấy max. Muốn số đúng: chạy lại `08_export_trial_data.py` với code mới (query lại snapshot Prometheus, không cần thu lại 7 ngày).

Hai tầng gộp: **cạnh** (`identity_key`, có protocol + port) là kết quả mục này; **cặp** (`pair_key`, chỉ nguồn + đích) là nơi cạnh traffic gặp bằng chứng config (mục 1, `candidates.py`).

---

## 3. Định danh ổn định (Identity Resolution)

**Vấn đề gốc đã sửa**: dùng tên workload thô (`payment-v1`, `payment-v2`) làm key sẽ phân mảnh cùng một dependency logic thành nhiều edge khi rollout, và có thể đụng độ giữa các namespace khác nhau.

**Key chuẩn hóa** (dùng label sẵn có của Istio, không tự viết logic gộp):

```
identity_key = (
    source_namespace,
    source_canonical_service,     # gộp tự động các version rollout
    source_service_account,       # SPIFFE identity, quan trọng cho Zero Trust
    destination_namespace,
    destination_canonical_service,
    protocol,
    port                          # int | None (None = unknown, mục 2.1b)
)
```

- `*_canonical_service` lấy trực tiếp từ label Istio (`destination_canonical_service`, `source_canonical_service`) — không cần tự viết logic gộp `payment-v1/v2/v3`.
- `namespace` bắt buộc trong key — tránh đụng độ `ns-a/payment` và `ns-b/payment`.
- `service_account` đưa vào key vì NetworkPolicy/Zero Trust thật quan tâm identity (ai được phép gọi), không chỉ tên workload.
- Không cần phân biệt loại controller (Deployment/StatefulSet/DaemonSet/Job/CronJob) — canonical identity tự động cover hết loại nào cũng vậy.

Bằng chứng cấu hình (từ `CONFIG_EVIDENCE_COLLECTOR.md`) resolve identity theo đúng cùng key này — bằng cách query `Service.spec.selector` → tìm pod khớp (hoặc pod template của workload nếu chưa có pod đang chạy — CronJob, `replicas=0`) → đọc nhãn `service.istio.io/canonical-name` (thiếu thì áp đúng thứ tự Istio tự suy ra: `app.kubernetes.io/name` → `app` → tên workload) + `serviceAccountName`, độc lập với việc pod đã có traffic hay chưa. Port của bằng chứng cấu hình là `targetPort` (port pod thật), không phải port của Service — khớp với cách NetworkPolicy mở port.

## 3b. Từ canonical identity sang NetworkPolicy thật — validation trước khi sinh YAML

**Vấn đề phát sinh**: `canonical_service` là nhãn đã bị Istio "nén" từ nhiều workload thật lại làm một. Khi cần sinh YAML `NetworkPolicy` thật, K8s yêu cầu `podSelector` dựa trên label pod — không hiểu khái niệm canonical service.

**Giải pháp — lưu song song ngay từ lúc thu thập, không dịch ngược**:

```
canonical_service        → dùng để gộp/tính điểm (mục 3)
destination_service_ref  → (service_name, service_namespace), trỏ thẳng
                            K8s Service thật, dùng riêng cho lúc sinh NetworkPolicy
```

**Validation bắt buộc tại thời điểm sinh YAML** (không phải lúc quan sát traffic):

1. Query trực tiếp `Service.spec.selector` từ K8s API tại thời điểm sinh policy, không dùng cache cũ.
2. Kiểm tra tập pod mà selector trả về không rỗng.
3. **Overlap check**: liệt kê mọi Service khác trong cùng namespace, kiểm tra tập pod của selector này có giao với tập pod của Service nào khác không — có giao → selector không đủ đặc thù, cảnh báo, không tự động enforce.
4. Đối chiếu ngược lại `destination_workload` đã quan sát: lần theo `ownerReferences` của pod, xác nhận thực sự thuộc đúng workload đã ghi nhận traffic.

**Trường hợp không có selector** (Service dùng `Endpoints` thủ công, `spec.selector` rỗng): dùng `ipBlock` thay vì `podSelector`, bắt buộc gắn cờ cần review thủ công.

**Luật cứng**: nếu bất kỳ bước validation nào thất bại → trạng thái `NEEDS_MANUAL_SELECTOR` (mục 5), đẩy vào hàng đợi exception — không tự động sinh NetworkPolicy.

**Chốt 15/10 (📐):**
- **Sửa lỗi bước 4**: code (`KubernetesSelectorValidator`) đọc nhãn `security.istio.io/canonical-name` — sai tên, và phần lớn pod không gắn nhãn canonical tường minh → trên cluster thật mọi cạnh STRONG sẽ rớt NEEDS_MANUAL_SELECTOR. Sửa: suy canonical của pod theo đúng thứ tự Istio (`service.istio.io/canonical-name` → `app.kubernetes.io/name` → `app` → tên workload), dùng chung hàm với Config Collector.
- **Kiểm 2 lần**: trong Tier 1 ngay sau khi lên STRONG (báo sớm), và **bắt buộc** lại trong Migration Planner ngay trước khi sinh YAML (Tier 1 có thể chạy trên snapshot cũ vài ngày).
- **Đánh giá dùng validator thật** (chỉ đọc Service/Pod, cùng quyền với Collector). Số liệu hiện có dùng `MockSelectorValidator` (có service_ref là qua) → NEEDS_MANUAL_SELECTOR chỉ xuất hiện khi thiếu service_ref; báo cáo phải ghi rõ.
- **Đọc 1 lần mỗi namespace mỗi lần kiểm**: liệt kê toàn bộ Service + Pod của namespace một lần rồi dùng cho mọi cạnh (vẫn là dữ liệu đọc tại thời điểm kiểm, không phải cache cũ) — thay vì mỗi cạnh liệt kê lại mọi Service và pod.
- **Phía nguồn**: luật A → B cần cả egress ở A; selector của workload nguồn do Planner tìm và kiểm (Tier 1 chỉ kiểm phía đích).

---

## 4. Scoring — đa yếu tố, trọng số học từ dữ liệu

**Vấn đề gốc đã sửa**: ngưỡng cứng (`count≥20 AND distinct_days≥3 → confidence=0.9`) vừa arbitrary vừa bất công (service traffic cao/thấp bị đối xử như nhau).

```
score = sigmoid(bias + w1·recency + w2·log_volume + w3·distinct_days_ratio
              + w5·healthcheck_ratio + w6·monitoring_ratio
              [+ w7·config_match_strength — mục 4a])
```

**Định nghĩa từng đặc trưng, chuẩn hóa về [0,1]:**
- `recency = 1 − (days_since_last_seen / K)`
- `log_volume = min(log(count+1) / log(volume_reference+1), 1)`. **Chốt 15/10:** `volume_reference` là **hằng số trong `ScoringConfig`**, lấy 1 lần (phân vị 95 count các cạnh của snapshot đầu tiên) rồi đóng băng. Bản cũ dùng `EXPECTED_MAX = percentile_95(...)` tính lại theo từng snapshot → điểm của một cạnh thay đổi khi cạnh **khác** tăng/giảm traffic (ví dụ thêm 1 cạnh 50.000 request làm điểm cạnh cartservice giảm dù traffic không đổi), trạng thái đổi vì lý do không liên quan, điểm giữa các trial không so được.
- `distinct_days_ratio = distinct_days / K` (`distinct_days` = số ngày trong hợp `traffic_days`, mục 2b)
- `healthcheck_ratio`, `monitoring_ratio` — tỷ lệ request thuộc traffic_class tương ứng

**Đã bỏ (15/10) — `protocol_diversity = min(distinct_protocols / 2, 1)`**: `protocol` nằm trong `identity_key` nên mọi series gộp vào một cạnh cùng protocol → `distinct_protocols` luôn = 1 → đặc trưng luôn = 0,5 cho mọi cạnh, không phân biệt được gì (chỉ như cộng `w4·0,5 = 0,4·0,5 = +0,2` vào bias). Bỏ đặc trưng và **bù bias −1,2 → −1,0** để điểm số giữ nguyên về toán (ngưỡng 0,68/0,58 không đổi). Báo cáo ghi rõ lý do loại. Số đặc trưng còn 6 (5 traffic + `config_match_strength`).

**Trọng số — học bằng logistic regression** (`sklearn.linear_model.LogisticRegression`), không tự đoán. Nhãn huấn luyện: 1 nếu là dependency thật (ground truth Online Boutique + dependency đã cấy), 0 nếu không (health check, monitoring, cặp ngẫu nhiên). Kết quả là một xác suất thật P(dependency thật | evidence quan sát được). Hiện là giá trị đặt tay (`tier1/config.py`), fit đã lên kế hoạch (`claude/TRANG_THAI_CHOT.md` mục 3.3).

**Chống overfitting với dataset nhỏ**: L2 regularization, K-fold cross-validation (k=5), tách bạch tuyệt đối tập fit trọng số với tập báo cáo precision/recall cuối cùng.

`θ_strong` chọn qua đường ROC trên ground truth thật, ưu tiên recall cao hơn precision theo nguyên tắc bất đối xứng an toàn. Giá trị khởi tạo: `θ_strong = 0.6`.

### 4a. Bổ sung bằng chứng cấu hình vào công thức — chỉ khi đã có traffic nền

Khi một cặp đã có traffic (`count > 0`), bằng chứng từ Config Evidence Collector (`config_finding`) được thêm làm đặc trưng `config_match_strength`, tái tính điểm qua cùng công thức sigmoid. **Luật cứng, không thương lượng**: `count > 0` là điều kiện cần **tuyệt đối** cho `OBSERVED_STRONG` — không có tổ hợp feature nào (kể cả `config_match_strength` cực mạnh) được phép một mình kéo một cặp `count=0` lên `OBSERVED_STRONG`. Ràng buộc này validate cứng ở tầng kiến trúc, không để logistic regression tự học từ dữ liệu (dataset nhỏ có thể học sai theo hướng nguy hiểm). Xem mục 5 về nhánh xử lý riêng cho `count=0`.

**Strength của config khi fit (chốt 15/10)**: trước khi fit giữ bảng STRUCTURAL/EMBEDDED 1,0 · UNRESOLVED 0,3 · không có 0. Khi fit hồi quy, thay `config_match_strength` bằng **3 đặc trưng 0/1** `is_structural`, `is_embedded`, `is_unresolved` để dữ liệu tự quyết trọng số từng loại (EMBEDDED — địa chỉ lẫn trong câu dài — hiện được tin ngang STRUCTURAL, không có căn cứ).

**`count_floor` — chốt 15/10: KHÔNG dùng.** Ví dụ: cạnh 1 request/1 ngày, thấy hôm qua, có config STRUCTURAL → `x ≈ −1,0 + 0,9·0,86 + 1,1·0,10 + 1,0·0,14 + 0,8·1,0 = 0,82` → score ≈ 0,69 → STRONG. Đây là **2 nguồn độc lập cùng xác nhận** (traffic thật + config trỏ đúng đích); đặt sàn sẽ hại đúng nhóm cạnh thưa cần cứu. Bù lại: dashboard đánh dấu cạnh STRONG có `count < 3` để người xem lướt. Tham số `count_floor` trong `ScoringConfig` giữ mặc định `None`.

---

## 5. Trạng thái đầu ra và luật xử lý

**Vấn đề gốc đã sửa**: "không đạt threshold" bị hiểu ngầm thành "không có dependency" — thiếu bằng chứng khác với bằng chứng của sự thiếu vắng.

```
dependency_status ∈ {
    OBSERVED_STRONG,       # score ≥ θ_strong VÀ count > 0 → dừng ở Tier 1
    OBSERVED_WEAK,         # có traffic (0 < score < θ_strong) → escalate Tier 2
    NOT_OBSERVED,          # thuộc C_potential nhưng count=0 → xem nhánh riêng dưới đây
    UNKNOWN,               # có traffic nhưng traffic_class mâu thuẫn/không rõ → escalate Tier 2
    NEEDS_MANUAL_SELECTOR  # OBSERVED_STRONG nhưng validation mục 3b thất bại
                           # → không tự sinh NetworkPolicy, cần người xử lý
}
```

**Nhánh riêng cho `NOT_OBSERVED`** (tách Safety khỏi Cost — không đi qua lại công thức sigmoid để tìm STRONG, vì luôn thất bại điều kiện cần `count>0`):

- Config Evidence Collector trả `FOUND_STRUCTURAL`/`FOUND_EMBEDDED` sạch (không conflict) → đẩy vào **hàng đợi review con người**, không escalate Tier 2 AI Agent (bỏ qua chi phí AI vì đã có bằng chứng cấu hình rõ ràng) — nhưng vẫn **không tự động enforce**.
- Config Evidence Collector trả `FOUND_UNRESOLVED`/`NOT_FOUND` → escalate Tier 2 (AI Agent), vì hoàn toàn mù cả traffic lẫn config.

**Nhánh riêng cho cạnh chỉ toàn nhiễu (chốt 15/10, 📐)** — cạnh có traffic nhưng **không có series BUSINESS nào** (mọi request là HEALTHCHECK / MONITORING / CONTROL_PLANE). Bản cũ: chỉ xét điểm → điểm ~0,47–0,49 (traffic đều mỗi ngày bù phần bị trừ do nhiễu) → OBSERVED_WEAK → escalate Tier 2, tốn chi phí AI chỉ để kết luận lại "đây là healthcheck". Cạnh nhiễu **vẫn là traffic thật cần được cho phép** (chặn Prometheus = mất giám sát, chặn healthcheck = service bị coi là chết), nên không bỏ mà rẽ nhánh:

| Loại | Xử lý |
|---|---|
| MONITORING **đã xác minh** (danh tính Prometheus, mục 2.2) | Planner **tự mở** như DNS (mục 7), ghi `auto_allow_reason = VERIFIED_MONITORING` |
| Chỉ HEALTHCHECK, hoặc MONITORING chưa xác minh (`claimed_monitoring`), hoặc CONTROL_PLANE | **Hàng đợi người duyệt** (`needs_human_review`; mã `NOISE_ONLY_REVIEW` — ⚠ trước 22/10 chưa có mã nào đưa vào hàng đợi, cạnh bị bỏ quên), **không** escalate Tier 2 — bằng chứng chỉ là tên path / danh tính, AI không thêm được gì |
| Có lẫn ít nhất 1 series BUSINESS | Đi luồng thường (hysteresis bên dưới) |

### 5b. Chống nhấp nháy trạng thái — hysteresis bất đối xứng

**Vấn đề**: score dao động tự nhiên quanh `θ_strong` khiến trạng thái đổi qua lại liên tục giữa các lần quan sát.

**Vì sao không dùng majority vote**: majority vote đối xử công bằng giữa "được thăng lên STRONG" và "bị rớt khỏi STRONG" — vi phạm nguyên tắc bất đối xứng an toàn.

**Giải pháp — ngưỡng kép không đối xứng**:

```
θ_promote = 0.68   # khó lên
θ_demote  = 0.58   # dễ rớt

Nếu đang KHÔNG phải STRONG: chỉ chuyển thành STRONG khi score ≥ θ_promote
Nếu đang LÀ STRONG: chỉ bị hạ khỏi STRONG khi score < θ_demote
score trong [θ_demote, θ_promote) → giữ nguyên trạng thái hiện tại (vùng chết)
```

Chỉ cần lưu 1 cột `previous_status`. Nếu vẫn đổi trạng thái quá thường xuyên dù đã có hysteresis (≥3 lần/7 ngày) → gắn cờ `unstable_classification=true` để con người chú ý khi review.

**Ghi chú triển khai (15/10):**
- `previous_status` tra trong SQLite theo **khóa cạnh** (`identity_key`). Không có lịch sử → coi như "không phải STRONG" (cần ≥ θ_promote).
- **Đường đánh giá 15 trial chạy 1 snapshot/trial, không SQLite** (`conn = None`) → hysteresis **không hoạt động**, kết quả precision/recall hiện có thực chất dùng ngưỡng đơn 0,68. Hysteresis chỉ đo ở ablation mục 13.4.
- Sau khi đổi định dạng khóa cạnh (port suy ra thay 0, đích `raw:<host>`) → khóa cũ không khớp khóa mới → **chấp nhận reset lịch sử 1 lần**, không viết code chuyển đổi.
- Thứ tự quyết định trạng thái cho mỗi dòng: (Q1) count = 0 → nhánh NOT_OBSERVED · (Q2) cạnh chỉ toàn nhiễu → nhánh nhiễu (mục 5) · (Q3) ≥ 50% request class UNKNOWN → UNKNOWN → Tier 2 · (Q4) hysteresis → STRONG (→ kiểm selector mục 3b) / WEAK (→ Tier 2).
- Trạng thái UNKNOWN thực tế gần như không xảy ra (class UNKNOWN chỉ khi protocol ngoài HTTP/gRPC/TCP) — giữ làm chốt chặn, báo cáo ghi "0 trường hợp".
- **Ngưỡng tĩnh** (0,68 / 0,58; ≥ 50% UNKNOWN; ≥ 3 lần đổi / 7 ngày) **giữ giá trị khởi tạo**; con số chính thức chọn khi chạy đánh giá: sau khi fit trọng số, chọn θ trên ROC của tập fit (ưu tiên recall), đặt θ_promote = θ + 0,05, θ_demote = θ − 0,05, báo cáo trên tập còn lại.

**Luật cứng, áp toàn hệ thống**: chỉ `OBSERVED_STRONG` mới được tự kết luận đủ tin cậy ở Tier 1. Ba trạng thái còn lại (trừ nhánh review-người ở `NOT_OBSERVED`) đều bắt buộc escalate — **không bao giờ** Tier 1 tự kết luận "không có dependency" chỉ vì thiếu quan sát.

---

## 6. Đủ chi tiết để sinh policy hẹp

Schema evidence bắt buộc có `protocol` + `port`, không chỉ cặp (source, dest) — nếu không, phải sinh `A → B: allow tất cả port`, làm giảm giá trị Zero Trust đáng kể.

Vì traffic đã thu không có nhãn port, port lấy theo mục 2.1b và mang theo `port_source`. Cạnh `port_source = unknown` **không** được Planner sinh "allow mọi port" tự động — đưa vào hàng đợi người duyệt để chọn port.

**Chốt 22/10 — cổng port ở cửa ra, không ở cửa vào.** "Có quan hệ không" (Tầng 1 trả lời bằng điểm/trạng thái) độc lập với "mở cửa nào" (chỉ cần khi sinh YAML). Chặn cạnh thiếu port trước Tầng 1 = vứt cạnh → bị chặn khi bật default-deny (lỗi nguy hiểm nhất). Vì vậy:

- **Tầng 1** (cảnh báo sớm, không đổi điểm/trạng thái): cờ `port_unresolved` khi port `None`; cờ `config_port_differs` khi cạnh có port nhưng config cùng cặp chỉ ghi port khác (`candidates.py`).
- **Cửa ra** (hàng đợi review, vai Migration Planner — `orchestration/api/review.py::edge_port_reason`), chỉ xét cạnh sắp sinh rule (STRONG, Tier 2 CONFIDENT, `NEEDS_MANUAL_SELECTOR`, hoặc `auto_allow_reason`):

| Tình huống | Mã lý do |
|---|---|
| `multiple_ports_seen` | `PORT_CONFLICT` |
| port **suy ra** (`inferred_*`) + `config_port_differs` | `PORT_CONFLICT` |
| port `None` | `NEEDS_MANUAL_PORT` |
| port **thấy được** lệch config | không chặn — config lệch đã thành dòng `NOT_OBSERVED` riêng (config dư, mục 1b) |
| `inferred_protocol_match` không mâu thuẫn | không chặn, `port_source` hiện trên dashboard |

Hành động: `SUBMIT_PORT` (payload `port` 1..65535 = targetPort pod đích) hoặc `REJECT`. DB cũ có CHECK 11 mã → API khởi động tự dựng lại bảng `human_decisions` (giữ nguyên dữ liệu, `decisions_store.migrate_decision_checks`).

Thứ tự nguồn port giữ nguyên: thấy được (metrics, log) → Service spec (1 port → khớp tên giao thức) → để trống. Hướng mở (chưa làm): dùng port trong config của bên gọi (đã đổi sang targetPort) làm nguồn trước bước suy theo tên giao thức — cần đưa config vào trước bước gộp cạnh.

---

## 7. DNS dependency — xử lý riêng, không đưa vào scoring

DNS là "hidden dependency" kinh điển: nếu default-deny chặn nhầm DNS, ứng dụng không resolve được tên dù bản thân dependency vẫn tồn tại và đã enforce đúng.

**Quyết định**: Kube-dns/CoreDNS luôn **auto-allow mặc định** trong mọi migration wave (egress tới `kube-system/kube-dns` UDP/TCP 53, mọi pod), không phụ thuộc kết luận của Tier 1/Tier 2. Nguy cơ chặn nhầm chỉ nằm ở NetworkPolicy (L3/L4); AuthorizationPolicy của Istio không tác động DNS vì DNS không đi qua mesh.

**Một chỗ quyết DNS (chốt 15/10)**: bỏ `dns_external.is_dns_edge` (port 53) mà `pipeline.py` đang dùng để tách DNS riêng; tách theo `traffic_class == DNS` do bước phân loại (mục 2.2) gán.

**Vì sao telemetry không có DNS**: DNS đi UDP 53; ztunnel chỉ đo TCP (`istio_tcp_*`), waypoint chỉ đo HTTP/gRPC (`istio_requests_total`). Luật DNS ở mục 2.2 chỉ là chốt chặn để series DNS hiếm hoi (qua TCP) không thành ứng viên nghiệp vụ.

**Ngoài phạm vi (chốt 15/10)**: dùng log truy vấn CoreDNS (plugin `log`: tên hỏi, A/AAAA/SRV, NOERROR/NXDOMAIN) làm nguồn bằng chứng thứ ba — cần sửa ConfigMap `coredns` trên cluster, xử lý search path `ndots:5` và ánh xạ IP → pod theo thời gian. Ghi là hướng phát triển.

---

## 8. External dependency — phân loại riêng

```
scope ∈ { IN_CLUSTER, EXTERNAL, UNKNOWN }
```

Nếu `EXTERNAL`: lưu FQDN + port thay vì workload identity; Migration Planner sinh `ServiceEntry` thay vì `NetworkPolicy` thuần.

**Cách xác định scope cho cạnh traffic (chốt 15/10, 📐)** — code cũ chỉ có: có service_ref → IN_CLUSTER; tên đích có dấu chấm → EXTERNAL; còn lại → UNKNOWN. Luật "dấu chấm" gần như không khớp traffic thật: gọi ra ngoài chưa khai ServiceEntry thì Istio ghi `destination_service_name = PassthroughCluster` (bị chặn: `BlackHoleCluster`) và canonical `unknown`. Thứ tự mới:

1. `destination_service_name ∈ {PassthroughCluster, BlackHoleCluster}` → EXTERNAL, cờ `external_unregistered`
2. Host khớp ServiceEntry (index cluster của Config Collector, có trie cho host wildcard) → EXTERNAL
3. Có `destination_service_ref` → IN_CLUSTER
4. Tên đích có dấu chấm → EXTERNAL (phương án cuối)
5. Còn lại → UNKNOWN

Cạnh chỉ có config: dùng `scope` Collector đã tính. Cạnh EXTERNAL không qua kiểm selector (mục 3b).

---

## 9. Retry & traffic bất ổn định

Nếu tỷ lệ response lỗi (4xx/5xx) cao bất thường: `unstable = true` (ngưỡng khởi tạo `error_ratio > 0.3`). Không loại bỏ dependency `unstable=true` — có traffic dù lỗi vẫn là bằng chứng có gọi. Cờ này chỉ dùng để review kỹ hơn, không ảnh hưởng classification.

---

## 10. Schema evidence đầy đủ

```jsonc
{
  "source_namespace": "default",
  "source_canonical_service": "frontend",
  "source_service_account": "frontend-sa",
  "destination_namespace": "default",
  "destination_canonical_service": "payment",
  "destination_service_ref": {"service_name": "payment", "service_namespace": "default"},
  "protocol": "TCP",
  "port": 8080,                         // null nếu port_source = "unknown"
  "port_source": "inferred_single_port",// observed | observed_log | inferred_single_port | inferred_protocol_match | config | unknown
  "scope": "IN_CLUSTER",
  "traffic_class": "BUSINESS",
  "flags": [],                          // ví dụ "claimed_monitoring", "port_unknown"

  "count": 143,
  "traffic_days": ["2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-07", "2026-10-08"],
  "distinct_days": 6,
  "error_ratio": 0.02,
  "unstable": false,

  "config_finding": "FOUND_EMBEDDED",   // từ Config Evidence Collector, null nếu không có
                                        // (chi tiết provenance/flags: bảng config_evidence,
                                        //  CONFIG_EVIDENCE_COLLECTOR.md mục 6)

  "score": 0.71,
  "dependency_status": "OBSERVED_WEAK",   // → escalate Tier 2 (AI Agent)

  "snapshot_id": "snap_0087",
  "workload_revision": "sha256:abc123..."
}
```

Chi tiết `is_injected`, `run_id`, `collection_start/end` xem `TRAFFIC_COLLECTION_PIPELINE.md`.

## 10b. Lưu trữ — bảng `dependency_evidence` (`storage.py`)

Append-only: mỗi lần chạy thêm 1 dòng cho mỗi kết quả, không ghi đè. Ba nơi đọc: stage 5 lần sau (`previous_status`, đếm số lần đổi trạng thái), Tier 2 (dòng `escalate_to_tier2 = 1`, cập nhật thêm cột kết luận Tier 2), dashboard.

**Chốt 15/10 (📐):**
- ⚠ **Sửa lỗi tra lịch sử thiếu SA**: câu SQL tìm `previous_status` chỉ so 6 phần khóa (thiếu `source_service_account`) → 2 cạnh chỉ khác SA dùng chung lịch sử. Thêm SA vào điều kiện và vào index.
- ⚠ **Sửa lỗi đọc nhầm chính mình**: câu SQL lấy "dòng mới nhất" không loại dòng cùng `snapshot_id` → chạy lại cùng 1 snapshot (vd sau khi sửa bug) đọc kết quả lần trước của chính snapshot đó làm `previous_status`. Chỉ lấy dòng của snapshot **trước** snapshot hiện tại.
- Thêm cột `snapshot_time` (thời điểm của dữ liệu), đếm số lần đổi trạng thái theo cột này thay vì `created_at` (giờ chạy code) — cần cho ablation 7 snapshot (mục 13.4), vốn chạy cả 7 lần trong vài phút.
- Thêm cột cho các quyết định 15/10: `port_source`, `traffic_days` (JSON), `flags` (JSON), `config_match_level`, `auto_allow_reason`.
- Đường đánh giá (`08_export` / `09`) hiện lưu JSON, không ghi bảng này → ablation hysteresis phải chạy đường có SQLite.

---

## 11. Escalation sang Tier 2 (AI Agent) — điều gì được mang theo

Khi escalate (`OBSERVED_WEAK`, `UNKNOWN`, hoặc `NOT_OBSERVED` với config `FOUND_UNRESOLVED`/`NOT_FOUND`), Tier 2 nhận nguyên schema mục 10 làm input — bao gồm cả phần bằng chứng traffic lẫn phần `config_finding` đã tích hợp sẵn — không phải chỉ một kết luận nhị phân. Tier 2 cần thấy `traffic_class`, `error_ratio`, `dependency_status` gốc để đối chiếu có ngữ cảnh, thay vì đánh giá lại từ đầu.

---

## 12. Điều gì KHÔNG làm trong phạm vi PoC

- Không xây engine phân loại protocol/traffic_class bằng ML — dùng rule/danh sách tĩnh (+ probe đọc từ cluster, mục 2.2).
- Không tự động xử lý mọi edge case của external dependency (rate limit, multi-region DNS...).
- Không cố phát hiện dependency qua UDP, kể cả DNS (log CoreDNS là hướng phát triển, mục 7).
- Không nhận diện công cụ monitoring ngoài Prometheus một cách tự động (Grafana, Kiali, blackbox-exporter… khai báo qua config — hướng mở rộng).
- Trọng số scoring và ngưỡng (`θ_strong`, `error_ratio`) là **giá trị khởi tạo**, không tự động học liên tục — tinh chỉnh nằm ở giai đoạn đánh giá, không phải trong phạm vi thiết kế Tier 1 này.

---

## 13. Baseline đánh giá — cô lập đóng góp của scoring đa yếu tố + hysteresis

**Phân biệt với baseline ở `PROPOSAL.md` mục 7**: 4 baseline A–D ở đó so sánh **toàn hệ thống** (Tier 1 + Tier 2) với các công cụ/phương pháp bên ngoài (Kubescape/Otterize, rule-based, LLM one-shot, LLM whole-system). Hai điểm đối chứng dưới đây nằm ở **mức component**, chỉ thay phần scoring/hysteresis (mục 4, 5b) của riêng Tier 1, giữ nguyên candidate discovery (mục 1), identity resolution (mục 3), và luật escalation (mục 5) — để trả lời một câu hỏi hẹp và trực tiếp hơn cho phần này của đồ án: *khác biệt precision/recall của Tier 1 có thật sự đến từ logistic regression + hysteresis, hay phần lớn giá trị đã nằm sẵn ở việc hợp nhất `C_observed ∪ C_potential`?*

**B1 và B2 đóng 2 vai trò khác nhau, không trình bày như 2 baseline ngang hàng**:
- **B1 = baseline đối ngoại** (external-style) — mô phỏng cách các công cụ hiện có (Kubescape/Otterize dạng đơn giản) suy luận, trả lời câu hỏi "so với hiện trạng công nghệ bên ngoài, phần scoring của Tier 1 hơn ở đâu".
- **B2 = ablation nội bộ** (internal ablation) — chính là công thức mục 4 đã tự nhận là "vấn đề gốc đã sửa" (thiết kế cũ của chính đồ án), KHÔNG phải đại diện cho một công cụ/phương pháp bên ngoài nào. Giá trị của nó là chứng minh **định lượng** được bước cải tiến nội bộ. Cần nói rõ ranh giới này khi trình bày, để tránh bị hỏi "so với chính lỗi cũ của em thì có ý nghĩa gì" — câu trả lời đúng là B2 không nhằm "thắng" ai, mà nhằm định lượng khoảng cách trước/sau khi phát hiện vấn đề ở mục 4.

### 13.1 B1 — quy tắc "có traffic là tin" (naive presence rule, baseline đối ngoại)

Bất kỳ cặp nào có `count > 0` trong cửa sổ K ngày → gán thẳng `OBSERVED_STRONG`, bỏ qua hoàn toàn sigmoid/logistic regression/hysteresis — không phân biệt `traffic_class` (không loại trừ `HEALTHCHECK`/`MONITORING`/`CONTROL_PLANE` khỏi kết luận). Cặp `count=0` vẫn xử lý qua đúng nhánh `NOT_OBSERVED` (mục 5) như Tier 1 thật, để phần không đổi này không làm nhiễu phép so sánh.

### 13.2 B2 — ngưỡng cứng cũ (ablation nội bộ, không phải baseline đối ngoại)

`count ≥ 20 AND distinct_days ≥ 3` → `OBSERVED_STRONG` với confidence gán cứng `0.9`; ngược lại xử lý như `NOT_OBSERVED`/escalate theo đúng mục 5. Trình bày trong luận văn như một **ablation study** ("nếu bỏ scoring liên tục, quay lại ngưỡng nhị phân cũ thì mất bao nhiêu recall/precision") — tách riêng khỏi kết luận "Tier 1 tốt hơn công cụ bên ngoài" (kết luận đó dựa trên B1, không phải B2).

### 13.3 Phương pháp so sánh

- Chạy B1, B2, và Tier 1 thật (đầy đủ scoring + hysteresis) trên **đúng cùng R=15 lần chạy độc lập**, cùng ground truth tự thiết kế đã dùng ở `PROPOSAL.md` mục 7 — không dựng tập dữ liệu riêng cho baseline.
- Metric: precision, recall, F1, báo cáo `mean ± CI 95%` theo đúng quy ước của mục 7.
- **Kiểm định thống kê**: vì Tier 1, B1, B2 chạy trên **đúng cùng bộ 15 trial** (không phải 3 tập số độc lập), dùng **kiểm định bắt cặp (Wilcoxon signed-rank test)** so từng cặp — Tier 1 vs B1, Tier 1 vs B2 — trên 15 cặp giá trị recall (và riêng 15 cặp giá trị precision), thay vì t-test 2 mẫu độc lập (sai giả định thống kê ở đây, bỏ phí thông tin bắt cặp) hoặc chỉ nhìn CI 95% có chồng lấn hay không bằng mắt.
- **Chỉ số chính (chốt 15/10)**: recall của **Tier 1 + Tier 2** (cạnh được kết luận đúng sau khi Tier 2 xử lý phần escalate) khi so với B1. Recall riêng Tier 1 chỉ dùng để phân tích: Tier 1 theo thiết kế không tự kết luận cạnh thưa thiếu config (ví dụ 5 request/2 ngày, không config → score ≈ 0,52 < 0,68), còn B1 đạt recall cao nhờ chấp nhận mọi cạnh có traffic, kể cả nhiễu.
- Vì nguyên tắc bất đối xứng an toàn (mục 0, mục 4) coi recall quan trọng hơn precision, báo cáo tách riêng thêm hai lát cắt:
  - **Recall trên nhóm traffic thưa** — định nghĩa cụ thể: `distinct_days ≤ 2` trong cửa sổ K ngày (dùng thẳng tiêu chí đã có sẵn, không cần đợi `count_floor` ở mục 4a được chốt chính thức) — nơi B2 dự kiến thất bại nặng nhất vì đòi hỏi cứng `distinct_days≥3`. Lưu ý: `distinct_days` tính theo hợp ngày (mục 2b) — số liệu trước 15/10 dùng max nên có thể thấp hơn thực tế.
  - **Precision trên nhóm HEALTHCHECK/MONITORING** — nơi B1 dự kiến precision thấp vì không phân loại `traffic_class` trước khi kết luận.

### 13.4 Ablation riêng cho hysteresis (mục 5b)

Biến thể "Tier 1 không hysteresis": giữ nguyên logistic regression + `θ_strong=0.6`, nhưng dùng một ngưỡng đơn thay vì cặp `θ_promote`/`θ_demote`. Đo trên một chuỗi quan sát **lặp lại theo thời gian trên cùng một kịch bản** (không phải R=15 lần độc lập khác kịch bản) tần suất đổi trạng thái, so với Tier 1 đầy đủ hysteresis. Metric ở đây là **số lần đổi trạng thái/7 ngày** (đúng định nghĩa `unstable_classification` ở mục 5b), không phải precision/recall — vì hysteresis không sửa độ chính xác của MỘT lần quan sát, mà sửa độ ổn định qua NHIỀU lần quan sát.

**NOTE — điểm mở/diễn giải**: R=15 ở `PROPOSAL.md` mục 7 là 15 lần cấy kịch bản khác nhau, không lặp lại — phù hợp đo precision/recall của B1/B2 (mục 13.3) nhưng không đủ để đo flapping (mục 13.4), vốn cần nhiều snapshot liên tiếp trên CÙNG một trial. Ablation hysteresis vì vậy cần thêm một chuỗi đo riêng ngoài R=15 chính. **Chốt 15/10: N = 7 snapshot**, cắt từ chính 7 ngày dữ liệu đã thu (snapshot k dùng dữ liệu ngày 1..k, chạy pipeline có SQLite để `previous_status` được ghi/đọc), trên 1–2 trial — không cần thu thêm dữ liệu.
