"""A Teachworks Student ID held by more than one Students board item is never
resolved to either of them: no session is connected to either item, and the ID
plus its Monday item IDs are reported for manual review. Unique IDs match as before."""

import json
from unittest.mock import MagicMock

import config
import student_sync
import sync
from monday_client import MondayClient
from test_reconcile import FakeBoard, FakeTeachworks, row, student, tw_lesson


def real_client(items):
    """MondayClient over a fake HTTP session serving one page of Students items."""
    session = MagicMock()

    def post(url, headers=None, json=None, timeout=None):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": {"boards": [{"items_page": {"cursor": None, "items": [
            {"id": item_id, "name": item_id, "column_values": [{"id": "tw", "text": tw_id, "value": None}]}
            for item_id, tw_id in items]}}]}}
        return resp

    session.post.side_effect = post
    return MondayClient(api_token="t", session=session, max_retries=1)


def test_index_keeps_unique_ids_and_separates_duplicates():
    client = real_client([("s1", "100"), ("s2", "200"), ("s3", "200"), ("s4", ""), ("s5", " 300 "), ("s6", "200")])
    lookup, duplicates = client.get_student_index(18413873041, "tw")
    assert lookup == {"100": "s1", "300": "s5"}
    assert duplicates == {"200": ["s2", "s3", "s6"]}


def test_get_student_lookup_never_picks_one_of_the_duplicates(caplog):
    client = real_client([("s1", "100"), ("s2", "200"), ("s3", "200")])
    assert client.get_student_lookup(18413873041, "tw") == {"100": "s1"}      # previously {"100": "s1", "200": "s3"}
    assert "DUPLICATE_STUDENT_ID teachworks_student_id=200 monday_item_ids=['s2', 's3']" in caplog.text


def board_with_duplicate():
    lessons = [tw_lesson(1, "2026-10-02", [(100, "Ann Lee")]),
               tw_lesson(2, "2026-10-03", [(200, "Bo Diaz")]),
               tw_lesson(3, "2026-10-04", [(200, "Bo Diaz")])]
    rows = [row(1, 3, 200, "2026-10-04", "Bo Diaz", connected=False)]        # existing, unconnected
    students = [student("S_ANN", 100), student("S_BO_1", 200), student("S_BO_2", 200)]
    return FakeTeachworks(lessons), FakeBoard(rows, students)


def test_sessions_for_a_duplicated_id_are_created_but_never_connected():
    tw, board = board_with_duplicate()
    report = sync.reconcile_session_log(tw.client(), board, "2026-10-01", "2026-10-07")

    assert sorted(c["unique_key"] for c in report.created) == ["1_100", "2_200"]   # the session itself is still logged
    assert board.connections == [(board.created_items[0]["id"], "S_ANN")]          # unique ID: connected as before
    assert not any(s in ("S_BO_1", "S_BO_2") for _item, s in board.connections)    # neither duplicate, new or existing row
    assert report.duplicate_student_ids == [
        {"teachworks_id": "200", "item_ids": ["S_BO_1", "S_BO_2"], "sessions": ["2_200", "3_200"]}]
    assert report.missing_students == [] and report.succeeded


def test_duplicate_ids_are_reported_in_the_nightly_and_do_not_fail_it(capsys):
    tw, board = board_with_duplicate()
    assert sync.run_daily_sync(tw.client(), board, "2026-10-01", "2026-10-07") == 0
    out = capsys.readouterr().out
    assert f"{'Duplicate Teachworks IDs on Students board:':<46}1" in out
    assert ("Teachworks ID 200: Students items S_BO_1, S_BO_2; 2 session(s) left unconnected (2_200, 3_200)") in out


def test_rerun_writes_nothing_and_still_reports():
    tw, board = board_with_duplicate()
    sync.reconcile_session_log(tw.client(), board, "2026-10-01", "2026-10-07")
    writes = board.writes()
    again = sync.reconcile_session_log(tw.client(), board, "2026-10-01", "2026-10-07")
    assert board.writes() == writes and len(again.duplicate_student_ids) == 1


def test_once_the_duplicate_is_resolved_the_sessions_connect():
    tw, board = board_with_duplicate()
    sync.reconcile_session_log(tw.client(), board, "2026-10-01", "2026-10-07")
    board.student_items = [s for s in board.student_items if s["item_id"] != "S_BO_2"]   # a person fixes the board
    report = sync.reconcile_session_log(tw.client(), board, "2026-10-01", "2026-10-07")
    assert sorted(c["item_id"] for c in report.connected) == sorted(
        [board.created_items[1]["id"], "1"]) and report.duplicate_student_ids == []
    assert {s for _i, s in board.connections if s.startswith("S_BO")} == {"S_BO_1"}


def test_plain_scheduled_sync_also_never_connects_a_duplicated_id():
    tw, board = board_with_duplicate()
    sync.run_sync(tw.client(), board, "2026-10-02", "2026-10-03")
    assert [s for _i, s in board.connections] == ["S_ANN"]


def test_student_roster_sync_still_reports_the_duplicate_and_creates_nothing(monkeypatch):
    monkeypatch.setattr(config, "STUDENT_SYNC_CREATE", True)
    monkeypatch.setattr(config, "MONDAY_STUDENTS_NEW_GROUP_ID", "g")
    _tw, board = board_with_duplicate()
    roster = [{"id": 100, "first_name": "Ann", "last_name": "Lee"}, {"id": 200, "first_name": "Bo", "last_name": "Diaz"}]
    report = student_sync.sync_students(FakeTeachworks([], roster).client(), board)
    assert report.created == [] and getattr(board, "student_creates", []) == []
    assert any(e["teachworks_id"] == "200" and "2 Students items share this Teachworks Student ID" in e["reason"]
               for e in report.exceptions)
