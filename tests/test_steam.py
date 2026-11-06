import base64
import datetime as dt
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "recorder"))
import record as R  # noqa: E402
import steam as S  # noqa: E402

UTC = dt.timezone.utc
SAT = dt.datetime(2026, 11, 7, 12, 0, tzinfo=UTC)      # a Saturday
GAMES = {7: {"league_id": 1, "league": "England - Premier League", "home": "Arsenal FC", "away": "Chelsea",
             "start": SAT + dt.timedelta(hours=3)}}


def st(home, draw, away, stake):
    """Pinnacle state for game 7 (prices as American odds)."""
    return {(7, "moneyline", 0, "home", None): (home, stake, "open"),
            (7, "moneyline", 0, "draw", None): (draw, stake, "open"),
            (7, "moneyline", 0, "away", None): (away, stake, "open")}


# ------------------------------------------------------------------ maths and rules
def test_shin_adds_to_one_and_favours_favourites_less_than_proportional():
    p = S.shin([1.8, 3.6, 4.5])
    assert abs(sum(p) - 1) < 1e-9
    prop = [1 / 1.8 / (1 / 1.8 + 1 / 3.6 + 1 / 4.5)]
    assert p[0] > prop[0]          # Shin moves margin towards long shots, so the favourite's chance is higher
    assert S.shin([2.0, 2.0]) == [0.5, 0.5]


def test_rule_b_needs_a_raised_max_stake_and_rule_a_does_not():
    d = S.Detector()
    assert d.update("soccer", st(110, 240, 260, 3000), GAMES, SAT) == []
    t = d.update("soccer", st(-105, 250, 280, 5000), GAMES, SAT + dt.timedelta(minutes=5))
    assert len(t) == 1 and t[0]["side"] == "home" and t[0]["rule_b"] and t[0]["rise"] >= 0.02
    d2 = S.Detector()
    d2.update("soccer", st(110, 240, 260, 3000), GAMES, SAT)
    t2 = d2.update("soccer", st(-105, 250, 280, 3000), GAMES, SAT + dt.timedelta(minutes=5))
    assert len(t2) == 1 and not t2[0]["rule_b"] and not t2[0]["rule_c"]


def test_no_trigger_when_stake_low_start_far_or_already_running():
    d = S.Detector()
    d.update("soccer", st(110, 240, 260, 500), GAMES, SAT)
    assert d.update("soccer", st(-105, 250, 280, 900), GAMES, SAT + dt.timedelta(minutes=5)) == []   # stake < 1000
    far = {7: dict(GAMES[7], start=SAT + dt.timedelta(hours=9))}
    d = S.Detector()
    d.update("soccer", st(110, 240, 260, 3000), far, SAT)
    assert d.update("soccer", st(-105, 250, 280, 5000), far, SAT + dt.timedelta(minutes=5)) == []   # > 6 h away
    d = S.Detector()
    d.update("soccer", st(110, 240, 260, 3000), GAMES, SAT)
    assert len(d.update("soccer", st(-105, 250, 280, 5000), GAMES, SAT + dt.timedelta(minutes=5))) == 1
    assert d.update("soccer", st(-110, 255, 285, 6000), GAMES, SAT + dt.timedelta(minutes=8)) == []  # same event


def test_old_readings_fall_out_of_the_hour():
    d = S.Detector()
    d.update("soccer", st(110, 240, 260, 3000), GAMES, SAT - dt.timedelta(minutes=70))
    assert d.update("soccer", st(-105, 250, 280, 5000), GAMES, SAT) == []


def test_sampling_is_fixed_and_about_one_in_ten():
    hits = sum(S.sampled(g, "home", SAT) for g in range(5000))
    assert 400 < hits < 600
    assert S.sampled(123, "away", SAT) == S.sampled(123, "away", SAT)


