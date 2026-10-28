"""Transactional event log and rebuildable workload projections."""

import contextlib
import json
import sqlite3
import threading


class Store:
    # Open the shared SQLite connection and initialize the durable log and projections.
    # WAL and FULL synchronization support committed-state recovery after a crash.
    # Inputs:
    #   self: Store instance being initialized.
    #   path: SQLite database file path.
    # Output: None; creates the connection, lock, and missing schema objects.
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

    # Serialize access to the shared connection and wrap a mutation in one transaction.
    # Commit on success; roll back on any exception, including interruption.
    # Inputs:
    #   self: Store whose shared connection and lock guard the transaction.
    # Output: Context manager yielding None; commits on success and rolls back on error.
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

    # Decode one workload or VM record, returning None when absent.
    # Reads share the caller's transaction with any dependent state changes.
    # Inputs:
    #   self: Store with an open SQLite connection.
    #   table: "workloads" or "vms".
    #   key: Record ID to look up.
    # Output: Decoded record dictionary, or None if the ID does not exist.
    def get(self, table, key):
        assert table in {"workloads", "vms"}
        row = self.db.execute(f"SELECT data FROM {table} WHERE id = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    # Decode all records from an allowed projection table in insertion order.
    # Callers use a transaction when the results inform scheduling or mutation.
    # Inputs:
    #   self: Store with an open SQLite connection.
    #   table: "workloads" or "vms".
    # Output: List of decoded record dictionaries ordered by SQLite row ID.
    def all(self, table):
        assert table in {"workloads", "vms"}
        return [json.loads(r[0]) for r in self.db.execute(f"SELECT data FROM {table} ORDER BY rowid")]

    # Insert or replace a JSON projection record within the caller's transaction.
    # Only the internal workload and VM tables are allowed as SQL identifiers.
    # Inputs:
    #   self: Store with an open transaction.
    #   table: "workloads" or "vms".
    #   key: Record ID to insert or replace.
    #   value: JSON-serializable record dictionary.
    # Output: None; writes the projection without committing the transaction.
    def put(self, table, key, value):
        assert table in {"workloads", "vms"}
        self.db.execute(f"INSERT INTO {table} VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                        (key, json.dumps(value, allow_nan=False)))

    # Increment the workload version and append an event with optional worker context.
    # SQLite assigns its globally ordered sequence number; the caller owns the commit.
    # Inputs:
    #   self: Store with an open transaction.
    #   task: Mutable workload dictionary supplying its ID and version.
    #   kind: Event type string.
    #   now: Event timestamp in seconds.
    #   worker: Optional worker dictionary supplying ID, attempt, and parent context.
    #   **payload: JSON-serializable event fields.
    # Output: None; advances task's version and inserts the event.
    def event(self, task, kind, now, worker=None, **payload):
        task["version"] += 1
        self.db.execute("INSERT INTO events(task_id,worker_id,attempt,parent_id,timestamp,version,type,payload) VALUES(?,?,?,?,?,?,?,?)",
                        (task["id"], worker["id"] if worker else None,
                         worker["attempt"] if worker else None, worker.get("parent_id") if worker else None,
                         now, task["version"], kind, json.dumps(payload, allow_nan=False)))

    # Append a complete workload checkpoint and update its materialized projection.
    # Both writes belong to the caller's transaction so replay sees a consistent state.
    # Inputs:
    #   self: Store with an open transaction.
    #   task: Mutable workload dictionary to snapshot.
    #   now: Checkpoint timestamp in seconds.
    # Output: None; increments the version and writes a checkpoint plus its projection.
    def save(self, task, now):
        # A durable snapshot event makes the projection reconstructible after every transaction.
        task["version"] += 1
        self.db.execute("INSERT INTO events(task_id,timestamp,version,type,payload) VALUES(?,?,?,?,?)",
                        (task["id"], now, task["version"], "checkpoint", json.dumps(task, allow_nan=False)))
        self.put("workloads", task["id"], task)

    # Atomically reconstruct workload projections from checkpoint events in log order.
    # VM records and the simulated effect registry are retained unchanged.
    # Inputs:
    #   self: Store containing the checkpoint event history.
    # Output: None; replaces workload projections with their latest logged snapshots.
    def rebuild(self):
        with self.transaction():
            self.db.execute("DELETE FROM workloads")
            for row in self.db.execute("SELECT task_id,payload FROM events WHERE type='checkpoint' ORDER BY seq"):
                self.put("workloads", row["task_id"], json.loads(row["payload"]))

    # Read a page of decoded workload events strictly after the supplied sequence.
    # Optional worker filtering preserves global sequence ordering for pagination.
    # Inputs:
    #   self: Store containing the event log.
    #   task_id: Workload identifier to filter by.
    #   worker: Optional worker ID; None includes workload-wide and worker events.
    #   after: Exclusive lower sequence bound; defaults to 0.
    #   limit: Maximum number of events returned; defaults to 500.
    # Output: Event dictionaries in ascending sequence order, with decoded payloads.
    def events(self, task_id, worker=None, after=0, limit=500):
        sql = "SELECT * FROM events WHERE task_id=? AND seq>?"
        args = [task_id, after]
        if worker is not None:
            sql += " AND worker_id=?"
            args.append(worker)
        sql += " ORDER BY seq LIMIT ?"
        args.append(limit)
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in self.db.execute(sql, args)]
