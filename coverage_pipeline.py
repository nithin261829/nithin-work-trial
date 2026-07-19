#!/usr/bin/env python3
"""
Dental insurance coverage pipeline.

For every patient in the work-trial dataset:
  1. StediAgent   - real-time eligibility check (X12 270/271) via the Stedi API:
                    active/inactive, deductible remaining, annual max remaining,
                    coinsurance per dental category, per-CDT-code copay schedules.
  2. WebAgent     - OpenAI SDK fallback when the payer returns nothing (e.g. Aetna
                    without an enrolled NPI): an LLM with the web-search tool looks
                    up typical coverage for the carrier/plan and returns JSON.
                    A built-in typical-coverage table is the last resort.
  3. Calculator   - per pending procedure: insurance pays vs patient out-of-pocket,
                    applying deductible and remaining annual maximum.

Outputs:
  final_patient_coverage_report.csv  (one row per patient)
  final_procedure_detail.csv         (one row per pending procedure)

Usage:
  export STEDI_API_KEY=...
  export OPENAI_API_KEY=...
  export PROVIDER_NPI=...        # optional: real practice NPI (needed for Aetna)
  export CACHE_TTL_DAYS=7        # optional: re-verify eligibility older than N days (default 7)
  python3 coverage_pipeline.py [--live]

  --live   ignore the cache entirely and re-check every patient against the payer
"""

import csv
import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict

