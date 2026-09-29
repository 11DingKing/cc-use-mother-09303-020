"""启动资源中心服务端。

用法：
    python3 tools/run_server.py --db data/resource_center.db --port 8080 --seed
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from resource_center.api import make_server
from resource_center.seed import seed_demo
from resource_center.service import ResourceCenterService
from resource_center.store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="资源中心服务端")
    parser.add_argument("--db", default="data/resource_center.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--seed", action="store_true", help="写入演示数据（幂等）")
    parser.add_argument("--sweep-interval", type=float, default=30.0, help="过期暂占巡检间隔（秒）")
    parser.add_argument("--admin-token", default=os.environ.get("RESOURCE_CENTER_ADMIN_TOKEN", "dev-admin-token"))
    args = parser.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db_path)
    service = ResourceCenterService(store)
    if args.seed:
        result = seed_demo(store)
        print("演示数据已写入" if result["seeded"] else "演示数据已存在，跳过")

    released = service.recover()
    if released:
        print(f"服务恢复：继续释放 {len(released)} 条过期暂占：{released}")
    service.start_sweeper(interval_seconds=args.sweep_interval)

    server = make_server(service, host=args.host, port=args.port, admin_token=args.admin_token)
    print(f"资源中心服务已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop_sweeper()
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
