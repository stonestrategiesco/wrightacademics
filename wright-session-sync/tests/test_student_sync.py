"""Students board roster sync: every Teachworks student gets exactly one Students
item, created only when nothing could already be them; everything uncertain is
an exception for review. Real TeachworksClient over a fake HTTP layer; stateful
fake Monday board (shared with test_reconcile)."""

import pytest

import config
import student_sync
import sync
from test_reconcile import FakeBoard, FakeTeachworks, row, student, tw_lesson

GROUP = "group_new_students"


def tw_student(sid, first, last, status="Active"):
    return {"id": sid, "first_name": first, "last_name": last, "status": status}


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(config, "STUDENT_SYNC_CREATE", True)
    monkeypatch.setattr(config, "MONDAY_STUDENTS_NEW_GROUP_ID", GROUP)
    monkeypatch.setattr(config, "STUDENT_SYNC_MAX_CREATES", 25)
    monkeypatch.setattr(config, "COL_TEACHWORKS_SYNC_FLAG", "")


def named(item_id, name, sid=None):
    item = student(item_id, sid if sid is not None else "")
    item["item_name"] = name
    return item


def run(students, board, dry_run=False):
    return student_sync.sync_students(FakeTeachworks([], students).client(), board, dry_run=dry_run)


def test_matched_by_id_and_clean_new_student_created_with_only_id_and_baseline():
    board = FakeBoard([row(1, 500, 2, "2026-08-03", "Bo Diaz", pre_baseline=True, connected=False),
                       row(2, 501, 2, "2026-08-10", "Bo Diaz", pre_baseline=True, connected=False),
                       row(3, 502, 2, "2026-10-02", "Bo Diaz", connected=False)],
                      [named("S1", "Ann Lee", 1)])
    report = run([tw_student(1, "Ann", "Lee"), tw_student(2, "Bo", "Diaz")], board)
    assert report.matched == 1 and report.exceptions == [] and report.succeeded
    assert board.student_creates == [{"group_id": GROUP, "item_name": "Bo Diaz", "column_values": {
        config.STUDENT_BOARD_COL_TEACHWORKS_ID: "2", config.STUDENT_COL_HISTORICAL_BASELINE: 2}}]
    assert sorted(item for item, _s in board.connections) == ["1", "2", "3"]   # all of Bo's rows, any date
    assert report.sessions_connected == 3 and board.student_updates == []


def test_running_twice_creates_nothing_the_second_time():
    board = FakeBoard([], [named("S1", "Ann Lee", 1)])
    students = [tw_student(1, "Ann", "Lee"), tw_student(2, "Bo", "Diaz")]
    run(students, board)
    writes = board.writes()
    second = run(students, board)
    assert board.writes() == writes and second.to_create == [] and second.matched == 2


@pytest.mark.parametrize("monday_name, monday_id, reason", [
    ("Bo Diaz", None, "has no Teachworks ID"),                 # likely the same student, ID never filled in
    ("Diaz, Bo", None, "has no Teachworks ID"),                # word order / punctuation
    ("bo  DIAZ", None, "has no Teachworks ID"),                # case / spacing
    ("Bo Martin Diaz", None, "has no Teachworks ID"),          # middle name
    ("Bo Diaz", 99, "has Teachworks ID 99"),                   # same name, different Teachworks student
])
def test_possible_duplicate_on_monday_is_an_exception_not_a_create(monday_name, monday_id, reason):
    board = FakeBoard([], [named("S9", monday_name, monday_id)])
    report = run([tw_student(2, "Bo", "Diaz")] + ([tw_student(99, "Bo", "Diaz")] if monday_id else []), board)
    exc = next(e for e in report.exceptions if e["teachworks_id"] == "2")
    assert f"Students item S9 {monday_name!r} {reason}" in exc["reason"]
    assert getattr(board, "student_creates", []) == [] and report.to_create == []


def test_two_teachworks_students_with_the_same_name_are_both_exceptions():
    board = FakeBoard([], [])
    report = run([tw_student(2, "Bo", "Diaz"), tw_student(3, "Bo", "Diaz")], board)
    assert sorted(e["teachworks_id"] for e in report.exceptions) == ["2", "3"]
    assert getattr(board, "student_creates", []) == []


def test_unrelated_shared_surname_is_not_a_duplicate():
    board = FakeBoard([], [named("S1", "Ann Diaz", 1)])
    report = run([tw_student(1, "Ann", "Diaz"), tw_student(2, "Bo", "Diaz")], board)
    assert [p["name"] for p in report.created] == ["Bo Diaz"] and report.exceptions == []


def test_missing_name_and_duplicate_monday_ids_are_exceptions():
    board = FakeBoard([], [named("S1", "Ann Lee", 1), named("S2", "Ann Lee (dup)", 1)])
    report = run([tw_student(1, "Ann", "Lee"), tw_student(4, "Cher", "")], board)
    reasons = {e["teachworks_id"]: e["reason"] for e in report.exceptions}
    assert reasons["1"].startswith("2 Students items share this Teachworks Student ID")
    assert reasons["4"] == "no usable first and last name in Teachworks"
    assert getattr(board, "student_creates", []) == []


