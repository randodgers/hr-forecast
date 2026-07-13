#!/usr/bin/env python3
"""Home Run Forecast data pipeline (v2).

Fetches today's MLB slate, weather, park factors, and player quality-of-contact
data from free APIs and writes data/predictions.json for the static site.

Data sources (all free, no API keys):
  - MLB Stats API (statsapi.mlb.com): schedule, probable pitchers, lineups,
    rosters, venue coordinates / azimuth / roof, and per-player season stats,
    last-10-games form, and platoon splits (vs LHP / vs RHP).
  - Open-Meteo (api.open-meteo.com): hourly temp, wind, humidity, pressure.
  - Baseball Savant: 3-yr rolling Statcast HR park factors, plus the
    exit-velocity/barrels leaderboard CSV (barrel rate per PA).

Zero third-party dependencies — stdlib only.
"""

import csv
import io
import json
import math
import re
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

STATSAPI = "https://statsapi.mlb.com/api/v1"
SAVANT_PF_URL = (
    "https://baseballsavant.mlb.com/leaderboard/statcast-park-factors"
    "?type=year&year={year}&batSide=&stat=index_wOBA&condition=All&rolling="
)
SAVANT_BARRELS_URL = (
    "https://baseballsavant.mlb.com/leaderboard/statcast"
    "?type={side}&year={year}&position=&team=&min=25&csv=true"
)
BOVADA_EVENTS_URL = (
    "https://www.bovada.lv/services/sports/event/coupon/events/A/description"
    "/baseball/mlb?marketFilterId=def&preMatchOnly=true&lang=en"
)
BOVADA_EVENT_URL = (
    "https://www.bovada.lv/services/sports/event/coupon/events/A/description"
    "{link}?lang=en"
)
HOMERUN_ODDS_URL = "https://djstrauss08.github.io/HomeRunOdds/api/v1/players.json"
SAVANT_ARSENAL_URL = (
    "https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats"
    "?type={side}&pitchType=&year={year}&team=&min=10&csv=true"
)
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
)

# League baselines (2024-2026 era). Used as shrinkage priors so small
# samples don't produce silly probabilities.
LG_HR_PER_PA = 0.031
LG_HR_PER_9 = 1.10

# Model knobs — documented in README methodology section.
BATTER_PRIOR_PA = 200        # PA of league-average blended into overall rate
PLATOON_PRIOR_PA = 250       # PA of the batter's own overall rate blended into splits
FORM_PRIOR_PA = 60           # PA of the batter's own rate blended into L10 form
FORM_CLAMP = (0.85, 1.20)    # bounds on the hot/cold multiplier
BARREL_HR_RATE = 0.55        # league share of barrels that become home runs
BARREL_BLEND = 0.35          # weight of barrel-based xHR rate vs platoon rate
MIN_BARREL_BBE = 25          # min batted-ball events before barrels count
PITCHER_PRIOR_BF = 150       # batters faced of league-average blended into splits
PITCHER_BARREL_BLEND = 0.35  # weight of barrels-allowed xHR vs split-based rate
PITCHER_PRIOR_IP = 60        # IP prior for the season HR/9 fallback
STARTER_SHARE = 0.65         # share of batter PAs vs the starting pitcher
PARK_DAMPING = 0.85          # regression of park factor toward 100
ARSENAL_WEIGHT = 0.5         # damping of the pitch-arsenal matchup index
ARSENAL_CLAMP = (0.85, 1.18)
ARSENAL_MIN_USAGE = 5.0      # ignore pitches under this usage %
ARSENAL_MIN_SEEN = 40        # batter must have seen this many of a pitch type
MARKET_DEVIG_DEFAULT = 0.85  # prior haircut on one-sided prop implied prob
MARKET_DEVIG_CLAMP = (0.70, 1.00)
MARKET_DEVIG_MIN_N = 200     # graded priced picks before empirical calibration
TEMP_PCT_PER_DEG_F = 0.007   # relative HR change per degree F vs 72F
WIND_PCT_PER_MPH = 0.012     # relative HR change per mph of out-blowing wind
WIND_SHIELD = 0.6            # stadiums shield field-level wind vs 10m reading
BASELINE_TEMP_F = 72.0
MIN_PA_FALLBACK = 150        # roster fallback when no lineup is posted


