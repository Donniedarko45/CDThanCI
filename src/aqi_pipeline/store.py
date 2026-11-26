"""SQLite store: observations, forecasts, monitoring snapshots, events and state.

One file holds everything the pipeline needs between cycles, so a run can be
stopped and resumed, and every decision can be audited afterwards.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pandas as pd

from .schema import OBS_COLUMNS
from .validation import apply_mask

TS = "%Y-%m-%d %H:%M:%S"


def _ts(value) -> str:
    return pd.Timestamp(value).strftime(TS)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init(self) -> None:
        cols = ", ".join(f"{c} REAL" for c in OBS_COLUMNS)
        with self._conn() as c:
            c.executescript(f"""
            CREATE TABLE IF NOT EXISTS observations (
                timestamp TEXT PRIMARY KEY, {cols},
                qc_mask INTEGER NOT NULL DEFAULT 0, qc_notes TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS predictions (
                made_at TEXT NOT NULL, target_time TEXT NOT NULL, model_version TEXT NOT NULL,
                role TEXT NOT NULL, y_pred REAL, y_model REAL, y_naive REAL,
                served TEXT NOT NULL DEFAULT 'model', input_ok INTEGER NOT NULL DEFAULT 1,
                y_true REAL, label_ok INTEGER,
                PRIMARY KEY (made_at, model_version));
            CREATE INDEX IF NOT EXISTS idx_pred_target ON predictions (target_time);
            CREATE TABLE IF NOT EXISTS cycles (cycle_time TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
                severity TEXT NOT NULL, message TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{{}}');
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    # ---- observations ------------------------------------------------------
    def upsert_observations(self, df: pd.DataFrame) -> int:
        """Store validated rows (index = timestamp, with qc_mask and qc_notes)."""
        if df.empty:
            return 0
        cols = ["timestamp"] + OBS_COLUMNS + ["qc_mask", "qc_notes"]
        frame = df.reset_index()
        frame["timestamp"] = frame["timestamp"].dt.strftime(TS)
        values = frame[cols].astype(object).where(frame[cols].notna(), None).values.tolist()
        sql = f"INSERT OR REPLACE INTO observations ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        with self._conn() as c:
            c.executemany(sql, values)
        return len(values)

    def load_observations(self, start=None, end=None, clean: bool = True) -> pd.DataFrame:
        """Observations on a complete hourly index.

        ``clean=True`` blanks cells flagged by the quality gate and keeps ``qc_mask``;
        ``clean=False`` returns the raw readings as stored.
        """
        sql, args = "SELECT * FROM observations", []
        clauses = []
        if start is not None:
            clauses.append("timestamp >= ?")
            args.append(_ts(start))
        if end is not None:
            clauses.append("timestamp <= ?")
            args.append(_ts(end))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._conn() as c:
            df = pd.read_sql_query(sql + " ORDER BY timestamp", c, params=args)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
        if df.empty:
            return df
        df = df.reindex(pd.date_range(df.index[0], df.index[-1], freq="h"))
        df.index.name = "timestamp"
        df["qc_mask"] = df["qc_mask"].fillna(0).astype(np.int64)
        df["qc_notes"] = df["qc_notes"].fillna("gap")
        df[OBS_COLUMNS] = df[OBS_COLUMNS].astype(float)
        if clean:
            cleaned = apply_mask(df)
            cleaned["qc_mask"] = df["qc_mask"]
            return cleaned
        return df

    def last_observation_time(self) -> pd.Timestamp | None:
        with self._conn() as c:
            row = c.execute("SELECT MAX(timestamp) FROM observations").fetchone()
        return pd.Timestamp(row[0]) if row and row[0] else None

    # ---- predictions -------------------------------------------------------
    def insert_predictions(self, df: pd.DataFrame) -> int:
        cols = ["made_at", "target_time", "model_version", "role", "y_pred", "y_model", "y_naive",
                "served", "input_ok"]
        if df.empty:
            return 0
        frame = df[cols].copy()
        for col in ("made_at", "target_time"):
            frame[col] = pd.to_datetime(frame[col]).dt.strftime(TS)
        values = frame.astype(object).where(frame.notna(), None).values.tolist()
        sql = (f"INSERT OR REPLACE INTO predictions ({', '.join(cols)}) "
               f"VALUES ({', '.join('?' * len(cols))})")
        with self._conn() as c:
            c.executemany(sql, values)
        return len(values)

    def last_prediction_time(self, model_version: str | None = None) -> pd.Timestamp | None:
        sql, args = "SELECT MAX(made_at) FROM predictions", []
        if model_version:
            sql += " WHERE model_version = ?"
            args.append(model_version)
        with self._conn() as c:
            row = c.execute(sql, args).fetchone()
        return pd.Timestamp(row[0]) if row and row[0] else None

    def unscored_targets(self, until) -> list[str]:
        with self._conn() as c:
            rows = c.execute("SELECT DISTINCT target_time FROM predictions "
                             "WHERE label_ok IS NULL AND target_time <= ?", [_ts(until)]).fetchall()
        return [r[0] for r in rows]

    def score_predictions(self, labels: pd.DataFrame) -> int:
        """Attach matured labels. ``labels``: index target_time, columns y_true, label_ok."""
        if labels.empty:
            return 0
        values = [(None if pd.isna(r.y_true) else float(r.y_true), int(r.label_ok), _ts(idx))
                  for idx, r in labels.iterrows()]
        with self._conn() as c:
            c.executemany("UPDATE predictions SET y_true = ?, label_ok = ? WHERE target_time = ?", values)
        return len(values)

    def load_predictions(self, start=None, end=None, role: str | None = None,
                         model_version: str | None = None, by: str = "target_time") -> pd.DataFrame:
        sql, args, clauses = "SELECT * FROM predictions", [], []
        if start is not None:
            clauses.append(f"{by} > ?")
            args.append(_ts(start))
        if end is not None:
            clauses.append(f"{by} <= ?")
            args.append(_ts(end))
        if role:
            clauses.append("role = ?")
            args.append(role)
        if model_version:
            clauses.append("model_version = ?")
            args.append(model_version)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._conn() as c:
            df = pd.read_sql_query(sql + f" ORDER BY {by}", c, params=args)
        for col in ("made_at", "target_time"):
            df[col] = pd.to_datetime(df[col])
        return df

    # ---- cycles, events, state --------------------------------------------
    def log_cycle(self, cycle_time, payload: dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO cycles (cycle_time, payload) VALUES (?, ?)",
                      [_ts(cycle_time), json.dumps(payload, default=_json_default)])

    def load_cycles(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT cycle_time, payload FROM cycles ORDER BY cycle_time").fetchall()
        return [{"cycle_time": r[0], **json.loads(r[1])} for r in rows]

    def latest_cycle(self) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT cycle_time, payload FROM cycles ORDER BY cycle_time DESC LIMIT 1").fetchone()
        return {"cycle_time": row[0], **json.loads(row[1])} if row else None

    def log_event(self, ts, kind: str, severity: str, message: str,
                  payload: dict[str, Any] | None = None) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO events (ts, kind, severity, message, payload) VALUES (?, ?, ?, ?, ?)",
                      [_ts(ts), kind, severity, message, json.dumps(payload or {}, default=_json_default)])

    def load_events(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT ts, kind, severity, message, payload FROM events ORDER BY id"
        with self._conn() as c:
            rows = c.execute(sql).fetchall()
        events = [{"ts": r[0], "kind": r[1], "severity": r[2], "message": r[3],
                   "payload": json.loads(r[4])} for r in rows]
        return events[-limit:] if limit else events

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._conn() as c:
            row = c.execute("SELECT value FROM state WHERE key = ?", [key]).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                      [key, json.dumps(value, default=_json_default)])


def _json_default(obj):
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp,)):
        return obj.strftime(TS)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"not JSON serialisable: {type(obj)}")
