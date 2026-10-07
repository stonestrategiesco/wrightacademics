"""Reproduces the October 2026 production failure: the nightly current-month
update kept loading a Teachworks cache fetched on 2026-09-30 from a persistent
output/_cache, so every October lesson still looked Scheduled and October was
written as 0 sessions. The current-month update must always fetch fresh data,
and must refuse to write from anything older than the run itself, while
historical/manual runs can still use the cache."""

import builtins
import contextlib
import datetime as real_datetime_module
import io
import json
import sys

import pytest

import audit
import sync_monday as sm
import sync_reporting as sr
from test_month_names import MONDAY_CFG, TW_CONFIG, FakeMonday

STUDENTS = [{"id": i, "first_name": f"S{i}", "last_name": "T"} for i in (1, 2, 3)]
SEPT_30_FETCH = "2026-09-30T11:16:43.663669+00:00"


def lesson(lesson_id, day, student_id, status):
    return {"id": lesson_id, "from_datetime": f"2026-10-{day:02d}T15:00:00Z",
            "participants": [{"student_id": student_id, "status": status, "amount": "50.00"}]}


def seed_cache(out_dir, lessons, fetched_at):
    cache = out_dir / "_cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "students_raw.json").write_text(json.dumps(STUDENTS))
    (cache / "lessons_raw.json").write_text(json.dumps(lessons))
    (cache / "cache_meta.json").write_text(json.dumps({
        "fetched_at": fetched_at, "students_total": len(STUDENTS), "lessons_total": len(lessons),
        "students_pages": 1, "lessons_pages": 1}))


def cached_fetched_at(out_dir):
    return json.loads((out_dir / "_cache" / "cache_meta.json").read_text())["fetched_at"]


