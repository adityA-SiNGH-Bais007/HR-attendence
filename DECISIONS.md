# DECISIONS.md

1. **Indexes:** Unique index on `employees.emp_code` enforces unique IDs. Compound unique index on `attendance_logs(emp_code, date)` guarantees one punch-in daily. Index on `(date, emp_code)` optimizes pagination and date-range analytics. Indexing `history` was rejected as audit logs are appended, not queried.

2. **Punch-in race:** Simultaneous punch-ins compute the attendance date concurrently. MongoDB's unique index on `(emp_code, date)` serializes insertion: the first succeeds (HTTP 201), while the second throws `DuplicateKeyError`, returning HTTP 409 without check-then-insert races.

3. **Cutoff ties:** `$setWindowFields` with `$rank` assigns identical ranks to ties and skips subsequent numbers (1, 2, 2, 4). Filtering `rank <= limit` post-ranking returns all tied candidates at the boundary, ordered by `emp_code`.

4. **Headcount with zero logs:** Aggregation starts from `employees` filtered by joining date, joining `attendance_logs` via `$lookup`. Unwinding with `preserveNullAndEmptyArrays: true` preserves zero-log staff in headcount counts.

5. **100x scale change:** Pre-aggregate daily metrics into bucketed summary collections using MongoDB change streams, eliminating real-time `$lookup` joins across millions of logs.
