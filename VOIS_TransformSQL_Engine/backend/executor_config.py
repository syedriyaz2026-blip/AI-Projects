"""Configuration for executing generated SQL files from GCS."""

import os


def env_bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def env_optional_int(name: str, default: str):
    value = os.getenv(name, default).strip()
    return int(value) if value else None


# ============================================================
# Project configuration
# ============================================================

RUNTIME_PROJECT_ID = os.getenv(
    "GOOGLE_CLOUD_PROJECT",
    "vf-grp-gbissdbx-dev-1",
)

BIGQUERY_JOB_PROJECT_ID = os.getenv(
    "BIGQUERY_PROJECT",
    "vf-grp-gbissdbx-dev-1",
)

BIGQUERY_DATA_PROJECT_ID = os.getenv(
    "BIGQUERY_DATA_PROJECT",
    BIGQUERY_JOB_PROJECT_ID,
)

# Compatibility aliases used by existing executor versions.
PROJECT_ID = BIGQUERY_JOB_PROJECT_ID
EXECUTION_PROJECT_ID = BIGQUERY_JOB_PROJECT_ID
DATA_PROJECT_ID = BIGQUERY_DATA_PROJECT_ID


# ============================================================
# BigQuery source and target
# ============================================================

SOURCE_PROJECT_ID = os.getenv(
    "BIGQUERY_SOURCE_PROJECT",
    "vf-grp-gbissdbx-dev-1",
)

TARGET_PROJECT_ID = os.getenv(
    "BIGQUERY_TARGET_PROJECT",
    "vf-grp-gbissdbx-dev-1",
)

SOURCE_DATASET = os.getenv(
    "BIGQUERY_SOURCE_DATASET",
    "Dummy_GR",
)

TARGET_DATASET = os.getenv(
    "BIGQUERY_TARGET_DATASET",
    "Dummy_GR",
)

SOURCE_TABLE = os.getenv(
    "BIGQUERY_SOURCE_TABLE",
    "Intermediate",
)

TARGET_TABLE = os.getenv(
    "BIGQUERY_TARGET_TABLE",
    "Target",
)

LOCATION = os.getenv(
    "BIGQUERY_LOCATION",
    "EU",
)


# ============================================================
# GCS configuration
# ============================================================

INPUT_BUCKET = os.getenv(
    "GCS_RESULTS_BUCKET",
    "narasimha-test-bucket",
)

INPUT_FOLDER = os.getenv(
    "GCS_SQL_INPUT_PREFIX",
    "vois-transformsql/sql-input",
)

OUTPUT_BUCKET = os.getenv(
    "GCS_RESULTS_BUCKET",
    "narasimha-test-bucket",
)

OUTPUT_FOLDER = os.getenv(
    "GCS_EXECUTION_REPORTS_PREFIX",
    "vois-transformsql/execution-reports",
)


# ============================================================
# Execution controls
# ============================================================

ENABLE_DRY_RUN = env_bool(
    "TRANSFORMSQL_DRY_RUN_ENABLED",
    "true",
)

CONTINUE_ON_ERROR = env_bool(
    "CONTINUE_ON_ERROR",
    "false",
)

QUERY_TIMEOUT_SECONDS = int(
    os.getenv("QUERY_TIMEOUT_SECONDS", "1800")
)

MAX_RESULT_ROWS = env_optional_int(
    "BIGQUERY_MAX_ROWS",
    "5000",
)

MAXIMUM_BYTES_BILLED = env_optional_int(
    "BIGQUERY_MAX_BYTES_BILLED",
    "1000000000",
)
