"""Slot generator for parkmed-voice-demo.

`slots.json` is regenerated on server startup if missing or older than 24 h.
For each branch, generates 7 days × 6 times (09:00, 10:00, 11:00, 14:00, 15:00,
16:00) with ~30 % marked unavailable so the agent has realistic "let me find
another time" moments. Slot ID format: `{branch_id}-{YYYYMMDD}-{HHMM}`.
"""

from __future__ import annotations

import json
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
SLOTS_PATH = DATA_DIR / "slots.json"
BRANCHES_PATH = DATA_DIR / "branches.json"

TIMES = ["09:00", "10:00", "11:00", "14:00", "15:00", "16:00"]
DAYS_AHEAD = 7
UNAVAILABLE_RATE = 0.30
MAX_AGE_SECONDS = 24 * 3600


def _load_branches() -> list[dict]:
    return json.loads(BRANCHES_PATH.read_text())


def _generate_for_branch(branch_id: str, today: date, rng: random.Random) -> list[dict]:
    slots: list[dict] = []
    for day_offset in range(DAYS_AHEAD):
        d = today + timedelta(days=day_offset)
        for t in TIMES:
            hh, mm = t.split(":")
            slot_id = f"{branch_id}-{d.strftime('%Y%m%d')}-{hh}{mm}"
            slots.append(
                {
                    "id": slot_id,
                    "branch_id": branch_id,
                    "date": d.isoformat(),
                    "time": t,
                    "datetime": f"{d.isoformat()}T{t}:00",
                    "available": rng.random() >= UNAVAILABLE_RATE,
                }
            )
    return slots


def generate_slots(seed: int | None = None) -> dict:
    today = date.today()
    rng = random.Random(seed) if seed is not None else random.Random()
    branches = _load_branches()
    payload = {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "slots": [s for b in branches for s in _generate_for_branch(b["id"], today, rng)],
    }
    SLOTS_PATH.write_text(json.dumps(payload, indent=2))
    return payload


def ensure_slots_fresh() -> dict:
    if not SLOTS_PATH.exists():
        return generate_slots()
    age = time.time() - SLOTS_PATH.stat().st_mtime
    if age > MAX_AGE_SECONDS:
        return generate_slots()
    return json.loads(SLOTS_PATH.read_text())


def load_slots() -> list[dict]:
    if not SLOTS_PATH.exists():
        ensure_slots_fresh()
    return json.loads(SLOTS_PATH.read_text())["slots"]


def save_slots(slots: list[dict]) -> None:
    payload = {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "slots": slots,
    }
    SLOTS_PATH.write_text(json.dumps(payload, indent=2))


def mark_unavailable(slot_id: str) -> bool:
    return _set_availability(slot_id, available=False)


def mark_available(slot_id: str) -> bool:
    """Release a slot — used when a booking is reassigned to a different slot."""
    return _set_availability(slot_id, available=True)


def _set_availability(slot_id: str, *, available: bool) -> bool:
    slots = load_slots()
    found = False
    for s in slots:
        if s["id"] == slot_id:
            s["available"] = available
            found = True
            break
    if found:
        save_slots(slots)
    return found


def get_slot(slot_id: str) -> dict | None:
    for s in load_slots():
        if s["id"] == slot_id:
            return s
    return None
