"""资源中心应用服务。

- 两阶段预约：申请时在同一事务内暂锁设备、工位与指导教师，确认后生效；
- 设备停用、改期、取消与维护窗口延长在同一事务内原子更新全部关联占用；
- 冲突时返回可解释的替代方案（同时段替代资源 / 时间平移）；
- 过期暂占由后台巡检与启动恢复继续释放；
- 机构只能查看与自己预约相关的细节，其余仅以匿名忙块呈现。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta

from . import planner
from .models import (
    CANCELLED,
    CONFIRMED,
    EXPIRED,
    HELD,
    IN_USE,
    RELEASED,
    RESOURCE_EQUIPMENT,
    RESOURCE_INSTRUCTOR,
    RESOURCE_WORKSTATION,
    CourseRequirement,
    Selection,
)
from .timeutil import iso, now_utc, parse_local, parse_utc, render_local


class ServiceError(Exception):
    code = "error"


class ValidationError(ServiceError):
    code = "validation"


class UnauthorizedError(ServiceError):
    code = "unauthorized"


class UnauthorizedError(ServiceError):
    code = "unauthorized"


class NotFoundError(ServiceError):
    code = "not_found"


class ForbiddenError(ServiceError):
    code = "forbidden"


class StateError(ServiceError):
    code = "invalid_state"


class ConflictError(ServiceError):
    code = "conflict"

    def __init__(self, conflicts: list[dict], alternatives: list[dict]):
        super().__init__("资源冲突，无法完成暂锁")
        self.conflicts = conflicts
        self.alternatives = alternatives


class ResourceCenterService:
    """资源中心核心服务；actor_institution_id 为 None 表示管理员。"""

    def __init__(self, store, hold_ttl: timedelta = timedelta(minutes=15)):
        self.store = store
        self.hold_ttl = hold_ttl
        self._sweeper_stop = threading.Event()
        self._sweeper_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 基础校验
    # ------------------------------------------------------------------
    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"

    def _require_institution(self, institution_id: str) -> dict:
        institution = self.store.get_institution(institution_id)
        if institution is None:
            raise NotFoundError(f"机构不存在：{institution_id}")
        return institution

    def _require_equipment(self, equipment_id: str) -> dict:
        equipment = self.store.get_equipment(equipment_id)
        if equipment is None:
            raise NotFoundError(f"设备不存在：{equipment_id}")
        return equipment

    def _require_booking(self, booking_id: str) -> dict:
        booking = self.store.get_booking(booking_id)
        if booking is None:
            raise NotFoundError("预约不存在")
        return booking

    @staticmethod
    def _require_related(booking: dict, actor_institution_id: str | None) -> None:
        """机构隔离：无关机构一律得到 404，不泄露预约存在性。"""
        if actor_institution_id is None:
            return
        related = (booking["consumer_institution_id"], booking["provider_institution_id"])
        if actor_institution_id not in related:
            raise NotFoundError("预约不存在")

    @staticmethod
    def _require_owner_or_admin(equipment: dict, actor_institution_id: str | None) -> None:
        if actor_institution_id is not None and equipment["institution_id"] != actor_institution_id:
            raise ForbiddenError("仅设备所属机构可以执行该操作")

    @staticmethod
    def _parse_local(value, tz_name: str) -> datetime:
        if not value:
            raise ValidationError("缺少时间字段")
        try:
            return parse_local(str(value), tz_name)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    # ------------------------------------------------------------------
    # 目录管理（管理端）
    # ------------------------------------------------------------------
    def add_institution(self, payload: dict) -> dict:
        institution_id = str(payload.get("id") or "").strip()
        name = str(payload.get("name") or "").strip()
        kind = payload.get("kind")
        timezone = str(payload.get("timezone") or "").strip()
        if not institution_id or not name:
            raise ValidationError("机构 id 与 name 不能为空")
        if kind not in ("provider", "consumer", "both"):
            raise ValidationError("kind 必须是 provider / consumer / both")
        self._parse_local("2026-01-01T00:00:00", timezone)  # 校验时区
        try:
            self.store.add_institution(id=institution_id, name=name, kind=kind, timezone=timezone)
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"机构已存在：{institution_id}") from exc
        return self.store.get_institution(institution_id)

    def add_equipment(self, payload: dict) -> dict:
        equipment_id = str(payload.get("id") or "").strip()
        institution_id = str(payload.get("institution_id") or "").strip()
        name = str(payload.get("name") or "").strip()
        capabilities = payload.get("capabilities") or []
        timezone = str(payload.get("timezone") or "").strip()
        if not equipment_id or not name:
            raise ValidationError("设备 id 与 name 不能为空")
        self._require_institution(institution_id)
        if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
            raise ValidationError("capabilities 必须是字符串列表")
        self._parse_local("2026-01-01T00:00:00", timezone)
        try:
            self.store.add_equipment(
                id=equipment_id, institution_id=institution_id, name=name,
                capabilities=capabilities, timezone=timezone)
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"设备已存在：{equipment_id}") from exc
        return self.store.get_equipment(equipment_id)

    def add_workstation(self, payload: dict) -> dict:
        workstation_id = str(payload.get("id") or "").strip()
        institution_id = str(payload.get("institution_id") or "").strip()
        name = str(payload.get("name") or "").strip()
        capacity = payload.get("capacity")
        if not workstation_id or not name:
            raise ValidationError("工位 id 与 name 不能为空")
        self._require_institution(institution_id)
        if not isinstance(capacity, int) or capacity <= 0:
            raise ValidationError("capacity 必须是正整数")
        try:
            self.store.add_workstation(
                id=workstation_id, institution_id=institution_id, name=name, capacity=capacity)
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"工位已存在：{workstation_id}") from exc
        return self.store.get_workstation(workstation_id)

    def add_instructor(self, payload: dict) -> dict:
        instructor_id = str(payload.get("id") or "").strip()
        institution_id = str(payload.get("institution_id") or "").strip()
        name = str(payload.get("name") or "").strip()
        skills = payload.get("skills") or []
        timezone = str(payload.get("timezone") or "").strip()
        if not instructor_id or not name:
            raise ValidationError("指导教师 id 与 name 不能为空")
        self._require_institution(institution_id)
        if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
            raise ValidationError("skills 必须是字符串列表")
        self._parse_local("2026-01-01T00:00:00", timezone)
        try:
            self.store.add_instructor(
                id=instructor_id, institution_id=institution_id, name=name,
                skills=skills, timezone=timezone)
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"指导教师已存在：{instructor_id}") from exc
        return self.store.get_instructor(instructor_id)

    def add_instructor_busy(self, payload: dict) -> dict:
        instructor_id = str(payload.get("instructor_id") or "").strip()
        instructor = self.store.get_instructor(instructor_id)
        if instructor is None:
            raise NotFoundError(f"指导教师不存在：{instructor_id}")
        tz_name = str(payload.get("timezone") or instructor["timezone"])
        start = self._parse_local(payload.get("start_local"), tz_name)
        end = self._parse_local(payload.get("end_local"), tz_name)
        if end <= start:
            raise ValidationError("日程结束时间必须晚于开始时间")
        busy_id = self._new_id("busy")
        self.store.add_instructor_busy(
            id=busy_id, instructor_id=instructor_id,
            start_utc=iso(start), end_utc=iso(end),
            note=str(payload.get("note") or ""))
        return {"id": busy_id, "instructor_id": instructor_id,
                "start_utc": iso(start), "end_utc": iso(end)}

    def catalog(self) -> dict:
        """资源目录：只含静态信息，不含任何占用细节。"""
        return {
            "institutions": self.store.list_institutions(),
            "equipment": self.store.list_equipment(),
            "workstations": self.store.list_workstations(),
            "instructors": self.store.list_instructors(),
        }

    # ------------------------------------------------------------------
    # 两阶段预约
    # ------------------------------------------------------------------
    def _parse_requirement(self, payload: dict, actor_institution_id: str | None) -> CourseRequirement:
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        consumer = actor_institution_id or payload.get("consumer_institution_id")
        if not consumer:
            raise ValidationError("缺少 consumer_institution_id")
        self._require_institution(str(consumer))
        provider = payload.get("provider_institution_id")
        if provider:
            self._require_institution(str(provider))
        course_id = str(payload.get("course_id") or "").strip()
        if not course_id:
            raise ValidationError("缺少 course_id")
        duration = payload.get("duration_minutes")
        if not isinstance(duration, int) or duration <= 0:
            raise ValidationError("duration_minutes 必须是正整数")
        seats = payload.get("workstation_seats", 0)
        if not isinstance(seats, int) or seats < 0:
            raise ValidationError("workstation_seats 必须是非负整数")
        capabilities = payload.get("required_capabilities") or []
        skills = payload.get("instructor_skills") or []
        if not all(isinstance(c, str) for c in capabilities):
            raise ValidationError("required_capabilities 必须是字符串列表")
        if not all(isinstance(s, str) for s in skills):
            raise ValidationError("instructor_skills 必须是字符串列表")
        timezone = payload.get("timezone")
        if not timezone:
            raise ValidationError("缺少 timezone")
        equipment_id = payload.get("equipment_id")
        workstation_id = payload.get("workstation_id")
        instructor_id = payload.get("instructor_id")
        if equipment_id:
            self._require_equipment(str(equipment_id))
        if workstation_id and self.store.get_workstation(str(workstation_id)) is None:
            raise NotFoundError(f"工位不存在：{workstation_id}")
        if instructor_id and self.store.get_instructor(str(instructor_id)) is None:
            raise NotFoundError(f"指导教师不存在：{instructor_id}")
        return CourseRequirement(
            course_id=course_id,
            consumer_institution_id=str(consumer),
            required_capabilities=tuple(capabilities),
            workstation_seats=seats,
            instructor_skills=tuple(skills),
            duration_minutes=duration,
            timezone=str(timezone),
            provider_institution_id=str(provider) if provider else None,
            equipment_id=str(equipment_id) if equipment_id else None,
            workstation_id=str(workstation_id) if workstation_id else None,
            instructor_id=str(instructor_id) if instructor_id else None,
        )

    def create_application(self, payload: dict, actor_institution_id: str | None,
                           now: datetime | None = None) -> dict:
        """第一阶段：在同一事务内暂锁全部资源，返回待确认的暂占预约。"""
        now = now or now_utc()
        req = self._parse_requirement(payload, actor_institution_id)
        start = self._parse_local(payload.get("start_local"), req.timezone)
        end = start + timedelta(minutes=req.duration_minutes)
        with self.store.transaction():
            selection, failures = planner.select_resources(self.store, req, start, end)
            if selection is None:
                alternatives = planner.suggest_alternatives(
                    self.store, req, start, end,
                    current_selection=self._pinned_selection(req))
                raise ConflictError(failures, alternatives)
            equipment = self.store.get_equipment(selection.equipment_id)
            booking_id = self._new_id("bk")
            requirement_doc = {
                "required_capabilities": list(req.required_capabilities),
                "workstation_seats": req.workstation_seats,
                "instructor_skills": list(req.instructor_skills),
                "duration_minutes": req.duration_minutes,
                "timezone": req.timezone,
            }
            self.store.create_booking({
                "id": booking_id,
                "course_id": req.course_id,
                "consumer_institution_id": req.consumer_institution_id,
                "provider_institution_id": equipment["institution_id"],
                "state": HELD,
                "start_utc": iso(start),
                "end_utc": iso(end),
                "requirement": json.dumps(requirement_doc, ensure_ascii=False),
                "hold_expires_at": iso(now + self.hold_ttl),
                "created_at": iso(now),
                "updated_at": iso(now),
            })
            self._insert_occupancy(booking_id, selection, start, end, HELD)
            booking = self.store.get_booking(booking_id)
        return self._booking_view(booking)

    def confirm_booking(self, booking_id: str, actor_institution_id: str | None,
                        now: datetime | None = None) -> dict:
        """第二阶段：在暂锁有效期内确认，占用由暂占转为确认。"""
        now = now or now_utc()
        error = None
        with self.store.transaction():
            booking = self._require_booking(booking_id)
            self._require_related(booking, actor_institution_id)
            if (actor_institution_id is not None
                    and actor_institution_id != booking["consumer_institution_id"]):
                raise ForbiddenError("仅使用院校可以确认预约")
            booking = self._expire_if_needed(booking, now)
            if booking["state"] != HELD:
                # 延迟到事务提交后再抛出，确保过期状态落盘
                error = StateError(f"当前状态为 {booking['state']}，无法确认")
            else:
                self.store.update_booking(
                    booking_id, iso(now), state=CONFIRMED, hold_expires_at=None)
                self.store.set_occupancy_state_for_booking(booking_id, CONFIRMED, from_states=(HELD,))
                booking = self.store.get_booking(booking_id)
        if error is not None:
            raise error
        return self._booking_view(booking)

    def cancel_booking(self, booking_id: str, actor_institution_id: str | None,
                       now: datetime | None = None) -> dict:
        """取消：同一事务内释放全部关联占用。"""
        now = now or now_utc()
        error = None
        with self.store.transaction():
            booking = self._require_booking(booking_id)
            self._require_related(booking, actor_institution_id)
            booking = self._expire_if_needed(booking, now)
            if booking["state"] not in (HELD, CONFIRMED):
                error = StateError(f"当前状态为 {booking['state']}，无法取消")
            else:
                self.store.update_booking(
                    booking_id, iso(now), state=CANCELLED,
                    release_reason="cancelled", hold_expires_at=None)
                self.store.set_occupancy_state_for_booking(booking_id, RELEASED)
                booking = self.store.get_booking(booking_id)
        if error is not None:
            raise error
        return self._booking_view(booking)

    def reschedule_booking(self, booking_id: str, actor_institution_id: str | None,
                           start_local: str, timezone: str | None = None,
                           now: datetime | None = None) -> dict:
        """改期：同一事务内释放旧占用并暂锁新占用；冲突时原占用保持不变。"""
        now = now or now_utc()
        error = None
        with self.store.transaction():
            booking = self._require_booking(booking_id)
            self._require_related(booking, actor_institution_id)
            booking = self._expire_if_needed(booking, now)
            if booking["state"] not in (HELD, CONFIRMED):
                error = StateError(f"当前状态为 {booking['state']}，无法改期")
            else:
                req = self._requirement_from_booking(booking)
                tz_name = timezone or req.timezone
                start = self._parse_local(start_local, tz_name)
                end = start + timedelta(minutes=req.duration_minutes)
                req = CourseRequirement(**{**req.__dict__, "timezone": tz_name})

                current = self._current_selection(booking_id)
                if current is not None and self._selection_available(current, req, start, end, booking_id):
                    selection = current
                else:
                    selection, failures = planner.select_resources(
                        self.store, req, start, end, exclude_booking_id=booking_id)
                    if selection is None:
                        alternatives = planner.suggest_alternatives(
                            self.store, req, start, end, exclude_booking_id=booking_id,
                            current_selection=current.as_dict() if current else None)
                        raise ConflictError(failures, alternatives)

                occupancy_state = HELD if booking["state"] == HELD else CONFIRMED
                self.store.set_occupancy_state_for_booking(booking_id, RELEASED)
                self._insert_occupancy(booking_id, selection, start, end, occupancy_state)
                equipment = self.store.get_equipment(selection.equipment_id)
                self.store.update_booking(
                    booking_id, iso(now), start_utc=iso(start), end_utc=iso(end),
                    provider_institution_id=equipment["institution_id"])
                booking = self.store.get_booking(booking_id)
        if error is not None:
            raise error
        return self._booking_view(booking)

    def start_use(self, booking_id: str, actor_institution_id: str | None,
                  now: datetime | None = None) -> dict:
        now = now or now_utc()
        error = None
        with self.store.transaction():
            booking = self._require_booking(booking_id)
            self._require_related(booking, actor_institution_id)
            booking = self._expire_if_needed(booking, now)
            if booking["state"] != CONFIRMED:
                error = StateError(f"当前状态为 {booking['state']}，无法开始使用")
            else:
                self.store.update_booking(booking_id, iso(now), state=IN_USE)
                booking = self.store.get_booking(booking_id)
        if error is not None:
            raise error
        return self._booking_view(booking)

    def complete_booking(self, booking_id: str, actor_institution_id: str | None,
                         now: datetime | None = None) -> dict:
        """完成使用：同一事务内释放全部占用。"""
        now = now or now_utc()
        with self.store.transaction():
            booking = self._require_booking(booking_id)
            self._require_related(booking, actor_institution_id)
            if booking["state"] not in (CONFIRMED, IN_USE):
                raise StateError(f"当前状态为 {booking['state']}，无法完成")
            self.store.update_booking(
                booking_id, iso(now), state=RELEASED, release_reason="completed")
            self.store.set_occupancy_state_for_booking(booking_id, RELEASED)
            booking = self.store.get_booking(booking_id)
        return self._booking_view(booking)

    # ------------------------------------------------------------------
    # 设备停用与维护窗口：级联原子释放
    # ------------------------------------------------------------------
    def deactivate_equipment(self, equipment_id: str, actor_institution_id: str | None,
                             now: datetime | None = None) -> dict:
        """停用设备：同一事务内整体释放其全部活跃预约的关联占用。"""
        now = now or now_utc()
        with self.store.transaction():
            equipment = self._require_equipment(equipment_id)
            self._require_owner_or_admin(equipment, actor_institution_id)
            self.store.set_equipment_status(equipment_id, "inactive")
            affected = self._release_bookings_on_equipment(
                equipment, "equipment_deactivated", now)
            equipment = self.store.get_equipment(equipment_id)
        return {"equipment": equipment, "affected": affected}

    def activate_equipment(self, equipment_id: str, actor_institution_id: str | None) -> dict:
        with self.store.transaction():
            equipment = self._require_equipment(equipment_id)
            self._require_owner_or_admin(equipment, actor_institution_id)
            self.store.set_equipment_status(equipment_id, "active")
            equipment = self.store.get_equipment(equipment_id)
        return {"equipment": equipment}

    def create_maintenance_window(self, equipment_id: str, start_local: str, end_local: str,
                                  timezone: str, reason: str,
                                  actor_institution_id: str | None,
                                  now: datetime | None = None) -> dict:
        """登记维护窗口：与窗口重叠的活跃预约被整体级联释放。"""
        now = now or now_utc()
        equipment = self._require_equipment(equipment_id)
        self._require_owner_or_admin(equipment, actor_institution_id)
        start = self._parse_local(start_local, timezone)
        end = self._parse_local(end_local, timezone)
        if end <= start:
            raise ValidationError("维护窗口结束时间必须晚于开始时间")
        with self.store.transaction():
            window_id = self._new_id("mw")
            self.store.add_maintenance_window(
                id=window_id, equipment_id=equipment_id,
                start_utc=iso(start), end_utc=iso(end), reason=reason or "")
            affected = self._release_bookings_on_equipment(
                equipment, "maintenance_overlap", now, window=(start, end))
            window = self.store.get_maintenance_window(window_id)
        return {"window": window, "affected": affected}

    def extend_maintenance_window(self, window_id: str, new_end_local: str, timezone: str,
                                  actor_institution_id: str | None,
                                  now: datetime | None = None) -> dict:
        """延长维护窗口：新重叠到的活跃预约在同一事务内整体释放。"""
        now = now or now_utc()
        window = self.store.get_maintenance_window(window_id)
        if window is None:
            raise NotFoundError(f"维护窗口不存在：{window_id}")
        equipment = self._require_equipment(window["equipment_id"])
        self._require_owner_or_admin(equipment, actor_institution_id)
        new_end = self._parse_local(new_end_local, timezone)
        if new_end <= parse_utc(window["end_utc"]):
            raise ValidationError("新的结束时间必须晚于原结束时间")
        with self.store.transaction():
            self.store.update_maintenance_window_end(window_id, iso(new_end))
            affected = self._release_bookings_on_equipment(
                equipment, "maintenance_extended", now,
                window=(parse_utc(window["start_utc"]), new_end))
            window = self.store.get_maintenance_window(window_id)
        return {"window": window, "affected": affected}

    def _release_bookings_on_equipment(self, equipment: dict, reason: str, now: datetime,
                                       window: tuple[datetime, datetime] | None = None) -> list[dict]:
        """整体释放设备关联预约：设备、工位、指导教师占用一并释放。"""
        if window is None:
            bookings = self.store.list_active_bookings_using_equipment(equipment["id"])
        else:
            bookings = self.store.list_active_bookings_on_equipment_overlapping(
                equipment["id"], iso(window[0]), iso(window[1]))
        affected = []
        for booking in bookings:
            current = self._current_selection(booking["id"])
            self.store.update_booking(
                booking["id"], iso(now), state=RELEASED,
                release_reason=reason, hold_expires_at=None)
            self.store.set_occupancy_state_for_booking(booking["id"], RELEASED)
            req = self._requirement_from_booking(booking)
            alternatives = planner.suggest_alternatives(
                self.store, req, parse_utc(booking["start_utc"]), parse_utc(booking["end_utc"]),
                exclude_booking_id=booking["id"],
                current_selection=current.as_dict() if current else None)
            affected.append({
                "booking": self._booking_view(self.store.get_booking(booking["id"])),
                "alternatives": alternatives,
            })
        return affected

    # ------------------------------------------------------------------
    # 过期暂占：巡检与恢复
    # ------------------------------------------------------------------
    def sweep_expired(self, now: datetime | None = None) -> list[str]:
        """释放所有已过期的暂占；幂等，可重复执行。"""
        now = now or now_utc()
        with self.store.transaction():
            expired = self.store.list_expired_holds(iso(now))
            for booking in expired:
                self.store.update_booking(
                    booking["id"], iso(now), state=EXPIRED,
                    release_reason="hold_expired", hold_expires_at=None)
                self.store.set_occupancy_state_for_booking(booking["id"], RELEASED)
        return [booking["id"] for booking in expired]

    def recover(self, now: datetime | None = None) -> list[str]:
        """服务恢复入口：启动时继续释放宕机期间过期的暂占。"""
        return self.sweep_expired(now)

    def start_sweeper(self, interval_seconds: float = 30.0) -> None:
        if self._sweeper_thread is not None:
            return
        self._sweeper_stop.clear()

        def loop() -> None:
            while not self._sweeper_stop.wait(interval_seconds):
                try:
                    self.sweep_expired()
                except Exception:
                    pass  # 巡检失败留待下个周期重试

        self._sweeper_thread = threading.Thread(target=loop, name="hold-sweeper", daemon=True)
        self._sweeper_thread.start()

    def stop_sweeper(self) -> None:
        self._sweeper_stop.set()
        if self._sweeper_thread is not None:
            self._sweeper_thread.join(timeout=5)
            self._sweeper_thread = None

    # ------------------------------------------------------------------
    # 查询与机构隔离
    # ------------------------------------------------------------------
    def get_booking_view(self, booking_id: str, actor_institution_id: str | None) -> dict:
        booking = self._require_booking(booking_id)
        self._require_related(booking, actor_institution_id)
        return self._booking_view(booking)

    def list_bookings(self, actor_institution_id: str) -> list[dict]:
        self._require_institution(actor_institution_id)
        rows = self.store.list_bookings_for_institution(actor_institution_id)
        return [self._booking_view(row) for row in rows]

    def occupancy_plan(self, actor_institution_id: str, start_local: str,
                       end_local: str, timezone: str) -> dict:
        """整体占用计划：相关预约展示细节，其余仅以匿名忙块呈现。"""
        institution = self._require_institution(actor_institution_id)
        start = self._parse_local(start_local, timezone)
        end = self._parse_local(end_local, timezone)
        if end <= start:
            raise ValidationError("结束时间必须晚于开始时间")

        resource_keys: set[tuple[str, str]] = set()
        for equipment in self.store.list_equipment(provider_id=actor_institution_id):
            resource_keys.add((RESOURCE_EQUIPMENT, equipment["id"]))
        for workstation in self.store.list_workstations(institution_id=actor_institution_id):
            resource_keys.add((RESOURCE_WORKSTATION, workstation["id"]))
        for instructor in self.store.list_instructors(institution_id=actor_institution_id):
            resource_keys.add((RESOURCE_INSTRUCTOR, instructor["id"]))
        my_bookings = self.store.list_bookings_for_institution(actor_institution_id)
        my_booking_ids = [b["id"] for b in my_bookings]
        for occupancy in self.store.list_active_occupancy_for_bookings(my_booking_ids):
            resource_keys.add((occupancy["resource_type"], occupancy["resource_id"]))

        resources = []
        for resource_type, resource_id in sorted(resource_keys):
            blocks = []
            for occupancy in self.store.list_active_occupancy_for_resource(
                    resource_type, resource_id, iso(start), iso(end)):
                booking = self.store.get_booking(occupancy["booking_id"])
                related = actor_institution_id in (
                    booking["consumer_institution_id"], booking["provider_institution_id"])
                block = {
                    "kind": "occupancy",
                    "start_utc": occupancy["start_utc"],
                    "end_utc": occupancy["end_utc"],
                    "units": occupancy["units"],
                    "state": occupancy["state"],
                    "related": related,
                    "booking": self._booking_summary(booking) if related else None,
                }
                blocks.append(block)
            if resource_type == RESOURCE_EQUIPMENT:
                for window in self.store.list_maintenance_overlapping(
                        resource_id, iso(start), iso(end)):
                    blocks.append({
                        "kind": "maintenance",
                        "start_utc": window["start_utc"],
                        "end_utc": window["end_utc"],
                        "reason": window["reason"],
                    })
            if resource_type == RESOURCE_INSTRUCTOR:
                instructor = self.store.get_instructor(resource_id)
                if instructor and instructor["institution_id"] == actor_institution_id:
                    for busy in self.store.list_instructor_busy_overlapping(
                            resource_id, iso(start), iso(end)):
                        blocks.append({
                            "kind": "instructor_busy",
                            "start_utc": busy["start_utc"],
                            "end_utc": busy["end_utc"],
                            "note": busy["note"],
                        })
            resources.append({
                "resource_type": resource_type,
                "resource_id": resource_id,
                "name": self._resource_name(resource_type, resource_id),
                "blocks": sorted(blocks, key=lambda b: b["start_utc"]),
            })
        return {
            "institution_id": institution["id"],
            "window": {"start_utc": iso(start), "end_utc": iso(end), "timezone": timezone},
            "resources": resources,
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _expire_if_needed(self, booking: dict, now: datetime) -> dict:
        if (booking["state"] == HELD and booking["hold_expires_at"]
                and parse_utc(booking["hold_expires_at"]) <= now):
            self.store.update_booking(
                booking["id"], iso(now), state=EXPIRED,
                release_reason="hold_expired", hold_expires_at=None)
            self.store.set_occupancy_state_for_booking(booking["id"], RELEASED)
            booking = self.store.get_booking(booking["id"])
        return booking

    def _insert_occupancy(self, booking_id: str, selection: Selection,
                          start: datetime, end: datetime, state: str) -> None:
        self.store.add_occupancy(
            id=self._new_id("occ"), booking_id=booking_id,
            resource_type=RESOURCE_EQUIPMENT, resource_id=selection.equipment_id,
            units=1, start_utc=iso(start), end_utc=iso(end), state=state)
        if selection.workstation_id:
            self.store.add_occupancy(
                id=self._new_id("occ"), booking_id=booking_id,
                resource_type=RESOURCE_WORKSTATION, resource_id=selection.workstation_id,
                units=selection.workstation_seats, start_utc=iso(start), end_utc=iso(end),
                state=state)
        if selection.instructor_id:
            self.store.add_occupancy(
                id=self._new_id("occ"), booking_id=booking_id,
                resource_type=RESOURCE_INSTRUCTOR, resource_id=selection.instructor_id,
                units=1, start_utc=iso(start), end_utc=iso(end), state=state)

    def _current_selection(self, booking_id: str) -> Selection | None:
        active = [o for o in self.store.list_occupancy_for_booking(booking_id)
                  if o["state"] != RELEASED]
        equipment = next((o for o in active if o["resource_type"] == RESOURCE_EQUIPMENT), None)
        if equipment is None:
            return None
        workstation = next((o for o in active if o["resource_type"] == RESOURCE_WORKSTATION), None)
        instructor = next((o for o in active if o["resource_type"] == RESOURCE_INSTRUCTOR), None)
        return Selection(
            equipment_id=equipment["resource_id"],
            workstation_id=workstation["resource_id"] if workstation else None,
            workstation_seats=workstation["units"] if workstation else 0,
            instructor_id=instructor["resource_id"] if instructor else None,
        )

    def _selection_available(self, selection: Selection, req: CourseRequirement,
                             start: datetime, end: datetime, booking_id: str) -> bool:
        if planner.check_equipment(self.store, selection.equipment_id, req, start, end, booking_id):
            return False
        if selection.workstation_id and planner.check_workstation(
                self.store, selection.workstation_id, selection.workstation_seats,
                req, start, end, booking_id):
            return False
        if selection.instructor_id and planner.check_instructor(
                self.store, selection.instructor_id, req, start, end, booking_id):
            return False
        return True

    @staticmethod
    def _pinned_selection(req: CourseRequirement) -> dict:
        return {
            "equipment": req.equipment_id,
            "workstation": req.workstation_id,
            "workstation_seats": req.workstation_seats,
            "instructor": req.instructor_id,
        }

    def _requirement_from_booking(self, booking: dict) -> CourseRequirement:
        doc = json.loads(booking["requirement"])
        return CourseRequirement(
            course_id=booking["course_id"],
            consumer_institution_id=booking["consumer_institution_id"],
            required_capabilities=tuple(doc["required_capabilities"]),
            workstation_seats=doc["workstation_seats"],
            instructor_skills=tuple(doc["instructor_skills"]),
            duration_minutes=doc["duration_minutes"],
            timezone=doc["timezone"],
            provider_institution_id=booking["provider_institution_id"],
        )

    def _resource_name(self, resource_type: str, resource_id: str) -> str:
        if resource_type == RESOURCE_EQUIPMENT:
            row = self.store.get_equipment(resource_id)
        elif resource_type == RESOURCE_WORKSTATION:
            row = self.store.get_workstation(resource_id)
        else:
            row = self.store.get_instructor(resource_id)
        return row["name"] if row else resource_id

    @staticmethod
    def _booking_summary(booking: dict) -> dict:
        return {
            "id": booking["id"],
            "course_id": booking["course_id"],
            "state": booking["state"],
            "consumer_institution_id": booking["consumer_institution_id"],
            "provider_institution_id": booking["provider_institution_id"],
        }

    def _booking_view(self, booking: dict) -> dict:
        req = json.loads(booking["requirement"])
        occupancy = self.store.list_occupancy_for_booking(booking["id"])
        active = [o for o in occupancy if o["state"] != RELEASED]

        def pick(resource_type: str) -> dict | None:
            return next((o for o in active if o["resource_type"] == resource_type), None)

        equipment, workstation, instructor = (
            pick(RESOURCE_EQUIPMENT), pick(RESOURCE_WORKSTATION), pick(RESOURCE_INSTRUCTOR))
        return {
            "id": booking["id"],
            "course_id": booking["course_id"],
            "state": booking["state"],
            "consumer_institution_id": booking["consumer_institution_id"],
            "provider_institution_id": booking["provider_institution_id"],
            "window": {
                "start_utc": booking["start_utc"],
                "end_utc": booking["end_utc"],
                "start_local": render_local(booking["start_utc"], req["timezone"]),
                "end_local": render_local(booking["end_utc"], req["timezone"]),
                "timezone": req["timezone"],
            },
            "resources": {
                "equipment": equipment["resource_id"] if equipment else None,
                "workstation": workstation["resource_id"] if workstation else None,
                "workstation_seats": workstation["units"] if workstation else 0,
                "instructor": instructor["resource_id"] if instructor else None,
            },
            "requirement": req,
            "occupancy": occupancy,
            "hold_expires_at": booking["hold_expires_at"],
            "release_reason": booking["release_reason"],
            "version": booking["version"],
            "updated_at": booking["updated_at"],
        }
