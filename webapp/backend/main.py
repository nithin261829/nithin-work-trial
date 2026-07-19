#!/usr/bin/env python3
"""
Scheduling web assistant - FastAPI backend.

A chat agent (OpenAI tool-calling) for front-desk / patient scheduling questions:
  - "How much will Sharon Mascari pay out of pocket?"    -> coverage lookup
  - "What crown slots are open Tuesday morning?"          -> NexHealth slot search
  - "Book Sharon's crown for Tuesday 9am"                 -> rule-checked booking

All office policy comes from scheduling_rules.yaml (shared with the batch
scheduler): treatments not offered (implants -> referral), per-category
durations, and the no-crowns/bridges/root-canals/veneers-after-3pm rule.

Run locally:
  export OPENAI_API_KEY=... NEXHEALTH_API_KEY=...
  uvicorn main:app --reload --port 8000        (from webapp/backend)

Endpoints:
  POST /api/chat   {"session_id": "...", "message": "..."} -> {"reply": "..."}
  GET  /api/patients                       -> roster w/ coverage + booking status
  GET  /api/rules                          -> the parsed rules (for the UI header)
"""

import csv
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(os.path.dirname(BACKEND_DIR))
RULES = yaml.safe_load(open(os.path.join(REPO_DIR, "scheduling_rules.yaml")))
CUTOFF = RULES["afternoon_cutoff"]
CUTOFF_HOUR = int(CUTOFF["time"].split(":")[0])
NEXHEALTH_API_KEY = os.environ.get("NEXHEALTH_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

app = FastAPI(title="Green River Dental scheduling assistant")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ----------------------------------------------------------------------------- data
def _read(name):
    with open(os.path.join(REPO_DIR, name), newline="") as f:
        return list(csv.DictReader(f))


REPORT = {r["patient_name"]: r for r in _read("targeted_patient_coverage_report.csv")}
DETAIL = {}
for d in _read("targeted_procedure_detail.csv"):
    DETAIL.setdefault(d["patient"], []).append(d)
BOOKINGS = {r["patient"]: r for r in _read("nexhealth_booking_results.csv")}
NEX_STATE = json.load(open(os.path.join(REPO_DIR, "nexhealth_cache", "state.json")))


# ----------------------------------------------------------------------------- NexHealth client
class NexHealth:
    def __init__(self):
        self.token = None

    def _req(self, method, path, params=None, body=None):
        if self.token is None and path != "/authenticates":
            self.login()
        params = {"subdomain": RULES["practice"]["subdomain"],
                  "location_id": RULES["practice"]["location_id"], **(params or {})}
        if path == "/authenticates" or "lids[]" in params:
            params.pop("location_id", None)
        url = f"https://nexhealth.info{path}?{urllib.parse.urlencode(params, doseq=True)}"
        headers = {"Nex-Api-Version": "v20240412", "Accept": "application/json",
                   "User-Agent": "scheduling-assistant/1.0"}
        headers["Authorization"] = (NEXHEALTH_API_KEY if path == "/authenticates"
                                    else f"Bearer {self.token}")
        data = json.dumps(body).encode() if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data, headers, method=method)
        try:
            resp = json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            resp = json.loads(e.read())
        if resp.get("error"):
            raise RuntimeError(str(resp["error"]))
        return resp

    def login(self):
        r = self._req("POST", "/authenticates")
        self.token = r["data"]["token"]


nex = NexHealth()


# ----------------------------------------------------------------------------- tools
def find_patient(name: str) -> dict:
    """Coverage + booking status for a patient (fuzzy name match)."""
    key = next((k for k in REPORT if name.lower() in k.lower()
                or all(w in k.lower() for w in name.lower().split())), None)
    if not key:
        return {"error": f"no patient matching '{name}'",
                "roster": sorted(REPORT)}
    r = REPORT[key]
    b = BOOKINGS.get(key, {})
    return {
        "patient": key, "treatment_category": r["treatment_category"],
        "coverage_status": r["coverage_status"], "carrier": r["carrier"],
        "deductible_remaining": r["deductible_remaining"],
        "annual_max_remaining": r["annual_max_remaining"],
        "target_treatment_fee": r["pending_total_fee"],
        "insurance_pays_estimate": r["est_insurance_pays"],
        "patient_out_of_pocket_estimate": r["est_patient_out_of_pocket"],
        "per_procedure": [
            {"code": d["procedure_code"], "description": d["description"],
             "fee": d["fee"], "patient_pays": d["patient_oop_est"],
             "basis": d["basis"], "confidence": d["confidence"]}
            for d in DETAIL.get(key, [])],
        "notes": r["notes"],
        "booking": {"status": b.get("status"), "start": b.get("start"),
                    "appointment_type": b.get("type"),
                    "appointment_id": b.get("appointment_id")},
    }


