# Thiết kế — Traffic Collection Pipeline

Tài liệu này tách ra từ `TIER1_DESIGN.md` cũ, chỉ mô tả **cơ chế vật lý thu thập bằng chứng runtime** (traffic thật) — không bàn tới cách tính điểm hay ra quyết định (xem `TIER1_DESIGN.md`). Song song với `CONFIG_EVIDENCE_COLLECTOR.md` ở phía bằng chứng cấu hình — hai tài liệu này là hai nguồn input độc lập, cùng đổ vào Tier 1.

---

## 0. Vì sao dùng service mesh làm nguồn quan sát

Để suy luận dependency giữa các service mà không cần đọc hay sửa mã nguồn ứng dụng, cách khả thi nhất là quan sát traffic mạng thực tế đi qua hệ thống. Istio service mesh cung cấp cơ chế này một cách trong suốt — ứng dụng hoàn toàn không biết và không cần thay đổi gì. Cơ chế cụ thể (đổi ngày 2026-09-26, xem `infra/README.md` mục "Vì sao ambient thay vì sidecar" để biết lý do đầy đủ):

- **`ztunnel`** (DaemonSet, 1/node — không phải 1/pod): chặn toàn bộ traffic ra/vào ở tầng hệ điều hành, xử lý mTLS + đếm metric tầng L4 (`istio_tcp_*`).
- **`waypoint`** (1 Deployment dùng chung/namespace): xử lý tầng L7 (HTTP/gRPC) cho traffic tới các service trong namespace đó — đây là nơi thật sự sinh ra `istio_requests_total`.

Envoy (trong ztunnel lẫn waypoint) đếm số lượng request giữa từng cặp service dưới dạng metric — không phải log nội dung chi tiết — và Prometheus định kỳ thu thập các metric này về một kho dữ liệu trung tâm.

Hệ quả cần làm rõ: dữ liệu dựa vào là các bộ đếm tổng hợp, gắn nhãn theo danh tính service, không phải log chi tiết từng request. Việc này giữ chi phí lưu trữ ở mức thấp ngay cả với khối lượng traffic lớn, vì kích thước dữ liệu tăng theo số lượng cặp service khác nhau, không tăng theo số lượng request.

**Ràng buộc quan trọng riêng của ambient**: `ztunnel` một mình (không có `waypoint`) chỉ sinh metric L4, không parse HTTP nên KHÔNG sinh `istio_requests_total`. Cột HTTP/gRPC ở bảng mục 1 dưới đây — và do đó toàn bộ rule phân loại HEALTHCHECK ở `TIER1_DESIGN.md` mục 2 — phụ thuộc metric này, nên mọi namespace cần quan sát traffic HTTP/gRPC BẮT BUỘC phải bật `waypoint` (nhãn `istio.io/use-waypoint`), không chỉ bật ambient suông.

---

## 1. Nguồn traffic thu thập song song

Thu thập song song 3 loại, không chỉ HTTP:

| Nguồn | Metric Istio | Bắt được gì | Sinh ra ở đâu (ambient) |
|---|---|---|---|
| HTTP/gRPC | `istio_requests_total` | Request tầng ứng dụng | `waypoint` (BẮT BUỘC bật, xem mục 0) |
| TCP | `istio_tcp_connections_opened_total`, `istio_tcp_sent_bytes_total` | Postgres, Redis, Kafka — thứ HTTP counter bỏ sót hoàn toàn | `ztunnel` (mặc định, không cần waypoint) |
| DNS | `istio_dns_requests_total` (nếu bật) hoặc suy luận gián tiếp | Xem TIER1_DESIGN.md mục 7 — xử lý riêng, không đưa vào scoring | — |

---

## 2. Xử lý counter đúng cách trước khi snapshot

`istio_requests_total`/`istio_tcp_*` là **counter**, không phải gauge — cộng dồn giá trị thô sẽ sai khi proxy (ztunnel/waypoint)/pod restart (counter reset về 0).

**Không tự viết logic bù trừ reset** — dùng đúng hàm PromQL chuẩn:

```
increase(istio_requests_total{...}[7d])
```

`increase()` đã tự động xử lý reset đúng cách. Việc cần làm thêm chỉ là đảm bảo scrape không có gap quá dài (kiểm tra bằng `up{job="istio-proxy"}` trước khi tin kết quả — job name giữ nguyên dù chạy trên ztunnel/waypoint hay sidecar cũ).

---

## 3. Provenance cho traffic test/injected

