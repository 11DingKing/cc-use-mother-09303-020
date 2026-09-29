"""HTTP 接口层。

只负责 JSON 编解码、身份识别与错误映射；业务规则全部在 service 层。
机构隔离：除 /health 与 /admin/* 外，所有接口要求 X-Institution-Id，
且只能看到与自己预约相关的细节；管理接口要求 X-Admin-Token。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .service import ConflictError, ServiceError, UnauthorizedError, ValidationError

ERROR_STATUS = {
    "validation": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "invalid_state": 409,
    "conflict": 409,
}


def _one(query: dict, name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


# ---------------------------------------------------------------------------
# 路由处理函数：签名为 (handler, actor, query, **path_params) -> (status, payload)
# actor 为 (kind, institution_id)，kind ∈ {"public", "admin", "institution"}
# ---------------------------------------------------------------------------

def h_health(handler, actor, query):
    return 200, {"status": "ok"}


def h_add_institution(handler, actor, query):
    return 201, {"institution": handler.server.service.add_institution(handler._json_body())}


def h_add_equipment(handler, actor, query):
    return 201, {"equipment": handler.server.service.add_equipment(handler._json_body())}


def h_add_workstation(handler, actor, query):
    return 201, {"workstation": handler.server.service.add_workstation(handler._json_body())}


def h_add_instructor(handler, actor, query):
    return 201, {"instructor": handler.server.service.add_instructor(handler._json_body())}


def h_add_instructor_busy(handler, actor, query):
    return 201, {"busy": handler.server.service.add_instructor_busy(handler._json_body())}


def h_admin_sweep(handler, actor, query):
    return 200, {"released": handler.server.service.sweep_expired()}


def h_catalog(handler, actor, query):
    return 200, handler.server.service.catalog()


def h_create_application(handler, actor, query):
    _, actor_id = actor
    booking = handler.server.service.create_application(handler._json_body(), actor_id)
    return 201, {
        "booking": booking,
        "message": "已暂锁全部资源，请在 hold_expires_at 前确认",
    }


def h_list_bookings(handler, actor, query):
    _, actor_id = actor
    return 200, {"bookings": handler.server.service.list_bookings(actor_id)}


def h_get_booking(handler, actor, query, booking_id):
    _, actor_id = actor
    return 200, {"booking": handler.server.service.get_booking_view(booking_id, actor_id)}


def h_confirm(handler, actor, query, booking_id):
    _, actor_id = actor
    return 200, {"booking": handler.server.service.confirm_booking(booking_id, actor_id)}


def h_cancel(handler, actor, query, booking_id):
    _, actor_id = actor
    return 200, {"booking": handler.server.service.cancel_booking(booking_id, actor_id)}


def h_reschedule(handler, actor, query, booking_id):
    _, actor_id = actor
    body = handler._json_body()
    booking = handler.server.service.reschedule_booking(
        booking_id, actor_id, body.get("start_local"), body.get("timezone"))
    return 200, {"booking": booking}


def h_start_use(handler, actor, query, booking_id):
    _, actor_id = actor
    return 200, {"booking": handler.server.service.start_use(booking_id, actor_id)}


def h_complete(handler, actor, query, booking_id):
    _, actor_id = actor
    return 200, {"booking": handler.server.service.complete_booking(booking_id, actor_id)}


def h_plan(handler, actor, query):
    _, actor_id = actor
    plan = handler.server.service.occupancy_plan(
        actor_id, _one(query, "start_local"), _one(query, "end_local"), _one(query, "timezone"))
    return 200, plan


def h_deactivate(handler, actor, query, equipment_id):
    _, actor_id = actor
    return 200, handler.server.service.deactivate_equipment(equipment_id, actor_id)


def h_activate(handler, actor, query, equipment_id):
    _, actor_id = actor
    return 200, handler.server.service.activate_equipment(equipment_id, actor_id)


def h_create_maintenance(handler, actor, query, equipment_id):
    _, actor_id = actor
    body = handler._json_body()
    result = handler.server.service.create_maintenance_window(
        equipment_id, body.get("start_local"), body.get("end_local"),
        body.get("timezone"), body.get("reason", ""), actor_id)
    return 201, result


def h_extend_maintenance(handler, actor, query, window_id):
    _, actor_id = actor
    body = handler._json_body()
    return 200, handler.server.service.extend_maintenance_window(
        window_id, body.get("new_end_local"), body.get("timezone"), actor_id)


ROUTES = [
    ("GET", r"/health", "public", h_health),
    ("POST", r"/admin/institutions", "admin", h_add_institution),
    ("POST", r"/admin/equipment", "admin", h_add_equipment),
    ("POST", r"/admin/workstations", "admin", h_add_workstation),
    ("POST", r"/admin/instructors", "admin", h_add_instructor),
    ("POST", r"/admin/instructor-busy", "admin", h_add_instructor_busy),
    ("POST", r"/admin/sweep", "admin", h_admin_sweep),
    ("GET", r"/catalog", "institution", h_catalog),
    ("POST", r"/applications", "institution", h_create_application),
    ("GET", r"/bookings", "institution", h_list_bookings),
    ("GET", r"/bookings/(?P<booking_id>[^/]+)", "institution", h_get_booking),
    ("POST", r"/bookings/(?P<booking_id>[^/]+)/confirm", "institution", h_confirm),
    ("POST", r"/bookings/(?P<booking_id>[^/]+)/cancel", "institution", h_cancel),
    ("POST", r"/bookings/(?P<booking_id>[^/]+)/reschedule", "institution", h_reschedule),
    ("POST", r"/bookings/(?P<booking_id>[^/]+)/start", "institution", h_start_use),
    ("POST", r"/bookings/(?P<booking_id>[^/]+)/complete", "institution", h_complete),
    ("GET", r"/plan", "institution", h_plan),
    ("POST", r"/equipment/(?P<equipment_id>[^/]+)/deactivate", "actor", h_deactivate),
    ("POST", r"/equipment/(?P<equipment_id>[^/]+)/activate", "actor", h_activate),
    ("POST", r"/equipment/(?P<equipment_id>[^/]+)/maintenance-windows", "actor", h_create_maintenance),
    ("POST", r"/maintenance-windows/(?P<window_id>[^/]+)/extend", "actor", h_extend_maintenance),
]


def make_server(service, host: str = "127.0.0.1", port: int = 8080,
                admin_token: str = "dev-admin-token") -> ThreadingHTTPServer:
    """构建 HTTP 服务；service 与 admin_token 挂在 server 上供路由使用。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "ResourceCenter/0.1"

        def log_message(self, format, *args):  # noqa: A002 - 保持基类签名
            pass  # 静默访问日志，避免干扰调用方

        # --- 基础工具 ---
        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是合法 JSON") from exc

        def _actor(self, auth: str) -> tuple[str, str | None]:
            token = self.headers.get("X-Admin-Token")
            institution = self.headers.get("X-Institution-Id")
            if auth == "public":
                return ("public", None)
            if auth == "admin":
                if token != self.server.admin_token:
                    raise UnauthorizedError("缺少或错误的管理员令牌")
                return ("admin", None)
            if auth == "actor" and token == self.server.admin_token:
                return ("admin", None)
            if not institution:
                raise UnauthorizedError("缺少 X-Institution-Id 请求头")
            if self.server.service.store.get_institution(institution) is None:
                raise UnauthorizedError(f"未知机构：{institution}")
            return ("institution", institution)

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            for route_method, pattern, auth, handler_fn in ROUTES:
                if route_method != method:
                    continue
                match = re.fullmatch(pattern, parsed.path)
                if not match:
                    continue
                try:
                    actor = self._actor(auth)
                    status, payload = handler_fn(self, actor, query, **match.groupdict())
                    self._send(status, payload)
                except ServiceError as exc:
                    body = {"error": {"code": exc.code, "message": str(exc)}}
                    if isinstance(exc, ConflictError):
                        body["conflicts"] = exc.conflicts
                        body["alternatives"] = exc.alternatives
                    self._send(ERROR_STATUS.get(exc.code, 500), body)
                except Exception as exc:  # noqa: BLE001 - 兜底，避免连接直接断开
                    self._send(500, {"error": {"code": "internal",
                                               "message": f"服务内部错误：{exc}"}})
                return
            self._send(404, {"error": {"code": "not_found", "message": "接口不存在"}})

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

    server = ThreadingHTTPServer((host, port), Handler)
    server.service = service
    server.admin_token = admin_token
    return server
