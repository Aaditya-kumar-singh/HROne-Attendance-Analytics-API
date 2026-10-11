# HROne Attendance API — System Design

## 1. Purpose and constraints
Implement a contract-exact HR attendance service with FastAPI, PyMongo and MongoDB 6.0+. The assignment explicitly requires all application code in `app/main.py`; tests and documentation may be separate.

Capabilities: employee CRUD, punch-in/out, manual regularization, employee monthly analytics, department summary, late leaderboard, daily trend with gap filling and moving average, and query-plan explain.

Quality goals: correctness, concurrent request safety, indexed reads over roughly 100,000 attendance records, timezone/rounding determinism, auditable corrections, safe configuration and explainable decisions.

## 2. Logical architecture
HTTP client/grader
 -> FastAPI routing and request validation
 -> app/main.py (all application logic in one file)
    - request models and strict epoch-millisecond validation
    - UTC/IST shift calculations and Decimal half-up rounding
    - BSON/API serializers
    - route handlers, reusable Mongo filters and aggregation pipeline builders
    - explain executionStats endpoint
 -> one process-level PyMongo client
 -> MongoDB database from MONGO_DB
    - employees
    - attendance_logs
    - unique and query-supporting indexes

No external service calls are permitted. Environment variables override .env. Health checks verify database reachability.

## 3. Data ownership and invariants
Employees:
- emp_code is unique, immutable business identity.
- shift_start/shift_end are HH:MM; joined_on is YYYY-MM-DD.
- created_at is BSON datetime internally, epoch milliseconds externally.
- Never expose Mongo _id.

Attendance:
- Natural key is (emp_code, date), unique.
- date is the R1 shift-start attendance date, not always the civil date of an early-morning overnight punch-in.
- BSON instants are UTC datetimes; HTTP instants are integer epoch milliseconds.
- Derived values are computed on writes, stored, and read as stored by analytics.
- Missing legacy history -> [] and missing half_day -> false.
- history records manual corrections only and is append-only.

Invariants:
1. One record maximum per employee attendance date.
2. Presence statuses require punch_in; ABSENT/LEAVE have both punch times null.
3. punch_out > punch_in and is no more than 24 hours later.
4. Instants are truncated to whole seconds before calculation/storage.
5. Responses never expose _id or id.
6. A successful PATCH changes something and appends one entry containing only actually changed fields.
7. Analytics do not recompute stored derived values.

## 4. Time-domain design
- Use explicit UTC for instants and IST (UTC+05:30) for shift/calendar interpretation.
- Use UTC-aware epoch conversion; never depend on machine-local timezone.
- Reject booleans, floats, strings, seconds-like values below 100000000000 and timestamps above 4102444800000 with 422.
- Truncate sub-second precision at ingress.
- Overnight when shift_end <= shift_start.
- Late only if strictly >600 seconds after shift start; if late, floor whole minutes since shift start.
- Overtime is floor minutes after shift end, only if at least 30; overnight end occurs the following day.
- Work hours = duration seconds / 3600, rounded to 2 decimal places with ROUND_HALF_UP.
- half_day iff rounded work_hours < 4.50.
- Rates use 4 decimal places with half-up rounding.
- Share tested domain helpers across punch-in, punch-out and PATCH.

## 5. Request lifecycle
1. FastAPI validates primitive request/path/query shape.
2. Domain validation applies strict timestamps and cross-field rules.
3. Endpoint builds a Mongo filter or aggregation via shared builders.
4. MongoDB handles filtering, sorting, pagination, joins and analytics.
5. Serializer converts all BSON timestamps, including nested history values, to epoch milliseconds and strips internal fields.
6. Map missing resources to 404, conflicts to 409, invalid input/state to 422, unhealthy database to 503.

## 6. Concurrency strategy
Employee creation:
- Unique employees.emp_code index is authoritative.
- Preflight lookup is optional UX optimization only.
- Insert then catch duplicate-key race -> 409.

Punch-in:
- Resolve attendance date by R1.
- Unique attendance_logs(emp_code, date) prevents duplicate rows when parallel requests race.
- One insert wins; other requests receive duplicate-key and map to 409.
- Check-then-insert alone is insufficient.

Punch-out:
- Find latest record whose punch_in <= requested timestamp, including across midnight.
- Validate duration and open/closed state.
- Atomic conditional update requires natural key and open punch_out; one request wins.
- Concurrent loser re-reads enough state to return correct 409/404/422.
- Punch-out does not append history.

Regularization:
- Read current state, validate resulting status/times, compute derived values and exact changes.
- Append one history entry and update values in one atomic document update.
- Use optimistic compare-and-set (version if schema permits, or compare current fields/history length) so concurrent edits cannot overwrite one another.
- If CAS fails, return 409; never silently lose history.

## 7. Indexes and explain
Baseline candidates:
- Unique employees.emp_code: identity and employee lookups.
- Unique attendance_logs(emp_code, date): natural-key reads and race-safe punch-in.
- attendance_logs(date descending, emp_code ascending): default attendance list sort/date range.

Consider status/date or employee/date compound indexes only if query shapes and explain show value. Indexes improve reads but add write/storage cost.

