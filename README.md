# Dental Insurance Coverage Pipeline

For every patient with unscheduled (pending) treatment in the practice dataset, this
pipeline answers three questions:

1. **Is their insurance active, and what are its terms?** — verified live against the
   payer via the Stedi clearinghouse (X12 270/271 eligibility).
2. **How much will insurance pay for each pending procedure?**
3. **How much will the patient owe out of pocket?**

## Results at a glance

| Output file | Contents |
|---|---|
| `final_patient_coverage_report.csv` | One row per patient: coverage status, deductible / annual max remaining, completed treatments, pending codes, estimated insurance payment, **estimated out-of-pocket**, notes |
| `final_procedure_detail.csv` | One row per pending procedure: fee, insurance vs. patient split, the pricing basis, a **confidence** level, and a **prior-auth-required** flag |
| `01_patients/patients.csv` | The source patient list extended with the coverage/OOP columns (`patients_original.csv` preserves the pristine file) |

## How to run

```bash
export STEDI_API_KEY=...          # Stedi clearinghouse key
export OPENAI_API_KEY=...         # optional: web fallback agent
export PROVIDER_NPI=...           # practice NPI (required for Aetna responses)
export PROVIDER_NAME="..."        # provider name matching the NPI
export CACHE_TTL_DAYS=7           # optional: re-verify eligibility older than N days
python3 coverage_pipeline.py [--live]   # --live bypasses the cache entirely
```

All payer responses are cached in `stedi_cache/` (raw JSON incl. raw X12), so reruns
are free and reproducible; anything older than `CACHE_TTL_DAYS` is re-verified live.

## Architecture

```
load_dataset()          practice CSVs -> per-patient {pending, completed, insurance}
      |
StediAgent.check()      X12 270/271 real-time eligibility per patient (cached, TTL)
StediAgent.parse()      271 -> active/inactive, deductible & annual-max remaining,
                        category coinsurance, per-CDT copay/coinsurance schedules
                        (both free-text lists and EB13 composite identifiers)
StediAgent.probe_codes()  procedure-level inquiries for every pending CDT code the
                        payer did not price (batched via encounter.medicalProcedures,
                        single-code fallback) - reveals code-specific exclusions
StediAgent.discover_insurance()  Insurance Discovery for uninsured/terminated patients
      |
WebAgent (OpenAI SDK)   fallback when a payer won't answer: web-search agent returns
                        carrier-typical coverage as JSON; typical-plan table last
      |
estimate_patient()      per procedure: per-code benefit > category coinsurance >
                        fallback; applies deductible, caps at annual max remaining
      |
CSV writers             report + detail + extended patients.csv
```

## Reading the detail CSV

**`confidence`** — how each line's estimate was derived:

| Level | Meaning |
|---|---|
| `per-code` | The payer stated a benefit for this exact CDT code (strongest) |
| `category` | Priced from the payer's category coinsurance; cannot see code-specific exclusions |
| `web-estimate` | Carrier-typical rates researched by the web fallback agent |
| `conservative` | Payer reported nothing for the code/category; full fee assumed so the patient is never under-quoted |
| `certain` | House/lab code (never billable) or patient has no active coverage |

**`prior_auth_required`** — `Y` when the payer's `authOrCertIndicator` flags the code.

## Notable findings in this dataset

- **Ryan Scott** (Delta Dental CA) has **exhausted his $1,000 annual max** — the 271
  reports Remaining $0.00. Insurance pays nothing this year; his filling drops to
  ~$76 if scheduled after January 1.
- **Carl Thompson** (Aetna Medicare Eagle PPO): procedure-level probing revealed his
  two **D6104 bone grafts are NON-COVERED** (category math had assumed 50%), while
  his extractions ARE covered at 20%.
- **Kelly Carruthers** (Delta Dental AZ): the raw X12 prices her pending **D2394 at
  100% patient share** (`EB*A*******1.00****Y*AD>D2394`) — insurance pays $0 on her
  entire pending plan.
- **Patricia Salinas** (EMI Health): coverage **terminated**; Insurance Discovery
  found no replacement coverage.
- **Roy Otterbein** (uninsured): Insurance Discovery located an **inactive
  UnitedHealthcare policy** (member 160026997) — worth confirming whether he has
  current coverage before quoting the full $5,227.
- **Barbara Paul & Robert Frieling** owe **$0** — their plans fully cover the
  pending work. Easiest scheduling wins.
- **El-khatib Suzy** hits her annual max mid-plan; splitting her three crowns across
  two plan years saves her ~$500.
- Cigna DHMO patients have **no annual maximum** (copay-schedule plans) — their
  "not reported" annual max is correct, not missing data.

## Known limitations / next steps

- **Frequency limitations** (e.g., crowns 1-per-5-years-per-tooth) arrive as HSD
  segments and free-text and are not computed against each tooth's history —
  submit an 837D **pre-determination** for definitive adjudication.
- **Alternate benefit clauses** ("ALTERNATE BENEFITS MAY APPLY") allow payers to pay
  premium materials at a cheaper alternative's rate; only a pre-determination
  resolves the exact allowance.
- Estimates use **office fees**, not payer-contracted allowed amounts.
- Eligibility is checked as of **today**; patients scheduling far out should be
  re-verified near the appointment (`dateOfService` supports this).
- **Coordination of Benefits** (secondary coverage) is not yet checked.
- Claims in `05_claims/claims_history.csv` with SUBMITTED/CREATED status could be
  tracked via Stedi's 276/277 claim-status API.

## Data provenance

`stedi_cache/` holds every raw payer response (including raw X12 271) for full
auditability. `patients_original.csv` is the untouched source file. The Aetna
responses required a registered provider NPI; other payers answered with a test NPI.
