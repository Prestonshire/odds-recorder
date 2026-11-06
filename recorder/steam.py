"""Steam forward logger: spots sudden Pinnacle price moves while the recorder runs, and on the chosen ones
takes a cheap snapshot of UK bookmakers' prices for that match (OddsRelay), then a second one 60 s later.

Words: "steam" = Pinnacle suddenly shortens a price (usually big-money bettors backing it). "Max stake" =
the biggest bet Pinnacle will take; it raises it when it trusts its price more. A "stale price" is a UK
bookmaker still offering the old, longer price. The rules are fixed in advance in the PRIVATE repo
(docs/steam-logger-preregistration.md, 6 Nov 2026); this file only puts them into code.

Safety rules (this repo is PUBLIC):
  * Off unless both ODDSRELAY_KEY and STEAM_PUSH_TOKEN are set (GitHub Actions secrets). Off = does nothing.
  * OddsRelay's prices are never written to this repo and never printed. They are sent only to the private
    repo (Prestonshire/betting-system) with a token that can write to that repo alone. Logs show counts only.
  * Any error inside the logger is caught: the Pinnacle recorder carries on as before.
  * Spending guard on tokens actually charged (X-Tokens-Cost header): 150 a day Mon-Fri, 500 Sat-Sun (UTC),
    6,800 in total, and nothing at all if the OddsRelay balance is under 1,500.
Standard library only, like record.py.
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import difflib
import glob
import gzip
import hashlib
import io
import json
import math
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

OR_API = "https://api.oddsrelay.io/v2"
GH_API = "https://api.github.com"
PRIVATE_REPO = "Prestonshire/betting-system"
OUT_DIR = "data/steam_logger"

RISE_A, RISE_C = 0.02, 0.03          # relative rise in Pinnacle's fair chance (rules A and C)
MIN_STAKE = 1000                     # Pinnacle max stake, $
WINDOW_S = 3600                      # look-back for the rise
MAX_START_H = 6                      # only games starting within 6 hours
SAMPLE_ONE_IN = 10                   # A-only triggers checked: a fixed 1 in 10
RECHECK_S = 60                       # second snapshot after 60 s
SHARE_S = 60                         # triggers on the same match within 60 s share the calls
CAP_WEEKDAY, CAP_WEEKEND, CAP_TOTAL, BALANCE_FLOOR = 150, 500, 6800, 1500
LAST_CHECK_DAY = dt.date(2026, 12, 4)  # verdict on Sat 5 Dec: no paid checks after Fri 4 Dec (UTC)
MATCH_MINUTES, MATCH_SCORE = 30, 0.7
EVENTS_REFRESH_S = 600
# recorder sport -> OddsRelay sport key prefix
SPORT_MAP = {"soccer": "soccer", "tennis": "tennis", "basketball": "basketball", "ice_hockey": "ice_hockey",
             "american_football": "american_football", "baseball": "baseball", "esports": "esports",
             "darts": "darts", "snooker": "snooker", "volleyball": "volleyball", "handball": "handball"}
TRIGGER_COLS = ["ts_utc", "sport", "game_id", "league", "home", "away", "start_utc", "side", "fair_now", "fair_low",
                "rise", "max_stake", "prev_stake", "rule_b", "rule_c", "sampled", "action", "check_id"]


# ------------------------------------------------------------------ fair chances (Shin's method)
def shin(prices: list) -> list:
    """Fair chances (adding to 1) from decimal prices, Shin's method as in betting-system src/value/prices.py."""
    pi = [1.0 / p for p in prices]
    b = sum(pi)
    if b <= 1:
        return [x / b for x in pi]

    def total(z):
        ps = [(math.sqrt(z * z + 4 * (1 - z) * x * x / b) - z) / (2 * (1 - z)) for x in pi]
        return ps, sum(ps)

    lo, hi = 0.0, 0.5
    for _ in range(60):
        mid = (lo + hi) / 2
        if total(mid)[1] > 1:
            lo = mid
        else:
            hi = mid
    ps, s = total((lo + hi) / 2)
    return [x / s for x in ps]


