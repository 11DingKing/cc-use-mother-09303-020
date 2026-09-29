"""端到端演示：维护窗口临时延长后，关联占用被原子释放并可重新安排。

场景还原：联合实训课确认后，设备维护窗口临时延长。旧系统只释放机器，
工位与远程指导教师仍被占用；本演示展示资源中心如何在同一事务内释放
全部关联占用、返回可解释替代方案，并在服务恢复后继续释放过期暂占。

用法：python3 tools/demo_scenario.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from resource_center.seed import seed_demo
from resource_center.service import NotFoundError, ResourceCenterService
from resource_center.store import Store
from resource_center.timeutil import now_utc


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "resource_center.db"
    store = Store(db_path)
    service = ResourceCenterService(store)
    seed_demo(store)

    # 1. 使用院校申请联合实训课：暂锁设备 + 工位 + 远程指导教师
    application = {
        "course_id": "joint-welding-101",
        "consumer_institution_id": "inst-beta",
        "required_capabilities": ["焊接"],
        "workstation_seats": 6,
        "instructor_skills": ["焊接"],
        "duration_minutes": 120,
        "start_local": "2026-10-10T09:00:00",
        "timezone": "Asia/Dubai",
    }
    booking = service.create_application(application, "inst-beta")
    show("1. 申请：全部资源已暂锁（注意指导教师为远程的马里娅姆，陈工该时段有日程）", {
        "id": booking["id"], "state": booking["state"],
        "window": booking["window"], "resources": booking["resources"],
        "hold_expires_at": booking["hold_expires_at"],
    })

    # 2. 使用院校确认预约
    booking = service.confirm_booking(booking["id"], "inst-beta")
    show("2. 确认：暂占转为确认", {"id": booking["id"], "state": booking["state"]})

    # 3. 提供院校临时延长维护窗口，覆盖该课程时段
    result = service.extend_maintenance_window(
        "mw-welder-1", "2026-10-10T16:00:00", "Asia/Shanghai", "inst-alpha")
    affected = result["affected"][0]
    show("3. 维护窗口延长：设备、工位、指导教师在同一事务内整体释放", {
        "window": result["window"],
        "released_booking_state": affected["booking"]["state"],
        "release_reason": affected["booking"]["release_reason"],
        "occupancy": [(o["resource_type"], o["resource_id"], o["state"])
                      for o in affected["booking"]["occupancy"]],
        "alternatives": affected["alternatives"],
    })

    # 4. 使用院校按替代方案同时段改用替代设备，重新申请并确认
    rebooked = service.create_application(application, "inst-beta")
    rebooked = service.confirm_booking(rebooked["id"], "inst-beta")
    show("4. 重新安排：同一时段改用替代设备 eq-welder-2 并确认", {
        "id": rebooked["id"], "state": rebooked["state"],
        "resources": rebooked["resources"],
    })

    # 5. 过期暂占：服务恢复后继续释放
    now = now_utc()
    stale = service.create_application({**application, "course_id": "joint-welding-102",
                                        "start_local": "2026-10-11T09:00:00"},
                                       "inst-beta", now=now)
    print(f"\n=== 5. 暂占 {stale['id']} 未确认，模拟服务宕机重启 ===")
    store.close()
    store2 = Store(db_path)
    service2 = ResourceCenterService(store2)
    released = service2.recover(now=now + service.hold_ttl + timedelta(minutes=1))
    show("服务恢复后继续释放过期暂占", {"released": released})

    # 6. 机构隔离：无关机构看不到他人预约细节
    try:
        service2.get_booking_view(rebooked["id"], "inst-gamma")
        isolation = "隔离失败"
    except NotFoundError:
        isolation = "inst-gamma 查询他人预约得到 404，细节未泄露"
    beta_plan = service2.occupancy_plan(
        "inst-beta", "2026-10-10T00:00:00", "2026-10-11T00:00:00", "Asia/Dubai")
    gamma_plan = service2.occupancy_plan(
        "inst-gamma", "2026-10-10T00:00:00", "2026-10-11T00:00:00", "Europe/Berlin")
    show("6. 机构隔离", {
        "unrelated_access": isolation,
        "beta_plan_related_blocks": [
            {"resource": r["resource_id"], "related": b["related"]}
            for r in beta_plan["resources"] for b in r["blocks"] if b["kind"] == "occupancy"
        ],
        "gamma_plan_resources": len(gamma_plan["resources"]),
    })

    store2.close()
    tmp.cleanup()
    print("\n演示完成。")


if __name__ == "__main__":
    main()
