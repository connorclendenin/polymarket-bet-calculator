# Polymarket Bet Calculator

Local dashboard that scans upcoming sports games on Polymarket for pricing
signals: risk-free arbitrage (both outcomes of a game summing to under $1)
and, optionally, mispricing vs. a sportsbook-odds consensus. Read-only —
it never holds a private key and never places a trade. Every opportunity
links out to the real market on polymarket.com for you to act on manually.

Leagues covered: NFL, NBA, MLB, NHL, CFB, CBB.

## Run

```
pip install -r requirements.txt
python3 app.py
```

Open http://127.0.0.1:5007

## Optional: sportsbook comparison

Copy `.env.example` to `.env` and set `ODDS_API_KEY` (free tier at
https://the-odds-api.com). Without it, the arbitrage scanner still works —
only the "value vs. sportsbooks" panel is disabled.

## My polymarket.us account (read-only, your own API key)

A separate panel from the one below — **polymarket.us is a different product/backend from
the original Polymarket**, with no shared wallet or address space, so it needed its own
integration. Set these in `.env` (generate/rotate at `polymarket.us/developer`, never paste
the values anywhere but `.env`):

```
POLYMARKET_KEY_ID=
POLYMARKET_SECRET_KEY=
```

`us_api.py` talks to `api.polymarket.us` directly over plain `requests` with Ed25519-signed
headers (see docs.polymarket.us/api-reference/authentication) — no third-party SDK dependency,
just the well-established `cryptography` package (`pip install cryptography` in the venv).
The panel stays hidden until both env vars are set.

## Positions & P/L lookup — any public polymarket.com address (read-only)

Paste your public Polymarket wallet address (not a private key — this is
public on-chain data, the same thing anyone can look up from your profile)
into the "Your positions & P/L" box. Nothing else to connect — no OAuth, no
API key, no wallet signer. The address is only ever stored in your own
browser's localStorage; the server never persists it.

This shows:
- **Stat tiles**: realized cash flow, current open-positions value, and an
  estimated all-time P/L (the two added together).
- **A P/L-over-time chart**, reconstructed from your full trade/redemption
  history (Polymarket has no dedicated P/L-history endpoint, so this is
  built from the same activity log the site itself uses). It's a running
  total of cash out (buys) and cash in (sells, redemptions, maker/taker
  rebates) — i.e. **realized** cash flow. An open, unresolved position
  shows as a dip in the line until it settles; that's not a loss, just
  money not back yet. Add the "open positions value" tile to the latest
  point for the full picture, which is exactly what the "Est. all-time
  P/L" tile already does.
- **P/L by position**: a bar per open position, from the same data as the
  table below it.

First load for an active account can take a few seconds (it pages through
up to 3,000 activity entries); results are cached 5 minutes so reloading
right after is instant.

## How the numbers work

- **Arbitrage**: for each game's moneyline market, sums the current best
  ask across every outcome. If that sum is under $1.00 (by at least 1 cent,
  configurable in `app.py`), buying one share of every outcome locks in a
  $1.00 payout for less than $1.00 — a risk-free edge before Polymarket's
  trading fees and gas. Fees aren't netted out here, so treat small edges
  as a flag to double-check manually, not a guaranteed profit.
- **Value vs. sportsbooks**: de-vigs each tracked sportsbook's moneyline
  odds into an implied win probability per team, averages across books,
  and flags when Polymarket's ask price for that team is well below that
  consensus (default threshold: 4 points). Games are matched between
  Polymarket and the odds feed by team name + kickoff time, since the two
  sources share no common id.
- **Model confidence**: a stats-based win probability per team, independent
  of Polymarket's price, built from real season data pulled live from
  ESPN's public scoreboard/schedule API (NFL/NBA/MLB/NHL only — CFB/CBB
  fall back to Polymarket's own season-record field, no deeper stats).
  Blends season win%, home/road split, and last-10-game form (50/30/20%)
  via the log5 method (a standard sabermetric formula), then averages that
  with a scoring-margin-based estimate. Every card's "Why?" toggle shows
  the underlying numbers for both teams so the percentage is never a black
  box. This is a transparent heuristic, not a trained ML model — treat it
  as one more data point, not a certainty.
- **Locks**: the flip side of the underdog signal — favorites the model is
  independently very confident in (80%+) whose price is still in the
  $0.55-$0.90 range (payout roughly 1.1x-1.8x), rather than already bid up
  near $1 where there'd be nothing left to gain. This isn't about the model
  disagreeing with the market like the underdog/value signals are; it's
  model and market agreeing a team is a strong favorite while the price
  still pays something worthwhile. Same live/pre-game basis as the
  underdog signal. Early in a season, small sample sizes can push this
  toward overconfident numbers (a 5-0 team's win% is extreme but noisy) —
  worth weighing more in December than in the first weeks of a season.
- **Live underdog signal**: for games in progress, blends each team's
  pregame model confidence with the current score and how much of the game
  remains (heavier weight on the score as the clock runs down) to produce
  a live win-probability estimate. When the trailing/underdog team's live
  estimate sits well above its current Polymarket ask price (7+ points),
  it's surfaced in the "Live underdog value" panel. This in-game formula is
  a simplified approximation (no play-by-play data), not a calibrated win-
  probability model — useful as a prompt to look closer, not a signal to
  bet on alone.

None of these signals are investment advice — they're mechanical
calculations you should sanity-check (injuries, bullpen/rotation news,
liquidity, fees) before betting, and no formula here is tuned or proven to
maximize returns.

## Paper trading — model accuracy test

Three independent simulated $100 bankrolls — `underdogs` (only "Smart
underdog" signals), `locks` (only "Lock" signals), and `combined` (both
signal types, with stake sizing that *learns from this account's own
settled history* — see below) — that automatically bet on this app's own
signals, settle each bet against the real Polymarket market resolution,
and report P/L plus a **calibration table**: for bets the model called
"80-90% confidence," did they actually win 80-90% of the time? That's the
real test of whether the confidence percentages mean anything. No real
money, no real orders — purely reading public market data.

When more eligible signals exist than an account can act on (the per-league
pending cap), the games that actually get bet on are the ones starting
soonest (`paper_trading._soonest_first`) — results come back fast instead
of sitting on far-future games.

**How `combined` and `locks` learn from losses:** every account already
pauses a (signal_type, league) segment outright once it's lost money
across 10+ of its own settled bets (`_segment_performance`'s `paused`
flag — a real pattern, not one bad beat). `combined` and `locks` go
further: below that pause threshold, stake for a segment scales
continuously with that segment's own win rate so far (`_adaptive_stake`)
— a segment that's been winning gets up to 1.75x the flat stake, one
that's been losing gets as low as 0.4x, before ever reaching the hard
pause. `underdogs` stays flat-staked on purpose, as a control the other
two are actually being compared against.

**`locks`'s live dip-buying:** a lock-caliber team is still bet
immediately when it first qualifies pregame — unchanged default behavior.
But once a game goes live, `locks` holds off and only bets once the price
has dropped at least 3 cents (`LOCKS_LIVE_DIP_THRESHOLD`) from the
earliest price ever observed for that exact outcome
(`update_price_watch`/`_locks_live_entry_gate`) — the same team, cheaper
than it opened, for a better payout at the same win probability. A
lightweight price-watch cache (`data/locks_price_watch.json` locally,
`polymarket-bet-calculator:locks_price_watch` on Redis) tracks the
earliest price for every outcome in every paper-trading-league game, not
just ones that are already locks, so there's a real "before" price to
compare against even for a team that only becomes a lock once live.
`underdogs` and `combined` are unaffected — they still bet the moment a
signal qualifies, live or not.

**Runs two ways:** while the app is open (page polls `/api/paper` every
30s), and independently via a scheduled GitHub Actions workflow
(`.github/workflows/paper-trading-cron.yml`, runs every 5 minutes — GitHub's
shortest supported interval) so it keeps advancing even with the laptop
closed or off — both paths call the exact same `paper_trading.run_all_strategies`,
are idempotent, and write to the same state files, so it's safe for both to
run at once.

To turn on the scheduled run: push this repo to GitHub, then add these as
encrypted repo secrets (Settings → Secrets and variables → Actions → New
repository secret) with the same values as your local `.env`:
- `POLYMARKET_KEY_ID`
- `POLYMARKET_SECRET_KEY`
- `ODDS_API_KEY` (optional — only if you have one)

**This repo is public** (deliberately, per Connor's own choice): GitHub
Actions minutes are unlimited/free on public repos at any frequency, unlike
a private repo's ~2,000 min/month free tier, which 5-minute cadence would
blow through fast (every run is a genuinely cold scan, no warm cache
carries over between runs — real compute, not a cheap no-op). Nothing
secret has ever lived in this repo's history (verified before flipping
visibility) — real credentials only ever exist as GitHub/Vercel encrypted
env vars, never committed. You can also trigger a run manually anytime
from the repo's Actions tab ("Run workflow") instead of waiting.

The three accounts (see `paper_trading.py`'s `STRATEGIES`) run side by
side. Bets are placed from an all-leagues scan (independent of whatever
league filter you have checked in the UI), then filtered down to
`paper_trading.PAPER_TRADING_LEAGUES` (NFL/CFB/NBA/CBB — Connor's own
predominant sports) before any paper-trading signal is derived — the rest
of the app (Smart bets, Games grid, etc.) still covers every league; only
new paper-trading placements are scoped narrower. Existing pending bets
already placed in other leagues before this scoping aren't cancelled, they
just settle out normally over time. State lives in
`data/paper_trades_<strategy_id>.json`; delete one (and its matching
`polymarket-bet-calculator:paper_trades:<strategy_id>` Redis key, if
deployed) to reset that account's bankroll back to $100.

## Deploy so you can view it from anywhere

Not deployed yet by default — this runs the real `.us` API scan which is
too expensive (hundreds of games, ~50s cold) to recompute on every page
load on a serverless host, so deploying isn't just "point Vercel at it"
like the sibling apps. The architecture: the GitHub Actions cron
(`.github/workflows/paper-trading-cron.yml`, running every 5 minutes — see
above) does the expensive scan and writes the result plus paper-trading
state to Upstash Redis; the deployed dashboard only ever *reads* that
cache (fast, well inside a serverless function's time limit) rather than
recomputing it. **This means the deployed dashboard is only as fresh as
the last cron run** — up to ~5 minutes behind, not truly
live/second-by-second like running it locally, but close. The "Live
tracker" and "Smart" sections will feel slightly less real-time once
deployed; that's the actual tradeoff of making this reachable without your
computer on, not a bug.

To set it up:
1. Add `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` as secrets on
   this GitHub repo (Settings → Secrets and variables → Actions) — reusing
   the same free Upstash database as `portfolio-dashboard`/
   `financial-growth-analyzer` is fine, this project namespaces its own
   keys under `polymarket-bet-calculator:`. The next cron run (or a manual
   "Run workflow" from the Actions tab) starts populating the cache.
2. Import this GitHub repo into Vercel (auto-detects `vercel.json`, no
   config needed) — same `altos` team as the other apps keeps billing/auth
   in one place, but any account works.
3. Add the same two Upstash env vars to the Vercel project (Settings →
   Environment Variables). `POLYMARKET_KEY_ID`/`POLYMARKET_SECRET_KEY` are
   only needed there too if you want the "My account" panel to work on the
   deployed version — paper trading itself never needs them.
4. **No password gate is configured** — the deployment URL itself is what
   keeps this private, so don't share it. It shows your real balance and
   positions (read-only; this app never places real orders either way).
   By default a new Vercel project under a team enables "Require Log In"
   under Settings → Deployment Protection, which would actually block
   anyone without a login to that Vercel team, including your own phone —
   turn that **off** to get the plain unlisted-URL behavior described here
   (same toggle noted in `portfolio-dashboard`'s README).

If the Upstash env vars aren't set, both the local app and
`paper_trade_cron.py` fall back to local `data/*.json` files exactly as
before — nothing about local-only usage changes.

## Architecture

- `polymarket.py` — Gamma API for game schedule/metadata, CLOB `/books`
  (batched) for live best bid/ask per outcome, data-api for positions.
- `oddsapi.py` — The Odds API client + no-vig probability math.
- `stats.py` — ESPN public API client (teams/schedule/scoreboard), the
  log5 + scoring-margin confidence model, and the live win-probability
  heuristic.
- `confidence.py` — wires `stats.py` into each game row: pregame model
  confidence, live estimate, and the underdog-value signal.
- `matcher.py` — fuzzy team-name + time-window matching between Polymarket
  and the odds-API data (separate from `stats.py`'s own ESPN team matching).
- `performance.py` — reconstructs the realized-P/L time series from a
  wallet's Polymarket activity feed (trades, redemptions, rebates).
- `paper_trading.py` — three independent simulated bankrolls (`STRATEGIES`),
  each placing/settling/reporting on the underdog/lock signals under its own
  rules. `polymarket.fetch_event_resolution` reads the real winner once
  Polymarket closes a market.
- `store.py` — Redis-or-local-file persistence, shared by `paper_trading.py`
  (bankroll state) and `app.py` (cached scan results) — see "Deploy" above.
- `paper_trade_cron.py` — standalone entrypoint (no Flask) for the scheduled
  job: one scan + paper-trading cycle, no HTTP request involved.
- `app.py` — Flask routes (`/api/scan`, `/api/positions`, `/api/paper`) +
  dashboard.

Port 5007, registered in `~/Downloads/.claude/launch.json` as
`polymarket-bet-calculator`.
