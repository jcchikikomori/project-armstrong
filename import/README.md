# CSV importer — Google Sheets health tracker → MediKeep

Reads the seven CSVs in `datasets/`, maps them onto MediKeep's record types and
loads them over the REST API. One-way migration, run by hand, safe to re-run.

Claude generated these files.

## Why it lives outside the app

MediKeep ships exactly one CSV importer and it is vitals-only and hardcoded to
Dexcom — `app/services/vitals_parsers/__init__.py` registers a single parser and
offers no way to add another without rebuilding the image. So this is a client
that speaks HTTP to `/api/v1`, not a patch.

Going through the API rather than `psql` is deliberate. Direct inserts skip
Pydantic validation, the activity log and the sequence handling — and it was
MediKeep's own validators that caught the two impossible SpO2 readings in this
dataset.

## What maps where

| Source | Rows | Becomes |
| --- | --- | --- |
| `health_tracker.csv`, BP rows | 39 | 39 vitals |
| `health_tracker.csv`, symptom + EMS rows | 116 | 6 symptoms, 191 occurrences |
| `laboratory_log.csv` | 125 | 25 lab result panels, 125 test components |
| `medicine_tracking.csv` | 7 | 7 medications |
| `assessments.csv` | 18 | 18 allergies, tagged `trigger-assessment` |
| `doctor_notes.csv` | 2 | 2 encounters |
| `persona.csv`, drug-allergy row | 1 | 1 allergy, severity `mild` |
| everything else | 548 | `datasets/derived/*.json` |

414 records in total, plus 548 sidecar entries.

**The sidecar is not a consolation prize.** 508 of the tracker rows are
medication _dose events_, and MediKeep's `medications` table holds one row per
drug, not one per swallow. Forcing them in would produce a technically
successful import and an unusable medication list, so they keep full fidelity
in `datasets/derived/medication_log.json` — timestamp, drug, milligrams, intake
time — where the LLM pipeline can still read them. Sleep, food, activity,
insights and persona rows go the same way, for the same reason: MediKeep has no
table that models them honestly.

**On the allergies.** `assessments.csv` is a food and environment _trigger_
matrix — dark chocolate, white rice, coffee. Those are intolerances, not IgE
allergies. MediKeep has no intolerance table, so they land in allergies, but
every record says so in its own notes and carries the `trigger-assessment` tag.

## Running it

Everything runs in Docker. The container joins `medikeep-network` so it can
reach the app container by name, because the app deliberately publishes no
ports (see `deploy/docker-compose.yml`).

```bash
export MEDIKEEP_PASSWORD='...'        # never as an argument

# Dry run. Writes nothing, talks to nothing.
docker compose -f import/docker-compose.yml run --rm importer plan

# Derived JSON only, no API calls.
docker compose -f import/docker-compose.yml run --rm importer sidecar

# The real thing.
docker compose -f import/docker-compose.yml run --rm importer \
  apply --yes --base-url http://medikeep-app:8000 --username admin

# Tests.
docker compose -f import/docker-compose.yml run --rm --entrypoint pytest importer
```

If the MediKeep stack is not up on this machine, the network will not exist:

```bash
docker network create medikeep-network
```

### Options

```text
plan | apply | sidecar     default: plan
--datasets PATH            default: /data (the bind mount)
--sidecar-dir PATH         default: <datasets>/derived
--ledger PATH              default: <sidecar-dir>/.import-ledger.sqlite3
--base-url URL             default: http://localhost:8000
--username NAME            default: admin
--patient-id N             skip auto-detection
--split-freetext           one symptom per free-text complaint (see below)
--limit N                  stop after N creates; for smoke tests
--yes                      required by apply
```

`--split-freetext` changes how 84 distinct one-off complaints are handled. By
default they are grouped under a single `Other (self-reported)` symptom with the
text kept verbatim in each occurrence's notes, because 84 singleton symptoms
bury the three that actually recur (Dizzy/Nahihilo 47, Anxiety 37, Chest Pains
10). Pass the flag to get one symptom each instead.

## Before the first apply

**Read the warnings from `plan`.** Seven of them, and each one is a decision the
importer made on its own: two SpO2 fields dropped as impossible, two backdates
honoured, two negative pill counts passed through, and one invented symptom
severity.

