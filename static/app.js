const WALLET_KEY = "pmbc_wallet_address";
const REFRESH_MS = 30000;
let liveOn = true;
let refreshTimer = null;
let latestGames = [];
let latestPaperSummary = null;

function pct(p) {
  return p === null || p === undefined ? "—" : `${Math.round(p * 100)}%`;
}

function setSectionVisible(panelId, navId, visible) {
  const panel = document.getElementById(panelId);
  if (panel) panel.hidden = !visible;
  const navLink = document.getElementById(navId);
  if (navLink) navLink.hidden = !visible;
}

function fmtTime(iso) {
  try {
    return new Date(iso).toLocaleString(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function selectedLeagues() {
  return Array.from(document.querySelectorAll("#league-filters input:checked")).map(
    (el) => el.value
  );
}

function renderOppList(elId, rows, metricFn) {
  const el = document.getElementById(elId);
  if (!el) return;
  if (!rows.length) {
    el.innerHTML = '<div class="empty-state">Nothing right now — checked every tracked game.</div>';
    return;
  }
  el.innerHTML = rows.map((r) => metricFn(r)).join("");
}

function arbCard(r) {
  return `
    <div class="opp-card">
      <div>
        <div class="matchup">${r.title}</div>
        <div class="sub">${r.league.toUpperCase()} · ${fmtTime(r.game_start)} · buy all outcomes for $${r.arbitrage.cost_to_buy_all_outcomes.toFixed(3)}, payout $1.00</div>
      </div>
      <div class="metric good">+${r.arbitrage.roi_pct.toFixed(2)}%</div>
      <a class="open-btn" href="${r.polymarket_url}" target="_blank" rel="noopener">Open →</a>
    </div>`;
}

function valueCard(r) {
  const best = r.value_bets.reduce((a, b) => (a.edge > b.edge ? a : b));
  return `
    <div class="opp-card">
      <div>
        <div class="matchup">${r.title}</div>
        <div class="sub">${r.league.toUpperCase()} · ${best.outcome} priced ${pct(best.polymarket_ask)} on Polymarket vs. ${pct(best.sportsbook_prob)} at sportsbooks</div>
      </div>
      <div class="metric warn">+${(best.edge * 100).toFixed(1)}pt</div>
      <a class="open-btn" href="${r.polymarket_url}" target="_blank" rel="noopener">Open →</a>
    </div>`;
}

function stakeAndPositionNote(r, signal) {
  const parts = [];
  if (signal.suggested_stake !== undefined) {
    parts.push(`<span class="stake-note">Suggested stake: $${signal.suggested_stake.toFixed(2)} (${(signal.suggested_stake_pct * 100).toFixed(1)}% of buying power)</span>`);
  } else if (r.balance_too_low) {
    parts.push(`<span class="stake-note muted">Balance too low to size a stake</span>`);
  }
  if (r.your_position) {
    const p = r.your_position;
    if (p.conflicts === true) {
      parts.push(`<span class="position-note conflict">⚠ You hold ${p.outcome} — opposite this signal</span>`);
    } else if (p.conflicts === false) {
      parts.push(`<span class="position-note match">✓ You already hold this</span>`);
    } else {
      parts.push(`<span class="position-note">You have a position in this market</span>`);
    }
  }
  return parts.length ? `<div class="stake-row">${parts.join("")}</div>` : "";
}

function underdogCard(r) {
  const s = r.underdog_bet;
  const basisTag = s.basis === "live" ? "LIVE" : "PRE-GAME";
  const when = s.basis === "live" && r.live_score
    ? `${r.live_score.away}-${r.live_score.home} (${r.live_score.detail})`
    : fmtTime(r.game_start);
  return `
    <div class="opp-card">
      <div>
        <div class="matchup">${r.title} <span class="basis-tag ${s.basis}">${basisTag}</span></div>
        <div class="sub">${r.league.toUpperCase()} · ${when} · ${s.outcome} trading at ${pct(s.market_price)} on Polymarket</div>
        ${stakeAndPositionNote(r, s)}
      </div>
      <div class="confidence-block">
        <div class="confidence-pct">${pct(s.confidence)}</div>
        <div class="confidence-label">model confidence</div>
      </div>
      <div class="metric good">${s.payout_multiplier ? s.payout_multiplier.toFixed(2) + "x" : "—"}</div>
      <a class="open-btn" href="${r.polymarket_url}" target="_blank" rel="noopener">Open →</a>
    </div>`;
}

function lockCard(r) {
  const l = r.lock;
  const basisTag = l.basis === "live" ? "LIVE" : "PRE-GAME";
  const when = l.basis === "live" && r.live_score
    ? `${r.live_score.away}-${r.live_score.home} (${r.live_score.detail})`
    : fmtTime(r.game_start);
  return `
    <div class="opp-card">
      <div>
        <div class="matchup">${r.title} <span class="basis-tag ${l.basis}">${basisTag}</span></div>
        <div class="sub">${r.league.toUpperCase()} · ${when} · ${l.outcome} priced ${pct(l.market_price)} on Polymarket</div>
        ${stakeAndPositionNote(r, l)}
      </div>
      <div class="confidence-block">
        <div class="confidence-pct">${pct(l.confidence)}</div>
        <div class="confidence-label">model confidence</div>
      </div>
      <div class="metric good">${l.payout_multiplier.toFixed(2)}x</div>
      <a class="open-btn" href="${r.polymarket_url}" target="_blank" rel="noopener">Open →</a>
    </div>`;
}

function buildTrackerSpark(timeline, matchedSide) {
  const w = 280;
  const h = 50;
  const vals = timeline.map((p) => {
    const v = matchedSide ? p.your_win_pct : p.home_win_pct;
    return v === null || v === undefined ? 0.5 : v;
  });
  const xScale = (i) => (i / Math.max(1, vals.length - 1)) * w;
  const yScale = (v) => h - v * h;
  const path = vals.map((v, i) => `${i === 0 ? "M" : "L"}${xScale(i).toFixed(1)},${yScale(v).toFixed(1)}`).join(" ");
  const lineColor = vals[vals.length - 1] >= 0.5 ? "#2ee6a6" : "#ff5c72";
  return `
    <svg viewBox="0 0 ${w} ${h}" class="tracker-spark" preserveAspectRatio="none">
      <line x1="0" y1="${yScale(0.5)}" x2="${w}" y2="${yScale(0.5)}" class="zero-line" />
      <path d="${path}" fill="none" stroke="${lineColor}" stroke-width="2" vector-effect="non-scaling-stroke" />
    </svg>`;
}

function trackerCard(r) {
  const t = r.position_tracker;
  const timeline = t.timeline;
  const last = timeline[timeline.length - 1];
  const sideLabel = t.matched_side === "home" ? t.home_team : t.matched_side === "away" ? t.away_team : null;
  const currentPct = sideLabel ? last.your_win_pct : last.home_win_pct;

  const plays = timeline
    .slice()
    .reverse()
    .slice(0, 15)
    .map((p) => {
      const playPct = sideLabel ? p.your_win_pct : p.home_win_pct;
      const scoreTag = p.scoring_play ? '<span class="basis-tag live">SCORE</span>' : "";
      const when = [p.period, p.clock].filter(Boolean).join(" · ");
      return `
      <div class="play-row">
        <div class="play-meta">${when} ${scoreTag}</div>
        <div class="play-text">${p.text}</div>
        <div class="play-pct">${pct(playPct)}</div>
      </div>`;
    })
    .join("");

  return `
    <div class="opp-card tracker-card">
      <div>
        <div class="matchup">${r.title} <span class="basis-tag live">LIVE</span></div>
        <div class="sub">${r.league.toUpperCase()} · ${r.live_score ? `${r.live_score.away}-${r.live_score.home} (${r.live_score.detail})` : ""}${sideLabel ? ` · tracking ${sideLabel}` : " · couldn't confirm which side your position is on — showing home team's win probability instead"}</div>
        ${buildTrackerSpark(timeline, !!sideLabel)}
        <div class="play-feed">${plays}</div>
      </div>
      <div class="confidence-block">
        <div class="confidence-pct">${pct(currentPct)}</div>
        <div class="confidence-label">${sideLabel ? "your side" : "home team"} win prob.</div>
      </div>
    </div>`;
}

function propCard(r) {
  const p = r.prop_bet;
  const label = p.team ? `${p.team} total` : "Game total";
  return `
    <div class="opp-card">
      <div>
        <div class="matchup">${r.title}</div>
        <div class="sub">${r.league.toUpperCase()} · ${fmtTime(r.game_start)} · ${label}: ${p.side} ${p.line} trading at ${pct(p.market_price)} on Polymarket</div>
        <div class="sub">Model projects ${p.projected} points${p.team ? "" : " total"} → ${pct(p.model_prob)} probability of ${p.side.toLowerCase()}</div>
      </div>
      <div class="confidence-block">
        <div class="confidence-pct">${pct(p.model_prob)}</div>
        <div class="confidence-label">model probability</div>
      </div>
      <div class="metric good">+${(p.edge * 100).toFixed(1)}pt</div>
      <a class="open-btn" href="${r.polymarket_url}" target="_blank" rel="noopener">Open →</a>
    </div>`;
}

function statsRow(label, home, away, fmt) {
  if (home === undefined && away === undefined) return "";
  return `<tr><th>${label}</th><td>${fmt(away)}</td><td>${fmt(home)}</td></tr>`;
}

function buildDetailsPanel(row, awayOut, homeOut) {
  const ap = awayOut.profile;
  const hp = homeOut.profile;
  if (!ap || !hp) {
    const reason =
      row.model_basis === "basic"
        ? "season record only (no detailed stats available for this league)"
        : row.model_basis === "insufficient_data"
        ? "not enough season data yet (both teams have no games played — too early in the season to model)"
        : "unavailable";
    return `<div>Model confidence: ${reason}.</div>`;
  }
  const p = (v) => (v === undefined || v === null ? "—" : `${Math.round(v * 100)}%`);
  const n = (v) => (v === undefined || v === null ? "—" : v);
  return `
    <table>
      <thead><tr><th></th><th>${awayOut.name}</th><th>${homeOut.name}</th></tr></thead>
      <tbody>
        ${statsRow("Season W%", hp.season_win_pct, ap.season_win_pct, p)}
        ${statsRow("Home/Road W%", hp.home_win_pct, ap.away_win_pct, p)}
        ${statsRow("Last 10 W%", hp.last10_win_pct, ap.last10_win_pct, p)}
        ${statsRow("Avg pts for", hp.avg_points_for, ap.avg_points_for, n)}
        ${statsRow("Avg pts against", hp.avg_points_against, ap.avg_points_against, n)}
        ${statsRow("Avg margin", hp.avg_margin, ap.avg_margin, n)}
      </tbody>
    </table>
    <div style="margin-top:6px">Model confidence blends season record (50%), home/road split (30%) and last-10 form (20%) via the log5 method, then averages with a scoring-margin estimate. ${row.status === "live" ? "Live estimate reweights toward the current score as the game progresses — a heuristic, not a trained in-game model." : ""}</div>`;
}

function teamPctRow(outcome, pillPct, pillKind) {
  const logo = outcome.logo
    ? `<img class="team-logo" src="${outcome.logo}" alt="">`
    : `<span class="team-logo team-logo-empty"></span>`;
  const mkt = outcome.best_ask !== null && outcome.best_ask !== undefined
    ? `<span class="team-mkt">mkt ${pct(outcome.best_ask)}</span>`
    : "";
  const pill = pillPct !== null && pillPct !== undefined
    ? `<span class="pct-pill ${pillKind}">${pct(pillPct)}</span>`
    : `<span class="pct-pill pct-pill-empty">—</span>`;
  return `
    <div class="team-pct-row">
      <div class="team-info">
        ${logo}
        <span class="team-name">${outcome.name}</span>
        ${mkt}
      </div>
      ${pill}
    </div>`;
}

function renderGameCard(row, template) {
  const node = template.content.cloneNode(true);
  const card = node.querySelector(".game-card");

  const away = row.outcomes.find((o) => o.home_away === "away") || row.outcomes[0];
  const home = row.outcomes.find((o) => o.home_away === "home") || row.outcomes[1];

  node.querySelector(".league-badge").textContent = row.league.toUpperCase();

  const statusEl = node.querySelector(".status-badge");
  if (row.status === "live") {
    statusEl.classList.add("is-live");
    const score = row.live_score ? ` ${row.live_score.away}-${row.live_score.home} · ${row.live_score.detail}` : "";
    statusEl.innerHTML = `<span class="dot-live"></span> LIVE${score}`;
  } else {
    statusEl.textContent = fmtTime(row.game_start);
  }

  const isLive = row.status === "live" && (away.live_prob !== undefined || home.live_prob !== undefined);
  const pillKind = isLive ? "pct-pill-live" : "pct-pill-model";
  const awayPct = isLive ? away.live_prob : away.model_prob;
  const homePct = isLive ? home.live_prob : home.model_prob;

  const rowsEl = node.querySelector(".team-pct-rows");
  rowsEl.innerHTML = teamPctRow(away, awayPct, pillKind) + teamPctRow(home, homePct, pillKind);

  const legendEl = node.querySelector(".pct-legend");
  legendEl.innerHTML = isLive ? "Live model estimate" : "Model confidence";

  const badgesEl = node.querySelector(".card-badges");
  const badges = [];
  if (row.arbitrage) badges.push(`<span class="badge arb">+${row.arbitrage.roi_pct.toFixed(1)}% arb</span>`);
  if (row.value_bets && row.value_bets.length) badges.push(`<span class="badge value">sportsbook value</span>`);
  if (row.underdog_bet) badges.push(`<span class="badge underdog">${pct(row.underdog_bet.confidence)} underdog</span>`);
  if (row.lock) badges.push(`<span class="badge lock">${row.lock.payout_multiplier.toFixed(2)}x lock</span>`);
  badgesEl.innerHTML = badges.join("");

  const toggle = node.querySelector(".details-toggle");
  const detailsPanel = node.querySelector(".details-panel");
  toggle.addEventListener("click", () => {
    const hidden = detailsPanel.hasAttribute("hidden");
    if (hidden) {
      detailsPanel.innerHTML = buildDetailsPanel(row, away, home);
      detailsPanel.removeAttribute("hidden");
      toggle.textContent = "Why? ▴";
    } else {
      detailsPanel.setAttribute("hidden", "");
      toggle.textContent = "Why? ▾";
    }
  });

  const link = node.querySelector(".open-link");
  link.href = row.polymarket_url;

  return node;
}

function matchesSearch(row, query) {
  if (!query) return true;
  const haystack = [row.title, row.league, ...row.outcomes.map((o) => o.name)]
    .join(" ")
    .toLowerCase();
  // every space-separated word in the query must appear somewhere, so
  // "chiefs bills" matches "Chiefs vs. Bills" regardless of word order.
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((word) => haystack.includes(word));
}

function renderFilteredGames() {
  const grid = document.getElementById("games-grid");
  const template = document.getElementById("game-card-template");
  const searchInput = document.getElementById("matchup-search");
  const query = searchInput ? searchInput.value.trim() : "";
  const liveOnlyEl = document.getElementById("live-only-filter");
  const liveOnly = liveOnlyEl ? liveOnlyEl.checked : false;

  const filtered = latestGames.filter(
    (row) => matchesSearch(row, query) && (!liveOnly || row.status === "live")
  );

  const filtersActive = query || liveOnly;
  document.getElementById("game-count").textContent = filtersActive
    ? `(${filtered.length} of ${latestGames.length})`
    : `(${latestGames.length})`;

  grid.innerHTML = "";
  if (filtersActive && !filtered.length) {
    const reason = query ? `matching "${query}"` : "";
    const liveReason = liveOnly ? (reason ? " that are live right now" : "live right now") : "";
    grid.innerHTML = `<div class="empty-state">No games ${reason}${liveReason}.</div>`;
    return;
  }
  filtered.forEach((row) => grid.appendChild(renderGameCard(row, template)));
}

function buildBalanceChart(history) {
  if (history.length < 2) {
    return '<div class="empty-state">No settled bets yet — the chart fills in as games finish.</div>';
  }
  const w = 680;
  const h = 170;
  const pad = 30;
  const ys = history.map((p) => p.balance);
  const yMin = Math.min(...ys, 0);
  const yMax = Math.max(...ys);
  const xScale = (i) => pad + (i / (history.length - 1)) * (w - 2 * pad);
  const yScale = (v) => (yMax === yMin ? h / 2 : h - pad - ((v - yMin) / (yMax - yMin)) * (h - 2 * pad));

  const path = history
    .map((p, i) => `${i === 0 ? "M" : "L"}${xScale(i).toFixed(1)},${yScale(p.balance).toFixed(1)}`)
    .join(" ");
  const startY = yScale(history[0].balance).toFixed(1);
  const last = history[history.length - 1];
  const lineColor = last.balance >= history[0].balance ? "#2ee6a6" : "#ff5c72";
  const lastSettled = history.filter((p) => p.timestamp).slice(-1)[0];

  return `
    <svg viewBox="0 0 ${w} ${h}" class="pnl-chart" preserveAspectRatio="none">
      <line x1="${pad}" y1="${startY}" x2="${w - pad}" y2="${startY}" class="zero-line" />
      <path d="${path}" fill="none" stroke="${lineColor}" stroke-width="2" vector-effect="non-scaling-stroke" />
    </svg>
    <div class="chart-axis-labels"><span>Start · $${history[0].balance.toFixed(2)}</span><span>${lastSettled ? fmtTime(lastSettled.timestamp) : ""}</span></div>
    <div class="chart-caption">Balance after each settled bet (dashed line = starting $100). ${history.length - 1} settled so far.</div>`;
}

function buildCalibrationTable(calibration) {
  if (!calibration.length) {
    return '<div class="empty-state">Not enough settled bets yet to check calibration.</div>';
  }
  const rows = calibration
    .map((c) => {
      const diff = c.actual_win_rate - c.avg_predicted_confidence;
      const diffClass = Math.abs(diff) <= 0.1 ? "calib-good" : "calib-bad";
      return `
      <tr>
        <td>${c.range}</td>
        <td>${pct(c.avg_predicted_confidence)}</td>
        <td><span class="calib-bar-track"><span class="calib-bar-fill" style="width:${c.actual_win_rate * 100}%"></span></span>${pct(c.actual_win_rate)}</td>
        <td class="${diffClass}">${diff >= 0 ? "+" : ""}${(diff * 100).toFixed(1)}pt</td>
        <td>${c.count}</td>
      </tr>`;
    })
    .join("");
  return `
    <table class="calibration-table">
      <thead><tr><th>Confidence range</th><th>Model predicted</th><th>Actually won</th><th>Diff</th><th># bets</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function buildSegmentTable(segments) {
  if (!segments.length) {
    return '<div class="empty-state">Not enough settled bets yet to size up any bet type.</div>';
  }
  const rows = segments
    .map((s) => {
      const pnlClass = s.pnl > 0 ? "pnl-pos" : s.pnl < 0 ? "pnl-neg" : "";
      const statusClass = s.paused ? "lost" : "won";
      const statusLabel = s.paused ? "paused" : "active";
      return `
      <tr>
        <td>${s.signal_type} · ${s.league.toUpperCase()}</td>
        <td>${pct(s.win_rate)}</td>
        <td class="${pnlClass}">${money(s.pnl)}</td>
        <td>${s.count}</td>
        <td><span class="bet-status ${statusClass}">${statusLabel}</span></td>
      </tr>`;
    })
    .join("");
  return `
    <table class="calibration-table">
      <thead><tr><th>Bet type</th><th>Win rate</th><th>P/L</th><th># settled</th><th>Status</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function buildBetHistory(bets) {
  if (!bets.length) return '<div class="empty-state">No bets placed yet — they appear automatically as signals come up.</div>';
  const rows = bets
    .slice(0, 25)
    .map((b) => {
      const pnlStr = b.pnl === null || b.pnl === undefined ? "—" : money(b.pnl);
      const pnlClass = b.pnl > 0 ? "pnl-pos" : b.pnl < 0 ? "pnl-neg" : "";
      return `
      <tr>
        <td><a href="${b.polymarket_url}" target="_blank" rel="noopener">${b.title}</a></td>
        <td>${b.outcome}</td>
        <td>${b.signal_type}</td>
        <td>${pct(b.confidence)}</td>
        <td>$${b.price.toFixed(3)}</td>
        <td>$${b.stake.toFixed(2)}</td>
        <td><span class="bet-status ${b.status}">${b.status}</span></td>
        <td class="${pnlClass}">${pnlStr}</td>
      </tr>`;
    })
    .join("");
  return `
    <table>
      <thead><tr><th>Game</th><th>Bet</th><th>Signal</th><th>Confidence</th><th>Price</th><th>Stake</th><th>Status</th><th>P/L</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function buildStrategyComparisonTable(strategies) {
  if (!strategies || !strategies.length) return "";
  const rows = strategies
    .map((s) => {
      const roiClass = s.roi_pct > 0 ? "pnl-pos" : s.roi_pct < 0 ? "pnl-neg" : "";
      const isBaseline = s.strategy_id === "baseline";
      return `
      <tr${isBaseline ? ' class="strategy-row-baseline"' : ""}>
        <td>${s.label}</td>
        <td>$${s.balance.toFixed(2)}</td>
        <td class="${roiClass}">${s.roi_pct === null ? "—" : (s.roi_pct >= 0 ? "+" : "") + s.roi_pct.toFixed(1) + "%"}</td>
        <td>${s.win_rate === null ? "—" : pct(s.win_rate)}</td>
        <td>${s.settled_count}</td>
        <td>${s.pending_count}</td>
      </tr>`;
    })
    .join("");
  return `
    <h3 class="sub-heading">Strategy comparison <span class="hint">(several strategies run side by side against the same signals — the only honest way to tell which actually works is real settled bets accumulating over weeks, not a quick tuning pass)</span></h3>
    <div class="empty-state strategy-caveat">ROI here includes stake already committed to bets still pending (not yet won or lost) — a big negative number with few settled bets usually means "money deployed," not "money lost." Judge a strategy by its win rate and settled count, not ROI, until settled_count is well into the tens.</div>
    <table class="calibration-table">
      <thead><tr><th>Strategy</th><th>Balance</th><th>ROI</th><th>Win rate</th><th># settled</th><th># pending</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function refreshPaperTrading() {
  const el = document.getElementById("paper-body");
  try {
    const resp = await fetch("/api/paper");
    const data = await resp.json();
    latestPaperSummary = data;
    const total = data.total_pnl;
    const stats = `
      <div class="stat-tiles">
        <div class="stat-tile"><div class="stat-value ${data.balance >= data.starting_balance ? "pnl-pos" : "pnl-neg"}">$${data.balance.toFixed(2)}</div><div class="stat-label">Current balance</div></div>
        <div class="stat-tile"><div class="stat-value ${total >= 0 ? "pnl-pos" : "pnl-neg"}">${money(total)}</div><div class="stat-label">Realized P/L</div></div>
        <div class="stat-tile"><div class="stat-value">${data.win_rate === null ? "—" : pct(data.win_rate)}</div><div class="stat-label">Win rate (${data.settled_count} settled)</div></div>
        <div class="stat-tile"><div class="stat-value">${data.pending_count}</div><div class="stat-label">Pending bets</div></div>
      </div>`;
    el.innerHTML = `
      ${stats}
      <div class="pnl-chart-wrap">${buildBalanceChart(data.history)}</div>
      ${buildStrategyComparisonTable(data.strategy_comparison)}
      <h3 class="sub-heading">Calibration — predicted confidence vs. actual win rate</h3>
      ${buildCalibrationTable(data.calibration)}
      <h3 class="sub-heading">Bet type performance <span class="hint">(paused after 10+ settled bets net-losing — frees that stake for bet types that are actually working)</span></h3>
      ${buildSegmentTable(data.segment_performance || [])}
      <h3 class="sub-heading">Bet history</h3>
      ${buildBetHistory(data.bets)}`;
  } catch (err) {
    el.innerHTML = '<div class="empty-state">Couldn\'t load paper trading data.</div>';
  }
}

async function refreshScan() {
  const leagues = selectedLeagues();
  const params = leagues.length ? `?leagues=${leagues.join(",")}` : "";
  try {
    const resp = await fetch(`/api/scan${params}`);
    const data = await resp.json();
    if (data.error) {
      document.getElementById("last-updated").textContent = data.error;
      await refreshPaperTrading();
      refreshUsAccount();
      return;
    }

    renderOppList("arb-list", data.arbitrage_opportunities || [], arbCard);
    if (data.odds_configured) renderOppList("value-list", data.value_opportunities || [], valueCard);

    const tracked = (data.games || []).filter((g) => g.position_tracker);
    setSectionVisible("tracker-panel", "nav-tracker", tracked.length > 0);
    if (tracked.length) renderOppList("tracker-list", tracked, trackerCard);

    const underdogs = data.underdog_opportunities || [];
    setSectionVisible("underdog-panel", "nav-underdog", underdogs.length > 0);
    if (underdogs.length) renderOppList("underdog-list", underdogs, underdogCard);

    const locks = data.lock_opportunities || [];
    setSectionVisible("lock-panel", "nav-lock", locks.length > 0);
    if (locks.length) renderOppList("lock-list", locks, lockCard);

    const props = data.prop_opportunities || [];
    setSectionVisible("prop-panel", "nav-prop", props.length > 0);
    if (props.length) renderOppList("prop-list", props, propCard);

    latestGames = data.games || [];
    renderFilteredGames();

    document.getElementById("last-updated").textContent = data.scan_computed_at
      ? `data as of ${fmtTime(data.scan_computed_at)} (updated hourly)`
      : `updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    document.getElementById("last-updated").textContent = "update failed, retrying…";
  }
  await refreshPaperTrading();
  refreshUsAccount();
}

function scheduleRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  if (!liveOn) return;
  refreshTimer = setInterval(() => {
    if (!document.hidden) refreshScan();
  }, REFRESH_MS);
}

function buildCapitalFlowChart(capital) {
  if (!capital || !capital.points || capital.points.length < 2) {
    return '<div class="empty-state">Not enough deposit/withdrawal history yet to chart.</div>';
  }
  const points = capital.points;
  const w = 680;
  const h = 170;
  const pad = 30;
  const ys = points.map((p) => p.net_capital);
  const yMin = Math.min(...ys, 0);
  const yMax = Math.max(...ys, 0);
  const xScale = (i) => pad + (i / (points.length - 1)) * (w - 2 * pad);
  const yScale = (v) => (yMax === yMin ? h / 2 : h - pad - ((v - yMin) / (yMax - yMin)) * (h - 2 * pad));

  const path = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${xScale(i).toFixed(1)},${yScale(p.net_capital).toFixed(1)}`)
    .join(" ");
  const zeroY = yScale(0).toFixed(1);
  const last = points[points.length - 1];
  const lineColor = last.net_capital >= 0 ? "#2ee6a6" : "#ff5c72";
  const actualBalance = last.current_balance ?? 0;
  const returnLine =
    capital.overall_return_pct !== null && capital.overall_return_pct !== undefined
      ? ` · Overall return: <strong class="${capital.overall_return_pct >= 0 ? "pnl-pos" : "pnl-neg"}">${capital.overall_return_pct >= 0 ? "+" : ""}${capital.overall_return_pct}%</strong>`
      : "";

  return `
    <svg viewBox="0 0 ${w} ${h}" class="pnl-chart" preserveAspectRatio="none">
      <line x1="${pad}" y1="${zeroY}" x2="${w - pad}" y2="${zeroY}" class="zero-line" />
      <path d="${path}" fill="none" stroke="${lineColor}" stroke-width="2" vector-effect="non-scaling-stroke" />
    </svg>
    <div class="chart-axis-labels"><span>${fmtTime(points[0].timestamp)}</span><span>Now</span></div>
    <div class="chart-caption">
      Net capital deposited minus withdrawn: <strong>$${capital.total_net_capital.toFixed(2)}</strong> ·
      Actual balance today: <strong>$${Number(actualBalance).toFixed(2)}</strong>${returnLine}.
      This tracks capital flow, not trade-by-trade portfolio value — polymarket.us's activity log
      doesn't reliably label which side of a matched trade is yours, so trade cash flow isn't
      plotted here rather than risk guessing the sign wrong on real money. It can legitimately dip
      below zero if you've ever withdrawn more than you'd deposited, funded by real trading gains.
    </div>`;
}

function buildComparisonChart(capital, paperSummary) {
  if (!paperSummary || !paperSummary.history || paperSummary.history.length < 2) {
    return '<div class="empty-state">Not enough paper-trading history yet to compare.</div>';
  }
  if (!capital || capital.overall_return_pct === null || capital.overall_return_pct === undefined) {
    return '<div class="empty-state">Not enough real-account deposit history to compute a comparable return.</div>';
  }

  const paperPoints = paperSummary.history.map(
    (p) => (p.balance / paperSummary.starting_balance - 1) * 100
  );
  const w = 680;
  const h = 170;
  const pad = 30;
  const allVals = [...paperPoints, capital.overall_return_pct, 0];
  const yMin = Math.min(...allVals);
  const yMax = Math.max(...allVals);
  const xScale = (i) => pad + (i / (paperPoints.length - 1)) * (w - 2 * pad);
  const yScale = (v) => (yMax === yMin ? h / 2 : h - pad - ((v - yMin) / (yMax - yMin)) * (h - 2 * pad));

  const path = paperPoints
    .map((v, i) => `${i === 0 ? "M" : "L"}${xScale(i).toFixed(1)},${yScale(v).toFixed(1)}`)
    .join(" ");
  const zeroY = yScale(0).toFixed(1);
  const refY = yScale(capital.overall_return_pct).toFixed(1);
  const lastPaper = paperPoints[paperPoints.length - 1];

  return `
    <svg viewBox="0 0 ${w} ${h}" class="pnl-chart" preserveAspectRatio="none">
      <line x1="${pad}" y1="${zeroY}" x2="${w - pad}" y2="${zeroY}" class="zero-line" />
      <line x1="${pad}" y1="${refY}" x2="${w - pad}" y2="${refY}" stroke="#a78bfa" stroke-width="1.5" stroke-dasharray="5 4" vector-effect="non-scaling-stroke" />
      <path d="${path}" fill="none" stroke="${lastPaper >= 0 ? "#2ee6a6" : "#ff5c72"}" stroke-width="2" vector-effect="non-scaling-stroke" />
    </svg>
    <div class="chart-axis-labels">
      <span>Paper trading: ${lastPaper >= 0 ? "+" : ""}${lastPaper.toFixed(1)}%</span>
      <span>Your real account: ${capital.overall_return_pct >= 0 ? "+" : ""}${capital.overall_return_pct}% (dashed)</span>
    </div>
    <div class="chart-caption">
      Both shown as % return on capital so they're comparable despite very different bankroll sizes
      ($100 simulated vs. $${capital.total_net_capital.toFixed(2)} real net deposits). The dashed
      line is your real account's overall return as of today — a single number, not a day-by-day
      curve (see the capital-flow chart above for why).
    </div>`;
}

function renderUsAccount(data) {
  const el = document.getElementById("us-account-body");

  if (!data.configured) {
    setSectionVisible("us-account-panel", "nav-us-account", false); // no keys set — don't show an empty/broken section
    return;
  }
  setSectionVisible("us-account-panel", "nav-us-account", true);

  if (data.error) {
    el.innerHTML = `<div class="empty-state">${data.error}</div>`;
    return;
  }

  const balances = data.balances || [];
  const positions = data.positions || {};
  const positionEntries = Object.entries(positions);

  const balanceTiles = balances
    .map(
      (b) => `
      <div class="stat-tile">
        <div class="stat-value">$${Number(b.currentBalance ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}</div>
        <div class="stat-label">${b.currency || "Balance"}</div>
      </div>
      <div class="stat-tile">
        <div class="stat-value">$${Number(b.buyingPower ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}</div>
        <div class="stat-label">Buying power</div>
      </div>`
    )
    .join("");

  let positionsHtml;
  if (!positionEntries.length) {
    positionsHtml = '<div class="empty-state">No open positions.</div>';
  } else {
    const rows = positionEntries
      .map(([slug, p]) => {
        // Exact field names aren't nailed down from docs yet — show the
        // common ones if present, raw JSON otherwise so nothing's hidden.
        const known = ["outcome", "size", "avgPrice", "currentValue", "unrealizedPnl", "realizedPnl"];
        const hasKnown = known.some((k) => p[k] !== undefined);
        if (hasKnown) {
          const pnl = p.unrealizedPnl ?? p.realizedPnl;
          const pnlClass = pnl > 0 ? "pnl-pos" : pnl < 0 ? "pnl-neg" : "";
          return `
          <tr>
            <td>${slug}</td>
            <td>${p.outcome ?? "—"}</td>
            <td>${p.size ?? "—"}</td>
            <td>${p.avgPrice !== undefined ? "$" + Number(p.avgPrice).toFixed(3) : "—"}</td>
            <td>${p.currentValue !== undefined ? "$" + Number(p.currentValue).toFixed(2) : "—"}</td>
            <td class="${pnlClass}">${pnl !== undefined ? money(pnl) : "—"}</td>
          </tr>`;
        }
        return `<tr><td>${slug}</td><td colspan="5"><code>${JSON.stringify(p)}</code></td></tr>`;
      })
      .join("");
    positionsHtml = `
      <table>
        <thead><tr><th>Market</th><th>Side</th><th>Size</th><th>Avg price</th><th>Current value</th><th>P/L</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  el.innerHTML = `
    <div class="stat-tiles">${balanceTiles}</div>
    ${positionsHtml}
    <h3 class="sub-heading">Capital flow over time</h3>
    <div class="pnl-chart-wrap">${buildCapitalFlowChart(data.capital_history)}</div>
    <h3 class="sub-heading">Real account vs. paper trading — % return comparison</h3>
    <div class="pnl-chart-wrap">${buildComparisonChart(data.capital_history, latestPaperSummary)}</div>`;
}

async function refreshUsAccount() {
  try {
    const resp = await fetch("/api/us-account");
    const data = await resp.json();
    renderUsAccount(data);
  } catch (err) {
    setSectionVisible("us-account-panel", "nav-us-account", true);
    document.getElementById("us-account-body").innerHTML =
      '<div class="empty-state">Couldn\'t reach the account API.</div>';
  }
}

function money(v) {
  const n = Number(v);
  const sign = n >= 0 ? "+" : "-";
  return `${sign}$${Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function buildStatTiles(history, portfolioValue) {
  const realized = history.length ? history[history.length - 1].cumulative_pnl : 0;
  const openValue = portfolioValue || 0;
  const total = realized + openValue;
  const tile = (value, label, signed) => `
    <div class="stat-tile">
      <div class="stat-value ${signed ? (value >= 0 ? "pnl-pos" : "pnl-neg") : ""}">${signed ? money(value) : `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`}</div>
      <div class="stat-label">${label}</div>
    </div>`;
  return `
    <div class="stat-tiles">
      ${tile(total, "Est. all-time P/L", true)}
      ${tile(realized, "Realized cash flow", true)}
      ${tile(openValue, "Open positions value", false)}
    </div>`;
}

function buildPnlChart(history) {
  if (!history.length) {
    return '<div class="empty-state">No trade history found for this address yet.</div>';
  }
  const w = 680;
  const h = 170;
  const pad = 30;
  const xs = history.map((p) => p.timestamp);
  const ys = history.map((p) => p.cumulative_pnl);
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const yMin = Math.min(0, ...ys);
  const yMax = Math.max(0, ...ys);
  const xScale = (t) => (xMax === xMin ? pad : pad + ((t - xMin) / (xMax - xMin)) * (w - 2 * pad));
  const yScale = (v) => (yMax === yMin ? h / 2 : h - pad - ((v - yMin) / (yMax - yMin)) * (h - 2 * pad));

  const path = history
    .map((p, i) => `${i === 0 ? "M" : "L"}${xScale(p.timestamp).toFixed(1)},${yScale(p.cumulative_pnl).toFixed(1)}`)
    .join(" ");
  const zeroY = yScale(0).toFixed(1);
  const last = history[history.length - 1];
  const lineColor = last.cumulative_pnl >= 0 ? "#2ee6a6" : "#ff5c72";
  const firstDate = fmtTime(history[0].date);
  const lastDate = fmtTime(last.date);

  return `
    <svg viewBox="0 0 ${w} ${h}" class="pnl-chart" preserveAspectRatio="none">
      <line x1="${pad}" y1="${zeroY}" x2="${w - pad}" y2="${zeroY}" class="zero-line" />
      <path d="${path}" fill="none" stroke="${lineColor}" stroke-width="2" vector-effect="non-scaling-stroke" />
    </svg>
    <div class="chart-axis-labels"><span>${firstDate}</span><span>${lastDate}</span></div>
    <div class="chart-caption">Cumulative realized cash flow from ${history.length.toLocaleString()} trades/redemptions. Open (unresolved) bets show as a temporary dip until they settle — see "Open positions value" above for what's still in play.</div>`;
}

function buildPositionBars(positions) {
  if (!positions.length) return "";
  const maxAbs = Math.max(1, ...positions.map((p) => Math.abs(p.cashPnl)));
  const sorted = [...positions].sort((a, b) => b.cashPnl - a.cashPnl);
  const rows = sorted
    .map((p) => {
      const isPos = p.cashPnl >= 0;
      const widthPct = Math.min(100, (Math.abs(p.cashPnl) / maxAbs) * 100);
      return `
      <div class="pos-bar-row">
        <div class="pos-bar-label" title="${p.title || p.slug}">${p.title || p.slug} <span class="pos-bar-outcome">${p.outcome}</span></div>
        <div class="pos-bar-track"><div class="pos-bar-fill ${isPos ? "pos" : "neg"}" style="width:${widthPct}%"></div></div>
        <div class="pos-bar-value ${isPos ? "pnl-pos" : "pnl-neg"}">${money(p.cashPnl)}</div>
      </div>`;
    })
    .join("");
  return `<div class="pos-bars">${rows}</div>`;
}

function renderPositions(data) {
  const el = document.getElementById("positions-body");
  if (data.error) {
    el.innerHTML = `<div class="empty-state">${data.error}</div>`;
    return;
  }
  const positions = data.positions || [];
  const history = data.pnl_history || [];

  const statsHtml = buildStatTiles(history, data.portfolio_value);
  const chartHtml = `<div class="pnl-chart-wrap">${buildPnlChart(history)}</div>`;

  if (!positions.length) {
    el.innerHTML = `${statsHtml}${chartHtml}<div class="empty-state">No open positions for this address.</div>`;
    return;
  }

  const barsHtml = buildPositionBars(positions);
  const rows = positions
    .map((p) => {
      const pnlClass = p.cashPnl >= 0 ? "pnl-pos" : "pnl-neg";
      return `
      <tr>
        <td>${p.title || p.slug}</td>
        <td>${p.outcome}</td>
        <td>${Number(p.size).toLocaleString()}</td>
        <td>$${Number(p.avgPrice).toFixed(3)}</td>
        <td>$${Number(p.currentValue).toFixed(2)}</td>
        <td class="${pnlClass}">${money(p.cashPnl)}</td>
      </tr>`;
    })
    .join("");

  el.innerHTML = `
    ${statsHtml}
    ${chartHtml}
    <h3 class="sub-heading">P/L by position</h3>
    ${barsHtml}
    <table>
      <thead><tr><th>Market</th><th>Side</th><th>Shares</th><th>Avg price</th><th>Current value</th><th>P&L</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function loadPositions(address) {
  const el = document.getElementById("positions-body");
  el.innerHTML = "Loading…";
  try {
    const resp = await fetch(`/api/positions?address=${encodeURIComponent(address)}`);
    const data = await resp.json();
    renderPositions(data);
  } catch {
    el.innerHTML = '<div class="empty-state">Couldn\'t reach Polymarket right now.</div>';
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("league-filters").addEventListener("change", refreshScan);

  const searchInput = document.getElementById("matchup-search");
  searchInput.addEventListener("input", renderFilteredGames);
  searchInput.addEventListener("focus", () => {
    // jump to the results so typing from the header isn't searching blind
    document.getElementById("games-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  });

  document.getElementById("live-only-filter").addEventListener("change", renderFilteredGames);

  const liveToggle = document.getElementById("live-toggle");
  liveToggle.addEventListener("change", () => {
    liveOn = liveToggle.checked;
    scheduleRefresh();
  });

  const walletInput = document.getElementById("wallet-address");
  const savedAddress = localStorage.getItem(WALLET_KEY);
  if (savedAddress) {
    walletInput.value = savedAddress;
    loadPositions(savedAddress);
  }

  document.getElementById("wallet-save").addEventListener("click", () => {
    const address = walletInput.value.trim();
    if (!address) return;
    localStorage.setItem(WALLET_KEY, address);
    loadPositions(address);
  });

  refreshScan();
  refreshUsAccount();
  scheduleRefresh();
});
