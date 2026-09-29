"""启动资源中心服务。

用法：python3 tools/run_server.py [数据库路径] [端口]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from resource_center.server import serve

if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "resource_center.db"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    serve(db_path=db_path, port=port)