**`DRUG_ALLERGY_SEVERITY` is set to `mild`** in
`medikeep_import/mappers/allergies.py`, for the persona row
`Fluoroquinolones due to Levofloxacin overdose`. MediKeep requires a severity
and the source records none, so that value is a stated choice rather than
anything derived from the data. Set it to `None` to skip the record instead.

**Get a trustworthy field reference** if anything looks off. Set
`ENABLE_API_DOCS=true` in `deploy/.env`, restart the app, and read
`/api/v1/openapi.json`. The published API reference has drifted from the code —
it documents `encounter_date` where the schema wants `date`, and
`provider_name` where insurance wants `company_name`. Turn the flag back off
afterwards.

`openapi.json` is necessary but not sufficient. MediKeep enforces a lot in
Pydantic field validators, which do not appear in the schema at all. Three of
them shaped the lab mapping, and each was found by POSTing and reading the 422:

- `LabTestComponentCreate` requires `lab_result_id` **in the body**, even
  though the path already carries it.
- `qualitative_value` accepts only `positive`, `negative`, `detected`,
  `undetected`. Real results like `Yellow`, `Steatosis` and `Trace` go to
  `textual_value` instead.
- Qualitative components may not carry a reference range at all
  (`Reference ranges are not applicable for qualitative tests`), so the
  sheet's range moves into `notes`.

If a future MediKeep version rejects something new, the failure looks the same:
a `FAILED` line naming the row and quoting the validator.

## Re-running

Safe. Every record carries a `source_key` of `<file>:<row_index>:<sub_entity>`,
and the SQLite ledger at `datasets/derived/.import-ledger.sqlite3` remembers
which ones exist. A second `apply` reports `skipped` and creates nothing.

The ledger is scoped by base URL, so importing into a local throwaway stack does
not convince the importer that the droplet is already done.

Row index is the stable part of that key, which holds because a Google Forms
response sheet only ever appends. Re-exporting after inserting a row in the
middle of the sheet would shift every key after it, and the importer would
create duplicates.

## Undo

Vitals carry `import_source=gsheets-health-tracker`, which unlocks MediKeep's
own endpoint:

```text
DELETE /api/v1/vitals/patient/{patient_id}/import/gsheets-health-tracker/date/{date}
```

Everything else carries the `gsheets-import` tag. There is no bulk delete by tag
in the API, so a cleanup means either the admin bulk-delete endpoint with ids
from the ledger, or a restore from `pg_dump`. Take the backup first — see
`deploy/README.md`.

After any delete, remove the matching rows from the ledger, or the next `apply`
will skip records that no longer exist.

## Verification checklist

Verified end-to-end against a local stack on 2026-09-25: a from-scratch import
created all 414 records with zero failures, and the re-run created none.

```bash
docker compose -f import/docker-compose.yml run --rm --entrypoint pytest importer  # 154 passed
docker compose -f import/docker-compose.yml run --rm importer plan                 # 414 records, 7 warnings
docker compose -f import/docker-compose.yml run --rm importer apply --yes --limit 5  # smoke test
docker compose -f import/docker-compose.yml run --rm importer apply --yes
docker compose -f import/docker-compose.yml run --rm importer apply --yes          # 0 created, 414 skipped
```

Then open the UI and check one BP reading, one lab panel and one medication by
eye. The counts reconciling is necessary, not sufficient.

## Risks worth knowing about

1. **There is no transaction.** 414 individual POSTs, each its own transaction.
   A failure part-way leaves a partial import — which the ledger makes
   resumable, but it is still partial.
1. **A 4xx does not stop the run.** One rejected payload costs that row, not the
   other 413. Read the `FAILED` lines at the end; they are the point.
1. **The sidecar JSON is plaintext medical data.** Written `0600` into a `0700`
   directory and gitignored, the same treatment `export-json.sh` gives its
   output. It never leaves the machine on its own.
1. **No practitioner records are created.** Two of the three prescribers are
   doctors and the third is a health centre, and creating a practitioner needs a
   `specialty_id` whose endpoint is rate-limited to 20/hour. The names are kept
   in the medication and encounter notes instead.
1. **Symptom severity is invented.** The source records none, so every
   occurrence gets `mild` and the EMS row gets `critical`. That is a floor, not
   a measurement.
