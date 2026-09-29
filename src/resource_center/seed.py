"""演示数据：跨时区的设备提供方、使用方与远程指导教师。"""
from __future__ import annotations

DEMO_INSTITUTIONS = [
    {"id": "inst-alpha", "name": "华东联合实训基地", "kind": "provider", "timezone": "Asia/Shanghai"},
    {"id": "inst-beta", "name": "海湾合作学院", "kind": "consumer", "timezone": "Asia/Dubai"},
    {"id": "inst-gamma", "name": "北欧交换学院", "kind": "consumer", "timezone": "Europe/Berlin"},
]

DEMO_EQUIPMENT = [
    {"id": "eq-welder-1", "institution_id": "inst-alpha", "name": "焊接机器人一号",
     "capabilities": ["焊接", "示教"], "timezone": "Asia/Shanghai"},
    {"id": "eq-welder-2", "institution_id": "inst-alpha", "name": "焊接机器人二号",
     "capabilities": ["焊接"], "timezone": "Asia/Shanghai"},
    {"id": "eq-cnc-1", "institution_id": "inst-alpha", "name": "数控加工中心",
     "capabilities": ["数控", "测量"], "timezone": "Asia/Shanghai"},
]

DEMO_WORKSTATIONS = [
    {"id": "ws-a1", "institution_id": "inst-alpha", "name": "实训工位A", "capacity": 20},
    {"id": "ws-a2", "institution_id": "inst-alpha", "name": "实训工位B", "capacity": 8},
]

DEMO_INSTRUCTORS = [
    {"id": "ins-chen", "institution_id": "inst-alpha", "name": "陈工",
     "skills": ["焊接", "安全"], "timezone": "Asia/Shanghai"},
    {"id": "ins-mariam", "institution_id": "inst-beta", "name": "马里亚姆",
     "skills": ["焊接"], "timezone": "Asia/Dubai"},
]

# 陈工在 2026-10-10 13:00-15:00（Asia/Shanghai）已有日程，对应 UTC 05:00-07:00
DEMO_INSTRUCTOR_BUSY = [
    {"id": "busy-chen-1", "instructor_id": "ins-chen",
     "start_utc": "2026-10-10T05:00:00+00:00", "end_utc": "2026-10-10T07:00:00+00:00",
     "note": "校内安全例会"},
]

# 焊接机器人一号的例行保养窗口，演示中会被临时延长
DEMO_MAINTENANCE = [
    {"id": "mw-welder-1", "equipment_id": "eq-welder-1",
     "start_utc": "2026-10-10T00:00:00+00:00", "end_utc": "2026-10-10T02:00:00+00:00",
     "reason": "例行保养"},
]


def seed_demo(store) -> dict:
    """写入演示数据；已存在时直接跳过，保证幂等。"""
    if store.get_institution(DEMO_INSTITUTIONS[0]["id"]) is not None:
        return {"seeded": False}
    for institution in DEMO_INSTITUTIONS:
        store.add_institution(**institution)
    for equipment in DEMO_EQUIPMENT:
        store.add_equipment(**equipment)
    for workstation in DEMO_WORKSTATIONS:
        store.add_workstation(**workstation)
    for instructor in DEMO_INSTRUCTORS:
        store.add_instructor(**instructor)
    for busy in DEMO_INSTRUCTOR_BUSY:
        store.add_instructor_busy(**busy)
    for window in DEMO_MAINTENANCE:
        store.add_maintenance_window(**window)
    return {"seeded": True}
