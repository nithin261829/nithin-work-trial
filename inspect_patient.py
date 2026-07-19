#!/usr/bin/env python3
"""
Per-patient insurance inspector - for manual verification.

Dumps, for one patient (or all), a readable view that lets you cross-check the
computed out-of-pocket against the raw 271 the payer returned:

  python3 inspect_patient.py "Sharon Mascari"     # one patient
  python3 inspect_patient.py --all                # every patient, one block each
  python3 inspect_patient.py "Sharon" --raw       # also dump raw EB benefit rows
  python3 inspect_patient.py "Sharon" --json      # dump the full raw 271 JSON

Reads the cached 271s in stedi_cache/ and the report/detail CSVs; no API calls.
"""

import csv
import glob
import json
import os
import re
import sys

DIR = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(DIR, "stedi_cache")

REPORT = {r["patient_name"]: r for r in csv.DictReader(open(os.path.join(DIR, "targeted_patient_coverage_report.csv")))}
DETAIL = {}
for _d in csv.DictReader(open(os.path.join(DIR, "targeted_procedure_detail.csv"))):
    DETAIL.setdefault(_d["patient"], []).append(_d)


def cache_file(name):
    f = os.path.join(CACHE, re.sub(r"\W+", "_", name) + ".json")
    return f if os.path.exists(f) else None


def parse(name):
    """Run the pipeline's own parser so what you see is what it computed from."""
    sys.path.insert(0, DIR)
    from coverage_pipeline import StediAgent
    f = cache_file(name)
    if not f:
        return None
    return StediAgent.parse(json.load(open(f)))


def show(name, raw=False, dump_json=False):
    key = next((k for k in REPORT if name.lower() in k.lower()
                or all(w in k.lower() for w in name.lower().split())), None)
    if not key:
        print(f"no patient matching '{name}'. Known: {sorted(REPORT)}")
        return
    r = REPORT[key]
    print("=" * 74)
    print(f"{key}  (dob {r['dob']})")
    print(f"  carrier: {r['carrier']}   status: {r['coverage_status']}")
    print(f"  source:  {r['benefit_source']}")
    print(f"  deductible remaining: ${r['deductible_remaining']}   "
          f"annual max remaining: ${r['annual_max_remaining']}")

    p = parse(key)
    if p:
        print("  --- parsed from the 271 ---")
        if p["coins"]:
            print(f"  category coinsurance (patient share): "
                  f"{ {k: f'{v:.0%}' for k, v in sorted(p['coins'].items()) if k} }")
        if p["deductible_by_stc"]:
            print(f"  deductible by category: { {k: f'${v:.0f}' for k, v in sorted(p['deductible_by_stc'].items())} }")
        if p["alt_benefit_stcs"]:
            print(f"  ALTERNATE BENEFITS flagged on categories: {sorted(p['alt_benefit_stcs'])}")
        pc = p["percode"]
        if pc:
            noncov = [c for c, e in pc.items() if e.get('noncovered')]
            print(f"  per-code entries: {len(pc)}   non-covered: {noncov or 'none'}")

    print("  --- target procedures & computed OOP ---")
    tot_fee = tot_ins = tot_oop = 0.0
    for d in DETAIL.get(key, []):
        fee, ins, oop = float(d["fee"]), float(d["insurance_pays_est"]), float(d["patient_oop_est"])
        tot_fee += fee; tot_ins += ins; tot_oop += oop
        flag = " [ALT-BENEFIT?]" if d.get("alt_benefit_downgrade_risk") == "Y" else ""
        pa = " [PRIOR-AUTH]" if d.get("prior_auth_required") == "Y" else ""
        print(f"    {d['procedure_code']:6s} t{d['tooth'] or '-':>3s} ${fee:8.2f}  "
              f"ins ${ins:8.2f}  pt ${oop:8.2f}  [{d['confidence']}] {d['basis'][:46]}{flag}{pa}")
    print(f"    {'TOTAL':6s}     ${tot_fee:8.2f}  ins ${tot_ins:8.2f}  pt ${tot_oop:8.2f}")
    if r["notes"]:
        print(f"  notes: {r['notes']}")

    if raw and cache_file(key):
        print("  --- raw EB benefit rows (in-network) ---")
        resp = json.load(open(cache_file(key)))
        for b in resp.get("benefitsInformation", []):
            if b.get("inPlanNetworkIndicatorCode") == "N":
                continue
            txt = " ".join(a.get("description", "") for a in (b.get("additionalInformation") or []))[:44]
            print(f"    {b.get('code')} {b.get('name','')[:16]:16s} stc:{','.join(b.get('serviceTypeCodes') or []):8s} "
                  f"tq:{b.get('timeQualifier',''):14s} amt:{b.get('benefitAmount')} pct:{b.get('benefitPercent')} {txt}")

    if dump_json and cache_file(key):
        print("  --- raw 271 JSON ---")
        print(json.dumps(json.load(open(cache_file(key))), indent=1))
    print()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    raw = "--raw" in sys.argv
    dj = "--json" in sys.argv
    if "--all" in sys.argv:
        for name in REPORT:
            show(name, raw, dj)
    elif args:
        show(" ".join(args), raw, dj)
    else:
        print(__doc__)
        print("patients:", ", ".join(sorted(REPORT)))
