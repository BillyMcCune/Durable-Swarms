"""Transactional event log and rebuildable workload projections."""

import contextlib
import json
import sqlite3
import threading


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS workloads (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS vms (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                worker_id TEXT, attempt INTEGER, parent_id TEXT,
                timestamp REAL NOT NULL, version INTEGER NOT NULL,
                type TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS event_task ON events(task_id, seq);
            CREATE TABLE IF NOT EXISTS effects (
                operation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                payload TEXT NOT NULL, receipt TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS world (
                task_id TEXT NOT NULL, key TEXT NOT NULL, value INTEGER NOT NULL,
                PRIMARY KEY(task_id, key));
        """)

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def get(self, table, key):
        assert table in {"workloads", "vms"}
        row = self.db.execute(f"SELECT data FROM {table} WHERE id = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, table):
        assert table in {"workloads", "vms"}
        return [json.loads(r[0]) for r in self.db.execute(f"SELECT data FROM {table} ORDER BY rowid")]

    def put(self, table, key, value):
        assert table in {"workloads", "vms"}
        self.db.execute(f"INSERT INTO {table} VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                        (key, json.dumps(value, allow_nan=False)))

    def event(self, task, kind, now, worker=None, **payload):
        task["version"] += 1
        self.db.execute("INSERT INTO events(task_id,worker_id,attempt,parent_id,timestamp,version,type,payload) VALUES(?,?,?,?,?,?,?,?)",
                        (task["id"], worker["id"] if worker else None,
                         worker["attempt"] if worker else None, worker.get("parent_id") if worker else None,
                         now, task["version"], kind, json.dumps(payload, allow_nan=False)))

    def save(self, task, now):
        # A durable snapshot event makes the projection reconstructible after every transaction.
        task["version"] += 1
        self.db.execute("INSERT INTO events(task_id,timestamp,version,type,payload) VALUES(?,?,?,?,?)",
                        (task["id"], now, task["version"], "checkpoint", json.dumps(task, allow_nan=False)))
        self.put("workloads", task["id"], task)

    def rebuild(self):
        with self.transaction():
            self.db.execute("DELETE FROM workloads")
            for row in self.db.execute("SELECT task_id,payload FROM events WHERE type='checkpoint' ORDER BY seq"):
                self.put("workloads", row["task_id"], json.loads(row["payload"]))

    def events(self, task_id, worker=None, after=0, limit=500):
        sql = "SELECT * FROM events WHERE task_id=? AND seq>?"
        args = [task_id, after]
        if worker is not None:
            sql += " AND worker_id=?"
            args.append(worker)
        sql += " ORDER BY seq LIMIT ?"
        args.append(limit)
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in self.db.execute(sql, args)]
