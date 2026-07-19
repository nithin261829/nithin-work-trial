#!/usr/bin/env python3
"""
NexHealth scheduling integration for the outreach campaign.

Takes the targeted coverage results (targeted_patient_coverage_report.csv +
targeted_procedure_detail.csv, produced by coverage_pipeline.py) and, for every
patient, books the campaign treatment into the NexHealth sandbox practice while
writing the out-of-pocket estimate where the front desk can see it.

Office scheduling rules (encoded in RULES):
  Implants ............ not performed here -> no appointment, referral alert
  Bridge work ......... 120 min   } not after 3pm
  Crowns .............. 90 min    }
  Root canals ......... 120 min   }
  Veneers ............. 90 min    }
  Extractions ......... 60 min      any time
  Fillings ............ 60 min      any time
  Dentures/partials ... 30 min      (exam / post-op / impressions visit)
  Sealants/space maint. 30 min      any time
  Consultation ........ 30 min      (used for patients with no active coverage)

Where the money information lands in NexHealth:
  1. appointment.note (128 chars, syncs into the EHR schedule) - one-line OOP quote
  2. patient alert (POST /patients/{id}/alerts) - full coverage breakdown; alerts
     surface on the patient chart, which is what staff see when a patient calls
     asking "how much do I owe?"

Usage:
  export NEXHEALTH_API_KEY=...
  python3 nexhealth_scheduler.py [--dry-run]

Idempotent: created patients/appointments/alerts are recorded in
nexhealth_cache/state.json and reused on rerun.
"""

import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
API_KEY = os.environ.get("NEXHEALTH_API_KEY", "")
BASE = "https://nexhealth.info"
SUBDOMAIN = "10xdental-demo-practice"
LOCATION_ID = 340195
DRY_RUN = "--dry-run" in sys.argv
CACHE_DIR = os.path.join(DATA_DIR, "nexhealth_cache")
STATE_PATH = os.path.join(CACHE_DIR, "state.json")
START_DATE = os.environ.get("SCHEDULE_START_DATE", "2026-07-20")  # first day to offer
SEARCH_DAYS = 10

# category -> (appointment type name, minutes, must_start_before_3pm)
# None means the practice does not perform this treatment.
RULES = {
    "implant": None,                                # we don't do them here
    "bridge": ("Bridge Work", 120, True),
    "crown": ("Crown", 90, True),
    "root_canal": ("Root Canal (120 min)", 120, True),
    "veneer": ("Veneer", 90, True),
    "extraction": ("Extraction (60 min)", 60, False),
    "filling": ("Filling (60 min)", 60, False),
    "denture": ("Denture/Partial Visit", 30, False),  # exam/post-op/impressions
    "sealant": ("Sealant/Space Maintainer", 30, False),
    "consultation": ("Consultation", 30, False),
}
# patients.csv stores names as they appear in the PMS; these are surname-first
NAME_ORDER_FIX = {"El-khatib Suzy": ("Suzy", "El-khatib"),
                  "Gonzales Jesse": ("Jesse", "Gonzales")}


# ----------------------------------------------------------------------------- API client
class NexHealth:
    def __init__(self, api_key):
        self.token = None
        self.api_key = api_key

    def _req(self, method, path, params=None, body=None, auth=True):
        params = {"subdomain": SUBDOMAIN, "location_id": LOCATION_ID, **(params or {})}
        if path == "/authenticates" or "lids[]" in params:
            params.pop("location_id", None)
        url = f"{BASE}{path}?{urllib.parse.urlencode(params, doseq=True)}"
        headers = {
            "Nex-Api-Version": "v20240412",
            "Accept": "application/json",
            "User-Agent": "coverage-pipeline/1.0",
        }
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            headers["Authorization"] = self.api_key
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data, headers, method=method)
        try:
            resp = json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            try:
                resp = json.loads(e.read())
            except Exception:
                raise RuntimeError(f"{method} {path}: HTTP {e.code}")
        if resp.get("error"):
            raise RuntimeError(f"{method} {path}: {resp['error']}")
        return resp

    def login(self):
        r = self._req("POST", "/authenticates", auth=False)
        self.token = r["data"]["token"]

    def get(self, path, **params):
        return self._req("GET", path, params)

    def post(self, path, body, **params):
        return self._req("POST", path, params, body)


# ----------------------------------------------------------------------------- helpers
def load_state():
    os.makedirs(CACHE_DIR, exist_ok=True)
    if os.path.exists(STATE_PATH):
        return json.load(open(STATE_PATH))
    return {"patients": {}, "appointments": {}, "alerts": {}, "appointment_types": {},
            "working_hours_created": False}


def save_state(state):
    json.dump(state, open(STATE_PATH, "w"), indent=1)


def synth_email(name):
    slug = re.sub(r"[^a-z]+", ".", name.lower()).strip(".")
    return f"{slug}.trial@example.com"


