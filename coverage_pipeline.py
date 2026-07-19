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
# restrict the analysis to each patient's campaign target codes (the
# procedure_codes column in patients.csv) and write targeted_*.csv instead
TARGET_ONLY = "--target-only" in sys.argv or os.environ.get("TARGET_CODES_ONLY") == "1"
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
        targets = json.loads(pt["procedure_codes"]) if pt.get("procedure_codes") else []
        pending = d["pending"]
        if TARGET_ONLY and targets:
            pending = [x for x in pending if x["code"] in targets]
        out.append({
            "name": pt["patient_name"],
            "dob": pt["birth_date"],
            "category": pt["primary_category"],
            "status": pt["status"],
            "insurance": sorted(ins_by_patient.get(pid, []), key=lambda x: x["priority"])[:1],
            "completed": sorted(d["completed"], key=lambda x: x["date"] or "", reverse=True),
            "pending": pending,
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

    def probe_codes(self, patient, cdts):
        """Procedure-level eligibility inquiry: ask the payer about specific CDT
        codes via encounter.medicalProcedures - ONE request for all of them.
        Some payers (Aetna) only reveal code-specific exclusions this way.
        Returns {cdt: percode_entry} for every code the payer answered."""
        cdts = sorted(cdts)
        cache = os.path.join(
            CACHE_DIR, "probe_" + re.sub(r"\W+", "_", patient["name"]) + ".json")
        wrapper = None
        if self.cache_is_fresh(cache):
            wrapper = json.load(open(cache))
            if not set(cdts) <= set(wrapper.get("codes", [])):
                wrapper = None  # cache doesn't cover all needed codes

        ins = patient["insurance"][0]
        member_id = re.sub(r"\s+\d+$", "", ins["member_id"]).replace("-00", "").strip()
        parts = patient["name"].split()
        base = {
            "tradingPartnerServiceId": ins["payer_id"],
            "provider": {"organizationName": self.org_name, "npi": self.npi},
            "subscriber": {
                "firstName": parts[0], "lastName": " ".join(parts[1:]),
                "dateOfBirth": patient["dob"].replace("-", ""), "memberId": member_id,
            },
        }

        if wrapper is None:
            body = dict(base, encounter={"medicalProcedures": [
                {"productOrServiceIDQualifier": "AD", "procedureCode": c} for c in cdts]})
            try:
                resp = self._post(body)
            except Exception:
                return {}
            wrapper = {"codes": cdts, "response": resp, "singles": {}}
            json.dump(wrapper, open(cache, "w"), indent=1)
            time.sleep(0.4)
        wrapper.setdefault("singles", {})

        def extract(resp, wanted):
            found = {}
            for b in resp.get("benefitsInformation", []):
                if b.get("inPlanNetworkIndicatorCode") == "N":
                    continue
                cmpi = (b.get("compositeMedicalProcedureIdentifier") or {}).get("procedureCode")
                if cmpi not in wanted:
                    continue  # only trust rows the payer tied to a probed code
                code = b.get("code")
                entry = found.setdefault(cmpi, {})
                if code in ("I", "E"):
                    entry.clear(); entry["noncovered"] = True
                elif code == "A" and b.get("benefitPercent") is not None and "noncovered" not in entry:
                    entry["coins"] = float(b["benefitPercent"])
                elif code == "B" and b.get("benefitAmount") not in (None, "") and not entry:
                    entry["copay"] = float(b["benefitAmount"])
                if b.get("authOrCertIndicator") == "Y":
                    entry["prior_auth"] = True
            return found

        out = extract(wrapper["response"], set(cdts))
        # some payers ignore multi-procedure requests - retry those codes singly
        missing = [c for c in cdts if c not in out]
        dirty = False
        for c in missing:
            if c in wrapper["singles"]:
                resp = wrapper["singles"][c]
            else:
                body = dict(base, encounter={"productOrServiceIDQualifier": "AD", "procedureCode": c})
                try:
                    resp = self._post(body)
                except Exception:
                    continue
                wrapper["singles"][c] = resp
                dirty = True
                time.sleep(0.4)
            out.update(extract(resp, {c}))
        if dirty:
            json.dump(wrapper, open(cache, "w"), indent=1)
        return out

    def discover_insurance(self, patient):
        """Insurance Discovery: given only demographics, ask Stedi to locate
        coverage the practice doesn't have on file. Used for patients with no
        insurance or terminated coverage. Returns a human-readable note or None."""
        cache = os.path.join(
            CACHE_DIR, "discovery_" + re.sub(r"\W+", "_", patient["name"]) + ".json")
        if self.cache_is_fresh(cache):
            resp = json.load(open(cache))
        else:
            parts = patient["name"].split()
            body = {
                "provider": {"npi": self.npi, "organizationName": self.org_name},
                "subscriber": {"firstName": parts[0], "lastName": " ".join(parts[1:]),
                               "dateOfBirth": patient["dob"].replace("-", "")},
            }
            url = "https://healthcare.us.stedi.com/2024-04-01/insurance-discovery/check/v1"
            req = urllib.request.Request(url, json.dumps(body).encode(),
                                         {"Authorization": "Key " + self.api_key,
                                          "Content-Type": "application/json"})
            try:
                resp = json.load(urllib.request.urlopen(req, timeout=120))
            except Exception:
                return None
            json.dump(resp, open(cache, "w"), indent=1)
            time.sleep(0.4)

        if not resp.get("coveragesFound"):
            return "insurance discovery found no coverage"
        findings = []
        for item in resp.get("items", []):
            payer = (item.get("payer") or {}).get("name", "unknown payer")
            member = (item.get("subscriber") or {}).get("memberId", "?")
            codes = {b.get("code") for b in item.get("benefitsInformation", [])}
            state = "ACTIVE" if "1" in codes and "6" not in codes else "INACTIVE"
            findings.append(f"{state} {payer} policy (member {member})")
        return "insurance discovery: " + "; ".join(findings) + " - confirm with patient"

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
        # per-category deductible: stc -> remaining $ (payers report $0 for the
        # categories they waive - preventive/diagnostic - and the real amount for
        # basic/major). Lets us apply the deductible only where it actually bites.
        ded_by_stc = {}
        alt_benefit_stcs = set()  # categories flagged "ALTERNATE BENEFITS MAY APPLY"
        secondary_payers = []     # code R = Other/Additional Payor (secondary coverage)
        freq_limits = {}          # CDT code -> {"quantity": n, "months": window}
        active = inactive = False

        # plan/eligibility dates (top-level planDateInformation)
        pdi = resp.get("planDateInformation") or {}
        eligibility_begin = pdi.get("eligibilityBegin") or pdi.get("planBegin")
        latest_visit = pdi.get("latestVisitOrConsultation")

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

            # code R = a second payer on this member (secondary insurance / COB)
            if code == "R":
                for ent in ([b.get("benefitsRelatedEntity")] + (b.get("benefitsRelatedEntities") or [])):
                    nm = (ent or {}).get("entityName")
                    if nm and nm not in secondary_payers:
                        secondary_payers.append(nm)

            # frequency limitations: "F" rows carry a quantity + a time window, in
            # two shapes - benefitQuantity (+ timeQualifier), or benefitsServiceDelivery
            fcode = (b.get("compositeMedicalProcedureIdentifier") or {}).get("procedureCode")
            if code == "F" and fcode:
                qty = months = None
                if b.get("benefitQuantity"):
                    try:
                        qty = int(float(b["benefitQuantity"]))
                    except ValueError:
                        qty = None
                    tq = (b.get("timeQualifier") or "").lower()
                    months = 12 if ("year" in tq or "calendar" in tq) else 12
                for sd in (b.get("benefitsServiceDelivery") or []):
                    try:
                        qty = int(float(sd.get("quantity", qty or 1)))
                        n = int(float(sd.get("numOfPeriods", 1)))
                    except (ValueError, TypeError):
                        continue
                    unit = (sd.get("timePeriodQualifier") or "").lower()
                    months = n * (12 if "year" in unit else 1 if "month" in unit else 12)
                if qty and months and (fcode not in freq_limits or months > freq_limits[fcode]["months"]):
                    freq_limits[fcode] = {"quantity": qty, "months": months}

            # alternate-benefit (downgrade) disclaimer - the payer reserves the
            # right to pay a premium procedure at a cheaper alternative's rate
            _txt_all = " ".join(a.get("description", "") for a in (b.get("additionalInformation") or []))
            if "alternate benefit" in _txt_all.lower():
                for s in stcs:
                    alt_benefit_stcs.add(s)

            # per-CDT-code benefits arrive two ways:
            #  1. structured: EB13 compositeMedicalProcedureIdentifier (Delta, UHC)
            #     including code "I" = Non-Covered (payer pays nothing for that CDT)
            #  2. free text: CDT codes in additionalInformation (Cigna DHMO, Humana)
            cmpi_code = (b.get("compositeMedicalProcedureIdentifier") or {}).get("procedureCode")
            if cmpi_code:
                entry = percode.setdefault(cmpi_code, {})
                if code == "A" and pct is not None:
                    entry["coins"] = float(pct)
                elif code == "B" and amt not in (None, ""):
                    entry["copay"] = float(amt)
                elif code in ("I", "E"):
                    entry["noncovered"] = True
                if b.get("authOrCertIndicator") == "Y":
                    entry["prior_auth"] = True
                continue
            txt = " ".join(a.get("description", "") for a in (b.get("additionalInformation") or []))
            cdt_codes = re.findall(r"\bD\d{4}\b", txt)
            for c in cdt_codes:
                if code == "A" and pct is not None:
                    percode.setdefault(c, {})["coins"] = float(pct)
                if code == "B" and amt not in (None, ""):
                    percode.setdefault(c, {})["copay"] = float(amt)
                if code in ("I", "E"):
                    percode.setdefault(c, {})["noncovered"] = True
            if cdt_codes:
                continue  # already captured at per-code granularity

            if code == "A" and pct is not None:
                for s in stcs:
                    coins.setdefault(s, []).append(float(pct))
            if code == "B" and amt not in (None, ""):
                for s in stcs:
                    copays.setdefault(s, []).append(float(amt))
            if code == "C" and amt not in (None, "") and lvl in ("IND", ""):
                a = float(amt)
                # plan-level deductible (stc 30/35) - what we report as "the" number
                if any(s in ("35", "30", "") for s in stcs):
                    if tq == "Remaining":
                        ded_rem = a if ded_rem is None else min(ded_rem, a)
                    elif tq == "Calendar Year":
                        ded_cal = a if ded_cal is None else max(ded_cal, a)
                # per-category deductible - remaining preferred over calendar-year
                if tq in ("Remaining", "Calendar Year", ""):
                    for s in stcs:
                        if s in ("", "30"):
                            continue
                        prev = ded_by_stc.get(s)
                        if prev is None or tq == "Remaining":
                            ded_by_stc[s] = a
            if code == "F" and amt not in (None, "") and lvl in ("IND", ""):
                if any(s in ("35", "30") for s in stcs):
                    a = float(amt)
                    # $0 Calendar Year rows are placeholders, but a $0 Remaining is
                    # genuine exhaustion when the plan reports a positive annual max
                    if "Remaining" in tq and "Lifetime" not in tq:
                        max_rem = a if max_rem is None else max(max_rem, a)
                    elif tq == "Calendar Year" and a > 0:
                        max_cal = a if max_cal is None else max(max_cal, a)

        return {
            "active": active and not inactive,
            "inactive": inactive,
            "coins": {s: min(v) for s, v in coins.items()},       # min = best in-network share
            "copays": {s: min(v) for s, v in copays.items()},
            "percode": percode,
            "deductible": ded_rem if ded_rem is not None else ded_cal,
            "deductible_by_stc": ded_by_stc,
            "alt_benefit_stcs": alt_benefit_stcs,
            "secondary_payers": secondary_payers,
            "freq_limits": freq_limits,
            "eligibility_begin": eligibility_begin,   # YYYYMMDD
            "latest_visit": latest_visit,             # YYYYMMDD, payer's own last-visit record
            "annual_max": max_cal,
            # trust a Remaining value (even $0 = exhausted) only when it is positive
            # or the plan reported a positive annual max; bare $0s are placeholders
            "annual_max_remaining": (
                max_rem if max_rem is not None and (max_rem > 0 or max_cal)
                else max_cal),
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
def estimate_patient(patient, benefits, fallback, no_coverage=False):
    """Compute (insurance_pays, patient_oop, detail_rows, notes) for one patient.

    Each detail row carries a confidence level:
      certain          - house/lab code, or patient has no active coverage
      per-code         - the payer stated a benefit for this exact CDT code
      category         - priced from the payer's category coinsurance
      web-estimate     - carrier-typical rates from the web fallback agent
      conservative     - payer reported nothing for this code/category; full
                         fee assumed so the patient is never under-quoted
    """
    detail, notes = [], []
    ins_total = oop_total = 0.0

    ded_by_stc, alt_stcs, freq_limits = {}, set(), {}
    if benefits:
        coins = benefits["coins"] or None
        percode = benefits["percode"]
        ded_left = benefits["deductible"] or 0
        ded_by_stc = dict(benefits.get("deductible_by_stc") or {})
        alt_stcs = set(benefits.get("alt_benefit_stcs") or set())
        freq_limits = benefits.get("freq_limits") or {}
        max_left = benefits["annual_max_remaining"]
        # secondary insurance (code R) - a second payer may cover part of the
        # patient's share; we estimate against the primary only, so real OOP is lower
        for sp in benefits.get("secondary_payers") or []:
            notes.append(f"has SECONDARY coverage ({sp}) - estimate is primary-only; "
                         "actual out-of-pocket is likely LOWER after coordination of benefits")
    else:
        coins, percode, ded_left, max_left = None, {}, 0, 0

    from datetime import date
    TODAY = date(2026, 7, 19)

    def _ymd(s):
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except (TypeError, ValueError):
            return None

    # waiting-period risk: a recent eligibility start means major work (crowns,
    # bridges, dentures, implants) may sit inside a 6-12 month waiting period and
    # not be covered yet. eligibilityBegin == Jan 1 is ambiguous (renewal vs new),
    # so we FLAG for verification rather than deny.
    major_wait_risk = False
    if benefits:
        eb = _ymd(benefits.get("eligibility_begin"))
        if eb and (TODAY.toordinal() - eb.toordinal()) < 365:
            major_wait_risk = True
            months_in = round((TODAY.toordinal() - eb.toordinal()) / 30.44)
            notes.append(f"coverage began {eb.isoformat()} (~{months_in} mo ago) - VERIFY no "
                         "waiting period on major work; if new enrollee, major treatment "
                         "may not be covered yet and OOP would be full fee")

    # frequency cross-check: how many times has each code been used within its limit
    # window (ending today)? Counts our completed-procedure history AND the payer's
    # own latest-visit date (latestVisitOrConsultation) - the payer's record is
    # authoritative for whole-mouth services (exams, cleanings, x-rays).
    completed = patient.get("completed", [])
    payer_last_visit = _ymd(benefits.get("latest_visit")) if benefits else None
    WHOLE_MOUTH = {"23", "41"}  # diagnostic / preventive - one latest-visit date applies

    def frequency_exhausted(code, tooth):
        lim = freq_limits.get(code)
        if not lim:
            return False
        cutoff = TODAY.toordinal() - int(lim["months"] * 30.44)
        dates = []
        for c in completed:
            if c["code"] != code:
                continue
            if tooth and c.get("tooth") and c["tooth"] != tooth:
                continue  # per-tooth procedures count only the same tooth
            cd = _ymd((c.get("date") or "").replace("-", "")[:8])
            if cd and cd.toordinal() >= cutoff:
                dates.append(cd.toordinal())
        # the payer's authoritative last-visit date adds one use for whole-mouth
        # services (exams/x-rays), unless we already counted a visit that day
        if (payer_last_visit and cdt_to_stc(code) in WHOLE_MOUTH
                and payer_last_visit.toordinal() >= cutoff
                and payer_last_visit.toordinal() not in dates):
            dates.append(payer_last_visit.toordinal())
        return len(dates) >= lim["quantity"]
    if fallback:
        coins, ded_left, max_left = fallback[0], fallback[1], fallback[2]
        percode = {}
    if max_left is None:
        max_left = float("inf")
        notes.append("annual max not reported by payer - uncapped estimate")

    # a plan-wide alternate-benefit disclaimer (attached to stc 30/35) flags every
    # elective-material procedure; note it once so staff send a pre-determination
    plan_wide_alt = bool(alt_stcs & {"30", "35", ""})
    if alt_stcs:
        notes.append("plan reports ALTERNATE BENEFITS MAY APPLY - insurance may pay premium "
                     "materials (crowns, composites) at a cheaper alternative's rate; "
                     "confirm with a pre-determination")

    def category_deductible(stc):
        """Remaining deductible that applies to this category. Prefer the payer's
        per-category figure; fall back to the plan-level number for categories the
        payer didn't itemize. $0 means the category is exempt (e.g. preventive)."""
        if stc in ded_by_stc:
            return ded_by_stc[stc]
        return None  # unknown at category level -> use the shared plan deductible

    def apply_coinsurance(fee, share, stc):
        """Patient share for a coinsurance procedure, consuming the deductible for
        this category. Returns (patient_amount, deductible_used)."""
        nonlocal ded_left
        cat_ded = category_deductible(stc)
        pool = cat_ded if cat_ded is not None else ded_left
        ded_use = min(pool, fee) if (share < 1 and pool > 0) else 0
        covered = max(fee - ded_use, 0) * (1 - share)
        if cat_ded is not None:
            ded_by_stc[stc] = max(cat_ded - ded_use, 0)
        else:
            ded_left -= ded_use
        return round(fee - covered, 2), ded_use

    for proc in patient["pending"]:
        fee, code = proc["fee"], proc["code"]
        stc = cdt_to_stc(code)
        pat_amt, basis, conf = None, "", ""
        alt_flag = freq_flag = ""

        # frequency limit already used up this window -> payer denies, patient owes full
        if frequency_exhausted(code, proc.get("tooth")):
            lim = freq_limits[code]
            pat_amt = fee
            basis = f"frequency limit reached ({lim['quantity']} per {lim['months']}mo) - not covered"
            conf = "per-code"
            freq_flag = "Y"
        elif stc is None:
            pat_amt, basis, conf = fee, "house/lab code - not billable to insurance", "certain"
        elif code in percode:
            conf = "per-code"
            e = percode[code]
            if e.get("noncovered"):
                pat_amt = fee
                basis = "payer lists this code as non-covered"
            elif "coins" in e:
                pat_amt, ded_use = apply_coinsurance(fee, e["coins"], stc)
                deducted = f", ${ded_use:.0f} deductible" if ded_use else ""
                basis = f"per-code coinsurance {e['coins']:.0%} patient share{deducted}"
            else:
                pat_amt = min(e["copay"], fee) if fee else e["copay"]
                basis = f"per-code copay ${e['copay']:.0f}"
        elif coins:
            share = coins.get(stc, coins.get("35"))
            if share is not None:
                pat_amt, ded_use = apply_coinsurance(fee, share, stc)
                deducted = f", ${ded_use:.0f} deductible" if ded_use else ""
                basis = f"{STC_NAMES.get(stc, stc)} coinsurance {share:.0%} patient share{deducted}"
                conf = "web-estimate" if fallback else "category"

        if pat_amt is None:
            if no_coverage:
                pat_amt, basis, conf = fee, "no active coverage - full fee", "certain"
            else:
                pat_amt, basis, conf = fee, "no benefit info - assume full fee", "conservative"

        # alternate-benefit (downgrade) exposure: crowns/onlays and posterior
        # composites are the classic downgrade targets. We can flag but not size it
        # (the 271 carries no downgrade allowance) - so the patient's real OOP on
        # these could be HIGHER than shown, pending a pre-determination.
        downgradeable = code in {"D2391", "D2392", "D2393", "D2394"} or stc in ("36", "39")
        if (plan_wide_alt or stc in alt_stcs) and downgradeable and pat_amt is not None and pat_amt < fee:
            alt_flag = "Y"

        # waiting-period exposure on major work (crowns/prostho/oral surgery)
        wait_flag = "Y" if (major_wait_risk and stc in ("36", "39", "40")
                            and pat_amt is not None and pat_amt < fee) else ""

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
            "basis": basis, "confidence": conf,
            "prior_auth_required": "Y" if percode.get(code, {}).get("prior_auth") else "",
            "alt_benefit_downgrade_risk": alt_flag,
            "frequency_limit_reached": freq_flag,
            "waiting_period_risk": wait_flag,
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
            found = stedi.discover_insurance(p)
            if found:
                notes.append(found)
        else:
            print(f"checking {p['name']} ({ins['carrier']}) ...")
            resp = stedi.check(p)
            parsed = StediAgent.parse(resp)
            if parsed["inactive"]:
                covstat, source = "INACTIVE (terminated per Stedi 271)", "stedi_live"
                notes.append("coverage inactive - full fee unless new insurance obtained")
                found = stedi.discover_insurance(p)
                if found:
                    notes.append(found)
            elif parsed["active"] and (parsed["coins"] or parsed["percode"] or parsed["copays"]):
                covstat, source, benefits = "ACTIVE (verified via Stedi)", "stedi_live", parsed
                if parsed["percode"]:
                    source += " (per-procedure schedule)"
                # category-level answers can hide code-specific exclusions, so ask
                # the payer directly about every pending CDT code it did not price
                # (batched: one extra request per patient via medicalProcedures)
                unpriced = sorted({x["code"] for x in p["pending"]
                                   if re.fullmatch(r"D\d{4}", x["code"])
                                   and x["code"] not in parsed["percode"]})
                if unpriced:
                    for cdt, got in stedi.probe_codes(p, unpriced).items():
                        parsed["percode"][cdt] = got
                        what = ("NON-COVERED" if got.get("noncovered")
                                else f"coins {got.get('coins')}" if "coins" in got
                                else f"copay ${got.get('copay')}")
                        pa = " [prior auth required]" if got.get("prior_auth") else ""
                        print(f"    probe {cdt}: {what}{pa}")
                # some payers (UHC) omit the annual max from the 271, but plan names
                # like "UHC 1000" or "UHC 5000-100-50-50" carry it; DHMO plans have
                # no max at all, so only infer when the plan uses coinsurance
                if parsed["annual_max_remaining"] is None and parsed["coins"]:
                    m = re.search(r"\b([1-9]\d{3})\b", ins["carrier"])
                    if m:
                        parsed["annual_max_remaining"] = float(m.group(1))
                        notes.append(f"annual max ${m.group(1)} inferred from plan name "
                                     "(payer did not report it; assumes none used this year)")
            else:
                # payer refused (e.g. Aetna without enrolled NPI) -> OpenAI web fallback
                errs = "; ".join(e.get("description", "")[:60] for e in resp.get("errors", []))
                shares, ded, amax, wsrc = web.coverage_for(ins["carrier"], ins["carrier"])
                covstat = f"UNVERIFIED ({errs or 'no benefit data returned'})"
                source = f"web_fallback ({wsrc})"
                fallback = (shares, ded, amax)

        no_cov = not ins or covstat.startswith("INACTIVE")
        ins_pays, oop, detail, calc_notes = estimate_patient(p, benefits, fallback, no_coverage=no_cov)
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

    prefix = "targeted" if TARGET_ONLY else "final"
    out1 = os.path.join(DATA_DIR, f"{prefix}_patient_coverage_report.csv")
    out2 = os.path.join(DATA_DIR, f"{prefix}_procedure_detail.csv")
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
