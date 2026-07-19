#!/usr/bin/env python3
"""
Patient-facing scheduling assistant - FastAPI app + identity gate + chat agent.

This is a PATIENT chatbot: each person verifies who they are (name + date of
birth), and every tool is hard-bound to that one verified patient. There is no
roster and no way to reach another patient's data - the chat tools never accept
a patient name from the model; they operate only on the session's patient.

Data lives in the NexHealth PMS and is pulled LIVE:
  patient_data.py   - patient identity / demographics
  appointments.py   - slots, book, cancel, reschedule (all live)
  coverage_data.py  - insurance OOP estimates (from Stedi eligibility, not PMS)

Run:
  export OPENAI_API_KEY=... NEXHEALTH_API_KEY=...
  uvicorn main:app --port 8000     (from webapp/backend)
"""

import hashlib
import json
import os
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import appointments as appt
import coverage_data as cov
import patient_data as pd
from nexhealth_client import BACKEND_DIR, CUTOFF, PRACTICE, RULES

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

app = FastAPI(title="Green River Dental patient assistant")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ----------------------------------------------------------------------------- identity gate
# A verified session binds a token to exactly one PMS patient. The token hash ->
# {"name": ..., "pms_id": ...}. Everything the chat can do is scoped to this.
SESSIONS: dict[str, dict] = {}


class VerifyIn(BaseModel):
    name: str
    date_of_birth: str  # YYYY-MM-DD


@app.post("/api/verify")
def verify(inp: VerifyIn):
    """Confirm a patient by name + DOB against the PMS, then open a scoped session."""
    p = pd.resolve(inp.name)
    if not p:
        raise HTTPException(status_code=404, detail="We couldn't find you. Please check the spelling of your name.")
    bio = p.get("bio") or {}
    dob = (bio.get("date_of_birth") or "").strip()
    if dob != inp.date_of_birth.strip():
        raise HTTPException(status_code=401, detail="That date of birth doesn't match our records.")
    tok = secrets.token_urlsafe(24)
    full = pd._full_name(p)
    SESSIONS[hashlib.sha256(tok.encode()).hexdigest()] = {
        "name": full, "pms_id": p.get("id"),
        "chat": [],  # per-patient conversation history
    }
    return {"token": tok, "patient_name": full}


def session(authorization: str = Header(default="")) -> dict:
    tok = authorization.removeprefix("Bearer ").strip()
    s = SESSIONS.get(hashlib.sha256(tok.encode()).hexdigest())
    if not s:
        raise HTTPException(status_code=401, detail="Please verify your identity to continue.")
    return s


# ----------------------------------------------------------------------------- scoped tools
# The chat model never names a patient. Each tool closes over the verified
# patient from the session, so "book me a crown" can only ever act on THIS
# patient - a request about anyone else is impossible by construction.
def build_tools(patient_name: str):
    def my_coverage():
        return pd.find_patient(patient_name)

    def my_record():
        return pd.get_patient_record(patient_name)

    def my_appointments():
        return appt.get_appointments(patient_name)

    def slots(category: str, start_date: str, days: int = 5):
        return appt.available_slots(category, start_date, days)

    def category_rule(category: str):
        return appt.category_rule(category)

    def book(category: str, start_time: str, provider_id: int, operatory_id: int = None):
        return appt.book_appointment(patient_name, category, start_time, provider_id, operatory_id)

    def cancel(confirm: bool = False):
        return appt.cancel_appointment(patient_name, confirm)

    def reschedule(new_start_time: str):
        return appt.reschedule_appointment(patient_name, new_start_time)

    fns = {"my_coverage": my_coverage, "my_record": my_record,
           "my_appointments": my_appointments, "available_slots": slots,
           "category_rule": category_rule, "book_appointment": book,
           "cancel_appointment": cancel, "reschedule_appointment": reschedule}
    return fns, TOOL_SCHEMAS


TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "my_coverage",
        "description": "Your verified insurance coverage and per-procedure out-of-pocket estimate.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "my_record",
        "description": "Your demographic record on file (contact info, DOB).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "my_appointments",
        "description": "Your upcoming appointments, pulled live from the practice system.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "category_rule",
        "description": "How the office schedules a treatment category (duration, cutoff, offered?).",
        "parameters": {"type": "object", "properties": {"category": {"type": "string"}},
                       "required": ["category"]}}},
    {"type": "function", "function": {
        "name": "available_slots",
        "description": "Open appointment slots for a treatment category, filtered by office rules.",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string"}, "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "days": {"type": "integer", "default": 5}}, "required": ["category", "start_date"]}}},
    {"type": "function", "function": {
        "name": "book_appointment",
        "description": "Book YOUR appointment after you confirm a slot; the cost estimate is "
                       "written to the appointment note.",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string"}, "start_time": {"type": "string"},
            "provider_id": {"type": "integer"}, "operatory_id": {"type": "integer"}},
            "required": ["category", "start_time", "provider_id"]}}},
    {"type": "function", "function": {
        "name": "cancel_appointment",
        "description": "Cancel your upcoming appointment. Preview first, then call with confirm=true.",
        "parameters": {"type": "object", "properties": {"confirm": {"type": "boolean", "default": False}}}}},
    {"type": "function", "function": {
        "name": "reschedule_appointment",
        "description": "Move your upcoming appointment; office rules re-checked, duration kept.",
        "parameters": {"type": "object", "properties": {"new_start_time": {"type": "string"}},
                       "required": ["new_start_time"]}}},
]


