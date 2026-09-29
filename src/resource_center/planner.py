"""可用性检查、组合资源选择与可解释替代方案生成。

冲突说明只描述资源与时段，不泄露其他机构预约的任何细节；
替代方案分为「同时段替代资源」与「时间平移」两类，均附带中文解释。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from .models import (
    RESOURCE_EQUIPMENT,
    RESOURCE_INSTRUCTOR,
    RESOURCE_WORKSTATION,
    CourseRequirement,
    Selection,
)
from .timeutil import iso, render_local

SHIFT_STEP = timedelta(minutes=30)
MAX_SHIFT_STEPS = 6 * 24 * 2  # 向后扫描 6 天


def _conflict(code: str, resource_type: str, resource_id: str, message: str) -> dict:
    return {
        "code": code,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "message": message,
    }


def _dedup(conflicts: list[dict]) -> list[dict]:
    seen, result = set(), []
    for item in conflicts:
        key = (item["code"], item["resource_type"], item["resource_id"], item["message"])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def check_equipment(store, equipment_id: str, req: CourseRequirement,
                    start: datetime, end: datetime,
                    exclude_booking_id: str | None = None) -> list[dict]:
    """检查设备在指定时段是否可用：能力、停用状态、维护窗口与占用。"""
    equipment = store.get_equipment(equipment_id)
    if equipment is None:
        return [_conflict("equipment_not_found", RESOURCE_EQUIPMENT, equipment_id,
                          f"设备 {equipment_id} 不存在")]
    missing = sorted(set(req.required_capabilities) - set(equipment["capabilities"]))
    if missing:
        return [_conflict("equipment_capability", RESOURCE_EQUIPMENT, equipment_id,
                          f"设备 {equipment_id} 缺少能力：{'、'.join(missing)}")]
    if equipment["status"] != "active":
        return [_conflict("equipment_inactive", RESOURCE_EQUIPMENT, equipment_id,
                          f"设备 {equipment_id} 已停用")]
    conflicts = []
    for window in store.list_maintenance_overlapping(equipment_id, iso(start), iso(end)):
        conflicts.append(_conflict(
            "equipment_maintenance", RESOURCE_EQUIPMENT, equipment_id,
            f"设备 {equipment_id} 处于维护窗口（至 "
            f"{render_local(window['end_utc'], req.timezone)}，{req.timezone}）"))
    if store.list_active_occupancy_for_resource(
            RESOURCE_EQUIPMENT, equipment_id, iso(start), iso(end), exclude_booking_id):
        conflicts.append(_conflict("equipment_occupied", RESOURCE_EQUIPMENT, equipment_id,
                                   f"设备 {equipment_id} 在该时段已被占用"))
    return conflicts


def check_workstation(store, workstation_id: str, seats: int, req: CourseRequirement,
                      start: datetime, end: datetime,
                      exclude_booking_id: str | None = None) -> list[dict]:
    """检查工位容量：同时段占用席位之和不得超过工位容量。"""
    workstation = store.get_workstation(workstation_id)
    if workstation is None:
        return [_conflict("workstation_not_found", RESOURCE_WORKSTATION, workstation_id,
                          f"工位 {workstation_id} 不存在")]
    used = store.sum_workstation_units(
        workstation_id, iso(start), iso(end), exclude_booking_id)
    remaining = workstation["capacity"] - used
    if remaining < seats:
        return [_conflict(
            "workstation_capacity", RESOURCE_WORKSTATION, workstation_id,
            f"工位 {workstation_id} 该时段剩余容量 {remaining} 个，不足 {seats} 个")]
    return []


def check_instructor(store, instructor_id: str, req: CourseRequirement,
                     start: datetime, end: datetime,
                     exclude_booking_id: str | None = None) -> list[dict]:
    """检查指导教师：技能、预约占用与既有日程。"""
    instructor = store.get_instructor(instructor_id)
    if instructor is None:
        return [_conflict("instructor_not_found", RESOURCE_INSTRUCTOR, instructor_id,
                          f"指导教师 {instructor_id} 不存在")]
    missing = sorted(set(req.instructor_skills) - set(instructor["skills"]))
    if missing:
        return [_conflict("instructor_skill", RESOURCE_INSTRUCTOR, instructor_id,
                          f"指导教师 {instructor_id} 缺少技能：{'、'.join(missing)}")]
    if store.list_active_occupancy_for_resource(
            RESOURCE_INSTRUCTOR, instructor_id, iso(start), iso(end), exclude_booking_id):
        return [_conflict("instructor_occupied", RESOURCE_INSTRUCTOR, instructor_id,
                          f"指导教师 {instructor_id} 在该时段已被占用")]
    if store.list_instructor_busy_overlapping(instructor_id, iso(start), iso(end)):
        return [_conflict("instructor_busy", RESOURCE_INSTRUCTOR, instructor_id,
                          f"指导教师 {instructor_id} 在该时段已有日程安排")]
    return []


def select_resources(store, req: CourseRequirement, start: datetime, end: datetime,
                     exclude_booking_id: str | None = None) -> tuple[Selection | None, list[dict]]:
    """为课程需求选择一整套可用资源；失败时返回全部冲突原因。"""
    failures: list[dict] = []

    # 1. 设备：满足能力要求且在时段内空闲
    if req.equipment_id:
        equipment_ids = [req.equipment_id]
    else:
        equipment_ids = sorted(
            e["id"]
            for e in store.list_equipment(provider_id=req.provider_institution_id, status="active")
            if set(req.required_capabilities) <= set(e["capabilities"])
        )
        if not equipment_ids:
            failures.append(_conflict("no_equipment_candidate", RESOURCE_EQUIPMENT, "-",
                                      "没有满足能力要求的可用设备"))
    equipment = None
    for equipment_id in equipment_ids:
        conflicts = check_equipment(store, equipment_id, req, start, end, exclude_booking_id)
        if conflicts:
            failures.extend(conflicts)
            continue
        equipment = store.get_equipment(equipment_id)
        break
    if equipment is None:
        return None, _dedup(failures)

    # 2. 工位：与设备同属一个机构，容量按时段核算
    workstation_id = None
    if req.workstation_seats > 0:
        if req.workstation_id:
            workstation_ids = [req.workstation_id]
        else:
            workstation_ids = sorted(
                w["id"] for w in store.list_workstations(institution_id=equipment["institution_id"]))
            if not workstation_ids:
                failures.append(_conflict(
                    "no_workstation_candidate", RESOURCE_WORKSTATION, "-",
                    f"机构 {equipment['institution_id']} 没有可用工位"))
        for candidate in workstation_ids:
            workstation = store.get_workstation(candidate)
            if workstation is not None and workstation["institution_id"] != equipment["institution_id"]:
                failures.append(_conflict(
                    "workstation_site_mismatch", RESOURCE_WORKSTATION, candidate,
                    f"工位 {candidate} 与设备 {equipment['id']} 不属于同一机构"))
                continue
            conflicts = check_workstation(
                store, candidate, req.workstation_seats, req, start, end, exclude_booking_id)
            if conflicts:
                failures.extend(conflicts)
                continue
            workstation_id = candidate
            break
        if workstation_id is None:
            return None, _dedup(failures)

    # 3. 指导教师：技能匹配，优先设备所属机构（可远程指导）
    instructor_id = None
    if req.instructor_skills:
        if req.instructor_id:
            instructor_ids = [req.instructor_id]
        else:
            candidates = [
                i for i in store.list_instructors()
                if set(req.instructor_skills) <= set(i["skills"])
            ]
            candidates.sort(key=lambda i: (i["institution_id"] != equipment["institution_id"], i["id"]))
            instructor_ids = [i["id"] for i in candidates]
            if not instructor_ids:
                failures.append(_conflict("no_instructor_candidate", RESOURCE_INSTRUCTOR, "-",
                                          "没有满足技能要求的可用指导教师"))
        for candidate in instructor_ids:
            conflicts = check_instructor(store, candidate, req, start, end, exclude_booking_id)
            if conflicts:
                failures.extend(conflicts)
                continue
            instructor_id = candidate
            break
        if instructor_id is None:
            return None, _dedup(failures)

    return Selection(equipment["id"], workstation_id, req.workstation_seats, instructor_id), []


def suggest_alternatives(store, req: CourseRequirement, start: datetime, end: datetime,
                         exclude_booking_id: str | None = None,
                         current_selection: dict | None = None,
                         limit: int = 3) -> list[dict]:
    """生成可解释替代方案：同时段替代资源优先，其次时间平移。"""
    alternatives: list[dict] = []
    relaxed = replace(req, equipment_id=None, workstation_id=None, instructor_id=None)

    # 同时段替代资源：放开具体资源指定后整体重选
    selection, _ = select_resources(store, relaxed, start, end, exclude_booking_id)
    if selection is not None and selection.as_dict() != (current_selection or {}):
        changes = []
        for key, label in (("equipment", "设备"), ("workstation", "工位"), ("instructor", "指导教师")):
            old = (current_selection or {}).get(key)
            new = selection.as_dict()[key]
            if old != new and (old or new):
                changes.append(f"{label} {old or '无'} → {new or '无'}")
        detail = "；".join(changes) if changes else "整体重选成功"
        alternatives.append({
            "kind": "resource_substitute",
            "window": {
                "start_local": render_local(start, req.timezone),
                "end_local": render_local(end, req.timezone),
                "timezone": req.timezone,
            },
            "resources": selection.as_dict(),
            "explanation": f"同一时段可改用替代资源（{detail}）",
        })

    # 时间平移：以 30 分钟为步长向后扫描整体可用的时段
    shifts = 0
    for step_index in range(1, MAX_SHIFT_STEPS + 1):
        if shifts >= limit:
            break
        shifted_start = start + SHIFT_STEP * step_index
        shifted_end = end + SHIFT_STEP * step_index
        selection, _ = select_resources(store, relaxed, shifted_start, shifted_end,
                                        exclude_booking_id)
        if selection is None:
            continue
        shifts += 1
        alternatives.append({
            "kind": "time_shift",
            "window": {
                "start_local": render_local(shifted_start, req.timezone),
                "end_local": render_local(shifted_end, req.timezone),
                "timezone": req.timezone,
            },
            "resources": selection.as_dict(),
            "explanation": (
                f"整体资源在 {render_local(shifted_start, req.timezone)}"
                f"（{req.timezone}）起可用"),
        })
    return alternatives
