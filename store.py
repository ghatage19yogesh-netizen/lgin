"""Tiny JSON-file store for allocation runs, shared by every user of the app instance.

Good enough for a demo / pilot. For production, swap this for BigQuery, Cloud SQL or
Firestore so runs survive restarts and redeploys.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd

DATA_DIR = Path(os.environ.get("ALLOC_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
RUNS_FILE = DATA_DIR / "runs.json"
_LOCK = threading.Lock()

DF_KEYS = ["detail", "summary", "shares", "filled_lanes", "slow_notes"]


def _read() -> list[dict]:
    if not RUNS_FILE.exists():
        return []
    try:
        return json.loads(RUNS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _write(runs: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RUNS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(runs, default=str, indent=1))
    tmp.replace(RUNS_FILE)


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def serialise_results(res: dict) -> dict:
    out = {k: v for k, v in res.items() if k not in DF_KEYS}
    for k in DF_KEYS:
        df = res.get(k)
        out[k] = [] if df is None else json.loads(df.to_json(orient="records"))
    return out


def deserialise_results(raw: dict) -> dict:
    res = dict(raw)
    for k in DF_KEYS:
        res[k] = pd.DataFrame(raw.get(k, []))
    return res


def save_run(record: dict) -> dict:
    with _LOCK:
        runs = _read()
        runs = [r for r in runs if r["id"] != record["id"]] + [record]
        _write(runs)
    return record


def list_runs() -> list[dict]:
    with _LOCK:
        return sorted(_read(), key=lambda r: r["created_at"], reverse=True)


def get_run(run_id: str) -> dict | None:
    return next((r for r in list_runs() if r["id"] == run_id), None)


def update_run(run_id: str, **changes) -> dict | None:
    with _LOCK:
        runs = _read()
        for r in runs:
            if r["id"] == run_id:
                r.update(changes)
                r.setdefault("history", []).append({"at": now(), **{k: v for k, v in changes.items()
                                                                     if k in ("status", "comment", "by")}})
                _write(runs)
                return r
    return None


def last_approved_slow_map() -> dict:
    for r in list_runs():
        if r.get("status") == "Approved":
            return r.get("results", {}).get("slow_map", {}) or {}
    return {}
