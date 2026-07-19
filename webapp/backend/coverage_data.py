"""Stedi-derived coverage / out-of-pocket data (from coverage_pipeline.py output).

The PMS does NOT hold these estimates - they come from live insurance
eligibility checks - so they are loaded from the pipeline's CSV outputs and
joined to PMS patients by name.
"""

import csv
import os

from nexhealth_client import REPO_DIR


def _read(name):
    with open(os.path.join(REPO_DIR, name), newline="") as f:
        return list(csv.DictReader(f))


REPORT = {r["patient_name"]: r for r in _read("targeted_patient_coverage_report.csv")}
DETAIL = {}
for _d in _read("targeted_procedure_detail.csv"):
    DETAIL.setdefault(_d["patient"], []).append(_d)
BOOKINGS = {r["patient"]: r for r in _read("nexhealth_booking_results.csv")}


def match_name(name):
    """Fuzzy-match a spoken name to a campaign patient key."""
    return next((k for k in REPORT if name.lower() in k.lower()
                 or all(w in k.lower() for w in name.lower().split())), None)


def coverage_for(key):
    """Coverage + per-procedure OOP for a matched patient key, or None."""
    r = REPORT.get(key)
    if not r:
        return None
    return {
        "treatment_category": r["treatment_category"],
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
    }
