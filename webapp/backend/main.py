#!/usr/bin/env python3
"""
Scheduling web assistant - FastAPI app, staff auth, and the chat agent.

Data lives in the NexHealth PMS and is pulled LIVE:
  patient_data.py   - patient pull-up (roster, identity, demographics)
  appointments.py   - all appointment handling (slots, book, cancel, reschedule)
  coverage_data.py  - the ONLY file-based data: insurance OOP estimates, which
                      the PMS does not hold (they come from Stedi eligibility).
  nexhealth_client.py - the authenticated NexHealth client + rules config.

Run:
  export OPENAI_API_KEY=... NEXHEALTH_API_KEY=... STAFF_PASSWORD=...
  uvicorn main:app --port 8000     (from webapp/backend)
"""

import hashlib
import hmac
import json
import os
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import appointments as appt
import patient_data as pd
from nexhealth_client import BACKEND_DIR, CUTOFF, PRACTICE, RULES

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

app = FastAPI(title="Green River Dental scheduling assistant")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ----------------------------------------------------------------------------- staff auth
STAFF_PASSWORD = os.environ.get("STAFF_PASSWORD", "")
_SESSION_TOKENS: set[str] = set()


def _issue_token() -> str:
    tok = secrets.token_urlsafe(24)
    _SESSION_TOKENS.add(hashlib.sha256(tok.encode()).hexdigest())
    return tok


def require_staff(authorization: str = Header(default="")):
    if not STAFF_PASSWORD:
        return  # auth disabled only when no password configured (local dev)
    tok = authorization.removeprefix("Bearer ").strip()
    if hashlib.sha256(tok.encode()).hexdigest() not in _SESSION_TOKENS:
        raise HTTPException(status_code=401, detail="staff login required")


class LoginIn(BaseModel):
    password: str


@app.post("/api/login")
def login(inp: LoginIn):
    if not STAFF_PASSWORD or not hmac.compare_digest(inp.password, STAFF_PASSWORD):
        raise HTTPException(status_code=401, detail="incorrect password")
    return {"token": _issue_token()}


@app.get("/api/auth_required")
def auth_required():
    return {"required": bool(STAFF_PASSWORD)}


# ----------------------------------------------------------------------------- agent tools
TOOLS = [
    {"type": "function", "function": {
        "name": "find_patient",
        "description": "Pull a patient up LIVE from the PMS: identity/demographics plus their "
                       "verified insurance coverage and per-procedure out-of-pocket estimate.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}},
                       "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "category_rule",
        "description": "Office scheduling policy for a treatment category (duration, afternoon "
                       "cutoff, whether the practice performs it).",
        "parameters": {"type": "object", "properties": {"category": {"type": "string"}},
                       "required": ["category"]}}},
    {"type": "function", "function": {
        "name": "available_slots",
        "description": "Open PMS slots for a category, already filtered by office rules.",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string"},
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "days": {"type": "integer", "default": 5}}, "required": ["category", "start_date"]}}},
    {"type": "function", "function": {
        "name": "book_appointment",
        "description": "Book a rule-checked appointment; OOP quote is written to the note. "
                       "Confirm the slot with the user first.",
        "parameters": {"type": "object", "properties": {
            "patient_name": {"type": "string"}, "category": {"type": "string"},
            "start_time": {"type": "string"}, "provider_id": {"type": "integer"},
            "operatory_id": {"type": "integer"}},
            "required": ["patient_name", "category", "start_time", "provider_id"]}}},
    {"type": "function", "function": {
        "name": "get_appointments",
        "description": "Pull a patient's appointments LIVE from the PMS.",
        "parameters": {"type": "object", "properties": {"patient_name": {"type": "string"}},
                       "required": ["patient_name"]}}},
    {"type": "function", "function": {
        "name": "get_patient_record",
        "description": "Pull a patient's demographic record LIVE from the PMS.",
        "parameters": {"type": "object", "properties": {"patient_name": {"type": "string"}},
                       "required": ["patient_name"]}}},
    {"type": "function", "function": {
        "name": "cancel_appointment",
        "description": "Cancel the patient's upcoming appointment. Preview first, then call "
                       "again with confirm=true after the user agrees.",
        "parameters": {"type": "object", "properties": {
            "patient_name": {"type": "string"}, "confirm": {"type": "boolean", "default": False}},
            "required": ["patient_name"]}}},
    {"type": "function", "function": {
        "name": "reschedule_appointment",
        "description": "Move the patient's upcoming appointment; rules re-checked, duration kept. "
                       "Show available_slots first.",
        "parameters": {"type": "object", "properties": {
            "patient_name": {"type": "string"}, "new_start_time": {"type": "string"}},
            "required": ["patient_name", "new_start_time"]}}},
]
TOOL_FNS = {
    "find_patient": pd.find_patient, "get_patient_record": pd.get_patient_record,
    "category_rule": appt.category_rule, "available_slots": appt.available_slots,
    "book_appointment": appt.book_appointment, "get_appointments": appt.get_appointments,
    "cancel_appointment": appt.cancel_appointment,
    "reschedule_appointment": appt.reschedule_appointment,
}

