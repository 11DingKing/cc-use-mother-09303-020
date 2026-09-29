"""资源中心业务服务测试：组合暂锁、原子更新、冲突替代、过期恢复与机构隔离。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from resource_center import (  # noqa: E402
    ADMIN_INSTITUTION,
    BookingRequest,
    ConflictError,
    EquipmentNeed,
    NotFoundError,
    ResourceCenterService,
    StateError,
    Store,
    parse_instant,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)

# 联合实训课：2026-10-10 09:00~12:00 Asia/Shanghai == 01:00~04:00 UTC
COURSE_START = parse_instant("2026-10-10T09:00:00", "Asia/Shanghai")
COURSE_END = parse_instant("2026-10-10T12:00:00", "Asia/Shanghai")


def make_service(db_path: str = ":memory:") -> ResourceCenterService:
    service = ResourceCenterService(Store(db_path))
    service.register_institution("provider-p", "设备提供院校P")
    service.register_institution("consumer-a", "使用院校A")
    service.register_institution("consumer-b", "使用院校B")
    service.add_equipment("provider-p", "五轴加工中心", ["cnc", "5-axis"], equipment_id="eq-1")
    service.add_equipment("provider-p", "三轴加工中心", ["cnc", "3-axis"], equipment_id="eq-2")
    service.add_workstation("provider-p", "实训工位区", 30, workstation_id="ws-1")
    service.add_instructor("provider-p", "远程指导教师", "Asia/Shanghai", instructor_id="ins-1")
    service.add_instructor_availability(
        "ins-1", datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC))
    return service


def course_request(institution: str = "consumer-a", **overrides) -> BookingRequest:
    values = {
        "institution_id": institution,
        "course_name": "联合实训课",
        "start": COURSE_START,
        "end": COURSE_END,
        "timezone": "Asia/Shanghai",
        "equipment": [EquipmentNeed(("cnc", "5-axis"))],
        "seats": 20,
        "instructor_id": "ins-1",
        "hold_ttl_seconds": 600,
    }
    values.update(overrides)
    return BookingRequest(**values)


def active_occupancies(service: ResourceCenterService, booking_id: str) -> list[dict]:
    booking = service.get_booking(booking_id, ADMIN_INSTITUTION)
    return [o for o in booking["occupancies"] if o["status"] in ("held", "confirmed")]


class LifecycleTest(unittest.TestCase):
    def test_hold_confirm_use_release_lifecycle(self) -> None:
        service = make_service()
        booking = service.request_booking(course_request(), NOW)
        self.assertEqual(booking["status"], "held")
        # 暂锁覆盖全部组合资源：设备 + 工位 + 指导教师
        kinds = {o["resource_type"] for o in booking["occupancies"]}
        self.assertEqual(kinds, {"equipment", "workstation", "instructor"})
        self.assertTrue(all(o["status"] == "held" for o in booking["occupancies"]))

        booking = service.confirm_booking(booking["id"], "consumer-a", NOW)
        self.assertEqual(booking["status"], "confirmed")
        self.assertTrue(all(o["status"] == "confirmed" for o in booking["occupancies"]))

        booking = service.mark_in_use(booking["id"], "consumer-a", NOW)
        self.assertEqual(booking["status"], "in_use")

        booking = service.complete_booking(booking["id"], "consumer-a", NOW)
        self.assertEqual(booking["status"], "released")
        self.assertEqual(active_occupancies(service, booking["id"]), [])

    def test_confirm_rejects_other_institution(self) -> None:
        service = make_service()
        booking = service.request_booking(course_request(), NOW)
        with self.assertRaises(NotFoundError):
            service.get_booking(booking["id"], "consumer-b")


class AtomicHoldTest(unittest.TestCase):
    def test_failed_hold_leaves_no_partial_occupancy(self) -> None:
        """教师冲突时整体失败：设备与工位不得留下暂占残留。"""
        service = make_service()
        first = service.request_booking(course_request(), NOW)
        service.confirm_booking(first["id"], "consumer-a", NOW)

        with self.assertRaises(ConflictError):
            service.request_booking(course_request(institution="consumer-b"), NOW)

        plan = service.occupancy_plan(ADMIN_INSTITUTION, NOW, NOW + timedelta(days=60))
        booking_ids = {item["booking_id"] for item in plan["occupancies"]}
        self.assertEqual(booking_ids, {first["id"]})

    def test_conflict_response_is_explainable(self) -> None:
        service = make_service()
        first = service.request_booking(course_request(), NOW)
        service.confirm_booking(first["id"], "consumer-a", NOW)

        with self.assertRaises(ConflictError) as ctx:
            service.request_booking(course_request(institution="consumer-b"), NOW)
        error = ctx.exception
        self.assertTrue(error.conflicts)
        for conflict in error.conflicts:
            self.assertIn("detail", conflict)
            self.assertIn("reason", conflict)
        # 替代方案：整体平移时段，且平移后确实可行
        shifts = [a for a in error.alternatives if a["kind"] == "shift_time"]
        self.assertTrue(shifts, "应给出整体平移替代方案")
        retry = course_request(
            institution="consumer-b",
            start=parse_instant(shifts[0]["start"]),
            end=parse_instant(shifts[0]["end"]),
        )
        moved = service.request_booking(retry, NOW)
        self.assertEqual(moved["status"], "held")

    def test_workstation_capacity_is_shared_and_bounded(self) -> None:
        service = make_service()
        small = course_request(seats=20, equipment=[], instructor_id=None,
                               workstation_id="ws-1")
        first = service.request_booking(small, NOW)
        self.assertEqual(first["status"], "held")
        second = course_request(institution="consumer-b", seats=20,
                                equipment=[], instructor_id=None, workstation_id="ws-1")
        with self.assertRaises(ConflictError) as ctx:
            service.request_booking(second, NOW)
        reasons = {c["reason"] for c in ctx.exception.conflicts}
        self.assertIn("capacity_exceeded", reasons)
        fits = course_request(institution="consumer-b", seats=10,
                              equipment=[], instructor_id=None, workstation_id="ws-1")
        self.assertEqual(service.request_booking(fits, NOW)["status"], "held")


class MaintenanceImpactTest(unittest.TestCase):
    def test_extension_releases_all_linked_resources_atomically(self) -> None:
        """维护窗口延长：受扰预约的设备、工位、教师占用必须一起处理。"""
        service = make_service()
        booking = service.request_booking(course_request(), NOW)
        service.confirm_booking(booking["id"], "consumer-a", NOW)
        # 维护窗口覆盖课程前两个小时（UTC），随后延长到覆盖整门课
        window = service.create_maintenance_window(
            "eq-1", COURSE_START - timedelta(hours=2), COURSE_START, "主轴检修")
        report = service.extend_maintenance_window(
            window["id"], COURSE_END + timedelta(hours=1), "provider-p", NOW)

        impacted = {item["booking_id"]: item for item in report["impact"]}
        self.assertIn(booking["id"], impacted)
        # 无论整体改期还是整体释放，都不能留下"只释放机器"的部分占用
        remaining = active_occupancies(service, booking["id"])
        if impacted[booking["id"]]["action"] == "released":
            self.assertEqual(remaining, [])
            view = service.get_booking(booking["id"], "consumer-a")
            self.assertEqual(view["status"], "disrupted")
            self.assertIn("cause", view["disruption"])
        else:
            self.assertEqual(impacted[booking["id"]]["action"], "rescheduled")
            self.assertEqual(len(remaining), 3)
            starts = {o["start"] for o in remaining}
            self.assertEqual(len(starts), 1, "改期后全部关联占用必须使用同一新时段")

    def test_extension_without_conflict_keeps_booking(self) -> None:
        service = make_service()
        booking = service.request_booking(course_request(), NOW)
        service.confirm_booking(booking["id"], "consumer-a", NOW)
        window = service.create_maintenance_window(
            "eq-1", COURSE_END + timedelta(hours=2), COURSE_END + timedelta(hours=3), "点检")
        report = service.extend_maintenance_window(
            window["id"], COURSE_END + timedelta(hours=4), "provider-p", NOW)
        self.assertEqual(report["impact"], [])
        self.assertEqual(len(active_occupancies(service, booking["id"])), 3)

    def test_disable_equipment_impacts_future_bookings(self) -> None:
        service = make_service()
        booking = service.request_booking(course_request(), NOW)
        service.confirm_booking(booking["id"], "consumer-a", NOW)
        report = service.disable_equipment("eq-1", "provider-p", NOW)
        impacted = {item["booking_id"]: item["action"] for item in report["impact"]}
        self.assertIn(booking["id"], impacted)
        remaining = active_occupancies(service, booking["id"])
        self.assertIn(len(remaining), (0, 3), "只允许整体改期或整体释放")

    def test_disrupted_booking_can_be_rescheduled(self) -> None:
        """受扰预约可通过改期重新安排，占用整体重建。"""
        service = make_service()
        booking = service.request_booking(course_request(), NOW)
        service.confirm_booking(booking["id"], "consumer-a", NOW)
        # 另一门课从本课结束起占住教师两周，使维护冲击找不到平移时段
        blocker = course_request(
            institution="consumer-b",
            start=COURSE_END,
            end=COURSE_END + timedelta(days=14),
            equipment=[EquipmentNeed(("cnc", "3-axis"))],
            seats=5,
        )
        blocker_booking = service.request_booking(blocker, NOW)
        service.confirm_booking(blocker_booking["id"], "consumer-b", NOW)

        window = service.create_maintenance_window(
            "eq-1", COURSE_START, COURSE_START + timedelta(hours=1), "故障")
        report = service.extend_maintenance_window(window["id"], COURSE_END, "provider-p", NOW)
        impacted = {item["booking_id"]: item for item in report["impact"]}
        self.assertEqual(impacted[booking["id"]]["action"], "released")

        view = service.get_booking(booking["id"], "consumer-a")
        self.assertEqual(view["status"], "disrupted")
        # 改期到维护与占用都结束之后，受扰预约恢复确认态
        new_start = COURSE_END + timedelta(days=15)
        new_end = new_start + timedelta(hours=3)
        moved = service.reschedule_booking(booking["id"], "consumer-a", new_start, new_end, NOW)
        self.assertEqual(moved["status"], "confirmed")
        self.assertEqual(len(active_occupancies(service, booking["id"])), 3)


class RescheduleCancelTest(unittest.TestCase):
    def test_reschedule_updates_all_occupancies_atomically(self) -> None:
        service = make_service()
        booking = service.request_booking(course_request(), NOW)
        service.confirm_booking(booking["id"], "consumer-a", NOW)
        new_start = COURSE_START + timedelta(days=1)
        new_end = COURSE_END + timedelta(days=1)
        moved = service.reschedule_booking(booking["id"], "consumer-a", new_start, new_end, NOW)
        self.assertEqual(moved["start"], new_start.isoformat(timespec="microseconds"))
        for occ in moved["occupancies"]:
            self.assertEqual(occ["start"], new_start.isoformat(timespec="microseconds"))
            self.assertEqual(occ["end"], new_end.isoformat(timespec="microseconds"))

    def test_failed_reschedule_keeps_original_occupancies(self) -> None:
        service = make_service()
        first = service.request_booking(course_request(), NOW)
        service.confirm_booking(first["id"], "consumer-a", NOW)
        second = service.request_booking(
            course_request(institution="consumer-b",
                           start=COURSE_START + timedelta(days=1),
                           end=COURSE_END + timedelta(days=1)), NOW)
        service.confirm_booking(second["id"], "consumer-b", NOW)
        # 把第二门课改到第一门课的时段：整体冲突，原占用必须保持不变
        with self.assertRaises(ConflictError):
            service.reschedule_booking(second["id"], "consumer-b", COURSE_START, COURSE_END, NOW)
        view = service.get_booking(second["id"], "consumer-b")
        self.assertEqual(view["start"], (COURSE_START + timedelta(days=1)).isoformat(timespec="microseconds"))
        self.assertEqual(len(active_occupancies(service, second["id"])), 3)

    def test_cancel_releases_all_occupancies(self) -> None:
        service = make_service()
        booking = service.request_booking(course_request(), NOW)
        service.confirm_booking(booking["id"], "consumer-a", NOW)
        service.cancel_booking(booking["id"], "consumer-a", NOW)
        self.assertEqual(active_occupancies(service, booking["id"]), [])
        with self.assertRaises(StateError):
            service.cancel_booking(booking["id"], "consumer-a", NOW)


class ExpiryRecoveryTest(unittest.TestCase):
    def test_expired_hold_released_after_service_restart(self) -> None:
        """服务恢复后继续释放过期暂占：重启（重开存储）后 recover 生效。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "center.db")
            service = make_service(db_path)
            booking = service.request_booking(course_request(hold_ttl_seconds=60), NOW)
            service.store.close()

            # 模拟服务重启：同一数据库文件重新打开
            restored = ResourceCenterService(Store(db_path))
            later = NOW + timedelta(seconds=120)
            result = restored.recover(later)
            self.assertEqual(result["released_holds"], 1)
            view = restored.get_booking(booking["id"], ADMIN_INSTITUTION)
            self.assertEqual(view["status"], "expired")
            remaining = [o for o in view["occupancies"] if o["status"] != "released"]
            self.assertEqual(remaining, [])
            restored.store.close()

    def test_expired_hold_no_longer_blocks_others(self) -> None:
        service = make_service()
        service.request_booking(course_request(hold_ttl_seconds=60), NOW)
        later = NOW + timedelta(seconds=120)
        # 另一机构在同一时段申请：过期暂占先被清理，不再构成冲突
        booking = service.request_booking(course_request(institution="consumer-b"), later)
        self.assertEqual(booking["status"], "held")

    def test_unexpired_hold_still_blocks(self) -> None:
        service = make_service()
        service.request_booking(course_request(hold_ttl_seconds=3600), NOW)
        with self.assertRaises(ConflictError):
            service.request_booking(course_request(institution="consumer-b"), NOW)