**Vấn đề gốc đã sửa**: nếu chỉ dán nhãn `is_injected` sau khi quan sát, không có cách nào phân biệt chắc chắn traffic thật với traffic test đã cấy vào cùng hệ thống.

**Thiết kế ban đầu — chủ động tạo dấu vân tay, không đoán ngược**: bộ test harness (dùng khi đánh giá framework) chạy từ pod riêng có label `test-harness: "true"`, hoặc đính kèm HTTP header đặc biệt (`X-Injected-Test: true`) vào mọi request test. Khi query Prometheus, lọc theo label/header này để tách bạch **tuyệt đối** traffic test khỏi traffic production ngay từ nguồn — không cần suy luận sau.

Đây chính là cơ chế dùng để tự thiết kế kịch bản test (dependency thưa, health check nhiễu...) trên Online Boutique đã chốt — traffic được cấy vào là traffic **thật**, chạy qua Envoy thật (ztunnel/waypoint), không phải số liệu tự bịa.

Schema evidence mở rộng thêm:
```
snapshot_id, experiment_id, run_id, is_injected, injection_id
collection_start, collection_end
workload_revision   (image digest / rollout revision — xem mục 4)
```

**CẬP NHẬT — ĐÃ XÁC NHẬN THẬT (chạy trên máy Bin, 2026-09-27, trial-01, 15 phút traffic thật qua waypoint): cơ chế label ở trên KHÔNG hoạt động dưới Istio ambient, đây là giới hạn kiến trúc thật của Istio (không phải lỗi cấu hình của đồ án).**

