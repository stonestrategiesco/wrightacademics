import config
from sync import run_sync
from tests.fakes import FakeMondayClient, FakeTeachworksClient, make_lesson, make_participant


def test_existing_unique_id_is_skipped():
    lesson = make_lesson(123456, "2026-09-10", [make_participant(789, "Alice")])
    tw = FakeTeachworksClient([lesson])
    monday = FakeMondayClient(existing_ids={"123456_789"})

    report = run_sync(tw, monday, "2026-09-08", "2026-09-11")

    assert report.sessions_skipped == 1
    assert report.sessions_created == 0
    assert monday.created_items == []


def test_running_same_sync_twice_creates_zero_duplicates():
    lesson = make_lesson(123456, "2026-09-10", [make_participant(789, "Alice")])
    tw = FakeTeachworksClient([lesson])
    monday = FakeMondayClient()

    first = run_sync(tw, monday, "2026-09-08", "2026-09-11")
    second = run_sync(tw, monday, "2026-09-08", "2026-09-11")

    assert first.sessions_created == 1
    assert second.sessions_created == 0
    assert second.sessions_skipped == 1
    assert len(monday.created_items) == 1


def test_group_lesson_with_two_students_produces_two_records():
    lesson = make_lesson(555, "2026-09-10", [
        make_participant(1, "Alice"),
        make_participant(2, "Bob"),
    ])
    tw = FakeTeachworksClient([lesson])
    monday = FakeMondayClient()

    report = run_sync(tw, monday, "2026-09-08", "2026-09-11")

    assert report.sessions_created == 2
    keys = {item["column_values"][config.COL_UNIQUE_ID] for item in monday.created_items}
    assert keys == {"555_1", "555_2"}


def test_same_lesson_and_student_cannot_be_created_twice_in_one_run():
    # Simulate a Teachworks pagination glitch returning the same lesson twice.
    lesson = make_lesson(123456, "2026-09-10", [make_participant(789, "Alice")])
    tw = FakeTeachworksClient([lesson, lesson])
    monday = FakeMondayClient()

    report = run_sync(tw, monday, "2026-09-08", "2026-09-11")

    assert report.sessions_created == 1
    assert report.sessions_skipped == 1
    assert len(monday.created_items) == 1


def test_missing_monday_student_does_not_create_a_student_and_is_reported():
    lesson = make_lesson(123456, "2026-09-10", [make_participant(789, "Alice")])
    tw = FakeTeachworksClient([lesson])
    monday = FakeMondayClient(student_lookup={})  # no students at all

    report = run_sync(tw, monday, "2026-09-08", "2026-09-11")

    # The session log item is still created...
    assert report.sessions_created == 1
    assert len(monday.created_items) == 1
    # ...but nothing that looks like a "create student" action ever happens:
    # our FakeMondayClient exposes no such method, and no connection is made.
    assert monday.connections == []
    assert report.connections_made == 0
    # and it's clearly reported.
    assert len(report.missing_students) == 1
    assert report.missing_students[0] == {
        "teachworks_student_id": 789,
        "student_name": "Alice",
        "lesson_id": 123456,
    }


def test_matched_student_is_connected():
    lesson = make_lesson(123456, "2026-09-10", [make_participant(789, "Alice")])
    tw = FakeTeachworksClient([lesson])
    monday = FakeMondayClient(student_lookup={"789": "mnd-item-1"})

    report = run_sync(tw, monday, "2026-09-08", "2026-09-11")

    assert report.students_matched == 1
    assert report.connections_made == 1
    assert monday.connections == [(monday.created_items[0]["id"], "mnd-item-1")]


def test_dry_run_performs_zero_writes():
    lesson = make_lesson(123456, "2026-09-10", [make_participant(789, "Alice")])
    tw = FakeTeachworksClient([lesson])
    monday = FakeMondayClient(student_lookup={"789": "mnd-item-1"})

    report = run_sync(tw, monday, "2026-09-08", "2026-09-11", dry_run=True)

    assert report.sessions_created == 1  # "would create"
    assert monday.created_items == []
    assert monday.connections == []


def test_creation_error_is_surfaced_and_does_not_stop_the_run():
    lessons = [
        make_lesson(1, "2026-09-10", [make_participant(1, "Alice")]),
        make_lesson(2, "2026-09-10", [make_participant(2, "Bob")]),
    ]
    tw = FakeTeachworksClient(lessons)
    monday = FakeMondayClient()

    calls = {"n": 0}

    def flaky_create(column_values):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom: monday rejected the request")

    monday.create_side_effect = flaky_create

    report = run_sync(tw, monday, "2026-09-08", "2026-09-11")

    assert report.sessions_created == 1
    assert len(report.creation_errors) == 1
    assert len(monday.created_items) == 1
