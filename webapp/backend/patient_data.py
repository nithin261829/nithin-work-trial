"""Patient pull-up - lives on the NexHealth PMS, not in this codebase.

The PMS is the source of truth for who the patients are and their demographics;
everything here is fetched LIVE via the NexHealth API. The only thing joined in
from files is the insurance out-of-pocket estimate, which the PMS does not hold
(it comes from Stedi eligibility checks in coverage_pipeline.py).
"""

import time

import coverage_data as cov
from nexhealth_client import PRACTICE, nex

_ROSTER_CACHE = {"at": 0.0, "data": None}
_ROSTER_TTL = 60  # seconds; the PMS is authoritative, cache only to avoid hammering


def _pms_patients():
    """All patients for this location, pulled live from the PMS (short cache)."""
    if _ROSTER_CACHE["data"] and time.time() - _ROSTER_CACHE["at"] < _ROSTER_TTL:
        return _ROSTER_CACHE["data"]
    out, page, seen = [], 1, set()
    while page <= 20:  # hard cap: guards against an API that ignores paging
        try:
            r = nex.request("GET", "/patients", {"per_page": 300, "page": page})
        except Exception:
            break  # network hiccup on a later page -> use what we already have
        data = r["data"]
        chunk = data if isinstance(data, list) else data.get("patients", [])
        ids = {p.get("id") for p in chunk}
        if not chunk or ids <= seen:  # empty page, or same ids again -> stop
            break
        seen |= ids
        out.extend(chunk)
        if len(chunk) < 300:
            break
        page += 1
    if out:  # only refresh the cache on a successful (non-empty) pull
        _ROSTER_CACHE.update(at=time.time(), data=out)
    return _ROSTER_CACHE["data"] or out


def _full_name(p):
    return (p.get("name") or f"{p.get('first_name','')} {p.get('last_name','')}").strip()


def resolve(name):
    """Match a spoken name to a live PMS patient record. Returns the PMS patient
    dict (or None). Name/identity come from the PMS, never from local files."""
    n = name.lower().strip()
    best = None
    for p in _pms_patients():
        full = _full_name(p).lower()
        if n == full or n in full or all(w in full for w in n.split()):
            best = p
            if n == full:
                break
    return best


def roster():
    """Campaign roster for the staff view: each PMS patient joined with their
    coverage/OOP estimate (by name). Patient facts are LIVE from the PMS."""
    items = []
    for p in _pms_patients():
        name = _full_name(p)
        key = cov.match_name(name)
        if not key:
            continue  # only surface campaign patients we have coverage analysis for
        c = cov.coverage_for(key)
        items.append({
            "pms_id": p.get("id"),
            "name": name,
            "category": c["treatment_category"],
            "coverage": c["coverage_status"],
            "oop": c["patient_out_of_pocket_estimate"],
        })
    return items


def find_patient(name):
    """Full pull-up for one patient: LIVE PMS identity + demographics, joined
    with the coverage/OOP estimate. Used by the chat agent's find_patient tool."""
    p = resolve(name)
    if not p:
        return {"error": f"no patient matching '{name}' in the PMS"}
    full = _full_name(p)
    key = cov.match_name(full)
    bio = p.get("bio") or {}
    out = {
        "patient": full,
        "pms_id": p.get("id"),
        "email": p.get("email"),
        "date_of_birth": bio.get("date_of_birth"),
        "phone": bio.get("phone_number") or bio.get("cell_phone_number"),
    }
    c = cov.coverage_for(key) if key else None
    if c:
        out.update(c)
    else:
        out["coverage_status"] = "no coverage analysis on file"
    return out


def get_patient_record(name):
    """Live demographic record straight from the PMS (GET /patients/{id})."""
    p = resolve(name)
    if not p:
        return {"error": f"no patient matching '{name}' in the PMS"}
    r = nex.request("GET", f"/patients/{p['id']}")
    rec = r["data"].get("patient", r["data"]) if isinstance(r["data"], dict) else p
    bio = rec.get("bio") or {}
    return {"pms_id": rec.get("id"), "name": _full_name(rec),
            "first_name": rec.get("first_name"), "last_name": rec.get("last_name"),
            "email": rec.get("email"), "date_of_birth": bio.get("date_of_birth"),
            "phone": bio.get("phone_number") or bio.get("cell_phone_number"),
            "created_in_pms": rec.get("created_at"),
            "location_id": PRACTICE["location_id"]}
