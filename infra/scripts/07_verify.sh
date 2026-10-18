#!/usr/bin/env bash
# Kiểm tra sanity sau khi deploy 1 hoặc nhiều trial: pod Running (KHÔNG sidecar — ambient
# không chèn container thứ 2 vào pod, xem 03_install_istio.sh), ztunnel (DaemonSet, cả
# cluster) + waypoint (Deployment, riêng từng trial) sẵn sàng, Prometheus scrape được, và
# (nếu đã có traffic thật) nhãn tuỳ chỉnh request_path đã xuất hiện đúng trong
# istio_requests_total.
#
# LƯU Ý (đã xác nhận thật, 2026-09-27): KHÔNG còn kiểm tra test_harness/injection_id ở đây —
# đây là giới hạn THẬT của Istio ambient (source.labels[...] không đọc được nhãn Pod qua
# waypoint, xem comment đầy đủ trong manifests/telemetry-custom-labels.yaml), không phải lỗi
# cấu hình, KHÔNG có cách nào để mục kiểm tra đó pass. Không chặn pipeline chấm điểm thật
# (evaluation.py không đọc 2 nhãn này) nên không cần giữ lại làm mục kiểm tra bắt buộc.
#
# Dùng: ./07_verify.sh [trial_id ...]   (không truyền gì -> quét MỌI namespace có nhãn
#                                        kltn/trial-id, tức mọi trial đã deploy)
set -uo pipefail  # KHÔNG dùng -e: script này chỉ BÁO CÁO, 1 lỗi không nên dừng cả kiểm tra
export PATH="$HOME/bin:$PATH"

if [ "$#" -gt 0 ]; then
  TRIAL_IDS=("$@")