def us_to_dec(a):
    try:
        a = float(a)
    except (TypeError, ValueError):
        return None
    if a >= 100:
        return 1 + a / 100
    if a <= -100:
        return 1 + 100 / -a
    return None


# ------------------------------------------------------------------ detection (Pinnacle data only, no network)
class Detector:
    """Applies rules A/B/C to each Pinnacle reading. Pure: no network, so it can be tested and replayed."""

    def __init__(self):
        self.hist = {}        # (sport, game, side) -> [(time, fair chance)]
        self.active = set()   # (sport, game, side) whose event is still running
        self.prev_stake = {}  # (sport, game) -> max stake at the previous priced reading

    @staticmethod
    def moneylines(state: dict) -> dict:
        """{game: ({side: decimal price}, max stake)} for full-game match-winner markets fully open and priced."""
        out = {}
        for (gid, typ, per, side, _line), (price, stake, status) in state.items():
            if typ != "moneyline" or per != 0:
                continue
            g = out.setdefault(gid, [{}, 0.0, True])
            dec = us_to_dec(price)
            if dec is None or dec <= 1 or status != "open":
                g[2] = False
            else:
                g[0][side] = dec
            g[1] = max(g[1], float(stake or 0))
        return {gid: (sides, stake) for gid, (sides, stake, ok) in out.items()
                if ok and set(sides) in ({"home", "away"}, {"home", "draw", "away"})}

    def update(self, sport: str, state: dict, games: dict, now: dt.datetime, emit=True) -> list:
        trig = []
        for gid, (sides, stake) in self.moneylines(state).items():
            names = sorted(sides)
            fair = dict(zip(names, shin([sides[s] for s in names])))
            prev = self.prev_stake.get((sport, gid))
            g = games.get(gid) or {}
            start = g.get("start")
            soon = start is not None and dt.timedelta(0) < start - now <= dt.timedelta(hours=MAX_START_H)
            for side, fp in fair.items():
                k = (sport, gid, side)
                h = [x for x in self.hist.get(k, []) if (now - x[0]).total_seconds() <= WINDOW_S]
                low = min((x[1] for x in h), default=None)
                rise = fp / low - 1 if low else 0.0
                cond = low is not None and rise >= RISE_A and stake >= MIN_STAKE and soon
                if cond and k not in self.active:
                    self.active.add(k)
                    if emit:
                        b = prev is not None and stake > prev
                        trig.append(dict(ts=now, sport=sport, game_id=gid, side=side, fair_now=fp, fair_low=low,
                                         rise=rise, max_stake=stake, prev_stake=prev, rule_b=b,
                                         rule_c=b and rise >= RISE_C, league=g.get("league", ""),
                                         home=g.get("home", ""), away=g.get("away", ""), start=start))
                elif not cond:
                    self.active.discard(k)
                h.append((now, fp))
                self.hist[k] = h
            self.prev_stake[(sport, gid)] = stake
        return trig


def sampled(gid, side, ts: dt.datetime) -> bool:
    """The fixed 1-in-10 choice of A-only triggers (a hash, so nobody can steer it)."""
    h = hashlib.sha256(f"{gid}|{side}|{ts:%Y-%m-%dT%H:%M:%SZ}".encode()).hexdigest()
    return int(h, 16) % SAMPLE_ONE_IN == 0


def replay(detector: Detector, sport: str, price_path: str, games_path: str, before: dt.datetime):
    """Feed an earlier recorder file through the detector (no triggers), so rises can span two runs."""
    P = lambda s: dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    games = {}
    with gzip.open(games_path, "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            games[int(r["game_id"])] = {"start": P(r["start_utc"]), "league": r["league"], "home": r["home"],
                                        "away": r["away"]}
    state, cur, rows = {}, None, []
    with gzip.open(price_path, "rt", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["type"] == "moneyline" and r["period"] == "0"]
    for r in rows + [None]:
        if r is None or r["ts_utc"] != cur:
            if cur is not None and (before - P(cur)).total_seconds() <= WINDOW_S:
                detector.update(sport, state, games, P(cur), emit=False)
            if r is None:
                break
            cur = r["ts_utc"]
        k = (int(r["game_id"]), "moneyline", 0, r["side"], None)
        if r["status"] == "removed":
            state.pop(k, None)
        else:
            state[k] = (float(r["price_us"]) if r["price_us"] else None, float(r["max_stake"] or 0), r["status"])


