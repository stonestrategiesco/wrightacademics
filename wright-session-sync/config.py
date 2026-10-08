"""
Configuration and constants for the Wright Academics Teachworks -> Monday.com sync.

All secrets come from environment variables. Never hardcode credentials here.
Board IDs and column IDs are fixed per the client's Monday.com setup and are
intentionally hardcoded (per spec) rather than made configurable, since changing
them would require reworking the board itself.
"""

import os

from dotenv import load_dotenv

# Loads variables from a local .env file if present (no-op in production
# environments like Railway where env vars are set directly).
load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


# ---------------------------------------------------------------------------
# Credentials / environment (never hardcode these)
# ---------------------------------------------------------------------------

TEACHWORKS_API_KEY = os.environ.get("TEACHWORKS_API_KEY", "")
TEACHWORKS_BASE_URL = os.environ.get("TEACHWORKS_BASE_URL", "https://api.teachworks.com/v1")
MONDAY_API_TOKEN = os.environ.get("MONDAY_API_TOKEN", "")

# ---------------------------------------------------------------------------
# Sync window
# ---------------------------------------------------------------------------

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "3"))

# --daily-sync reconciles the Session Log against Teachworks for the last
# RECONCILE_DAYS calendar days through today: creates attended sessions that
# are missing, corrects drifted fields, and reports rows Teachworks no longer
# has as attended. Re-checking an already-correct day changes nothing.
RECONCILE_DAYS = int(os.environ.get("RECONCILE_DAYS", "30"))

# The reconciliation window never starts before this date. Every session on or
# after it is post-baseline, so creating it without Pre-Baseline (as the nightly
# does) is correct; earlier gaps need the baseline-aware September repair.
RECONCILE_FLOOR_DATE = os.environ.get("RECONCILE_FLOOR_DATE", "2026-10-01")

# Integrity limit: if more Session Log rows than this in the window are not
# attended in Teachworks, the Teachworks data is treated as suspect - nothing
# is flagged and the run fails, instead of acting on a partial response.
RECONCILE_MAX_STALE_ROWS = int(os.environ.get("RECONCILE_MAX_STALE_ROWS", "10"))

# Pause between per-day Teachworks /lessons requests (~31 a night).
TEACHWORKS_REQUEST_DELAY_SECONDS = float(os.environ.get("TEACHWORKS_REQUEST_DELAY_SECONDS", "0.5"))

# Start date used for --full reconciliation. Configurable because "all of
# history" means different things for different Teachworks accounts.
FULL_SYNC_START_DATE = os.environ.get("FULL_SYNC_START_DATE", "2020-01-01")

# ---------------------------------------------------------------------------
# HTTP behavior
# ---------------------------------------------------------------------------

REQUEST_TIMEOUT_SECONDS = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "30"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "5"))
RETRY_BASE_DELAY_SECONDS = float(os.environ.get("RETRY_BASE_DELAY_SECONDS", "1"))

# ---------------------------------------------------------------------------
# Monday.com board / column configuration (fixed by client spec — do not change)
# ---------------------------------------------------------------------------

MONDAY_SESSIONS_BOARD_ID = 18423473385
MONDAY_STUDENTS_BOARD_ID = 18413873041
MONDAY_SESSION_GROUP_ID = "topics"

COL_SESSION_DATE = "date_mm5h3b41"
COL_STUDENT_NAME = "text_mm5h2kvj"
COL_TEACHWORKS_STUDENT_ID = "text_mm5he28n"
COL_TUTOR = "text_mm5henbg"
COL_SERVICE = "text_mm5hk1xg"
COL_DURATION = "numeric_mm5h68gd"
COL_LOCATION = "text_mm5hpbmt"
COL_UNIQUE_ID = "text_mm5h9n9g"
COL_STUDENT_CONNECTION = "board_relation_mm5q6eh3"
COL_AMOUNT = "numeric_mm5r8r3g"

STUDENT_BOARD_COL_TEACHWORKS_ID = "text_mm3gj3hy"

# Confirmed via `sync.py --diagnose-student-columns` against the real
# Students board. Used for the read-only rollup diagnostic; no write code
# exists yet for any of these.
STUDENT_COL_FIRST_SESSION_DATE = "date_mm5g76js"
STUDENT_COL_SESSION_COUNT = "numeric_mm4cpxr0"
STUDENT_COL_LAST_SESSION_DATE = "date_mm4cgyym"
STUDENT_COL_TUTOR = "text_mm5g2f0e"
STUDENT_COL_SESSION_DATA_LAST_SYNCED = "date_mm5gk46f"
# Confirmed but intentionally NOT read or written by any rollup code yet.
STUDENT_COL_MILESTONES = "color_mm4c964d"

# ---------------------------------------------------------------------------
# Baseline + SET migration (new architecture, not yet built)
# ---------------------------------------------------------------------------
# Confirmed via `sync.py --diagnose-baseline-migration-columns` against the
# real boards. No migration writes exist yet - these are read by
# diagnose_baseline_migration() only.
STUDENT_COL_HISTORICAL_BASELINE = "numeric_mm785914"
COL_PRE_BASELINE = "boolean_mm78a02"

# OPTIONAL, not yet created on the board. A text column on the Session Log
# (e.g. "Teachworks Sync Flag"). While unset, Session Log rows that Teachworks
# no longer has as attended are only REPORTED by --daily-sync. Once set, the
# nightly writes a note into it for such rows (clears it if Teachworks shows
# the session attended again), and the student rollup stops counting flagged
# rows. Nothing is ever deleted or archived automatically.
COL_TEACHWORKS_SYNC_FLAG = os.environ.get("MONDAY_SESSION_COL_SYNC_FLAG", "")

# ---------------------------------------------------------------------------
# Student roster sync (--daily-sync step 1)
# ---------------------------------------------------------------------------
# Every Teachworks student should have a Students board item (matched by
# Teachworks Student ID). Creation is OFF until STUDENT_SYNC_CREATE=true: until
# then every run (live or dry) only reports who WOULD be created.
STUDENT_SYNC_CREATE = os.environ.get("STUDENT_SYNC_CREATE", "false").strip().lower() == "true"
# Students board group new students are created in. Required for creation.
MONDAY_STUDENTS_NEW_GROUP_ID = os.environ.get("MONDAY_STUDENTS_NEW_GROUP_ID", "")
# More creations than this in one run = something is off (e.g. a roster or
# board read problem): nothing is created and the run is flagged.
STUDENT_SYNC_MAX_CREATES = int(os.environ.get("STUDENT_SYNC_MAX_CREATES", "25"))


def validate():
    """Raise ConfigError with a clear message if required credentials are missing.

    Called explicitly at CLI startup (not at import time) so tests can import
    this module freely without needing real credentials set.
    """
    missing = []
    if not TEACHWORKS_API_KEY:
        missing.append("TEACHWORKS_API_KEY")
    if not MONDAY_API_TOKEN:
        missing.append("MONDAY_API_TOKEN")
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". See .env.example."
        )
