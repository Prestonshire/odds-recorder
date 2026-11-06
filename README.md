# odds-recorder

Records Pinnacle's public prices (main lines only) and every change to them, once an hour on GitHub Actions, for research into how quickly UK bookmakers follow Pinnacle's price moves. Read-only: it logs in nowhere and cannot bet.

- Code: `recorder/record.py` (plain Python, no packages needed). Tests: `python -m pytest -q tests`.
- Schedule: `.github/workflows/record.yml`, hourly 08:00–02:59 UTC, each run reading every 2.5 minutes for 52 minutes.
- Output: `data/<date>/<sport>_<HHMM>.csv.gz` (prices and changes) and `..._games.csv.gz` (games). Times are UTC.
- Sports: basketball, American football, ice hockey, tennis, baseball, soccer, esports, darts, snooker, volleyball, handball.

## Steam logger (added 6 Nov 2026)
`recorder/steam.py` watches each Pinnacle reading for a sudden price move ("steam"). For the moves chosen by rules fixed in advance (kept in the owner's private repo), it takes a 1-token snapshot of UK bookmakers' prices for that match from OddsRelay, then a second one 60 seconds later.
- **Off unless two Actions secrets are set:** `ODDSRELAY_KEY` and `STEAM_PUSH_TOKEN` (a token that can write to the private repo only).
- OddsRelay's prices are **never** saved in this public repo or printed in its logs: they go straight to the private repo. The public log shows counts only.
- Spending guard (tokens actually charged): 150 a day Mon–Fri, 500 Sat–Sun, 6,800 in total to 5 Dec 2026, and nothing if the balance is under 1,500.
- Any error in the logger is caught; Pinnacle recording carries on as before.
