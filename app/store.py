import json
import sqlite3
from contextlib import closing, contextmanager

from app.errors import ReviewError
from app.manifest import canonical


class RunStore:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            yield connection

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS wafer_runs (
                    run_id TEXT PRIMARY KEY,
                    manifest TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    pinned TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS wafer_dispatches (
                    dispatch_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    lot TEXT NOT NULL,
                    capacity INTEGER NOT NULL,
                    data_sha256 TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(wafer_dispatches)")}
            if "lot" not in columns:
                connection.execute("ALTER TABLE wafer_dispatches ADD COLUMN lot TEXT NOT NULL DEFAULT ''")
            connection.execute("""
                CREATE INDEX IF NOT EXISTS wafer_dispatches_by_lot
                ON wafer_dispatches (lot, created_at)
            """)

    def put(self, manifest, snapshot, pinned):
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO wafer_runs VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO NOTHING",
                (manifest["run_id"], canonical(manifest), canonical(snapshot), canonical(pinned)),
            )
            created = cursor.rowcount == 1
            row = connection.execute("SELECT manifest FROM wafer_runs WHERE run_id = ?",
                                     (manifest["run_id"],)).fetchone()
        return json.loads(row[0]), created

    def get(self, run_id):
        with self.connect() as connection:
            row = connection.execute("SELECT manifest, snapshot, pinned FROM wafer_runs WHERE run_id = ?",
                                     (run_id,)).fetchone()
        if row is None:
            raise ReviewError("RUN_NOT_FOUND", "图批未找到", 404)
        return tuple(json.loads(part) for part in row)

    def list(self):
        with self.connect() as connection:
            rows = connection.execute("SELECT manifest FROM wafer_runs ORDER BY rowid DESC").fetchall()
        return [json.loads(row[0]) for row in rows]

    def publish_dispatch(self, dispatch_id, run_id, lot, capacity, data_sha256, plan_version, status, plan, created_at):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT dispatch_id, run_id, lot, capacity, data_sha256, plan_version, status, plan, created_at"
                " FROM wafer_dispatches WHERE dispatch_id = ?", (dispatch_id,)).fetchone()
            created = row is None
            if created:
                connection.execute(
                    "UPDATE wafer_dispatches SET status = 'SUPERSEDED'"
                    " WHERE lot = ? AND status = 'PUBLISHED'", (lot,))
                connection.execute(
                    "INSERT INTO wafer_dispatches"
                    " (dispatch_id, run_id, lot, capacity, data_sha256, plan_version, status, plan, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (dispatch_id, run_id, lot, capacity, data_sha256, plan_version, status,
                     canonical(plan), created_at))
            elif row[6] != "PUBLISHED":
                # 同一内容的旧版被替代后又重新发布：复位该版，不新增第二份单据。
                connection.execute(
                    "UPDATE wafer_dispatches SET status = 'SUPERSEDED'"
                    " WHERE lot = ? AND dispatch_id != ? AND status = 'PUBLISHED'", (lot, dispatch_id))
                connection.execute("UPDATE wafer_dispatches SET status = ?, run_id = ? WHERE dispatch_id = ?",
                                   (status, run_id, dispatch_id))
            row = connection.execute(
                "SELECT dispatch_id, run_id, lot, capacity, data_sha256, plan_version, status, plan, created_at"
                " FROM wafer_dispatches WHERE dispatch_id = ?", (dispatch_id,)).fetchone()
        return self._dispatch(row), created

    def dispatches(self, lot):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT dispatch_id, run_id, lot, capacity, data_sha256, plan_version, status, plan, created_at"
                " FROM wafer_dispatches WHERE lot = ? ORDER BY created_at ASC, rowid ASC", (lot,)).fetchall()
        return [self._dispatch(row) for row in rows]

    def dispatch(self, lot, dispatch_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT dispatch_id, run_id, lot, capacity, data_sha256, plan_version, status, plan, created_at"
                " FROM wafer_dispatches WHERE lot = ? AND dispatch_id = ?", (lot, dispatch_id)).fetchone()
        if row is None:
            raise ReviewError("DISPATCH_NOT_FOUND", "派工单未找到", 404)
        return self._dispatch(row)

    @staticmethod
    def _dispatch(row):
        keys = ("dispatch_id", "run_id", "lot", "capacity", "data_sha256", "plan_version",
                "status", "plan", "created_at")
        record = dict(zip(keys, row))
        record["plan"] = json.loads(record["plan"])
        return record
