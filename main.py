"""parkmed-voice-demo backend.

FastAPI app: mints OpenAI Realtime ephemeral tokens, executes booking tools,
and exposes bookings + transcripts for the dashboard.

This is a deliberately scoped demo. See prd.md for non-goals.
"""

from __future__ import annotations

import os

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from db import (
    get_transcript,
    init_db,
    list_bookings_today,
    save_transcript,
    seed_demo_bookings,
)
from prompts import SYSTEM_PROMPT, TOOL_SCHEMAS
from slots import ensure_slots_fresh
from tools import (
    book_appointment,
    check_availability,
    find_nearest_branches,
    send_sms_confirmation,
    update_booking,
)

load_dotenv()

ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_REALTIME_MODEL = os.getenv(
    "OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17"
)
# `ballad` leans toward an English RP-ish delivery — closest match to the
# British receptionist persona among current Realtime voices. Hot-swap via
# OPENAI_REALTIME_VOICE without redeploying. Other options: alloy, ash, coral,
# echo, sage, shimmer, verse.
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "ballad")
OPENAI_SESSIONS_URL = "https://api.openai.com/v1/realtime/sessions"

app = FastAPI(title="parkmed-voice-demo", version="0.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN] if ALLOWED_ORIGIN != "*" else ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    ensure_slots_fresh()
    seeded = seed_demo_bookings()
    if seeded:
        print(f"[seed] inserted {seeded} demo bookings (table was empty)")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/bookings")
def bookings() -> list[dict]:
    return list_bookings_today()


# ---------- Transcripts ------------------------------------------------------


class TranscriptBody(BaseModel):
    booking_id: str
    turns: list[dict]


@app.post("/transcripts")
def post_transcript(body: TranscriptBody) -> dict:
    save_transcript(body.booking_id, body.turns)
    return {"ok": True, "booking_id": body.booking_id, "turn_count": len(body.turns)}


@app.get("/transcripts/{booking_id}")
def fetch_transcript(booking_id: str) -> dict:
    record = get_transcript(booking_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no transcript for {booking_id}")
    return record


# ---------- Realtime session minting -----------------------------------------


@app.post("/session")
async def session() -> dict:
    """Mint an OpenAI Realtime ephemeral token for the browser to use over WebRTC.

    PRD §5.4 — return the OpenAI response body verbatim. The frontend reads
    `client_secret.value` and uses it as the bearer token for the WebRTC SDP
    exchange against `https://api.openai.com/v1/realtime`.
    """
    if not OPENAI_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="OPENAI_API_KEY is not set on the backend.",
        )
    payload = {
        "model": OPENAI_REALTIME_MODEL,
        "voice": OPENAI_REALTIME_VOICE,
        "instructions": SYSTEM_PROMPT,
        "tools": TOOL_SCHEMAS,
        "tool_choice": "auto",
        "input_audio_transcription": {"model": "whisper-1"},
        "turn_detection": {
            "type": "server_vad",
            "threshold": 0.5,
            "silence_duration_ms": 500,
        },
    }
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(OPENAI_SESSIONS_URL, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"OpenAI request failed: {exc}") from exc

    if resp.status_code >= 400:
        # Surface the upstream error body so debugging is easy.
        try:
            body = resp.json()
        except ValueError:
            body = {"text": resp.text}
        raise HTTPException(status_code=resp.status_code, detail=body)

    return resp.json()


# ---------- Tool request bodies ----------------------------------------------


class FindBranchesBody(BaseModel):
    postcode: str = Field(min_length=2)


class CheckAvailabilityBody(BaseModel):
    branch_id: str
    date: str  # ISO YYYY-MM-DD


class BookAppointmentBody(BaseModel):
    branch_id: str
    slot_id: str
    name: str
    phone: str
    symptoms: str | None = None


class SendSmsBody(BaseModel):
    phone: str
    booking_id: str


class UpdateBookingBody(BaseModel):
    booking_id: str
    name: str | None = None
    phone: str | None = None
    symptoms: str | None = None
    slot_id: str | None = None
    branch_id: str | None = None


# ---------- Tool routes ------------------------------------------------------


@app.post("/tools/find_nearest_branches")
def tool_find_nearest_branches(body: FindBranchesBody) -> dict:
    return find_nearest_branches(body.postcode)


@app.post("/tools/check_availability")
def tool_check_availability(body: CheckAvailabilityBody) -> dict:
    return check_availability(body.branch_id, body.date)


@app.post("/tools/book_appointment")
def tool_book_appointment(body: BookAppointmentBody) -> dict:
    try:
        return book_appointment(
            branch_id=body.branch_id,
            slot_id=body.slot_id,
            name=body.name,
            phone=body.phone,
            symptoms=body.symptoms,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/tools/send_sms_confirmation")
def tool_send_sms_confirmation(body: SendSmsBody) -> dict:
    return send_sms_confirmation(body.phone, body.booking_id)


@app.post("/tools/update_booking")
def tool_update_booking(body: UpdateBookingBody) -> dict:
    try:
        return update_booking(
            booking_id=body.booking_id,
            name=body.name,
            phone=body.phone,
            symptoms=body.symptoms,
            slot_id=body.slot_id,
            branch_id=body.branch_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
