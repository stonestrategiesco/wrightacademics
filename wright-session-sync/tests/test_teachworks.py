from unittest.mock import MagicMock, patch

import pytest
import requests

from teachworks import TeachworksAPIError, TeachworksClient


def _response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = text
    return resp


def test_request_matches_known_working_zapier_shape():
    """Locks in the request shape recovered from Wright's previously-working
    Zapier implementation: base URL, /lessons path, Authorization header
    ('Token token=<key>'), Accept/Content-Type headers, and from_date/to_date/
    status/page/per_page query params. Single-day range: from_date == to_date,
    confirmed via production diagnostics to be the only shape that works."""
    session = MagicMock()
    session.get.side_effect = [_response(200, [{"id": 1}])]

    client = TeachworksClient(api_key="secret-key-123", base_url="https://api.teachworks.com/v1", session=session)
    client.get_lessons("2026-09-12", "2026-09-12")

    call = session.get.call_args
    assert call.args[0] == "https://api.teachworks.com/v1/lessons"

    headers = call.kwargs["headers"]
    assert headers["Authorization"] == "Token token=secret-key-123"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"

    params = call.kwargs["params"]
    assert params["status"] == "Attended"
    assert params["from_date"] == "2026-09-12"
    assert params["to_date"] == "2026-09-12"
    assert params["page"] == 1
    assert params["per_page"] == 100


def test_single_day_range_makes_exactly_one_request():
    """A single-day range (start_date == end_date) still works as a simple,
    one-request case."""
    session = MagicMock()
    session.get.side_effect = [_response(200, [{"id": 1}, {"id": 2}])]

    client = TeachworksClient(api_key="key", base_url="https://example.com", session=session)
    lessons = client.get_lessons("2026-09-13", "2026-09-13", per_page=100)

    assert session.get.call_count == 1
    assert len(lessons) == 2
    params = session.get.call_args.kwargs["params"]
    assert params["from_date"] == "2026-09-13"
    assert params["to_date"] == "2026-09-13"


def test_multi_day_range_makes_one_request_per_calendar_date():
    """Confirmed via production diagnostics that a single multi-day from_date/
    to_date request returns zero records even when the real data exists, so
    get_lessons must issue one request PER DATE instead. A 3-day range must
    make exactly 3 requests, each with from_date == to_date == that date."""
    session = MagicMock()
    session.get.side_effect = [
        _response(200, [{"id": 1}]),
        _response(200, [{"id": 2}]),
        _response(200, [{"id": 3}]),
    ]

    client = TeachworksClient(api_key="key", base_url="https://example.com", session=session)
    lessons = client.get_lessons("2026-09-13", "2026-09-15", per_page=100)

    assert session.get.call_count == 3
    expected_dates = ["2026-09-13", "2026-09-14", "2026-09-15"]
    for call, expected_date in zip(session.get.call_args_list, expected_dates):
        params = call.kwargs["params"]
        assert params["from_date"] == expected_date
        assert params["to_date"] == expected_date  # identical from_date/to_date on every request

    assert [lesson["id"] for lesson in lessons] == [1, 2, 3]  # all dates' lessons combined


def test_each_date_paginates_independently():
    """Day 1 has two pages of results, day 2 has one short page. Pagination
    must reset to page 1 for each new date, and each date's page count must
    not affect the others."""
    session = MagicMock()
    day1_page1 = [{"id": i} for i in range(2)]   # full page (per_page=2) -> continue
    day1_page2 = [{"id": 99}]                     # short page -> day 1 done
    day2_page1 = [{"id": 100}]                     # short page -> day 2 done
    session.get.side_effect = [
        _response(200, day1_page1),
        _response(200, day1_page2),
        _response(200, day2_page1),
    ]

    client = TeachworksClient(api_key="key", base_url="https://example.com", session=session)
    lessons = client.get_lessons("2026-09-13", "2026-09-14", per_page=2)

    assert session.get.call_count == 3
    calls = session.get.call_args_list
    assert calls[0].kwargs["params"]["from_date"] == "2026-09-13" and calls[0].kwargs["params"]["page"] == 1
    assert calls[1].kwargs["params"]["from_date"] == "2026-09-13" and calls[1].kwargs["params"]["page"] == 2
    assert calls[2].kwargs["params"]["from_date"] == "2026-09-14" and calls[2].kwargs["params"]["page"] == 1
    # all lessons across both dates and both of day 1's pages are combined
    assert [lesson["id"] for lesson in lessons] == [0, 1, 99, 100]


def test_empty_day_does_not_stop_subsequent_dates():
    session = MagicMock()
    session.get.side_effect = [
        _response(200, []),              # 2026-09-13: nothing attended that day
        _response(200, [{"id": 42}]),    # 2026-09-14: one lesson
    ]

    client = TeachworksClient(api_key="key", base_url="https://example.com", session=session)
    lessons = client.get_lessons("2026-09-13", "2026-09-14", per_page=100)

    assert session.get.call_count == 2
    assert lessons == [{"id": 42}]


