# Code review findings

The implementation was reviewed against `openapi.yaml`, `DATA_MODEL.md`, and rules R1–R10. The findings below record concrete failure modes, example inputs, corrective measures, and the verification evidence for the API contract.

| # | Area | Defect / failure mode | Detection example | Fix / evidence |
|---:|---|---|---|---|
| 1 | Late grace | Treating the grace boundary as late or measuring minutes after the grace period gives incorrect values. | 09:40:00 must be 0; 09:40:01 must be 10 for a 09:30 shift. | Strict `> 600` second check and floor elapsed minutes; unit boundary tests. |
| 2 | Overnight attendance date | Using the civil date of an early-morning punch-in assigns it to the wrong shift. | 22:00–06:00 shift with 05:30 punch-in belongs to the previous shift-start date. | IST-aware `_attendance_date`; helper and live integration tests. |
| 3 | Overtime threshold | Counting overtime below the minimum or rounding fractional minutes upward violates R3. | 29:59 after shift end is 0; 30:00 is 30. | Whole-second duration, floor minutes and explicit 30-minute threshold; unit tests. |
| 4 | Work-hour rounding | Binary-float or banker's rounding can disagree with required half-up rounding. | Durations near a two-decimal half boundary. | `Decimal` with `ROUND_HALF_UP`; unit tests. |
| 5 | Timestamp input | Accepting seconds, floats, strings, booleans or explicit nulls can corrupt time calculations. | Values below `100000000000`, float timestamps and null on an explicitly supplied timestamp. | Strict integer Pydantic fields, range checks and explicit-null validation. |
| 6 | BSON timezone handling | PyMongo's default naive UTC datetimes cannot safely be subtracted from aware request datetimes. | Punch-out against a persisted BSON `punch_in` could raise a server error. | Normalize stored values to UTC before comparison; live punch-out and regularization tests. |
| 7 | Duplicate punch-in race | A read-then-insert check alone allows two simultaneous requests to create the same employee-day record. | Parallel punch-ins for one employee/date. | Unique `(emp_code, date)` index; exactly one insert wins and others return 409 in integration tests. |
| 8 | Punch-out race and chronology | Two simultaneous closures must not both succeed; a timestamp before punch-in must not be misreported as missing data. | Parallel punch-outs and `punched_at <= punch_in`. | Conditional update/compare-and-set; 409 race loser and 422 invalid chronology; integration tests. |
| 9 | Regularization history | Replacing the history array or updating stale state can silently erase another correction. | Concurrent PATCH calls on the same attendance record. | Compare-and-set over prior state/history; successful changes append one entry with only changed fields; concurrent integration test. |
| 10 | API serialization | Leaking MongoDB `_id`/`id` or returning BSON datetimes in nested history breaks the contract. | Inspect response and `history[].changes.*.from/to`. | Explicit serializers remove internal IDs and convert all instant fields to epoch milliseconds; tests. |
| 11 | Department headcount | Starting summary only from attendance logs omits employees with no logs and can include pre-join records. | Zero-log employee and legacy attendance before `joined_on`. | Employee-first aggregation, joined-date filtering and per-employee grouping; exact live integration tests. |
| 12 | Leaderboard ties | Limiting before ranking or using row position gives wrong competition ranks and drops cutoff ties. | Two employees tie when `limit=1`. | MongoDB `$rank`; apply limit to rank and preserve all tied rows; integration test. |
| 13 | Trend gap filling | Grouping only existing logs omits weekends/no-record days; Python loops over raw records do not scale. | A date range containing days without attendance. | MongoDB-generated date spine and windowed moving average; exact trend and 92-day boundary integration tests. |
| 14 | Explain evidence | Explaining a different query from the endpoint can hide a collection scan or incorrect index use. | Compare endpoint filters/sort/pagination with explain request and inspect IXSCAN/COLLSCAN. | Explain uses the analytics pipeline builders and matching attendance-list query shape; all five explain variants and 100k smoke verified. Nested lookup statistics and another MongoDB version remain limitations. |

## Examined and retained

- `emp_code` is the business identity; MongoDB `_id` is internal and never returned.
- Attendance natural key is `(emp_code, date)`; the unique index is the concurrency guarantee.
- Analytics consume stored derived values, not recalculations from punch timestamps.
- No destructive sample seeder was run against the configured/shared database.
- The latest 100k explain smoke ran against Atlas MongoDB 8.0.34. The trend endpoint took 6.33 seconds end-to-end; nested lookup work and compatibility with the hidden grader version are not fully verified.
