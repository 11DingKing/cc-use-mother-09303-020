"""SQLite 持久化层。

所有多步写入都通过 ``transaction()`` 在单事务中完成，保证暂锁、确认、
改期、停用、维护延长等操作对关联占用的原子更新；数据落盘使服务恢复后
可以继续释放过期暂占。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS institutions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('provider', 'consumer', 'both')),
    timezone TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equipment (
    id TEXT PRIMARY KEY,
    institution_id TEXT NOT NULL REFERENCES institutions (id),
    name TEXT NOT NULL,
    capabilities TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
    timezone TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workstations (
    id TEXT PRIMARY KEY,
    institution_id TEXT NOT NULL REFERENCES institutions (id),
    name TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity > 0)
);
CREATE TABLE IF NOT EXISTS instructors (
    id TEXT PRIMARY KEY,
    institution_id TEXT NOT NULL REFERENCES institutions (id),
    name TEXT NOT NULL,
    skills TEXT NOT NULL,
    timezone TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instructor_busy (
    id TEXT PRIMARY KEY,
    instructor_id TEXT NOT NULL REFERENCES instructors (id),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS maintenance_windows (
    id TEXT PRIMARY KEY,
    equipment_id TEXT NOT NULL REFERENCES equipment (id),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    consumer_institution_id TEXT NOT NULL REFERENCES institutions (id),
    provider_institution_id TEXT NOT NULL REFERENCES institutions (id),
    state TEXT NOT NULL CHECK (state IN ('held', 'confirmed', 'in_use', 'released', 'cancelled', 'expired')),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    requirement TEXT NOT NULL,
    hold_expires_at TEXT,
    release_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS occupancy (
    id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL REFERENCES bookings (id),
    resource_type TEXT NOT NULL CHECK (resource_type IN ('equipment', 'workstation', 'instructor')),
    resource_id TEXT NOT NULL,
    units INTEGER NOT NULL CHECK (units > 0),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('held', 'confirmed', 'released'))
);
CREATE INDEX IF NOT EXISTS idx_occupancy_resource
    ON occupancy (resource_type, resource_id, state, start_utc, end_utc);
CREATE INDEX IF NOT EXISTS idx_occupancy_booking ON occupancy (booking_id);
CREATE INDEX IF NOT EXISTS idx_bookings_state ON bookings (state, hold_expires_at);
"""

ACTIVE_OCCUPANCY_STATES = ("held", "confirmed")