def test_transient_failure_retries_then_succeeds():
    session = MagicMock()
    session.get.side_effect = [
        _response(500, text="server exploded"),
        _response(200, [{"id": 1}]),
    ]

    client = TeachworksClient(api_key="key", base_url="https://example.com", session=session, max_retries=3, retry_base_delay=0)
    with patch("teachworks.time.sleep"):
        lessons = client.get_lessons("2026-01-01", "2026-01-01", per_page=100)

    assert lessons == [{"id": 1}]
    assert session.get.call_count == 2


def test_transient_network_error_retries_then_succeeds():
    session = MagicMock()
    session.get.side_effect = [
        requests.ConnectionError("connection reset"),
        _response(200, [{"id": 1}]),
    ]

    client = TeachworksClient(api_key="key", base_url="https://example.com", session=session, max_retries=3, retry_base_delay=0)
    with patch("teachworks.time.sleep"):
        lessons = client.get_lessons("2026-01-01", "2026-01-01", per_page=100)

    assert lessons == [{"id": 1}]


def test_permanent_failure_is_surfaced_without_exhausting_retries():
    session = MagicMock()
    session.get.side_effect = [_response(401, text="bad api key")]

    client = TeachworksClient(api_key="bad-key", base_url="https://example.com", session=session, max_retries=5, retry_base_delay=0)
    with pytest.raises(TeachworksAPIError):
        client.get_lessons("2026-01-01", "2026-01-01")

    assert session.get.call_count == 1


def test_permanent_failure_after_exhausting_retries():
    session = MagicMock()
    session.get.side_effect = [_response(503, text="down") for _ in range(3)]

    client = TeachworksClient(api_key="key", base_url="https://example.com", session=session, max_retries=3, retry_base_delay=0)
    with patch("teachworks.time.sleep"):
        with pytest.raises(TeachworksAPIError):
            client.get_lessons("2026-01-01", "2026-01-01")

    assert session.get.call_count == 3


def test_diagnostic_get_does_not_raise_on_error_status_and_reports_it():
    session = MagicMock()
    session.get.side_effect = [_response(404, json_data={"error": "not found"}, text="not found")]

    client = TeachworksClient(api_key="key", base_url="https://api.teachworks.com/v1", session=session)
    status_code, payload, records = client.diagnostic_get("/lessons", {"page": 1, "per_page": 10})

    assert status_code == 404
    assert payload == {"error": "not found"}
    assert records is None
    assert session.get.call_count == 1  # no retries for diagnostics


def test_diagnostic_get_extracts_records_from_bare_list():
    session = MagicMock()
    session.get.side_effect = [_response(200, [{"id": 1}, {"id": 2}])]

    client = TeachworksClient(api_key="key", base_url="https://api.teachworks.com/v1", session=session)
    status_code, payload, records = client.diagnostic_get("/lessons", {"status": "Attended"})

    assert status_code == 200
    assert records == [{"id": 1}, {"id": 2}]


def test_diagnostic_get_extracts_records_from_wrapped_dict():
    session = MagicMock()
    session.get.side_effect = [_response(200, {"data": [{"id": 1}]})]

    client = TeachworksClient(api_key="key", base_url="https://api.teachworks.com/v1", session=session)
    status_code, payload, records = client.diagnostic_get("/lessons", {"status": "Attended"})

    assert records == [{"id": 1}]


def test_normalize_participant_matches_confirmed_2026_09_13_production_response():
    """Fixture modeled directly on the real Teachworks response for the
    2026-09-13 lesson/participant confirmed via production diagnostics."""
    lesson = {
        "id": 93279926,
        "from_date": "2026-09-13",
        "from_time": "11:00:00",
        "employee_name": "Monsueir - Young, Mary",
        "employee_id": 236943,
        "service_name": "EXECUTIVE FUNCTIONING TUTORING ",
        "service_id": 115858,
        "location_name": "*Room A",
        "status": "Attended",
    }
    participant = {
        "student_name": "Heyne, Jacob",
        "student_id": 2233786,
        "status": "Attended",
        "lesson_id": 93279926,
    }

    session = TeachworksClient.normalize_participant(lesson, participant)

    assert session["lesson_id"] == 93279926
    assert session["student_id"] == 2233786
    assert session["student_name"] == "Heyne, Jacob"
    assert session["session_date"] == "2026-09-13"
    assert session["tutor"] == "Monsueir - Young, Mary"
    assert session["service"] == "EXECUTIVE FUNCTIONING TUTORING "
    assert session["location"] == "*Room A"
    assert session["unique_key"] == "93279926_2233786"
    assert TeachworksClient._is_attended(participant) is True


def test_extract_attended_sessions_only_includes_attended_participants():
    lessons = [
        {
            "id": 1,
            "date": "2026-01-05",
            "tutor_name": "Jane",
            "service_name": "Math",
            "location_name": "Online",
            "duration": 60,
            "participants": [
                {"student_id": 1, "student_name": "Alice", "attended": True, "price": 45},
                {"student_id": 2, "student_name": "Bob", "attended": False, "price": 45},
                {"student_id": 3, "student_name": "Carl", "status": "no_show", "price": 45},
            ],
        }
    ]

    sessions = TeachworksClient.extract_attended_sessions(lessons)

    assert len(sessions) == 1
    assert sessions[0]["unique_key"] == "1_1"
    assert sessions[0]["student_name"] == "Alice"
