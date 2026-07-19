"""All appointment handling - slots, booking, cancel, reschedule, live pulls.

Appointments live on the NexHealth PMS and are read/written LIVE. Office policy
comes from scheduling_rules.yaml. The sandbox practice has no downstream PMS
sync so NexHealth rejects PATCH; a local shadow ledger records those state
changes and get_appointments reflects them (production uses PATCH directly).
"""

import json
import os
from datetime import datetime, timedelta

import coverage_data as cov
import patient_data as pd
from nexhealth_client import CUTOFF, CUTOFF_HOUR, NEX_STATE, PRACTICE, REPO_DIR, RULES, nex

OPEN_HOUR = int(PRACTICE["open"].split(":")[0])
CLOSE_HOUR = int(PRACTICE["close"].split(":")[0])

OVERRIDES_PATH = os.path.join(REPO_DIR, "nexhealth_cache", "overrides.json")
OVERRIDES = json.load(open(OVERRIDES_PATH)) if os.path.exists(OVERRIDES_PATH) else {}


def _save_overrides():
    json.dump(OVERRIDES, open(OVERRIDES_PATH, "w"), indent=1)


# ----------------------------------------------------------------------------- rules
def category_rule(category: str) -> dict:
    """Office policy for a treatment category (duration, cutoff, offered?)."""
    if category in RULES["not_offered"]:
        return {"offered": False, "policy": RULES["not_offered"][category]}
    appt = RULES["appointments"].get(category)
    if not appt:
        return {"error": f"unknown category '{category}'", "known": list(RULES["appointments"])}
    return {"offered": True, "minutes": appt["minutes"],
            "appointment_type": appt["type_name"],
            "must_start_before": CUTOFF["time"] if category in CUTOFF["applies_to"] else None}


def _within_hours(start, minutes):
    end = start + timedelta(minutes=minutes)
    if start.weekday() >= 5:
        return "office is closed on weekends"
    if start.hour < OPEN_HOUR or end.hour > CLOSE_HOUR or (end.hour == CLOSE_HOUR and end.minute > 0):
        return f"must fit within office hours {PRACTICE['open']}-{PRACTICE['close']}"
    return None


# ----------------------------------------------------------------------------- read (live)
def get_appointments(patient_name: str) -> dict:
    """Pull a patient's appointments LIVE from the PMS, applying the ledger."""
    p = pd.resolve(patient_name)
    if not p:
        return {"error": f"no patient matching '{patient_name}' in the PMS"}
    r = nex.request("GET", "/appointments",
                    {"patient_id": p["id"], "start": "2026-01-01", "end": "2027-12-31",
                     "per_page": 25})
    appts = r["data"] if isinstance(r["data"], list) else r["data"].get("appointments", [])
    out = []
    for a in appts:
        ov = OVERRIDES.get(str(a.get("id")), {})
        out.append({"appointment_id": a.get("id"), "start_time": a.get("start_time"),
                    "end_time": a.get("end_time"), "note": a.get("note"),
                    "provider_id": a.get("provider_id"), "operatory_id": a.get("operatory_id"),
                    "confirmed": a.get("confirmed"),
                    "cancelled": a.get("cancelled") or ov.get("status") == "cancelled",
                    "superseded": ov.get("status") == "superseded"})
    return {"patient": pd._full_name(p), "appointments": out}


def _active_appt(patient_name):
    live = [a for a in get_appointments(patient_name).get("appointments", [])
            if not a.get("cancelled") and not a.get("superseded")]
    return live[0] if live else None


def _category_of(patient_key, minutes=None):
    type_name = cov.BOOKINGS.get(patient_key, {}).get("type")
    for cat, a in RULES["appointments"].items():
        if a["type_name"] == type_name:
            return cat
    if minutes:
        for cat, a in RULES["appointments"].items():
            if a["minutes"] == minutes:
                return cat
    return None


# ----------------------------------------------------------------------------- slots (live)
def available_slots(category: str, start_date: str, days: int = 5) -> dict:
    """Open PMS slots for a category, filtered by every office rule."""
    rule = category_rule(category)
    if not rule.get("offered"):
        return rule
    type_id = NEX_STATE["appointment_types"].get(rule["appointment_type"])
    r = nex.request("GET", "/available_slots",
                    {"lids[]": PRACTICE["location_id"], "start_date": start_date,
                     "days": days, "slot_length": rule["minutes"],
                     "appointment_type_id": type_id})
    out = []
    for loc in r.get("data", []):
        for slot in loc.get("slots", []):
            start = datetime.fromisoformat(slot["time"])
            if rule["must_start_before"] and start.hour >= CUTOFF_HOUR:
                continue
            out.append({"start": slot["time"], "provider_id": loc.get("pid"),
                        "operatory_id": slot.get("operatory_id"),
                        "label": start.strftime("%a %b %-d, %-I:%M %p"), "category": category})
            if len(out) >= 12:
                return {"slots": out, "rule": rule}
    return {"slots": out, "rule": rule}