def category_rule(category: str) -> dict:
    """What the office rules say about scheduling one treatment category."""
    if category in RULES["not_offered"]:
        return {"offered": False, "policy": RULES["not_offered"][category]}
    appt = RULES["appointments"].get(category)
    if not appt:
        return {"error": f"unknown category '{category}'",
                "known": list(RULES["appointments"])}
    return {"offered": True, "minutes": appt["minutes"],
            "appointment_type": appt["type_name"],
            "must_start_before": CUTOFF["time"] if category in CUTOFF["applies_to"] else None}


def available_slots(category: str, start_date: str, days: int = 5) -> dict:
    """Open slots for a treatment category honoring every office rule."""
    rule = category_rule(category)
    if not rule.get("offered"):
        return rule
    type_id = NEX_STATE["appointment_types"].get(rule["appointment_type"])
    r = nex._req("GET", "/available_slots",
                 {"lids[]": RULES["practice"]["location_id"], "start_date": start_date,
                  "days": days, "slot_length": rule["minutes"],
                  "appointment_type_id": type_id})
    out = []
    for loc in r.get("data", []):
        for slot in loc.get("slots", []):
            start = datetime.fromisoformat(slot["time"])
            if rule["must_start_before"] and start.hour >= CUTOFF_HOUR:
                continue
            out.append({"start": slot["time"], "provider_id": loc.get("pid"),
                        "operatory_id": slot.get("operatory_id")})
            if len(out) >= 20:
                return {"slots": out, "rule": rule}
    return {"slots": out, "rule": rule}


def book_appointment(patient_name: str, category: str, start_time: str,
                     provider_id: int, operatory_id: int | None = None) -> dict:
    """Book after re-checking every rule; writes the OOP quote into the note."""
    info = find_patient(patient_name)
    if "error" in info:
        return info
    rule = category_rule(category)
    if not rule.get("offered"):
        return {"refused": True, "reason": rule["policy"]}
    start = datetime.fromisoformat(start_time)
    if rule["must_start_before"] and start.hour >= CUTOFF_HOUR:
        return {"refused": True,
                "reason": f"{category} must start before {CUTOFF['time']} - pick a morning/early-afternoon slot"}
    if info["coverage_status"].startswith(("NO INSURANCE", "INACTIVE")) and category != "consultation":
        return {"refused": True,
                "reason": "patient has no active coverage - book a consultation first (office policy)"}
    pid = NEX_STATE["patients"].get(info["patient"])
    if not pid:
        return {"error": "patient not yet created in NexHealth - run nexhealth_scheduler.py"}
    oop = float(info["patient_out_of_pocket_estimate"])
    fee = float(info["target_treatment_fee"])
    ins = float(info["insurance_pays_estimate"])
    note = (f"Est pt OOP ${oop:.0f} of ${fee:.0f} tx; ins ~${ins:.0f}. "
            f"Booked via scheduling assistant.")[:128]
    body = {"appt": {"patient_id": pid, "provider_id": provider_id,
                     "start_time": start.isoformat(),
                     "end_time": (start + timedelta(minutes=rule["minutes"])).isoformat(),
                     "appointment_type_id": NEX_STATE["appointment_types"][rule["appointment_type"]],
                     "note": note}}
    if operatory_id:
        body["appt"]["operatory_id"] = operatory_id
    r = nex._req("POST", "/appointments", {"notify_patient": "false"}, body)
    appt = r["data"].get("appt", r["data"])
    return {"booked": True, "appointment_id": appt.get("id"),
            "start": start.isoformat(), "minutes": rule["minutes"],
            "note_on_appointment": note}


