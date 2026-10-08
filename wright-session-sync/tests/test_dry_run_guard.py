"""--dry-run must never write to Monday, for --daily-sync or any other command.
Uses the real MondayClient over a fake HTTP session, so the assertion is on what
would actually have been sent."""

import json
from unittest.mock import MagicMock

import pytest

import config
import sync
from monday_client import MondayClient
from test_reconcile import FakeTeachworks, tw_lesson


class FakeMondayHTTP:
    def __init__(self):
        self.queries = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.queries.append(json["query"])
        resp = MagicMock(status_code=200)
        if "mutation" in json["query"].lower():
            resp.json.return_value = {"data": {"create_item": {"id": "1"}, "change_column_value": {"id": "1"},
                                               "change_multiple_column_values": {"id": "1"}}}
        else:
            resp.json.return_value = {"data": {"boards": [{"items_page": {"cursor": None, "items": []}}]}}
        return resp

    def mutations(self):
        return [q for q in self.queries if "mutation" in q.lower()]


def real_monday(http):
    session = MagicMock()
    session.post.side_effect = http.post
    return MondayClient(api_token="t", session=session, max_retries=1)


@pytest.fixture
def cli(monkeypatch):
    http = FakeMondayHTTP()
    tw = FakeTeachworks([tw_lesson(1, "2026-10-02", [(10, "Ann")])],
                        [{"id": 10, "first_name": "Ann", "last_name": "Lee", "status": "Active"}])
    monkeypatch.setattr(config, "TEACHWORKS_API_KEY", "k")
    monkeypatch.setattr(config, "MONDAY_API_TOKEN", "t")
    monkeypatch.setattr(config, "COL_TEACHWORKS_SYNC_FLAG", "")
    # Student creation fully enabled: --dry-run alone must still prevent every write.
    monkeypatch.setattr(config, "STUDENT_SYNC_CREATE", True)
    monkeypatch.setattr(config, "MONDAY_STUDENTS_NEW_GROUP_ID", "group_new")
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: tw.client())
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: real_monday(http))
    return http


def test_make_read_only_blocks_every_write_and_any_mutation():
    http = FakeMondayHTTP()
    monday = sync.make_read_only(real_monday(http))
    for call in (lambda: monday.create_session_item(1, "g", "n", {}),
                 lambda: monday.create_student_item(1, "g", "n", {}),
                 lambda: monday.connect_student(1, "1", "c", "2"),
                 lambda: monday.update_item_columns(1, "1", {}),
                 lambda: monday.update_student_columns(1, "1", {}),
                 lambda: monday._execute("mutation { archive_item(item_id: 1) { id } }")):
        with pytest.raises(sync.DryRunWriteBlocked):
            call()
    assert monday.get_items(config.MONDAY_SESSIONS_BOARD_ID, [config.COL_UNIQUE_ID]) == []   # reads still work
    assert http.mutations() == [] and len(http.queries) == 1


def test_daily_sync_dry_run_sends_no_mutation(cli, capsys):
    assert sync.main(["--daily-sync", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert cli.mutations() == [] and cli.queries                       # read, never wrote
    assert "rolling 30-day reconciliation | DRY RUN - Monday writes are blocked" in out
    assert "Reconciliation window:" in out and "Would create:" in out
    assert "SKIPPED - dry run." in out
    assert "WOULD CREATE:\n  - Ann Lee (Teachworks ID 10" in out and "NOT CREATED: dry run; nothing created" in out


def test_dry_run_still_blocks_a_write_if_the_code_had_a_bug(cli, monkeypatch, capsys):
    def buggy_reconcile(tw, monday, start, end, dry_run=False):
        monday.create_session_item(config.MONDAY_SESSIONS_BOARD_ID, config.MONDAY_SESSION_GROUP_ID, "x", {})

    monkeypatch.setattr(sync, "reconcile_session_log", buggy_reconcile)
    assert sync.main(["--daily-sync", "--dry-run"]) == 3
    assert cli.mutations() == []
    assert "ABORTED: --dry-run: blocked MondayClient.create_session_item(). Nothing was written to Monday." in capsys.readouterr().out


def test_dry_run_guards_other_write_commands_too(cli, capsys):
    assert sync.main(["--post-baseline-rollups", "--apply", "--dry-run"]) == 1   # refused by its own check
    assert sync.main(["--apply-baseline-migration", "--dry-run"]) in (0, 1, 3)
    assert cli.mutations() == []


def test_live_daily_sync_does_write(cli, monkeypatch):
    monkeypatch.setattr(sync, "apply_post_baseline_student_rollup_updates",
                        lambda *a, **k: {"results": [], "written": [], "unchanged": [], "skipped_unmatched": [], "failed": []})
    assert sync.main(["--daily-sync"]) == 0
    assert sum("create_item" in q for q in cli.mutations()) == 2      # the student, then their session
