# 09 — Thỏa thuận làm việc với AI

## Ràng buộc cứng (Bin đặt, không tự ý đổi)

1. **Không bịa hay giả lập kết quả.** Mọi số liệu từ lần chạy thật. Không chạy được thì nói rõ vì sao, không điền số thay. Số không chính thức phải ghi "không chính thức".
2. **Không yêu cầu dán API key/token vào chat.** Chỉ hướng dẫn Bin tự đặt trong shell (`read -rs TÊN_BIẾN && export TÊN_BIẾN`).
3. **Không push, không merge PR #2, không sửa `main`** khi Bin chưa nói. (Lịch sử: trước 30/10 quy tắc là không commit/push gì, giao code bằng zip; từ 30/10 Bin cho phép push lên nhánh làm việc khi đã yêu cầu.)
4. **Không chắc thì hỏi trước.**
5. Không chia sẻ `.env`, API key, kubeconfig.
6. Không restart/sửa Prometheus hay dữ liệu `shared-eval`.
7. **Không sửa file nộp (docx) khi chưa hỏi.** Không tự tạo/sửa file khi Bin chỉ trả lời câu hỏi phạm vi.
8. Báo cáo không được viết "Tầng 1 tốt hơn baseline".
9. Chỉ tính số chính thức Tầng 2 trên cạnh tự cấy.
10. Không dùng LLM sinh dữ liệu thay số đo thật.

## Cách làm việc (chốt 05/11)

- **Trước khi code: liệt kê file và thay đổi, chờ Bin duyệt.**
- Sau mỗi thay đổi: cập nhật sơ đồ Tầng 2 (`docs/TIER2_MAP.html`) và changelog.
- Làm từng phần nhỏ, giải thích từng phần, không chạy một mạch dài không giám sát.
- Không trộn phần giải thích với danh sách lỗi dài trong cùng một câu trả lời.

## Phong cách giải thích

- Tiếng Việt, **ngắn, rõ, có cấu trúc**, không vòng vo. Bin từng phàn nàn "lý giải rối".
- Thực tế, có lý do thực tiễn, không kiểu sách giáo khoa.
- Bin hiểu thiết kế ở mức tổng, dễ lạc ở chi tiết triển khai → đi từ luồng tổng xuống, mỗi lần một mảnh.
- Bin hỏi vặn sắc, phản bác khi lập luận thiếu → câu trả lời phải chính xác, chịu được chất vấn.
- Bin cần tự hiểu mọi thứ trong khóa luận để bảo vệ.

## Văn bản báo cáo

- Theo mẫu của khoa UIT, giữ đúng heading của mẫu.
- Căn đều hai bên. Thuật ngữ tiếng Anh dịch sang tiếng Việt kèm tiếng Anh trong ngoặc, ví dụ "các dịch vụ (service)".
- Dùng "Tầng 1 / Tầng 2" thay "Tier".
- Lời văn cụ thể, không trừu tượng, không nhồi chi tiết; thể hiện Bin thực sự làm và hiểu.
- Quy trình: thống nhất dàn ý chương → AI viết → Bin duyệt/sửa → khóa → chương sau. Mỗi sơ đồ tối giản, giải thích nằm ở văn bản bên dưới.
- Chương 3 viết lại theo thứ tự: cấu trúc → tổng quan → Tầng 1 (đọc traffic → cấu hình → lõi → kết luận) → Tầng 2.
- Kiểm tra docx trong Word/WPS thật nếu có thể: từng có lỗi font (style Normal mất Times New Roman) và trang trắng chỉ thấy trong WPS, không thấy khi render bằng LibreOffice.

## Tổ chức code

- 4 package độc lập: `tier1_inference`, `tier2_agent`, `orchestration`, `infra`. `orchestration` phụ thuộc 2 package suy luận, không ngược lại.
- Lõi Tầng 1 chỉ dùng thư viện chuẩn. Mọi thứ cần mạng/cụm có bản giả (Fake*) để test không cần key.
- Mỗi thay đổi có test hồi quy. Không sửa ngưỡng/prompt/luật dừng dựa trên ground truth.
- Tách bảng SQLite theo bên ghi; API Server không ghi bảng của Batch (có test khóa).
- Commit message tiếng Việt, mô tả lý do.

## Sai lầm AI từng mắc (tránh lặp lại)

- (06/10) Trả lời vài câu rồi tự build cả tầng orchestration không hỏi → Bin phản đối.
- (11/10) Sửa file đề cương khi Bin chỉ trả lời câu hỏi phạm vi.
- (11/10) Trộn giải thích với danh sách lỗi dài → Bin quá tải.
- (05/11) Tự thêm/bớt/sửa bên dưới mà Bin không theo dõi được → phải vẽ bản đồ + changelog.
- Giải thích rối, dài → Bin cân nhắc đổi công cụ.
- `cac079f`: đẩy code + số liệu từ bản làm lại ngoài `main` → phải revert, số đó không dùng được.
- Viết lệnh mẫu `export OPENAI_API_KEY="..."` khiến Bin chạy nguyên văn → 401. Phải dùng `read -rs`.
- Đưa khối lệnh nhiều dòng có `read -rs` → nuốt dòng sau. Đưa từng lệnh riêng.
- Đọc Tầng 2 v1 "không được gì" mà không kiểm tích hợp trước → lỗi `identity_key` lẽ ra bắt được sớm hơn.
- Luật corroboration và giả thuyết không chiều được thiết kế mà không thử trên ca có bằng chứng thật → chỉ lộ ra ở benchmark.
- Sửa test: trùng tên helper, đặt assert sai chỗ.
- Thiết kế dashboard/v2.1 được viết vào Chương 3/4 như đã có trong khi chưa code.
