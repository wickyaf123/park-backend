"""Tool implementations for the OpenAI Realtime agent.

Each tool is a plain function that the FastAPI route layer wraps. They are
also reachable via curl during development without going through the agent.

Naming and contracts mirror PRD §5.8–5.11 and §7 tool schemas exactly.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from db import (
    get_booking,
    insert_booking,
    new_booking_id,
    update_booking_record,
)
from slots import get_slot, load_slots, mark_available, mark_unavailable

DATA_DIR = Path(__file__).parent / "data"
BRANCHES_PATH = DATA_DIR / "branches.json"

# Central-London fallback when neither tier-1 nor tier-2 produces 3 results
# (rare with 59 branches — only fires for postcodes far outside our coverage,
# or completely malformed input). Order chosen by central transport accessibility.
DEFAULT_FALLBACK = ["liverpool-st", "kings-cross", "waterloo"]


def _load_branches_by_id() -> dict[str, dict]:
    branches = json.loads(BRANCHES_PATH.read_text())
    return {b["id"]: b for b in branches}


def _outward_full(postcode: str) -> str:
    """Full outward code: 'SE1 8UL' -> 'SE1', 'EC2M 7PY' -> 'EC2M', 'W5 5JY' -> 'W5'."""
    m = re.match(r"^\s*([A-Z]+\d+[A-Z]?)", postcode.upper())
    return m.group(1) if m else ""


def _outward_alpha(postcode: str) -> str:
    """Alpha prefix only: 'SE1 8UL' -> 'SE', 'W5 5JY' -> 'W'."""
    m = re.match(r"^\s*([A-Z]+)", postcode.upper())
    return m.group(1) if m else ""


def find_nearest_branches(postcode: str) -> dict:
    """Return the 3 nearest Clear Earwax branches via tiered postcode matching.

    Tier 1 — branches with the same full outward code as the caller (e.g.
             caller in SW4 7AA → Clapham Common at SW4 0HY).
    Tier 2 — branches in the same alpha-prefix area (e.g. caller in SE1 8UL
             also gets Greenwich SE10, Lewisham SE13, East Dulwich SE22).
    Tier 3 — central-London defaults if the first two tiers don't fill 3 slots.

    Scales to any UK postcode; no hand-curated prefix map. Branch order
    within each tier follows their order in branches.json (geographic-ish).
    """
    branches = json.loads(BRANCHES_PATH.read_text())
    target_full = _outward_full(postcode)
    target_alpha = _outward_alpha(postcode)

    tier1 = [b for b in branches if _outward_full(b["postcode"]) == target_full]
    seen = {b["id"] for b in tier1}
    tier2 = [
        b for b in branches
        if _outward_alpha(b["postcode"]) == target_alpha and b["id"] not in seen
    ]
    seen |= {b["id"] for b in tier2}

    chosen = tier1 + tier2
    if len(chosen) < 3:
        for did in DEFAULT_FALLBACK:
            if did in seen:
                continue
            b = next((b for b in branches if b["id"] == did), None)
            if b is not None:
                chosen.append(b)
                seen.add(did)
            if len(chosen) >= 3:
                break

    return {"postcode": postcode, "branches": chosen[:3]}


def check_availability(branch_id: str, date: str) -> dict:
    """PRD §5.9 — return up to 5 available slots for a branch on a given date."""
    slots = load_slots()
    available = [
        s for s in slots
        if s["branch_id"] == branch_id and s["date"] == date and s["available"]
    ]
    return {"branch_id": branch_id, "date": date, "slots": available[:5]}


def book_appointment(
    branch_id: str,
    slot_id: str,
    name: str,
    phone: str,
    symptoms: str | None = None,
) -> dict:
    """PRD §5.10 — write booking to DB, mark slot unavailable, return confirmation."""
    if not all([branch_id, slot_id, name, phone]):
        raise ValueError("branch_id, slot_id, name, and phone are required")

    by_id = _load_branches_by_id()
    if branch_id not in by_id:
        raise ValueError(f"unknown branch_id: {branch_id}")

    # Locate slot for confirmation message + datetime stamp.
    slots = load_slots()
    slot = next((s for s in slots if s["id"] == slot_id), None)
    if slot is None:
        raise ValueError(f"unknown slot_id: {slot_id}")
    if not slot["available"]:
        raise ValueError(f"slot already taken: {slot_id}")

    booking_id = new_booking_id()
    branch = by_id[branch_id]

    record = insert_booking(
        booking_id=booking_id,
        name=name,
        phone=phone,
        branch_id=branch_id,
        branch_name=branch["name"],
        slot_id=slot_id,
        slot_datetime=slot["datetime"],
        symptoms=symptoms,
    )
    mark_unavailable(slot_id)

    confirmation = (
        f"Booked at {branch['name']} on {slot['date']} at {slot['time']}. "
        f"Confirmation number: {booking_id}."
    )
    return {
        "booking_id": booking_id,
        "confirmation_message": confirmation,
        "branch": branch,
        "slot": {"id": slot_id, "date": slot["date"], "time": slot["time"]},
        "record": record,
    }


def update_booking(
    booking_id: str,
    name: str | None = None,
    phone: str | None = None,
    symptoms: str | None = None,
    slot_id: str | None = None,
    branch_id: str | None = None,
) -> dict:
    """Update a confirmed booking mid-session.

    Caller (the agent) supplies only the fields the patient wants changed.
    Slot/branch reassignment swaps slot availability atomically:
        - the OLD slot is released (mark_available)
        - the NEW slot is locked (mark_unavailable)
    If anything fails partway, the function raises ValueError and the
    bookings row is left untouched.
    """
    existing = get_booking(booking_id)
    if existing is None:
        raise ValueError(f"unknown booking_id: {booking_id}")

    # Resolve new branch (if changing) for branch_name lookup + slot validation.
    by_id = _load_branches_by_id()
    new_branch = None
    if branch_id is not None:
        if branch_id not in by_id:
            raise ValueError(f"unknown branch_id: {branch_id}")
        new_branch = by_id[branch_id]

    # Resolve new slot (if changing) and verify availability.
    new_slot = None
    if slot_id is not None:
        new_slot = get_slot(slot_id)
        if new_slot is None:
            raise ValueError(f"unknown slot_id: {slot_id}")
        if not new_slot["available"] and slot_id != existing["slot_id"]:
            raise ValueError(f"slot already taken: {slot_id}")
        # If branch is also changing, sanity-check the slot belongs to the
        # new branch — otherwise we'd record a Clapham slot under Stratford.
        target_branch = branch_id or existing["branch_id"]
        if new_slot["branch_id"] != target_branch:
            raise ValueError(
                f"slot {slot_id} belongs to {new_slot['branch_id']}, "
                f"not the target branch {target_branch}"
            )

    # Build the field dict for the DB update.
    fields: dict = {}
    if name is not None:
        fields["name"] = name
    if phone is not None:
        fields["phone"] = phone
    if symptoms is not None:
        fields["symptoms"] = symptoms
    if new_branch is not None:
        fields["branch_id"] = new_branch["id"]
        fields["branch_name"] = new_branch["name"]
    if new_slot is not None:
        fields["slot_id"] = new_slot["id"]
        fields["slot_datetime"] = new_slot["datetime"]

    if not fields:
        return {
            "booking_id": booking_id,
            "updated_fields": [],
            "confirmation_message": "Nothing to update — no changes provided.",
            "record": existing,
        }

    # Swap slot availability BEFORE the DB write so a slot-resolution failure
    # doesn't leave the database in a half-updated state.
    if new_slot is not None and new_slot["id"] != existing["slot_id"]:
        mark_available(existing["slot_id"])  # release old
        mark_unavailable(new_slot["id"])     # lock new

    record = update_booking_record(booking_id, fields)
    if record is None:
        # Extremely unlikely (we already checked existence) but guard anyway.
        raise ValueError(f"booking vanished during update: {booking_id}")

    # Build a friendly confirmation_message for the agent to read back.
    parts: list[str] = []
    if "name" in fields:
        parts.append(f"name to {fields['name']}")
    if "phone" in fields:
        parts.append(f"phone to {fields['phone']}")
    if "symptoms" in fields:
        parts.append("symptom note")
    if "branch_id" in fields:
        parts.append(f"branch to {fields['branch_name']}")
    if "slot_id" in fields:
        parts.append(f"slot to {new_slot['date']} at {new_slot['time']}")
    summary = ", ".join(parts) if parts else "no fields"
    confirmation = (
        f"Updated booking {booking_id}: changed {summary}."
    )

    return {
        "booking_id": booking_id,
        "updated_fields": list(fields.keys()),
        "confirmation_message": confirmation,
        "record": record,
    }


def send_sms_confirmation(phone: str, booking_id: str) -> dict:
    """PRD §5.11 — never actually send. Always print to stdout, always return success.

    The frontend renders a branded SMS preview card from the response body, so
    we now also return the message body and recipient verbatim. The caller
    (the model) sees `sent: true` and can verbalise "I've sent it" without
    needing to read the body itself.
    """
    message_id = uuid.uuid4().hex
    body = (
        f"Clear Earwax Clinic: your appointment ({booking_id}) is confirmed. "
        f"£59 for one or both ears — first follow-up within 14 days is free. "
        f"Reply CANCEL to cancel. — Park Medical Clinic Ltd"
    )
    print(f"[SMS MOCK] to={phone} booking_id={booking_id} message_id={message_id} body=\"{body}\"")
    return {
        "sent": True,
        "message_id": message_id,
        "to": phone,
        "message_body": body,
    }