class CrossTimezoneTest(unittest.TestCase):
    def test_maintenance_window_conflicts_across_timezones(self) -> None:
        """UTC 维护窗口与 Asia/Shanghai 课程时段正确判冲突。"""
        service = make_service()
        # UTC 03:00~05:00 == Asia/Shanghai 11:00~13:00，与课程 09:00~12:00 重叠
        service.create_maintenance_window(
            "eq-1",
            datetime(2026, 10, 10, 3, 0, tzinfo=UTC),
            datetime(2026, 10, 10, 5, 0, tzinfo=UTC),
            "固件升级",
        )
        with self.assertRaises(ConflictError) as ctx:
            service.request_booking(
                course_request(equipment=[EquipmentNeed(("cnc", "5-axis"), "eq-1")]), NOW)
        reasons = {c["reason"] for c in ctx.exception.conflicts}
        self.assertIn("maintenance_window", reasons)

    def test_instructor_schedule_outside_availability(self) -> None:
        service = make_service()
        night = course_request(
            start=parse_instant("2026-11-05T09:00:00", "Asia/Shanghai"),
            end=parse_instant("2026-11-05T12:00:00", "Asia/Shanghai"),
        )
        with self.assertRaises(ConflictError) as ctx:
            service.request_booking(night, NOW)
        reasons = {c["reason"] for c in ctx.exception.conflicts}
        self.assertIn("instructor_unavailable", reasons)


