# 01 — Tổng quan dự án

Nhãn dùng trong bộ tài liệu: **[FACT]** đã xác minh (code, dữ liệu, file) · **[DECISION]** đã thống nhất với Bin · **[EXPERIMENT]** thí nghiệm và kết quả · **[ASSUMPTION]** chưa kiểm chứng · **[TODO]** chưa xong · **[BLOCKER]** đang cản tiến độ.

Ngày bàn giao: 11/11/2026. Commit tham chiếu: `c62e37a` trên nhánh `tier2-run-tooling` của repo `biniter1/KLTN`.

## 1. Thông tin đề tài

| | |
|---|---|
| Sinh viên | Hà Sơn Bin – 23520149 – NT505.R11.ANTT, ngành An toàn thông tin, Khoa Mạng máy tính và Truyền thông, UIT – ĐHQG-HCM |
| GVHD | ThS. Trần Thị Dung (Phó Trưởng Khoa MMT&TT) |
| Tên đăng ký ban đầu | "Khung triển khai chính sách mạng cho hệ thống Kubernetes sử dụng tác nhân trí tuệ nhân tạo áp dụng nguyên lý Zero Trust" |
| Tên sửa (11/10) | "Khung triển khai chính sách mạng theo kiến trúc Zero Trust cho hệ thống Kubernetes sử dụng tác nhân trí tuệ nhân tạo" — sửa theo góp ý người nhận xét "Sửa 'nguyên lý Zero Trust'". **[TODO]** báo cô Dung trước khi nộp; bìa file báo cáo hiện vẫn ghi tên cũ (có lỗi chính tả "KUBERNTES", "ÁP ỤNG") |
| Tên tiếng Anh | "A Zero Trust Architecture-based network policy deployment framework for Kubernetes using AI agents" |
| Thời gian đăng ký | 07/09/2026 – 29/12/2026 |
| **Hạn báo cáo / bảo vệ** | **[DECISION]** cuối tháng 01/2027 (Bin nói 11/11) |

## 2. Bối cảnh và bài toán

- [FACT] Kubernetes mặc định cho mọi pod gọi mọi pod. Zero Trust (NIST SP 800-207) ở tầng mạng = NetworkPolicy default-deny, chỉ mở đúng kết nối cần.
- Hệ thống legacy không có tài liệu đầy đủ về phụ thuộc giữa service → không dám bật default-deny.
- **Nguyên tắc an toàn bất đối xứng**: bỏ sót phụ thuộc thật (FN) làm sập service; mở thừa (FP) chỉ làm policy lỏng và rà lại được.
- Nhóm khó nhất: cạnh thưa (CronJob chu kỳ dài, nhánh lỗi hiếm), cạnh chỉ có trong code/PR/tài liệu.
- Hạn chế của công cụ hiện có (Kubescape, Otterize, Calico Cloud): kết luận nhị phân, phụ thuộc cửa sổ quan sát, không tách lưu lượng phụ trợ, không chủ động tìm thêm bằng chứng.

## 3. Mục tiêu (theo đề cương đã sửa theo cô Dung)

1. Xây dựng bộ khung suy luận quan hệ phụ thuộc có kiểm soát độ tin cậy.
2. Triển khai bộ khung gồm 3 phần chính: Tầng 1 (tổng hợp và tính điểm), Tầng 2 (tác nhân AI), Migration Planner.
3. Áp dụng Zero Trust khi tích hợp tác nhân AI.

**[DECISION] Mục tiêu đã đổi ngày 05/11/2026:** công cụ là trợ giúp di chuyển sang Zero Trust bằng cách **thu hẹp phạm vi**, admin duyệt và chọn cạnh đúng ở bước cuối — không phải tự động hoàn toàn. **Giảm FN tối đa; FP cao chấp nhận được.** Hệ quả: đo thêm chỉ số theo hàng đợi review (`queue_recall`, `queue_size`, `false_drop`).

## 4. Phạm vi

- Trong phạm vi: phân đoạn mạng giữa service trong 1 cụm; HTTP/gRPC, TCP; kiểm soát tác nhân AI; giám sát sau triển khai ở mức demo.
- Ngoài phạm vi: định danh người dùng, trạng thái thiết bị, multi-cluster, UDP (trừ DNS), production thật.
- Môi trường thí nghiệm: kind trên VM Azure, Istio ambient + waypoint, Prometheus, Online Boutique (~11 service) + harness tự cấy.

