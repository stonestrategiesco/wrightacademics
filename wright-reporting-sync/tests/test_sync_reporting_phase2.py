import sys
import io
import json
import contextlib
import tempfile
from pathlib import Path


def test_sync_reporting_phase2():

    import audit
    import sync_monday as sm
    import sync_reporting as sr

    STUDENTS = [{"id": i, "first_name": f"S{i}", "last_name": "T"} for i in range(1, 3)]


    def dt(day, month=9, year=2026, hour=10):
        return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:00:00Z"


    LESSONS = [
        {"id": 1, "from_datetime": dt(1), "participants": [{"student_id": 1, "status": "Attended"}]},
        {"id": 2, "from_datetime": dt(2), "participants": [
            {"student_id": 2, "status": "Attended"},
            {"student_id": 1, "status": "Missed"},
        ]},
    ]
    # Expected: Attended=2, Missed=1, Total=3, StudentsServed=2


    def fake_request_json(config, path, params=None, max_retries=3):
        class R:
            url = "https://fake"
            status_code = 200

        page = params.get("page", 1)
        if path == config["students_path"]:
            return (STUDENTS if page == 1 else []), R()
        if path == config["lessons_path"]:
            return (LESSONS if page == 1 else []), R()
        raise AssertionError(f"unexpected path {path}")


    audit.request_json = fake_request_json

    TW_CONFIG = {
        "base_url": "https://fake", "api_key": "fake", "auth_header": "Authorization",
        "auth_scheme": "Token token={key}", "students_path": "/students", "lessons_path": "/lessons",
        "tutors_path": "", "page_param": "page", "per_page_param": "per_page", "response_data_key": "",
        "lesson_start_date_param": "", "lesson_end_date_param": "", "student_id_param": "student_id",
        "test_student_limit": 5, "test_lesson_limit": 20, "test_lesson_lookback_days": 30,
        "per_page": 80, "max_pages": 30, "request_delay_seconds": 0, "status_field": "status",
        "attended_status_value": "Attended", "student_id_field": "id", "student_name_field": "",
        "student_first_name_field": "", "student_last_name_field": "", "lesson_date_field": "",
        "lesson_tutor_id_field": "", "lesson_tutor_name_field": "", "tutor_record_id_field": "id",
        "tutor_record_name_field": "", "participants_field": "participants", "participant_sample_size": 20,
        "participant_id_field": "", "participant_type_field": "", "participant_student_type_value": "student",
    }
    audit.load_config = lambda: TW_CONFIG

    MONDAY_CFG = {
        "api_url": "https://fake-monday", "api_token": "fake-token", "auth_header": "Authorization",
        "auth_scheme": "{key}", "api_version": "", "items_page_size": 100,
        "board_id": "18432993218",
        "column_sessions_attended": "numeric_mm7mrpe4",
        "column_sessions_missed": "numeric_mm7mh4qd",
        "column_total_sessions": "numeric_mm7me68f",
        "column_students_served": "numeric_mm7mhsf3",
        "column_last_updated": "date_mm7m4799",
    }


    def make_item(item_id, name, sessions_attended, sessions_missed, total_sessions, students_served, last_updated=""):
        def cv(col_id, text):
            return {"id": col_id, "text": text, "value": json.dumps(text)}
        return {
            "id": item_id, "name": name,
            "column_values": [
                cv(MONDAY_CFG["column_sessions_attended"], str(sessions_attended)),
                cv(MONDAY_CFG["column_sessions_missed"], str(sessions_missed)),
                cv(MONDAY_CFG["column_total_sessions"], str(total_sessions)),
                cv(MONDAY_CFG["column_students_served"], str(students_served)),
                cv(MONDAY_CFG["column_last_updated"], last_updated),
            ],
        }


    # =====================================================================
    # Test 1: dry-run preview -- MATCHED item, stale values -> diffs reported, NO writes made.
    # =====================================================================
    write_calls = []


    def fake_monday_graphql_matched(cfg, query, variables=None, max_retries=3):
        if "items_page" in query and "next_items_page" not in query:
            item = make_item("111", "September 2026", 0, 0, 0, 0, "")
            return {"boards": [{"items_page": {"cursor": None, "items": [item]}}]}
        if "change_simple_column_value" in query:
            write_calls.append(variables)
            return {"change_simple_column_value": {"id": variables["itemId"]}}
        raise AssertionError(f"unexpected query: {query[:50]}")


    sm.monday_graphql = fake_monday_graphql_matched

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sr.run_dry_run(TW_CONFIG, output_dir, 2026, 9, refresh_teachworks_cache=False, monday_cfg=MONDAY_CFG)
        output = buf.getvalue()

        assert write_calls == [], f"dry-run must NEVER write to Monday, but got: {write_calls}"
        assert "Matched Monday item id='111'" in output
        assert "Sessions Attended: '0' -> '2'" in output
        assert "Sessions Missed: '0' -> '1'" in output
        assert "Total Sessions: '0' -> '3'" in output
        assert "Students Served: '0' -> '2'" in output
        print("PASS: dry-run preview shows exact diffs for a MATCHED item and makes zero Monday writes")

        out_path = output_dir / "wright-teachworks-reporting-dry-run-2026-09.json"
        with open(out_path) as f:
            result = json.load(f)
        assert result["monday_preview"]["status"] == "MATCHED"
        assert result["monday_preview"]["item_id"] == "111"
        assert result["monday_preview"]["diffs"]["sessions_attended"] == {"current": "0", "new": "2"}
        print("PASS: dry-run JSON output records the Monday preview status/diffs")

    # =====================================================================
    # Test 2: update mode -- MATCHED item, confirmed with typed YES -> writes exactly the diffs + Last Updated.
    # =====================================================================
    write_calls.clear()
    import builtins
    orig_input = builtins.input
    builtins.input = lambda prompt="": "YES"

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sr.run_update(TW_CONFIG, MONDAY_CFG, output_dir, 2026, 9, refresh_teachworks_cache=False)
        output = buf.getvalue()

        written_columns = {c["columnId"]: c["value"] for c in write_calls}
        assert written_columns[MONDAY_CFG["column_sessions_attended"]] == "2"
        assert written_columns[MONDAY_CFG["column_sessions_missed"]] == "1"
        assert written_columns[MONDAY_CFG["column_total_sessions"]] == "3"
        assert written_columns[MONDAY_CFG["column_students_served"]] == "2"
        assert MONDAY_CFG["column_last_updated"] in written_columns
        assert "Update complete." in output
        print(f"PASS: update mode (after typed YES) writes exactly the 4 KPI fields + Last Updated: {written_columns}")

        log_path = output_dir / "wright-teachworks-reporting-update-2026-09.json"
        with open(log_path) as f:
            log = json.load(f)
        assert log["errors"] == []
        assert log["fields_written"] == {
            "sessions_attended": "2", "sessions_missed": "1", "total_sessions": "3", "students_served": "2",
        }
        print("PASS: update log JSON records exactly what was written, no errors")

    builtins.input = orig_input

    # =====================================================================
    # Test 3: the temporary September-2026-only guard is gone -- a manual
    # single-month update of October 2026 goes through the same matching,
    # preview and typed-YES confirmation, and writes only after YES.
    # =====================================================================
    october_lessons = [
        {"id": 31, "from_datetime": dt(5, month=10), "participants": [{"student_id": 1, "status": "Attended"}]},
        {"id": 32, "from_datetime": dt(6, month=10), "participants": [{"student_id": 2, "status": "Missed"}]},
    ]
    LESSONS.extend(october_lessons)  # October: Attended=1, Missed=1, Total=2, StudentsServed=2


    def fake_monday_graphql_october(cfg, query, variables=None, max_retries=3):
        if "items_page" in query and "next_items_page" not in query:
            items = [make_item("111", "09 - September 2026", 2, 1, 3, 2), make_item("222", "10 - October 2026", 0, 0, 0, 0)]
            return {"boards": [{"items_page": {"cursor": None, "items": items}}]}
        if "change_simple_column_value" in query:
            write_calls.append(variables)
            return {"change_simple_column_value": {"id": variables["itemId"]}}
        raise AssertionError(f"unexpected query: {query[:50]}")


    sm.monday_graphql = fake_monday_graphql_october

    # 3a: declining the confirmation writes nothing (and is not an error).
    write_calls.clear()
    builtins.input = lambda prompt="": "no"
    with tempfile.TemporaryDirectory() as tmpdir:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                sr.run_update(TW_CONFIG, MONDAY_CFG, Path(tmpdir), 2026, 10, refresh_teachworks_cache=False)
            raise AssertionError("expected SystemExit after a declined confirmation")
        except SystemExit as e:
            assert e.code == 0
        output = buf.getvalue()
        assert "restricts writes to September 2026" not in output
        assert "Matched Monday item id='222' name='10 - October 2026'" in output
        assert "Aborted -- nothing was written to Monday." in output
        assert write_calls == []
    print("PASS: October update previews the '10 - October 2026' item and writes nothing when not confirmed")

    # 3b: confirming with YES writes October's fields, and only to the October item.
    write_calls.clear()
    builtins.input = lambda prompt="": "YES"
    with tempfile.TemporaryDirectory() as tmpdir:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sr.run_update(TW_CONFIG, MONDAY_CFG, Path(tmpdir), 2026, 10, refresh_teachworks_cache=False)
        output = buf.getvalue()
        assert {c["itemId"] for c in write_calls} == {"222"}
        written_columns = {c["columnId"]: c["value"] for c in write_calls}
        assert written_columns[MONDAY_CFG["column_sessions_attended"]] == "1"
        assert written_columns[MONDAY_CFG["column_sessions_missed"]] == "1"
        assert written_columns[MONDAY_CFG["column_total_sessions"]] == "2"
        assert written_columns[MONDAY_CFG["column_students_served"]] == "2"
        assert MONDAY_CFG["column_last_updated"] in written_columns
        assert "Update complete." in output
    builtins.input = orig_input
    del LESSONS[-len(october_lessons):]
    print("PASS: October update (after typed YES) writes October's KPI fields to the October item only")

    # =====================================================================
    # Test 4: update mode aborts cleanly on NOT_FOUND (no item named "September 2026") -- never creates one.
    # =====================================================================
    def fake_monday_graphql_not_found(cfg, query, variables=None, max_retries=3):
        if "items_page" in query and "next_items_page" not in query:
            return {"boards": [{"items_page": {"cursor": None, "items": []}}]}
        raise AssertionError("must not attempt to write/create when NOT_FOUND")


    sm.monday_graphql = fake_monday_graphql_not_found
    builtins.input = lambda prompt="": (_ for _ in ()).throw(AssertionError("must never prompt to confirm a write when NOT_FOUND"))

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                sr.run_update(TW_CONFIG, MONDAY_CFG, output_dir, 2026, 9, refresh_teachworks_cache=False)
            raise AssertionError("expected SystemExit for NOT_FOUND")
        except SystemExit as e:
            assert e.code == 1
        output = buf.getvalue()
        assert "NOT FOUND" in output
        assert "Aborting -- nothing was written to Monday" in output
        print("PASS: update mode aborts cleanly on NOT_FOUND, never creates an item, never prompts")

    builtins.input = orig_input

    print("\nALL Phase 2 (Monday preview/update) TESTS PASSED")