def test_matching_names_and_times():
    ev = [{"id": "e1", "commence_time": "2026-11-07T15:00:00Z", "home_team": "Arsenal", "away_team": "Chelsea"},
          {"id": "e2", "commence_time": "2026-11-07T15:00:00Z", "home_team": "Everton", "away_team": "Fulham"},
          {"id": "e3", "commence_time": "2026-11-07T19:00:00Z", "home_team": "Arsenal", "away_team": "Chelsea"}]
    e, score, swapped = S.match_event(GAMES[7], ev)
    assert e["id"] == "e1" and score >= 0.7 and not swapped
    e, _s, _w = S.match_event(dict(GAMES[7], home="Chelsea", away="Arsenal"), ev)
    assert e["id"] == "e1"
    assert S.match_event(dict(GAMES[7], home="Leeds", away="Burnley"), ev)[0] is None
    assert S.sim("Bayern München", "Bayern Munchen") > 0.9


def test_trim_reply_keeps_only_match_winner_prices():
    d = {"data": [{"id": "e1", "bookmakers": [
        {"key": "william_hill", "last_update": "2026-11-07T12:00:01Z", "link": "x",
         "markets": [{"key": "h2h", "outcomes": [{"name": "Arsenal", "price": 2.1, "extra": 1}]},
                     {"key": "totals", "outcomes": []}]}]}]}
    assert S.trim_reply(d, "e1") == [{"k": "william_hill", "u": "2026-11-07T12:00:01Z",
                                       "o": [{"name": "Arsenal", "price": 2.1}]}]


# ------------------------------------------------------------------ the logger with fake OddsRelay and GitHub
class Fake:
    def __init__(self, balance=40000, ledger=None, raw_cost="1", delta=None):
        self.calls, self.puts, self.balance, self.ledger, self.raw_cost = [], {}, balance, ledger, raw_cost
        self.delta = delta or []      # venues returned by the "changed since" second look

    def __call__(self, method, url, headers, body=None, timeout=30):
        self.calls.append((method, url, dict(headers)))
        if url.startswith(S.GH_API):
            path = url.split("/contents/", 1)[1]
            if method == "GET":
                if path.endswith("ledger.json") and self.ledger is not None:
                    c = base64.b64encode(json.dumps(self.ledger).encode()).decode()
                    return 200, {}, json.dumps({"content": c, "sha": "abc"}).encode()
                return 404, {}, b"{}"
            self.puts[path] = json.loads(body)
            return 201, {}, b"{}"
        h = {"x-tokens-remaining": str(self.balance)}
        if "/usage" in url:
            return 200, h, json.dumps({"tokens": {"remaining": self.balance}}).encode()
        if "/events" in url:
            return 200, dict(h, **{"x-tokens-cost": "0"}), json.dumps({"data": [
                {"id": "e1", "commence_time": "2026-11-07T15:00:00Z", "home_team": "Arsenal",
                 "away_team": "Chelsea"}], "meta": {"next_cursor": None}}).encode()
        if "/odds/raw" in url and "quote=true" in url:
            return 200, dict(h, **{"x-tokens-cost": "0"}), json.dumps({"cost": 3, "remaining": self.balance}).encode()
        if "/odds/raw" in url and "updatedSince=" in url:
            body = {"data": [{"id": "e1", "bookmakers": self.delta}] if self.delta else []}
            return 200, dict(h, **{"x-tokens-cost": "0.1"}), json.dumps(body).encode()
        if "/odds/raw" in url:
            if headers.get("If-None-Match") == "W/1":
                return 304, dict(h, **{"x-tokens-cost": "0"}), b""
            self.balance -= 1
            return 200, dict(h, **{"x-tokens-cost": self.raw_cost, "etag": "W/1"}), json.dumps({"data": [
                {"id": "e1", "bookmakers": [{"key": "william_hill", "last_update": "t",
                                             "markets": [{"key": "h2h", "outcomes": [
                                                 {"name": "Arsenal", "price": 2.2}]}]}]}]}).encode()
        return 404, h, b""


def logger(fake, clock, out=None):
    said = []
    lg = S.SteamLogger("KEY-SECRET", "GH-SECRET", out or "/nonexistent", SAT, http_fn=fake, clock=clock,
                       say=said.append)
    return lg, said


