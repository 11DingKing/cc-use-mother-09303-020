"""领域模型与状态机定义。

预约状态机（对应领域契约：申请 → 暂占 → 确认 → 使用 → 释放）：

    held(暂占) → confirmed(确认) → in_use(使用) → released(释放)
      │             │
      ▼             ▼
    expired(过期)  cancelled(取消) / released(释放，级联释放)

held 在暂锁超时后进入 expired；任何活跃态都可能因设备停用或
维护窗口延长被整体级联释放为 released。
"""
from __future__ import annotations

from dataclasses import dataclass

HELD = "held"
CONFIRMED = "confirmed"
IN_USE = "in_use"
RELEASED = "released"
CANCELLED = "cancelled"
EXPIRED = "expired"

BOOKING_STATES = (HELD, CONFIRMED, IN_USE, RELEASED, CANCELLED, EXPIRED)
ACTIVE_BOOKING_STATES = (HELD, CONFIRMED, IN_USE)

RESOURCE_EQUIPMENT = "equipment"
RESOURCE_WORKSTATION = "workstation"
RESOURCE_INSTRUCTOR = "instructor"
RESOURCE_TYPES = (RESOURCE_EQUIPMENT, RESOURCE_WORKSTATION, RESOURCE_INSTRUCTOR)


@dataclass(frozen=True)
class CourseRequirement:
    """课程需求：设备能力、工位容量、指导教师技能与跨时区时段。"""

    course_id: str
    consumer_institution_id: str
    required_capabilities: tuple[str, ...] = ()
    workstation_seats: int = 0
    instructor_skills: tuple[str, ...] = ()
    duration_minutes: int = 60
    timezone: str = "UTC"
    provider_institution_id: str | None = None
    equipment_id: str | None = None
    workstation_id: str | None = None
    instructor_id: str | None = None


@dataclass(frozen=True)
class Selection:
    """一次完整的组合资源选择结果。"""

    equipment_id: str
    workstation_id: str | None
    workstation_seats: int
    instructor_id: str | None

    def as_dict(self) -> dict:
        return {
            "equipment": self.equipment_id,
            "workstation": self.workstation_id,
            "workstation_seats": self.workstation_seats,
            "instructor": self.instructor_id,
        }