def get_json(url, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "hr-forecast/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - retry any transient failure
            if attempt == retries - 1:
                raise
            print(f"  retry {attempt + 1} for {url}: {exc}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))


def get_text(url, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "hr-forecast/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            print(f"  retry {attempt + 1} for {url}: {exc}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))


def fetch_park_factors(year):
    """Return {venue_id(int): hr_index(int)} from Savant, with local fallback."""
    snapshot = DATA_DIR / "park_factors.json"
    try:
        html = get_text(SAVANT_PF_URL.format(year=year))
        match = re.search(r"var data = (\[.*?\]);", html, re.DOTALL)
        if not match:
            raise ValueError("park factor payload not found in page")
        rows = json.loads(match.group(1))
        factors = {int(r["venue_id"]): int(r["index_hr"]) for r in rows}
        if len(factors) >= 25:
            snapshot.write_text(json.dumps(factors, indent=2))
            print(f"Park factors: {len(factors)} venues from Baseball Savant")
            return factors
        raise ValueError(f"only {len(factors)} venues parsed")
    except Exception as exc:  # noqa: BLE001
        print(f"Park factor scrape failed ({exc}); using snapshot", file=sys.stderr)
        if snapshot.exists():
            return {int(k): v for k, v in json.loads(snapshot.read_text()).items()}
        print("No snapshot available — defaulting all parks to 100", file=sys.stderr)
        return {}


def fetch_barrel_rates(year, side="batter"):
    """Return {player_id: {brlPa, bbe, avgEv}} from the Savant EV leaderboard.

    side="batter" gives barrels hit; side="pitcher" gives barrels allowed.
    Optional signal — on any failure the model just runs without barrels.
    """
    snapshot = DATA_DIR / f"barrels_{side}.json"
    try:
        text = get_text(SAVANT_BARRELS_URL.format(year=year, side=side))
        rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
        out = {}
        for r in rows:
            try:
                out[int(r["player_id"])] = {
                    "brlPa": float(r["brl_pa"]),
                    "bbe": int(r["attempts"]),
                    "avgEv": float(r["avg_hit_speed"]),
                }
            except (KeyError, ValueError):
                continue
        if len(out) < 100:
            raise ValueError(f"only {len(out)} {side}s parsed")
        snapshot.write_text(json.dumps(out))
        print(f"Barrel rates: {len(out)} {side}s from Baseball Savant")
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"Barrel scrape ({side}) failed ({exc}); using snapshot", file=sys.stderr)
        if snapshot.exists():
            return {int(k): v for k, v in json.loads(snapshot.read_text()).items()}
        return {}


def fetch_arsenal(year):
    """Pitch-arsenal matchup data from Savant.

    Returns (pitchers, batters, league):
      pitchers: {pid: [{pt, usage, xslg}]}         — mix + damage allowed
      batters:  {bid: {pt: {xslg, pitches}}}       — performance vs pitch type
      league:   {pt: xslg}                         — usage-weighted league avg
    Optional signal — empty dicts on failure mean arsenal_mult stays 1.0.
    """
    snapshot = DATA_DIR / "arsenal.json"
    try:
        pitchers, batters = {}, {}
        lg_num, lg_den = {}, {}
        for side in ("pitcher", "batter"):
            text = get_text(SAVANT_ARSENAL_URL.format(side=side, year=year))
            for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
                try:
                    pid = int(r["player_id"])
                    pt = r["pitch_type"]
                    n = int(r["pitches"])
                    xslg = float(r["est_slg"] or r["slg"])
                except (KeyError, ValueError):
                    continue
                if side == "pitcher":
                    usage = float(r["pitch_usage"] or 0)
                    pitchers.setdefault(pid, []).append(
                        {"pt": pt, "usage": usage, "xslg": xslg}
                    )
                    lg_num[pt] = lg_num.get(pt, 0.0) + xslg * n
                    lg_den[pt] = lg_den.get(pt, 0) + n
                else:
                    batters.setdefault(pid, {})[pt] = {"xslg": xslg, "pitches": n}
        league = {pt: lg_num[pt] / lg_den[pt] for pt in lg_num if lg_den[pt]}
        if len(pitchers) < 100 or len(batters) < 100:
            raise ValueError("arsenal CSVs too small")
        snapshot.write_text(json.dumps(
            {"pitchers": pitchers, "batters": batters, "league": league}
        ))
        print(f"Arsenal: {len(pitchers)} pitchers, {len(batters)} batters, "
              f"{len(league)} pitch types")
        return pitchers, batters, league
    except Exception as exc:  # noqa: BLE001
        print(f"Arsenal scrape failed ({exc}); using snapshot", file=sys.stderr)
        if snapshot.exists():
            d = json.loads(snapshot.read_text())
            return (
                {int(k): v for k, v in d["pitchers"].items()},
                {int(k): {pt: s for pt, s in v.items()} for k, v in d["batters"].items()},
                d["league"],
            )
        return {}, {}, {}


