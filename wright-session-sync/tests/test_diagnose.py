"""Tests proving --diagnose-teachworks is read-only: it must never
instantiate or call MondayClient, and must never print credentials."""

import config
import sync


class FakeDiagnosticTeachworksClient:
    def __init__(self, **kwargs):
        self.calls = []

    def diagnostic_get(self, path, params):
        self.calls.append((path, dict(params)))
        return 200, [{"id": 1, "date": "2026-09-10"}], [{"id": 1, "date": "2026-09-10"}]


class MondayClientMustNotBeInstantiated:
    def __init__(self, *args, **kwargs):
        raise AssertionError("MondayClient must not be instantiated in --diagnose-teachworks mode")


def test_diagnose_teachworks_makes_zero_monday_calls(monkeypatch, capsys):
    monkeypatch.setattr(config, "TEACHWORKS_API_KEY", "fake-teachworks-key")
    monkeypatch.setattr(config, "MONDAY_API_TOKEN", "fake-monday-token")

    fake_tw = FakeDiagnosticTeachworksClient()
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: fake_tw)
    monkeypatch.setattr(sync, "MondayClient", MondayClientMustNotBeInstantiated)

    exit_code = sync.main(["--diagnose-teachworks", "--lookback-days", "5"])

    assert exit_code == 0
    # 9 request variants (bare pagination, dates-only, status-only, full
    # production query, from_date only, to_date only, known-good single day,
    # known-good day + status, known-recent day + status) plus 1 call from
    # the pagination-walk diagnostic that runs automatically afterward
    # (the fake returns a single short record, so it stops after page 1).
    assert len(fake_tw.calls) == 10
    variant_params = [params for _, params in fake_tw.calls]

    assert variant_params[0] == {"page": 1, "per_page": 10}
    assert "status" not in variant_params[1] and "from_date" in variant_params[1] and "to_date" in variant_params[1]
    assert "from_date" not in variant_params[2] and variant_params[2]["status"] == "Attended"
    assert variant_params[3]["status"] == "Attended" and "from_date" in variant_params[3]

    assert "to_date" not in variant_params[4] and "from_date" in variant_params[4] and "status" not in variant_params[4]
    assert "from_date" not in variant_params[5] and "to_date" in variant_params[5] and "status" not in variant_params[5]

    assert variant_params[6] == {"from_date": sync.KNOWN_GOOD_HISTORICAL_DATE, "to_date": sync.KNOWN_GOOD_HISTORICAL_DATE, "page": 1, "per_page": 10}
    assert variant_params[7] == {
        "status": "Attended", "from_date": sync.KNOWN_GOOD_HISTORICAL_DATE, "to_date": sync.KNOWN_GOOD_HISTORICAL_DATE,
        "page": 1, "per_page": 10,
    }
    assert variant_params[8] == {
        "status": "Attended",
        "from_date": sync.KNOWN_RECENT_DATE_WITH_EXPECTED_SESSIONS,
        "to_date": sync.KNOWN_RECENT_DATE_WITH_EXPECTED_SESSIONS,
        "page": 1, "per_page": 100,
    }
    # The pagination-walk call: status only, no date filters.
    assert variant_params[9] == {"status": "Attended", "page": 1, "per_page": 100}


def test_diagnose_teachworks_never_prints_credentials(monkeypatch, capsys):
    monkeypatch.setattr(config, "TEACHWORKS_API_KEY", "super-secret-teachworks-key")
    monkeypatch.setattr(config, "MONDAY_API_TOKEN", "super-secret-monday-token")

    fake_tw = FakeDiagnosticTeachworksClient()
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: fake_tw)
    monkeypatch.setattr(sync, "MondayClient", MondayClientMustNotBeInstantiated)

    sync.main(["--diagnose-teachworks", "--lookback-days", "5"])

    out = capsys.readouterr().out
    assert "super-secret-teachworks-key" not in out
    assert "super-secret-monday-token" not in out
    assert "Authorization" not in out
    assert "zero" in out.lower() or "DIAGNOSTIC COMPLETE" in out


class PagedFakeTeachworksClient:
    """Serves a fixed sequence of pages by page number, for testing the
    pagination-walk diagnostic directly."""

    def __init__(self, pages):
        self.pages = pages  # list of record-lists, one per page (1-indexed)
        self.calls = []

    def diagnostic_get(self, path, params):
        self.calls.append(dict(params))
        page = params["page"]
        records = self.pages[page - 1] if page <= len(self.pages) else []
        return 200, records, records


def test_pagination_diagnostic_reports_earliest_latest_and_stops_on_short_page(capsys):
    pages = [
        [{"id": 1, "from_date": "2024-01-05"}, {"id": 2, "from_date": "2023-06-01"}],
        [{"id": 3, "from_date": "2025-12-31"}],  # short page (< per_page) -> stop here
    ]
    client = PagedFakeTeachworksClient(pages)

    sync.diagnose_teachworks_pagination(client, per_page=2, max_pages=10)

    out = capsys.readouterr().out
    assert len(client.calls) == 2
    assert "Total pages fetched: 2" in out
    assert "Total lessons: 3" in out
    assert "Earliest from_date seen: 2023-06-01" in out
    assert "Latest from_date seen: 2025-12-31" in out
    assert "WARNING" not in out


def test_pagination_diagnostic_reports_safety_cap_reached(capsys):
    # Every page is exactly per_page-sized, so the walk never finds a short
    # page on its own and must be stopped by the cap instead.
    full_page = [{"id": i, "from_date": "2026-01-01"} for i in range(5)]
    client = PagedFakeTeachworksClient([full_page] * 20)

    sync.diagnose_teachworks_pagination(client, per_page=5, max_pages=3)

    out = capsys.readouterr().out
    assert len(client.calls) == 3
    assert "Total pages fetched: 3" in out
    assert "WARNING: safety cap of 3 pages was reached" in out