def drive(lg, t):
    lg.on_reading("soccer", st(110, 240, 260, 3000), GAMES, t["now"])
    t["now"] += dt.timedelta(minutes=2, seconds=30)
    lg.on_reading("soccer", st(-105, 250, 280, 5000), GAMES, t["now"])


def test_rule_b_trigger_gets_two_snapshots_and_saves_privately_without_printing_prices(monkeypatch):
    monkeypatch.setattr(S, "full_check", lambda cid: False)
    t = {"now": SAT}
    fake = Fake()
    lg, said = logger(fake, lambda: t["now"])
    drive(lg, t)
    raw = [c for c in fake.calls if "/odds/raw" in c[1] and "quote=true" not in c[1]]
    assert len(raw) == 1 and "eventIds=e1" in raw[0][1]
    t["now"] += dt.timedelta(seconds=30)
    lg.tick()
    assert len([c for c in fake.calls if "/odds/raw" in c[1] and "quote=true" not in c[1]]) == 1          # not due yet
    t["now"] += dt.timedelta(seconds=31)
    lg.tick()
    raw = [c for c in fake.calls if "/odds/raw" in c[1] and "quote=true" not in c[1]]
    assert len(raw) == 2 and "updatedSince=2026-11-07T12:02:25Z" in raw[1][1]   # cheap "changed since" look
    lg.finish(sleep=lambda s: None)
    paths = sorted(fake.puts)
    assert all(p.startswith("data/steam_logger/") for p in paths) and len(paths) == 3
    checks = gzip.decompress(base64.b64decode(fake.puts[[p for p in paths if "checks" in p][0]]["content"]))
    c = json.loads(checks.decode().splitlines()[0])
    assert c["status2"] == 200 and c["reply2"] == c["reply1"] and c["reply1"][0]["k"] == "william_hill"
    assert c["method2"] == "changed-since" and c["changed2"] == []
    ledger = json.loads(base64.b64decode(fake.puts["data/steam_logger/ledger.json"]["content"]))
    assert abs(ledger["total"] - 1.1) < 1e-9 and abs(ledger["days"]["2026-11-07"] - 1.1) < 1e-9
    text = " ".join(said)
    for secret in ("2.2", "william", "Arsenal", "KEY-SECRET", "GH-SECRET"):
        assert secret not in text
    gh = [c for c in fake.calls if c[1].startswith(S.GH_API)]
    assert all("/repos/Prestonshire/betting-system/" in c[1] for c in gh)


def test_daily_cap_and_balance_floor_stop_paid_checks():
    t = {"now": SAT}
    fake = Fake(ledger={"days": {"2026-11-07": 499}, "total": 1000})
    lg, _ = logger(fake, lambda: t["now"])
    drive(lg, t)
    assert not [c for c in fake.calls if "/odds/raw" in c[1] and "quote=true" not in c[1]] and lg.counts["skipped_cap"] == 1
    t = {"now": SAT}
    fake = Fake(balance=1501)
    lg, _ = logger(fake, lambda: t["now"])
    drive(lg, t)
    assert not [c for c in fake.calls if "/odds/raw" in c[1] and "quote=true" not in c[1]]
    t = {"now": SAT}
    fake = Fake(ledger={"days": {}, "total": 6799})
    lg, _ = logger(fake, lambda: t["now"])
    drive(lg, t)
    assert not [c for c in fake.calls if "/odds/raw" in c[1] and "quote=true" not in c[1]]


def test_weekday_cap_is_150():
    mon = dt.datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    games = {7: dict(GAMES[7], start=mon + dt.timedelta(hours=3))}
    t = {"now": mon}
    fake = Fake(ledger={"days": {"2026-11-09": 149}, "total": 149})
    lg, _ = logger(fake, lambda: t["now"])
    lg.on_reading("soccer", st(110, 240, 260, 3000), games, t["now"])
    t["now"] += dt.timedelta(minutes=2)
    lg.on_reading("soccer", st(-105, 250, 280, 5000), games, t["now"])
    assert lg.counts["skipped_cap"] == 1


