"""资源中心领域模型：状态机、跨时区时间工具与需求数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

UTC = timezone.utc

# 资源中心管理员身份：可查看与操作全部机构的预约。
ADMIN_INSTITUTION = "resource-center"


class BookingStatus(str, Enum):
    """预约状态机：申请→暂占→确认→使用→释放；取消、受扰、过期为终态。"""

    REQUESTED = "requested"
    HELD = "held"
    CONFIRMED = "confirmed"
    IN_USE = "in_use"
    RELEASED = "released"
    CANCELLED = "cancelled"
    DISRUPTED = "disrupted"
    EXPIRED = "expired"


ACTIVE_BOOKING_STATES = (
    BookingStatus.HELD.value,
    BookingStatus.CONFIRMED.value,
    BookingStatus.IN_USE.value,
)

TERMINAL_BOOKING_STATES = (
    BookingStatus.RELEASED.value,
    BookingStatus.CANCELLED.value,
    BookingStatus.EXPIRED.value,
)


class OccupancyStatus(str, Enum):
    """单资源占用状态：暂占、确认、已释放。"""

    HELD = "held"
    CONFIRMED = "confirmed"
    RELEASED = "released"


ACTIVE_OCCUPANCY_STATES = (
    OccupancyStatus.HELD.value,
    OccupancyStatus.CONFIRMED.value,
)


class ResourceType(str, Enum):
    """参与整体占用计划的资源类型。"""

    EQUIPMENT = "equipment"
    WORKSTATION = "workstation"
    INSTRUCTOR = "instructor"


def parse_instant(value: str, default_timezone: str | None = None) -> datetime:
    """解析 ISO 8601 时刻；无时区偏移时按 default_timezone 解释，统一返回 UTC。"""
    instant = datetime.fromisoformat(value.strip())
    if instant.tzinfo is None:
        if not default_timezone:
            raise ValueError(f"时刻 {value!r} 缺少时区且未提供默认时区")
        instant = instant.replace(tzinfo=ZoneInfo(default_timezone))
    return instant.astimezone(UTC)


def format_instant(value: datetime) -> str:
    """统一存储格式：UTC 固定宽度，字符串比较等价于时间比较。"""
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def parse_stored(value: str) -> datetime:
    """读取存储格式的时刻。"""
    return datetime.fromisoformat(value)


@dataclass
class EquipmentNeed:
    """单台设备需求：按能力自动匹配，或显式指定设备。"""

    capabilities: tuple[str, ...] = ()
    equipment_id: str | None = None


@dataclass
class BookingRequest:
    """课程预约需求：组合设备能力、工位容量、指导教师与跨时区时段。"""

    institution_id: str
    course_name: str
    start: datetime
    end: datetime
    timezone: str
    equipment: list[EquipmentNeed] = field(default_factory=list)
    seats: int = 0
    instructor_id: str | None = None
    workstation_id: str | None = None
    hold_ttl_seconds: int = 900

    @property
    def duration(self) -> timedelta:
        return self.end - self.start