# ----------------------------------------------------------------------------- config
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
STEDI_API_KEY = os.environ.get("STEDI_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
PROVIDER_NPI = os.environ.get("PROVIDER_NPI", "1999999984")  # Stedi test NPI; use the real practice NPI for Aetna
PROVIDER_NAME = os.environ.get("PROVIDER_NAME", "Dental Practice")
CACHE_TTL_DAYS = float(os.environ.get("CACHE_TTL_DAYS", "7"))  # eligibility older than this is re-verified
FORCE_LIVE = "--live" in sys.argv                              # bypass the cache entirely
CACHE_DIR = os.path.join(DATA_DIR, "stedi_cache")
STEDI_URL = "https://healthcare.us.stedi.com/2024-04-01/change/medicalnetwork/eligibility/v3"

# X12 service type codes for dental categories
STC_NAMES = {
    "23": "diagnostic", "41": "preventive", "25": "restorative(basic)",
    "36": "crowns(major)", "26": "endodontics", "24": "periodontics",
    "39": "prosthodontics", "40": "oral surgery", "38": "orthodontics",
    "28": "adjunctive", "35": "dental care",
}


def cdt_to_stc(code):
    """Map a CDT procedure code (D####) to an X12 dental service type code."""
    if not re.fullmatch(r"D\d{4}", code or ""):
        return None  # house/lab codes (ESSIX, 9995, ...) - not billable to insurance
    n = int(code[1:])
    if n < 1000: return "23"   # D0xxx diagnostic
    if n < 2000: return "41"   # D1xxx preventive
    if n < 2500: return "25"   # D2140-D2499 fillings
    if n < 3000: return "36"   # D25xx-D29xx inlays/crowns
    if n < 4000: return "26"   # D3xxx endo
    if n < 5000: return "24"   # D4xxx perio
    if n < 7000: return "39"   # D5xxx dentures, D6xxx implants/bridges
    if n < 8000: return "40"   # D7xxx oral surgery
    if n < 9000: return "38"   # D8xxx ortho
    return "28"                # D9xxx adjunctive


# ----------------------------------------------------------------------------- data loading
def load_dataset():
    """Read the practice CSVs into one list of per-patient dicts."""
    def read(path):
        with open(os.path.join(DATA_DIR, path), newline="") as f:
            return list(csv.DictReader(f))

    patients = read("01_patients/patients.csv")
    procedures = read("03_procedures_and_treatment_plans/procedures.csv")
    insurance = read("04_insurance/insurance.csv")

    procs_by_patient = defaultdict(lambda: {"completed": [], "pending": []})
    for p in procedures:
        raw = json.loads(p["raw"]) if p["raw"] else {}
        fee = p["fee_amount"] or raw.get("billed_amount") or ""
        entry = {
            "code": p["procedure_code"],
            "desc": p["description"] or raw.get("description", ""),
            "fee": float(fee) if fee else 0.0,
            "date": p["date"] or raw.get("procedure_time", "")[:10],
            "tooth": raw.get("tooth_number", ""),
        }
        key = "completed" if p["state"] == "COMPLETED" else "pending"
        procs_by_patient[p["patient_id"]][key].append(entry)

    ins_by_patient = defaultdict(list)
    for i in insurance:
        if not (i["carrier_title"] or i["carrier_payer_id"]):
            continue  # empty shell rows -> effectively uninsured
        ins_by_patient[i["patient_id"]].append({
            "carrier": i["carrier_title"],
            "payer_id": i["carrier_payer_id"],
            "member_id": (i["member_id"] or i["subscriber_reference_id"]).strip(),
            "priority": i["priority"],
        })

    out = []
    for pt in patients:
        pid = pt["patient_id"]
        d = procs_by_patient[pid]
        out.append({
            "name": pt["patient_name"],
            "dob": pt["birth_date"],
            "category": pt["primary_category"],
            "status": pt["status"],
            "insurance": sorted(ins_by_patient.get(pid, []), key=lambda x: x["priority"])[:1],
            "completed": sorted(d["completed"], key=lambda x: x["date"] or "", reverse=True),
            "pending": d["pending"],
        })
    return out


# ----------------------------------------------------------------------------- Stedi agent
class StediAgent:
    """Runs 270/271 eligibility checks against the Stedi clearinghouse API."""

    def __init__(self, api_key, npi, org_name):
        self.api_key = api_key
        self.npi = npi
        self.org_name = org_name
        os.makedirs(CACHE_DIR, exist_ok=True)

    def _post(self, body):
        req = urllib.request.Request(
            STEDI_URL, json.dumps(body).encode(),
            {"Authorization": "Key " + self.api_key, "Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=90))

    @staticmethod
    def cache_is_fresh(path):
        """A cached 271 is usable only if it exists, is younger than CACHE_TTL_DAYS,
        and --live was not passed. Benefits drift (deductibles get consumed, plans
        terminate, Jan 1 resets), so eligibility should be re-verified regularly."""
        if FORCE_LIVE or not os.path.exists(path):
            return False
        age_days = (time.time() - os.path.getmtime(path)) / 86400
        return age_days <= CACHE_TTL_DAYS

    def check(self, patient):
        """Return the raw 271 JSON for a patient (cached on disk to avoid re-billing)."""
        cache = os.path.join(CACHE_DIR, re.sub(r"\W+", "_", patient["name"]) + ".json")
        if self.cache_is_fresh(cache):
            return json.load(open(cache))
        if os.path.exists(cache):
            reason = "--live" if FORCE_LIVE else f"cache older than {CACHE_TTL_DAYS:g} days"
            print(f"    re-checking {patient['name']} live ({reason})")

        ins = patient["insurance"][0]
        member_id = re.sub(r"\s+\d+$", "", ins["member_id"]).replace("-00", "").strip()
        parts = patient["name"].split()
        body = {
            "controlNumber": "123456789",
            "tradingPartnerServiceId": ins["payer_id"],
            "provider": {"organizationName": self.org_name, "npi": self.npi},
            "subscriber": {
                "firstName": parts[0], "lastName": " ".join(parts[1:]),
                "dateOfBirth": patient["dob"].replace("-", ""), "memberId": member_id,
            },
            "encounter": {"serviceTypeCodes": ["35"]},
        }
        try:
            resp = self._post(body)
        except Exception as e:
            return {"errors": [{"code": "HTTP", "description": str(e)[:200]}]}

        # some names are stored "Last First" - retry swapped if the payer found nobody
        if resp.get("errors") and not resp.get("benefitsInformation"):
            body["subscriber"]["firstName"] = parts[-1]
            body["subscriber"]["lastName"] = " ".join(parts[:-1])
            try:
                retry = self._post(body)
                if retry.get("benefitsInformation"):
                    resp = retry
            except Exception:
                pass

        json.dump(resp, open(cache, "w"), indent=1)
        time.sleep(0.4)
        return resp

    @staticmethod
    def parse(resp):
        """Extract usable benefit terms from a 271 response."""
        coins, copays, percode = {}, {}, {}
        ded_cal = ded_rem = max_cal = max_rem = None
        active = inactive = False

        for b in resp.get("benefitsInformation", []):
            if b.get("inPlanNetworkIndicatorCode") == "N":
                continue  # out-of-network rows
            code = b.get("code")
            stcs = b.get("serviceTypeCodes") or [""]
            tq = b.get("timeQualifier", "")
            lvl = b.get("coverageLevelCode", "")
            amt = b.get("benefitAmount")
            pct = b.get("benefitPercent")

            if code == "1": active = True
            if code == "6": inactive = True

            # per-CDT-code schedules (Humana coinsurance, Cigna DHMO copays) live in
            # the additionalInformation description text
            txt = " ".join(a.get("description", "") for a in (b.get("additionalInformation") or []))
            cdt_codes = re.findall(r"\bD\d{4}\b", txt)
            for c in cdt_codes:
                if code == "A" and pct is not None:
                    percode.setdefault(c, {})["coins"] = float(pct)
                if code == "B" and amt not in (None, ""):
                    percode.setdefault(c, {})["copay"] = float(amt)
            if cdt_codes:
                continue  # already captured at per-code granularity

            if code == "A" and pct is not None:
                for s in stcs:
                    coins.setdefault(s, []).append(float(pct))
            if code == "B" and amt not in (None, ""):
                for s in stcs:
                    copays.setdefault(s, []).append(float(amt))
            if code == "C" and amt not in (None, "") and lvl in ("IND", ""):
                if any(s in ("35", "30", "") for s in stcs):
                    a = float(amt)
                    if tq == "Remaining":
                        ded_rem = a if ded_rem is None else min(ded_rem, a)
                    elif tq == "Calendar Year":
                        ded_cal = a if ded_cal is None else max(ded_cal, a)
            if code == "F" and amt not in (None, "") and lvl in ("IND", ""):
                if any(s in ("35", "30") for s in stcs) and float(amt) > 0:
                    a = float(amt)
                    if "Remaining" in tq and "Lifetime" not in tq:
                        max_rem = a if max_rem is None else max(max_rem, a)
                    elif tq == "Calendar Year":
                        max_cal = a if max_cal is None else max(max_cal, a)

        return {
            "active": active and not inactive,
            "inactive": inactive,
            "coins": {s: min(v) for s, v in coins.items()},       # min = best in-network share
            "copays": {s: min(v) for s, v in copays.items()},
            "percode": percode,
            "deductible": ded_rem if ded_rem is not None else ded_cal,
            "annual_max": max_cal,
            "annual_max_remaining": max_rem if max_rem is not None else max_cal,
        }


# ----------------------------------------------------------------------------- OpenAI web agent
class WebAgent:
    """Fallback agent built on the OpenAI SDK: when the payer API can't answer,
    an LLM with the web-search tool researches typical coverage for the
    carrier/plan and returns structured JSON."""

    # industry-typical structures used when the LLM/web search is unavailable
    TYPICAL = {
        "ppo":      {"preventive": 100, "basic": 80, "major": 50, "deductible": 50, "annual_max": 1250},
        "medicare": {"preventive": 100, "basic": 50, "major": 50, "deductible": 0, "annual_max": 1000},
    }

    def __init__(self, api_key, model):
        self.model = model
        self.cache = {}
        self.client = None
        if api_key:
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=api_key)
            except ImportError:
                print("openai package not installed (pip install openai); "
                      "using typical-plan table", file=sys.stderr)

    def _ask_llm(self, carrier, plan_hint):
        prompt = (
            f"Search the web for how the dental insurance plan below typically covers "
            f"treatment, then answer with ONLY a JSON object (no prose):\n"
            f'  carrier: "{carrier}"  plan name hint: "{plan_hint}"\n'
            "JSON keys:\n"
            "  preventive_pct  - % of cost the plan pays for preventive/diagnostic (D0/D1)\n"
            "  basic_pct       - % paid for basic restorative (fillings, endo, perio, extractions)\n"
            "  major_pct       - % paid for major work (crowns, dentures, bridges, implants)\n"
            "  deductible      - typical individual annual deductible in USD\n"
            "  annual_max      - typical individual annual maximum in USD (if the plan name "
            "contains a number like 3000, that is usually the annual max/allowance)\n"
            "Use typical published values for this carrier's dental plans. Numbers only."
        )
        resp = self.client.responses.create(
            model=self.model,
            tools=[{"type": "web_search_preview"}],
            input=prompt,
        )
        text = resp.output_text
        m = re.search(r"\{[^{}]*\}", text, re.S)
        return json.loads(m.group(0)) if m else None

    def coverage_for(self, carrier, plan_hint=""):
        """Return (patient_shares_by_stc, deductible, annual_max, source_note)."""
        key = (carrier or "") + "|" + (plan_hint or "")
        if key in self.cache:
            return self.cache[key]

        is_medicare = "medicare" in key.lower()
        plan = dict(self.TYPICAL["medicare" if is_medicare else "ppo"])
        plan = {"preventive_pct": plan["preventive"], "basic_pct": plan["basic"],
                "major_pct": plan["major"], "deductible": plan["deductible"],
                "annual_max": plan["annual_max"]}
        source = "typical-plan table"

        if self.client:
            try:
                got = self._ask_llm(carrier, plan_hint)
                if got:
                    plan.update({k: float(v) for k, v in got.items() if v is not None})
                    source = f"openai web-search agent ({self.model})"
            except Exception as e:
                print(f"    openai agent failed ({e}); using typical-plan table", file=sys.stderr)

        # plan names like "Aetna Medicare 3000" carry the annual allowance
        m = re.search(r"\b([1-9]\d{3})\b", plan_hint)
        if m:
            plan["annual_max"] = float(m.group(1))
            source += f" + plan name '{plan_hint.strip()}'"

        prev = 1 - plan["preventive_pct"] / 100
        basic = 1 - plan["basic_pct"] / 100
        major = 1 - plan["major_pct"] / 100
        shares = {"23": prev, "41": prev,
                  "25": basic, "26": basic, "24": basic, "40": basic, "28": basic,
                  "36": major, "39": major, "38": 1.0}
        result = (shares, plan["deductible"], plan["annual_max"], source)
        self.cache[key] = result
        return result


# ----------------------------------------------------------------------------- calculator
def estimate_patient(patient, benefits, fallback):
    """Compute (insurance_pays, patient_oop, detail_rows, notes) for one patient."""
    detail, notes = [], []
    ins_total = oop_total = 0.0

    if benefits:
        coins = benefits["coins"] or None
        percode = benefits["percode"]
        ded_left = benefits["deductible"] or 0
        max_left = benefits["annual_max_remaining"]
    else:
        coins, percode, ded_left, max_left = None, {}, 0, 0
    if fallback:
        coins, ded_left, max_left = fallback[0], fallback[1], fallback[2]
        percode = {}
    if max_left is None:
        max_left = float("inf")
        notes.append("annual max not reported by payer - uncapped estimate")

    for proc in patient["pending"]:
        fee, code = proc["fee"], proc["code"]
        stc = cdt_to_stc(code)
        pat_amt, basis = None, ""

        if stc is None:
            pat_amt, basis = fee, "house/lab code - not billable to insurance"
        elif code in percode:
            e = percode[code]
            if "coins" in e:
                pat_amt = round(fee * e["coins"], 2)
                basis = f"per-code coinsurance {e['coins']:.0%} patient share"
            else:
                pat_amt = min(e["copay"], fee) if fee else e["copay"]
                basis = f"per-code copay ${e['copay']:.0f}"
        elif coins:
            share = coins.get(stc, coins.get("35"))
            if share is not None:
                ded_use = min(ded_left, fee) if share < 1 else 0
                covered = max(fee - ded_use, 0) * (1 - share)
                pat_amt = round(fee - covered, 2)
                ded_left -= ded_use
                basis = f"{STC_NAMES.get(stc, stc)} coinsurance {share:.0%} patient share"

        if pat_amt is None:
            pat_amt, basis = fee, "no benefit info - assume full fee"

        ins_pay = max(fee - pat_amt, 0)
        if ins_pay > max_left:  # annual maximum cap
            pat_amt = round(fee - max_left, 2)
            ins_pay = max_left
            basis += " (annual max reached)"
        max_left -= ins_pay

        ins_total += ins_pay
        oop_total += pat_amt
        detail.append({
            "patient": patient["name"], "procedure_code": code, "description": proc["desc"],
            "tooth": proc["tooth"], "fee": f"{fee:.2f}",
            "insurance_pays_est": f"{ins_pay:.2f}", "patient_oop_est": f"{pat_amt:.2f}",
            "basis": basis,
        })
    return ins_total, oop_total, detail, notes


# ----------------------------------------------------------------------------- main
def main():
    if not STEDI_API_KEY:
        sys.exit("Set STEDI_API_KEY in the environment")

    stedi = StediAgent(STEDI_API_KEY, PROVIDER_NPI, PROVIDER_NAME)
    web = WebAgent(OPENAI_API_KEY, OPENAI_MODEL)
    patients = load_dataset()
    rows, all_detail = [], []

    for p in patients:
        ins = p["insurance"][0] if p["insurance"] else None
        benefits = fallback = None
        notes = []

        if not ins:
            covstat, source = "NO INSURANCE", "none"
            notes.append("uninsured - full fee; consider in-house discount plan")
        else:
            print(f"checking {p['name']} ({ins['carrier']}) ...")
            resp = stedi.check(p)
            parsed = StediAgent.parse(resp)
            if parsed["inactive"]:
                covstat, source = "INACTIVE (terminated per Stedi 271)", "stedi_live"
                notes.append("coverage inactive - full fee unless new insurance obtained")
            elif parsed["active"] and (parsed["coins"] or parsed["percode"] or parsed["copays"]):
                covstat, source, benefits = "ACTIVE (verified via Stedi)", "stedi_live", parsed
                if parsed["percode"]:
                    source += " (per-procedure schedule)"
            else:
                # payer refused (e.g. Aetna without enrolled NPI) -> OpenAI web fallback
                errs = "; ".join(e.get("description", "")[:60] for e in resp.get("errors", []))
                shares, ded, amax, wsrc = web.coverage_for(ins["carrier"], ins["carrier"])
                covstat = f"UNVERIFIED ({errs or 'no benefit data returned'})"
                source = f"web_fallback ({wsrc})"
                fallback = (shares, ded, amax)

        ins_pays, oop, detail, calc_notes = estimate_patient(p, benefits, fallback)
        notes += calc_notes
        all_detail += detail

        pending_total = sum(x["fee"] for x in p["pending"])
        recent = "; ".join(f"{c['code']} {c['desc']}".strip()[:38] for c in p["completed"][:5])
        rows.append({
            "patient_name": p["name"], "dob": p["dob"],
            "treatment_category": p["category"], "outreach_status": p["status"],
            "carrier": ins["carrier"] if ins else "NONE",
            "member_id": ins["member_id"] if ins else "",
            "coverage_status": covstat, "benefit_source": source,
            "deductible_remaining": f"{(benefits or {}).get('deductible') or (fallback[1] if fallback else 0):.0f}" if ins else "",
            "annual_max_remaining": (
                f"{benefits['annual_max_remaining']:.0f}"
                if benefits and benefits.get("annual_max_remaining") is not None
                else (f"{fallback[2]:.0f}" if fallback else "not reported")) if ins else "",
            "completed_treatments_count": len(p["completed"]),
            "recent_completed_treatments": recent,
            "pending_procedures_count": len(p["pending"]),
            "pending_codes": " ".join(sorted({x["code"] for x in p["pending"]})),
            "pending_total_fee": f"{pending_total:.2f}",
            "est_insurance_pays": f"{ins_pays:.2f}",
            "est_patient_out_of_pocket": f"{oop:.2f}",
            "notes": "; ".join(notes),
        })

    out1 = os.path.join(DATA_DIR, "final_patient_coverage_report.csv")
    out2 = os.path.join(DATA_DIR, "final_procedure_detail.csv")
    with open(out1, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    with open(out2, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_detail[0].keys()))
        w.writeheader(); w.writerows(all_detail)

    print(f"\n{'patient':22s} {'coverage':28s} {'pending':>9s} {'insurance':>10s} {'OOP':>9s}")
    for r in rows:
        print(f"{r['patient_name']:22s} {r['coverage_status'][:28]:28s} "
              f"${float(r['pending_total_fee']):8.2f} ${float(r['est_insurance_pays']):9.2f} "
              f"${float(r['est_patient_out_of_pocket']):8.2f}")
    print(f"\nwrote {out1}\nwrote {out2}")


if __name__ == "__main__":
    main()
