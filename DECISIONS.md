# DECISIONS.md

1. **Indexes.** Created unique index `employees.emp_code` (guarantees employee uniqueness) and compound unique index `attendance_logs.(emp_code, date)` (enforces one record per day per employee and prevents punch-in race conditions). Created `attendance_logs.(date, emp_code)` for sorted pagination and `attendance_logs.(late_minutes, date)` for leaderboard queries. Considered and rejected an index on `history.at` because `history` is an embedded array within attendance logs, not queried independently.

2. **Punch-in race.** When two identical punch-in requests arrive at the same instant, both convert the timestamp to the attendance date. Both attempt `db.attendance_logs.insert_one()`. MongoDB's atomic index check on `(emp_code, date)` allows exactly one insert to succeed (returning 201), while the second fails with a `DuplicateKeyError`. The exception handler catches this and returns HTTP 409 (`already punched in for this date`).

3. **Ties.** The leaderboard uses standard competition ranking (`$rank` in `$setWindowFields`). If two employees tie at rank $k \le \text{limit}$, both are assigned rank $k$ and returned. The `limit` filter (`rank <= limit`) applies after ranking, expanding the response set so tied employees are never arbitrarily truncated. Ties are deterministically ordered by `emp_code` ascending.

4. **Headcount.** Department summary aggregates starting from the `employees` collection (filtering `joined_on <= end_of_month`) and `$lookup` joins matching `attendance_logs`. It then performs `$unwind` with `preserveNullAndEmptyArrays: True` and groups by department counting `$addToSet: "$emp_code"`, ensuring zero-log employees contribute to headcount.

5. **100x Data Scaling.** At 100x scale (~10M logs), pre-aggregating daily/monthly metrics asynchronously via a background worker into a cached summary collection would replace dynamic full-collection aggregation pipelines.