@pytest.mark.parametrize("setting, value, message", [
    ("STUDENT_SYNC_CREATE", False, "student creation is off"),
    ("MONDAY_STUDENTS_NEW_GROUP_ID", "", "MONDAY_STUDENTS_NEW_GROUP_ID is not set"),
])
def test_creation_needs_both_switches(monkeypatch, setting, value, message):
    monkeypatch.setattr(config, setting, value)
    board = FakeBoard([], [])
    report = run([tw_student(2, "Bo", "Diaz")], board)
    assert [p["name"] for p in report.to_create] == ["Bo Diaz"] and report.created == []
    assert message in report.blocked and board.writes() == 0 and report.succeeded


def test_dry_run_reports_who_would_be_created_and_writes_nothing():
    board = FakeBoard([row(1, 502, 2, "2026-10-02", "Bo Diaz", connected=False)], [])
    report = run([tw_student(2, "Bo", "Diaz")], board, dry_run=True)
    assert [(p["name"], p["baseline"], len(p["unconnected_rows"])) for p in report.to_create] == [("Bo Diaz", 0, 1)]
    assert report.blocked == "dry run; nothing created" and board.writes() == 0


def test_too_many_new_students_creates_none_and_fails(monkeypatch):
    monkeypatch.setattr(config, "STUDENT_SYNC_MAX_CREATES", 2)
    board = FakeBoard([], [])
    report = run([tw_student(i, f"First{i}", f"Last{i}") for i in range(1, 4)], board)
    assert board.writes() == 0 and len(report.to_create) == 3 and not report.succeeded
    assert "more than STUDENT_SYNC_MAX_CREATES (2)" in report.blocked


def test_more_than_80_students_are_all_read():
    students = [tw_student(i, f"First{i}", f"Last{i}") for i in range(1, 171)]
    board = FakeBoard([], [named(f"S{i}", f"First{i} Last{i}", i) for i in range(1, 171)])
    report = run(students, board)
    assert report.teachworks_students == 170 and report.matched == 170


# --- inside --daily-sync -----------------------------------------------------------------

def test_new_student_is_created_then_their_new_sessions_connect_and_count(capsys):
    lessons = [tw_lesson(700, "2026-10-02", [(2, "Bo Diaz")]), tw_lesson(701, "2026-10-05", [(2, "Bo Diaz")])]
    tw = FakeTeachworks(lessons, [tw_student(2, "Bo", "Diaz")])
    board = FakeBoard([row(1, 600, 2, "2026-08-10", "Bo Diaz", pre_baseline=True, connected=False)], [])

    assert sync.run_daily_sync(tw.client(), board, "2026-10-01", "2026-10-07") == 0
    bo = next(s for s in board.student_items if s["item_name"] == "Bo Diaz")
    assert all(item_id_student == bo["item_id"] for _i, item_id_student in board.connections)
    assert len(board.connections) == 3                                     # old row + 2 new sessions
    assert bo["columns"][config.STUDENT_COL_SESSION_COUNT] == "3"           # baseline 1 + 2 post-baseline
    out = capsys.readouterr().out
    assert "Students created:                             1" in out
    assert "Bo Diaz (Teachworks ID 2, Teachworks status Active) - baseline 1, 1 Session Log row(s) to connect" in out

    writes = board.writes()
    assert sync.run_daily_sync(tw.client(), board, "2026-10-01", "2026-10-07") == 0
    assert board.writes() == writes                                         # no duplicate students or sessions
    assert len([s for s in board.student_items if s["item_name"] == "Bo Diaz"]) == 1


def test_exceptions_do_not_fail_the_run(capsys):
    tw = FakeTeachworks([], [tw_student(2, "Bo", "Diaz")])
    board = FakeBoard([], [named("S9", "Bo Diaz")])
    assert sync.run_daily_sync(tw.client(), board, "2026-10-01", "2026-10-07") == 0
    out = capsys.readouterr().out
    assert "Exceptions requiring review:                  1" in out
    assert "'Bo Diaz' (Teachworks ID 2): Students item S9 'Bo Diaz' has no Teachworks ID" in out


def test_roster_failure_creates_no_students_but_the_session_log_still_reconciles(capsys):
    tw = FakeTeachworks([tw_lesson(700, "2026-10-02", [(1, "Ann Lee")])])
    tw.fail_on_students = True
    board = FakeBoard([], [named("S1", "Ann Lee", 1)])
    original = tw.get

    def get(url, headers=None, params=None, timeout=None):
        if url.endswith("/students"):
            from unittest.mock import MagicMock
            return MagicMock(status_code=401, text="unauthorized")
        return original(url, headers=headers, params=params, timeout=timeout)

    tw.get = get
    assert sync.run_daily_sync(tw.client(), board, "2026-10-01", "2026-10-07") == 1
    out = capsys.readouterr().out
    assert "--- STEP 0: STUDENTS BOARD ROSTER ---\nEXCEPTION:" in out and "FINAL RESULT: FAILURE" in out
    assert getattr(board, "student_creates", []) == [] and len(board.created_items) == 1