TOOLS = [
    {"type": "function", "function": {
        "name": "find_patient",
        "description": "Look up a campaign patient: verified insurance coverage, per-procedure "
                       "out-of-pocket estimate, and current booking status.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "category_rule",
        "description": "Office scheduling policy for a treatment category (duration, afternoon "
                       "cutoff, whether the practice performs it at all).",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string"}}, "required": ["category"]}}},
    {"type": "function", "function": {
        "name": "available_slots",
        "description": "Open NexHealth slots for a category, already filtered by office rules.",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string"},
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "days": {"type": "integer", "default": 5}},
            "required": ["category", "start_date"]}}},
    {"type": "function", "function": {
        "name": "book_appointment",
        "description": "Book a rule-checked appointment; the OOP quote is written to the "
                       "appointment note. Confirm slot with the user before calling.",
        "parameters": {"type": "object", "properties": {
            "patient_name": {"type": "string"}, "category": {"type": "string"},
            "start_time": {"type": "string", "description": "ISO datetime from available_slots"},
            "provider_id": {"type": "integer"}, "operatory_id": {"type": "integer"}},
            "required": ["patient_name", "category", "start_time", "provider_id"]}}},
]
TOOL_FNS = {"find_patient": find_patient, "category_rule": category_rule,
            "available_slots": available_slots, "book_appointment": book_appointment}

SYSTEM_PROMPT = f"""You are the scheduling assistant for {RULES['practice']['name']}
({RULES['practice']['timezone']}). Today is 2026-07-19 (Sunday); the office is open
{RULES['practice']['open']}-{RULES['practice']['close']} {', '.join(RULES['practice']['days'])}.

Office rules (from scheduling_rules.yaml - never violate them):
- Not offered: {json.dumps(RULES['not_offered'])}. Offer a referral instead.
- {', '.join(CUTOFF['applies_to'])} must START before {CUTOFF['time']}.
- Durations: {json.dumps({k: v['minutes'] for k, v in RULES['appointments'].items()})} minutes.
- Patients with inactive/no insurance get a 30-min consultation first, not treatment.

When a patient asks "how much will I pay", use find_patient and quote the
patient_out_of_pocket_estimate, explaining per-procedure numbers and their
confidence (per-code = payer-stated; category/conservative = estimate; suggest a
pre-determination for big category-confidence items). Amounts are estimates from
live eligibility checks, not guarantees. Always confirm a specific slot with the
user before booking. Be concise and warm."""


# ----------------------------------------------------------------------------- chat
SESSIONS: dict[str, list] = {}


class ChatIn(BaseModel):
    session_id: str = "default"
    message: str


@app.post("/api/chat")
def chat(inp: ChatIn):
    from openai import OpenAI
    client = OpenAI()
    history = SESSIONS.setdefault(inp.session_id, [{"role": "system", "content": SYSTEM_PROMPT}])
    history.append({"role": "user", "content": inp.message})
    for _ in range(8):  # tool loop
        resp = client.chat.completions.create(
            model=OPENAI_MODEL, messages=history, tools=TOOLS)
        msg = resp.choices[0].message
        history.append({"role": "assistant", "content": msg.content,
                        "tool_calls": [tc.model_dump() for tc in (msg.tool_calls or [])] or None})
        if not msg.tool_calls:
            return {"reply": msg.content}
        for tc in msg.tool_calls:
            try:
                result = TOOL_FNS[tc.function.name](**json.loads(tc.function.arguments))
            except Exception as e:
                result = {"error": str(e)[:300]}
            history.append({"role": "tool", "tool_call_id": tc.id,
                            "content": json.dumps(result)})
    return {"reply": "Sorry - I couldn't finish that request. Please try again."}


@app.get("/api/patients")
def patients():
    return [{"name": k,
             "category": r["treatment_category"],
             "coverage": r["coverage_status"],
             "oop": r["est_patient_out_of_pocket"],
             "booking": BOOKINGS.get(k, {}).get("status", "")}
            for k, r in REPORT.items()]


@app.get("/api/rules")
def rules():
    return RULES


# serve the built React frontend (webapp/frontend/dist) so one port hosts everything
_dist = os.path.join(os.path.dirname(BACKEND_DIR), "frontend", "dist")
if os.path.isdir(_dist):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
