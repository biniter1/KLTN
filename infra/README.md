# infra/ — Hạ tầng thu dữ liệu thật cho R=15 trial (PROPOSAL.md mục 7)

Thư mục này dựng 1 cluster Kubernetes cục bộ (laptop, Windows+WSL2+Docker Desktop),
cấy 15 kịch bản dependency ẩn khác nhau (mỗi kịch bản 1 namespace/trial), chạy Online
Boutique thật + traffic cấy trong 1 cửa sổ 7 ngày **chung duy nhất** (song song, không
chạy tuần tự 15×7 ngày — quyết định đã chốt khi thiết kế phần này), rồi thu traffic/config
thật bằng đúng code đã có + test đầy đủ ở `tier1_inference/` (`config_collector.py`,
`traffic_collector.py`) để chạy `tier1.experiment.run_experiment()` ra kết quả cuối
(`TIER1_DESIGN.md` mục 13).

**Mesh mode: Istio ambient (ztunnel + waypoint) — KHÔNG dùng sidecar** (đổi ngày
2026-09-26, xem "Vì sao ambient thay vì sidecar" bên dưới). Có **2 model triển khai**,
cả hai đã implement + test đầy đủ:

- **Namespace riêng/trial** (`05_deploy_trial.sh` + `06_deploy_all_trials.sh`, 15
  namespace, mỗi namespace 1 bản Online Boutique thật riêng) — model gốc, đơn giản, cách
  ly hoàn toàn từng trial, nhưng **ĐÃ XÁC NHẬN THẬT chỉ chạy nổi tối đa 4/15 trial đồng
  thời** trên laptop thường (`check_capacity.sh 15`, xem "CẬP NHẬT 2026-09-27" ngay dưới).
- **Dùng chung hạ tầng** (`05_deploy_shared.sh`, MỘT namespace duy nhất, 1 Online Boutique
  thật + ~83 harness Deployment chia sẻ theo `pair_key()` giữa 15 "virtual trial") — **ĐÃ
  IMPLEMENT XONG** (2026-09-27), giải quyết đúng giới hạn capacity ở trên: hạ tổng REQUEST
  từ ~25.65 vCPU/23.41 GiB xuống ~2.27 vCPU/2.96 GiB cho CẢ 15 trial, vì app + waypoint chỉ
  chạy 1 LẦN thay vì ×15. Đây là hướng **được khuyến nghị** khi máy không đủ chạy 15
  namespace riêng đồng thời — xem mục "Kiến trúc dùng chung hạ tầng" bên dưới.

## Đọc trước khi chạy bất cứ gì

**Toàn bộ nội dung trong `scripts/`, `manifests/`, `scenarios/` (trừ `scenario_pool.py`,
`render_manifests.py`, phần thuần Python của `08_export_trial_data.py`, và test của chúng)
được soạn trong 1 sandbox KHÔNG có cluster/Prometheus/Istio thật.** `scenario_pool.py` và
`render_manifests.py` có test đầy đủ (thuần Python, không cần cluster — 46 test, xem
`tests/`, gồm cả 19 test riêng cho kiến trúc "dùng chung hạ tầng" — xem mục riêng bên
dưới) và output YAML đã được kiểm tra kỹ (parse hợp lệ, tên tài nguyên đúng RFC 1123, cạnh
NOISE_HEALTHCHECK sinh đúng traffic HTTP mang path health-check thật, namespace mang đúng
nhãn ambient — các lỗi cụ thể đã tìm thấy và sửa qua việc tự chạy thử, xem "Đã tìm & sửa"
bên dưới). Phần CÒN LẠI (script bash gọi
`kubectl`/`istioctl`, cú pháp Istio Telemetry API, giả định ServiceAccount, hành vi ambient
cụ thể trên `kind`) là thiết kế tốt nhất có thể làm mà không có cluster để chạy thử —
ban đầu **chưa được xác nhận chạy đúng trên cluster thật**, trừ phần cổng/Service Online
Boutique và số liệu request mặc định của ztunnel (đã đối chiếu trực tiếp với nguồn công khai,
xem "Đã đối chiếu với thật" bên dưới). Kỳ vọng hợp lý: sẽ cần debug/sửa nhỏ khi chạy lần đầu —
không phải vì thiết kế sai, mà vì chưa có cách xác minh 100% ở đây.