class Store:
    """线程安全的 SQLite 存储；写操作经 BEGIN IMMEDIATE 串行化。"""

    def __init__(self, path: str | Path):
        self._path = str(path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        try:
            self._conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass  # 内存库不支持 WAL，忽略
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self):
        """单事务执行多步写入：全部成功才提交，任何异常整体回滚。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def _query_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def _execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)

    # --- 机构 ---
    def add_institution(self, *, id: str, name: str, kind: str, timezone: str) -> None:
        self._execute(
            "INSERT INTO institutions (id, name, kind, timezone) VALUES (?, ?, ?, ?)",
            (id, name, kind, timezone),
        )

    def get_institution(self, institution_id: str) -> dict | None:
        return self._query_one("SELECT * FROM institutions WHERE id = ?", (institution_id,))

    def list_institutions(self) -> list[dict]:
        return self._query("SELECT * FROM institutions ORDER BY id")

    # --- 设备 ---
    def add_equipment(self, *, id: str, institution_id: str, name: str,
                      capabilities: list[str], timezone: str) -> None:
        self._execute(
            "INSERT INTO equipment (id, institution_id, name, capabilities, status, timezone)"
            " VALUES (?, ?, ?, ?, 'active', ?)",
            (id, institution_id, name, json.dumps(capabilities, ensure_ascii=False), timezone),
        )

    def get_equipment(self, equipment_id: str) -> dict | None:
        row = self._query_one("SELECT * FROM equipment WHERE id = ?", (equipment_id,))
        return self._decode_equipment(row)

    def list_equipment(self, provider_id: str | None = None, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM equipment"
        clauses, params = [], []
        if provider_id is not None:
            clauses.append("institution_id = ?")
            params.append(provider_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return [self._decode_equipment(r) for r in self._query(sql + " ORDER BY id", tuple(params))]

    def set_equipment_status(self, equipment_id: str, status: str) -> None:
        self._execute("UPDATE equipment SET status = ? WHERE id = ?", (status, equipment_id))

    @staticmethod
    def _decode_equipment(row: dict | None) -> dict | None:
        if row is not None:
            row["capabilities"] = json.loads(row["capabilities"])
        return row

    # --- 工位 ---
    def add_workstation(self, *, id: str, institution_id: str, name: str, capacity: int) -> None:
        self._execute(
            "INSERT INTO workstations (id, institution_id, name, capacity) VALUES (?, ?, ?, ?)",
            (id, institution_id, name, capacity),
        )

    def get_workstation(self, workstation_id: str) -> dict | None:
        return self._query_one("SELECT * FROM workstations WHERE id = ?", (workstation_id,))

    def list_workstations(self, institution_id: str | None = None) -> list[dict]:
        if institution_id is None:
            return self._query("SELECT * FROM workstations ORDER BY id")
        return self._query(
            "SELECT * FROM workstations WHERE institution_id = ? ORDER BY id", (institution_id,))

    # --- 指导教师 ---
    def add_instructor(self, *, id: str, institution_id: str, name: str,
                       skills: list[str], timezone: str) -> None:
        self._execute(
            "INSERT INTO instructors (id, institution_id, name, skills, timezone) VALUES (?, ?, ?, ?, ?)",
            (id, institution_id, name, json.dumps(skills, ensure_ascii=False), timezone),
        )

    def get_instructor(self, instructor_id: str) -> dict | None:
        row = self._query_one("SELECT * FROM instructors WHERE id = ?", (instructor_id,))
        return self._decode_instructor(row)

    def list_instructors(self, institution_id: str | None = None) -> list[dict]:
        if institution_id is None:
            rows = self._query("SELECT * FROM instructors ORDER BY id")
        else:
            rows = self._query(
                "SELECT * FROM instructors WHERE institution_id = ? ORDER BY id", (institution_id,))
        return [self._decode_instructor(r) for r in rows]

    @staticmethod
    def _decode_instructor(row: dict | None) -> dict | None:
        if row is not None:
            row["skills"] = json.loads(row["skills"])
        return row

    def add_instructor_busy(self, *, id: str, instructor_id: str,
                            start_utc: str, end_utc: str, note: str = "") -> None:
        self._execute(
            "INSERT INTO instructor_busy (id, instructor_id, start_utc, end_utc, note)"
            " VALUES (?, ?, ?, ?, ?)",
            (id, instructor_id, start_utc, end_utc, note),
        )

    def list_instructor_busy_overlapping(self, instructor_id: str,
                                         start_utc: str, end_utc: str) -> list[dict]:
        return self._query(
            "SELECT * FROM instructor_busy"
            " WHERE instructor_id = ? AND start_utc < ? AND end_utc > ? ORDER BY start_utc",
            (instructor_id, end_utc, start_utc),
        )

    # --- 维护窗口 ---
    def add_maintenance_window(self, *, id: str, equipment_id: str,
                               start_utc: str, end_utc: str, reason: str = "") -> None:
        self._execute(
            "INSERT INTO maintenance_windows (id, equipment_id, start_utc, end_utc, reason)"
            " VALUES (?, ?, ?, ?, ?)",
            (id, equipment_id, start_utc, end_utc, reason),
        )

    def get_maintenance_window(self, window_id: str) -> dict | None:
        return self._query_one("SELECT * FROM maintenance_windows WHERE id = ?", (window_id,))

    def update_maintenance_window_end(self, window_id: str, end_utc: str) -> None:
        self._execute(
            "UPDATE maintenance_windows SET end_utc = ? WHERE id = ?", (end_utc, window_id))

    def list_maintenance_overlapping(self, equipment_id: str,
                                     start_utc: str, end_utc: str) -> list[dict]:
        return self._query(
            "SELECT * FROM maintenance_windows"
            " WHERE equipment_id = ? AND start_utc < ? AND end_utc > ? ORDER BY start_utc",
            (equipment_id, end_utc, start_utc),
        )

    # --- 预约 ---
    def create_booking(self, booking: dict) -> None:
        self._execute(
            "INSERT INTO bookings (id, course_id, consumer_institution_id, provider_institution_id,"
            " state, start_utc, end_utc, requirement, hold_expires_at, release_reason,"
            " created_at, updated_at, version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 1)",
            (
                booking["id"], booking["course_id"], booking["consumer_institution_id"],
                booking["provider_institution_id"], booking["state"], booking["start_utc"],
                booking["end_utc"], booking["requirement"], booking["hold_expires_at"],
                booking["created_at"], booking["updated_at"],
            ),
        )

    def get_booking(self, booking_id: str) -> dict | None:
        return self._query_one("SELECT * FROM bookings WHERE id = ?", (booking_id,))

    def update_booking(self, booking_id: str, updated_at: str, **fields) -> None:
        fields["updated_at"] = updated_at
        columns = ", ".join(f"{key} = ?" for key in fields)
        self._execute(
            f"UPDATE bookings SET {columns}, version = version + 1 WHERE id = ?",
            (*fields.values(), booking_id),
        )

    def list_bookings_for_institution(self, institution_id: str) -> list[dict]:
        return self._query(
            "SELECT * FROM bookings"
            " WHERE consumer_institution_id = ? OR provider_institution_id = ?"
            " ORDER BY created_at, id",
            (institution_id, institution_id),
        )

    def list_expired_holds(self, now_utc: str) -> list[dict]:
        return self._query(
            "SELECT * FROM bookings"
            " WHERE state = 'held' AND hold_expires_at IS NOT NULL AND hold_expires_at <= ?"
            " ORDER BY hold_expires_at",
            (now_utc,),
        )

    def list_active_bookings_using_equipment(self, equipment_id: str) -> list[dict]:
        return self._query(
            "SELECT b.* FROM bookings b"
            " JOIN occupancy o ON o.booking_id = b.id"
            " WHERE o.resource_type = 'equipment' AND o.resource_id = ?"
            " AND o.state IN ('held', 'confirmed')"
            " AND b.state IN ('held', 'confirmed', 'in_use') ORDER BY b.start_utc",
            (equipment_id,),
        )

    def list_active_bookings_on_equipment_overlapping(self, equipment_id: str,
                                                      start_utc: str, end_utc: str) -> list[dict]:
        return self._query(
            "SELECT b.* FROM bookings b"
            " JOIN occupancy o ON o.booking_id = b.id"
            " WHERE o.resource_type = 'equipment' AND o.resource_id = ?"
            " AND o.state IN ('held', 'confirmed')"
            " AND b.state IN ('held', 'confirmed', 'in_use')"
            " AND o.start_utc < ? AND o.end_utc > ? ORDER BY b.start_utc",
            (equipment_id, end_utc, start_utc),
        )

    # --- 占用 ---
    def add_occupancy(self, *, id: str, booking_id: str, resource_type: str, resource_id: str,
                      units: int, start_utc: str, end_utc: str, state: str) -> None:
        self._execute(
            "INSERT INTO occupancy (id, booking_id, resource_type, resource_id, units,"
            " start_utc, end_utc, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (id, booking_id, resource_type, resource_id, units, start_utc, end_utc, state),
        )

    def list_occupancy_for_booking(self, booking_id: str) -> list[dict]:
        return self._query(
            "SELECT * FROM occupancy WHERE booking_id = ? ORDER BY resource_type, id",
            (booking_id,),
        )

    def list_active_occupancy_for_resource(self, resource_type: str, resource_id: str,
                                           start_utc: str, end_utc: str,
                                           exclude_booking_id: str | None = None) -> list[dict]:
        sql = (
            "SELECT * FROM occupancy WHERE resource_type = ? AND resource_id = ?"
            " AND state IN ('held', 'confirmed') AND start_utc < ? AND end_utc > ?"
        )
        params: list = [resource_type, resource_id, end_utc, start_utc]
        if exclude_booking_id is not None:
            sql += " AND booking_id != ?"
            params.append(exclude_booking_id)
        return self._query(sql + " ORDER BY start_utc", tuple(params))

    def list_active_occupancy_for_bookings(self, booking_ids: list[str]) -> list[dict]:
        if not booking_ids:
            return []
        placeholders = ", ".join("?" for _ in booking_ids)
        return self._query(
            f"SELECT * FROM occupancy WHERE booking_id IN ({placeholders})"
            " AND state IN ('held', 'confirmed') ORDER BY resource_type, resource_id",
            tuple(booking_ids),
        )

    def set_occupancy_state_for_booking(self, booking_id: str, new_state: str,
                                        from_states: tuple[str, ...] = ACTIVE_OCCUPANCY_STATES) -> None:
        placeholders = ", ".join("?" for _ in from_states)
        self._execute(
            f"UPDATE occupancy SET state = ? WHERE booking_id = ? AND state IN ({placeholders})",
            (new_state, booking_id, *from_states),
        )

    def sum_workstation_units(self, workstation_id: str, start_utc: str, end_utc: str,
                              exclude_booking_id: str | None = None) -> int:
        sql = (
            "SELECT COALESCE(SUM(units), 0) AS used FROM occupancy"
            " WHERE resource_type = 'workstation' AND resource_id = ?"
            " AND state IN ('held', 'confirmed') AND start_utc < ? AND end_utc > ?"
        )
        params: list = [workstation_id, end_utc, start_utc]
        if exclude_booking_id is not None:
            sql += " AND booking_id != ?"
            params.append(exclude_booking_id)
        row = self._query_one(sql, tuple(params))
        return int(row["used"])
