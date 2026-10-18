#!/usr/bin/env bash
# Áp Telemetry CR bổ sung nhãn test_harness/injection_id/request_path (mesh-wide).
#
# Với ambient (03_install_istio.sh): CR này áp dụng mesh-wide (namespace istio-system)
# nên tự động có hiệu lực cho MỌI waypoint được tạo sau này ở từng trial (05_deploy_trial.sh)
# — không cần áp lại riêng cho từng waypoint. `istio_requests_total` (metric bị override ở
# đây) giờ được WAYPOINT sinh ra (không phải sidecar trong từng pod), nhưng cơ chế Telemetry
# API/tagOverrides không đổi vì waypoint vẫn là cùng 1 image istio-proxy (Envoy) như sidecar
# trước đây — chỉ khác VỊ TRÍ chạy (1 proxy dùng chung/namespace, không phải 1/pod).
# CHƯA xác nhận thêm (khác với bản sidecar cũ): `source.labels[...]` cần "peer metadata
# exchange" để waypoint (không nằm trong cùng pod với nguồn) biết được nhãn của POD NGUỒN
# thật -- cơ chế này chạy qua kết nối HBONE giữa ztunnel nguồn và waypoint đích, về nguyên
# lý vẫn mang cùng thông tin như sidecar-to-sidecar, nhưng CHƯA gọi thử trên cluster thật.
set -euo pipefail
export PATH="$HOME/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
kubectl apply -f "$SCRIPT_DIR/../manifests/telemetry-custom-labels.yaml"

echo ""
echo "=== Kiểm tra nhanh (không thay thế việc xem số liệu thật sau khi có traffic) ==="
kubectl get telemetry -n istio-system
echo ""
echo "=> Áp dụng xong. CHƯA có cách xác nhận nhãn hoạt động đúng cho tới khi có traffic"
echo "   thật đi qua VÀ pod nguồn có gắn nhãn 'test-harness'/'injection-id' (xem manifests"
echo "   test-harness-template.yaml, bước 05). Sau khi có traffic, kiểm tra bằng:"
echo "     kubectl -n istio-system port-forward svc/prometheus 9090:9090 &"
echo '     curl -s "http://localhost:9090/api/v1/query?query=istio_requests_total" | python3 -m json.tool | grep -A2 test_harness'
echo "   Nếu không thấy field 'test_harness' trong nhãn trả về -> cú pháp tagOverrides"
echo "   không khớp bản Istio đang cài, cần tra lại doc Telemetry API đúng version."
