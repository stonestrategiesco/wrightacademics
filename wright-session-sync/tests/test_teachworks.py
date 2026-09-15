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
    status/page/per_page query params."""
    session = MagicMock()
    session.get.side_effect = [_response(200, [{"id": 1}])]

    client = TeachworksClient(api_key="secret-key-123", base_url="https://api.teachworks.com/v1", session=session)
    client.get_lessons("2026-09-12", "2026-09-15")

    call = session.get.call_args
    assert call.args[0] == "https://api.teachworks.com/v1/lessons"

    headers = call.kwargs["headers"]
    assert headers["Authorization"] == "Token token=secret-key-123"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"

    params = call.kwargs["params"]
    assert params["status"] == "Attended"
    assert params["from_date"] == "2026-09-12"
    assert params["to_date"] == "2026-09-15"
    assert params["page"] == 1
    assert params["per_page"] == 100


def test_pagination_retrieves_all_pages():
    session = MagicMock()
    page1 = [{"id": i} for i in range(100)]
    page2 = [{"id": i} for i in range(100, 150)]
    session.get.side_effect = [
        _response(200, page1),
        _response(200, page2),
    ]

    client = TeachworksClient(api_key="key", base_url="https://example.com", session=session, max_retries=3)
    lessons = client.get_lessons("2026-01-01", "2026-01-31", per_page=100)

    assert len(lessons) == 150
    assert session.get.call_count == 2
    first_call_params = session.get.call_args_list[0].kwargs["params"]
    second_call_params = session.get.call_args_list[1].kwargs["params"]
    assert first_call_params["page"] == 1
    assert second_call_params["page"] == 2


def test_pagination_stops_on_short_final_page_even_if_only_one_page():
    session = MagicMock()
    session.get.side_effect = [_response(200, [{"id": 1}])]

    client = TeachworksClient(api_key="key", base_url="https://example.com", session=session)
    lessons = client.get_lessons("2026-01-01", "2026-01-31", per_page=100)

    assert len(lessons) == 1
    assert session.get.call_count == 1


def test_transient_failure_retries_then_succeeds():
    session = MagicMock()
    session.get.side_effect = [
        _response(500, text="server exploded"),
        _response(200, [{"id": 1}]),
    ]

    client = TeachworksClient(api_key="key", base_url="https://example.com", session=session, max_retries=3, retry_base_delay=0)
    with patch("teachworks.time.sleep"):
        lessons = client.get_lessons("2026-01-01", "2026-01-31", per_page=100)

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
        lessons = client.get_lessons("2026-01-01", "2026-01-31", per_page=100)

    assert lessons == [{"id": 1}]


def test_permanent_failure_is_surfaced_without_exhausting_retries():
    session = MagicMock()
    session.get.side_effect = [_response(401, text="bad api key")]

    client = TeachworksClient(api_key="bad-key", base_url="https://example.com", session=session, max_retries=5, retry_base_delay=0)
    with pytest.raises(TeachworksAPIError):
        client.get_lessons("2026-01-01", "2026-01-31")

    assert session.get.call_count == 1


def test_permanent_failure_after_exhausting_retries():
    session = MagicMock()
    session.get.side_effect = [_response(503, text="down") for _ in range(3)]

    client = TeachworksClient(api_key="key", base_url="https://example.com", session=session, max_retries=3, retry_base_delay=0)
    with patch("teachworks.time.sleep"):
        with pytest.raises(TeachworksAPIError):
            client.get_lessons("2026-01-01", "2026-01-31")

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