The explain endpoint should use the same filter/pipeline builder as its real endpoint, with identical filters, sort, pagination and parameters. Current implementation shares analytics pipelines; attendance-list explain reconstructs its list query shape separately, so keep this parity under review. Use executionStats and return raw output. Inspect IXSCAN/COLLSCAN, keys examined, docs examined and latency. Explain shape varies with MongoDB engine/version. Aggregations starting from employees or a generated date spine need empirical verification; do not promise a no-COLLSCAN plan before testing against the assignment dataset.

## 8. Analytics designs

Employee monthly:
- Confirm employee exists.
- Use stored derived fields from the employee's monthly records.
- Working days = weekdays in month from max(month start, joined_on) through month end.
- present_days includes only weekday presence statuses; half_day contributes 0.5.
- Weekend logs still contribute late/overtime totals.
- Count LEAVE records and late records; sum stored late/overtime.
- attendance_pct = present_days / working_days * 100, four-decimal half-up; null when no working days.

Department summary:
- Headcount is employee-based, includes joined_on <= month end, and includes employees with no logs.
- Join attendance by emp_code and aggregate monthly logs.
- avg_work_hours is record-weighted over presence-status records with non-null work_hours, not average of employee averages.
- Count late records/minutes, LEAVE and ON_DUTY.
- Omit headcount-zero departments and sort department ascending.

Late leaderboard:
- Aggregate stored late_minutes by employee/month; ignore orphan logs.
- Join employees for name and department.
- Apply optional department filter before rank.
- Exclude employees with total <= 0.
- Sort total late minutes descending, emp_code ascending.
- Use competition ranking (1,2,2,4), not array position.
- Apply limit to rank; preserve all ties whose rank <= limit.

Department trend:
- Validate from <= to and inclusive range <=92 days; 404 if department has no employees.
- Build complete date spine in MongoDB, not Python.
- Join daily attendance aggregate and headcount by joined_on.
- Exactly one row per date, including weekends/no-record days.
- Rate is null on weekends or when headcount is zero; no-record weekday with positive headcount has rate 0.
- Moving average is mean of non-null rates in current row plus up to six preceding rows within requested range; early rows use shorter windows.
- Rates/moving average round to four decimals half-up.

## 9. Proposed index matrix
Index | Purpose | Trade-off / verification
- unique employees.emp_code | identity and employee lookup | duplicate existing values would prevent creation; write overhead
- unique attendance_logs(emp_code, date) | natural key and punch-in race guarantee | duplicate existing keys would prevent creation; write overhead
- attendance_logs(date desc, emp_code asc) | default attendance list sort/page | validate actual filter+sort plan
- optional status/date or emp/date compound | filtered list shapes | add only after explain shows benefit

Startup index creation is idempotent. Required index failure should fail clearly rather than silently weakening guarantees.

## 10. Validation/error matrix
- 200: healthy status, list/analytics success, successful punch-out/PATCH.
- 201: employee creation and punch-in.
- 404: unknown employee, no eligible punch-in, missing attendance record, unknown trend department.
- 409: duplicate employee, duplicate employee-day punch-in, already-closed/concurrent punch-out, optimistic concurrency loser.
- 422: invalid timestamp, date/month/time, unsupported status, pagination bounds, invalid range, invalid transition, no-op PATCH.
- 503: health ping cannot reach MongoDB.
Never expose Mongo _id/id or undocumented response fields.

## 11. Test strategy
1. Unit: timestamp validation, UTC/IST conversion, truncation, R1-R5 boundaries, half-up rounding.
2. API contract: all routes, schema validation, defaults, response fields and status codes.
3. Integration: index creation, employee CRUD, filtering/pagination, punch flows, PATCH and nested history.
4. Concurrency: parallel employee create, punch-in, punch-out and PATCH.
5. Analytics: zero-log employees, joiners, weekends, absent/leave, half-days, orphan logs, ties, missing weekdays, empty days and max date range.
6. Explain/performance: all five supported endpoints at realistic scale; verify actual plans and examined document counts.
7. Clean clone: install requirements, start with exact uvicorn command, environment override works, health within 20 seconds.
8. Security: no committed credentials, no destructive seeding on valuable data, no external network use.

## 12. Scaling beyond the assignment
At 100x data, first use explain and telemetry to optimize compound indexes and aggregation shapes. Then consider materialized monthly rollups for recurring expensive analytics, memory/sort constraints, connection-pool tuning, pagination strategy if the contract changes, Atlas capacity, and only later sharding based on measured access patterns. Production additions such as metrics, structured logs, backups, deployment automation and tracing are reasonable follow-ups but must not violate assignment constraints.

## 13. Decision log topics
1. Which indexes were created and why; one rejected index.
2. Exact race path for two simultaneous punch-ins.
3. Why leaderboard ties at rank <= limit are preserved.
4. How zero-log employees remain in headcount.
5. First justified change for 100x data.

## 14. References
- FastAPI testing: https://fastapi.tiangolo.com/tutorial/testing/
- MongoDB indexes: https://www.mongodb.com/docs/manual/indexes/
- MongoDB explain: https://www.mongodb.com/docs/manual/reference/explain-results/
- ECC plan/test/implement/review/verify: https://github.com/affaan-m/ECC
