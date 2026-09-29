"""资源中心 HTTP 接口回归测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from resource_center.api import make_server  # noqa: E402
from resource_center.service import ResourceCenterService  # noqa: E402
from resource_center.store import Store  # noqa: E402

ADMIN = {"X-Admin-Token": "test-token"}
BETA = {"X-Institution-Id": "inst-beta"}
GAMMA = {"X-Institution-Id": "inst-gamma"}
ALPHA = {"X-Institution-Id": "inst-alpha"}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls._tmp.name) / "api.db")
        cls.service = ResourceCenterService(cls.store)
        cls.server = make_server(cls.service, host="127.0.0.1", port=0,
                                 admin_token="test-token")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.store.close()
        cls._tmp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None,
                headers: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self) -> None:
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_auth_required(self) -> None:
        status, _ = self.request("POST", "/admin/institutions", {"id": "x"})
        self.assertEqual(status, 401)
        status, _ = self.request("POST", "/applications", {})
        self.assertEqual(status, 401)
        status, _ = self.request("POST", "/applications", {},
                                 {"X-Institution-Id": "inst-unknown"})
        self.assertEqual(status, 401)

    def test_full_flow_and_isolation(self) -> None:
        # 管理端登记资源目录
        for institution in (
                {"id": "inst-alpha", "name": "华东联合实训基地", "kind": "provider",
                 "timezone": "Asia/Shanghai"},
                {"id": "inst-beta", "name": "海湾合作学院", "kind": "consumer",
                 "timezone": "Asia/Dubai"},
                {"id": "inst-gamma", "name": "北欧交换学院", "kind": "consumer",
                 "timezone": "Europe/Berlin"}):
            status, _ = self.request("POST", "/admin/institutions", institution, ADMIN)
            self.assertEqual(status, 201)
        status, _ = self.request("POST", "/admin/equipment", {
            "id": "eq-1", "institution_id": "inst-alpha", "name": "焊接机器人",
            "capabilities": ["焊接"], "timezone": "Asia/Shanghai"}, ADMIN)
        self.assertEqual(status, 201)
        status, _ = self.request("POST", "/admin/workstations", {
            "id": "ws-1", "institution_id": "inst-alpha", "name": "实训工位A",
            "capacity": 4}, ADMIN)
        self.assertEqual(status, 201)

        application = {
            "course_id": "joint-welding-101",
            "required_capabilities": ["焊接"],
            "workstation_seats": 2,
            "duration_minutes": 120,
            "start_local": "2026-10-10T09:00:00",
            "timezone": "Asia/Dubai",
        }
        # 申请：暂锁全部资源
        status, body = self.request("POST", "/applications", application, BETA)
        self.assertEqual(status, 201)
        booking = body["booking"]
        self.assertEqual(booking["state"], "held")
        self.assertEqual(booking["consumer_institution_id"], "inst-beta")
        self.assertEqual(booking["provider_institution_id"], "inst-alpha")

        # 同时段再次申请：409 + 可解释替代方案
        status, body = self.request("POST", "/applications", application, BETA)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "conflict")
        self.assertTrue(body["conflicts"])
        self.assertTrue(body["alternatives"])
        self.assertTrue(all(a["explanation"] for a in body["alternatives"]))

        # 确认
        status, body = self.request(
            "POST", f"/bookings/{booking['id']}/confirm", None, BETA)
        self.assertEqual(status, 200)
        self.assertEqual(body["booking"]["state"], "confirmed")

        # 机构隔离：无关机构看不到该预约
        status, _ = self.request("GET", f"/bookings/{booking['id']}", None, GAMMA)
        self.assertEqual(status, 404)
        status, body = self.request("GET", "/bookings", None, GAMMA)
        self.assertEqual(body["bookings"], [])
        status, body = self.request(
            "GET", "/plan?start_local=2026-10-10T00:00:00&end_local=2026-10-11T00:00:00"
                   "&timezone=Asia/Dubai", None, BETA)
        self.assertEqual(status, 200)
        related = [b for r in body["resources"] for b in r["blocks"]
                   if b["kind"] == "occupancy"]
        self.assertTrue(related)
        self.assertTrue(all(b["related"] for b in related))

        # 设备停用：关联占用整体释放，返回替代方案
        status, body = self.request("POST", "/equipment/eq-1/deactivate", None, ALPHA)
        self.assertEqual(status, 200)
        affected = body["affected"][0]["booking"]
        self.assertEqual(affected["state"], "released")
        self.assertTrue(all(o["state"] == "released" for o in affected["occupancy"]))
        self.assertTrue(body["affected"][0]["alternatives"] is not None)

    def test_unknown_route(self) -> None:
        status, body = self.request("GET", "/no-such-route")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
