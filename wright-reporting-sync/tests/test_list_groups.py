import sys
import io
import contextlib
import builtins
import tempfile
from pathlib import Path


def test_list_groups():

    import audit
    import sync_monday as sm
    import sync_reporting as sr

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

    orig_input = builtins.input
    orig_request_json = audit.request_json


    def teachworks_should_never_be_called(config, path, params=None, max_retries=3):
        raise AssertionError(f"--list-groups must never call Teachworks, but tried to fetch {path}")


    def run_cli(argv):
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
            return buf.getvalue(), exit_code


    # =====================================================================
    # Test 1: --list-groups makes exactly one Monday call (a groups query),
    # zero item creations, zero column-value mutations, zero item-page fetches,
    # and zero Teachworks calls of any kind.
    # =====================================================================
    calls = []


    def graphql_tracking_mutations(cfg, query, variables=None, max_retries=3):
        calls.append(query)
        if query == sr.BOARD_GROUPS_QUERY:
            return {"boards": [{"id": MONDAY_CFG["board_id"], "name": "Teachworks Reporting", "groups": [
                {"id": "group_2026", "title": "2026"},
                {"id": "group_2025", "title": "2025"},
            ]}]}
        if "create_item" in query:
            raise AssertionError("--list-groups must NEVER call create_item")
        if "change_simple_column_value" in query:
            raise AssertionError("--list-groups must NEVER call change_simple_column_value")
        if "items_page" in query or "itemIds" in query:
            raise AssertionError("--list-groups must NEVER fetch board items")
        raise AssertionError(f"--list-groups made an unexpected Monday call: {query[:60]}")


    builtins.input = lambda prompt="": (_ for _ in ()).throw(AssertionError("--list-groups must never prompt for input"))
    audit.request_json = teachworks_should_never_be_called
    sm.monday_graphql = graphql_tracking_mutations

    calls.clear()
    output, exit_code = run_cli(["--list-groups"])
    builtins.input = orig_input
    audit.request_json = orig_request_json

    assert exit_code is None, (exit_code, output)  # returns normally -> process exit 0
    assert len(calls) == 1, f"expected exactly one Monday call (the groups query), got {len(calls)}: {calls}"
    assert calls[0] == sr.BOARD_GROUPS_QUERY
    print("PASS: --list-groups makes exactly one Monday call (the groups query) and nothing else -- no mutations, no item fetches, no Teachworks calls")

    # --- Output content ---
    assert "id='group_2026'" in output and "title='2026'" in output, output
    assert "id='group_2025'" in output and "title='2025'" in output, output
    assert "MONDAY_REPORTING_YEAR_GROUP_TITLE_TEMPLATE resolves this year to:" in output, output
    print("PASS: --list-groups prints every group's real id/title, plus the resolved current-year group title")

    # =====================================================================
    # Test 2: --list-groups is a top-level flag independent of --mode/--year/
    # --month -- it exits before any of that branching logic runs, even if
    # other (irrelevant) flags are also passed.
    # =====================================================================
    calls.clear()
    sm.monday_graphql = graphql_tracking_mutations
    audit.request_json = teachworks_should_never_be_called
    output, exit_code = run_cli(["--list-groups", "--mode", "update", "--current-month", "--yes"])
    assert exit_code is None, (exit_code, output)
    assert len(calls) == 1, calls
    print("PASS: --list-groups short-circuits before any --mode/--current-month/--yes logic, even when those flags are also passed")

    # =====================================================================
    # Test 3: an empty board (no groups at all) is handled gracefully, still
    # read-only, still makes exactly one call.
    # =====================================================================
    def graphql_no_groups(cfg, query, variables=None, max_retries=3):
        calls.append(query)
        return {"boards": [{"id": MONDAY_CFG["board_id"], "name": "Teachworks Reporting", "groups": []}]}


    calls.clear()
    sm.monday_graphql = graphql_no_groups
    output, exit_code = run_cli(["--list-groups"])
    assert exit_code is None, (exit_code, output)
    assert "No groups found on this board." in output, output
    assert len(calls) == 1, calls
    print("PASS: an empty board (zero groups) is reported clearly, still read-only")

    sm.monday_graphql = None  # reset to avoid leaking into any test run after this file
    print("\nALL --list-groups TESTS PASSED")
