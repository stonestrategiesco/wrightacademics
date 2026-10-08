"""Teachworks pagination must never silently truncate a day: Teachworks serves at
most 80 records per page whatever per_page asks for."""

from unittest.mock import MagicMock, patch

import pytest

from teachworks import TeachworksAPIError, TeachworksClient


# --- Teachworks pagination: days with more than 80 lessons ---------------------

def capped_session(day_lessons, cap=80):
    """Serves pages the way Teachworks does: at most `cap` records, whatever per_page asks."""
    session = MagicMock()

    def get(url, headers=None, params=None, timeout=None):
        page = params["page"]
        records = day_lessons[(page - 1) * cap: page * cap]
        resp = MagicMock(status_code=200)
        resp.json.return_value = records
        return resp

    session.get.side_effect = get
    return session


@pytest.mark.parametrize("count, requests", [(87, 2), (80, 2), (160, 3), (79, 1), (0, 1)])
def test_day_with_more_than_80_lessons_is_not_truncated(count, requests):
    lessons = [{"id": i} for i in range(count)]
    session = capped_session(lessons)
    client = TeachworksClient(api_key="k", base_url="https://x", session=session)
    assert client.get_lessons("2026-10-01", "2026-10-01") == lessons      # production default per_page=100
    assert session.get.call_count == requests


def test_page_that_repeats_raises_instead_of_looping_or_truncating():
    session = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = [{"id": i} for i in range(80)]
    session.get.return_value = resp
    client = TeachworksClient(api_key="k", base_url="https://x", session=session)
    with pytest.raises(TeachworksAPIError, match="same records"):
        client.get_lessons("2026-10-01", "2026-10-01")


def test_rate_limit_403_is_retried():
    session = MagicMock()
    limited = MagicMock(status_code=403, text="Rate Limit Exceeded")
    ok = MagicMock(status_code=200)
    ok.json.return_value = [{"id": 1}]
    session.get.side_effect = [limited, ok]
    client = TeachworksClient(api_key="k", base_url="https://x", session=session, retry_base_delay=0)
    with patch("teachworks.time.sleep"):
        assert client.get_lessons("2026-10-01", "2026-10-01") == [{"id": 1}]


def test_delay_between_days_only():
    session = capped_session([])
    client = TeachworksClient(api_key="k", base_url="https://x", session=session, request_delay_seconds=0.5)
    with patch("teachworks.time.sleep") as sleep:
        client.get_lessons("2026-10-01", "2026-10-03")
    assert [c.args[0] for c in sleep.call_args_list] == [0.5, 0.5]
