# GCS SQL generation and execution

When a workbook is scanned, the backend saves generated row queries to the existing bucket `narasimha-test-bucket`.

- `vois-transformsql/sql-input/`: only READY read-only SELECT/WITH queries. This managed folder is refreshed for each generation.
- `vois-transformsql/sql-archive/<run-id>/`: every generated row query plus a manifest.
- `vois-transformsql/sql-review/<run-id>/`: REVIEW/TODO queries that are intentionally excluded from execution.
- `vois-transformsql/execution-reports/`: Excel execution reports created by `backend/bq_executor.py`.

Run from the repository root:

```bash
python -m pip install -r backend/requirements.txt
cd backend
python bq_executor.py
```

The executor blocks actual execution if dry-run fails and accepts only SELECT/WITH queries.