def arsenal_multiplier(pitcher_id, batter_id, arsenal):
    """Matchup multiplier from the pitcher's mix vs the batter's pitch-type damage.

    For each pitch the starter actually throws (≥5% usage), compare (a) the
    batter's expected SLG vs that pitch type and (b) the pitcher's expected SLG
    allowed on it, both against league average for the pitch type; weight by
    usage. This is the systematic version of the betting-doc workflow of
    isolating a starter's vulnerable pitches.
    """
    pitchers, batters, league = arsenal
    mix = pitchers.get(pitcher_id)
    if not mix:
        return 1.0
    bat = batters.get(batter_id, {})
    num = den = 0.0
    for p in mix:
        if p["usage"] < ARSENAL_MIN_USAGE or p["pt"] not in league:
            continue
        lg = league[p["pt"]]
        pit_ratio = p["xslg"] / lg if lg else 1.0
        b = bat.get(p["pt"])
        bat_ratio = (
            b["xslg"] / lg if b and b["pitches"] >= ARSENAL_MIN_SEEN and lg else 1.0
        )
        num += p["usage"] * (0.5 * bat_ratio + 0.5 * pit_ratio)
        den += p["usage"]
    if not den:
        return 1.0
    index = num / den
    mult = 1.0 + (index - 1.0) * ARSENAL_WEIGHT
    return max(ARSENAL_CLAMP[0], min(ARSENAL_CLAMP[1], mult))


def market_devig_factor():
    """Fair-probability haircut for one-sided prop prices.

    One-sided markets can't be de-vigged by pairing (no 'No' side is offered),
    so we start from a documented prior and, once enough of our own picks have
    been graded, calibrate empirically: actual HR rate of priced players
    divided by their average raw implied probability.
    """
    hist = DATA_DIR / "history.json"
    try:
        rows = json.loads(hist.read_text()).get("rows", [])
        priced = [r for r in rows if r.get("mkt")]
        if len(priced) >= MARKET_DEVIG_MIN_N:
            actual = sum(1 for r in priced if r["hit"]) / len(priced)
            implied = sum(r["mkt"] for r in priced) / len(priced) / 100.0
            factor = actual / implied if implied else MARKET_DEVIG_DEFAULT
            factor = max(MARKET_DEVIG_CLAMP[0], min(MARKET_DEVIG_CLAMP[1], factor))
            print(f"Market devig: empirical {factor:.3f} from {len(priced)} picks")
            return round(factor, 3), len(priced)
    except (OSError, ValueError, KeyError):
        pass
    return MARKET_DEVIG_DEFAULT, 0


def normalize_name(name):
    """'Peña, Jeremy' / 'Jeremy Peña (HOU)' / 'Jeremy Pena' → 'jeremy pena'."""
    name = re.sub(r"\s*\([A-Z]{2,3}\)\s*$", "", name.strip())
    if "," in name:
        last, first = name.split(",", 1)
        name = f"{first.strip()} {last.strip()}"
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    name = re.sub(r"[^a-z ]", "", name.lower())
    return re.sub(r"\s+", " ", name).strip()


def american_to_implied_pct(odds):
    """American odds → implied probability %, vig included (one-sided market)."""
    odds = int(odds)
    if odds > 0:
        return 100.0 * 100.0 / (odds + 100.0)
    return 100.0 * -odds / (-odds + 100.0)


