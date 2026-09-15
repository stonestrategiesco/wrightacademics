from unittest.mock import MagicMock

import config
from sync import run_sync
from teachworks import TeachworksClient
from tests.fakes import FakeMondayClient, FakeTeachworksClient, make_lesson, make_participant


def _tw_response(json_data):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_data
    return resp


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


def test_duplicate_protection_holds_across_teachworks_day_by_day_requests():
    """End-to-end: uses the REAL TeachworksClient (exercising its per-calendar-
    date request loop) feeding into the real run_sync dedup logic. If the same
    lesson were ever returned by two different days' single-day queries, the
    unique-key check must still ensure only one Monday item gets created."""
    same_lesson = {
        "id": 999,
        "from_date": "2026-09-13",
        "participants": [{"student_id": 5, "student_name": "Zoe", "status": "Attended"}],
    }
    session = MagicMock()
    session.get.side_effect = [
        _tw_response([same_lesson]),  # 2026-09-13 query
        _tw_response([same_lesson]),  # 2026-09-14 query returns the same lesson again
    ]
    tw = TeachworksClient(api_key="key", base_url="https://api.teachworks.com/v1", session=session)
    monday = FakeMondayClient()

    report = run_sync(tw, monday, "2026-09-13", "2026-09-14")

    # one request per calendar date, exactly as the day-by-day loop requires
    assert session.get.call_count == 2
    assert report.sessions_created == 1
    assert report.sessions_skipped == 1
    assert len(monday.created_items) == 1


# --- Legacy Zapier unique-ID backward compatibility ----------------------
#
# The legacy Zap stored ONLY the bare lesson_id in text_mm5h9n9g (no student
# component). New records store the composite {lesson_id}_{student_id} key.
# Both formats can coexist on the board, unmigrated, and both must be
# recognized as "already exists" - the legacy form is only ever checked,
# never written.

def test_existing_composite_key_is_skipped_as_duplicate():
    lesson = make_lesson(94419922, "2026-09-13", [make_participant(2246673, "Someone")])
    tw = FakeTeachworksClient([lesson])
    monday = FakeMondayClient(existing_ids={"94419922_2246673"})

    report = run_sync(tw, monday, "2026-09-13", "2026-09-13")

    assert report.sessions_skipped == 1
    assert report.sessions_created == 0
    assert monday.created_items == []


def test_existing_legacy_lesson_only_key_is_skipped_as_duplicate():
    lesson = make_lesson(94419922, "2026-09-13", [make_participant(2246673, "Someone")])
    tw = FakeTeachworksClient([lesson])
    monday = FakeMondayClient(existing_ids={"94419922"})  # legacy format: lesson_id only

    report = run_sync(tw, monday, "2026-09-13", "2026-09-13")

    assert report.sessions_skipped == 1
    assert report.sessions_created == 0
    assert monday.created_items == []


def test_neither_key_exists_creates_item_with_composite_key():
    lesson = make_lesson(94419922, "2026-09-13", [make_participant(2246673, "Someone")])
    tw = FakeTeachworksClient([lesson])
    monday = FakeMondayClient()  # nothing existing at all

    report = run_sync(tw, monday, "2026-09-13", "2026-09-13")

    assert report.sessions_created == 1
    assert len(monday.created_items) == 1
    # ALWAYS stores the composite key on new records - never the legacy form.
    assert monday.created_items[0]["column_values"][config.COL_UNIQUE_ID] == "94419922_2246673"


def test_legacy_key_double_run_idempotency_remains_intact():
    lesson = make_lesson(94419922, "2026-09-13", [make_participant(2246673, "Someone")])
    tw = FakeTeachworksClient([lesson])
    monday = FakeMondayClient(existing_ids={"94419922"})

    first = run_sync(tw, monday, "2026-09-13", "2026-09-13")
    second = run_sync(tw, monday, "2026-09-13", "2026-09-13")

    assert first.sessions_created == 0
    assert first.sessions_skipped == 1
    assert second.sessions_created == 0
    assert second.sessions_skipped == 1
    assert monday.created_items == []


def test_multi_student_new_lesson_creates_both_composite_keys():
    lesson = make_lesson(99999, "2026-09-13", [
        make_participant(111, "Alice"),
        make_participant(222, "Bob"),
    ])
    tw = FakeTeachworksClient([lesson])
    monday = FakeMondayClient()  # nothing existing

    report = run_sync(tw, monday, "2026-09-13", "2026-09-13")

    assert report.sessions_created == 2
    keys = {item["column_values"][config.COL_UNIQUE_ID] for item in monday.created_items}
    assert keys == {"99999_111", "99999_222"}


def test_legacy_multi_student_lesson_skips_both_participants():
    """The legacy Zap represented this whole lesson with one bare lesson_id
    record. Neither of the new per-participant composite keys should be
    created - both participants are already covered by that legacy record."""
    lesson = make_lesson(99999, "2026-09-13", [
        make_participant(111, "Alice"),
        make_participant(222, "Bob"),
    ])
    tw = FakeTeachworksClient([lesson])
    monday = FakeMondayClient(existing_ids={"99999"})  # legacy: lesson-only

    report = run_sync(tw, monday, "2026-09-13", "2026-09-13")

    assert report.sessions_created == 0
    assert report.sessions_skipped == 2
    assert monday.created_items == []


def test_mixed_legacy_and_composite_existing_ids_are_each_recognized_correctly():
    lessons = [
        make_lesson(100, "2026-09-13", [make_participant(1, "Legacy Match")]),      # matches legacy "100"
        make_lesson(200, "2026-09-13", [make_participant(2, "Composite Match")]),   # matches composite "200_2"
        make_lesson(300, "2026-09-13", [make_participant(3, "Brand New")]),         # matches neither
    ]
    tw = FakeTeachworksClient(lessons)
    monday = FakeMondayClient(existing_ids={"100", "200_2"})

    report = run_sync(tw, monday, "2026-09-13", "2026-09-13")

    assert report.sessions_skipped == 2  # the legacy and composite matches
    assert report.sessions_created == 1  # only the brand-new one
    assert len(monday.created_items) == 1
    assert monday.created_items[0]["column_values"][config.COL_UNIQUE_ID] == "300_3"


def test_legacy_key_match_is_also_respected_during_dry_run():
    lesson = make_lesson(94419922, "2026-09-13", [make_participant(2246673, "Someone")])
    tw = FakeTeachworksClient([lesson])
    monday = FakeMondayClient(existing_ids={"94419922"})

    report = run_sync(tw, monday, "2026-09-13", "2026-09-13", dry_run=True)

    assert report.sessions_created == 0
    assert report.sessions_skipped == 1
    assert monday.created_items == []
    assert monday.connections == []
