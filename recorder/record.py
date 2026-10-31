"""Pinnacle price recorder: saves Pinnacle's main prices and every change to them, for later research.

Why: to measure how long UK bookmakers keep an old price after Pinnacle (the sharpest bookmaker) moves.
Pinnacle's public website feed is only reachable from outside the UK, so this runs on GitHub's servers.
It only READS public prices. It never logs in anywhere and cannot bet.

Each run lasts --minutes (default 52). Every --poll seconds (default 150) it reads Pinnacle's main markets
for each sport below and writes:
  * the full state once at the start of the run (so every file stands on its own), then
  * one row each time a price, a maximum stake or a market's status changes.
Only main (not alternative) lines are kept: match winner ("moneyline"), handicap ("spread") and total,
for the full game (period 0) and the first half/period/set (period 1), for games starting within 36 hours.

Output (UTC dates and times):
  data/<YYYY-MM-DD>/<sport>_<HHMM>.csv.gz        prices: time, game id, market, side, line, price, max stake
  data/<YYYY-MM-DD>/<sport>_<HHMM>_games.csv.gz  games: id, league, home, away, start time

Words: "American" odds are Pinnacle's format (+150 means win £150 for £100; -150 means stake £150 to win £100);
price_dec is the same price as a UK decimal price. max_stake is Pinnacle's maximum bet on that market
(a bigger maximum usually means Pinnacle trusts its price more).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
# Pinnacle's public website key (the one its own site uses), not a personal secret.
GUEST_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"
SPORTS = {"basketball": 4, "american_football": 15, "ice_hockey": 19, "tennis": 33, "baseball": 3, "soccer": 29,
          "esports": 12, "darts": 10, "snooker": 28, "volleyball": 34, "handball": 18}
KEEP_TYPES = {"moneyline", "spread", "total"}
KEEP_PERIODS = {0, 1}
HORIZON_H = 36
GAMES_EVERY_S = 900
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRICE_COLS = ["ts_utc", "snapshot", "game_id", "type", "period", "side", "line", "price_us", "price_dec",
              "max_stake", "status"]
GAME_COLS = ["seen_utc", "game_id", "league_id", "league", "home", "away", "start_utc"]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(path: str, timeout=60, tries=3, wait=4.0):
    """GET a Pinnacle path. Brief hiccups (HTTP 429/5xx, timeouts) are retried a couple of times."""
    req = urllib.request.Request(BASE + path, headers={"X-API-Key": GUEST_KEY, "Accept": "application/json",
                                                       "Accept-Encoding": "gzip", "User-Agent": "Mozilla/5.0"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == tries - 1:
                raise
        time.sleep(wait * (attempt + 1))


def us_to_dec(a) -> float | None:
    try:
        a = float(a)
    except (TypeError, ValueError):
        return None
    if a >= 100:
        return round(1 + a / 100, 4)
    if a <= -100:
        return round(1 + 100 / -a, 4)
    return None


def parse_start(s: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def games_from(matchups: list, now: dt.datetime) -> dict:
    """Main games (not specials such as corners) starting within the horizon: {id: info}."""
    out = {}
    for m in matchups:
        if m.get("type") != "matchup" or m.get("parentId"):
            continue
        start = parse_start(m.get("startTime", ""))
        if start is None or not (now - dt.timedelta(hours=1) <= start <= now + dt.timedelta(hours=HORIZON_H)):
            continue
        sides = {p.get("alignment"): p.get("name", "") for p in m.get("participants") or []}
        lg = m.get("league") or {}
        out[m["id"]] = {"league_id": lg.get("id"), "league": lg.get("name", ""), "home": sides.get("home", ""),
                        "away": sides.get("away", ""), "start": start}
    return out


def state_from(markets: list, games: dict) -> dict:
    """{(game, type, period, side, line): (price_us, max_stake, status)} for the main lines we keep."""
    out = {}
    for m in markets:
        if m.get("isAlternate") or m.get("type") not in KEEP_TYPES or m.get("period") not in KEEP_PERIODS:
            continue
        gid = m.get("matchupId")
        if gid not in games:
            continue
        lim = next((x.get("amount") for x in m.get("limits") or [] if x.get("type") == "maxRiskStake"), None)
        for p in m.get("prices") or []:
            side = p.get("designation") or p.get("participantId")
            out[(gid, m["type"], m["period"], side, p.get("points"))] = (p.get("price"), lim, m.get("status"))
    return out


def changes(old: dict, new: dict, ts: str, snapshot: bool) -> list:
    rows = []
    for k, v in new.items():
        if snapshot or old.get(k) != v:
            gid, typ, per, side, line = k
            rows.append([ts, int(snapshot), gid, typ, per, side, line, v[0], us_to_dec(v[0]), v[1], v[2]])
    if not snapshot:
        for k in old.keys() - new.keys():          # a line taken down or moved: record it as gone
            gid, typ, per, side, line = k
            rows.append([ts, 0, gid, typ, per, side, line, None, None, None, "removed"])
    return rows


class Sink:
    """Collects rows for one sport during a run and writes gzip CSV files at the end."""

    def __init__(self, sport: str, run_start: dt.datetime, out_root: str):
        self.sport, self.run_start, self.out_root = sport, run_start, out_root
        self.prices, self.games, self.seen_games = [], [], set()

    def add_games(self, games: dict, ts: str):
        for gid, g in games.items():
            if gid not in self.seen_games:
                self.seen_games.add(gid)
                self.games.append([ts, gid, g["league_id"], g["league"], g["home"], g["away"], iso(g["start"])])

    def write(self) -> list:
        day = self.run_start.strftime("%Y-%m-%d")
        stem = os.path.join(self.out_root, "data", day, f"{self.sport}_{self.run_start:%H%M}")
        os.makedirs(os.path.dirname(stem), exist_ok=True)
        paths = []
        for suffix, cols, rows in ((".csv.gz", PRICE_COLS, self.prices), ("_games.csv.gz", GAME_COLS, self.games)):
            if not rows:
                continue
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(cols)
            w.writerows(rows)
            with gzip.open(stem + suffix, "wt", encoding="utf-8", newline="") as f:
                f.write(buf.getvalue())
            paths.append(stem + suffix)
        return paths


def run(minutes: float, poll_s: float, sports: dict, out_root=ROOT, fetcher=fetch, clock=utcnow, sleep=time.sleep,
        say=print) -> list:
    start = clock()
    end = start + dt.timedelta(minutes=minutes)
    sinks = {s: Sink(s, start, out_root) for s in sports}
    state = {s: {} for s in sports}
    games = {s: {} for s in sports}
    games_at = {s: None for s in sports}
    first = {s: True for s in sports}
    polls = 0
    while True:
        now = clock()
        ts = iso(now)
        for sport, sid in sports.items():
            try:
                if games_at[sport] is None or (now - games_at[sport]).total_seconds() >= GAMES_EVERY_S:
                    games[sport] = games_from(fetcher(f"/sports/{sid}/matchups"), now)
                    games_at[sport] = now
                    sinks[sport].add_games(games[sport], ts)
                new = state_from(fetcher(f"/sports/{sid}/markets/straight?primaryOnly=true"), games[sport])
            except (urllib.error.URLError, OSError, ValueError) as e:
                say(f"{ts} {sport}: read failed ({type(e).__name__} {getattr(e, 'code', '')}); will retry next poll")
                continue
            sinks[sport].prices += changes(state[sport], new, ts, first[sport])
            state[sport], first[sport] = new, False
        polls += 1
        if clock() + dt.timedelta(seconds=poll_s) > end:
            break
        sleep(poll_s)
    paths = []
    for s in sinks.values():
        paths += s.write()
    say(f"{polls} polls; wrote {len(paths)} files: " + ", ".join(
        f"{s.sport} {len(s.prices):,} price rows / {len(s.games):,} games" for s in sinks.values()))
    return paths


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=52)
    ap.add_argument("--poll", type=float, default=150, help="seconds between readings")
    ap.add_argument("--sports", default=",".join(SPORTS), help="comma list from: " + ", ".join(SPORTS))
    a = ap.parse_args(argv)
    chosen = {s: SPORTS[s] for s in a.sports.split(",") if s in SPORTS}
    run(a.minutes, a.poll, chosen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
