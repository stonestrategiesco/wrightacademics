"""Tests for the new, additive post-baseline canonical-identity dedup
helper (_group_post_baseline_session_log_rows_by_student /
_canonical_session_identity), built for the future baseline+SET rollup
calculation. This is NOT wired to any CLI command yet.

Crucially, none of this touches _group_session_log_rows_by_student() (the
function backing the CURRENT, still-live date-checkpoint --student-rollups
command) - see test_does_not_touch_the_existing_date_checkpoint_grouping
below, and the accompanying `git diff` review."""

import config
from sync import (
    _canonical_session_identity,
    _group_post_baseline_session_log_rows_by_student,
    _group_session_log_rows_by_student,
)


def _item(item_id, unique_key, tw_student_id, session_date="2026-09-01", tutor="Jane Tutor", pre_baseline=""):
    return {
        "item_id": item_id,
        "item_name": f"Session {item_id}",
        "columns": {
            config.COL_UNIQUE_ID: unique_key,
            config.COL_TEACHWORKS_STUDENT_ID: tw_student_id,
            config.COL_SESSION_DATE: session_date,
            config.COL_TUTOR: tutor,
            config.COL_PRE_BASELINE: pre_baseline,
        },
    }


def test_exact_composite_duplicate_counts_once():
    items = [
        _item("i1", "89187387_2203327", "2203327"),
        _item("i2", "89187387_2203327", "2203327"),
    ]

    rows_by_student = _group_post_baseline_session_log_rows_by_student(items)

    assert len(rows_by_student["2203327"]) == 1


def test_exact_bare_duplicate_counts_once():
    items = [
        _item("i1", "89515688", "111"),
        _item("i2", "89515688", "111"),
    ]

    rows_by_student = _group_post_baseline_session_log_rows_by_student(items)

    assert len(rows_by_student["111"]) == 1


def test_bare_and_composite_same_lesson_student_collapse_to_one():
    items = [
        _item("i1", "91267524", "2203327"),
        _item("i2", "91267524_2203327", "2203327"),
    ]

    rows_by_student = _group_post_baseline_session_log_rows_by_student(items)

    assert len(rows_by_student["2203327"]) == 1


def test_same_lesson_id_different_students_remain_separate():
    items = [
        _item("i1", "500_1", "1"),
        _item("i2", "500_2", "2"),
    ]

    rows_by_student = _group_post_baseline_session_log_rows_by_student(items)

    assert len(rows_by_student["1"]) == 1
    assert len(rows_by_student["2"]) == 1


def test_pre_baseline_row_only_contributes_zero():
    items = [_item("i1", "700_1", "1", pre_baseline="v")]

    rows_by_student = _group_post_baseline_session_log_rows_by_student(items)

    assert rows_by_student.get("1", []) == []


def test_post_baseline_row_only_contributes_one():
    items = [_item("i1", "701_1", "1")]

    rows_by_student = _group_post_baseline_session_log_rows_by_student(items)

    assert len(rows_by_student["1"]) == 1


def test_pre_baseline_and_post_baseline_same_identity_contributes_one():
    items = [
        _item("i1", "702_1", "1", pre_baseline="v"),
        _item("i2", "702_1", "1"),
    ]

    rows_by_student = _group_post_baseline_session_log_rows_by_student(items)

    assert len(rows_by_student["1"]) == 1


def test_two_post_baseline_rows_same_identity_contributes_one():
    items = [
        _item("i1", "703_1", "1"),
        _item("i2", "703_1", "1"),
    ]

    rows_by_student = _group_post_baseline_session_log_rows_by_student(items)

    assert len(rows_by_student["1"]) == 1


def test_bare_and_composite_post_baseline_rows_same_identity_contributes_one():
    items = [
        _item("i1", "704", "1"),
        _item("i2", "704_1", "1"),
    ]

    rows_by_student = _group_post_baseline_session_log_rows_by_student(items)

    assert len(rows_by_student["1"]) == 1


def test_missing_unique_key_falls_back_to_item_id_and_is_not_collapsed():
    # Two rows, both missing unique_key entirely: each falls back to its
    # own item_id as the identity, so they must NOT collapse together even
    # though they'd otherwise look identical (same student, same date).
    items = [
        _item("i1", "", "1"),
        _item("i2", "", "1"),
    ]

    rows_by_student = _group_post_baseline_session_log_rows_by_student(items)

    assert len(rows_by_student["1"]) == 2


def test_missing_teachworks_student_id_is_excluded_from_grouped_output():
    items = [_item("i1", "800_1", "")]

    rows_by_student = _group_post_baseline_session_log_rows_by_student(items)

    assert rows_by_student == {}


def test_canonical_session_identity_uses_column_student_id_not_key_suffix():
    # If the unique_key's encoded student id and the row's own
    # Teachworks Student ID column ever disagreed, the column wins.
    item = _item("i1", "900_999", "111")

    identity = _canonical_session_identity(item)

    assert identity == ("900", "111")


def test_canonical_session_identity_falls_back_when_lesson_id_missing():
    item = _item("i1", "", "111")

    identity = _canonical_session_identity(item)

    assert identity == (None, "i1")


def test_does_not_touch_the_existing_date_checkpoint_grouping():
    # Same input, run through both functions: the OLD function must keep
    # deduping by literal unique_key text only (no canonical-identity
    # collapse, no Pre-Baseline awareness), proving it is untouched.
    items = [
        _item("i1", "91267524", "2203327", pre_baseline="v"),
        _item("i2", "91267524_2203327", "2203327"),
    ]

    old_result = _group_session_log_rows_by_student(items)
    new_result = _group_post_baseline_session_log_rows_by_student(items)

    # Old function: two different literal unique_key strings -> not deduped.
    assert len(old_result["2203327"]) == 2
    # New function: same canonical identity, Pre-Baseline row excluded -> one.
    assert len(new_result["2203327"]) == 1