# ------------------------------------------------------------------ matching Pinnacle games to OddsRelay events
_DROP = {"fc", "cf", "afc", "sc", "ac", "fk", "sk", "cd", "the", "club", "de", "if", "bk"}


def tidy(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower().replace("&", " and ")
    return " ".join(t for t in re.sub(r"[^a-z0-9 ]", " ", s).split() if t not in _DROP)


def sim(a: str, b: str) -> float:
    ta, tb = tidy(a), tidy(b)
    if not ta or not tb:
        return 0.0
    seq = difflib.SequenceMatcher(None, ta, tb).ratio()
    wa, wb = {w for w in ta.split() if len(w) > 2}, {w for w in tb.split() if len(w) > 2}
    jac = len(wa & wb) / min(len(wa), len(wb)) if wa and wb else 0.0
    return max(seq, jac)


def match_event(game: dict, events: list):
    """Best OddsRelay event for a Pinnacle game: start within 30 min, names score >= 0.7 (either way round)."""
    best, best_s, swapped = None, 0.0, False
    for e in events:
        t = parse_iso(e.get("commence_time"))
        if t is None or game.get("start") is None or abs((t - game["start"]).total_seconds()) > MATCH_MINUTES * 60:
            continue
        straight = (sim(game["home"], e.get("home_team")) + sim(game["away"], e.get("away_team"))) / 2
        cross = (sim(game["home"], e.get("away_team")) + sim(game["away"], e.get("home_team"))) / 2
        s = max(straight, cross)
        if s > best_s:
            best, best_s, swapped = e, s, cross > straight
    return (best, round(best_s, 3), swapped) if best is not None and best_s >= MATCH_SCORE else (None, round(best_s, 3), False)


def parse_iso(s):
    if not s:
        return None
    try:
        return dt.datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


# ------------------------------------------------------------------ HTTP (replaceable in tests)
def http(method: str, url: str, headers: dict, body: bytes | None = None, timeout=30):
    """Returns (status, headers as a lower-case dict, body bytes). Never raises for HTTP errors."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, st, hd = r.read(), r.status, {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        raw, st, hd = e.read(), e.code, {k.lower(): v for k, v in (e.headers or {}).items()}
    if hd.get("content-encoding") == "gzip" and raw:
        raw = gzip.decompress(raw)
    return st, hd, raw


def trim_reply(data: dict, event_id: str) -> list:
    """Keep only what the analysis needs: per venue its key, last update and match-winner prices."""
    keep = []
    for e in (data or {}).get("data") or []:
        if e.get("id") != event_id and event_id not in (e.get("previous_ids") or []):
            continue
        for b in e.get("bookmakers") or []:
            for m in b.get("markets") or []:
                if m.get("key") != "h2h":
                    continue
                outs = [{k: o.get(k) for k in ("name", "price", "lay_price", "available") if k in o}
                        for o in m.get("outcomes") or []]
                keep.append({"k": b.get("key"), "u": b.get("last_update"), "o": outs})
    return keep


# ------------------------------------------------------------------ the logger
class SteamLogger:
    def __init__(self, or_key: str, gh_token: str, out_root: str, run_start: dt.datetime, http_fn=http,
                 clock=None, say=print):
        self.or_key, self.gh_token, self.out_root, self.run_start = or_key, gh_token, out_root, run_start
        self.http, self.say = http_fn, say
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.det = Detector()
        self.events, self.events_at, self.dead_sports = {}, {}, set()
        self.pending, self.checks, self.triggers = [], [], []
        self.ledger, self.ledger_sha, self.balance, self.disabled = None, None, None, None
        self.counts = dict(triggers=0, rule_b=0, sampled=0, checks=0, tokens=0.0, unmatched=0, skipped_cap=0,
                           not_sampled=0, after_end=0, errors=0)

    # --- safe wrappers: the recorder must never be harmed by the logger
    def _safe(self, fn, *a):
        try:
            return fn(*a)
        except Exception as e:  # noqa: BLE001 - deliberately broad; print the type only, never content
            self.counts["errors"] += 1
            self.say(f"steam: {fn.__name__} error ({type(e).__name__}); recorder carries on")
            return None

    def seed(self, sport: str):
        self._safe(self._seed, sport)

    def on_reading(self, sport: str, state: dict, games: dict, now: dt.datetime):
        self._safe(self._on_reading, sport, state, games, now)

    def tick(self, now: dt.datetime | None = None):
        self._safe(self._tick, now or self.clock())

    def finish(self, sleep=time.sleep):
        self._safe(self._finish, sleep)

    # --- internals
    def _seed(self, sport):
        """Replay this sport's most recent earlier recorder file (if within the hour) into the detector."""
        files = sorted(glob.glob(os.path.join(self.out_root, "data", "*", f"{sport}_[0-9][0-9][0-9][0-9].csv.gz")))
        if not files:
            return
        last = files[-1]
        gp = last.replace(".csv.gz", "_games.csv.gz")
        if os.path.exists(gp):
            replay(self.det, sport, last, gp, self.run_start)

    def _or(self, path: str, etag: str | None = None):
        h = {"Authorization": f"Bearer {self.or_key}", "Accept-Encoding": "gzip", "User-Agent": "research script"}
        if etag:
            h["If-None-Match"] = etag
        st, hd, raw = self.http("GET", OR_API + path, h)
        cost = float(hd.get("x-tokens-cost") or 0)
        rem = hd.get("x-tokens-remaining")
        if rem not in (None, ""):
            self.balance = float(rem)
        self._spend(cost)
        data = json.loads(raw) if st == 200 and raw else None
        return st, hd, data, cost

    def _spend(self, cost):
        if not cost:
            return
        if self.ledger is None:
            self.ledger = {"days": {}, "total": 0}
        day = f"{self.clock():%Y-%m-%d}"
        self.ledger["days"][day] = self.ledger["days"].get(day, 0) + cost
        self.ledger["total"] = self.ledger.get("total", 0) + cost
        self.counts["tokens"] += cost

    def _gh(self, method, path, body=None):
        h = {"Authorization": f"Bearer {self.gh_token}", "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "odds-recorder"}
        data = json.dumps(body).encode() if body is not None else None
        st, _hd, raw = self.http(method, f"{GH_API}/repos/{PRIVATE_REPO}/contents/{path}", h, data)
        return st, (json.loads(raw) if raw else {})

    def _ready(self) -> bool:
        """Load the spending ledger and the balance once per run. False = no paid checks this run."""
        if self.disabled is not None:
            return not self.disabled
        self.disabled = True
        st, d = self._gh("GET", f"{OUT_DIR}/ledger.json")
        if st == 200:
            self.ledger, self.ledger_sha = json.loads(base64.b64decode(d["content"])), d["sha"]
        elif st == 404:
            self.ledger = {"days": {}, "total": 0}
        else:
            self.say(f"steam: could not read the spending ledger (HTTP {st}); no paid checks this run")
            return False
        st, _hd, u, _c = self._or("/usage")
        if st == 200 and u:
            self.balance = (u.get("tokens") or {}).get("remaining", self.balance)
        if self.balance is None:
            self.say(f"steam: OddsRelay balance unknown (HTTP {st}); no paid checks this run")
            return False
        self.disabled = False
        return True

    def _room(self, need=2.0) -> bool:
        now = self.clock()
        cap = CAP_WEEKEND if now.weekday() >= 5 else CAP_WEEKDAY
        day = self.ledger["days"].get(f"{now:%Y-%m-%d}", 0)
        return (day + need <= cap and self.ledger.get("total", 0) + need <= CAP_TOTAL
                and (self.balance or 0) - need >= BALANCE_FLOOR)

    def _events(self, sport: str, now: dt.datetime) -> list:
        key = SPORT_MAP.get(sport)
        if not key or sport in self.dead_sports:
            return []
        at = self.events_at.get(sport)
        if at is None or (now - at).total_seconds() >= EVENTS_REFRESH_S:
            q = {"sport": key, "commenceTimeFrom": f"{now - dt.timedelta(minutes=45):%Y-%m-%dT%H:%M:%SZ}",
                 "commenceTimeTo": f"{now + dt.timedelta(hours=MAX_START_H + 1):%Y-%m-%dT%H:%M:%SZ}"}
            out, cursor = [], None
            for _ in range(10):
                qs = dict(q, **({"cursor": cursor} if cursor else {}))
                st, _hd, d, _c = self._or("/events?" + urllib.parse.urlencode(qs))
                if st != 200 or not d:
                    if st == 400:
                        self.dead_sports.add(sport)
                    break
                out += d.get("data") or []
                cursor = (d.get("meta") or {}).get("next_cursor")
                if not cursor:
                    break
            self.events[sport], self.events_at[sport] = out, now
        return self.events.get(sport, [])

    def _on_reading(self, sport, state, games, now):
        for t in self.det.update(sport, state, games, now):
            t["sampled"] = (not t["rule_b"]) and sampled(t["game_id"], t["side"], now)
            self.counts["triggers"] += 1
            self.counts["rule_b"] += t["rule_b"]
            self.counts["sampled"] += t["sampled"]
            t["action"], t["check_id"] = self._decide(t, now), ""
            self.triggers.append(t)

    def _decide(self, t, now) -> str:
        if not (t["rule_b"] or t["sampled"]):
            self.counts["not_sampled"] += 1
            return "not sampled"
        if now.date() > LAST_CHECK_DAY:
            self.counts["after_end"] += 1
            return "after end date"
        if not self._ready():
            return "logger off"
        for c in self.pending:     # share a check already running on this match
            if c["sport"] == t["sport"] and c["game_id"] == t["game_id"] and \
                    (now - c["t1"]).total_seconds() <= SHARE_S:
                c["triggers"].append(_tid(t))
                t["check_id"] = c["id"]
                return "shared"
        if (self.balance or 0) < BALANCE_FLOOR or not self._room():
            self.counts["skipped_cap"] += 1
            return "skipped: cap"
        ev, score, swapped = match_event({"start": t["start"], "home": t["home"], "away": t["away"]},
                                         self._events(t["sport"], now))
        if ev is None:
            self.counts["unmatched"] += 1
            return f"unmatched ({score})"
        if not self._room():          # an events page could in theory cost tokens
            self.counts["skipped_cap"] += 1
            return "skipped: cap"
        q = urllib.parse.urlencode({"eventIds": ev["id"], "markets": "h2h", "includeLiquidity": "true"})
        st, hd, d, cost = self._or("/odds/raw?" + q)
        c = dict(id=f"{t['sport']}-{t['game_id']}-{now:%H%M%S}", sport=t["sport"], game_id=t["game_id"],
                 or_event=ev["id"], or_home=ev.get("home_team"), or_away=ev.get("away_team"), match_score=score,
                 swapped=swapped, triggers=[_tid(t)], t1=now, status1=st, cost1=cost, reply1=trim_reply(d, ev["id"]),
                 etag=hd.get("etag"), q=q)
        self.counts["checks"] += 1
        t["check_id"] = c["id"]
        if st == 200:
            self.pending.append(c)
        else:
            c["t2"], c["status2"], c["cost2"], c["reply2"] = None, None, 0.0, None
            self.checks.append(c)
        return "checked"

    def _tick(self, now):
        due = [c for c in self.pending if (now - c["t1"]).total_seconds() >= RECHECK_S]
        for c in due:
            self.pending.remove(c)
            st, _hd, d, cost = self._or("/odds/raw?" + c["q"], etag=c.get("etag"))
            c.update(t2=now, status2=st, cost2=cost,
                     reply2=c["reply1"] if st == 304 else trim_reply(d, c["or_event"]))
            self.checks.append(c)

    def _finish(self, sleep):
        if self.pending:                      # wait (at most 60 s) for the last second snapshots
            wait = RECHECK_S - min((self.clock() - c["t1"]).total_seconds() for c in self.pending)
            if wait > 0:
                sleep(min(wait, RECHECK_S) + 1)
            self._tick(self.clock() + dt.timedelta(seconds=RECHECK_S))
        n = self.counts
        self.say(f"steam: {n['triggers']} triggers ({n['rule_b']} rule B, {n['sampled']} sampled A-only), "
                 f"{n['checks']} checks, {n['tokens']:g} tokens, {n['unmatched']} unmatched, "
                 f"{n['skipped_cap']} skipped by the cap, {n['errors']} errors")
        if not self.triggers:
            return
        stem = f"{OUT_DIR}/{self.run_start:%Y-%m-%d}/{self.run_start:%H%M}"
        ok = self._put(f"{stem}_triggers.csv.gz", _triggers_csv(self.triggers), "steam logger: triggers")
        if self.checks:
            ok &= self._put(f"{stem}_checks.jsonl.gz", _checks_jsonl(self.checks), "steam logger: checks")
        if self.ledger is not None and n["tokens"]:
            body = json.dumps(self.ledger, indent=1, sort_keys=True).encode()
            ok &= self._put(f"{OUT_DIR}/ledger.json", body, "steam logger: spending ledger", sha=self.ledger_sha)
        self.say("steam: saved to the private repo" if ok else "steam: saving to the private repo FAILED")

    def _put(self, path, content: bytes, message, sha=None) -> bool:
        body = {"message": message, "content": base64.b64encode(content).decode(), "branch": "main"}
        if sha:
            body["sha"] = sha
        for attempt in range(3):
            st, d = self._gh("PUT", path, body)
            if st in (200, 201):
                return True
            if st == 409 or st == 422:        # someone changed the file: fetch the new sha and retry
                st2, cur = self._gh("GET", path)
                if st2 == 200:
                    body["sha"] = cur.get("sha")
            time.sleep(2 * (attempt + 1))
        self.say(f"steam: could not save {os.path.basename(path)} (HTTP {st})")
        return False


def _tid(t) -> str:
    return f"{t['game_id']}|{t['side']}|{t['ts']:%Y-%m-%dT%H:%M:%SZ}"


def _iso(x):
    return x.strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(x, dt.datetime) else ("" if x is None else x)


def _triggers_csv(triggers) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(TRIGGER_COLS)
    for t in triggers:
        w.writerow([_iso(t["ts"]), t["sport"], t["game_id"], t["league"], t["home"], t["away"], _iso(t["start"]),
                    t["side"], round(t["fair_now"], 6), round(t["fair_low"], 6), round(t["rise"], 6), t["max_stake"],
                    t["prev_stake"], int(t["rule_b"]), int(t["rule_c"]), int(t["sampled"]), t["action"],
                    t["check_id"]])
    return gzip.compress(buf.getvalue().encode())


def _checks_jsonl(checks) -> bytes:
    lines = []
    for c in checks:
        d = {k: _iso(v) for k, v in c.items() if k not in ("q", "etag")}
        lines.append(json.dumps(d, separators=(",", ":")))
    return gzip.compress(("\n".join(lines) + "\n").encode())


def from_env(out_root: str, run_start: dt.datetime, say=print):
    """The logger, or None (off) unless both secrets are present."""
    k, g = os.environ.get("ODDSRELAY_KEY", "").strip(), os.environ.get("STEAM_PUSH_TOKEN", "").strip()
    if not k or not g:
        say("steam: off (secrets not set)")
        return None
    return SteamLogger(k, g, out_root, run_start, say=say)
