#!/usr/bin/env python3
"""Grade logged predictions against actual home runs.

Reads pending snapshots from data/picks_log.json (written by build_data.py),
looks up each player's home runs in the final boxscore, appends graded rows to
data/history.json, and rewrites data/track_record.json — the small summary the
site displays (hit rates, Brier scores, calibration buckets).

Usage: python3 scripts/grade_results.py [YYYY-MM-DD]
Defaults to grading every logged date at least one day old. Idempotent:
graded rows leave the log, so re-runs are no-ops. Players who never got a
plate appearance are dropped, not counted as misses.
"""

import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATSAPI = "https://statsapi.mlb.com/api/v1"

BUCKETS = [(0, 10), (10, 15), (15, 20), (20, 25), (25, 101)]


def get_json(url, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "hr-forecast/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            print(f"  retry {attempt + 1}: {exc}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))


def load(path, default):
    try:
        return json.loads((DATA_DIR / path).read_text())
    except (OSError, ValueError):
        return default


def game_batting(game_pk):
    """{player_id: (PA, HR)} from the final boxscore; None if game not final."""
    live = get_json(f"{STATSAPI}/game/{game_pk}/boxscore")
    status = get_json(
        f"{STATSAPI}/schedule?sportId=1&gamePk={game_pk}"
    )["dates"][0]["games"][0]["status"]["abstractGameState"]
    if status != "Final":
        return None
    out = {}
    for side in ("home", "away"):
        for key, pl in live["teams"][side]["players"].items():
            bat = (pl.get("stats") or {}).get("batting") or {}
            if bat:
                out[pl["person"]["id"]] = (
                    int(bat.get("plateAppearances", 0)),
                    int(bat.get("homeRuns", 0)),
                )
    return out


def brier(rows, field):
    scored = [r for r in rows if r.get(field) is not None]
    if not scored:
        return None, 0
    s = sum((r[field] / 100.0 - (1.0 if r["hit"] else 0.0)) ** 2 for r in scored)
    return round(s / len(scored), 4), len(scored)


def summarize(rows):
    n = len(rows)
    hits = sum(1 for r in rows if r["hit"])
    model_brier, _ = brier(rows, "prob")
    market_brier, market_n = brier(rows, "fair")
    top = sorted(rows, key=lambda r: -r["prob"])
    # "Top picks" = the model's ten highest-probability hitters per date.
    by_date = {}
    for r in rows:
        by_date.setdefault(r["d"], []).append(r)
    top_hits = top_n = 0
    daily = []
    for d in sorted(by_date):
        day = sorted(by_date[d], key=lambda r: -r["prob"])[:10]
        h = sum(1 for r in day if r["hit"])
        top_hits += h
        top_n += len(day)
        daily.append({"date": d, "topHits": h, "topN": len(day)})
    buckets = []
    for lo, hi in BUCKETS:
        b = [r for r in rows if lo <= r["prob"] < hi]
        if b:
            buckets.append({
                "range": f"{lo}–{hi if hi < 101 else '+'}%",
                "n": len(b),
                "predictedPct": round(sum(r["prob"] for r in b) / len(b), 1),
                "actualPct": round(100.0 * sum(1 for r in b if r["hit"]) / len(b), 1),
            })
    return {
        "gradedPicks": n,
        "totalHits": hits,
        "overallHitRatePct": round(100.0 * hits / n, 1) if n else None,
        "topPicksHitRatePct": round(100.0 * top_hits / top_n, 1) if top_n else None,
        "topPicksRecord": f"{top_hits}/{top_n}",
        "modelBrier": model_brier,
        "marketBrier": market_brier,
        "marketPricedPicks": market_n,
        "buckets": buckets,
        "recentDays": daily[-14:],
        "updatedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main():
    log = load("picks_log.json", {})
    history = load("history.json", {"rows": []})
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    target = sys.argv[1] if len(sys.argv) > 1 else None

    pending_games = {}
    for key, row in log.items():
        if target and row["d"] != target:
            continue
        if not target and row["d"] >= today:
            continue  # only grade past dates in default mode
        pending_games.setdefault(row["g"], []).append(key)

    if not pending_games:
        print("Nothing to grade")
        return

    graded = removed = 0
    for game_pk, keys in pending_games.items():
        try:
            batting = game_batting(game_pk)
        except Exception as exc:  # noqa: BLE001
            print(f"  boxscore {game_pk} unavailable ({exc}); skipping", file=sys.stderr)
            continue
        if batting is None:
            print(f"  game {game_pk} not final; keeping snapshots")
            continue
        for key in keys:
            row = log.pop(key)
            removed += 1
            pa, hr = batting.get(row["id"], (0, 0))
            if pa == 0:
                continue  # never batted — not a gradeable prediction
            row["hit"] = hr >= 1
            row["hr"] = hr
            history["rows"].append(row)
            graded += 1
        time.sleep(0.3)

    (DATA_DIR / "picks_log.json").write_text(json.dumps(log, indent=0))
    (DATA_DIR / "history.json").write_text(json.dumps(history, indent=0))
    (DATA_DIR / "track_record.json").write_text(
        json.dumps(summarize(history["rows"]), indent=1)
    )
    print(f"Graded {graded} picks ({removed - graded} DNP dropped); "
          f"history now {len(history['rows'])} rows")


if __name__ == "__main__":
    main()