class World:
    """Teachworks (current lesson statuses) + Monday, sharing one persistent output dir
    like Railway's /app/output."""

    def __init__(self, monkeypatch, tmp_path, teachworks_lessons, board):
        self.monkeypatch = monkeypatch
        self.out_dir = tmp_path / "output"
        self.out_dir.mkdir()
        self.teachworks_lessons = teachworks_lessons
        self.teachworks_requests = 0
        self.monday = FakeMonday(board)

        def fake_request_json(config, path, params=None, max_retries=3):
            class R:
                url, status_code = "https://fake", 200
            self.teachworks_requests += 1
            page = (params or {}).get("page", 1)
            data = STUDENTS if path == config["students_path"] else json.loads(json.dumps(self.teachworks_lessons))
            return (data if page == 1 else []), R()

        monkeypatch.setattr(audit, "request_json", fake_request_json)
        monkeypatch.setattr(audit, "load_config", lambda: TW_CONFIG)
        monkeypatch.setattr(sr, "load_reporting_monday_config", lambda: MONDAY_CFG)
        monkeypatch.setattr(sm, "monday_graphql", self.monday)
        monkeypatch.setattr(builtins, "input", lambda prompt="": (_ for _ in ()).throw(AssertionError("stdin read")))

    def set_clock(self, year, month, day, hour=9):
        class Clock(real_datetime_module.datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime_module.datetime(year, month, day, hour, 0, 0, tzinfo=tz)
        # sync_reporting decides "today"; audit stamps the cache's fetched_at.
        self.monkeypatch.setattr(sr, "datetime", Clock)
        self.monkeypatch.setattr(audit, "datetime", Clock)

    def run(self, argv):
        self.monkeypatch.setattr(sys, "argv", ["sync_reporting.py"] + argv + ["--output-dir", str(self.out_dir)])
        buf, code = io.StringIO(), None
        try:
            with contextlib.redirect_stdout(buf):
                sr.main()
        except SystemExit as e:
            code = e.code
        return buf.getvalue(), code

    def october_value(self, config_key):
        item = next(i for i in self.monday.items.values() if i["name"] == "10 - October 2026")
        return next(c["text"] for c in item["column_values"] if c["id"] == MONDAY_CFG[config_key])


NIGHTLY = ["--mode", "update", "--current-month", "--yes"]


def test_september_30_cache_is_not_used_for_october_nightly(monkeypatch, tmp_path):
    """The exact production failure: Sept 30 cache with October lessons still Scheduled."""
    stale = [lesson(1, 1, 1, "Scheduled"), lesson(2, 2, 2, "Scheduled"), lesson(3, 3, 3, "Scheduled")]
    current = [lesson(1, 1, 1, "Attended"), lesson(2, 2, 2, "Attended"), lesson(3, 3, 3, "Missed")]
    world = World(monkeypatch, tmp_path, current, ["09 - September 2026", "10 - October 2026"])
    seed_cache(world.out_dir, stale, SEPT_30_FETCH)
    world.set_clock(2026, 10, 7)

    out, code = world.run(NIGHTLY)

    assert code is None, out
    assert world.teachworks_requests > 0, "the nightly must fetch from Teachworks"
    assert "Using cached Teachworks data" not in out
    assert "Teachworks source: FRESH -- fetched by this run at 2026-10-07T09:00:00+00:00" in out
    assert cached_fetched_at(world.out_dir) == "2026-10-07T09:00:00+00:00"  # cache replaced, not reused
    assert world.october_value("column_sessions_attended") == "2"          # not 0 from the Sept 30 snapshot
    assert world.october_value("column_sessions_missed") == "1"
    assert world.october_value("column_total_sessions") == "3"
    assert world.october_value("column_students_served") == "3"
    assert "All 1 month(s) updated and verified successfully." in out


def test_scheduled_lesson_later_attended_is_reflected_on_the_next_nightly(monkeypatch, tmp_path):
    teachworks = [lesson(1, 1, 1, "Attended"), lesson(2, 6, 2, "Scheduled")]
    world = World(monkeypatch, tmp_path, teachworks, ["10 - October 2026"])

    world.set_clock(2026, 10, 7)
    out, code = world.run(NIGHTLY)
    assert code is None, out
    assert world.october_value("column_sessions_attended") == "1"
    requests_after_first = world.teachworks_requests

    world.teachworks_lessons = [lesson(1, 1, 1, "Attended"), lesson(2, 6, 2, "Attended")]  # marked Attended in Teachworks
    world.set_clock(2026, 10, 8)
    out, code = world.run(NIGHTLY)
    assert code is None, out
    assert world.teachworks_requests > requests_after_first, "the second nightly must fetch again, not reuse Oct 7's cache"
    assert "Teachworks source: FRESH -- fetched by this run at 2026-10-08T09:00:00+00:00" in out
    assert world.october_value("column_sessions_attended") == "2"
    assert world.october_value("column_students_served") == "2"


@pytest.mark.parametrize("cache_time", [SEPT_30_FETCH, "2026-10-07T06:00:00+00:00"])
def test_current_month_update_refuses_data_older_than_the_run(monkeypatch, tmp_path, cache_time):
    """Fail-safe on its own: even if the refresh were skipped, a current-month update will
    not write from a cache - from a previous day or earlier the same day."""
    world = World(monkeypatch, tmp_path, [lesson(1, 1, 1, "Attended")], ["10 - October 2026"])
    seed_cache(world.out_dir, [lesson(1, 1, 1, "Scheduled")], cache_time)
    world.set_clock(2026, 10, 7)

    buf = io.StringIO()
    with pytest.raises(SystemExit) as exc, contextlib.redirect_stdout(buf):
        sr.run_multi_update(TW_CONFIG, MONDAY_CFG, world.out_dir, 2026, 10, 10, refresh_teachworks_cache=False,
                            allow_incomplete_month=True, skip_confirmation=True, auto_create_missing=True,
                            require_fresh_source=True)
    out = buf.getvalue()
    assert exc.value.code == 1
    assert f"Teachworks source: CACHE -- loaded from {world.out_dir / '_cache'}, fetched at {real_datetime_module.datetime.fromisoformat(cache_time).isoformat()}" in out
    assert "Refusing to write:" in out and "Nothing was written to Monday." in out
    assert world.teachworks_requests == 0
    assert world.monday.writes == [] and world.monday.creates == []


def test_historical_and_manual_runs_can_still_use_the_cache(monkeypatch, tmp_path):
    september = [{"id": 90 + d, "from_datetime": f"2026-09-{d:02d}T15:00:00Z",
                  "participants": [{"student_id": 1, "status": "Attended", "amount": "10.00"}]} for d in (1, 2)]
    world = World(monkeypatch, tmp_path, september, ["09 - September 2026", "10 - October 2026"])
    seed_cache(world.out_dir, september, SEPT_30_FETCH)
    world.set_clock(2026, 10, 7)

    out, code = world.run(["--mode", "dry-run", "--year", "2026", "--month-range", "9-9"])
    assert code is None, out
    assert world.teachworks_requests == 0
    assert f"Teachworks source: CACHE -- loaded from {world.out_dir / '_cache'}, fetched at 2026-09-30T11:16:43.663669+00:00" in out

    out, code = world.run(["--mode", "update", "--year", "2026", "--month-range", "9-9", "--yes"])
    assert code is None, out
    assert world.teachworks_requests == 0
    assert "Teachworks source: CACHE" in out
    assert {w["itemId"] for w in world.monday.writes} == {"500"}

    out, code = world.run(["--mode", "update", "--year", "2026", "--month-range", "9-9", "--yes", "--refresh-teachworks-cache"])
    assert code is None, out
    assert world.teachworks_requests > 0 and "Teachworks source: FRESH" in out
