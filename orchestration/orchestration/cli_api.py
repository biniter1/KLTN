"""Chạy API Server (tiến trình dài hạn):

    export GITHUB_TOKEN="..."     # tuỳ chọn — không có thì GitHub giới hạn 60 request/giờ
    python -m orchestration.cli_api --db ../kltn.sqlite3 --port 8080
"""

from __future__ import annotations

import argparse
import os


def main(argv=None) -> None:  # pragma: no cover - khởi động uvicorn thật
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="kltn.sqlite3")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--no-github", action="store_true", help="gatekeeper trả rỗng thay vì gọi GitHub")
    args = p.parse_args(argv)
    os.environ["KLTN_DB"] = args.db
    if args.no_github:
        os.environ["KLTN_PR_SOURCE"] = "none"

    import uvicorn

    from .api.app import create_app, settings_from_env

    uvicorn.run(create_app(settings_from_env()), host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
