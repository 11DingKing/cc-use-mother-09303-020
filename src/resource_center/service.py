"""资源中心业务服务。

把设备能力、工位容量、维护窗口、课程需求、跨时区时段与指导教师日程汇成
整体占用计划：

- 申请先暂锁全部资源再确认，任一资源冲突则整体失败并返回可解释替代方案；
- 设备停用、维护窗口延长、改期与取消在同一事务内原子更新全部关联占用，
  不会出现"只释放机器、工位和教师仍被占用"的残留；
- 过期暂占在服务恢复后继续释放；
- 查询按机构隔离，各机构只能看到与自己预约相关的细节。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from .models import (
    ACTIVE_BOOKING_STATES,
    ACTIVE_OCCUPANCY_STATES,
    ADMIN_INSTITUTION,
    BookingRequest,
    BookingStatus,
    EquipmentNeed,
    OccupancyStatus,
    ResourceType,
    TERMINAL_BOOKING_STATES,
    UTC,
    format_instant,
    parse_stored,
)
from .store import Store

DEFAULT_HOLD_TTL_SECONDS = 900
SHIFT_STEP = timedelta(minutes=30)
SHIFT_HORIZON = timedelta(days=7)
MAX_ALTERNATIVES = 3


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class ServiceError(Exception):
    """业务错误基类：携带 HTTP 状态码与稳定错误码。"""

    status = 400
    error = "bad_request"

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class NotFoundError(ServiceError):
    status = 404
    error = "not_found"


class AccessDeniedError(ServiceError):
    status = 403
    error = "access_denied"


class StateError(ServiceError):
    status = 409
    error = "invalid_state"


class ConflictError(ServiceError):
    """资源冲突：附带逐条冲突解释与可执行的替代方案。"""

    status = 409
    error = "conflict"

    def __init__(self, conflicts: list[dict], alternatives: list[dict]):
        super().__init__("资源需求与现有占用计划冲突")
        self.conflicts = conflicts
        self.alternatives = alternatives


class ResourceCenterService:
    """资源中心核心服务：所有写操作以事务为原子边界。"""

    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------
    # 资源登记
    # ------------------------------------------------------------------

    def register_institution(self, institution_id: str, name: str, kind: str = "institution") -> dict:
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO institutions (id, name, kind) VALUES (?, ?, ?)",
                (institution_id, name, kind),
            )
        return {"id": institution_id, "name": name, "kind": kind}

    def add_equipment(
        self,
        institution_id: str,
        name: str,
        capabilities: list[str],
        equipment_id: str | None = None,
    ) -> dict:
        equipment_id = equipment_id or _new_id("eq")
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO equipment (id, institution_id, name, capabilities, status) VALUES (?, ?, ?, ?, 'active')",
                (equipment_id, institution_id, name, json.dumps(sorted(capabilities), ensure_ascii=False)),
            )
        return {"id": equipment_id, "institution_id": institution_id, "name": name,
                "capabilities": sorted(capabilities), "status": "active"}

    def add_workstation(
        self,
        institution_id: str,
        name: str,
        capacity: int,
        workstation_id: str | None = None,
    ) -> dict:
        if capacity <= 0:
            raise ServiceError("工位容量必须为正数")
        workstation_id = workstation_id or _new_id("ws")
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO workstations (id, institution_id, name, capacity) VALUES (?, ?, ?, ?)",
                (workstation_id, institution_id, name, capacity),
            )
        return {"id": workstation_id, "institution_id": institution_id, "name": name, "capacity": capacity}

    def add_instructor(
        self,
        institution_id: str,
        name: str,
        timezone: str,
        instructor_id: str | None = None,
    ) -> dict:
        instructor_id = instructor_id or _new_id("ins")
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO instructors (id, institution_id, name, timezone) VALUES (?, ?, ?, ?)",
                (instructor_id, institution_id, name, timezone),
            )
        return {"id": instructor_id, "institution_id": institution_id, "name": name, "timezone": timezone}

    def add_instructor_availability(self, instructor_id: str, start: datetime, end: datetime) -> dict:
        """登记指导教师可用日程窗口（UTC 存储，录入侧负责跨时区换算）。"""
        if end <= start:
            raise ServiceError("可用窗口结束必须晚于开始")
        with self.store.transaction() as conn:
            self._require_row(conn, "instructors", instructor_id, "指导教师")
            conn.execute(
                "INSERT INTO instructor_windows (instructor_id, start_utc, end_utc) VALUES (?, ?, ?)",
                (instructor_id, format_instant(start), format_instant(end)),
            )
        return {"instructor_id": instructor_id, "start": format_instant(start), "end": format_instant(end)}

    def create_maintenance_window(
        self,
        equipment_id: str,
        start: datetime,
        end: datetime,
        reason: str = "",
        window_id: str | None = None,
    ) -> dict:
        if end <= start:
            raise ServiceError("维护窗口结束必须晚于开始")
        window_id = window_id or _new_id("mw")
        with self.store.transaction() as conn:
            self._require_row(conn, "equipment", equipment_id, "设备")
            conn.execute(
                "INSERT INTO maintenance_windows (id, equipment_id, start_utc, end_utc, reason, status)"
                " VALUES (?, ?, ?, ?, ?, 'active')",
                (window_id, equipment_id, format_instant(start), format_instant(end), reason),
            )
        return {"id": window_id, "equipment_id": equipment_id,
                "start": format_instant(start), "end": format_instant(end), "reason": reason}

    # ------------------------------------------------------------------
    # 预约生命周期：暂锁 → 确认 → 使用 → 释放
    # ------------------------------------------------------------------

    def request_booking(self, req: BookingRequest, now: datetime | None = None) -> dict:
        """申请预约：同一事务内暂锁全部资源，任一冲突则整体失败。"""
        now = now or datetime.now(UTC)
        self._validate_request(req)
        conflicts: list[dict] = []
        with self.store.transaction() as conn:
            self._purge_expired(conn, now)
            resources, conflicts = self._select_resources(conn, req)
            if not conflicts:
                conflicts = self._check_resources(conn, resources, req.start, req.end, req.seats)
            if not conflicts:
                booking = self._place_hold(conn, req, resources, now)
        if conflicts:
            raise ConflictError(conflicts, self._build_alternatives(req, now))
        return booking

    def confirm_booking(self, booking_id: str, actor: str, now: datetime | None = None) -> dict:
        """确认暂占：暂占转正，占用状态同步升级。"""
        now = now or datetime.now(UTC)
        with self.store.transaction() as conn:
            self._purge_expired(conn, now)
            row = self._booking_row(conn, booking_id)
            self._require_consumer(row, actor)
            if row["status"] != BookingStatus.HELD.value:
                raise StateError(f"预约当前状态为 {row['status']}，不能确认")
            stamp = format_instant(now)
            conn.execute(
                "UPDATE bookings SET status=?, hold_expires_at=NULL, updated_at=? WHERE id=?",
                (BookingStatus.CONFIRMED.value, stamp, booking_id),
            )
            conn.execute(
                "UPDATE occupancies SET status=? WHERE booking_id=? AND status=?",
                (OccupancyStatus.CONFIRMED.value, booking_id, OccupancyStatus.HELD.value),
            )
            self._event(conn, booking_id, "confirmed", {"actor": actor}, now)
            return self._booking_view(conn, booking_id)

    def mark_in_use(self, booking_id: str, actor: str, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        with self.store.transaction() as conn:
            row = self._booking_row(conn, booking_id)
            self._require_consumer(row, actor)
            if row["status"] != BookingStatus.CONFIRMED.value:
                raise StateError(f"预约当前状态为 {row['status']}，不能进入使用")
            conn.execute(
                "UPDATE bookings SET status=?, updated_at=? WHERE id=?",
                (BookingStatus.IN_USE.value, format_instant(now), booking_id),
            )
            self._event(conn, booking_id, "in_use", {"actor": actor}, now)
            return self._booking_view(conn, booking_id)

    def complete_booking(self, booking_id: str, actor: str, now: datetime | None = None) -> dict:
        """课程结束：原子释放全部关联资源占用。"""
        now = now or datetime.now(UTC)
        with self.store.transaction() as conn:
            row = self._booking_row(conn, booking_id)
            self._require_consumer(row, actor)
            if row["status"] not in (BookingStatus.CONFIRMED.value, BookingStatus.IN_USE.value):
                raise StateError(f"预约当前状态为 {row['status']}，不能完成")
            self._release_occupancies(conn, booking_id)
            conn.execute(
                "UPDATE bookings SET status=?, updated_at=? WHERE id=?",
                (BookingStatus.RELEASED.value, format_instant(now), booking_id),
            )
            self._event(conn, booking_id, "released", {"actor": actor}, now)
            return self._booking_view(conn, booking_id)

    def cancel_booking(self, booking_id: str, actor: str, now: datetime | None = None) -> dict:
        """取消预约：同一事务内释放全部关联占用。"""
        now = now or datetime.now(UTC)
        with self.store.transaction() as conn:
            self._purge_expired(conn, now)
            row = self._booking_row(conn, booking_id)
            self._require_consumer(row, actor)
            if row["status"] in TERMINAL_BOOKING_STATES:
                raise StateError(f"预约当前状态为 {row['status']}，不能取消")
            self._release_occupancies(conn, booking_id)
            conn.execute(
                "UPDATE bookings SET status=?, hold_expires_at=NULL, updated_at=? WHERE id=?",
                (BookingStatus.CANCELLED.value, format_instant(now), booking_id),
            )
            self._event(conn, booking_id, "cancelled", {"actor": actor}, now)
            return self._booking_view(conn, booking_id)

    def reschedule_booking(
        self,
        booking_id: str,
        actor: str,
        new_start: datetime,
        new_end: datetime,
        now: datetime | None = None,
    ) -> dict:
        """改期：新时段全部资源可行才原子更新所有关联占用，否则整体不变。"""
        now = now or datetime.now(UTC)
        if new_end <= new_start:
            raise ServiceError("改期结束必须晚于开始")
        conflicts: list[dict] = []
        with self.store.transaction() as conn:
            self._purge_expired(conn, now)
            row = self._booking_row(conn, booking_id)
            self._require_consumer(row, actor)
            status = row["status"]
            if status not in (
                BookingStatus.HELD.value,
                BookingStatus.CONFIRMED.value,
                BookingStatus.DISRUPTED.value,
            ):
                raise StateError(f"预约当前状态为 {status}，不能改期")
            req = self._request_from_row(row, new_start, new_end)
            if status == BookingStatus.DISRUPTED.value:
                resources, conflicts = self._select_resources(conn, req)
            else:
                resources = self._booking_resources(conn, booking_id)
            if not conflicts:
                conflicts = self._check_resources(
                    conn, resources, new_start, new_end, row["seats"], ignore_booking_id=booking_id
                )
            if not conflicts:
                stamp = format_instant(now)
                if status == BookingStatus.DISRUPTED.value:
                    # 受扰预约重新安排：按原需求快照重建全部占用，直接恢复确认态。
                    self._insert_occupancies(conn, booking_id, resources, new_start, new_end,
                                             OccupancyStatus.CONFIRMED.value)
                    conn.execute(
                        "UPDATE bookings SET status=?, start_utc=?, end_utc=?, disruption=NULL, updated_at=? WHERE id=?",
                        (BookingStatus.CONFIRMED.value, format_instant(new_start),
                         format_instant(new_end), stamp, booking_id),
                    )
                else:
                    updates: list[Any] = [format_instant(new_start), format_instant(new_end), stamp]
                    hold_clause = ""
                    if status == BookingStatus.HELD.value:
                        hold_clause = ", hold_expires_at=?"
                        updates.append(format_instant(now + timedelta(seconds=req.hold_ttl_seconds)))
                    updates.append(booking_id)
                    conn.execute(
                        f"UPDATE bookings SET start_utc=?, end_utc=?, updated_at=?{hold_clause} WHERE id=?",
                        updates,
                    )
                    conn.execute(
                        "UPDATE occupancies SET start_utc=?, end_utc=? WHERE booking_id=? AND status IN (?, ?)",
                        (format_instant(new_start), format_instant(new_end), booking_id,
                         *ACTIVE_OCCUPANCY_STATES),
                    )
                self._event(conn, booking_id, "rescheduled",
                            {"actor": actor, "start": format_instant(new_start),
                             "end": format_instant(new_end)}, now)
        if conflicts:
            raise ConflictError(conflicts, self._build_alternatives(req, now))
        with self.store.read() as conn:
            return self._booking_view(conn, booking_id)

    # ------------------------------------------------------------------
    # 资源侧变更：维护窗口延长、设备停用——原子更新全部关联占用
    # ------------------------------------------------------------------

    def extend_maintenance_window(
        self,
        window_id: str,
        new_end: datetime,
        actor: str,
        now: datetime | None = None,
    ) -> dict:
        """延长维护窗口：受影响预约在同一事务内整体改期或整体释放。

        释放是组合级的——设备、工位、指导教师的关联占用一起释放，
        并为受扰预约生成可解释替代方案，保证后续课程可以重新安排。
        """
        now = now or datetime.now(UTC)
        with self.store.transaction() as conn:
            self._purge_expired(conn, now)
            window = self._require_row(conn, "maintenance_windows", window_id, "维护窗口")
            equipment = self._require_row(conn, "equipment", window["equipment_id"], "设备")
            self._require_provider(equipment, actor)
            old_end = parse_stored(window["end_utc"])
            if new_end <= old_end:
                raise ServiceError("新的维护结束时间必须晚于原结束时间")
            conn.execute(
                "UPDATE maintenance_windows SET end_utc=? WHERE id=?",
                (format_instant(new_end), window_id),
            )
            cause = f"设备 {equipment['name']} 维护窗口延长至 {format_instant(new_end)}"
            impact = self._absorb_equipment_impact(
                conn, equipment["id"], parse_stored(window["start_utc"]), new_end, now, cause
            )
            self._event(conn, None, "maintenance_extended",
                        {"window_id": window_id, "new_end": format_instant(new_end)}, now)
            return {"window_id": window_id, "equipment_id": equipment["id"],
                    "new_end": format_instant(new_end), "impact": impact}

    def disable_equipment(self, equipment_id: str, actor: str, now: datetime | None = None) -> dict:
        """设备停用：未来关联占用在同一事务内整体改期或整体释放。"""
        now = now or datetime.now(UTC)
        with self.store.transaction() as conn:
            self._purge_expired(conn, now)
            equipment = self._require_row(conn, "equipment", equipment_id, "设备")
            self._require_provider(equipment, actor)
            conn.execute("UPDATE equipment SET status='disabled' WHERE id=?", (equipment_id,))
            cause = f"设备 {equipment['name']} 已停用"
            impact = self._absorb_equipment_impact(
                conn, equipment_id, now, None, now, cause
            )
            self._event(conn, None, "equipment_disabled", {"equipment_id": equipment_id}, now)
            return {"equipment_id": equipment_id, "status": "disabled", "impact": impact}

    def enable_equipment(self, equipment_id: str, actor: str) -> dict:
        with self.store.transaction() as conn:
            equipment = self._require_row(conn, "equipment", equipment_id, "设备")
            self._require_provider(equipment, actor)
            conn.execute("UPDATE equipment SET status='active' WHERE id=?", (equipment_id,))
        return {"equipment_id": equipment_id, "status": "active"}

    # ------------------------------------------------------------------
    # 过期暂占：运行期清理与服务恢复后继续释放
    # ------------------------------------------------------------------

    def release_expired_holds(self, now: datetime | None = None) -> dict:
        """释放所有过期暂占：预约转过期终态，关联占用原子释放。"""
        now = now or datetime.now(UTC)
        with self.store.transaction() as conn:
            released = self._purge_expired(conn, now)
        return {"released_holds": released}

    def recover(self, now: datetime | None = None) -> dict:
        """服务恢复入口：重启后继续释放重启前已过期的暂占。"""
        now = now or datetime.now(UTC)
        result = self.release_expired_holds(now)
        return {"recovered_at": format_instant(now), **result}

    # ------------------------------------------------------------------
    # 查询：机构隔离视图
    # ------------------------------------------------------------------

    def get_booking(self, booking_id: str, actor: str) -> dict:
        with self.store.read() as conn:
            row = self._booking_row(conn, booking_id)
            if not self._can_view(conn, row, actor):
                # 对外不区分"不存在"与"无权查看"，避免跨机构探测。
                raise NotFoundError("预约不存在或无权查看")
            return self._booking_view(conn, booking_id)

    def list_bookings(self, actor: str) -> list[dict]:
        with self.store.read() as conn:
            rows = conn.execute("SELECT * FROM bookings ORDER BY created_at DESC").fetchall()
            return [
                self._booking_view(conn, row["id"])
                for row in rows
                if self._can_view(conn, row, actor)
            ]

    def occupancy_plan(self, actor: str, start: datetime, end: datetime) -> dict:
        """整体占用计划：本机构相关预约展示细节，他机构预约脱敏为忙碌块。"""
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT o.*, b.institution_id, b.course_name, b.status AS booking_status"
                " FROM occupancies o JOIN bookings b ON b.id = o.booking_id"
                " WHERE o.status IN (?, ?) AND o.start_utc < ? AND o.end_utc > ?"
                " ORDER BY o.start_utc",
                (*ACTIVE_OCCUPANCY_STATES, format_instant(end), format_instant(start)),
            ).fetchall()
            items = []
            for occ in rows:
                base = {
                    "booking_id": occ["booking_id"],
                    "resource_type": occ["resource_type"],
                    "resource_id": occ["resource_id"],
                    "start": occ["start_utc"],
                    "end": occ["end_utc"],
                    "status": occ["status"],
                }
                if self._can_view(conn, occ, actor):
                    items.append({**base, "course_name": occ["course_name"],
                                  "institution_id": occ["institution_id"],
                                  "seats": occ["seats"], "visibility": "full"})
                else:
                    items.append({**base, "visibility": "redacted"})
            maintenance = conn.execute(
                "SELECT mw.*, e.name AS equipment_name FROM maintenance_windows mw"
                " JOIN equipment e ON e.id = mw.equipment_id"
                " WHERE mw.status='active' AND mw.start_utc < ? AND mw.end_utc > ?",
                (format_instant(end), format_instant(start)),
            ).fetchall()
            return {
                "start": format_instant(start),
                "end": format_instant(end),
                "occupancies": items,
                "maintenance_windows": [
                    {"window_id": mw["id"], "equipment_id": mw["equipment_id"],
                     "equipment_name": mw["equipment_name"], "start": mw["start_utc"],
                     "end": mw["end_utc"], "reason": mw["reason"]}
                    for mw in maintenance
                ],
            }

    # ------------------------------------------------------------------
    # 内部：资源选择与冲突检测
    # ------------------------------------------------------------------

    def _validate_request(self, req: BookingRequest) -> None:
        if req.end <= req.start:
            raise ServiceError("课程结束必须晚于开始")
        if req.seats < 0:
            raise ServiceError("工位需求不能为负")
        if not req.equipment and not req.seats and not req.instructor_id:
            raise ServiceError("预约至少需要一项资源需求（设备、工位或指导教师）")
        if req.hold_ttl_seconds <= 0:
            raise ServiceError("暂锁时长必须为正数")
        with self.store.read() as conn:
            self._require_row(conn, "institutions", req.institution_id, "机构")

    def _select_resources(self, conn, req: BookingRequest) -> tuple[list[dict], list[dict]]:
        """按需求选择资源：设备按能力匹配，工位按容量匹配，教师显式指定。"""
        resources: list[dict] = []
        conflicts: list[dict] = []
        for need in req.equipment:
            if need.equipment_id:
                row = self._require_row_or_none(conn, "equipment", need.equipment_id)
                if row is None:
                    conflicts.append(self._conflict(
                        ResourceType.EQUIPMENT.value, need.equipment_id, "not_found",
                        f"设备 {need.equipment_id} 不存在"))
                    continue
                missing = sorted(set(need.capabilities) - set(json.loads(row["capabilities"])))
                if missing:
                    conflicts.append(self._conflict(
                        ResourceType.EQUIPMENT.value, row["id"], "capability_mismatch",
                        f"设备 {row['name']} 缺少能力：{'、'.join(missing)}"))
                    continue
                resources.append({"type": ResourceType.EQUIPMENT.value, "id": row["id"],
                                  "name": row["name"], "seats": 0})
                continue
            chosen = None
            for row in conn.execute("SELECT * FROM equipment WHERE status='active' ORDER BY id"):
                if not set(need.capabilities) <= set(json.loads(row["capabilities"])):
                    continue
                candidate = [{"type": ResourceType.EQUIPMENT.value, "id": row["id"], "seats": 0}]
                if not self._check_resources(conn, candidate, req.start, req.end, 0):
                    chosen = row
                    break
            if chosen is None:
                wanted = "、".join(need.capabilities) or "任意"
                conflicts.append(self._conflict(
                    ResourceType.EQUIPMENT.value, "", "no_available_equipment",
                    f"没有满足能力 [{wanted}] 且时段可用的设备",
                    blocking={"capabilities": list(need.capabilities)}))
            else:
                resources.append({"type": ResourceType.EQUIPMENT.value, "id": chosen["id"],
                                  "name": chosen["name"], "seats": 0})
        if req.seats > 0:
            if req.workstation_id:
                row = self._require_row_or_none(conn, "workstations", req.workstation_id)
                if row is None:
                    conflicts.append(self._conflict(
                        ResourceType.WORKSTATION.value, req.workstation_id, "not_found",
                        f"工位 {req.workstation_id} 不存在"))
                elif row["capacity"] < req.seats:
                    conflicts.append(self._conflict(
                        ResourceType.WORKSTATION.value, row["id"], "capacity_exceeded",
                        f"工位 {row['name']} 容量 {row['capacity']} 小于需求 {req.seats}"))
                else:
                    resources.append({"type": ResourceType.WORKSTATION.value, "id": row["id"],
                                      "name": row["name"], "seats": req.seats})
            else:
                chosen = None
                for row in conn.execute("SELECT * FROM workstations ORDER BY capacity ASC, id"):
                    if row["capacity"] < req.seats:
                        continue
                    candidate = [{"type": ResourceType.WORKSTATION.value, "id": row["id"],
                                  "seats": req.seats}]
                    if not self._check_resources(conn, candidate, req.start, req.end, req.seats):
                        chosen = row
                        break
                if chosen is None:
                    conflicts.append(self._conflict(
                        ResourceType.WORKSTATION.value, "", "no_available_workstation",
                        f"没有容量满足 {req.seats} 人且时段可用的工位"))
                else:
                    resources.append({"type": ResourceType.WORKSTATION.value, "id": chosen["id"],
                                      "name": chosen["name"], "seats": req.seats})
        if req.instructor_id:
            row = self._require_row_or_none(conn, "instructors", req.instructor_id)
            if row is None:
                conflicts.append(self._conflict(
                    ResourceType.INSTRUCTOR.value, req.instructor_id, "not_found",
                    f"指导教师 {req.instructor_id} 不存在"))
            else:
                resources.append({"type": ResourceType.INSTRUCTOR.value, "id": row["id"],
                                  "name": row["name"], "seats": 0})
        return resources, conflicts

    def _check_resources(
        self,
        conn,
        resources: list[dict],
        start: datetime,
        end: datetime,
        seats_needed: int,
        ignore_booking_id: str | None = None,
    ) -> list[dict]:
        """检查一组资源在时段内是否全部可行，返回逐条可解释冲突。"""
        conflicts: list[dict] = []
        start_s, end_s = format_instant(start), format_instant(end)
        ignore = ignore_booking_id or ""
        for res in resources:
            rtype, rid = res["type"], res["id"]
            if rtype == ResourceType.EQUIPMENT.value:
                row = self._require_row_or_none(conn, "equipment", rid)
                if row is None:
                    conflicts.append(self._conflict(rtype, rid, "not_found", f"设备 {rid} 不存在"))
                    continue
                if row["status"] != "active":
                    conflicts.append(self._conflict(
                        rtype, rid, "equipment_disabled", f"设备 {row['name']} 已停用"))
                for mw in conn.execute(
                    "SELECT * FROM maintenance_windows"
                    " WHERE equipment_id=? AND status='active' AND start_utc<? AND end_utc>?",
                    (rid, end_s, start_s),
                ):
                    conflicts.append(self._conflict(
                        rtype, rid, "maintenance_window",
                        f"设备 {row['name']} 处于维护窗口（{mw['start_utc']} ~ {mw['end_utc']}）：{mw['reason']}",
                        blocking={"window_id": mw["id"], "start": mw["start_utc"], "end": mw["end_utc"]}))
                conflicts.extend(self._overlap_conflicts(conn, res, start_s, end_s, ignore, row["name"]))
            elif rtype == ResourceType.WORKSTATION.value:
                row = self._require_row_or_none(conn, "workstations", rid)
                if row is None:
                    conflicts.append(self._conflict(rtype, rid, "not_found", f"工位 {rid} 不存在"))
                    continue
                used = conn.execute(
                    "SELECT COALESCE(SUM(seats), 0) AS used FROM occupancies"
                    " WHERE resource_type=? AND resource_id=? AND status IN (?, ?)"
                    " AND start_utc<? AND end_utc>? AND booking_id!=?",
                    (rtype, rid, *ACTIVE_OCCUPANCY_STATES, end_s, start_s, ignore),
                ).fetchone()["used"]
                if used + seats_needed > row["capacity"]:
                    conflicts.append(self._conflict(
                        rtype, rid, "capacity_exceeded",
                        f"工位 {row['name']} 容量不足：时段内已占用 {used}/{row['capacity']} 座，需求 {seats_needed} 座",
                        blocking={"used": used, "capacity": row["capacity"]}))
            elif rtype == ResourceType.INSTRUCTOR.value:
                row = self._require_row_or_none(conn, "instructors", rid)
                if row is None:
                    conflicts.append(self._conflict(rtype, rid, "not_found", f"指导教师 {rid} 不存在"))
                    continue
                covered = conn.execute(
                    "SELECT COUNT(*) AS n FROM instructor_windows"
                    " WHERE instructor_id=? AND start_utc<=? AND end_utc>=?",
                    (rid, start_s, end_s),
                ).fetchone()["n"]
                if not covered:
                    conflicts.append(self._conflict(
                        rtype, rid, "instructor_unavailable",
                        f"指导教师 {row['name']} 的日程不覆盖该时段"))
                conflicts.extend(self._overlap_conflicts(conn, res, start_s, end_s, ignore, row["name"]))
        return conflicts

    def _overlap_conflicts(self, conn, res: dict, start_s: str, end_s: str,
                           ignore: str, resource_name: str) -> list[dict]:
        """排他资源（设备、指导教师）的重叠占用冲突；不泄露对方课程细节。"""
        rows = conn.execute(
            "SELECT start_utc, end_utc FROM occupancies"
            " WHERE resource_type=? AND resource_id=? AND status IN (?, ?)"
            " AND start_utc<? AND end_utc>? AND booking_id!=?",
            (res["type"], res["id"], *ACTIVE_OCCUPANCY_STATES, end_s, start_s, ignore),
        ).fetchall()
        label = {"equipment": "设备", "instructor": "指导教师"}.get(res["type"], "资源")
        return [
            self._conflict(
                res["type"], res["id"], "occupancy_overlap",
                f"{label} {resource_name} 在 {row['start_utc']} ~ {row['end_utc']} 已被占用",
                blocking={"start": row["start_utc"], "end": row["end_utc"]})
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 内部：替代方案
    # ------------------------------------------------------------------

    def _build_alternatives(self, req: BookingRequest, now: datetime) -> list[dict]:
        """生成可解释替代方案：整体平移时段 + 同能力设备替换。"""
        alternatives: list[dict] = []
        with self.store.read() as conn:
            resources = self._best_effort_resources(conn, req)
            if resources:
                earliest = max(now, req.start)
                for start, end in self._shift_candidates(
                    conn, resources, req.seats, req.duration, earliest
                ):
                    alternatives.append({
                        "kind": "shift_time",
                        "summary": f"整体平移到 {format_instant(start)} ~ {format_instant(end)}，全部资源可用",
                        "start": format_instant(start),
                        "end": format_instant(end),
                    })
            for need in req.equipment:
                if not need.equipment_id:
                    continue
                alternatives.extend(self._substitute_equipment(conn, need.equipment_id, req))
        return alternatives[: MAX_ALTERNATIVES * 2]

    def _best_effort_resources(self, conn, req: BookingRequest) -> list[dict]:
        """尽力构建资源集合（忽略冲突），用于平移扫描。"""
        resources: list[dict] = []
        for need in req.equipment:
            if need.equipment_id:
                row = self._require_row_or_none(conn, "equipment", need.equipment_id)
                if row is not None:
                    resources.append({"type": ResourceType.EQUIPMENT.value, "id": row["id"], "seats": 0})
                continue
            row = conn.execute(
                "SELECT id, capabilities FROM equipment WHERE status='active' ORDER BY id"
            ).fetchall()
            matched = next(
                (r for r in row if set(need.capabilities) <= set(json.loads(r["capabilities"]))),
                None,
            )
            if matched is None:
                return []
            resources.append({"type": ResourceType.EQUIPMENT.value, "id": matched["id"], "seats": 0})
        if req.seats > 0:
            if req.workstation_id:
                row = self._require_row_or_none(conn, "workstations", req.workstation_id)
                if row is None:
                    return []
                resources.append({"type": ResourceType.WORKSTATION.value, "id": row["id"],
                                  "seats": req.seats})
            else:
                row = conn.execute(
                    "SELECT id FROM workstations WHERE capacity>=? ORDER BY capacity ASC, id",
                    (req.seats,),
                ).fetchone()
                if row is None:
                    return []
                resources.append({"type": ResourceType.WORKSTATION.value, "id": row["id"],
                                  "seats": req.seats})
        if req.instructor_id:
            row = self._require_row_or_none(conn, "instructors", req.instructor_id)
            if row is None:
                return []
            resources.append({"type": ResourceType.INSTRUCTOR.value, "id": row["id"], "seats": 0})
        return resources

    def _substitute_equipment(self, conn, equipment_id: str, req: BookingRequest) -> list[dict]:
        """同能力设备替换建议：原时段内可直接替换的备选设备。"""
        row = self._require_row_or_none(conn, "equipment", equipment_id)
        if row is None:
            return []
        caps = set(json.loads(row["capabilities"]))
        suggestions = []
        for other in conn.execute(
            "SELECT * FROM equipment WHERE id!=? AND status='active' ORDER BY id", (equipment_id,)
        ):
            if not caps <= set(json.loads(other["capabilities"])):
                continue
            candidate = [{"type": ResourceType.EQUIPMENT.value, "id": other["id"], "seats": 0}]
            if not self._check_resources(conn, candidate, req.start, req.end, 0):
                suggestions.append({
                    "kind": "substitute_equipment",
                    "summary": f"设备 {row['name']} 可替换为同能力设备 {other['name']}（原时段可行）",
                    "replaces": equipment_id,
                    "equipment_id": other["id"],
                })
            if len(suggestions) >= MAX_ALTERNATIVES:
                break
        return suggestions

    def _shift_candidates(self, conn, resources: list[dict], seats: int,
                          duration: timedelta, earliest: datetime,
                          ignore_booking_id: str | None = None,
                          limit: int = MAX_ALTERNATIVES) -> list[tuple[datetime, datetime]]:
        """从最早时刻起按固定步长扫描，找全部资源都可行的整体平移时段。"""
        found: list[tuple[datetime, datetime]] = []
        start = earliest
        horizon = earliest + SHIFT_HORIZON
        while start + duration <= horizon and len(found) < limit:
            end = start + duration
            if not self._check_resources(conn, resources, start, end, seats, ignore_booking_id):
                found.append((start, end))
            start += SHIFT_STEP
        return found

    # ------------------------------------------------------------------
    # 内部：维护/停用冲击的组合级吸收
    # ------------------------------------------------------------------

    def _absorb_equipment_impact(self, conn, equipment_id: str, impact_start: datetime,
                                 impact_end: datetime | None, now: datetime, cause: str) -> list[dict]:
        """对受影响预约逐个整体改期或整体释放（同事务，调用方负责提交）。"""
        params: list[Any] = [ResourceType.EQUIPMENT.value, equipment_id,
                             *ACTIVE_OCCUPANCY_STATES, format_instant(impact_start)]
        end_clause = ""
        if impact_end is not None:
            end_clause = " AND start_utc < ?"
            params.append(format_instant(impact_end))
        rows = conn.execute(
            f"SELECT DISTINCT booking_id FROM occupancies"
            f" WHERE resource_type=? AND resource_id=? AND status IN (?, ?) AND end_utc > ?{end_clause}",
            params,
        ).fetchall()
        return [self._absorb_booking_impact(conn, row["booking_id"], now, cause) for row in rows]

    def _absorb_booking_impact(self, conn, booking_id: str, now: datetime, cause: str) -> dict:
        row = self._booking_row(conn, booking_id)
        if row["status"] not in ACTIVE_BOOKING_STATES:
            return {"booking_id": booking_id, "action": "skipped", "status": row["status"]}
        resources = self._booking_resources(conn, booking_id)
        duration = parse_stored(row["end_utc"]) - parse_stored(row["start_utc"])
        earliest = max(now, parse_stored(row["start_utc"]))
        slots = self._shift_candidates(conn, resources, row["seats"], duration, earliest,
                                       ignore_booking_id=booking_id, limit=1)
        stamp = format_instant(now)
        if slots:
            new_start, new_end = slots[0]
            updates: list[Any] = [format_instant(new_start), format_instant(new_end), stamp]
            hold_clause = ""
            if row["status"] == BookingStatus.HELD.value:
                ttl_seconds = json.loads(row["requirements"]).get(
                    "hold_ttl_seconds", DEFAULT_HOLD_TTL_SECONDS)
                hold_clause = ", hold_expires_at=?"
                updates.append(format_instant(now + timedelta(seconds=ttl_seconds)))
            updates.append(booking_id)
            conn.execute(
                f"UPDATE bookings SET start_utc=?, end_utc=?, updated_at=?{hold_clause} WHERE id=?",
                updates,
            )
            conn.execute(
                "UPDATE occupancies SET start_utc=?, end_utc=? WHERE booking_id=? AND status IN (?, ?)",
                (format_instant(new_start), format_instant(new_end), booking_id,
                 *ACTIVE_OCCUPANCY_STATES),
            )
            self._event(conn, booking_id, "auto_rescheduled",
                        {"cause": cause, "start": format_instant(new_start),
                         "end": format_instant(new_end)}, now)
            return {"booking_id": booking_id, "action": "rescheduled",
                    "new_start": format_instant(new_start), "new_end": format_instant(new_end),
                    "cause": cause}
        # 找不到整体可行时段：组合级释放全部关联占用，预约转入受扰待重排。
        req = self._request_from_row(row, parse_stored(row["start_utc"]), parse_stored(row["end_utc"]))
        alternatives = self._build_alternatives(req, now)
        self._release_occupancies(conn, booking_id)
        conn.execute(
            "UPDATE bookings SET status=?, hold_expires_at=NULL, disruption=?, updated_at=? WHERE id=?",
            (BookingStatus.DISRUPTED.value,
             json.dumps({"cause": cause, "released_at": stamp, "alternatives": alternatives},
                        ensure_ascii=False),
             stamp, booking_id),
        )
        self._event(conn, booking_id, "disrupted", {"cause": cause}, now)
        return {"booking_id": booking_id, "action": "released", "cause": cause,
                "alternatives": alternatives}

    # ------------------------------------------------------------------
    # 内部：过期清理、占用释放、视图与权限
    # ------------------------------------------------------------------

    def _purge_expired(self, conn, now: datetime) -> int:
        """释放过期暂占：每个写事务入口先调用，保证过期暂占不再阻塞他人。"""
        rows = conn.execute(
            "SELECT id FROM bookings WHERE status=? AND hold_expires_at IS NOT NULL AND hold_expires_at<=?",
            (BookingStatus.HELD.value, format_instant(now)),
        ).fetchall()
        for row in rows:
            self._release_occupancies(conn, row["id"])
            conn.execute(
                "UPDATE bookings SET status=?, hold_expires_at=NULL, updated_at=? WHERE id=?",
                (BookingStatus.EXPIRED.value, format_instant(now), row["id"]),
            )
            self._event(conn, row["id"], "hold_expired", {}, now)
        return len(rows)

    def _release_occupancies(self, conn, booking_id: str) -> None:
        """组合级释放：设备、工位、指导教师的关联占用一起释放。"""
        conn.execute(
            "UPDATE occupancies SET status=? WHERE booking_id=? AND status IN (?, ?)",
            (OccupancyStatus.RELEASED.value, booking_id, *ACTIVE_OCCUPANCY_STATES),
        )

    def _place_hold(self, conn, req: BookingRequest, resources: list[dict], now: datetime) -> dict:
        booking_id = _new_id("bk")
        stamp = format_instant(now)
        expires = format_instant(now + timedelta(seconds=req.hold_ttl_seconds))
        conn.execute(
            "INSERT INTO bookings (id, institution_id, course_name, status, timezone,"
            " start_utc, end_utc, seats, requirements, hold_expires_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (booking_id, req.institution_id, req.course_name, BookingStatus.HELD.value,
             req.timezone, format_instant(req.start), format_instant(req.end), req.seats,
             self._requirements_snapshot(req), expires, stamp, stamp),
        )
        self._insert_occupancies(conn, booking_id, resources, req.start, req.end,
                                 OccupancyStatus.HELD.value)
        self._event(conn, booking_id, "hold_placed",
                    {"resources": [{"type": r["type"], "id": r["id"]} for r in resources],
                     "expires_at": expires}, now)
        return self._booking_view(conn, booking_id)

    def _insert_occupancies(self, conn, booking_id: str, resources: list[dict],
                            start: datetime, end: datetime, status: str) -> None:
        for res in resources:
            conn.execute(
                "INSERT INTO occupancies (id, booking_id, resource_type, resource_id, seats,"
                " start_utc, end_utc, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_new_id("occ"), booking_id, res["type"], res["id"], res.get("seats", 0),
                 format_instant(start), format_instant(end), status),
            )

    def _booking_resources(self, conn, booking_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT DISTINCT resource_type, resource_id, seats FROM occupancies"
            " WHERE booking_id=? AND status IN (?, ?)",
            (booking_id, *ACTIVE_OCCUPANCY_STATES),
        ).fetchall()
        return [{"type": row["resource_type"], "id": row["resource_id"], "seats": row["seats"]}
                for row in rows]

    def _requirements_snapshot(self, req: BookingRequest) -> str:
        return json.dumps({
            "equipment": [{"capabilities": list(n.capabilities), "equipment_id": n.equipment_id}
                          for n in req.equipment],
            "seats": req.seats,
            "instructor_id": req.instructor_id,
            "workstation_id": req.workstation_id,
            "hold_ttl_seconds": req.hold_ttl_seconds,
        }, ensure_ascii=False)

    def _request_from_row(self, row, start: datetime, end: datetime) -> BookingRequest:
        snapshot = json.loads(row["requirements"])
        return BookingRequest(
            institution_id=row["institution_id"],
            course_name=row["course_name"],
            start=start,
            end=end,
            timezone=row["timezone"],
            equipment=[EquipmentNeed(tuple(n.get("capabilities") or ()), n.get("equipment_id"))
                       for n in snapshot.get("equipment", [])],
            seats=snapshot.get("seats", 0),
            instructor_id=snapshot.get("instructor_id"),
            workstation_id=snapshot.get("workstation_id"),
            hold_ttl_seconds=snapshot.get("hold_ttl_seconds", DEFAULT_HOLD_TTL_SECONDS),
        )

    def _booking_row(self, conn, booking_id: str):
        row = self._require_row_or_none(conn, "bookings", booking_id)
        if row is None:
            raise NotFoundError("预约不存在或无权查看")
        return row

    def _booking_view(self, conn, booking_id: str) -> dict:
        row = self._booking_row(conn, booking_id)
        occupancies = conn.execute(
            "SELECT * FROM occupancies WHERE booking_id=? ORDER BY resource_type, resource_id",
            (booking_id,),
        ).fetchall()
        view = {
            "id": row["id"],
            "institution_id": row["institution_id"],
            "course_name": row["course_name"],
            "status": row["status"],
            "timezone": row["timezone"],
            "start": row["start_utc"],
            "end": row["end_utc"],
            "seats": row["seats"],
            "hold_expires_at": row["hold_expires_at"],
            "occupancies": [
                {"id": occ["id"], "resource_type": occ["resource_type"],
                 "resource_id": occ["resource_id"], "seats": occ["seats"],
                 "start": occ["start_utc"], "end": occ["end_utc"], "status": occ["status"]}
                for occ in occupancies
            ],
        }
        if row["disruption"]:
            view["disruption"] = json.loads(row["disruption"])
        return view

    def _can_view(self, conn, row, actor: str) -> bool:
        """机构隔离：使用院校、资源提供院校与资源中心可见，其余不可见。"""
        if actor == ADMIN_INSTITUTION:
            return True
        if row["institution_id"] == actor:
            return True
        seen = conn.execute(
            "SELECT COUNT(*) AS n FROM occupancies o"
            " LEFT JOIN equipment e ON o.resource_type='equipment' AND o.resource_id=e.id"
            " LEFT JOIN workstations w ON o.resource_type='workstation' AND o.resource_id=w.id"
            " LEFT JOIN instructors i ON o.resource_type='instructor' AND o.resource_id=i.id"
            " WHERE o.booking_id=? AND (e.institution_id=? OR w.institution_id=? OR i.institution_id=?)",
            (row["booking_id"] if "booking_id" in row.keys() else row["id"],
             actor, actor, actor),
        ).fetchone()["n"]
        return seen > 0

    def _require_consumer(self, row, actor: str) -> None:
        if actor != ADMIN_INSTITUTION and row["institution_id"] != actor:
            raise AccessDeniedError("只有使用院校或资源中心可以执行该操作")

    def _require_provider(self, resource_row, actor: str) -> None:
        if actor != ADMIN_INSTITUTION and resource_row["institution_id"] != actor:
            raise AccessDeniedError("只有资源所属机构或资源中心可以执行该操作")

    @staticmethod
    def _require_row_or_none(conn, table: str, row_id: str):
        return conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()

    def _require_row(self, conn, table: str, row_id: str, label: str):
        row = self._require_row_or_none(conn, table, row_id)
        if row is None:
            raise NotFoundError(f"{label} {row_id} 不存在")
        return row

    @staticmethod
    def _conflict(resource_type: str, resource_id: str, reason: str, detail: str,
                  blocking: dict | None = None) -> dict:
        conflict = {"resource_type": resource_type, "resource_id": resource_id,
                    "reason": reason, "detail": detail}
        if blocking:
            conflict["blocking"] = blocking
        return conflict

    @staticmethod
    def _event(conn, booking_id: str | None, kind: str, payload: dict, now: datetime) -> None:
        conn.execute(
            "INSERT INTO events (booking_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (booking_id, kind, json.dumps(payload, ensure_ascii=False), format_instant(now)),
        )