# ----------------------------------------------------------------------------- write (live)
def book_appointment(patient_name, category, start_time, provider_id, operatory_id=None) -> dict:
    """Book after re-checking every rule; writes the OOP quote into the note."""
    info = pd.find_patient(patient_name)
    if "error" in info:
        return info
    rule = category_rule(category)
    if not rule.get("offered"):
        return {"refused": True, "reason": rule["policy"]}
    start = datetime.fromisoformat(start_time)
    hours_err = _within_hours(start, rule["minutes"])
    if hours_err:
        return {"refused": True, "reason": hours_err}
    if rule["must_start_before"] and start.hour >= CUTOFF_HOUR:
        return {"refused": True,
                "reason": f"{category} must start before {CUTOFF['time']} - pick an earlier slot"}
    if info.get("coverage_status", "").startswith(("NO INSURANCE", "INACTIVE")) and category != "consultation":
        return {"refused": True,
                "reason": "patient has no active coverage - book a consultation first (office policy)"}
    pid = info.get("pms_id")
    if not pid:
        return {"error": "patient not found in the PMS"}
    oop = float(info.get("patient_out_of_pocket_estimate", 0) or 0)
    fee = float(info.get("target_treatment_fee", 0) or 0)
    ins = float(info.get("insurance_pays_estimate", 0) or 0)
    note = (f"Est pt OOP ${oop:.0f} of ${fee:.0f} tx; ins ~${ins:.0f}. "
            f"Booked via scheduling assistant.")[:128]
    body = {"appt": {"patient_id": pid, "provider_id": provider_id,
                     "start_time": start.isoformat(),
                     "end_time": (start + timedelta(minutes=rule["minutes"])).isoformat(),
                     "appointment_type_id": NEX_STATE["appointment_types"][rule["appointment_type"]],
                     "note": note}}
    if operatory_id:
        body["appt"]["operatory_id"] = operatory_id
    r = nex.request("POST", "/appointments", {"notify_patient": "false"}, body)
    appt = r["data"].get("appt", r["data"])
    return {"booked": True, "appointment_id": appt.get("id"), "start": start.isoformat(),
            "minutes": rule["minutes"], "note_on_appointment": note}


def cancel_appointment(patient_name, confirm=False) -> dict:
    """Cancel the patient's upcoming appointment (confirm-gated)."""
    appt = _active_appt(patient_name)
    if not appt:
        return {"error": "no active appointment found for this patient"}
    if not confirm:
        return {"needs_confirmation": True, "appointment": appt,
                "message": "ask the user to confirm cancelling this appointment"}
    try:
        nex.request("PATCH", f"/appointments/{appt['appointment_id']}", None,
                    {"appt": {"cancelled": True}})
    except RuntimeError as e:
        if "not synced" not in str(e):
            raise
        OVERRIDES[str(appt["appointment_id"])] = {"status": "cancelled"}
        _save_overrides()
    return {"cancelled": True, "appointment_id": appt["appointment_id"], "was_at": appt["start_time"]}


def reschedule_appointment(patient_name, new_start_time) -> dict:
    """Move the upcoming appointment, re-checking every office rule."""
    info = pd.find_patient(patient_name)
    if "error" in info:
        return info
    appt = _active_appt(patient_name)
    if not appt:
        return {"error": "no active appointment found for this patient"}
    old_s = datetime.fromisoformat(appt["start_time"].replace("Z", "+00:00"))
    old_e = datetime.fromisoformat(appt["end_time"].replace("Z", "+00:00"))
    minutes = int((old_e - old_s).total_seconds() // 60)
    category = _category_of(info["patient"], minutes)
    start = datetime.fromisoformat(new_start_time)
    hours_err = _within_hours(start, minutes)
    if hours_err:
        return {"refused": True, "reason": hours_err}
    if category in CUTOFF["applies_to"] and start.hour >= CUTOFF_HOUR:
        return {"refused": True,
                "reason": f"{category} must start before {CUTOFF['time']} - offer an earlier slot"}
    try:
        nex.request("PATCH", f"/appointments/{appt['appointment_id']}", None,
                    {"appt": {"start_time": start.isoformat(),
                              "end_time": (start + timedelta(minutes=minutes)).isoformat()}})
        new_id = appt["appointment_id"]
    except RuntimeError as e:
        if "not synced" not in str(e):
            raise
        type_name = cov.BOOKINGS.get(info["patient"], {}).get("type") or \
            RULES["appointments"].get(category, {}).get("type_name")
        note = (f"RESCHEDULED from {appt['start_time'][:16]}. " + (appt.get("note") or ""))[:128]
        body = {"appt": {"patient_id": info["pms_id"], "provider_id": appt.get("provider_id"),
                         "start_time": start.isoformat(),
                         "end_time": (start + timedelta(minutes=minutes)).isoformat(),
                         "appointment_type_id": NEX_STATE["appointment_types"].get(type_name),
                         "note": note}}
        if appt.get("operatory_id"):
            body["appt"]["operatory_id"] = appt["operatory_id"]
        r = nex.request("POST", "/appointments", {"notify_patient": "false"}, body)
        new_id = r["data"].get("appt", r["data"]).get("id")
        OVERRIDES[str(appt["appointment_id"])] = {"status": "superseded", "by": new_id}
        _save_overrides()
    return {"rescheduled": True, "appointment_id": new_id, "from": appt["start_time"],
            "to": start.isoformat(), "minutes": minutes, "category": category}