def system_prompt(patient_name: str) -> str:
    return f"""You are the patient scheduling assistant for {PRACTICE['name']}
({PRACTICE['timezone']}). You are speaking with {patient_name} - a verified
patient. Today is 2026-07-19; the office is open {PRACTICE['open']}-{PRACTICE['close']}
{', '.join(PRACTICE['days'])}.

You can ONLY help {patient_name} with THEIR OWN care. You have no access to any
other patient and must never discuss, confirm, or deny anyone else's information -
if asked about another person, say you can only help them with their own account.

HARD SCOPE: only this practice's scheduling, {patient_name}'s costs/insurance,
office info, and general dental education. Refuse anything else in one sentence:
"I'm the {PRACTICE['name']} assistant - I can help with your appointments, costs,
and dental questions, but not with that." Never give code, non-dental content,
diagnoses, or medication/dosage advice. For emergencies (uncontrolled bleeding,
trauma, swelling affecting breathing/swallowing) tell them to call 911 or go to
the ER now.

Office rules (never violate):
- Not offered: {json.dumps(RULES['not_offered'])}. Offer a referral instead.
- {', '.join(CUTOFF['applies_to'])} must START before {CUTOFF['time']}.
- Durations (min): {json.dumps({k: v['minutes'] for k, v in RULES['appointments'].items()})}.
- If your coverage is inactive/absent, you get a 30-min consultation first, not treatment.

For "how much will I pay", call my_coverage and give the out-of-pocket total with
a short per-procedure breakdown; note amounts are estimates from a live insurance
check, not guarantees, and suggest a pre-determination for large estimated items.
Booking: available_slots -> the app shows slots as clickable pill buttons, so keep
text short ("Here are the open times:") and let them tap one -> book_appointment.
Use my_appointments for "when is my appointment". Be warm, concise, reassuring."""


# ----------------------------------------------------------------------------- chat
class ChatIn(BaseModel):
    message: str


@app.post("/api/chat")
def chat(inp: ChatIn, s: dict = Depends(session)):
    from openai import OpenAI
    client = OpenAI()
    name = s["name"]
    tool_fns, tool_schemas = build_tools(name)
    history = s["chat"]
    if not history:
        history.append({"role": "system", "content": system_prompt(name)})
    history.append({"role": "user", "content": inp.message})
    slots_for_ui, booked_for_ui = [], None
    for _ in range(8):
        resp = client.chat.completions.create(model=OPENAI_MODEL, messages=history, tools=tool_schemas)
        msg = resp.choices[0].message
        history.append({"role": "assistant", "content": msg.content,
                        "tool_calls": [tc.model_dump() for tc in (msg.tool_calls or [])] or None})
        if not msg.tool_calls:
            return {"reply": msg.content, "slots": slots_for_ui, "booked": booked_for_ui}
        for tc in msg.tool_calls:
            try:
                result = tool_fns[tc.function.name](**json.loads(tc.function.arguments))
            except Exception as e:
                result = {"error": str(e)[:300]}
            if tc.function.name == "available_slots" and isinstance(result, dict):
                slots_for_ui = result.get("slots", [])
            if tc.function.name == "book_appointment" and isinstance(result, dict) and result.get("booked"):
                booked_for_ui, slots_for_ui = result, []
            history.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
    return {"reply": "Sorry - I couldn't finish that. Please try again.",
            "slots": slots_for_ui, "booked": booked_for_ui}


@app.get("/api/rules")
def rules():
    # office info only - no patient data
    return {"practice": {k: PRACTICE[k] for k in ("name", "open", "close", "days", "timezone")},
            "not_offered": RULES["not_offered"], "afternoon_cutoff": CUTOFF,
            "appointments": {k: v["minutes"] for k, v in RULES["appointments"].items()}}


# serve the built React frontend so one port hosts everything
_dist = os.path.join(os.path.dirname(BACKEND_DIR), "frontend", "dist")
if os.path.isdir(_dist):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