def test_after_the_end_date_nothing_is_paid_for():
    late = dt.datetime(2026, 12, 5, 12, 0, tzinfo=UTC)
    games = {7: dict(GAMES[7], start=late + dt.timedelta(hours=3))}
    t = {"now": late}
    fake = Fake()
    lg, _ = logger(fake, lambda: t["now"])
    lg.on_reading("soccer", st(110, 240, 260, 3000), games, t["now"])
    t["now"] += dt.timedelta(minutes=2)
    lg.on_reading("soccer", st(-105, 250, 280, 5000), games, t["now"])
    assert lg.counts["after_end"] == 1 and not [c for c in fake.calls if "/odds/" in c[1]]


def test_a_only_triggers_not_sampled_cost_nothing(monkeypatch):
    monkeypatch.setattr(S, "sampled", lambda *a: False)
    t = {"now": SAT}
    fake = Fake()
    lg, _ = logger(fake, lambda: t["now"])
    lg.on_reading("soccer", st(110, 240, 260, 3000), GAMES, t["now"])
    t["now"] += dt.timedelta(minutes=2)
    lg.on_reading("soccer", st(-105, 250, 280, 3000), GAMES, t["now"])      # stake unchanged: A only
    assert lg.counts["not_sampled"] == 1 and not [c for c in fake.calls if "/odds/" in c[1]]


def test_errors_inside_the_logger_never_stop_the_recorder(tmp_path):
    class Broken:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise RuntimeError("boom")
            return boom
    lg, said = logger(Broken(), lambda: SAT)
    drive(lg, {"now": SAT})
    lg.tick()
    lg.finish(sleep=lambda s: None)
    assert lg.counts["errors"] >= 1 and all("boom" not in s for s in said)


def test_off_without_both_secrets(monkeypatch):
    monkeypatch.delenv("ODDSRELAY_KEY", raising=False)
    monkeypatch.setenv("STEAM_PUSH_TOKEN", "x")
    assert S.from_env("/x", SAT, say=lambda s: None) is None


def test_recorder_run_with_logger_seeds_ticks_and_finishes(tmp_path):
    calls = []

    class Spy:
        def seed(self, s): calls.append("seed")
        def on_reading(self, *a): calls.append("reading")
        def tick(self, *a): calls.append("tick")
        def finish(self, sleep): calls.append("finish")

    t = {"now": SAT}

    def sleep(sec):
        t["now"] += dt.timedelta(seconds=sec)

    def fetcher(path):
        return [] if path.endswith("/matchups") else []
    R.run(5, 150, {"soccer": 29}, out_root=str(tmp_path), fetcher=fetcher, clock=lambda: t["now"], sleep=sleep,
          say=lambda s: None, steam=Spy())
    assert calls[0] == "seed" and calls[-1] == "finish" and calls.count("reading") == 3 and "tick" in calls


def test_replay_seeds_history_from_the_previous_run(tmp_path):
    d = tmp_path / "data" / "2026-11-07"
    d.mkdir(parents=True)
    with gzip.open(d / "soccer_1103.csv.gz", "wt") as f:
        f.write("ts_utc,snapshot,game_id,type,period,side,line,price_us,price_dec,max_stake,status\n")
        for side, p in (("home", 110), ("draw", 240), ("away", 260)):
            f.write(f"2026-11-07T11:40:00Z,1,7,moneyline,0,{side},,{p},0,3000,open\n")
    with gzip.open(d / "soccer_1103_games.csv.gz", "wt") as f:
        f.write("seen_utc,game_id,league_id,league,home,away,start_utc\n"
                "2026-11-07T11:40:00Z,7,1,EPL,Arsenal,Chelsea,2026-11-07T15:00:00Z\n")
    lg, _ = logger(Fake(), lambda: SAT, out=str(tmp_path))
    lg.seed("soccer")
    trig = lg.det.update("soccer", st(-105, 250, 280, 5000), GAMES, SAT)
    assert len(trig) == 1 and trig[0]["rule_b"]


