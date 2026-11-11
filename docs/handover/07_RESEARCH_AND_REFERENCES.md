# 07 — Nghiên cứu và tài liệu tham khảo

Danh mục dưới đây lấy nguyên từ mục "Tài liệu tham khảo" của `KLTN_23520149_HaSonBin.docx`. **Claude không đọc lại toàn văn các bài này trong phiên bàn giao**; phần "dùng để làm gì" là cách báo cáo đang trích dẫn.

| # | Tài liệu | Dùng cho |
|---|---|---|
| [1] | Ardebili & Bartolini, "KubeIntellect…", J. Grid Computing 24(3), 2026 | LLM vận hành K8s đa tác nhân, con người xác nhận thao tác ghi |
| [2] | ARMO, Kubescape "Generate network policies" | Baseline đối ngoại: sinh policy từ traffic |
| [3] | Cohen et al., "KubeGuard…", arXiv:2509.04191 | Công trình gần nhất: LLM sinh Role/NetworkPolicy/Deployment từ manifest + log |
| [4] | Debenedetti et al., "Defeating prompt injections by design" (CaMeL), arXiv:2503.18813 | Tách luồng điều khiển khỏi luồng dữ liệu |
| [5] | Google, Online Boutique (microservices-demo) | Ứng dụng nền |
| [6] | Greshake et al., indirect prompt injection, AISec '23 | Rủi ro dữ liệu đọc vào là chỉ thị |
| [7] | Istio, Ambient mode | ztunnel, waypoint |
| [8] | istio/istio issue #61590 | Không đọc được label nguồn ở ambient |
| [9] | Kubernetes, Network Policies | Default-deny, chỉ có rule cho phép |
| [10] | Malul et al., GenKubeSec, arXiv:2405.19954 | LLM phát hiện cấu hình sai (P 0,990 / R 0,999 theo bài) |
| [11] | Muntoni, Soldani, Brogi, ESOCC 2020 | Khai phá kiến trúc từ manifest K8s (TOSCA) |
| [12] | Nguyen & Tran, VKU 2025 | Nghiên cứu trong nước về Zero Trust mạng nội bộ |
| [13] | Otterize, Network mapper | Baseline đối ngoại |
| [14] | OWASP Top 10 for LLM 2025 | LLM01 Prompt Injection, LLM06 Excessive Agency |
| [15] | Pedregosa et al., scikit-learn | Hồi quy logistic, ROC |
| [16] | Prometheus, Query functions | `increase()` xử lý reset counter |
| [17] | NIST SP 800-207 | Zero Trust, continuous monitoring |
| [18] | Schneider et al., EMSE 30:128, 2025 | 13 công cụ phân tích tĩnh, Code2DFD F1 0,86/0,87 |
| [19] | Tigera, Calico Cloud policy recommendations | Baseline đối ngoại |
| [20] | Wilcoxon 1945 | Kiểm định bắt cặp |
| [21] | Yao et al., ReAct, ICLR 2023 | Vòng suy luận–hành động |

Ngoài danh mục: istio/istio#51565 (lý do thiết kế không có metadata exchange ở ambient) được trích trong `docs/design/TRAFFIC_COLLECTION_PIPELINE.md`.

## Nhận định chưa xác minh / không được trích

- Bài blog "Wavise OpenLLM" về chống prompt injection: chỉ dùng làm gợi ý (đã nhận 3 kỹ thuật rẻ). Đoạn về cộng đồng developer Trung Quốc (Zhihu, MIIT, thư viện GitHub có số sao cụ thể) **không kiểm chứng được — không trích**.
- Số liệu của các bài [1], [3], [10], [18] trong báo cáo lấy theo bài; **[ASSUMPTION]** chưa được Claude đối chiếu lại toàn văn trong phiên này.
- Tài liệu tiếng Việt: báo cáo ghi "chưa có tài liệu tiếng Việt được trích dẫn" dù có [12] (tiếng Anh, trường VN).

## Khoảng trống nghiên cứu (Bảng 2.1–2.2 của báo cáo)

1. Không có mức độ tin cậy cho từng quan hệ.
2. Mỗi hướng chỉ dựa một loại bằng chứng.
3. Đồng nhất "chưa thấy" với "không có".
4. Lưu lượng phụ trợ lẫn nghiệp vụ.
5. Tác nhân AI dùng cho mọi trường hợp và chưa được kiểm soát.
6. Chỉ dừng ở sinh policy, không giám sát sau triển khai.

## Phương pháp tham khảo đã dùng

ReAct (Tầng 2), hồi quy logistic + ROC (Tầng 1, thử LOTO), Wilcoxon + CI 95% (đánh giá), allowlist/least-privilege theo OWASP LLM06, delimiter + entropy chống injection, Zero Trust NIST 800-207 cho cả AI agent và vận hành.
