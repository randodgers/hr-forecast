const rampColor = (t) => {
  // Interpolate the sequential blue ramp (lo → hi) by magnitude t in [0,1].
  const styles = getComputedStyle(document.documentElement);
  const hex = (v) => styles.getPropertyValue(v).trim();
  const mix = (a, b, k) => {
    const pa = parseInt(a.slice(1), 16), pb = parseInt(b.slice(1), 16);
    const ch = (sh) => Math.round(((pa >> sh) & 255) + (((pb >> sh) & 255) - ((pa >> sh) & 255)) * k);
    return `rgb(${ch(16)}, ${ch(8)}, ${ch(0)})`;
  };
  return t < 0.5
    ? mix(hex("--ramp-lo"), hex("--ramp-mid"), t * 2)
    : mix(hex("--ramp-mid"), hex("--ramp-hi"), (t - 0.5) * 2);
};

const fmtWind = (p) => {
  if (p.roofClosed) return "roof closed";
  const out = p.windOutMph;
  if (Math.abs(out) < 1) return `wind neutral (${Math.round(p.windMph)} mph)`;
  return out > 0 ? `wind out ${out} mph ↗` : `wind in ${Math.abs(out)} mph ↘`;
};

const localTime = (iso) =>
  new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

function renderParks(parks) {
  const grid = document.getElementById("parks");
  grid.innerHTML = "";
  for (const p of parks) {
    const t = (p.hrfi - 1) / 9;
    const card = document.createElement("div");
    card.className = "park-card";
    card.innerHTML = `
      <div class="score-row">
        <span class="hrfi">${p.hrfi.toFixed(1)}</span>
        <span class="roof-tag">${p.roofClosed ? "Roof closed" : (p.dayNight === "day" ? "Day" : "Night")} · ${localTime(p.gameTimeUtc)}</span>
      </div>
      <div class="meter" role="img" aria-label="HRFI ${p.hrfi} of 10">
        <span style="width:${(t * 100).toFixed(0)}%; background:${rampColor(t)}"></span>
      </div>
      <div class="venue">${p.venue}</div>
      <div class="matchup">${p.matchup}</div>
      <div class="wx">
        <span>${Math.round(p.tempF)}°F</span>
        <span>${fmtWind(p)}</span>
        <span>park HR ${p.parkFactorHR}</span>
      </div>`;
    grid.appendChild(card);
  }
}

function factorChips(pl) {
  const f = pl.factors;
  const chips = [];
  const chip = (label, v) => {
    const cls = v >= 1.05 ? "up" : v <= 0.95 ? "down" : "";
    chips.push(`<span class="chip ${cls}" title="${label} multiplier ${v}">${label} ${v >= 1 ? "+" : "−"}${Math.abs(Math.round((v - 1) * 100))}%</span>`);
  };
  if (pl.platoonEdge) chips.push(`<span class="chip up" title="Batter has the platoon advantage vs this starter">platoon ✓</span>`);
  if (f.formMult != null) chip("form", f.formMult);
  chip("pitcher", f.pitcherMult);
  chip("park", f.parkMult);
  chip("weather", f.weatherMult);
  return chips.join("");
}

function renderPlayers(players, lineupOnly, maxProb, sortBy) {
  const tbody = document.querySelector("#players-table tbody");
  tbody.innerHTML = "";
  let rows = lineupOnly ? players.filter((p) => p.lineupSpot) : players;
  if (sortBy === "edge") {
    rows = rows.filter((p) => p.edgePct != null)
      .slice().sort((a, b) => b.edgePct - a.edgePct);
  }
  rows.slice(0, 30).forEach((pl, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="num">${i + 1}</td>
      <td class="player">${pl.name}${pl.bats ? ` <span class="sub">bats ${pl.bats}${pl.lineupSpot ? ` · batting ${pl.lineupSpot}` : " · lineup TBD"}</span>` : ""}</td>
      <td>${pl.team}<span class="sub" title="vs ${pl.pitcher}">vs ${pl.pitcher.split(" ").slice(-1)[0]}${pl.pitcherHand ? ` (${pl.pitcherHand}HP)` : ""}${pl.pitcherHr9 != null ? ` · ${pl.pitcherHr9} HR/9` : ""}</span></td>
      <td class="venue-cell" title="${pl.venue}">${pl.venue}</td>
      <td class="num">${pl.seasonHr} HR<span class="sub">${pl.seasonPa} PA</span></td>
      <td class="num">${pl.l10Hr != null ? `${pl.l10Hr} HR<span class="sub">${pl.l10Pa} PA</span>` : "—"}</td>
      <td class="num">${pl.brlPa != null ? `${pl.brlPa.toFixed(1)}%` : "—"}</td>
      <td class="num"><span class="prob-cell"><span class="prob-bar"><span style="width:${(pl.probPct / maxProb) * 100}%"></span></span><b>${pl.probPct}%</b></span></td>
      <td class="num">${pl.marketOdds != null ? `${pl.marketOdds > 0 ? "+" : ""}${pl.marketOdds}<span class="sub">${pl.marketPct}% impl.</span>` : "—"}</td>
      <td class="num">${pl.edgePct != null ? `<span class="edge ${pl.edgePct >= 2 ? "up" : pl.edgePct <= -2 ? "down" : ""}">${pl.edgePct > 0 ? "+" : ""}${pl.edgePct.toFixed(1)}</span>` : "—"}</td>
      <td><span class="chips">${factorChips(pl)}</span></td>`;
    tbody.appendChild(tr);
  });
}

async function init() {
  const res = await fetch(`data/predictions.json?t=${Date.now()}`);
  const data = await res.json();

  const gen = new Date(data.generatedAtUtc);
  document.getElementById("meta").textContent =
    `${data.date} · ${data.gameCount} games · updated ${gen.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`;

  renderParks(data.parks);

  const maxProb = Math.max(...data.players.map((p) => p.probPct), 1);
  const toggle = document.getElementById("lineup-only");
  const sortSel = document.getElementById("sort-by");
  const draw = () =>
    renderPlayers(data.players, toggle.checked, maxProb, sortSel.value);
  toggle.addEventListener("change", draw);
  sortSel.addEventListener("change", draw);
  draw();
}

init().catch((err) => {
  document.getElementById("meta").textContent = `Failed to load data: ${err.message}`;
});
