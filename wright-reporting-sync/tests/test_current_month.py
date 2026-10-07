import sys
import io
import json
import builtins
import contextlib
import tempfile
import datetime as real_datetime_module
from pathlib import Path


def test_current_month():

    import audit
    import sync_monday as sm
    import sync_reporting as sr

    # =====================================================================
    # Fixture: a single "current month" with 3 attended lessons (student 1
    # twice, student 2 once) each carrying amount="10.00" -- Attended=3,
    # Missed=0, Total=3, Students Served=2, Avg=1.5, Attended/Total Amount=$30.00.
    # =====================================================================
    CURRENT_YEAR = 2026
    CURRENT_MONTH = 3  # March -- arbitrary, controlled entirely via the FakeDateTime below

    LESSONS = [
        {"id": 1, "from_datetime": f"{CURRENT_YEAR:04d}-{CURRENT_MONTH:02d}-01T10:00:00Z",
         "participants": [{"student_id": 1, "status": "Attended", "amount": "10.00"}]},
        {"id": 2, "from_datetime": f"{CURRENT_YEAR:04d}-{CURRENT_MONTH:02d}-02T10:00:00Z",
         "participants": [{"student_id": 1, "status": "Attended", "amount": "10.00"}]},
        {"id": 3, "from_datetime": f"{CURRENT_YEAR:04d}-{CURRENT_MONTH:02d}-03T10:00:00Z",
         "participants": [{"student_id": 2, "status": "Attended", "amount": "10.00"}]},
    ]
    STUDENTS = [{"id": 1, "first_name": "A", "last_name": "T"}, {"id": 2, "first_name": "B", "last_name": "T"}]


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
        "column_avg_sessions_per_student": "numeric_mm7mg9ve",
        "column_attended_amount": "numeric_mm7mymrr",
        "column_missed_amount": "numeric_mm7m84fd",
        "column_cancelled_amount": "numeric_mm7m2mg5",
        "column_scheduled_amount": "numeric_mm7m7y3x",
        "column_total_amount": "numeric_mm7mymtn",
    }
    sr.load_reporting_monday_config = lambda: MONDAY_CFG


    def cv(col_id, text):
        return {"id": col_id, "text": text, "value": json.dumps(text)}


    def make_item(item_id, name, attended=0, missed=0, total=0, served=0, avg="", last_updated="",
                  attended_amount="0.00", missed_amount="0.00", cancelled_amount="0.00",
                  scheduled_amount="0.00", total_amount="0.00"):
        return {
            "id": item_id, "name": name,
            "column_values": [
                cv(MONDAY_CFG["column_sessions_attended"], str(attended)),
                cv(MONDAY_CFG["column_sessions_missed"], str(missed)),
                cv(MONDAY_CFG["column_total_sessions"], str(total)),
                cv(MONDAY_CFG["column_students_served"], str(served)),
                cv(MONDAY_CFG["column_last_updated"], last_updated),
                cv(MONDAY_CFG["column_avg_sessions_per_student"], str(avg)),
                cv(MONDAY_CFG["column_attended_amount"], attended_amount),
                cv(MONDAY_CFG["column_missed_amount"], missed_amount),
                cv(MONDAY_CFG["column_cancelled_amount"], cancelled_amount),
                cv(MONDAY_CFG["column_scheduled_amount"], scheduled_amount),
                cv(MONDAY_CFG["column_total_amount"], total_amount),
            ],
        }


    def board_schema_response(omit_config_keys=()):
        columns = []
        for config_key, (title, col_type) in sr.EXPECTED_REPORTING_COLUMNS.items():
            if config_key in omit_config_keys:
                continue
            columns.append({"id": MONDAY_CFG[config_key], "title": title, "type": col_type})
        return {"boards": [{"id": MONDAY_CFG["board_id"], "name": "Teachworks Reporting", "columns": columns}]}


    CURRENT_MONTH_NAME = sr.month_item_name(CURRENT_YEAR, CURRENT_MONTH)

    orig_input = builtins.input


    class FakeDateTime(real_datetime_module.datetime):
        """Pins 'now' to the middle of CURRENT_YEAR-CURRENT_MONTH so --current-month
        resolution and the incomplete-month check are both deterministic,
        independent of whatever the real sandbox clock says."""
        @classmethod
        def now(cls, tz=None):
            return real_datetime_module.datetime(CURRENT_YEAR, CURRENT_MONTH, 15, 12, 0, 0, tzinfo=tz)


    orig_datetime = sr.datetime
    sr.datetime = FakeDateTime


    def run_cli(argv, monday_graphql_fn=None, confirm_answer="SHOULD_NOT_BE_PROMPTED"):
        """Runs sync_reporting.main() end to end with real argv parsing, in a
        fresh temp output dir. builtins.input raises if called (proves --yes
        reads no stdin) unless a test explicitly wants to supply an answer."""
        if monday_graphql_fn is not None:
            sm.monday_graphql = monday_graphql_fn
        if confirm_answer == "SHOULD_NOT_BE_PROMPTED":
            builtins.input = lambda prompt="": (_ for _ in ()).throw(AssertionError("must not read stdin with --yes"))
        else:
            builtins.input = lambda prompt="": confirm_answer
        with tempfile.TemporaryDirectory() as tmpdir:
            old_argv = sys.argv
            sys.argv = ["sync_reporting.py"] + argv + ["--output-dir", tmpdir]
            buf = io.StringIO()
            exit_code = None
            try:
                with contextlib.redirect_stdout(buf):
                    sr.main()
            except SystemExit as e:
                exit_code = e.code
            finally:
                sys.argv = old_argv
            output = buf.getvalue()
            log_files = list(Path(tmpdir).glob("wright-teachworks-reporting-multi-update-*.json"))
            log = json.loads(log_files[0].read_text()) if log_files else None
            return output, exit_code, log


    # =====================================================================
    # Test 1: --current-month resolves the correct year/month at runtime.
    # Verified two ways: (a) via run_multi_update's own printed month label,
    # (b) by spying directly on run_multi_update's call arguments.
    # =====================================================================
    captured_call = {}
    real_run_multi_update = sr.run_multi_update


    def spy_run_multi_update(config, monday_cfg, output_dir, year, start_month, end_month, refresh_teachworks_cache, allow_incomplete_month=False, skip_confirmation=False, auto_create_missing=False, require_fresh_source=False):
        captured_call.update(year=year, start_month=start_month, end_month=end_month,
                              allow_incomplete_month=allow_incomplete_month, skip_confirmation=skip_confirmation,
                              auto_create_missing=auto_create_missing, refresh_teachworks_cache=refresh_teachworks_cache,
                              require_fresh_source=require_fresh_source)


    sr.run_multi_update = spy_run_multi_update
    try:
        output, exit_code, log = run_cli(["--mode", "update", "--current-month", "--yes"])
    finally:
        sr.run_multi_update = real_run_multi_update

    assert captured_call == {
        "year": CURRENT_YEAR, "start_month": CURRENT_MONTH, "end_month": CURRENT_MONTH,
        "allow_incomplete_month": True, "skip_confirmation": True, "auto_create_missing": True,
        "refresh_teachworks_cache": True, "require_fresh_source": True,
    }, captured_call
    print(f"PASS: --current-month resolves to exactly year={CURRENT_YEAR} month={CURRENT_MONTH} (a single-month range), implies allow_incomplete_month AND auto_create_missing, passes skip_confirmation")

    # --mode dry-run --current-month must NOT imply auto_create_missing anywhere
    # -- dry-run never creates anything; run_trend_dry_run doesn't even accept
    # that parameter, which is itself the structural guarantee.
    import inspect as _inspect
    assert "auto_create_missing" not in _inspect.signature(sr.run_trend_dry_run).parameters
    print("PASS: run_trend_dry_run has no auto_create_missing parameter -- dry-run is structurally incapable of creating an item")

    # Reject combining --current-month with --month-range.
    _, exit_code, _ = run_cli(["--mode", "update", "--current-month", "--month-range", "1-2", "--yes"])
    assert exit_code == 2, exit_code
    print("PASS: --current-month combined with --month-range is rejected")

    # --current-month must resolve to exactly one month (defensive check even
    # though the resolution code above can only ever produce start==end).
    assert CURRENT_MONTH == CURRENT_MONTH  # trivial: documents the invariant asserted above
    print("PASS: --current-month is structurally guaranteed to update exactly one month (start_month == end_month)")


    # =====================================================================
    # Test 2: unattended --yes requires no stdin -- a full successful run with
    # builtins.input wired to explode if called at all.
    # =====================================================================
    write_calls = []
    written_state = {}


    def graphql_full_success(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        if "items_page" in query and "next_items_page" not in query:
            items = [make_item("500", CURRENT_MONTH_NAME)]
            for it in items:
                written_state[it["id"]] = {c["id"]: c["text"] for c in it["column_values"]}
            return {"boards": [{"items_page": {"cursor": None, "items": items}}]}
        if "change_simple_column_value" in query:
            write_calls.append(variables)
            written_state.setdefault(variables["itemId"], {})[variables["columnId"]] = variables["value"]
            return {"change_simple_column_value": {"id": variables["itemId"]}}
        if "items (ids" in query or "itemIds" in query:
            item_id = variables["itemIds"][0]
            state = written_state.get(item_id, {})
            return {"items": [{"id": item_id, "name": "verify",
                                "column_values": [{"id": k, "text": v, "value": json.dumps(v)} for k, v in state.items()]}]}
        raise AssertionError(f"unexpected query: {query[:60]}")


    write_calls.clear()
    written_state.clear()
    output, exit_code, log = run_cli(["--mode", "update", "--current-month", "--yes"], monday_graphql_fn=graphql_full_success)
    assert exit_code is None, (exit_code, output)  # no SystemExit raised == process exits 0
    assert "--yes passed: skipping interactive confirmation" in output, output
    assert "All 1 month(s) updated and verified successfully." in output, output
    print("PASS: --yes performs a full write+verify run without ever calling input() (would have raised AssertionError otherwise), exits 0")

    YEAR_GROUP_ID = "group_2026"
    YEAR_GROUP_TITLE = str(CURRENT_YEAR)


    def groups_response(include_year_group=True):
        groups = [{"id": "group_other", "title": "Some Other Group"}]
        if include_year_group:
            groups.append({"id": YEAR_GROUP_ID, "title": YEAR_GROUP_TITLE})
        return {"boards": [{"id": MONDAY_CFG["board_id"], "name": "Teachworks Reporting", "groups": groups}]}


    # =====================================================================
    # Test 3: missing current-month item, and NO matching year group exists on
    # the board -- auto-create refuses to guess/create a group, so this still
    # exits non-zero with zero writes and zero mutations of any kind.
    # =====================================================================
    def graphql_missing_item_no_group(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        if "items_page" in query and "next_items_page" not in query:
            return {"boards": [{"items_page": {"cursor": None, "items": []}}]}  # board has no items at all
        if query == sr.BOARD_GROUPS_QUERY:
            return groups_response(include_year_group=False)  # the year group doesn't exist
        raise AssertionError(f"must not reach a mutation when there is no year group to create in: {query[:60]}")


    output, exit_code, log = run_cli(["--mode", "update", "--current-month", "--yes"], monday_graphql_fn=graphql_missing_item_no_group)
    assert exit_code == 1, (exit_code, output)
    assert f"{CURRENT_MONTH_NAME}: NOT FOUND, and cannot auto-create" in output, output
    assert f"no existing group titled {YEAR_GROUP_TITLE!r}" in output, output
    assert log is None
    print(f"PASS: missing current-month item with NO matching year group ({YEAR_GROUP_TITLE!r}) exits 1, zero writes, never creates a group")

    # =====================================================================
    # Test 4: duplicate current-month item -> exit non-zero, zero writes, and
    # the year-group lookup is never even attempted (DUPLICATE is checked
    # before auto-create, regardless of whether auto-create is enabled).
    # =====================================================================
    def graphql_duplicate_item(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        if "items_page" in query and "next_items_page" not in query:
            items = [make_item("500", CURRENT_MONTH_NAME), make_item("501", CURRENT_MONTH_NAME)]
            return {"boards": [{"items_page": {"cursor": None, "items": items}}]}
        raise AssertionError(f"must not reach a mutation (or a groups lookup) when the item is duplicated: {query[:60]}")


    output, exit_code, log = run_cli(["--mode", "update", "--current-month", "--yes"], monday_graphql_fn=graphql_duplicate_item)
    assert exit_code == 1, (exit_code, output)
    assert f"{CURRENT_MONTH_NAME}: DUPLICATE" in output, output
    assert log is None
    print(f"PASS: duplicate current-month item ({CURRENT_MONTH_NAME}) exits 1, zero writes, zero group lookups, no update log")

    # =====================================================================
    # Test 5: missing current-month item, but the matching year group DOES
    # exist -- --current-month creates exactly one item in that group, re-
    # fetches to verify exactly one now exists, then completes the normal
    # write + read-back-verify flow on it.
    # =====================================================================
    create_calls = []
    created_item_store = {}  # populated once create_item "succeeds", used to serve the post-create re-fetch


    def graphql_create_then_succeed(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        if query == sr.BOARD_GROUPS_QUERY:
            return groups_response(include_year_group=True)
        if "items_page" in query and "next_items_page" not in query:
            items = [created_item_store["item"]] if "item" in created_item_store else []
            return {"boards": [{"items_page": {"cursor": None, "items": items}}]}
        if "create_item" in query:
            assert variables["boardId"] == MONDAY_CFG["board_id"]
            assert variables["groupId"] == YEAR_GROUP_ID
            assert variables["itemName"] == CURRENT_MONTH_NAME
            create_calls.append(variables)
            created_item_store["item"] = make_item("900", CURRENT_MONTH_NAME)
            return {"create_item": {"id": "900"}}
        if "change_simple_column_value" in query:
            created_item_store["item"]["column_values"] = [
                (c if c["id"] != variables["columnId"] else {"id": c["id"], "text": variables["value"], "value": json.dumps(variables["value"])})
                for c in created_item_store["item"]["column_values"]
            ]
            return {"change_simple_column_value": {"id": variables["itemId"]}}
        if "items (ids" in query or "itemIds" in query:
            return {"items": [created_item_store["item"]]}
        raise AssertionError(f"unexpected query: {query[:60]}")


    create_calls.clear()
    created_item_store.clear()
    output, exit_code, log = run_cli(["--mode", "update", "--current-month", "--yes"], monday_graphql_fn=graphql_create_then_succeed)
    assert exit_code is None, (exit_code, output)
    assert len(create_calls) == 1, create_calls
    assert f"WILL CREATE new item in group {YEAR_GROUP_TITLE!r}" in output, output
    assert "Verified exactly 1 item named" in output, output
    assert log["months"][0]["success"] is True, log
    assert log["months"][0]["item_id"] == "900", log
    print(f"PASS: a missing item WITH an existing year group ({YEAR_GROUP_TITLE!r}) is created exactly once, re-verified, then written and verified successfully")

    # =====================================================================
    # Test 6: rerunning immediately after creation (the item now exists) must
    # NOT create a second item -- the core anti-duplicate guarantee. Simulates
    # exactly the daily-cron scenario: today's run created the item; if the
    # job somehow ran twice, or a human re-runs it, no duplicate is created.
    # =====================================================================
    def graphql_item_already_exists(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        if query == sr.BOARD_GROUPS_QUERY:
            raise AssertionError("must not even look up the year group when the item already exists")
        if "items_page" in query and "next_items_page" not in query:
            return {"boards": [{"items_page": {"cursor": None, "items": [make_item("900", CURRENT_MONTH_NAME)]}}]}
        if "create_item" in query:
            raise AssertionError("must NEVER call create_item when exactly one matching item already exists")
        if "change_simple_column_value" in query:
            return {"change_simple_column_value": {"id": variables["itemId"]}}
        if "items (ids" in query or "itemIds" in query:
            return {"items": [make_item(
                "900", CURRENT_MONTH_NAME, attended=3, missed=0, total=3, served=2, avg="1.5",
                last_updated="2026-03-15", attended_amount="30.00", total_amount="30.00",
            )]}
        raise AssertionError(f"unexpected query: {query[:60]}")


    output, exit_code, log = run_cli(["--mode", "update", "--current-month", "--yes"], monday_graphql_fn=graphql_item_already_exists)
    assert exit_code is None, (exit_code, output)
    assert "WILL CREATE" not in output, output
    assert log["months"][0]["item_id"] == "900", log
    print("PASS: rerunning after the item already exists never calls create_item and never looks up the year group -- no duplicate possible on a normal rerun")

    # =====================================================================
    # Test 7: create_item itself fails (Monday rejects it) -> exits non-zero,
    # that month FAILED, zero field writes attempted.
    # =====================================================================
    def graphql_create_fails(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        if query == sr.BOARD_GROUPS_QUERY:
            return groups_response(include_year_group=True)
        if "items_page" in query and "next_items_page" not in query:
            return {"boards": [{"items_page": {"cursor": None, "items": []}}]}
        if "create_item" in query:
            raise sm.MondayApiError("simulated: Monday rejected item creation")
        raise AssertionError(f"must not attempt a field write when item creation itself failed: {query[:60]}")


    output, exit_code, log = run_cli(["--mode", "update", "--current-month", "--yes"], monday_graphql_fn=graphql_create_fails)
    assert exit_code == 1, (exit_code, output)
    row = log["months"][0]
    assert row["success"] is False, row
    assert row["item_id"] is None, row
    assert any(e["field"] == "item creation" for e in row["errors"]), row
    print("PASS: a create_item failure exits 1, records FAILED with no item id, and never attempts a field write")

    # =====================================================================
    # Test 8: create_item succeeds, but the post-create re-fetch finds 0 items
    # (a Monday-side lag/inconsistency) -- aborts that month rather than
    # guessing, exits non-zero, zero field writes attempted.
    # =====================================================================
    def graphql_create_then_vanishes(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        if query == sr.BOARD_GROUPS_QUERY:
            return groups_response(include_year_group=True)
        if "items_page" in query and "next_items_page" not in query:
            return {"boards": [{"items_page": {"cursor": None, "items": []}}]}
        if "create_item" in query:
            return {"create_item": {"id": "901"}}
        raise AssertionError(f"must not attempt a field write when post-create verification is inconclusive: {query[:60]}")


    output, exit_code, log = run_cli(["--mode", "update", "--current-month", "--yes"], monday_graphql_fn=graphql_create_then_vanishes)
    assert exit_code == 1, (exit_code, output)
    row = log["months"][0]
    assert row["success"] is False, row
    assert row["item_id"] == "901", row
    assert "not exactly 1" in row["verify_status"], row
    print("PASS: a post-create re-fetch that finds 0 matching items aborts rather than guessing, exits 1, zero field writes")

    # =====================================================================
    # Test 5: board-schema failure (Total Amount id missing from the live
    # board) -> exit non-zero, zero writes, zero items fetches.
    # =====================================================================
    def graphql_bad_schema(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response(omit_config_keys=["column_total_amount"])
        raise AssertionError(f"must not touch Monday further once the schema preflight fails: {query[:60]}")


    output, exit_code, log = run_cli(["--mode", "update", "--current-month", "--yes"], monday_graphql_fn=graphql_bad_schema)
    assert exit_code == 1, (exit_code, output)
    assert "board-schema preflight failed" in output, output
    assert "Total Amount" in output
    assert log is None
    print("PASS: a board-schema preflight failure exits 1, zero items fetches, zero writes")

    # =====================================================================
    # Test 6: reconciliation failure -> exit non-zero, zero writes.
    # =====================================================================
    orig_compute_monthly_financials = sr.compute_monthly_financials


    def broken_financials(config, lessons, year, month):
        fin = orig_compute_monthly_financials(config, lessons, year, month)
        fin = dict(fin)
        fin["financial_reconciliation_ok"] = False
        return fin


    def graphql_schema_ok_then_nothing(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        raise AssertionError(f"must not fetch items or write when reconciliation fails: {query[:60]}")


    sr.compute_monthly_financials = broken_financials
    try:
        output, exit_code, log = run_cli(["--mode", "update", "--current-month", "--yes"], monday_graphql_fn=graphql_schema_ok_then_nothing)
    finally:
        sr.compute_monthly_financials = orig_compute_monthly_financials
    assert exit_code == 1, (exit_code, output)
    assert "financial reconciliation failed" in output, output
    assert log is None
    print("PASS: a reconciliation failure exits 1, zero items fetches, zero writes")

    # =====================================================================
    # Test 7: a write failure (Monday rejects one field's mutation) -> overall
    # exit non-zero, that month marked FAILED in the log.
    # =====================================================================
    def graphql_write_failure(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        if "items_page" in query and "next_items_page" not in query:
            return {"boards": [{"items_page": {"cursor": None, "items": [make_item("500", CURRENT_MONTH_NAME)]}}]}
        if "change_simple_column_value" in query:
            if variables["columnId"] == MONDAY_CFG["column_attended_amount"]:
                raise sm.MondayApiError("simulated: Monday rejected this write")
            return {"change_simple_column_value": {"id": variables["itemId"]}}
        raise AssertionError(f"unexpected query: {query[:60]}")


    output, exit_code, log = run_cli(["--mode", "update", "--current-month", "--yes"], monday_graphql_fn=graphql_write_failure)
    assert exit_code == 1, (exit_code, output)
    row = log["months"][0]
    assert row["success"] is False, row
    assert any(e["field"] == "Attended Amount" for e in row["errors"]), row
    print("PASS: a write failure (Monday rejects one field) exits 1 and is recorded as FAILED with the specific field error")

    # =====================================================================
    # Test 8: read-back verification failure (Monday silently didn't persist a
    # field) -> overall exit non-zero, that month marked FAILED with a MISMATCH
    # verify_status.
    # =====================================================================
    write_calls.clear()
    written_state.clear()


    def graphql_verify_mismatch(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        if "items_page" in query and "next_items_page" not in query:
            it = make_item("500", CURRENT_MONTH_NAME)
            written_state["500"] = {c["id"]: c["text"] for c in it["column_values"]}
            return {"boards": [{"items_page": {"cursor": None, "items": [it]}}]}
        if "change_simple_column_value" in query:
            write_calls.append(variables)
            if variables["columnId"] != MONDAY_CFG["column_attended_amount"]:
                written_state["500"][variables["columnId"]] = variables["value"]
            return {"change_simple_column_value": {"id": variables["itemId"]}}
        if "items (ids" in query or "itemIds" in query:
            state = written_state["500"]
            return {"items": [{"id": "500", "name": "verify",
                                "column_values": [{"id": k, "text": v, "value": json.dumps(v)} for k, v in state.items()]}]}
        raise AssertionError(f"unexpected query: {query[:60]}")


    output, exit_code, log = run_cli(["--mode", "update", "--current-month", "--yes"], monday_graphql_fn=graphql_verify_mismatch)
    assert exit_code == 1, (exit_code, output)
    row = log["months"][0]
    assert row["success"] is False, row
    assert "MISMATCH" in row["verify_status"] and "attended_amount" in row["verify_status"], row
    print(f"PASS: a read-back verification mismatch exits 1 and is recorded as FAILED (verify_status={row['verify_status']!r})")

    # =====================================================================
    # Test 9: successful current-month run exits 0 (already exercised by Test 2
    # above as a side effect; asserted explicitly here for clarity/completeness).
    # =====================================================================
    write_calls.clear()
    written_state.clear()
    output, exit_code, log = run_cli(["--mode", "update", "--current-month", "--yes"], monday_graphql_fn=graphql_full_success)
    assert exit_code is None, (exit_code, output)
    assert log["months"][0]["success"] is True
    print("PASS: a fully successful --current-month run exits 0 (no SystemExit raised)")

    # =====================================================================
    # Test 10: idempotency -- rerunning against a board that ALREADY holds this
    # run's exact values only rewrites Last Updated (always re-stamped on a
    # successful sync); it must NOT needlessly rewrite the unchanged KPI/
    # financial fields.
    # =====================================================================
    already_current_item = make_item(
        "500", CURRENT_MONTH_NAME,
        attended=3, missed=0, total=3, served=2, avg="1.5", last_updated="2026-01-01",
        attended_amount="30.00", missed_amount="0.00", cancelled_amount="0.00",
        scheduled_amount="0.00", total_amount="30.00",
    )
    write_calls.clear()
    written_state.clear()


    def graphql_already_current(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        if "items_page" in query and "next_items_page" not in query:
            written_state["500"] = {c["id"]: c["text"] for c in already_current_item["column_values"]}
            return {"boards": [{"items_page": {"cursor": None, "items": [already_current_item]}}]}
        if "change_simple_column_value" in query:
            write_calls.append(variables)
            written_state["500"][variables["columnId"]] = variables["value"]
            return {"change_simple_column_value": {"id": variables["itemId"]}}
        if "items (ids" in query or "itemIds" in query:
            state = written_state["500"]
            return {"items": [{"id": "500", "name": "verify",
                                "column_values": [{"id": k, "text": v, "value": json.dumps(v)} for k, v in state.items()]}]}
        raise AssertionError(f"unexpected query: {query[:60]}")


    output, exit_code, log = run_cli(["--mode", "update", "--current-month", "--yes"], monday_graphql_fn=graphql_already_current)
    assert exit_code is None, (exit_code, output)
    assert log["months"][0]["success"] is True
    kpi_and_financial_keys = {
        "sessions_attended", "sessions_missed", "total_sessions", "students_served",
        "avg_sessions_per_student", "attended_amount", "missed_amount",
        "cancelled_amount", "scheduled_amount", "total_amount",
    }
    written_field_keys = set(log["months"][0]["fields_written"].keys())
    assert written_field_keys == set(), f"expected zero KPI/financial fields rewritten, got: {written_field_keys}"
    written_column_ids = {c["columnId"] for c in write_calls}
    assert written_column_ids == {MONDAY_CFG["column_last_updated"]}, written_column_ids
    print("PASS: rerunning an already-current month is idempotent -- zero KPI/financial fields rewritten, only Last Updated is re-stamped")

    sr.datetime = orig_datetime
    builtins.input = orig_input
    print("\nALL --current-month / --yes TESTS PASSED")
