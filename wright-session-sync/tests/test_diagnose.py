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
    # Four request variants, exactly as specified: bare pagination, dates-only,
    # status-only, and the full production query.
    assert len(fake_tw.calls) == 4
    variant_params = [params for _, params in fake_tw.calls]
    assert variant_params[0] == {"page": 1, "per_page": 10}
    assert "status" not in variant_params[1] and "from_date" in variant_params[1]
    assert "from_date" not in variant_params[2] and variant_params[2]["status"] == "Attended"
    assert variant_params[3]["status"] == "Attended" and "from_date" in variant_params[3]


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