def fetch_market_odds():
    """Return {normalized_name: american_odds} for 'to hit a HR' props.

    Primary: Bovada's public event JSON (no key). Secondary: the HomeRunOdds
    consensus feed on GitHub Pages. Both optional — the site works without
    market data (market columns just show a dash).
    """
    odds = {}
    try:
        groups = get_json_ua(BOVADA_EVENTS_URL)
        events = [e for grp in groups for e in grp.get("events", [])]
        print(f"Bovada: {len(events)} events listed")
        for ev in events:
            try:
                data = get_json_ua(BOVADA_EVENT_URL.format(link=ev["link"]), retries=1)
                full = data[0]["events"][0]
                for dg in full.get("displayGroups", []):
                    for m in dg.get("markets", []):
                        if m.get("description") != "Player to hit a Home Run":
                            continue
                        for o in m.get("outcomes", []):
                            price = (o.get("price") or {}).get("american")
                            if price and o.get("description"):
                                odds[normalize_name(o["description"])] = int(
                                    str(price).replace("+", "")
                                )
            except Exception as exc:  # noqa: BLE001 - skip single bad event
                print(f"  Bovada event skipped ({exc})", file=sys.stderr)
            time.sleep(0.4)
    except Exception as exc:  # noqa: BLE001
        print(f"Bovada odds unavailable ({exc})", file=sys.stderr)

    if not odds:
        try:
            data = get_json(HOMERUN_ODDS_URL)
            for p in data.get("players", []):
                name = p.get("player") or p.get("name") or p.get("player_name")
                best = p.get("best_odds") or p.get("consensus_odds") or p.get("odds")
                if isinstance(best, dict):
                    best = best.get("american") or best.get("price")
                if name and best is not None:
                    odds[normalize_name(str(name))] = int(str(best).replace("+", ""))
            print(f"HomeRunOdds fallback: {len(odds)} players")
        except Exception as exc:  # noqa: BLE001
            print(f"HomeRunOdds fallback unavailable ({exc})", file=sys.stderr)

    print(f"Market odds: {len(odds)} players priced")
    return odds


