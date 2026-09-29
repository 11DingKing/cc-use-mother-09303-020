"""资源中心服务层回归测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from resource_center.service import (  # noqa: E402
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ResourceCenterService,
    StateError,
)
from resource_center.store import Store  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)

# 课程时段：2026-10-10 09:00-11:00（Asia/Dubai）= UTC 05:00-07:00
WINDOW = {"start_local": "2026-10-10T09:00:00", "timezone": "Asia/Dubai"}


def build_fixture(store: Store) -> None:
    store.add_institution(id="inst-alpha", name="华东联合实训基地", kind="provider",
                          timezone="Asia/Shanghai")
    store.add_institution(id="inst-beta", name="海湾合作学院", kind="consumer",
                          timezone="Asia/Dubai")
    store.add_institution(id="inst-gamma", name="北欧交换学院", kind="consumer",
                          timezone="Europe/Berlin")
    store.add_equipment(id="eq-1", institution_id="inst-alpha", name="焊接机器人一号",
                        capabilities=["焊接", "示教"], timezone="Asia/Shanghai")
    store.add_equipment(id="eq-2", institution_id="inst-alpha", name="焊接机器人二号",
                        capabilities=["焊接"], timezone="Asia/Shanghai")
    store.add_workstation(id="ws-1", institution_id="inst-alpha", name="实训工位A", capacity=4)
    store.add_instructor(id="ins-1", institution_id="inst-alpha", name="陈工",
                         skills=["焊接"], timezone="Asia/Shanghai")
    store.add_instructor(id="ins-2", institution_id="inst-beta", name="马里亚姆",
                         skills=["焊接"], timezone="Asia/Dubai")


def application(**overrides) -> dict:
    payload = {
        "course_id": "joint-welding-101",
        "consumer_institution_id": "inst-beta",
        "required_capabilities": ["焊接"],
        "workstation_seats": 2,
        "instructor_skills": ["焊接"],
        "duration_minutes": 120,
        **WINDOW,
    }
    payload.update(overrides)
    return payload


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        self.store = Store(self.db_path)
        build_fixture(self.store)
        self.service = ResourceCenterService(self.store)
        self.addCleanup(lambda: self.store.close())
        self.addCleanup(self._tmp.cleanup)

    def confirmed_booking(self, **overrides) -> dict:
        booking = self.service.create_application(application(**overrides), "inst-beta", now=T0)
        return self.service.confirm_booking(booking["id"], "inst-beta", now=T0)


class TwoPhaseBookingTest(ServiceTestCase):
    def test_hold_then_confirm_locks_all_resources(self) -> None:
        booking = self.service.create_application(application(), "inst-beta", now=T0)
        self.assertEqual(booking["state"], "held")
        self.assertIsNotNone(booking["hold_expires_at"])
        held = {(o["resource_type"], o["state"]) for o in booking["occupancy"]}
        self.assertEqual(held, {("equipment", "held"), ("workstation", "held"),
                                ("instructor", "held")})
        self.assertEqual(booking["resources"]["equipment"], "eq-1")
        self.assertEqual(booking["resources"]["workstation"], "ws-1")
        self.assertEqual(booking["resources"]["instructor"], "ins-1")

        booking = self.service.confirm_booking(booking["id"], "inst-beta", now=T0)
        self.assertEqual(booking["state"], "confirmed")
        self.assertIsNone(booking["hold_expires_at"])
        self.assertTrue(all(o["state"] == "confirmed" for o in booking["occupancy"]))

    def test_cross_timezone_window_is_stored_in_utc(self) -> None:
        booking = self.service.create_application(application(), "inst-beta", now=T0)
        self.assertEqual(booking["window"]["start_utc"], "2026-10-10T05:00:00+00:00")
        self.assertEqual(booking["window"]["end_utc"], "2026-10-10T07:00:00+00:00")
        self.assertEqual(booking["window"]["start_local"], "2026-10-10T09:00:00+04:00")

    def test_provider_cannot_confirm(self) -> None:
        booking = self.service.create_application(application(), "inst-beta", now=T0)
        with self.assertRaises(ForbiddenError):
            self.service.confirm_booking(booking["id"], "inst-alpha", now=T0)

    def test_cancel_releases_everything(self) -> None:
        booking = self.service.create_application(application(), "inst-beta", now=T0)
        booking = self.service.cancel_booking(booking["id"], "inst-beta", now=T0)
        self.assertEqual(booking["state"], "cancelled")
        self.assertTrue(all(o["state"] == "released" for o in booking["occupancy"]))


class ConflictTest(ServiceTestCase):
    def test_conflict_returns_explainable_alternatives(self) -> None:
        self.confirmed_booking(required_capabilities=["焊接", "示教"])
        with self.assertRaises(ConflictError) as ctx:
            self.service.create_application(
                application(required_capabilities=["焊接", "示教"]), "inst-beta", now=T0)
        conflicts = ctx.exception.conflicts
        self.assertEqual(conflicts[0]["code"], "equipment_occupied")
        # 冲突说明不泄露他人预约的任何标识
        self.assertNotIn("bk-", json.dumps(conflicts, ensure_ascii=False))
        kinds = {alt["kind"] for alt in ctx.exception.alternatives}
        self.assertIn("time_shift", kinds)
        for alt in ctx.exception.alternatives:
            self.assertTrue(alt["explanation"])

    def test_pinned_equipment_conflict_suggests_substitute(self) -> None:
        self.confirmed_booking(equipment_id="eq-1")
        with self.assertRaises(ConflictError) as ctx:
            self.service.create_application(
                application(equipment_id="eq-1"), "inst-beta", now=T0)
        substitutes = [a for a in ctx.exception.alternatives
                       if a["kind"] == "resource_substitute"]
        self.assertTrue(substitutes)
        self.assertEqual(substitutes[0]["resources"]["equipment"], "eq-2")
        self.assertIn("eq-1", substitutes[0]["explanation"])

    def test_workstation_capacity_is_enforced(self) -> None:
        self.confirmed_booking(workstation_seats=3, instructor_skills=[])
        with self.assertRaises(ConflictError) as ctx:
            self.service.create_application(
                application(workstation_seats=2, instructor_skills=[]), "inst-beta", now=T0)
        codes = {c["code"] for c in ctx.exception.conflicts}
        self.assertIn("workstation_capacity", codes)

    def test_instructor_busy_block_and_occupancy(self) -> None:
        self.store.add_instructor_busy(
            id="busy-1", instructor_id="ins-1",
            start_utc="2026-10-10T05:00:00+00:00", end_utc="2026-10-10T07:00:00+00:00",
            note="校内例会")
        # 陈工有日程，自动改选远程指导教师马里亚姆
        booking = self.service.create_application(application(), "inst-beta", now=T0)
        self.assertEqual(booking["resources"]["instructor"], "ins-2")
        self.service.confirm_booking(booking["id"], "inst-beta", now=T0)
        # 第二位教师也被占用后，再次申请只能改期
        with self.assertRaises(ConflictError) as ctx:
            self.service.create_application(application(), "inst-beta", now=T0)
        codes = {c["code"] for c in ctx.exception.conflicts}
        self.assertIn("instructor_busy", codes)
        self.assertIn("instructor_occupied", codes)


class ExpiryRecoveryTest(ServiceTestCase):
    def test_sweep_releases_expired_holds(self) -> None:
        booking = self.service.create_application(application(), "inst-beta", now=T0)
        released = self.service.sweep_expired(now=T0 + timedelta(minutes=16))
        self.assertEqual(released, [booking["id"]])
        view = self.service.get_booking_view(booking["id"], "inst-beta")
        self.assertEqual(view["state"], "expired")
        self.assertTrue(all(o["state"] == "released" for o in view["occupancy"]))
        # 资源已可再次预约
        again = self.service.create_application(application(), "inst-beta",
                                                now=T0 + timedelta(minutes=16))
        self.assertEqual(again["state"], "held")

    def test_recovery_after_restart_releases_expired_holds(self) -> None:
        booking = self.service.create_application(application(), "inst-beta", now=T0)
        self.store.close()
        # 模拟服务恢复：同一数据库文件上新起服务实例
        self.store = Store(self.db_path)
        self.service = ResourceCenterService(self.store)
        released = self.service.recover(now=T0 + timedelta(minutes=16))
        self.assertEqual(released, [booking["id"]])
        view = self.service.get_booking_view(booking["id"], "inst-beta")
        self.assertEqual(view["state"], "expired")

    def test_confirm_after_expiry_fails(self) -> None:
        booking = self.service.create_application(application(), "inst-beta", now=T0)
        with self.assertRaises(StateError):
            self.service.confirm_booking(booking["id"], "inst-beta",
                                         now=T0 + timedelta(minutes=20))
        view = self.service.get_booking_view(booking["id"], "inst-beta")
        self.assertEqual(view["state"], "expired")


class CascadeReleaseTest(ServiceTestCase):
    def test_deactivate_releases_all_resources_atomically(self) -> None:
        booking = self.confirmed_booking()
        result = self.service.deactivate_equipment("eq-1", "inst-alpha", now=T0)
        affected = result["affected"][0]["booking"]
        self.assertEqual(affected["id"], booking["id"])
        self.assertEqual(affected["state"], "released")
        self.assertEqual(affected["release_reason"], "equipment_deactivated")
        # 关键回归点：不只设备，工位与指导教师占用也一并释放
        released = {(o["resource_type"], o["state"]) for o in affected["occupancy"]}
        self.assertEqual(released, {("equipment", "released"), ("workstation", "released"),
                                    ("instructor", "released")})
        # 替代方案指向同能力的 eq-2，后续课程可立即重新安排
        alternatives = result["affected"][0]["alternatives"]
        self.assertEqual(alternatives[0]["kind"], "resource_substitute")
        self.assertEqual(alternatives[0]["resources"]["equipment"], "eq-2")
        rebooked = self.service.create_application(application(), "inst-beta", now=T0)
        self.assertEqual(rebooked["resources"]["equipment"], "eq-2")

    def test_maintenance_extension_releases_all_resources(self) -> None:
        booking = self.confirmed_booking()
        self.service.create_maintenance_window(
            "eq-1", "2026-10-10T00:00:00", "2026-10-10T02:00:00", "UTC",
            "例行保养", "inst-alpha", now=T0)
        window = self.store.list_maintenance_overlapping(
            "eq-1", "2026-10-10T00:00:00+00:00", "2026-10-10T02:00:00+00:00")[0]
        result = self.service.extend_maintenance_window(
            window["id"], "2026-10-10T08:00:00", "UTC", "inst-alpha", now=T0)
        affected = result["affected"][0]["booking"]
        self.assertEqual(affected["id"], booking["id"])
        self.assertEqual(affected["release_reason"], "maintenance_extended")
        self.assertTrue(all(o["state"] == "released" for o in affected["occupancy"]))
        kinds = {a["kind"] for a in result["affected"][0]["alternatives"]}
        self.assertIn("resource_substitute", kinds)

    def test_maintenance_window_blocks_new_applications(self) -> None:
        self.service.create_maintenance_window(
            "eq-1", "2026-10-10T04:00:00", "2026-10-10T08:00:00", "UTC",
            "更换焊枪", "inst-alpha", now=T0)
        with self.assertRaises(ConflictError) as ctx:
            self.service.create_application(
                application(required_capabilities=["焊接", "示教"]), "inst-beta", now=T0)
        self.assertEqual(ctx.exception.conflicts[0]["code"], "equipment_maintenance")
        self.assertIn("维护窗口", ctx.exception.conflicts[0]["message"])

    def test_other_institution_cannot_deactivate(self) -> None:
        with self.assertRaises(ForbiddenError):
            self.service.deactivate_equipment("eq-1", "inst-beta", now=T0)


class RescheduleTest(ServiceTestCase):
    def test_reschedule_moves_window_atomically(self) -> None:
        booking = self.confirmed_booking()
        moved = self.service.reschedule_booking(
            booking["id"], "inst-beta", "2026-10-11T09:00:00", now=T0)
        self.assertEqual(moved["window"]["start_utc"], "2026-10-11T05:00:00+00:00")
        self.assertEqual(moved["resources"], booking["resources"])
        active = [o for o in moved["occupancy"] if o["state"] == "confirmed"]
        self.assertEqual(len(active), 3)
        self.assertTrue(all(o["start_utc"] == "2026-10-11T05:00:00+00:00" for o in active))

    def test_reschedule_conflict_keeps_old_occupancy(self) -> None:
        first = self.confirmed_booking(required_capabilities=["焊接", "示教"])
        second = self.service.create_application(
            application(course_id="joint-welding-102", required_capabilities=["焊接", "示教"],
                        start_local="2026-10-11T09:00:00"), "inst-beta", now=T0)
        second = self.service.confirm_booking(second["id"], "inst-beta", now=T0)
        with self.assertRaises(ConflictError):
            self.service.reschedule_booking(second["id"], "inst-beta",
                                            "2026-10-10T09:00:00", now=T0)
        # 改期失败不留下任何中间状态：原占用完整保留
        view = self.service.get_booking_view(second["id"], "inst-beta")
        self.assertEqual(view["window"]["start_utc"], "2026-10-11T05:00:00+00:00")
        self.assertTrue(all(o["state"] == "confirmed" for o in view["occupancy"]))
        self.assertEqual(view["id"], second["id"])
        self.assertNotEqual(view["id"], first["id"])


class IsolationTest(ServiceTestCase):
    def test_unrelated_institution_cannot_see_booking(self) -> None:
        booking = self.confirmed_booking()
        with self.assertRaises(NotFoundError):
            self.service.get_booking_view(booking["id"], "inst-gamma")
        self.assertEqual(self.service.list_bookings("inst-gamma"), [])

    def test_plan_scopes_details_to_related_bookings(self) -> None:
        self.confirmed_booking()
        beta_plan = self.service.occupancy_plan(
            "inst-beta", "2026-10-10T00:00:00", "2026-10-11T00:00:00", "Asia/Dubai")
        related = [b for r in beta_plan["resources"] for b in r["blocks"]
                   if b["kind"] == "occupancy"]
        self.assertTrue(related)
        self.assertTrue(all(b["related"] and b["booking"] for b in related))

        gamma_plan = self.service.occupancy_plan(
            "inst-gamma", "2026-10-10T00:00:00", "2026-10-11T00:00:00", "Europe/Berlin")
        self.assertEqual(gamma_plan["resources"], [])

        alpha_plan = self.service.occupancy_plan(
            "inst-alpha", "2026-10-10T00:00:00", "2026-10-11T00:00:00", "Asia/Shanghai")
        blocks = [b for r in alpha_plan["resources"] for b in r["blocks"]
                  if b["kind"] == "occupancy"]
        self.assertTrue(all(b["related"] for b in blocks))

    def test_plan_anonymizes_unrelated_occupancy(self) -> None:
        self.confirmed_booking()
        # 第二使用院校在同一工位占用席位后，海湾学院只能看到匿名忙块
        self.service.create_application(
            application(course_id="joint-welding-103", consumer_institution_id="inst-gamma",
                        required_capabilities=["焊接"], workstation_seats=1,
                        instructor_skills=[], start_local="2026-10-10T09:00:00"),
            "inst-gamma", now=T0)
        beta_plan = self.service.occupancy_plan(
            "inst-beta", "2026-10-10T00:00:00", "2026-10-11T00:00:00", "Asia/Dubai")
        ws_blocks = [b for r in beta_plan["resources"] if r["resource_id"] == "ws-1"
                     for b in r["blocks"] if b["kind"] == "occupancy"]
        self.assertEqual(len(ws_blocks), 2)
        anonymous = [b for b in ws_blocks if not b["related"]]
        self.assertEqual(len(anonymous), 1)
        self.assertIsNone(anonymous[0]["booking"])


if __name__ == "__main__":
    unittest.main()
