"""Zero Trust Continuity Monitor (MIGRATION_PLANNER_DESIGN.md mục 2; báo cáo mục 3.6, 3.10.4,
3.10.5) — phần chạy NGOÀI API Server:

  - `audit.py`   : đọc Kubernetes Audit Log -> các thay đổi chính sách mạng (config_change_log)
  - `blocked.py` : đọc flow log của CNI / log ztunnel -> các kết nối bị chặn (blocked_connection_log)
  - `store.py`   : module DUY NHẤT ghi 2 bảng trên (append-only, khử trùng khi nạp lại)
  - `cli_continuity.py` (ở gói cha): lệnh chạy định kỳ (sweep)

Đối chiếu "thay đổi nào chưa quy được trách nhiệm", "ngoại lệ nào quá hạn", "kết nối bị chặn nào
chưa ai xử lý" KHÔNG nằm ở đây mà suy ra lúc đọc trong `api/readers.py` (cùng cách hàng đợi
review hiện có) -> không tiến trình nào phải UPDATE trạng thái.

Không xâm lấn: chỉ ĐỌC file log/sự kiện mà cụm đã ghi sẵn; không gọi ghi vào cụm.
"""