def name_parts(name):
    if name in NAME_ORDER_FIX:
        return NAME_ORDER_FIX[name]
    parts = name.split()
    return parts[0], " ".join(parts[1:])


def ensure_appointment_types(nx, state):
    """Create any appointment types the office rules need that don't exist."""
    existing = {a["name"]: a for a in nx.get("/appointment_types", per_page=100)["data"]}
    for rule in RULES.values():
        if rule is None:
            continue
        name, minutes, _ = rule
        if name in existing:
            state["appointment_types"][name] = existing[name]["id"]
            continue
        if DRY_RUN:
            print(f"  [dry-run] would create appointment type: {name} ({minutes} min)")
            continue
        r = nx.post("/appointment_types",
                    {"location_id": LOCATION_ID,
                     "appointment_type": {"name": name, "minutes": minutes,
                                          "bookable_online": True}})
        state["appointment_types"][name] = r["data"]["id"]
        print(f"  created appointment type: {name} ({minutes} min)")
    save_state(state)


def ensure_working_hours(nx, state, providers, operatories):
    """Sandbox providers have no schedules; give each provider weekday 8-5 in an
    operatory so available_slots has something to offer."""
    if state.get("working_hours_created"):
        return
    type_ids = list(state["appointment_types"].values())
    for prov, op in zip(providers, operatories):
        if DRY_RUN:
            print(f"  [dry-run] would add Mon-Fri 08:00-17:00 for {prov['name']} in {op['name']}")
            continue
        nx.post("/working_hours",
                {"location_id": LOCATION_ID,
                 "working_hour": {"provider_id": prov["id"], "operatory_id": op["id"],
                                  "begin_time": "08:00", "end_time": "17:00",
                                  "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
                                  "appointment_type_ids": type_ids}})
        print(f"  working hours: {prov['name']} Mon-Fri 08:00-17:00 in {op['name']}")
    if not DRY_RUN:
        state["working_hours_created"] = True
        save_state(state)


def ensure_patient(nx, state, row, provider_id):
    """Create (or reuse) the NexHealth patient for a report row."""
    name = row["patient_name"]
    if name in state["patients"]:
        return state["patients"][name]
    first, last = name_parts(name)
    phone = re.sub(r"\D", "", row.get("phone", "")) or "5555550100"
    body = {
        "provider": {"provider_id": provider_id},
        "patient": {"first_name": first, "last_name": last,
                    "email": synth_email(name),
                    "bio": {"date_of_birth": row["dob"],
                            "phone_number": phone[-10:]}},
    }
    if DRY_RUN:
        print(f"  [dry-run] would create patient {first} {last} (dob {row['dob']})")
        return None
    r = nx.post("/patients", body, location_id=LOCATION_ID)
    pid = r["data"]["user"]["id"] if "user" in r.get("data", {}) else r["data"]["id"]
    state["patients"][name] = pid
    save_state(state)
    print(f"  created patient {first} {last} -> id {pid}")
    return pid


def find_slot(nx, appt_type_id, minutes, before_3pm, booked):
    """First open slot honoring the office rules and prior bookings this run."""
    r = nx.get("/available_slots", **{"lids[]": LOCATION_ID, "start_date": START_DATE,
                                      "days": SEARCH_DAYS, "slot_length": minutes,
                                      "appointment_type_id": appt_type_id})
    for loc in r.get("data", []):
        pid = loc.get("pid")
        for slot in loc.get("slots", []):
            t = slot["time"]
            start = datetime.fromisoformat(t)
            if before_3pm and start.hour >= 15:
                continue
            key = (pid, t)
            if key in booked:
                continue
            booked.add(key)
            return {"provider_id": pid, "operatory_id": slot.get("operatory_id"),
                    "start": start, "end": start + timedelta(minutes=minutes)}
    return None


def build_notes(row, detail_rows):
    """(appointment note <=128 chars, full patient-alert text)."""
    oop = float(row["est_patient_out_of_pocket"])
    ins = float(row["est_insurance_pays"])
    fee = float(row["pending_total_fee"])
    note = (f"Est pt OOP ${oop:.0f} of ${fee:.0f} tx; ins ~${ins:.0f}. "
            f"Elig verified 2026-07-19. See pt alert for breakdown.")[:128]

    lines = [f"INSURANCE/OOP SUMMARY (verified 2026-07-19 via Stedi eligibility)",
             f"Carrier: {row['carrier'] or 'NONE'} - {row['coverage_status']}"]
    if row["deductible_remaining"]:
        lines.append(f"Deductible remaining: ${row['deductible_remaining']} | "
                     f"Annual max remaining: ${row['annual_max_remaining']}")
    lines.append(f"Campaign treatment total ${fee:.2f}: insurance ~${ins:.2f}, "
                 f"PATIENT ~${oop:.2f}")
    for d in detail_rows:
        lines.append(f"  {d['procedure_code']} {d['description'][:28]:28s} "
                     f"${float(d['fee']):7.2f} -> pt ${float(d['patient_oop_est']):7.2f}"
                     f" ({d['basis'][:40]}, {d['confidence']})")
    if row["notes"]:
        lines.append(f"Notes: {row['notes']}")
    return note, "\n".join(lines)


