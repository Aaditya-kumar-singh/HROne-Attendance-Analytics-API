# HROne Employee Attendance & Analytics API

This repository implements the HROne FastAPI/MongoDB assignment. The assignment requires all application code to remain in `app/main.py`; tests and documentation are separate.

## Run

Requirements: Python 3.11+ and MongoDB 6.0+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:MONGO_URI = "<your MongoDB URI>"
$env:MONGO_DB = "<your database name>"
uvicorn app.main:app --port 8000
```

The application also supports a local, uncommitted `.env`; real environment variables take precedence. Never commit credentials.

## Tests

```powershell
python -m pytest -q
```

Live integration tests create unique disposable `hrone_it_*` databases and drop only their own database:

```powershell
$env:HRONE_RUN_INTEGRATION = "1"
python -m pytest -q tests/test_mongodb_integration.py -ra
Remove-Item Env:HRONE_RUN_INTEGRATION
```

The 100,000-record explain smoke test writes to a disposable `hrone_perf_*` database and can take a few minutes:

```powershell
$env:HRONE_RUN_PERF = "1"
python -m pytest -s -q tests/test_mongodb_performance.py -ra
Remove-Item Env:HRONE_RUN_PERF
```

Do not run the live tests against a cluster where creating and dropping disposable databases is prohibited. The supplied sample JSON dumps and destructive sample seeder are intentionally excluded from the submission archive.

## Review and evidence

- `REVIEW.md`: contract failure modes, concrete examples, fixes and verification evidence.
- `DECISIONS.md`: the five requested design decisions.
- `docs/TRACEABILITY.md`: API and R1-R10 coverage map.
- `docs/VERIFICATION_REPORT.md`: test results, benchmark measurements and remaining limitations.
- `docs/SYSTEM_DESIGN.md`: architecture, invariants, concurrency and aggregation choices.

Latest verified results: 23 passed / 9 skipped in the default suite; 8 passed in the live integration suite; 1 passed in the 100k-record benchmark. The live MongoDB runs used Atlas 8.0.34. In the latest benchmark, the department trend endpoint took 6.33 seconds end-to-end; this is the main known performance limitation and is disclosed in `docs/VERIFICATION_REPORT.md`.

Do not upload `.env`, credentials, virtual environments, caches, database dumps, or a Dockerfile.
