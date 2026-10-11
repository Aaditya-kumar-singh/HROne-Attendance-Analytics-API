# DECISIONS.md

1. **Indexes.** A unique index on `employees.emp_code` enforces employee identity. A unique `attendance_logs(emp_code, date)` index enforces one record per employee per attendance day. A date-descending / employee-ascending index supports attendance listing. Additional date/late and employee-join indexes support the analytics query shapes. I would avoid adding more indexes until explain plans and write overhead justify them.

2. **Punch-in race.** Both requests may read the employee, but neither relies on that read to reserve the day. Both try to insert the same natural key. MongoDB's unique index permits one insert; the other receives a duplicate-key error and the API returns 409.

3. **Ties.** The leaderboard uses MongoDB `$rank` ordered by total late minutes. Employees tied at the cutoff share a rank and all rows with rank at most `limit` are returned.

4. **Headcount.** The department summary starts from employees joined by month-end, then looks up their logs. Employees with no matching logs remain in the pipeline, so they still count.

5. **100x scale.** I would profile representative data and explain plans first, then consider pre-aggregated monthly summaries or a reporting store. I would keep raw attendance records authoritative and measure the freshness trade-off before adding caching.