# ----------------------------------------------------------------------------- main
def main():
    if not API_KEY:
        sys.exit("Set NEXHEALTH_API_KEY in the environment")

    report = list(csv.DictReader(open(os.path.join(DATA_DIR, "targeted_patient_coverage_report.csv"))))
    details = list(csv.DictReader(open(os.path.join(DATA_DIR, "targeted_procedure_detail.csv"))))
    contacts = {r["patient_name"]: r for r in
                csv.DictReader(open(os.path.join(DATA_DIR, "01_patients", "patients.csv")))}
    for row in report:
        row["phone"] = contacts.get(row["patient_name"], {}).get("phone", "")
    det_by_pat = {}
    for d in details:
        det_by_pat.setdefault(d["patient"], []).append(d)

    nx = NexHealth(API_KEY)
    nx.login()
    state = load_state()

    providers = nx.get("/providers", location_id=LOCATION_ID, per_page=50)["data"]
    operatories = nx.get("/operatories", location_id=LOCATION_ID, per_page=50)["data"]
    print(f"sandbox: {len(providers)} providers, {len(operatories)} operatories")

    print("ensuring appointment types...")
    ensure_appointment_types(nx, state)
    print("ensuring working hours...")
    ensure_working_hours(nx, state, providers[:len(operatories)], operatories)

    booked = set()
    results = []
    for row in report:
        name = row["patient_name"]
        category = row["treatment_category"]
        no_coverage = row["coverage_status"].startswith(("NO INSURANCE", "INACTIVE"))
        rule = RULES.get("consultation") if no_coverage else RULES.get(category)
        note, alert_text = build_notes(row, det_by_pat.get(name, []))
        outcome = {"patient": name, "category": category, "oop": row["est_patient_out_of_pocket"]}

        default_provider = providers[0]["id"]
        pid = ensure_patient(nx, state, row, default_provider)

        # every patient gets the coverage alert on their chart; alerts sync to
        # the EHR, so a sandbox without a connected data source rejects them -
        # fall back to keeping the info in the appointment note only
        if pid and name not in state["alerts"] and not DRY_RUN:
            if RULES.get(category) is None:
                alert_text = ("REFERRAL NEEDED: practice does not place implants - "
                              "refer out for surgical phase.\n" + alert_text)
            try:
                r = nx.post(f"/patients/{pid}/alerts", {"patient_alert": {"note": alert_text}})
                state["alerts"][name] = r["data"].get("id")
            except RuntimeError as e:
                state["alerts"][name] = f"unavailable ({e})"[:120]
            save_state(state)

        if rule is None:  # implants - not performed here
            outcome.update(status="NOT SCHEDULED - implants not performed here, referral alert added")
            results.append(outcome)
            print(f"{name}: implants not done here - alert only")
            continue

        type_name, minutes, before_3pm = rule
        if no_coverage:
            outcome["category"] = f"{category} -> consultation (no active coverage)"

        if name in state["appointments"]:
            outcome.update(status=f"already booked (appt {state['appointments'][name]})")
            results.append(outcome)
            continue
        if DRY_RUN:
            outcome.update(status=f"[dry-run] would book {type_name} {minutes}min"
                                  f"{' before 3pm' if before_3pm else ''}")
            results.append(outcome)
            print(f"{name}: {outcome['status']}")
            continue

        slot = find_slot(nx, state["appointment_types"][type_name], minutes, before_3pm, booked)
        if not slot:
            outcome.update(status="NO SLOT FOUND")
            results.append(outcome)
            print(f"{name}: no slot found")
            continue

        body = {"appt": {"patient_id": pid, "provider_id": slot["provider_id"],
                         "start_time": slot["start"].isoformat(),
                         "end_time": slot["end"].isoformat(),
                         "appointment_type_id": state["appointment_types"][type_name],
                         "note": note}}
        if slot.get("operatory_id"):
            body["appt"]["operatory_id"] = slot["operatory_id"]
        r = nx.post("/appointments", body, location_id=LOCATION_ID, notify_patient="false")
        appt_id = r["data"].get("appt", r["data"]).get("id")
        state["appointments"][name] = appt_id
        save_state(state)
        outcome.update(status="BOOKED", appointment_id=appt_id, type=type_name,
                       start=slot["start"].isoformat(), provider_id=slot["provider_id"])
        results.append(outcome)
        print(f"{name}: booked {type_name} {slot['start'].isoformat()} (appt {appt_id})")
        time.sleep(0.3)

    out = os.path.join(DATA_DIR, "nexhealth_booking_results.csv")
    fields = ["patient", "category", "oop", "status", "appointment_id", "type", "start", "provider_id"]
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k, "") for k in fields})
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