def get_json_ua(url, retries=3):
    """get_json with a browser User-Agent (Bovada rejects generic agents)."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def fetch_venue(venue_id):
    data = get_json(f"{STATSAPI}/venues/{venue_id}?hydrate=location,fieldInfo")
    v = data["venues"][0]
    loc = v.get("location", {})
    coords = loc.get("defaultCoordinates", {})
    return {
        "id": v["id"],
        "name": v["name"],
        "lat": coords.get("latitude"),
        "lon": coords.get("longitude"),
        "azimuth": loc.get("azimuthAngle"),
        "elevation": loc.get("elevation"),
        "roof": (v.get("fieldInfo") or {}).get("roofType", "Open"),
        "city": loc.get("city"),
    }


def fetch_weather(lat, lon):
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m,wind_speed_10m,wind_direction_10m,"
        "relative_humidity_2m,surface_pressure,cloud_cover"
        "&temperature_unit=fahrenheit&wind_speed_unit=mph"
        "&forecast_days=2&timezone=auto"
    )
    return get_json(url)


def weather_at(weather, game_utc):
    """Pick the forecast hour closest to first pitch (local time)."""
    offset = timedelta(seconds=weather["utc_offset_seconds"])
    local = game_utc + offset
    target = local.strftime("%Y-%m-%dT%H:00")
    hours = weather["hourly"]["time"]
    idx = hours.index(target) if target in hours else min(
        range(len(hours)), key=lambda i: abs(
            datetime.fromisoformat(hours[i]) - local.replace(tzinfo=None)
        )
    )
    h = weather["hourly"]
    return {
        "temp_f": h["temperature_2m"][idx],
        "wind_mph": h["wind_speed_10m"][idx],
        "wind_from_deg": h["wind_direction_10m"][idx],
        "humidity_pct": h["relative_humidity_2m"][idx],
        "pressure_hpa": h["surface_pressure"][idx],
        "cloud_pct": h["cloud_cover"][idx],
        "local_hour": hours[idx],
    }


def wind_out_component(wind_mph, wind_from_deg, park_azimuth):
    """Positive = blowing out to center field, negative = blowing in.

    Open-Meteo reports the direction wind comes FROM; a wind blowing out
    comes from behind home plate, i.e. from (azimuth + 180) % 360.
    """
    if park_azimuth is None:
        return 0.0
    out_from = (park_azimuth + 180) % 360
    delta = math.radians(wind_from_deg - out_from)
    return wind_mph * math.cos(delta) * WIND_SHIELD


def weather_multiplier(wx, roof, azimuth):
    """Relative HR-rate multiplier from atmosphere. 1.0 = neutral."""
    if roof in ("Dome", "Retractable") and _roof_likely_closed(wx, roof):
        return 1.0, 0.0, True
    out = wind_out_component(wx["wind_mph"], wx["wind_from_deg"], azimuth)
    mult = 1.0
    mult *= 1.0 + TEMP_PCT_PER_DEG_F * (wx["temp_f"] - BASELINE_TEMP_F)
    mult *= 1.0 + WIND_PCT_PER_MPH * max(min(out, 15.0), -15.0)
    # Air density: lower pressure carries the ball slightly farther.
    mult *= 1.0 - 0.0004 * (wx["pressure_hpa"] - 1013.0)
    return max(0.6, min(1.5, mult)), out, False


def _roof_likely_closed(wx, roof):
    if roof == "Dome":
        return True
    # Retractable roofs: assume closed in extreme heat (the common case for
    # Texas/Arizona/Miami day games) — a simple, documented heuristic.
    return wx["temp_f"] >= 95 or wx["temp_f"] <= 40


def hrfi_score(mult, roof_closed):
    """Map the weather multiplier onto a 1-10 index (5 = neutral)."""
    if roof_closed:
        return 5.0
    return round(max(1.0, min(10.0, 5.0 + (mult - 1.0) * 20.0)), 1)


def fetch_people_stats(person_ids, group, season, extra_types):
    """Batch season + split stats for up to 40 ids per call.

    Returns {id: {name, bats, throws, blocks: {typeName+splitCode: stat}}}
    e.g. blocks keys: "season", "lastXGames", "statSplits:vl", "statSplits:vr".
    """
    hydrate = (
        f"stats(group=[{group}],type=[season{extra_types}],"
        f"sitCodes=[vr,vl],limit=10,season={season})"
    )
    out = {}
    ids = list(person_ids)
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        url = (
            f"{STATSAPI}/people?personIds={','.join(map(str, chunk))}"
            f"&hydrate={hydrate}"
        )
        data = get_json(url)
        for person in data.get("people", []):
            blocks = {}
            for block in person.get("stats", []):
                tname = block.get("type", {}).get("displayName", "")
                for sp in block.get("splits", []):
                    code = (sp.get("split") or {}).get("code")
                    key = f"{tname}:{code}" if code else tname
                    # lastXGames can appear twice with identical data; keep first
                    blocks.setdefault(key, sp["stat"])
            out[person["id"]] = {
                "name": person.get("fullName"),
                "bats": (person.get("batSide") or {}).get("code"),
                "throws": (person.get("pitchHand") or {}).get("code"),
                "blocks": blocks,
            }
        time.sleep(0.3)
    return out


def fetch_roster_hitter_ids(team_id):
    data = get_json(f"{STATSAPI}/teams/{team_id}/roster?rosterType=active")
    return [
        r["person"]["id"]
        for r in data.get("roster", [])
        if r.get("position", {}).get("code") != "1"
    ]


def _hr_pa(stat):
    return int(stat.get("homeRuns", 0)), int(stat.get("plateAppearances", 0))


def effective_bat_side(bats, pitcher_hand):
    """Side the batter actually hits from: switch hitters take the platoon edge."""
    if bats == "S" and pitcher_hand in ("L", "R"):
        return "L" if pitcher_hand == "R" else "R"
    return bats if bats in ("L", "R") else None


def batter_power_rate(blocks, pitcher_hand, barrels):
    """Blended HR rate per PA: platoon-split rate + barrel-based xHR rate."""
    season = blocks.get("season", {})
    hr, pa = _hr_pa(season)
    overall = (hr + LG_HR_PER_PA * BATTER_PRIOR_PA) / (pa + BATTER_PRIOR_PA)

    # Platoon: the batter's rate vs this pitcher's hand, regressed toward
    # his own overall rate (splits are noisy).
    rate = overall
    split = blocks.get(f"statSplits:v{pitcher_hand.lower()}") if pitcher_hand else None
    if split:
        s_hr, s_pa = _hr_pa(split)
        if s_pa >= 20:  # below that the split is pure noise
            rate = (s_hr + overall * PLATOON_PRIOR_PA) / (s_pa + PLATOON_PRIOR_PA)

    # Barrels: quality-of-contact expected HR rate, blended in when the
    # sample is meaningful.
    brl = barrels or {}
    if brl.get("bbe", 0) >= MIN_BARREL_BBE:
        x_rate = (brl["brlPa"] / 100.0) * BARREL_HR_RATE
        rate = (1 - BARREL_BLEND) * rate + BARREL_BLEND * x_rate

    return rate, overall, hr, pa


def form_multiplier(blocks, overall, season_pa):
    """Hot/cold multiplier from the last 10 games, heavily regressed.

    Neutral for small season samples: a recent call-up's L10 IS his season,
    so a form signal would just double-count the same handful of PAs.
    """
    l10 = blocks.get("lastXGames")
    if not l10:
        return 1.0, None, None
    hr10, pa10 = _hr_pa(l10)
    if pa10 < 15 or season_pa < 100 or season_pa <= pa10 * 1.5:
        return 1.0, hr10, pa10
    l10_rate = (hr10 + overall * FORM_PRIOR_PA) / (pa10 + FORM_PRIOR_PA)
    mult = max(FORM_CLAMP[0], min(FORM_CLAMP[1], l10_rate / overall))
    return mult, hr10, pa10


def pitcher_multiplier(pitcher, batter_side, brl_allowed=None):
    """HR-allowed multiplier vs league, preferring the split vs this batter side.

    When Savant barrels-allowed data exists, the results-based rate is blended
    with a quality-of-contact expected rate (barrels allowed per PA × the
    league barrel→HR share) so lucky/unlucky HR totals get corrected.
    """
    if not pitcher:
        return 1.0, None
    blocks = pitcher["blocks"]
    season = blocks.get("season", {})
    hr9 = season.get("homeRunsPer9")
    split = blocks.get(f"statSplits:v{batter_side.lower()}") if batter_side else None
    if split and split.get("battersFaced"):
        bf = int(split["battersFaced"])
        hr = int(split.get("homeRuns", 0))
        rate = (hr + LG_HR_PER_PA * PITCHER_PRIOR_BF) / (bf + PITCHER_PRIOR_BF)
        raw = rate / LG_HR_PER_PA
    else:
        # Fallback: season HR/9 shrunk by innings pitched.
        try:
            ip = float(str(season.get("inningsPitched", "0"))
                       .replace(".1", ".33").replace(".2", ".67"))
            hr9_f = float(hr9) if hr9 is not None else LG_HR_PER_9
        except (TypeError, ValueError):
            return 1.0, hr9
        shrunk = (hr9_f * ip + LG_HR_PER_9 * PITCHER_PRIOR_IP) / (ip + PITCHER_PRIOR_IP)
        raw = shrunk / LG_HR_PER_9
    brl = brl_allowed or {}
    if brl.get("bbe", 0) >= MIN_BARREL_BBE:
        x_raw = (brl["brlPa"] / 100.0) * BARREL_HR_RATE / LG_HR_PER_PA
        raw = (1 - PITCHER_BARREL_BLEND) * raw + PITCHER_BARREL_BLEND * x_raw
    # Batters only face the starter for part of the game; bullpen ≈ average.
    return STARTER_SHARE * raw + (1 - STARTER_SHARE), hr9


def expected_pa(lineup_spot):
    if lineup_spot is None:
        return 4.1
    return 4.7 - (lineup_spot - 1) * 0.11


def log_picks(date, players, game_times):
    """Snapshot pre-game predictions for later grading.

    Keyed by date|gamePk|playerId. Each refresh overwrites a player's snapshot
    only while his game hasn't started, so the log ends up holding the final
    pre-game numbers. grade_results.py consumes and removes these rows.
    Logged: everyone in a confirmed lineup or with a market price.
    """
    path = DATA_DIR / "picks_log.json"
    try:
        log = json.loads(path.read_text())
    except (OSError, ValueError):
        log = {}
    now = datetime.now(timezone.utc)
    for p in players:
        if p["lineupSpot"] is None and p["marketPct"] is None:
            continue
        start = game_times.get(p["gamePk"])
        if start and datetime.fromisoformat(start.replace("Z", "+00:00")) <= now:
            continue
        key = f"{date}|{p['gamePk']}|{p['playerId']}"
        log[key] = {
            "d": date,
            "g": p["gamePk"],
            "id": p["playerId"],
            "name": p["name"],
            "team": p["team"],
            "prob": p["probPct"],
            "mkt": p["marketPct"],
            "fair": p["marketFairPct"],
            "spot": p["lineupSpot"],
        }
    path.write_text(json.dumps(log, indent=0))
    print(f"Picks log: {len(log)} pending snapshots")


def main():
    season = datetime.now(timezone.utc).astimezone().year
    date = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    if len(sys.argv) > 1:
        date = sys.argv[1]

    print(f"Building predictions for {date} (season {season})")
    park_factors = fetch_park_factors(season - 1)
    barrel_rates = fetch_barrel_rates(season, "batter")
    barrel_allowed = fetch_barrel_rates(season, "pitcher")
    arsenal = fetch_arsenal(season)
    market_odds = fetch_market_odds()
    devig, devig_n = market_devig_factor()

    sched = get_json(
        f"{STATSAPI}/schedule?sportId=1&date={date}"
        "&hydrate=probablePitcher,lineups"
    )
    games = []
    for d in sched.get("dates", []):
        games.extend(d.get("games", []))
    games = [g for g in games if g.get("status", {}).get("detailedState") != "Postponed"]
    print(f"{len(games)} games scheduled")

    venues, weather_cache = {}, {}
    parks_out, players_out = [], []

    batter_ids, pitcher_ids = set(), set()
    game_ctx = []
    for g in games:
        vid = g["venue"]["id"]
        if vid not in venues:
            venues[vid] = fetch_venue(vid)
            time.sleep(0.2)
        lineups = g.get("lineups", {})
        ctx = {"game": g, "venue": venues[vid], "sides": {}}
        for side in ("away", "home"):
            team = g["teams"][side]
            probable = team.get("probablePitcher", {}).get("id")
            if probable:
                pitcher_ids.add(probable)
            lineup = [p["id"] for p in lineups.get(f"{side}Players", [])]
            roster = None
            if lineup:
                batter_ids.update(lineup)
            else:
                roster = fetch_roster_hitter_ids(team["team"]["id"])
                batter_ids.update(roster)
                time.sleep(0.2)
                lineup = None
            ctx["sides"][side] = {
                "team": team["team"],
                "pitcher_id": probable,
                "lineup": lineup,
                "roster": roster,
            }
        game_ctx.append(ctx)

    print(f"Fetching stats: {len(batter_ids)} batters, {len(pitcher_ids)} pitchers")
    batters = fetch_people_stats(
        batter_ids, "hitting", season, extra_types=",lastXGames,statSplits"
    )
    pitchers = fetch_people_stats(
        pitcher_ids, "pitching", season, extra_types=",statSplits"
    )

    for ctx in game_ctx:
        g, venue = ctx["game"], ctx["venue"]
        game_utc = datetime.fromisoformat(g["gameDate"].replace("Z", "+00:00"))
        vid = venue["id"]
        if vid not in weather_cache and venue["lat"] is not None:
            weather_cache[vid] = fetch_weather(venue["lat"], venue["lon"])
            time.sleep(0.2)
        wx = weather_at(weather_cache[vid], game_utc) if vid in weather_cache else None
        if wx is None:
            continue

        w_mult, wind_out, roof_closed = weather_multiplier(
            wx, venue["roof"], venue["azimuth"]
        )
        pf = park_factors.get(vid, 100)
        park_mult = 1.0 + (pf / 100.0 - 1.0) * PARK_DAMPING
        hrfi = hrfi_score(w_mult, roof_closed)

        away = g["teams"]["away"]["team"].get("abbreviation") or g["teams"]["away"]["team"]["name"]
        home = g["teams"]["home"]["team"].get("abbreviation") or g["teams"]["home"]["team"]["name"]

        parks_out.append({
            "venue": venue["name"],
            "city": venue["city"],
            "matchup": f"{away} @ {home}",
            "gameTimeUtc": g["gameDate"],
            "localHour": wx["local_hour"],
            "dayNight": g.get("dayNight"),
            "roof": venue["roof"],
            "roofClosed": roof_closed,
            "hrfi": hrfi,
            "tempF": wx["temp_f"],
            "windMph": wx["wind_mph"],
            "windFromDeg": wx["wind_from_deg"],
            "windOutMph": round(wind_out, 1),
            "humidityPct": wx["humidity_pct"],
            "cloudPct": wx["cloud_pct"],
            "parkFactorHR": pf,
            "elevationFt": venue["elevation"],
        })

        for side in ("away", "home"):
            info = ctx["sides"][side]
            opp_info = ctx["sides"]["home" if side == "away" else "away"]
            opp_pitcher = pitchers.get(opp_info["pitcher_id"]) if opp_info["pitcher_id"] else None
            pitcher_hand = opp_pitcher["throws"] if opp_pitcher else None

            ids = info["lineup"] or info["roster"] or []
            for spot, pid in enumerate(ids, start=1):
                b = batters.get(pid)
                if not b or "season" not in b["blocks"]:
                    continue
                bat_side = effective_bat_side(b["bats"], pitcher_hand)
                brl = barrel_rates.get(pid)
                rate, overall, hr, pa = batter_power_rate(
                    b["blocks"], pitcher_hand, brl
                )
                if info["lineup"] is None and pa < MIN_PA_FALLBACK:
                    continue
                f_mult, hr10, pa10 = form_multiplier(b["blocks"], overall, pa)
                pit_brl = (
                    barrel_allowed.get(opp_info["pitcher_id"])
                    if opp_info["pitcher_id"] else None
                )
                pit_mult, hr9 = pitcher_multiplier(opp_pitcher, bat_side, pit_brl)
                ars_mult = arsenal_multiplier(
                    opp_info["pitcher_id"], pid, arsenal
                ) if opp_info["pitcher_id"] else 1.0

                lineup_spot = spot if info["lineup"] else None
                p_pa = rate * f_mult * pit_mult * ars_mult * park_mult * w_mult
                p_game = 1.0 - (1.0 - p_pa) ** expected_pa(lineup_spot)

                m_odds = market_odds.get(normalize_name(b["name"] or ""))
                m_pct = round(american_to_implied_pct(m_odds), 1) if m_odds else None
                fair_pct = round(m_pct * devig, 1) if m_pct else None
                platoon_edge = (
                    bat_side is not None and pitcher_hand is not None
                    and bat_side != pitcher_hand
                )
                players_out.append({
                    "name": b["name"],
                    "bats": b["bats"],
                    "team": info["team"].get("name"),
                    "opponent": opp_info["team"].get("name"),
                    "pitcher": opp_pitcher["name"] if opp_pitcher else "TBD",
                    "pitcherHand": pitcher_hand,
                    "pitcherHr9": hr9,
                    "platoonEdge": platoon_edge,
                    "venue": venue["name"],
                    "lineupSpot": lineup_spot,
                    "seasonHr": hr,
                    "seasonPa": pa,
                    "l10Hr": hr10,
                    "l10Pa": pa10,
                    "brlPa": brl["brlPa"] if brl else None,
                    "probPct": round(p_game * 100, 1),
                    "marketOdds": m_odds,
                    "marketPct": m_pct,
                    "marketFairPct": fair_pct,
                    "edgePct": round(p_game * 100 - fair_pct, 1) if fair_pct else None,
                    "gamePk": g["gamePk"],
                    "playerId": pid,
                    "factors": {
                        "batterRatePct": round(rate * 100, 2),
                        "formMult": round(f_mult, 3),
                        "pitcherMult": round(pit_mult, 3),
                        "arsenalMult": round(ars_mult, 3),
                        "parkMult": round(park_mult, 3),
                        "weatherMult": round(w_mult, 3),
                    },
                })

    players_out.sort(key=lambda p: p["probPct"], reverse=True)
    parks_out.sort(key=lambda p: p["hrfi"], reverse=True)
    log_picks(date, players_out, {g["gamePk"]: g["gameDate"] for g in games})

    out = {
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": date,
        "gameCount": len(parks_out),
        "parks": parks_out,
        "players": players_out[:60],
        "leagueBaselines": {"hrPerPa": LG_HR_PER_PA, "hrPer9": LG_HR_PER_9},
        "marketDevig": {"factor": devig, "calibratedOn": devig_n},
    }
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "predictions.json").write_text(json.dumps(out, indent=1))
    print(f"Wrote {DATA_DIR / 'predictions.json'}: "
          f"{len(parks_out)} parks, {len(players_out)} players scored")


if __name__ == "__main__":
    main()
