"""资源中心 HTTP API 测试：端到端流程、冲突响应结构与机构隔离。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from resource_center.server import make_server  # noqa: E402
from resource_center.service import ResourceCenterService  # noqa: E402
from resource_center.store import Store  # noqa: E402

ADMIN = "resource-center"


class ServerTestCase(unittest.TestCase):
    """每个用例独立的服务实例与数据库，互不干扰。"""

    def setUp(self) -> None:
        self.service = ResourceCenterService(Store(":memory:"))
        self.server = make_server(self.service, port=0, sweep_interval=3600)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._seed()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server.sweeper.stop()
        self.service.store.close()

    def _seed(self) -> None:
        for institution_id, name in (("provider-p", "设备提供院校P"),
                                     ("consumer-a", "使用院校A"),
                                     ("consumer-b", "使用院校B")):
            self.call("POST", "/api/institutions",
                      {"institution_id": institution_id, "name": name}, ADMIN)
        self.call("POST", "/api/equipment",
                  {"institution_id": "provider-p", "name": "五轴加工中心",
                   "capabilities": ["cnc", "5-axis"], "equipment_id": "eq-1"}, ADMIN)
        self.call("POST", "/api/workstations",
                  {"institution_id": "provider-p", "name": "实训工位区",
                   "capacity": 30, "workstation_id": "ws-1"}, ADMIN)
        self.call("POST", "/api/instructors",
                  {"institution_id": "provider-p", "name": "远程指导教师",
                   "timezone": "Asia/Shanghai", "instructor_id": "ins-1"}, ADMIN)
        self.call("POST", "/api/instructors/ins-1/availability",
                  {"start": "2026-10-01T00:00:00Z", "end": "2026-10-31T00:00:00Z"}, ADMIN)

    def call(self, method: str, path: str, body: dict | None = None,
             actor: str | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Institution-Id"] = actor
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, payload


def course_body(institution: str = "consumer-a", **overrides) -> dict:
    body = {
        "institution_id": institution,
        "course_name": "联合实训课",
        "timezone": "Asia/Shanghai",
        "start": "2026-10-10T09:00:00",
        "end": "2026-10-10T12:00:00",
        "equipment": [{"capabilities": ["cnc", "5-axis"]}],
        "seats": 20,
        "instructor_id": "ins-1",
        "hold_ttl_seconds": 600,
    }
    body.update(overrides)
    return body


class EndToEndTest(ServerTestCase):
    def test_full_flow_over_http(self) -> None:
        status, booking = self.call("POST", "/api/bookings", course_body(), "consumer-a")
        self.assertEqual(status, 201)
        self.assertEqual(booking["status"], "held")
        self.assertEqual(len(booking["occupancies"]), 3)

        status, confirmed = self.call("POST", f"/api/bookings/{booking['id']}/confirm",
                                      {}, "consumer-a")
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["status"], "confirmed")

        status, plan = self.call(
            "GET", "/api/plan?start=2026-10-10T00:00:00Z&end=2026-10-11T00:00:00Z",
            actor="consumer-a")
        self.assertEqual(status, 200)
        self.assertEqual(len(plan["occupancies"]), 3)
        self.assertTrue(all(item["visibility"] == "full" for item in plan["occupancies"]))

        status, done = self.call("POST", f"/api/bookings/{booking['id']}/complete",
                                 {}, "consumer-a")
        self.assertEqual(status, 200)
        self.assertEqual(done["status"], "released")

    def test_conflict_returns_explainable_alternatives(self) -> None:
        status, booking = self.call("POST", "/api/bookings", course_body(), "consumer-a")
        self.assertEqual(status, 201)
        self.call("POST", f"/api/bookings/{booking['id']}/confirm", {}, "consumer-a")

        status, error = self.call("POST", "/api/bookings",
                                  course_body(institution="consumer-b"), "consumer-b")
        self.assertEqual(status, 409)
        self.assertEqual(error["error"], "conflict")
        self.assertTrue(error["conflicts"])
        self.assertTrue(all("detail" in c for c in error["conflicts"]))
        self.assertTrue(any(a["kind"] == "shift_time" for a in error["alternatives"]))

    def test_maintenance_extension_over_http(self) -> None:
        status, booking = self.call("POST", "/api/bookings", course_body(), "consumer-a")
        self.assertEqual(status, 201)
        self.call("POST", f"/api/bookings/{booking['id']}/confirm", {}, "consumer-a")
        status, window = self.call("POST", "/api/maintenance-windows", {
            "equipment_id": "eq-1",
            "start": "2026-10-10T00:30:00Z",
            "end": "2026-10-10T01:00:00Z",
            "reason": "主轴检修",
        }, "provider-p")
        self.assertEqual(status, 201)
        status, report = self.call("POST", f"/api/maintenance-windows/{window['id']}/extend",
                                   {"new_end": "2026-10-10T05:00:00Z"}, "provider-p")
        self.assertEqual(status, 200)
        impacted = {item["booking_id"]: item["action"] for item in report["impact"]}
        self.assertIn(booking["id"], impacted)
        status, view = self.call("GET", f"/api/bookings/{booking['id']}", actor="consumer-a")
        active = [o for o in view["occupancies"] if o["status"] in ("held", "confirmed")]
        self.assertIn(len(active), (0, 3), "关联占用必须整体改期或整体释放")


class IsolationHttpTest(ServerTestCase):
    def test_missing_identity_is_rejected(self) -> None:
        status, _ = self.call("GET", "/api/bookings")
        self.assertEqual(status, 403)

    def test_other_institution_cannot_read_booking(self) -> None:
        status, booking = self.call("POST", "/api/bookings", course_body(), "consumer-a")
        self.assertEqual(status, 201)
        status, _ = self.call("GET", f"/api/bookings/{booking['id']}", actor="consumer-b")
        self.assertEqual(status, 404)
        status, _ = self.call("GET", f"/api/bookings/{booking['id']}", actor="provider-p")
        self.assertEqual(status, 200)

    def test_plan_redacts_unrelated_bookings(self) -> None:
        status, booking = self.call("POST", "/api/bookings", course_body(), "consumer-a")
        self.assertEqual(status, 201)
        self.call("POST", f"/api/bookings/{booking['id']}/confirm", {}, "consumer-a")
        status, plan = self.call(
            "GET", "/api/plan?start=2026-10-10T00:00:00Z&end=2026-10-11T00:00:00Z",
            actor="consumer-b")
        self.assertEqual(status, 200)
        self.assertTrue(plan["occupancies"])
        for item in plan["occupancies"]:
            self.assertEqual(item["visibility"], "redacted")
            self.assertNotIn("course_name", item)

    def test_institution_cannot_register_foreign_resources(self) -> None:
        status, _ = self.call("POST", "/api/equipment",
                              {"institution_id": "provider-p", "name": "冒名设备"},
                              "consumer-b")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