def test_start_up_check_reports_without_numbers():
    fake = Fake(balance=1200)
    lg, said = logger(fake, lambda: SAT)
    lg.seed("soccer")
    assert any("no paid checks" in x for x in said) and not any("1200" in x for x in said)
    fake = Fake()
    lg, said = logger(fake, lambda: SAT)
    lg.seed("soccer")
    assert any(x.startswith("steam: on") for x in said)


def test_changed_prices_and_withdrawn_prices_show_in_the_second_look(monkeypatch):
    monkeypatch.setattr(S, "full_check", lambda cid: False)
    t = {"now": SAT}
    fake = Fake(delta=[{"key": "william_hill", "last_update": "t2", "markets": []}])   # price withdrawn
    lg, _ = logger(fake, lambda: t["now"])
    drive(lg, t)
    t["now"] += dt.timedelta(seconds=61)
    lg.tick()
    c = lg.checks[0]
    assert c["reply2"] == [{"k": "william_hill", "u": "t2", "o": []}]   # gone, not "still on offer"


def test_one_in_ten_checks_also_take_a_full_second_snapshot(monkeypatch):
    monkeypatch.setattr(S, "full_check", lambda cid: True)
    t = {"now": SAT}
    fake = Fake()
    lg, said = logger(fake, lambda: t["now"])
    drive(lg, t)
    t["now"] += dt.timedelta(seconds=61)
    lg.tick()
    raw = [c for c in fake.calls if "/odds/raw" in c[1] and "quote=true" not in c[1]]
    assert len(raw) == 3 and raw[2][2].get("If-None-Match") == "W/1"
    c = lg.checks[0]
    assert c["validate"] and c["status_full"] == 304 and c["reply2_full"] == c["reply1"]


def test_full_check_choice_is_about_one_in_ten():
    assert sum(S.full_check(f"x{i}") for i in range(5000)) in range(400, 600)


def test_comparison_checks_held_back_after_80_percent_of_the_day(monkeypatch):
    monkeypatch.setattr(S, "sampled", lambda *a: True)
    t = {"now": SAT}
    fake = Fake(ledger={"days": {"2026-11-07": 400}, "total": 400})       # 80% of Saturday's 500
    lg, _ = logger(fake, lambda: t["now"])
    lg.on_reading("soccer", st(110, 240, 260, 3000), GAMES, t["now"])
    t["now"] += dt.timedelta(minutes=2)
    lg.on_reading("soccer", st(-105, 250, 280, 3000), GAMES, t["now"])    # A-only (no stake rise)
    assert lg.counts["skipped_reserve"] == 1 and lg.triggers[0]["reserve"]
    assert not [c for c in fake.calls if "/odds/raw" in c[1] and "quote=true" not in c[1]]
    t = {"now": SAT}
    fake = Fake(ledger={"days": {"2026-11-07": 400}, "total": 400})
    lg, _ = logger(fake, lambda: t["now"])
    drive(lg, t)                                                          # rule B still checked
    assert [c for c in fake.calls if "/odds/raw" in c[1] and "quote=true" not in c[1]]


def test_price_probe_runs_once_per_run_free_and_prints_numbers_only(monkeypatch):
    monkeypatch.setattr(S, "full_check", lambda cid: False)
    t = {"now": SAT}
    fake = Fake()
    lg, said = logger(fake, lambda: t["now"])
    drive(lg, t)
    probes = [c for c in fake.calls if "quote=true" in c[1]]
    assert len(probes) == 6 and lg.counts["tokens"] == 1          # quotes free; only the real call charged
    line = [x for x in said if "price probe" in x][0]
    assert "as now 3" in line and "Arsenal" not in line and "william" not in line
    t["now"] += dt.timedelta(minutes=70)
    lg.on_reading("soccer", st(-130, 260, 300, 8000), GAMES, t["now"])
    assert len([c for c in fake.calls if "quote=true" in c[1]]) == 6   # not again in the same run
