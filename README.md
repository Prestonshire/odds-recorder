# odds-recorder

Records Pinnacle's public prices (main lines only) and every change to them, once an hour on GitHub Actions, for research into how quickly UK bookmakers follow Pinnacle's price moves. Read-only: it logs in nowhere and cannot bet.

- Code: `recorder/record.py` (plain Python, no packages needed). Tests: `python -m pytest -q tests`.
- Schedule: `.github/workflows/record.yml`, hourly 08:00–02:59 UTC, each run reading every 2.5 minutes for 52 minutes.
- Output: `data/<date>/<sport>_<HHMM>.csv.gz` (prices and changes) and `..._games.csv.gz` (games). Times are UTC.
- Sports: basketball, American football, ice hockey, tennis, baseball, soccer.