## 5. Câu hỏi nghiên cứu / giả thiết (Chương 3, mục 3.3 của báo cáo)

| # | Giả thiết | Cách kiểm | Trạng thái |
|---|---|---|---|
| H1 | Tính điểm đa đặc trưng + phân loại lưu lượng cho precision cao hơn "thấy lưu lượng là cho phép" (B1) trên nhóm health check/monitoring, không giảm recall | Thực nghiệm R=15 | Precision cao hơn có ý nghĩa; recall **thấp hơn** B1 có ý nghĩa → phần "không giảm recall" **không đạt** trên dữ liệu hiện tại. `noise_precision` lỗi định nghĩa |
| H2 | Tính điểm liên tục cho recall cao hơn ngưỡng cứng (B2) trên nhóm thưa | Thực nghiệm | Dữ liệu 08/10: 0,378 vs 0,000 (mô tả). Dữ liệu 20–26/10: cả hai 0,000 |
| H3 | Tác nhân AI bị kiểm soát theo Zero Trust không dẫn tới tự động áp dụng policy khi chưa có lưu lượng thật | Thiết kế + test | Luật đã đổi ở v3 (xem 04), cần viết lại cách đảm bảo |
| H4 | Trích xuất cấu hình bằng LLM (kẹp grounding + xác thực) cho recall cao hơn regex v1 mà không tăng tham chiếu sai | Thực nghiệm | **[TODO]** chưa chạy |

## 6. Đóng góp kỳ vọng

- Khung 2 tầng có trạng thái tường minh thay cho có/không.
- Kết hợp traffic + cấu hình (Config Evidence Collector dùng LLM có kiểm chứng tất định).
- Áp Zero Trust lên chính tác nhân AI (allowlist tool, gatekeeper, kết luận suy ra bằng luật).
- Migration Planner: Policy Impact Validator, Continuity Monitor (demo).
- Phân tích trung thực kết quả, kể cả kết quả âm (Tầng 2 không cải thiện recall có ý nghĩa trên dataset hiện tại).

## 7. Yêu cầu của giảng viên đã biết

- [FACT] Góp ý đề cương (8 comment, 14 và 19/09): viết lại mục tiêu theo 3 ý trên; bố cục "Nội dung N / Phương pháp"; giải thích "chi phí tăng dần"; dùng "Tầng" thay "Tier"; bổ sung "Kết quả mong đợi"; kế hoạch có ngày cụ thể.
- [FACT] Lần nộp bị trả (21/09): thống nhất font/cỡ chữ, trùng heading, in đậm + nghiêng tên "Nội dung".
- [FACT] Cô Dung yêu cầu căn đều hai bên; thuật ngữ tiếng Anh dịch sang tiếng Việt, kèm tiếng Anh trong ngoặc.
- [FACT] Cô Dung (23/09) yêu cầu dashboard cho con người theo dõi/can thiệp và giám sát sau triển khai (ví dụ mở port khẩn cấp rồi quên đóng).
- [FACT] Kiến trúc "dùng chung hạ tầng" đã thống nhất với cô Dung.
- [ASSUMPTION] Việc đổi 3 tầng → 2 tầng và dùng LLM trong Config Collector **có thể chưa được trình bày riêng** với cô Dung (tài liệu thiết kế ghi "chưa trao đổi"; đề cương nộp sau đó đã dùng 2 tầng).
- **Tiêu chí chấm của hội đồng: không có trong tài liệu truy cập được.**

## 8. Ràng buộc về nội dung báo cáo

- [DECISION] Không được viết "Tầng 1 tốt hơn baseline" — chỉ "so sánh và phân tích".
- [DECISION] (10/2026) Chương 4 chỉ báo cáo Tầng 1 (+B1/B2); recall gộp Tầng 1+2 chưa đưa vào vì Tầng 2 đang làm lại. (Ghi đè TRANG_THAI_CHOT, nơi ghi recall gộp là chỉ số chính.)
- [DECISION] Bảo vệ tập trung: module đọc traffic, module cấu hình, lõi Tầng 1, nguồn gốc trọng số, baseline; Tầng 2 trình bày như đã viết.
- [DECISION] Phương pháp đánh giá đặt ở Chương 3, kết quả ở Chương 4.
