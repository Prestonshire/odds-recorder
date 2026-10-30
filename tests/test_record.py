import datetime as dt
import gzip
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "recorder"))
import record as R  # noqa: E402

NOW = dt.datetime(2026, 10, 31, 12, 0, tzinfo=dt.timezone.utc)
MATCHUPS = [
    {"id": 1, "type": "matchup", "startTime": "2026-10-31T20:00:00Z", "league": {"id": 487, "name": "NBA"},
     "participants": [{"alignment": "home", "name": "Lakers"}, {"alignment": "away", "name": "Celtics"}]},
    {"id": 2, "type": "matchup", "parentId": 1, "startTime": "2026-10-31T20:00:00Z"},           # a special: skipped
    {"id": 3, "type": "matchup", "startTime": "2026-11-05T20:00:00Z", "participants": []},      # too far ahead
]


def markets(price_home=-120, limit=3000):
    return [
        {"matchupId": 1, "type": "moneyline", "period": 0, "status": "open",
         "limits": [{"type": "maxRiskStake", "amount": limit}],
         "prices": [{"designation": "home", "price": price_home}, {"designation": "away", "price": 105}]},
        {"matchupId": 1, "type": "total", "period": 0, "isAlternate": True, "prices": [{"designation": "over", "price": 100, "points": 230}]},
        {"matchupId": 1, "type": "team_total", "period": 0, "prices": [{"designation": "over", "price": 100}]},
    ]


def test_us_to_dec():
    assert R.us_to_dec(150) == 2.5 and R.us_to_dec(-200) == 1.5 and R.us_to_dec(None) is None


def test_games_and_state_filters():
    g = R.games_from(MATCHUPS, NOW)
    assert list(g) == [1] and g[1]["home"] == "Lakers"
    s = R.state_from(markets(), g)
    assert set(s) == {(1, "moneyline", 0, "home", None), (1, "moneyline", 0, "away", None)}


def test_changes_only_after_snapshot():
    g = R.games_from(MATCHUPS, NOW)
    a, b = R.state_from(markets(), g), R.state_from(markets(price_home=-130), g)
    assert len(R.changes({}, a, "t0", True)) == 2
    rows = R.changes(a, b, "t1", False)
    assert len(rows) == 1 and rows[0][7] == -130 and rows[0][9] == 3000


def test_run_writes_files(tmp_path):
    calls = {"n": 0}
    t = {"now": NOW}

    def sleep(sec):
        t["now"] += dt.timedelta(seconds=sec)

    def fetcher(path):
        if path.endswith("/matchups"):
            return MATCHUPS
        calls["n"] += 1
        return markets(price_home=-120 - calls["n"])

    paths = R.run(5, 150, {"basketball": 4}, out_root=str(tmp_path), fetcher=fetcher,
                  clock=lambda: t["now"], sleep=sleep, say=lambda s: None)
    assert any(p.endswith("basketball_1200.csv.gz") for p in paths)
    lines = gzip.open([p for p in paths if p.endswith("1200.csv.gz")][0], "rt").read().splitlines()
    assert lines[0].startswith("ts_utc,snapshot") and len(lines) == 1 + 2 + 2  # header, snapshot, 2 changes
