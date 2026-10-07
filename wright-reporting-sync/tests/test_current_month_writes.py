"""October and later months are writable (the temporary September-2026-only
guard is gone), through both the nightly --current-month path and a manual
--month update, while every safety check still refuses - and every refusal
exits non-zero so a scheduler cannot report it as a successful run."""

import pytest

from test_month_names import cli  # noqa: F401 - pytest fixture

NIGHTLY = ["--mode", "update", "--current-month", "--yes"]
OCTOBER = ["01 - January 2026", "09 - September 2026", "10 - October 2026"]


@pytest.mark.parametrize("today, board, item_written, created", [
    ((2026, 10), OCTOBER, "502", []),                                     # October exists -> update it
    ((2026, 11), OCTOBER, None, ["11 - November 2026"]),                 # future month -> create, then update
    ((2026, 12), OCTOBER + ["12 - December 2026"], "503", []),
])
def test_nightly_current_month_writes_october_and_later(cli, today, board, item_written, created):
    out, code, monday = cli(NIGHTLY, board, today)
    assert code is None, out                       # normal return == process exit 0
    assert "restricts writes to September" not in out
    assert [c["itemName"] for c in monday.creates] == created
    written = {w["itemId"] for w in monday.writes}
    assert written == ({item_written} if item_written else {"901"})
    assert "All 1 month(s) updated and verified successfully." in out


def test_manual_single_month_october_writes_after_confirmation(cli):
    out, code, monday = cli(["--mode", "update", "--year", "2026", "--month", "10"], OCTOBER, (2026, 10), confirm="YES")
    assert code is None, out
    assert "Matched Monday item id='502' name='10 - October 2026'" in out and "Update complete." in out
    assert {w["itemId"] for w in monday.writes} == {"502"} and monday.creates == []


def test_manual_single_month_october_declined_writes_nothing(cli):
    out, code, monday = cli(["--mode", "update", "--year", "2026", "--month", "10"], OCTOBER, (2026, 10), confirm="no")
    assert code == 0 and "Aborted -- nothing was written to Monday." in out
    assert monday.writes == [] and monday.creates == []


def test_manual_single_month_never_creates_a_missing_month(cli):
    out, code, monday = cli(["--mode", "update", "--year", "2026", "--month", "11"], OCTOBER, (2026, 11), confirm="YES")
    assert code == 1 and "NOT FOUND" in out
    assert monday.writes == [] and monday.creates == []


def _omit_total_amount(monday):
    monday.schema_omit.add("column_total_amount")


def _fail_writes(monday):
    monday.fail_writes = True


def _no_year_group(monday):
    monday.group_titles = ["2025"]


@pytest.mark.parametrize("board, today, configure, expected", [
    (OCTOBER + ["October 2026"], (2026, 10), None, "10 - October 2026: DUPLICATE"),
    (OCTOBER, (2026, 10), _omit_total_amount, "board-schema preflight failed"),
    (["09 - September 2026"], (2026, 10), _no_year_group, "cannot auto-create -- no existing group titled '2026'"),
    (OCTOBER, (2026, 10), _fail_writes, "month(s) FAILED: 10 - October 2026"),
])
def test_every_nightly_refusal_exits_non_zero(cli, board, today, configure, expected):
    out, code, monday = cli(NIGHTLY, board, today, configure=configure)
    assert code == 1, out
    assert expected in out
    assert "updated and verified successfully" not in out
    if configure is not _fail_writes:
        assert monday.writes == [] and monday.creates == []