Query trực tiếp Prometheus cho thấy mọi series `istio_requests_total{reporter="waypoint"}` trả về đúng literal `"unknown"` cho 2 nhãn `test_harness`/`injection_id` (không phải rỗng/absent — nghĩa là `tagOverrides` CÓ chạy, chỉ là attribute expression `source.labels[...]` không đọc được nhãn Pod thật). Xác nhận qua chính comment trong source code Istio: *"Source label matching is not supported in ambient mode, and thus not supported in waypoint routes"* ([istio/istio#61590](https://github.com/istio/istio/issues/61590), lý do thiết kế tại [istio/istio#51565](https://github.com/istio/istio/issues/51565)). Nguyên nhân gốc: cơ chế "Metadata Exchange" (trao đổi toàn bộ node labels giữa 2 proxy qua ALPN) mà `source.labels[...]` phụ thuộc chỉ tồn tại giữa 2 **sidecar** trong kiến trúc Istio cũ — `ztunnel`/`waypoint` (ambient) không cài lại cơ chế này.

Các attribute **built-in** (không phải `source.labels[...]` tuỳ ý) KHÔNG bị ảnh hưởng — `source_workload`, `source_canonical_service`, `source_principal` (SPIFFE), và `request.url_path` đều resolve ĐÚNG giá trị thật qua waypoint (đã xác nhận cùng lần chạy), vì chúng đến từ tra cứu SPIFFE identity ở istiod, không qua Metadata Exchange.

**Đánh giá tác động — KHÔNG chặn pipeline chấm điểm thật**: `tier1_inference/tier1/evaluation.py` (precision/recall/F1) chỉ so khớp theo `IdentityKey.pair_key()` (namespace + canonical_service 2 phía) với `ground_truth` đã biết SẴN ngay từ lúc sinh kịch bản (`scenario_pool.py`, biết trước khi deploy) — KHÔNG hề đọc `is_injected`/`injection_id` từ Prometheus. Với kiến trúc hiện tại (mỗi trial 1 namespace K8s riêng — đã chạy thật end-to-end thành công cho `trial-01`), `source_workload_namespace`/`destination_workload_namespace` (đã xác nhận resolve đúng) tự nó đã đủ để phân biệt trial nào sinh ra traffic nào — vai trò của `injection_id` trong thiết kế gốc thực ra chỉ cần thiết cho phương án "hạ tầng dùng chung" (nhiều trial ảo cùng chia sẻ 1 namespace, xem `infra/README.md` mục "Việc còn thiếu") — phương án đó CHƯA triển khai, nên giới hạn này hiện tại không có tác động thật tới kết quả đồ án.

`is_injected`/`injection_id` vẫn giữ nguyên trong schema `TrafficSeries` (không xoá field, không phải rewrite) — luôn nhận giá trị mặc định an toàn (`is_injected=False`, `injection_id=None`) dưới ambient, và sẵn sàng dùng lại ngay nếu sau này quay lại sidecar hoặc Istio bổ sung cơ chế tương đương cho ambient. `manifests/telemetry-custom-labels.yaml` đã bỏ 2 `tagOverrides` `test_harness`/`injection_id` (không có cách nào khiến chúng hoạt động), chỉ giữ `request_path`.

---

## 4. Temporal drift — khi migration diễn ra giữa lúc quan sát

Deploy mới giữa 2 lần chạy có thể tạo/xóa dependency mà snapshot cũ không phản ánh. Mỗi record gắn `workload_revision` (image digest hoặc rollout revision number). Khi phát hiện revision đổi so với lần quan sát trước, dependency liên quan tự động chuyển về "cần re-verify" thay vì mặc định coi vẫn đúng.

---

## 5. Pipeline vật lý tổng thể

Toàn bộ đường đi vật lý của dữ liệu, từ một request thật giữa hai service, tới bước lọc nhiễu và đóng băng thành snapshot cố định:

```
1. Request từ service A đi ra, được ztunnel trên node của A chặn lại (tầng hệ điều hành,
   không qua sidecar trong pod) → chuyển tới ztunnel/waypoint phía service B (waypoint nếu
   B cần xử lý L7, ztunnel thẳng nếu chỉ cần L4) → thực hiện xác thực mTLS

2. Proxy (ztunnel cho L4, waypoint cho L7) expose số liệu này ở endpoint public
   /stats/prometheus, kèm thông tin source/destination

3. Prometheus thu thập dữ liệu từ các endpoint này về, lưu vào TSDB

4. Vì TSDB có hạn lưu trữ, phải query dữ liệu cần phân tích về — dùng increase()
   để xử lý đúng case pod bị reset (mục 2)

5. Filter dữ liệu, loại noise (system, healthcheck...), gắn tag run_id (phân
   biệt các lần chạy thí nghiệm R=15) và is_injected (provenance, mục 3)

6. Đóng gói thành 1 bảng SQLite riêng cho lần chạy (trial) này — nguồn dữ liệu
   chung dùng lại cho mọi baseline so sánh TRONG CÙNG lần chạy, đảm bảo so
   sánh công bằng; không cần query lại Prometheus cho việc so baseline.
   Xem mục 6 bên dưới: mỗi lần trong R=15 lần vẫn cần lặp lại toàn bộ pipeline
   này, vì mỗi lần có kịch bản cấy khác nhau — "không query lại" chỉ áp dụng
   trong nội bộ một lần chạy, không áp dụng giữa các lần chạy với nhau.
```

---

## 6. Vận hành thí nghiệm — R=15 lần chạy nghĩa là gì, đóng băng ở cấp độ nào

**Sửa lại một hiểu lầm đã từng có trong bản trước**: R=15 **không phải** chạy lại 15 lần trên cùng một snapshot cố định. R=15 nghĩa là chạy lại **toàn bộ thí nghiệm** 15 lần, và **mỗi lần cấy một kịch bản dependency ẩn ngẫu nhiên khác nhau**. Lý do: Online Boutique chỉ có ~10-12 service — nếu chỉ đo 1 lần, kết quả precision/recall báo cáo phụ thuộc quá nhiều vào việc "tình cờ cấy trúng case dễ hay khó". Chạy 15 lần với 15 kịch bản cấy khác nhau → ra 15 con số precision/recall riêng biệt → báo cáo `mean ± CI 95%`, đo được **độ tin cậy của chính con số đánh giá**, không phải may rủi của một lần đo.

**Hệ quả trực tiếp**: vì mỗi lần trong 15 lần có kịch bản cấy khác nhau (service/dependency giả khác nhau), **cả traffic lẫn config đều phải thu thập lại mới cho từng lần** — không thể dùng chung 1 snapshot đóng băng cho cả 15 lần, vì snapshot cũ đơn giản là không phản ánh kịch bản mới. Đây chính là lý do `run_id` tồn tại trong schema (mục 3) — để tách biệt dữ liệu của 15 lần chạy khác nhau; nếu cả 15 lần dùng chung 1 snapshot, `run_id` sẽ không có tác dụng phân biệt gì cả.

**"Đóng băng" chỉ đúng ở một cấp độ hẹp hơn — trong nội bộ MỘT lần chạy**: sau khi đã thu thập xong traffic + config cho lần chạy thứ i, snapshot của lần đó được đóng băng và dùng chung cho **mọi baseline so sánh trong cùng lần chạy đó** (ví dụ so sánh công thức scoring của mình với một baseline ngưỡng cứng, trên cùng 1 dữ liệu) — đảm bảo so sánh công bằng trong nội bộ 1 trial, không phải không query lại Prometheus/K8s API giữa 15 trial với nhau.
