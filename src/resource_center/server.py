"""资源中心 HTTP 服务：JSON API、机构身份识别与过期暂占的后台清理。

认证方式：请求头 ``X-Institution-Id`` 标识机构身份，资源中心管理员使用
``resource-center``。服务启动时先执行恢复（继续释放过期暂占），运行期间
由后台线程定期清理。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .models import ADMIN_INSTITUTION, UTC, BookingRequest, EquipmentNeed, parse_instant
from .service import (
    AccessDeniedError,
    ConflictError,
    NotFoundError,
    ResourceCenterService,
    ServiceError,
    StateError,
)
from .store import Store

SWEEP_INTERVAL_SECONDS = 30.0


class HoldSweeper(threading.Thread):
    """后台清理线程：运行期间持续释放过期暂占。"""

    def __init__(self, service: ResourceCenterService, interval: float = SWEEP_INTERVAL_SECONDS):
        super().__init__(daemon=True, name="hold-sweeper")
        self._service = service
        self._interval = interval
        self._stopped = threading.Event()

    def run(self) -> None:
        while not self._stopped.wait(self._interval):
            self._service.release_expired_holds()

    def stop(self) -> None:
        self._stopped.set()


class ApiHandler(BaseHTTPRequestHandler):
    """JSON API 请求处理：路由分发、机构身份与错误映射。"""

    service: ResourceCenterService  # 由 make_server 注入

    # -- 基础工具 ------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 标准库签名
        pass

    def _send(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _actor(self) -> str:
        actor = self.headers.get("X-Institution-Id")
        if not actor:
            raise AccessDeniedError("缺少 X-Institution-Id 请求头")
        return actor

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ServiceError(f"请求体不是合法 JSON：{exc}") from exc

    def _require_self_or_admin(self, actor: str, institution_id: str) -> None:
        if actor != ADMIN_INSTITUTION and actor != institution_id:
            raise AccessDeniedError("只能登记本机构资源或以资源中心身份操作")

    # -- 路由入口 ------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - 标准库签名
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - 标准库签名
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            status, payload = self._route(method)
            self._send(status, payload)
        except ConflictError as exc:
            self._send(409, {"error": exc.error, "detail": exc.detail,
                             "conflicts": exc.conflicts, "alternatives": exc.alternatives})
        except ServiceError as exc:
            self._send(exc.status, {"error": exc.error, "detail": exc.detail})
        except ValueError as exc:
            self._send(400, {"error": "bad_request", "detail": str(exc)})

    def _route(self, method: str) -> tuple[int, dict | list]:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)
        if parts == ["health"]:
            return 200, {"status": "ok"}
        actor = self._actor()
        body = self._body() if method == "POST" else {}
        now = datetime.now(UTC)
        svc = self.service

        if parts == ["api", "institutions"] and method == "POST":
            self._require_self_or_admin(actor, body.get("institution_id", ""))
            return 201, svc.register_institution(body["institution_id"], body.get("name", ""),
                                                 body.get("kind", "institution"))
        if parts == ["api", "equipment"] and method == "POST":
            self._require_self_or_admin(actor, body.get("institution_id", ""))
            return 201, svc.add_equipment(body["institution_id"], body["name"],
                                          body.get("capabilities", []), body.get("equipment_id"))
        if parts == ["api", "workstations"] and method == "POST":
            self._require_self_or_admin(actor, body.get("institution_id", ""))
            return 201, svc.add_workstation(body["institution_id"], body["name"],
                                            int(body["capacity"]), body.get("workstation_id"))
        if parts == ["api", "instructors"] and method == "POST":
            self._require_self_or_admin(actor, body.get("institution_id", ""))
            return 201, svc.add_instructor(body["institution_id"], body["name"],
                                           body.get("timezone", "UTC"), body.get("instructor_id"))
        if len(parts) == 4 and parts[:2] == ["api", "instructors"] and parts[3] == "availability":
            tz = body.get("timezone")
            return 201, svc.add_instructor_availability(
                parts[2], parse_instant(body["start"], tz), parse_instant(body["end"], tz))
        if parts == ["api", "maintenance-windows"] and method == "POST":
            equipment = svc_equipment(self.service, body["equipment_id"])
            self._require_self_or_admin(actor, equipment["institution_id"])
            tz = body.get("timezone")
            return 201, svc.create_maintenance_window(
                body["equipment_id"], parse_instant(body["start"], tz),
                parse_instant(body["end"], tz), body.get("reason", ""))
        if len(parts) == 4 and parts[:2] == ["api", "maintenance-windows"] and parts[3] == "extend":
            tz = body.get("timezone")
            return 200, svc.extend_maintenance_window(
                parts[2], parse_instant(body["new_end"], tz), actor, now)
        if len(parts) == 4 and parts[:2] == ["api", "equipment"] and parts[3] == "disable":
            return 200, svc.disable_equipment(parts[2], actor, now)
        if len(parts) == 4 and parts[:2] == ["api", "equipment"] and parts[3] == "enable":
            return 200, svc.enable_equipment(parts[2], actor)

        if parts == ["api", "bookings"] and method == "POST":
            req = self._booking_request(body)
            self._require_self_or_admin(actor, req.institution_id)
            return 201, svc.request_booking(req, now)
        if parts == ["api", "bookings"] and method == "GET":
            return 200, {"bookings": svc.list_bookings(actor)}
        if len(parts) == 3 and parts[:2] == ["api", "bookings"] and method == "GET":
            return 200, svc.get_booking(parts[2], actor)
        if len(parts) == 4 and parts[:2] == ["api", "bookings"]:
            booking_id, action = parts[2], parts[3]
            if action == "confirm":
                return 200, svc.confirm_booking(booking_id, actor, now)
            if action == "cancel":
                return 200, svc.cancel_booking(booking_id, actor, now)
            if action == "complete":
                return 200, svc.complete_booking(booking_id, actor, now)
            if action == "in-use":
                return 200, svc.mark_in_use(booking_id, actor, now)
            if action == "reschedule":
                tz = body.get("timezone")
                return 200, svc.reschedule_booking(
                    booking_id, actor, parse_instant(body["start"], tz),
                    parse_instant(body["end"], tz), now)
        if parts == ["api", "plan"] and method == "GET":
            start = parse_instant(query["start"][0], query.get("timezone", [None])[0])
            end = parse_instant(query["end"][0], query.get("timezone", [None])[0])
            return 200, svc.occupancy_plan(actor, start, end)
        raise NotFoundError("接口不存在")

    @staticmethod
    def _booking_request(body: dict) -> BookingRequest:
        tz = body.get("timezone")
        return BookingRequest(
            institution_id=body["institution_id"],
            course_name=body.get("course_name", ""),
            start=parse_instant(body["start"], tz),
            end=parse_instant(body["end"], tz),
            timezone=tz or "UTC",
            equipment=[
                EquipmentNeed(tuple(item.get("capabilities") or ()), item.get("equipment_id"))
                for item in body.get("equipment", [])
            ],
            seats=int(body.get("seats", 0)),
            instructor_id=body.get("instructor_id"),
            workstation_id=body.get("workstation_id"),
            hold_ttl_seconds=int(body.get("hold_ttl_seconds", 900)),
        )


def svc_equipment(service: ResourceCenterService, equipment_id: str) -> dict:
    with service.store.read() as conn:
        row = conn.execute("SELECT * FROM equipment WHERE id=?", (equipment_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"设备 {equipment_id} 不存在")
    return dict(row)


def make_server(service: ResourceCenterService, host: str = "127.0.0.1", port: int = 8080,
                sweep_interval: float = SWEEP_INTERVAL_SECONDS) -> ThreadingHTTPServer:
    """构建 HTTP 服务：启动前先恢复（继续释放过期暂占），再开启后台清理。"""
    service.recover()
    handler = type("BoundApiHandler", (ApiHandler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    sweeper = HoldSweeper(service, sweep_interval)
    sweeper.start()
    server.sweeper = sweeper  # type: ignore[attr-defined]
    return server


def serve(db_path: str = "resource_center.db", host: str = "127.0.0.1", port: int = 8080) -> None:
    store = Store(db_path)
    service = ResourceCenterService(store)
    server = make_server(service, host, port)
    print(f"资源中心服务已启动：http://{host}:{server.server_address[1]}（数据库 {db_path}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.sweeper.stop()  # type: ignore[attr-defined]
        server.server_close()
        store.close()
