from unittest.mock import MagicMock, patch

import pytest

from monday_client import MondayAPIError, MondayClient


def _response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = text
    return resp


def _items_page(items, cursor=None):
    return {"data": {"boards": [{"items_page": {"cursor": cursor, "items": items}}]}}


def _next_items_page(items, cursor=None):
    return {"data": {"next_items_page": {"cursor": cursor, "items": items}}}


def test_get_existing_unique_ids_paginates_fully():
    session = MagicMock()
    session.post.side_effect = [
        _response(200, _items_page(
            [{"id": "1", "name": "a", "column_values": [{"id": "uid", "text": "111_1", "value": None}]}],
            cursor="cursor-1",
        )),
        _response(200, _next_items_page(
            [{"id": "2", "name": "b", "column_values": [{"id": "uid", "text": "222_2", "value": None}]}],
            cursor=None,
        )),
    ]

    client = MondayClient(api_token="token", session=session)
    ids = client.get_existing_unique_ids(18423473385, "uid")

    assert ids == {"111_1", "222_2"}
    assert session.post.call_count == 2


def test_get_board_columns_returns_title_id_and_type():
    session = MagicMock()
    session.post.return_value = _response(200, {
        "data": {
            "boards": [{
                "columns": [
                    {"id": "text_mm3gj3hy", "title": "Teachworks Student ID", "type": "text"},
                    {"id": "numeric_abc123", "title": "Session Count", "type": "numeric"},
                ],
            }],
        },
    })

    client = MondayClient(api_token="token", session=session)
    columns = client.get_board_columns(18413873041)

    assert columns == [
        {"id": "text_mm3gj3hy", "title": "Teachworks Student ID", "type": "text"},
        {"id": "numeric_abc123", "title": "Session Count", "type": "numeric"},
    ]
    assert session.post.call_count == 1


def test_get_board_columns_raises_if_board_not_found():
    session = MagicMock()
    session.post.return_value = _response(200, {"data": {"boards": []}})

    client = MondayClient(api_token="token", session=session)
    with pytest.raises(MondayAPIError):
        client.get_board_columns(18413873041)


def test_get_items_returns_multiple_columns_per_item_and_paginates():
    session = MagicMock()
    session.post.side_effect = [
        _response(200, _items_page(
            [{
                "id": "1", "name": "a",
                "column_values": [
                    {"id": "uid", "text": "111_1", "value": None},
                    {"id": "date", "text": "2026-09-13", "value": None},
                ],
            }],
            cursor="cursor-1",
        )),
        _response(200, _next_items_page(
            [{
                "id": "2", "name": "b",
                "column_values": [
                    {"id": "uid", "text": "", "value": None},
                    {"id": "date", "text": "2026-09-14", "value": None},
                ],
            }],
            cursor=None,
        )),
    ]

    client = MondayClient(api_token="token", session=session)
    items = client.get_items(18423473385, ["uid", "date"])

    assert session.post.call_count == 2
    assert items == [
        {"item_id": "1", "item_name": "a", "columns": {"uid": "111_1", "date": "2026-09-13"}},
        {"item_id": "2", "item_name": "b", "columns": {"uid": "", "date": "2026-09-14"}},
    ]


def test_get_student_lookup_paginates_fully_and_skips_blank_ids():
    session = MagicMock()
    session.post.side_effect = [
        _response(200, _items_page(
            [
                {"id": "s1", "name": "Alice", "column_values": [{"id": "tw", "text": "789", "value": None}]},
                {"id": "s2", "name": "NoTeachworksId", "column_values": [{"id": "tw", "text": "", "value": None}]},
            ],
            cursor=None,
        )),
    ]

    client = MondayClient(api_token="token", session=session)
    lookup = client.get_student_lookup(18413873041, "tw")

    assert lookup == {"789": "s1"}


def test_transient_http_error_retries_then_succeeds():
    session = MagicMock()
    session.post.side_effect = [
        _response(500, text="internal error"),
        _response(200, {"data": {"create_item": {"id": "999"}}}),
    ]

    client = MondayClient(api_token="token", session=session, max_retries=3, retry_base_delay=0)
    with patch("monday_client.time.sleep"):
        result = client._execute("mutation {...}", {})

    assert result == {"create_item": {"id": "999"}}
    assert session.post.call_count == 2


def test_retryable_graphql_error_retries_then_succeeds():
    session = MagicMock()
    session.post.side_effect = [
        _response(200, {"errors": [{"message": "Complexity budget exhausted"}]}),
        _response(200, {"data": {"create_item": {"id": "1"}}}),
    ]

    client = MondayClient(api_token="token", session=session, max_retries=3, retry_base_delay=0)
    with patch("monday_client.time.sleep"):
        result = client._execute("mutation {...}", {})

    assert result == {"create_item": {"id": "1"}}
    assert session.post.call_count == 2


def test_permanent_graphql_error_is_surfaced_immediately():
    session = MagicMock()
    session.post.side_effect = [
        _response(200, {"errors": [{"message": "Column 'bogus' not found on board"}]}),
    ]

    client = MondayClient(api_token="token", session=session, max_retries=5, retry_base_delay=0)
    with pytest.raises(MondayAPIError):
        client._execute("mutation {...}", {})

    assert session.post.call_count == 1


def test_create_session_item_sends_expected_mutation_shape():
    session = MagicMock()
    session.post.return_value = _response(200, {"data": {"create_item": {"id": "42"}}})

    client = MondayClient(api_token="token", session=session)
    item_id = client.create_session_item(
        18423473385, "topics", "Alice - 2026-01-05",
        {"text_mm5h9n9g": "1_1", "date_mm5h3b41": {"date": "2026-01-05"}, "skip_me": None},
    )

    assert item_id == "42"
    sent_body = session.post.call_args.kwargs["json"]
    assert "skip_me" not in sent_body["variables"]["columnValues"]
    assert "1_1" in sent_body["variables"]["columnValues"]


def test_update_student_columns_sends_expected_mutation_shape():
    session = MagicMock()
    session.post.return_value = _response(200, {"data": {"change_multiple_column_values": {"id": "999"}}})

    client = MondayClient(api_token="token", session=session)
    item_id = client.update_student_columns(
        18413873041, "999",
        {"numeric_mm4cpxr0": 12, "date_mm4cgyym": {"date": "2026-09-13"}, "text_mm5g2f0e": "Jane", "skip_me": None},
    )

    assert item_id == "999"
    sent_body = session.post.call_args.kwargs["json"]
    assert sent_body["variables"]["boardId"] == "18413873041"
    assert sent_body["variables"]["itemId"] == "999"
    assert "skip_me" not in sent_body["variables"]["columnValues"]
    assert "Jane" in sent_body["variables"]["columnValues"]
    assert "change_multiple_column_values" in sent_body["query"]
