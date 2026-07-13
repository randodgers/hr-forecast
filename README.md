# ⚾ HR Forecast

Daily MLB home run predictions built entirely on free public data — no API keys,
no paid services, no server. A static site refreshed by a scheduled GitHub Action.

Inspired by homerunforecast.com (which only scores weather), but goes further:
it combines ballpark conditions with player and pitcher data to rank the day's
most likely home run hitters.

## What it shows

- **Ballpark conditions (HRFI)** — a 1–10 index per stadium for today's games,
  where 5 is neutral. Built from first-pitch temperature, wind blown out/in
  relative to each stadium's actual orientation, and air pressure. Domes score
  a flat 5.
- **Top home run picks** — every hitter on today's slate ranked by the modeled
  probability of hitting a home run in the game, with the factor breakdown
  (pitcher, park, weather) shown per player.

## Data sources (all free)

| Source | What it provides |
|---|---|
| [MLB Stats API](https://statsapi.mlb.com) | Schedule, probable pitchers, lineups, rosters, season stats, last-10-games form, platoon splits (vs LHP/RHP for batters, vs LHB/RHB for pitchers), venue coordinates / azimuth / elevation / roof type |
| [Open-Meteo](https://open-meteo.com) | Hourly temperature, wind speed & direction, humidity, pressure per ballpark (no key) |
| [Baseball Savant](https://baseballsavant.mlb.com/leaderboard/statcast-park-factors) | 3-year rolling Statcast HR park factors, plus the exit-velocity/barrels leaderboard CSVs for both batters (barrels hit) and pitchers (barrels allowed) — all snapshotted to `data/` as fallbacks |
| Public sportsbook prop prices | "To hit a home run" player prop odds from a public sportsbook JSON feed, with the [HomeRunOdds](https://djstrauss08.github.io/HomeRunOdds/) consensus feed as fallback. Optional — everything else works if odds are unavailable |

## Methodology

For each hitter, per plate appearance:

```
P(HR per PA) = power_rate × form_mult × pitcher_mult × park_mult × weather_mult
```

- **power_rate** — the batter's HR rate **vs. the starter's handedness**
  (platoon split, regressed toward his overall rate with a 250-PA prior;
  the overall rate is itself regressed toward league average 0.031 with a
  200-PA prior), blended 65/35 with a barrel-based expected HR rate
  (barrels per PA × 55%, the league share of barrels that leave the yard).
  Switch hitters always take the platoon-advantage side.
- **form_mult** — last-10-games HR rate vs. the batter's own baseline,
  regressed with a 60-PA prior and clamped to 0.85–1.20 (hot/cold streaks
  are mostly noise; this keeps them a nudge, not a driver). Neutral for
  hitters under 100 season PA — a call-up's L10 *is* his season, so a form
  signal would double-count the same small sample.
- **pitcher_mult** — the starter's HR allowed per batter faced **vs. this
  batter's side** (150-BF prior), falling back to season HR/9 when splits are
  thin, blended 65/35 with a barrels-allowed expected rate (quality of contact
  corrects lucky/unlucky HR totals), then weighted 65/35 against a
  league-average bullpen since batters only face the starter for part of
  the game.
- **park_mult** — Savant's HR park factor, regressed 15% toward 100.
- **weather_mult** — +0.7% per °F above 72, ±1.2% per mph of out/in-blowing
  wind (10-m wind damped 40% for stadium shielding, capped at ±15 mph), and a
  small air-pressure term. Neutral (1.0) when the roof is closed.

Game probability assumes 3.7–4.7 plate appearances depending on lineup spot
(4.1 when lineups aren't posted yet):

```
P(HR in game) = 1 − (1 − P(HR per PA))^expected_PA
```

The HRFI is `5 + (weather_mult − 1) × 20`, clamped to 1–10.

### Pitch-arsenal matchup

For every pitch the opposing starter throws at ≥5% usage, the model compares
(a) the batter's expected SLG against that pitch type and (b) the starter's
expected SLG allowed on it, each vs. the league average for the pitch type,
weighted by usage (Savant pitch-arsenal-stats, expected stats so lucky
outcomes don't leak in). The resulting index is damped 50% and clamped to
0.85–1.18. This is the systematic version of the classic prop-betting
workflow of isolating a starter's vulnerable pitches by handedness.

### Market comparison (de-vigged)

Each "to hit a home run" prop price is converted to an implied probability
(`100/(odds+100)` for positive American odds). Because these are one-sided
markets (no "No" price is offered), standard two-way de-vigging is
impossible; the raw implied number contains the book's margin and one-sided
longshot props carry extra vig (favorite–longshot bias). The pipeline
therefore multiplies implied probabilities by a **de-vig factor** — a
documented prior of 0.85 until 200+ priced picks have been graded, after
which it is **calibrated empirically** from our own track record: the actual
HR rate of priced players divided by their average implied probability
(clamped 0.70–1.00). **Edge** = model % − de-vigged fair %.

### Track record

Every refresh snapshots the final pre-game prediction for each player in a
confirmed lineup or with a market price (`data/picks_log.json`). The next
morning `scripts/grade_results.py` grades those snapshots against final
boxscores (players with zero plate appearances are dropped, not counted as
misses), accumulates `data/history.json`, and publishes
`data/track_record.json`: top-10-picks hit rate, Brier scores for the model
and the de-vigged market, and calibration buckets (predicted vs. actual HR
rate by probability band). The site shows this section once data exists.
The market Brier score is the benchmark: matching it is genuinely hard, and
beating it would indicate real edge.

All knobs live at the top of [scripts/build_data.py](scripts/build_data.py).

## Run locally

```bash
python3 scripts/build_data.py          # writes data/predictions.json (today)
python3 scripts/build_data.py 2026-07-15   # or any date
python3 -m http.server 8642            # then open http://localhost:8642
```

Python 3.9+, standard library only.

## Deploy free on GitHub Pages

1. Create a GitHub repo and push this folder.
2. **Settings → Pages** → Source: *Deploy from a branch* → `main`, `/ (root)`.
3. **Settings → Actions → General** → Workflow permissions: *Read and write*.
4. Done. The workflow in `.github/workflows/refresh.yml` re-runs the pipeline
   every 3 hours during the game day, commits the fresh `data/predictions.json`,
   and Pages redeploys automatically. You can also trigger it manually from the
   Actions tab (*Run workflow*).

## Disclaimers

For entertainment and research only. Not betting advice, not financial advice.
Player data © MLB Advanced Media; check MLB's terms before any commercial use.