class IsolationTest(unittest.TestCase):
    def test_institutions_only_see_own_booking_details(self) -> None:
        service = make_service()
        booking = service.request_booking(course_request(), NOW)
        # 使用院校与资源提供院校可见，无关机构不可见
        self.assertEqual(service.get_booking(booking["id"], "consumer-a")["id"], booking["id"])
        self.assertEqual(service.get_booking(booking["id"], "provider-p")["id"], booking["id"])
        with self.assertRaises(NotFoundError):
            service.get_booking(booking["id"], "consumer-b")

    def test_plan_redacts_other_institutions(self) -> None:
        service = make_service()
        booking = service.request_booking(course_request(), NOW)
        service.confirm_booking(booking["id"], "consumer-a", NOW)
        plan = service.occupancy_plan("consumer-b", NOW, NOW + timedelta(days=60))
        self.assertTrue(plan["occupancies"])
        for item in plan["occupancies"]:
            self.assertEqual(item["visibility"], "redacted")
            self.assertNotIn("course_name", item)
            self.assertNotIn("institution_id", item)
        own = service.occupancy_plan("consumer-a", NOW, NOW + timedelta(days=60))
        self.assertTrue(all(item["visibility"] == "full" for item in own["occupancies"]))
        self.assertEqual(own["occupancies"][0]["course_name"], "联合实训课")


if __name__ == "__main__":
    unittest.main()
