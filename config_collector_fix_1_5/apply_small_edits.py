"""Sửa 2 chỗ nhỏ TẠI CHỖ (không ghi đè cả file — bản trên VM có thể đã vá
CHANGES_existing_modules.patch nên khác bản trên knowledge base):

  1. tier1/config_collector/pipeline.py : image_reader mặc định NullImageConfigReader()
                                          -> default_image_reader() (đọc image thật).
  2. tests/conftest.py                  : thêm fixture autouse đặt
                                          CONFIG_COLLECTOR_IMAGE_READER=none để test
                                          không gọi mạng/docker.

Chạy từ thư mục gốc tier1_inference:   python3 apply_small_edits.py
Chạy lại nhiều lần không sao (idempotent).
"""

from pathlib import Path
import sys

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
pipeline = root / "tier1/config_collector/pipeline.py"
conftest = root / "tests/conftest.py"

src = pipeline.read_text(encoding="utf-8")
old_line = "image_reader = image_reader or NullImageConfigReader()"
new_line = "image_reader = image_reader or default_image_reader()"
anchor = "from .merge import merge_effective_config\n"
new_import = "from .image_config import default_image_reader\n"
if new_line not in src:
    if old_line not in src or anchor not in src:
        sys.exit(f"!! {pipeline}: không thấy dòng cần sửa -- sửa tay theo docstring file này.")
    src = src.replace(old_line, new_line).replace(anchor, new_import + anchor)
    pipeline.write_text(src, encoding="utf-8")
    print(f"OK  {pipeline}")
else:
    print(f"--  {pipeline} đã sửa từ trước")

fixture = '''

@pytest.fixture(autouse=True)
def _no_real_image_reader(monkeypatch):
    """collect_config_evidence() mặc định đọc image thật (registry/docker) -- tắt trong
    test để không gọi mạng. Test nào cần thì tự monkeypatch lại."""
    monkeypatch.setenv("CONFIG_COLLECTOR_IMAGE_READER", "none")
'''
text = conftest.read_text(encoding="utf-8")
if "_no_real_image_reader" not in text:
    conftest.write_text(text.rstrip("\n") + "\n" + fixture, encoding="utf-8")
    print(f"OK  {conftest}")
else:
    print(f"--  {conftest} đã sửa từ trước")
