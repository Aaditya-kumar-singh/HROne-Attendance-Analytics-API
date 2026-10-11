# Verification Report - HROne Employee Attendance & Analytics API

Report date: 11 October 2026
Application entry point: `app/main.py`
Architecture: all application code is in `app/main.py`, as required by the assignment.

## Summary

The application has been checked against the OpenAPI contract and the assignment's R1–R10 rules. It implements all 12 method/path operations. The default suite, live MongoDB integration suite, and 100,000-record explain/performance smoke have passed. Remaining limitations are disclosed: trend aggregation is relatively slow, nested lookup work is not fully represented by the root explain stats, and the hidden grader's MongoDB version is unknown.

## Verification results

- Python 3.12.13 environment created and requirements installed.
- `python -m compileall -q app tests`: passed.
- `python -m pip check`: no broken requirements.
- Default suite: `23 passed, 9 skipped`. The skips are opt-in live integration/performance tests.
- OpenAPI parity: 12 expected operations, 12 registered operations, no missing or unexpected routes.
- Live MongoDB integration suite: `8 passed` against Atlas. It covers employee and attendance lifecycles, duplicate handling, concurrency, regularization history, analytics edge cases, overnight shifts and all explain variants.
- 100k benchmark: `1 passed`; 1,000 employees and 100,000 attendance records were inserted into a unique disposable database, all five explain operations were run, and the test database was dropped.
- The exact submission ZIP was tested after packaging: archive integrity passed, the credential scan found no MongoDB URI credentials, and the archived default suite returned `23 passed, 9 skipped`. The extracted archive also compiled and retained 12/12 OpenAPI route parity.

## Latest 100k explain/performance run

MongoDB Atlas version: 8.0.34. The assignment states MongoDB 6.0+; the hidden grader version is unknown.

| Operation | Explain time (reported root stats) | Docs examined | Keys examined | Root nReturned | Endpoint wall time |
|---|---:|---:|---:|---:|---:|
| Attendance list | 0 ms | 100 | 100 | 100 | 0.07 s |
| Employee monthly | 0 ms | 31 | 31 | 31 | 0.15 s |
| Department summary | 482 ms | 1,000 | 1,000 | 1,000 | 0.59 s |
| Late leaderboard | 1,466 ms | 0 | 26,598 | 26,567 | 1.64 s |
| Department trend | 5,282 ms | 0 | 1 | 1 | 6.33 s |

All five explain payloads contained IXSCAN evidence and no COLLSCAN text. The table reports the first executionStats node found in each explain tree; for aggregation lookups this is not a complete count of all nested work. Trend root stats describe its indexed employee seed, not all date-spine and lookup work. The 6.33-second endpoint wall time is the better end-to-end observation, and trend performance remains the main known performance limitation. The latest 100k insert phase took 87.33 seconds; that is fixture-loading time, not endpoint latency.

## Assignment-specific architecture

The original assignment explicitly says the public submission must put all application code in `app/main.py`. Request models, database setup, indexes, shared helpers, routes, aggregation pipelines and explain logic are in that single application file. Tests and documentation remain separate.

## Fixes validated during integration

1. Normalize PyMongo's naive BSON UTC datetimes before comparing them with aware request timestamps.
2. Return 422 for a punch-out timestamp before the matching open punch-in rather than misreporting a 404.
3. Keep employee-monthly explain parameters consistent with join-date handling in the endpoint.
4. Exclude legacy attendance before `joined_on` from department summary, trend and late leaderboard calculations.
5. Use short unique test database names that satisfy Atlas naming limits.
6. Handle explicit null timestamps/statuses and invalid month year zero as validation failures.

## Known limitations

1. More endpoint-level boundary tests could cover every pagination boundary, status transition, and zero-working-day month.
2. Nested explain execution statistics need a more complete recursive aggregation; root statistics do not fully describe lookup work.
3. The trend endpoint took 6.33 seconds on this 100k smoke test and merits further optimization.
4. Startup behavior against a legacy database with duplicate values that block unique-index creation has not been separately exercised.
5. Integration/performance evidence is from Atlas MongoDB 8.0.34, not the hidden grader's exact version.