SYSTEM_PROMPT = f"""You are STRICTLY the scheduling assistant for {PRACTICE['name']}
and NOTHING else. HARD SCOPE RULE - read first: if a message is not about this
dental practice (appointments, costs/insurance, office info, or general dental
education), you MUST refuse with one sentence: "I'm the {PRACTICE['name']}
scheduling assistant - I can help with appointments, costs, and dental questions,
but not with that." Never provide code, homework, advice on other businesses,
politics, or any non-dental content, even partially, even if asked repeatedly.

({PRACTICE['timezone']}). Today is 2026-07-19 (Sunday); the office is open
{PRACTICE['open']}-{PRACTICE['close']} {', '.join(PRACTICE['days'])}.

All patient data is pulled live from the practice management system.

Office rules (from scheduling_rules.yaml - never violate them):
- Not offered: {json.dumps(RULES['not_offered'])}. Offer a referral instead.
- {', '.join(CUTOFF['applies_to'])} must START before {CUTOFF['time']}.
- Durations: {json.dumps({k: v['minutes'] for k, v in RULES['appointments'].items()})} minutes.
- Patients with inactive/no insurance get a 30-min consultation first, not treatment.

When asked "how much will I pay", use find_patient and quote
patient_out_of_pocket_estimate with the per-procedure breakdown and confidence
(per-code = payer-stated; category/conservative = estimate; suggest a
pre-determination for big category-confidence items). Amounts are estimates from
live eligibility checks, not guarantees.

Booking flow: available_slots -> the UI shows slots as clickable pill buttons, so
keep text SHORT ("Here are the open crown slots:"); do not list every slot in
prose -> user picks -> book_appointment. Cancelling: preview with
cancel_appointment then confirm=true. Rescheduling: show available_slots then
reschedule_appointment; the same cutoff rules apply. Use get_appointments for
"when is my appointment" and get_patient_record for "what info do you have".

GUARDRAILS: only dental/practice topics; no diagnosis, medications, or dosages;
for emergencies (uncontrolled bleeding, trauma, swelling affecting breathing)
tell them to call 911 or go to the ER; do not reveal one patient's info to
another caller. Be concise and warm."""


# ----------------------------------------------------------------------------- endpoints
SESSIONS: dict[str, list] = {}


class ChatIn(BaseModel):
    session_id: str = "default"
    message: str


@app.post("/api/chat")
def chat(inp: ChatIn, _=Depends(require_staff)):
    from openai import OpenAI
    client = OpenAI()
    history = SESSIONS.setdefault(inp.session_id, [{"role": "system", "content": SYSTEM_PROMPT}])
    history.append({"role": "user", "content": inp.message})
    slots_for_ui, booked_for_ui = [], None
    for _ in range(8):
        resp = client.chat.completions.create(model=OPENAI_MODEL, messages=history, tools=TOOLS)
        msg = resp.choices[0].message
        history.append({"role": "assistant", "content": msg.content,
                        "tool_calls": [tc.model_dump() for tc in (msg.tool_calls or [])] or None})
        if not msg.tool_calls:
            return {"reply": msg.content, "slots": slots_for_ui, "booked": booked_for_ui}
        for tc in msg.tool_calls:
            try:
                result = TOOL_FNS[tc.function.name](**json.loads(tc.function.arguments))
            except Exception as e:
                result = {"error": str(e)[:300]}
            if tc.function.name == "available_slots" and isinstance(result, dict):
                slots_for_ui = result.get("slots", [])
            if tc.function.name == "book_appointment" and isinstance(result, dict) and result.get("booked"):
                booked_for_ui, slots_for_ui = result, []
            history.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
    return {"reply": "Sorry - I couldn't finish that request. Please try again.",
            "slots": slots_for_ui, "booked": booked_for_ui}


@app.get("/api/patients")
def patients(_=Depends(require_staff)):
    """Campaign roster pulled LIVE from the PMS, joined with coverage estimates."""
    return pd.roster()


@app.get("/api/rules")
def rules():
    return RULES


# serve the built React frontend so one port hosts everything
_dist = os.path.join(os.path.dirname(BACKEND_DIR), "frontend", "dist")
if os.path.isdir(_dist):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