else
  mapfile -t TRIAL_IDS < <(kubectl get ns -l kltn/trial-id -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n')
fi

if [ "${#TRIAL_IDS[@]}" -eq 0 ]; then
  echo "Không tìm thấy trial nào (namespace có nhãn kltn/trial-id). Đã deploy chưa? (05/06_*.sh)"
  exit 1
fi

FAIL=0

echo "=== [1/4] ztunnel (DaemonSet, dùng CHUNG cho cả cluster — không phải riêng từng trial) ==="
ZTUNNEL_TOTAL=$(kubectl get pods -n istio-system -l app=ztunnel --no-headers 2>/dev/null | wc -l)
ZTUNNEL_READY=$(kubectl get pods -n istio-system -l app=ztunnel --no-headers 2>/dev/null | awk '$2=="1/1"' | wc -l)
echo "    $ZTUNNEL_READY/$ZTUNNEL_TOTAL pod ztunnel Ready (kỳ vọng = số node của cluster)"
if [ "$ZTUNNEL_TOTAL" -eq 0 ] || [ "$ZTUNNEL_READY" -lt "$ZTUNNEL_TOTAL" ]; then
  echo "    !!! ztunnel chưa đủ Ready -- đã chạy 03_install_istio.sh (profile=ambient) chưa?"
  kubectl get pods -n istio-system -l app=ztunnel
  FAIL=1
fi

echo ""
echo "=== [2/4] Pod trong từng trial Running (1/1 -- KHÔNG phải 2/2, ambient không có sidecar) ==="
for ns in "${TRIAL_IDS[@]}"; do
  echo "--- $ns ---"
  total=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | wc -l)
  ready_11=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | awk '$2=="1/1"' | wc -l)
  echo "    tổng $total pod, $ready_11 pod ở trạng thái 1/1 Ready"
  if [ "$total" -eq 0 ]; then
    echo "    !!! namespace rỗng, chưa deploy gì?"
    FAIL=1
  elif [ "$ready_11" -lt "$total" ]; then
    echo "    !!! còn pod chưa 1/1 Ready -- liệt kê (2/2 ở đây là BẤT THƯỜNG với ambient, kiểm"
    echo "        tra namespace có lỡ còn nhãn 'istio-injection=enabled' cũ không):"
    kubectl get pods -n "$ns" --no-headers | awk '$2!="1/1"{print "        " $0}'
    FAIL=1
  fi

  # waypoint: 1 Deployment/namespace, tạo bởi `istioctl waypoint apply` ở 05_deploy_trial.sh
  if kubectl get deployment waypoint -n "$ns" >/dev/null 2>&1; then
    wp_ready=$(kubectl get deployment waypoint -n "$ns" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
    echo "    waypoint: readyReplicas=${wp_ready:-0}"
    [ "${wp_ready:-0}" -ge 1 ] || { echo "    !!! waypoint chưa Ready."; FAIL=1; }
  else
    echo "    !!! không thấy Deployment 'waypoint' -- đã chạy 'istioctl waypoint apply -n $ns"
    echo "        --enroll-namespace' (bước [2/6] của 05_deploy_trial.sh) chưa?"
    FAIL=1
  fi

  use_wp_label=$(kubectl get ns "$ns" -o jsonpath='{.metadata.labels.istio\.io/use-waypoint}' 2>/dev/null || echo "")
  if [ "$use_wp_label" != "waypoint" ]; then
    echo "    !!! namespace thiếu nhãn 'istio.io/use-waypoint=waypoint' -- traffic HTTP/gRPC sẽ"
    echo "        KHÔNG đi qua waypoint, 'istio_requests_total' sẽ KHÔNG được sinh ra."
    FAIL=1
  fi
done

echo ""
echo "=== [3/4] Prometheus (addon Istio) phản hồi truy vấn ==="
if kubectl get svc prometheus -n istio-system >/dev/null 2>&1; then
  kubectl -n istio-system port-forward svc/prometheus 39090:9090 >/tmp/kltn-verify-pf.log 2>&1 &
  PF_PID=$!
  sleep 3
  if curl -fsS "http://localhost:39090/api/v1/query?query=up" >/tmp/kltn-verify-up.json 2>/dev/null; then
    echo "    OK: Prometheus trả lời truy vấn 'up'."
  else
    echo "    !!! Prometheus KHÔNG phản hồi qua port-forward (xem /tmp/kltn-verify-pf.log)."
    FAIL=1
  fi
  kill "$PF_PID" 2>/dev/null || true
  wait "$PF_PID" 2>/dev/null || true
else
  echo "    !!! Không thấy Service 'prometheus' trong namespace istio-system -- đã chạy 03_install_istio.sh chưa?"
  FAIL=1
fi

echo ""
echo "=== [4/4] Nhãn tuỳ chỉnh request_path đã ghi nhận CHƯA (istio_requests_total) ==="
echo "    (CHỈ có ý nghĩa SAU KHI có traffic thật đi qua waypoint -- ngay sau deploy có thể"
echo "     vẫn rỗng, không tính là lỗi ở bước này, chỉ để tham khảo/theo dõi. KHÔNG kiểm tra"
echo "     test_harness/injection_id nữa -- giới hạn THẬT của ambient, xem comment trong"
echo "     manifests/telemetry-custom-labels.yaml, không chặn pipeline chấm điểm thật.)"
if [ -f /tmp/kltn-verify-up.json ]; then
  kubectl -n istio-system port-forward svc/prometheus 39090:9090 >/tmp/kltn-verify-pf2.log 2>&1 &
  PF_PID=$!
  sleep 3
  RESP=$(curl -fsS "http://localhost:39090/api/v1/query?query=istio_requests_total" 2>/dev/null || echo "")
  if echo "$RESP" | grep -q '"request_path":"'; then
    echo "    OK: đã thấy nhãn request_path (giá trị thật, không phải rỗng) trong istio_requests_total."
  else
    echo "    Chưa thấy (bình thường nếu vừa mới deploy, harness pod cần vài phút để gọi lần"
    echo "    đầu + Prometheus scrape). Nếu SAU 10-15 phút vẫn không thấy: kiểm tra lại"
    echo "    (a) manifests/telemetry-custom-labels.yaml đã apply đúng cú pháp bản Istio đang"
    echo "        cài chưa (04_apply_telemetry_customization.sh), (b) namespace có đủ nhãn"
    echo "        'istio.io/use-waypoint=waypoint' chưa (mục [2/4] ở trên) -- THIẾU nhãn này là"
    echo "        lý do PHỔ BIẾN NHẤT khiến istio_requests_total rỗng dưới ambient (traffic đi"
    echo "        thẳng qua ztunnel, không qua waypoint, nên không có xử lý L7)."
  fi
  kill "$PF_PID" 2>/dev/null || true
  wait "$PF_PID" 2>/dev/null || true
fi

rm -f /tmp/kltn-verify-up.json /tmp/kltn-verify-pf.log /tmp/kltn-verify-pf2.log

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "=== Tổng kết: các mục kiểm tra bắt buộc (ztunnel + pod + waypoint + Prometheus) đều OK. ==="
else
  echo "=== Tổng kết: CÓ mục kiểm tra bắt buộc CHƯA đạt (xem !!! ở trên). ==="
fi
exit "$FAIL"
