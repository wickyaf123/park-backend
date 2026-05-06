"""SQLite helpers for parkmed-voice-demo.

Single file `bookings.db` next to main.py. Schema is created idempotently on
first connection. Two tables: `bookings` (one row per appointment) and
`transcripts` (one row per booking, full conversation as JSON blob).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DB_PATH = Path(__file__).parent / "bookings.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    slot_datetime TEXT NOT NULL,
    symptoms TEXT,
    status TEXT DEFAULT 'confirmed'
);

CREATE TABLE IF NOT EXISTS transcripts (
    booking_id TEXT PRIMARY KEY,
    turns_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def new_booking_id() -> str:
    return uuid.uuid4().hex[:8].upper()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert_booking(
    *,
    booking_id: str,
    name: str,
    phone: str,
    branch_id: str,
    branch_name: str,
    slot_id: str,
    slot_datetime: str,
    symptoms: str | None,
) -> dict:
    record = {
        "id": booking_id,
        "created_at": now_iso(),
        "name": name,
        "phone": phone,
        "branch_id": branch_id,
        "branch_name": branch_name,
        "slot_id": slot_id,
        "slot_datetime": slot_datetime,
        "symptoms": symptoms or "",
        "status": "confirmed",
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO bookings
                (id, created_at, name, phone, branch_id, branch_name,
                 slot_id, slot_datetime, symptoms, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["id"],
                record["created_at"],
                record["name"],
                record["phone"],
                record["branch_id"],
                record["branch_name"],
                record["slot_id"],
                record["slot_datetime"],
                record["symptoms"],
                record["status"],
            ),
        )
        conn.commit()
    return record


def list_bookings_today() -> list[dict]:
    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE created_at LIKE ? ORDER BY created_at DESC",
            (f"{today_prefix}%",),
        ).fetchall()
    return [dict(row) for row in rows]


def get_booking(booking_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM bookings WHERE id = ?", (booking_id,)
        ).fetchone()
    return dict(row) if row else None


# Whitelist of columns the agent is allowed to update mid-session.
# `id`, `created_at`, `status` are deliberately NOT updateable — those are
# system-managed. `branch_name` is recomputed from `branch_id`, not set directly.
_UPDATABLE_FIELDS = {"name", "phone", "symptoms", "slot_id", "slot_datetime",
                     "branch_id", "branch_name"}


def update_booking_record(booking_id: str, fields: dict) -> dict | None:
    """Update whitelisted columns for an existing booking. Returns the new row,
    or None if the booking doesn't exist. Caller is responsible for slot
    swapping (mark old available, new unavailable) — this function only
    touches the bookings table."""
    safe = {k: v for k, v in fields.items() if k in _UPDATABLE_FIELDS and v is not None}
    if not safe:
        return get_booking(booking_id)

    set_clause = ", ".join(f"{k} = ?" for k in safe)
    values = list(safe.values()) + [booking_id]

    with connect() as conn:
        cur = conn.execute(
            f"UPDATE bookings SET {set_clause} WHERE id = ?",
            values,
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
    return get_booking(booking_id)


def save_transcript(booking_id: str, turns: list[dict]) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO transcripts (booking_id, turns_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(booking_id) DO UPDATE SET
                turns_json = excluded.turns_json,
                created_at = excluded.created_at
            """,
            (booking_id, json.dumps(turns), now_iso()),
        )
        conn.commit()


def get_transcript(booking_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM transcripts WHERE booking_id = ?", (booking_id,)
        ).fetchone()
    if row is None:
        return None
    return {
        "booking_id": row["booking_id"],
        "created_at": row["created_at"],
        "turns": json.loads(row["turns_json"]),
    }


# ---------- Demo seed -------------------------------------------------------
#
# Three "earlier today" bookings so the dashboard looks lived-in during the
# interview demo. Practitioner-branch pairs are taken from dossier §7 (real
# Trustpilot reviews). Phone numbers use the UK fictional prefix 07700 900xxx
# (officially reserved for drama/demo per Ofcom). Idempotent: only seeds when
# the bookings table is empty, so live calls during the demo never collide
# with seeds and a manually re-seeded DB doesn't get duplicate rows.

# Practitioner names listed by Trustpilot review against a specific branch.
# Caller names are realistic UK first+last to match the British UI tone.
_SEED_BOOKINGS: list[dict] = [
    {
        "minutes_ago": 90,
        "name": "Margaret Patel",
        "phone": "07700 900112",
        "branch_id": "bexley",
        "branch_name": "Bexley",
        "slot_offset_hours": 1,            # seen ~1h ago at the branch
        "slot_time": "09:00",
        "symptoms": "blocked left ear, hearing reduction (1 week)",
        "practitioner": "Mugunth",
    },
    {
        "minutes_ago": 60,
        "name": "James Okonkwo",
        "phone": "07700 900237",
        "branch_id": "stratford",
        "branch_name": "Stratford",
        "slot_offset_hours": 0,
        "slot_time": "10:00",
        "symptoms": "itchiness, mild pain",
        "practitioner": "Christo",
    },
    {
        "minutes_ago": 30,
        "name": "Linda Hughes",
        "phone": "07700 900458",
        "branch_id": "greenwich",
        "branch_name": "Greenwich",
        "slot_offset_hours": -1,           # upcoming, in 1h
        "slot_time": "11:00",
        "symptoms": "blocked feeling, both ears (3 days)",
        "practitioner": "Sunny",
    },
]


def _bookings_count() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM bookings").fetchone()
    return int(row["n"]) if row else 0


def seed_demo_bookings() -> int:
    """If bookings table is empty, insert the demo seed rows. Returns count seeded."""
    if _bookings_count() > 0:
        return 0

    from datetime import timedelta  # local import keeps module-level imports lean

    today = datetime.now(timezone.utc)
    seeded = 0
    for entry in _SEED_BOOKINGS:
        booking_id = new_booking_id()
        created_at = (today - timedelta(minutes=entry["minutes_ago"])).isoformat(timespec="seconds")
        slot_dt = (today - timedelta(hours=entry["slot_offset_hours"])).date().isoformat()
        slot_id = f"{entry['branch_id']}-{slot_dt.replace('-', '')}-{entry['slot_time'].replace(':', '')}"

        with connect() as conn:
            conn.execute(
                """
                INSERT INTO bookings
                    (id, created_at, name, phone, branch_id, branch_name,
                     slot_id, slot_datetime, symptoms, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    booking_id,
                    created_at,
                    entry["name"],
                    entry["phone"],
                    entry["branch_id"],
                    entry["branch_name"],
                    slot_id,
                    f"{slot_dt}T{entry['slot_time']}:00",
                    entry["symptoms"],
                    "confirmed",
                ),
            )
            conn.commit()

        # Stub transcript so the dashboard's "View" link doesn't 404.
        save_transcript(booking_id, [
            {
                "speaker": "system",
                "text": f"[Demo seed: assigned to {entry['practitioner']} at {entry['branch_name']}]",
                "at": created_at,
            }
        ])
        seeded += 1
    return seeded