**CẬP NHẬT 2026-09-27 — Bin đã tự chạy thật `00`→`07` cho 1 trial (`trial-01`), verify SẠCH,
kể cả sau 15 phút traffic**: toàn bộ pipeline chạy được trên cluster thật (kind trên
WSL2/Docker Desktop), qua các lỗi thật đã tìm/sửa dọc đường (xem "Đã tìm & sửa" #4, #5, #6).
Sau khi sửa: `istioctl install --set profile=ambient` chạy sạch, deploy `trial-01` xong
19/19 pod `1/1 Running` (kể cả `waypoint`), `07_verify.sh trial-01` báo OK cho cả 3 mục bắt
buộc (ztunnel, pod+waypoint, Prometheus), và sau 15 phút traffic thật: `request_path` lên
đúng giá trị thật (path thật của CheckoutService/cart/...), NHƯNG `test_harness`/
`injection_id` không bao giờ lên — điều tra kỹ (query Prometheus thô) cho thấy đây là giới
hạn kiến trúc THẬT của Istio ambient (đã xác nhận qua source Istio, xem "CHƯA xác nhận được"
#2), không phải chưa đợi đủ lâu — đã sửa xong (bỏ 2 tagOverrides đó, xem "Đã tìm & sửa" #6),
và xác nhận KHÔNG ảnh hưởng kết quả chấm điểm cuối của đồ án. Đây là lần đầu tiên cả pipeline
được xác nhận chạy đúng end-to-end trên cluster thật — các mục còn lại trong "CHƯA xác nhận
được" bên dưới (4, 5, 6, 7) vẫn đang mở, nhưng KHÔNG còn là "chưa chạy thử gì" nữa.

## Vì sao ambient thay vì sidecar

- **Sidecar** (bản trước): mỗi pod tự mang 1 Envoy riêng → chi phí mesh TĂNG TUYẾN TÍNH
  theo tổng số pod (15 trial × ~18 pod/trial = 270 sidecar, ~27 vCPU/34 GiB chỉ riêng phần
  mesh — lý do bản trước phải có script trim request sidecar cho từng pod).
- **Ambient**: `ztunnel` (mTLS + L4) chạy dạng DaemonSet — CHỈ 1 pod/**node** cho cả cluster,
  không nhân theo số pod/trial. Phần xử lý L7 (bắt buộc để có `istio_requests_total`, xem
  ngay dưới) tách riêng ra `waypoint` — 1 proxy dùng CHUNG cho cả 1 **namespace**, không
  phải 1:1 theo pod. Kết quả: chi phí mesh gần như CỐ ĐỊNH dù namespace có bao nhiêu pod.
- **Đánh đổi phải biết**: ambient THUẦN (chỉ ztunnel, không waypoint) CHỈ sinh metric L4
  (`istio_tcp_*`) — KHÔNG sinh `istio_requests_total` vì ztunnel không parse HTTP. Toàn bộ
  rule phân loại HEALTHCHECK và cột HTTP/gRPC ở `TRAFFIC_COLLECTION_PIPELINE.md` mục 1 phụ
  thuộc `istio_requests_total` → bắt buộc dùng ambient **+ waypoint**, không phải ambient
  thuần (xem `scripts/03_install_istio.sh`, `scripts/05_deploy_trial.sh`).
- Danh tính/mTLS/SPIFFE (`serviceAccountName`, canonical-name label) KHÔNG đổi giữa sidecar
  và ambient — Identity Resolution ở `TIER1_DESIGN.md` mục 3 không bị ảnh hưởng.
- **Quan trọng — ambient KHÔNG một mình giải quyết được bài toán capacity**: nó cắt gần hết
  chi phí *mesh proxy*, nhưng phần chiếm tài nguyên LỚN NHẤT khi chạy 15 namespace riêng là
  chính **app container** (11 service Online Boutique thật × 15 bản sao) — phần này không
  liên quan gì đến mesh mode. Xem `scripts/check_capacity.sh` để có con số cụ thể — kiến
  trúc "dùng chung hạ tầng" (ĐÃ implement, xem mục riêng bên dưới) mới thật sự giải quyết
  phần đó.

## Kiến trúc tổng quan (model hiện tại — namespace riêng/trial)

```
Laptop (Windows + WSL2 + Docker Desktop)
└── kind cluster "kltn-eval" (1 control-plane + 2 worker, max-pods=250/node)
    └── Istio ambient (profile=ambient: istiod + istio-cni + ztunnel DaemonSet)
        + Prometheus addon + Telemetry CR (nhãn tuỳ chỉnh, mesh-wide)
        ├── namespace trial-01 (dataplane-mode=ambient, use-waypoint=waypoint)
        │     ├── waypoint (1 Deployment, xử lý L7 CHO CẢ namespace)
        │     ├── Online Boutique thật (11 service + loadgenerator, 1/1 pod — KHÔNG sidecar)
        │     └── 6 harness Deployment (1/ScenarioKind cấy thêm, 1/1 pod)
        ├── namespace trial-02 ── (y hệt, kịch bản cấy KHÁC — seed riêng)
        ├── ...
        └── namespace trial-15
                │
                ▼ (7 ngày quan sát thật, traffic thật đi qua ztunnel + waypoint)
        Prometheus (istio_requests_total — từ waypoint,
                    istio_tcp_connections_opened_total — từ ztunnel, up)
                │
                ▼ 08_export_trial_data.py (KubernetesReader + HttpPrometheusClient)
        trial_data/trial-XX.json (TrafficSeries + ConfigEvidenceRecord + ground_truth)
                │
                ▼ 09_run_experiment.py (tier1.experiment.run_experiment)
        Tier 1 vs B1 vs B2 — precision/recall/f1 mean±CI95%, Wilcoxon p-value
```

## Thứ tự chạy

| # | Script | Việc gì | Chạy ở đâu |
|---|--------|---------|------------|
| 00 | `scripts/00_check_prereqs.sh` | Kiểm tra Docker/RAM/CPU/disk, tool đã cài chưa | WSL2 |
| 01 | `scripts/01_install_tools.sh` | Cài `kubectl`/`kind`/`istioctl` | WSL2 |
| 02 | `scripts/02_create_cluster.sh` | Tạo cluster `kltn-eval` (3 node, max-pods=250) | WSL2 |
| 03 | `scripts/03_install_istio.sh` | Cài Gateway API CRD + Istio (`profile=ambient`) + Prometheus addon | WSL2 |
| 04 | `scripts/04_apply_telemetry_customization.sh` | Áp nhãn tuỳ chỉnh `request_path` (mesh-wide, có hiệu lực trên mọi waypoint tạo sau — KHÔNG còn `test_harness`/`injection_id`, xem "CHƯA xác nhận được" #2) | WSL2 |
| — | `scripts/check_capacity.sh [n_trials]` | Ước tính request cần vs tài nguyên thật đang có — chạy TRƯỚC 05/06 nếu chưa chắc máy đủ | WSL2, chạy lại bất cứ lúc nào |
| 05 | `scripts/05_deploy_trial.sh <trial-id> [seed]` | **[Model namespace riêng]** Deploy 1 trial (namespace ambient + waypoint + Online Boutique + harness cấy) | WSL2 |
| 05b | `scripts/05b_trim_waypoint_resources.sh <trial-id>` | (Tuỳ chọn) hạ request của Deployment `waypoint` — xem lưu ý trong chính script | WSL2 |
| 06 | `scripts/06_deploy_all_trials.sh` | **[Model namespace riêng]** Deploy CẢ 15 trial (mặc định đồng thời) — CHỈ chạy nổi tối đa 4/15 đồng thời trên laptop thường, xem cảnh báo đầu file | WSL2 |
| **05'** | **`scripts/05_deploy_shared.sh [namespace] [n_trials] [base_seed]`** | **[Model dùng chung hạ tầng — khuyến nghị]** Deploy 1 namespace DUY NHẤT chứa 1 Online Boutique thật + toàn bộ harness cho N virtual trial | WSL2 |
| — | `scripts/07_verify.sh [trial-id ...]` | Sanity check: ztunnel + waypoint + pod 1/1 + Prometheus sống + nhãn đã lên chưa (dùng cho cả 2 model) | WSL2, chạy lại bất cứ lúc nào |
| — | **Đợi đủ 7 ngày quan sát thật** kể từ khi pod harness đầu tiên Running | | |
| 08 | `scripts/08_export_trial_data.py [trial-id...\|--all]` | **[Model namespace riêng]** Thu traffic/config thật → JSON/trial | WSL2 (cần `pip install kubernetes requests`) |
| 08' | `scripts/08_export_trial_data.py --shared --shared-namespace <ns> --owner-map <path>` | **[Model dùng chung hạ tầng]** 1 lần thu thập chung → tách thành N file JSON/virtual trial theo `pair_key()` | WSL2 |
| 09 | `scripts/09_run_experiment.py` | Chạy Tier 1 vs B1 vs B2 trên toàn bộ trial, in + ghi báo cáo (giống nhau cho cả 2 model — chỉ khác cách sinh `trial_data/`) | WSL2 (cần đúng `tier1_inference/` cạnh `infra/`, hoặc `--tier1-path`) |

Mỗi script in ra bước tiếp theo cần làm ở cuối — không cần nhớ thứ tự. Chọn MỘT trong 2
hàng "05" ở trên tuỳ model (không chạy cả hai vào cùng 1 cluster).

## Bố cục thư mục

```
infra/
├── scripts/           00-09 + 05_deploy_shared.sh, thứ tự chạy như bảng trên
├── manifests/          kind-cluster-config.yaml, telemetry-custom-labels.yaml
├── scenarios/           scenario_pool.py (sinh kịch bản, ĐÃ TEST), render_manifests.py (ĐÃ TEST)
├── tests/               46 test cho scenario_pool.py + render_manifests.py + export_shared (pytest)
├── ground_truth/        (tự sinh khi chạy 05/06 hoặc 05_deploy_shared.sh) — 1 file JSON ground truth / (virtual) trial
└── trial_data/          (tự sinh khi chạy 08) — 1 file JSON traffic+config thật / (virtual) trial
```

Chạy `python3 -m pytest tests/` trong thư mục `infra/` để xác nhận `scenario_pool.py`/
`render_manifests.py`/`08_export_trial_data.py` (phần thuần, không đụng cluster thật) vẫn
đúng trước khi deploy (không cần cluster).

## Đã đối chiếu với thật (không phải đoán)

- Manifest gốc Online Boutique (`GoogleCloudPlatform/microservices-demo`, nhánh `main`,
  `release/kubernetes-manifests.yaml`): cổng Service của cả 11 service khớp 100% với
  `scenario_pool.py::SERVICE_PORTS` (vd `cartservice:7070`, `emailservice:5000` — LƯU Ý
  emailservice có containerPort 8080 nhưng Service port là 5000, khác nhau); 10/11 service
  (trừ `redis-cart`) đã có sẵn `ServiceAccount` riêng cùng tên; manifest gốc có kèm
  `loadgenerator` tự sinh traffic thật vào `frontend` (nguồn traffic cho cạnh nền
  `ONLINE_BOUTIQUE_REAL_EDGES`, không cần tự viết gì thêm).
- **Request mặc định của `ztunnel`**: xác nhận trực tiếp từ Helm chart chính thức
  (`istio/istio` repo, `manifests/charts/ztunnel/values.yaml`, mục
  `_internal_defaults_do_not_set`) — **200m CPU / 512Mi RAM/pod** (chỉ có request, không
  có limit mặc định). Số này dùng trong `check_capacity.sh`.
- Lệnh cài đặt/nhãn ambient (`profile=ambient`, `istio.io/dataplane-mode=ambient`,
  `istioctl waypoint apply --enroll-namespace`, `istio.io/use-waypoint=waypoint`, và bước
  cài Gateway API CRD bắt buộc trước khi tạo waypoint) — đối chiếu trực tiếp trang hướng
  dẫn chính thức Istio (`istio.io/latest/docs/ambient/getting-started/`,
  `.../ambient/usage/waypoint/`, `.../ambient/usage/add-workloads/`), không phải đoán cú
  pháp.
- **Cluster K8s phải >=1.32 (KHÔNG PHẢI >=1.31) để cài Gateway API v1.6.0** — 2 CRD của nó
  dùng hàm CEL: `TLSRoute` dùng `isIP()` (thư viện `library.IP`, `IntroducedVersion: 1.30`
  trong source Kubernetes), `XBackend` dùng `format.dns1123Label()` (thư viện
  `library.Format`, `IntroducedVersion: 1.31`) — xác nhận trực tiếp từ source
  `k8s.io/apiserver/pkg/cel/environment/base.go` (nhánh `release-1.31`/`release-1.32` trên
  GitHub `kubernetes/kubernetes`). ĐIỂM DỄ HIỂU SAI: môi trường CEL dùng để validate CRD
  KHÔNG dùng đúng version cluster đang chạy, mà mặc định lùi lại 1 bậc ("n-1", để CEL rule
  portable qua nhiều bản trong lúc rolling upgrade HA control-plane — xác nhận qua doc chính
  thức `pkg.go.dev/k8s.io/apiserver/pkg/cel/environment`, hàm `DefaultCompatibilityVersion`).
  Vậy cluster chạy 1.31 thì môi trường CEL cư xử như 1.30 → `Format` (introduced@1.31) CHƯA
  bật, dù `IP` (introduced@1.30) đã bật — PHẢI chạy thật >=1.32 thì n-1=1.31 mới đủ.
  PHÁT HIỆN THẬT khi Bin tự chạy trên máy (2026-09-27), qua ĐÚNG 2 lần thử-sai: lần 1 (K8s
  1.30.0, do `kind` CLI cũ v0.23.0 mặc định) → lỗi "undeclared reference to 'isIP'"; lần 2
  (ghim tưởng-đủ K8s 1.31.14) → lỗi tiếp "undeclared reference to 'format'". Đã sửa (ghim
  1.32.11), xem mục "Đã tìm & sửa" #4 bên dưới.

## CHƯA xác nhận được (cần bạn kiểm tra khi có cluster thật)

~~1. Cú pháp Istio Telemetry API (`request.url_path`)~~ — **ĐÃ XÁC NHẬN THẬT**: cú pháp
   `tagOverrides` với `request.url_path` chạy đúng, thấy giá trị path thật (vd
   `/hipstershop.CheckoutService/PlaceOrder`, `/cart`) trong `istio_requests_total` (Bin tự
   chạy, 2026-09-27, 15 phút traffic qua waypoint). Riêng `source.labels['test-harness']`/
   `source.labels['injection-id']` đã BỎ khỏi manifest — xem mục 2 ngay dưới, đây KHÔNG phải
   lỗi cú pháp mà là giới hạn kiến trúc thật.
~~2. `source.labels[...]` có tự resolve đúng qua waypoint không~~ — **ĐÃ XÁC NHẬN LÀ KHÔNG,
   VÀ ĐÂY LÀ GIỚI HẠN THẬT CỦA ISTIO AMBIENT, KHÔNG PHẢI LỖI CẤU HÌNH**: query Prometheus thật
   (Bin, 2026-09-27, trial-01) cho thấy MỌI series `istio_requests_total{reporter="waypoint"}`
   trả về literal `"unknown"` cho cả `test_harness` lẫn `injection_id` — không phải rỗng, nghĩa
   là `tagOverrides` CÓ chạy, chỉ là `source.labels[...]` không đọc được nhãn Pod thật qua
   waypoint. Xác nhận qua chính comment trong source Istio: *"Source label matching is not
   supported in ambient mode, and thus not supported in waypoint routes"*
   ([istio/istio#61590](https://github.com/istio/istio/issues/61590), lý do thiết kế ở
   [istio/istio#51565](https://github.com/istio/istio/issues/51565)) — cơ chế "Metadata
   Exchange" (trao đổi labels qua ALPN) mà `source.labels[...]` cần chỉ tồn tại giữa 2
   **sidecar**, ztunnel/waypoint không có. Attribute built-in (`source_workload`,
   `source_canonical_service`, `request.url_path`) KHÔNG bị ảnh hưởng, vẫn đúng qua waypoint.
   **Không chặn kết quả đồ án**: `evaluation.py` chấm điểm chỉ dựa vào `pair_key()` +
   `ground_truth` đã biết sẵn, không đọc `is_injected`/`injection_id` từ Prometheus — xem
   `TRAFFIC_COLLECTION_PIPELINE.md` mục 3 (đã cập nhật đầy đủ) và "Đã tìm & sửa" #6 dưới đây.
   Đã bỏ 2 `tagOverrides` này khỏi `manifests/telemetry-custom-labels.yaml` và mục `[4/4]` của
   `07_verify.sh` (không có cách nào khiến chúng pass dưới ambient).
~~3. Ambient trên `kind` (CNI mặc định kindnet)~~ — **ĐÃ XÁC NHẬN THẬT, KHÔNG CÒN LÀ RỦI RO**:
   chạy thật trên máy Bin (2026-09-27, kind v0.31.0 + node image v1.32.11 + Istio tải qua
   `01_install_tools.sh`) — `istioctl install --set profile=ambient` chạy xong sạch
   ("Ztunnel installed", "CNI installed", "enjoy Istio without sidecars!"), toàn bộ pod
   `istio-system` Running/Ready (`istio-cni-node` 3/3 đúng 1/node, `ztunnel` 3/3 DaemonSet).
   KHÔNG gặp lại bug `istio/istio#41058` (lỗi lấy host IP) — có vẻ đã được vá trên bản Istio
   hiện dùng. Giữ lại mục này chỉ để ghi nhận đã kiểm chứng, không cần tra `istioctl x
   precheck` cho lý do này nữa.
4. **Request mặc định thật của Deployment `waypoint`** — không tìm được số liệu chính thức
   công khai; `check_capacity.sh`/`05b_trim_waypoint_resources.sh` tạm dùng mức
   `global.proxy.resources` mặc định chung của Istio (100m/128Mi, cùng mức đã xác nhận cho
   sidecar/gateway ở các bản trước) làm ước tính — kiểm tra thật bằng `kubectl get deploy
   waypoint -n <trial-id> -o jsonpath='{.spec.template.spec.containers[0].resources}'`.
5. **ztunnel có chạy trên CẢ 3 node kind (kể cả control-plane) không** — `check_capacity.sh`
   giả định có, ảnh hưởng số ước tính "chi phí cố định"; xác nhận bằng `kubectl get pods -n
   istio-system -l app=ztunnel -o wide`.
6. **gRPC reflection có thật sự sinh ra `istio_requests_total` hợp lệ không** — giả định
   Online Boutique bật gRPC server reflection mặc định, nhưng chưa gọi thử `grpcurl` vào 1
   pod thật (không đổi so với bản sidecar cũ, không liên quan ambient).
7. **Tài nguyên laptop cho 15 trial cùng lúc** — xem mục "Sizing" bên dưới, ước tính, không
   phải số đo thật.

## Đã tìm & sửa (qua tự chạy thử, không cần cluster)

Trước khi giao thư mục này, đã tự chạy `render_manifests.py` và viết test cấu trúc
(`tests/test_render_manifests.py`) để bắt lỗi tĩnh — tìm thấy 3 lỗi thật:

1. **`trial_id` dạng `trial_01` (gạch dưới) không phải tên namespace K8s hợp lệ** —
   Kubernetes chỉ chấp nhận RFC 1123 label (chữ thường/số/gạch NGANG). Đã sửa
   `scenario_pool.py::generate_all_trials()` sang `trial-01`, và thêm kiểm tra
   (`render_manifests.py::_validate_k8s_name`, cả `05_deploy_trial.sh`) để báo lỗi RÕ
   RÀNG ngay lập tức nếu ai đó lỡ dùng lại dấu gạch dưới.
2. **NOISE_HEALTHCHECK có thể cấy vào dependency GRPC/TCP** — `classification.py` (Tier 1
   thật) CHỈ nhận diện HEALTHCHECK qua `series.path`, chỉ quan sát được qua 1 request HTTP
   thật. Đã sửa `scenario_pool.py` để đích của NOISE_HEALTHCHECK LUÔN LÀ `frontend` (service
   HTTP duy nhất), đảm bảo traffic cấy mang đúng path.
3. **Namespace vẫn dùng nhãn `istio-injection: enabled` (sidecar) sau khi quyết định đổi
   sang ambient** — đã sửa `render_manifests.py`/`05_deploy_trial.sh` sang
   `istio.io/dataplane-mode: ambient` + thêm bước `istioctl waypoint apply
   --enroll-namespace`; `07_verify.sh` cũng sửa lại check "2/2 Ready" (đúng cho sidecar) sai
   thành "1/1 Ready" (đúng cho ambient — không còn container sidecar thứ 2 trong pod).
4. **Gateway API CRD apply lỗi CEL "undeclared reference to 'isIP'"/"format" trên kind K8s
   <1.32** — phát hiện THẬT lúc Bin tự chạy trên máy (2026-09-27, xem mục "Đã đối chiếu với
   thật" ở trên — bao gồm cả cơ chế "n-1" dễ hiểu sai). Đã sửa bằng cách ghim cứng
   `image: kindest/node:v1.32.11` cho cả 3 node trong `manifests/kind-cluster-config.yaml`
   (đọc comment chi tiết trong file đó để tự áp dụng lại quy tắc "n-1" nếu gặp lỗi CEL tương
   tự với hàm khác sau này) + `01_install_tools.sh` giờ tự kiểm tra và nâng cấp `kind` CLI lên
   >=v0.31.0 nếu thấy bản cũ hơn (bản kind chính thức công bố đã test cùng node image
   v1.32.11).
5. **Harness pod cấy vào dest GRPC bị CrashLoopBackOff/RunContainerError** — phát hiện THẬT
   lúc Bin deploy `trial-01` (2026-09-27): đúng 3/6 harness pod (`STRONG_HIDDEN`,
   `SPARSE_HIDDEN`, `NOISE_MONITORING` — cả 3 lần này đều rơi trúng dest GRPC) crash ngay,
   trong khi `NOISE_HEALTHCHECK` (dest luôn là `frontend`, HTTP) và 2 cạnh `CONFIG_ONLY_*`
   (không sinh traffic) chạy bình thường. Nguyên nhân: ảnh `fullstorydev/grpcurl:latest`
   (Docker Hub) build `FROM scratch` (xác nhận qua Dockerfile chính thức trên GitHub) — chỉ
   có đúng `/bin/grpcurl` + ca-certificates, KHÔNG có `/bin/sh`, trong khi `wrapper.sh` (vòng
   lặp lịch chạy theo `ACTIVE_DAYS`, dùng chung cho MỌI harness pod có traffic) bắt buộc cần
   `sh` để chạy. Đã sửa: đổi ảnh dùng cho dest GRPC sang
   `ghcr.io/fullstorydev/grpcurl:latest-alpine` (build từ `alpine:3`, có sẵn `sh` +
   `grpcurl` trên PATH — xác nhận qua chính workflow release CI của dự án grpcurl,
   `.github/workflows/release.yml`, publish CẢ 2 target Dockerfile "scratch"/"alpine" lên
   `ghcr.io`) trong `scenarios/render_manifests.py::_image_for_protocol`. Thêm test
   `test_traffic_generating_images_are_shell_capable` để khoá lại phát hiện này.
6. **Nhãn tuỳ chỉnh `test_harness`/`injection_id` không bao giờ đọc được nhãn Pod thật qua
   waypoint** — phát hiện THẬT khi Bin query Prometheus trực tiếp sau 15 phút traffic
   (2026-09-27, trial-01): mọi series trả về literal `"unknown"`. Đây LÀ giới hạn kiến trúc
   thật của Istio ambient (đã xác nhận qua source code Istio, xem mục 2 "CHƯA xác nhận được"
   ở trên), KHÔNG PHẢI lỗi cú pháp/cấu hình — không có cách sửa nào ở tầng YAML/code của đồ
   án khiến nó hoạt động. Đã sửa bằng cách BỎ 2 `tagOverrides` này khỏi
   `manifests/telemetry-custom-labels.yaml` (chỉ giữ `request_path`, cái vẫn hoạt động đúng),
   cập nhật lại mục `[4/4]` của `07_verify.sh` để kiểm tra `request_path` thay vì
   `test_harness`, và cập nhật docstring `tier1_inference/tier1/traffic_collector.py` +
   `TRAFFIC_COLLECTION_PIPELINE.md` mục 3 để ghi rõ: field `is_injected`/`injection_id` vẫn
   giữ nguyên trong schema (không xoá, luôn `False`/`None` dưới ambient) nhưng KHÔNG được
   `evaluation.py` dùng để chấm điểm — pipeline chấm điểm thật dựa hoàn toàn vào
   `pair_key()` so với `ground_truth` đã biết trước, nên giới hạn này không ảnh hưởng kết
   quả cuối của đồ án.

## Sizing / tài nguyên laptop (ước tính, chưa đo thật)

Dùng `scripts/check_capacity.sh [n_trials]` để tự tính theo đúng tài nguyên Docker Desktop
hiện tại của bạn — dưới đây chỉ là tóm tắt logic:

- Mỗi trial ≈ 1 waypoint (namespace, ~0.1 vCPU/128Mi ước tính) + 11 Deployment Online
  Boutique thật (redis-cart + 10 service) + 1 loadgenerator + 6 harness Deployment — TẤT
  CẢ pod ở đây giờ chỉ 1/1 (không sidecar), chi phí app container ≈ 1.57 vCPU/1.37 GiB/trial
  (KHÔNG đổi so với trước, không liên quan mesh mode).
- ztunnel: CỐ ĐỊNH ~0.6 vCPU/1.5 GiB cho CẢ cluster (3 node), KHÔNG tăng theo số trial —
  đây là khác biệt lớn nhất so với bản sidecar cũ (từng là ~27 vCPU/34 GiB CHỈ riêng phần
  mesh cho 15 trial).
- 15 trial cùng lúc ≈ 15 × 18 pod = 270 pod (KHÔNG còn ×2 container/pod như sidecar, mỗi
  pod giờ 1 container) — vẫn dùng 3-node kind + `max-pods=250` để né trần `maxPods=110`
  mặc định (lý do không đổi, không liên quan sidecar/ambient).
- Khuyến nghị `.wslconfig` (file `%UserProfile%\.wslconfig` trên Windows, cần
  `wsl --shutdown` rồi mở lại WSL2 sau khi sửa):
  ```ini
  [wsl2]
  memory=12GB
  processors=6
  ```
  (điều chỉnh theo RAM thật của máy — để lại tối thiểu 4GB cho Windows).
- **Quan trọng**: với model "namespace riêng/trial", ngay cả sau khi đổi sang ambient, tổng
  nhu cầu cho 15 trial (~25.6 vCPU/~23.4 GiB theo `check_capacity.sh`) vẫn có khả năng vượt
  quá tài nguyên laptop thật — vì phần app container (không đổi, không liên quan mesh mode)
  mới là phần chiếm nhiều nhất. Nếu `check_capacity.sh` báo không đủ chỗ: dùng
  `06_deploy_all_trials.sh --batch-size N` (đánh đổi: phá vỡ "1 cửa sổ 7 ngày chung"), hoặc —
  **khuyến nghị, ĐÃ implement** — chuyển sang kiến trúc dùng chung hạ tầng, xem mục ngay
  dưới (`check_capacity.sh [n_trials] --shared` cho con số cụ thể: ~2.27 vCPU/2.96 GiB cho
  CẢ 15 trial).

## Kiến trúc "dùng chung hạ tầng" (ĐÃ implement — `05_deploy_shared.sh`)

Giải quyết đúng giới hạn capacity của model "namespace riêng/trial" (tối đa 4/15 đồng thời,
xác nhận thật ở trên): thay vì 15 namespace mỗi cái 1 bản Online Boutique thật riêng, dùng
**1 namespace duy nhất** chứa **1 bản Online Boutique thật** + toàn bộ harness Deployment
cho 15 "virtual trial" — app + waypoint chỉ tính chi phí 1 LẦN thay vì ×15.

**Lưu ý quan trọng khi thiết kế lại (khác với tài liệu thiết kế ban đầu đã trình cô Dung)**:
bản thiết kế đầu tiên định tách traffic của từng virtual trial bằng nhãn Prometheus
`injection_id` — nhưng nhãn này đã được XÁC NHẬN THẬT là không bao giờ resolve được qua
waypoint dưới Istio ambient (xem "CHƯA xác nhận được" #2, "Đã tìm & sửa" #6 ở trên) — nên
KHÔNG THỂ dùng để tách. Đã đổi sang tách bằng `pair_key()` (Python thuần, không phụ thuộc
Prometheus label nào) trước khi implement, tránh lặp lại đúng giới hạn đã biết.

- **`scenario_pool.py::generate_shared_trials(n_trials, base_seed, namespace)`**: sinh các
  cạnh cấy (`SharedEdge`, có field `owners: list[str]`) từ 1 pool được xáo trộn TOÀN CỤC,
  rút KHÔNG lặp lại giữa các virtual trial cho 5/6 loại kịch bản. Riêng
  `NOISE_HEALTHCHECK` (đích luôn là `frontend`, chỉ có 10 service nguồn khả dụng cho 15
  trial — giới hạn cấu trúc, không tránh được) và một phần `NOISE_MONITORING` phải CHIA SẺ
  1 Deployment vật lý cho nhiều virtual trial, theo dõi qua `owners` — đúng cơ chế mà code
  sẵn có đã dùng để coi 15 cạnh thật Online Boutique là bằng chứng `ground_truth=True` DÙNG
  CHUNG cho mọi trial. Với `base_seed=20260101, n_trials=15`: 83 `SharedEdge` (thay vì
  90 = 6×15), mỗi virtual trial vẫn có đủ 6 loại kịch bản, `ground_truth` 21 cặp/trial.
- **`render_manifests.py::render_shared()`**: render 1 Namespace + 1 ConfigMap (wrapper
  script) + N Deployment (N = số `SharedEdge`, không phải 6×15) — dùng lại nguyên cơ chế
  ambient/waypoint đã có, chỉ đổi phần topology.
- **`scripts/05_deploy_shared.sh [namespace] [n_trials] [base_seed]`**: script deploy,
  tương tự `05_deploy_trial.sh` nhưng tạo namespace + waypoint + Online Boutique 1 LẦN, rồi
  render/apply toàn bộ harness Deployment dùng chung; ghi thêm `owner_map` (JSON, map
  `pair_key` → danh sách virtual trial sở hữu) vào `.cache/` (không nằm trong
  `ground_truth/` để tránh lẫn vào glob `--all` của `08_export_trial_data.py`).
- **`08_export_trial_data.py --shared --shared-namespace <ns> --owner-map <path>`**: thu
  traffic/config **1 LẦN DUY NHẤT** từ namespace chung, sau đó
  `split_shared_collection()` tách thành N file `trial_data/<virtual-trial-id>.json` —
  mỗi record được gán vào virtual trial nào hoàn toàn dựa trên so khớp `pair_key()` Python
  (cạnh thật Online Boutique → MỌI trial; cạnh cấy → chỉ trial có tên trong `owners`) —
  **không đọc bất kỳ nhãn Prometheus nào**. Từ bước 09 trở đi (chạy
  `tier1.experiment.run_experiment()`), pipeline giống hệt model namespace riêng.
- **`check_capacity.sh [n_trials] --shared`**: tính REQUEST riêng cho model này (app +
  waypoint × 1, không × N_TRIALS) — xác nhận: ~2.27 vCPU/2.96 GiB cho CẢ 15 virtual trial
  (so với ~25.65 vCPU/23.41 GiB của model namespace riêng).
- **Test**: 19 test mới (`tests/test_scenario_pool.py`, `tests/test_render_manifests.py`,
  `tests/test_export_shared.py`) xác nhận: mỗi virtual trial có đủ 6 loại kịch bản, tên
  Deployment không trùng toàn cục, `NOISE_HEALTHCHECK` phải chia sẻ owner khi pool cạn,
  `owner_map` phủ đủ mọi `SharedEdge`, `ground_truth` của 1 trial KHÔNG lẫn cạnh của trial
  khác, kết quả tái lập được (cùng seed → cùng output), và logic tách traffic/config theo
  `pair_key()` đúng trong mọi trường hợp (cạnh riêng, cạnh chia sẻ, trial thiếu ground
  truth). Toàn bộ 46 test trong `infra/tests/` đang PASS.
- **Đã kiểm thử tĩnh** (chưa chạy trên cluster thật — sandbox không có cluster, giống toàn
  bộ phần còn lại của thư mục này trước khi Bin tự chạy thử): `bash -n` cho
  `05_deploy_shared.sh` OK, CLI `render_manifests.py --shared` chạy thử sinh đúng 85 YAML
  doc (1 Namespace + 1 ConfigMap + 83 Deployment) + 15 file ground-truth + 1 file
  owner_map.

## Xoá / dọn dẹp

```bash
kind delete cluster --name kltn-eval   # xoá sạch, làm lại từ 02_create_cluster.sh
```
