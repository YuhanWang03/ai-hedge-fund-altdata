"""Point-in-time S&P 500 membership from the Wikipedia change table."""

from __future__ import annotations

import json

import pytest

from v2.screening import universes as U

HTML = """
<table class="wikitable sortable" id="changes"><tbody>
<tr><th rowspan="2">Effective Date</th><th colspan="2">Added</th><th colspan="2">Removed</th><th rowspan="2">Reason</th></tr>
<tr><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th></tr>
<tr><td>July 9, 2025</td><td>BRK.B</td><td>Berkshire</td><td></td><td></td><td>x</td></tr>
<tr><td>June 30, 2025</td><td></td><td></td><td>ANSS</td><td>Ansys</td><td>Acquired by Synopsys.</td></tr>
<tr><td>March 24, 2025</td><td>DASH</td><td>DoorDash</td><td>BWA</td><td>BorgWarner</td><td>Market cap.</td></tr>
<tr><td rowspan="2">September 23, 2024</td><td>PLTR</td><td>Palantir</td><td>AAL</td><td>American Airlines</td><td rowspan="2">Market cap change.</td></tr>
<tr><td>DELL</td><td>Dell</td><td>ETSY</td><td>Etsy</td></tr>
</tbody></table>
<table class="wikitable"><tr><th>Symbol</th><th>Security</th></tr><tr><td>AAPL</td><td>Apple</td></tr></table>
"""


def test_parse_changes_handles_rowspan_dates_empty_cells_and_dotted_tickers():
    rows = U.parse_changes(HTML)
    assert [(r["date"], r["added"], r["removed"]) for r in rows] == [
        ("2025-07-09", "BRK.B", None), ("2025-06-30", None, "ANSS"), ("2025-03-24", "DASH", "BWA"),
        ("2024-09-23", "PLTR", "AAL"), ("2024-09-23", "DELL", "ETSY"),
    ]
    assert rows[2]["added_name"] == "DoorDash" and rows[2]["removed_name"] == "BorgWarner"
    assert U.parse_constituents(HTML, U._HEADERS) == ["AAPL"]  # the constituent parser still picks the other table


def test_members_at_rewinds_todays_list_through_the_changes(tmp_path, monkeypatch):
    path = tmp_path / "universes.json"
    monkeypatch.setattr(U, "DATA_PATH", path)
    assert U.members_at("sp500", "2024-06-01") == (U._dedupe(U.SP500), False)  # no history → today's list, flagged
    assert U.membership_lookup("sp500") is None

    today = ["AAPL", "PLTR", "DELL", "DASH", "BRK.B"]
    path.write_text(json.dumps({"sp500": {"tickers": today, "as_of": "2025-09-01", "changes": U.parse_changes(HTML)}}))
    assert U.members_at("sp500", "2025-09-01") == (today, True)                       # on/after the snapshot date
    assert U.members_at("sp500", "2025-07-01") == (sorted(["AAPL", "PLTR", "DELL", "DASH"]), True)   # before BRK.B joined
    assert U.members_at("sp500", "2025-06-01") == (sorted(["AAPL", "PLTR", "DELL", "DASH", "ANSS"]), True)  # ANSS still in
    assert U.members_at("sp500", "2024-09-01") == (sorted(["AAPL", "AAL", "ETSY", "BWA", "ANSS"]), True)     # before the 2024 changes
    lookup = U.membership_lookup("sp500")
    assert lookup is not None and "AAL" in lookup("2024-09-01") and "PLTR" not in lookup("2024-09-01")
    status = U.universe_status()["sp500"]
    assert status["changes"] == 5 and status["history_from"] == "2024-09-23"
    with pytest.raises(KeyError):
        U.members_at("nope", "2024-01-01")
