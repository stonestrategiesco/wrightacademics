import os
import sys
from datetime import datetime as _real_datetime

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class _PinnedDateTime(_real_datetime):
    """sync_reporting reads "today" via datetime.now() (incomplete-month guard,
    cache-freshness warnings, --current-month). Several tests were written
    assuming today is mid-September 2026, so pin it there for every test;
    tests that need a different date replace sync_reporting.datetime
    themselves, which takes precedence for that test."""

    @classmethod
    def now(cls, tz=None):
        return _real_datetime(2026, 9, 15, 12, 0, 0, tzinfo=tz)


@pytest.fixture(autouse=True)
def _pin_reporting_clock(monkeypatch):
    import sync_reporting
    monkeypatch.setattr(sync_reporting, "datetime", _PinnedDateTime)
