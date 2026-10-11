# API and R1–R10 Traceability

This is a practical test map, not a claim that every listed contract branch is already covered. "Remaining" identifies the next evidence needed.

## API operation map

| Operation | Implementation | Current test evidence | Remaining coverage |
|---|---|---|---|
| GET /health | `app/main.py::health` | Integration healthy response; unit simulated PyMongo failure -> 503 | Readiness timing under grader runtime |
| POST /employees | `create_employee` | Integration create + duplicate 409; parallel create one-winner | All schema bounds and malformed inputs |
| GET /employees | `list_employees` | Integration department filter and total | Page bounds, multi-page sort/order, zero results |
| POST /attendance/punch-in | `punch_in` | Integration timestamp, IST date, duplicate 409; concurrent one-winner | Every status and exact timestamp boundary |
| GET /attendance | `list_attendance` | Integration employee/date filter and total | All filters, inclusive edges, pagination and invalid range |
| POST /attendance/punch-out | `punch_out` | Integration work hours/overtime, duplicate 409, concurrent one-winner, invalid chronology 422, overnight shift | Unknown employee/no record combinations and exact 24-hour boundary |
| PATCH /attendance/{emp_code}/{date} | `regularize_attendance` | Integration history diff, no-op/null/state validation, concurrent history preservation | Deterministic same-version race and all status transitions |
| GET /analytics/employees/{emp_code}/monthly | `employee_monthly` | Exact values for working days, half-day, leave, late/overtime and percentage | Empty periods, zero working days, orphan logs, joiner before joined_on |
| GET /analytics/departments/summary | `department_summary` | Exact headcount including zero-log employee, present days, average hours, late/leave | ABSENT/ON_DUTY fixtures and department filter |
| GET /analytics/leaderboard/late | `late_leaderboard` | Exact tie cutoff at limit=1; orphan and pre-join logs excluded | Department filter, ranks 1,2,2,4 and zero totals |
| GET /analytics/departments/{department}/trend | `department_trend` | Exact 3-day and weekend date spine/headcount/rates/moving averages; 404, invalid range and inclusive 92-day boundary | Broader trend fixtures on the exact grader-compatible MongoDB version |
| GET /admin/explain/{endpoint} | `explain_endpoint` | All five supported names return explain output; 100k smoke saw IXSCAN and no COLLSCAN text | Nested lookup stage stats and exact grader-compatible MongoDB version run |

## Business rules

| Rule | Expected invariant | Current evidence | Remaining coverage |
|---|---|---|---|
| R1 | Attendance date is IST shift-start date; overnight shifts end next day | Helper test and live overnight integration | More midnight and shift-end edge cases |
| R2 | 10-minute grace; late only strictly after grace, floor minutes from shift start | Unit boundaries 09:40:00, 09:40:01 and 10:15:59 | Endpoint-level timestamp cases |
| R3 | Overtime counts whole minutes only when >=30 minutes after shift end | Unit 29:59/30:00 threshold and overnight helper | Live 29:59/30:00 cases |
| R4 | Work hours duration rounded to 2 decimals HALF_UP | Unit half-up and live 9-hour lifecycle | Additional half-cent edge cases |
| R5 | Half-day iff rounded work_hours <4.50 | Unit tests exactly 4.50 and 4.49 | PATCH-derived threshold assertion |
| R6 | PRESENT/WFH/ON_DUTY count present; ABSENT/LEAVE do not | Monthly fixture covers PRESENT/WFH/LEAVE; regularization tests ABSENT/LEAVE null-time invariants | Explicit ON_DUTY analytics aggregate fixture |
| R7 | Weekdays only for presence; no holidays; working days start at joined_on | Monthly denominator, weekend log excluded, zero-log employee and post-period joiner; weekend trend rate is null | Zero-working-day edge and more full-month joiner cases |
| R8 | Values 2 decimals and rates 4 decimals HALF_UP | Exact monthly/trend fixture outputs | Tie-rounding cases for all analytics |
| R9 | Headcount includes employees joined by month end, even without logs | Summary exact zero-log employee and exclusion of employee joining after month end | Additional department-filter fixtures |
| R10 | Page starts 1, default 20, maximum 100, total after filters | Existing route bounds and list smoke | Explicit page 2/default/max/bad-bound tests |

## Running the evidence

- Unit/contract: `python -m pytest -q`
- MongoDB integration: set `HRONE_RUN_INTEGRATION=1`, then run `python -m pytest -q tests/test_mongodb_integration.py -ra`.
- 100k explain smoke: set `HRONE_RUN_PERF=1`, then run `python -m pytest -s -q tests/test_mongodb_performance.py -ra`.

Both live suites create uniquely named disposable databases and remove only the database they created. The live performance evidence currently comes from MongoDB Atlas 8.0.34; the hidden grader version is unknown.
