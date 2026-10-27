"""Standalone entrypoint for one scan + paper-trading cycle, no Flask server
needed — meant to be invoked on a schedule (GitHub Actions) so paper trading
keeps advancing even when the app isn't open anywhere. Reuses the exact same
app._run_scan / paper_trading.run_all_strategies logic the live app uses;
this file only drives that logic from a cron context instead of an HTTP
request.
"""

from dotenv import load_dotenv

load_dotenv()

import app as app_module
import paper_trading


def main():
    rows = app_module._run_scan(None)  # all leagues — the deployed dashboard
    # (via cache_scan_snapshot below) still shows every sport; only paper
    # trading itself is scoped narrower, just below.
    app_module.cache_scan_snapshot(rows)  # lets a deployed /api/scan read this instead of
    # recomputing live (would time out a serverless function) — see app.py's api_scan.

    paper_rows = paper_trading._restrict_to_paper_trading_leagues(rows)
    _, _, underdog_rows, lock_rows, _ = app_module._ranked_signal_lists(paper_rows)
    placed = paper_trading.run_all_strategies(paper_rows, underdog_rows, lock_rows)

    total_placed = sum(len(bets) for bets in placed.values())
    print(f"scanned {len(rows)} games, placed {total_placed} new bet(s)")
    for strategy_id, bets in placed.items():
        if bets:
            print(f"  {strategy_id}: {len(bets)} new bet(s)")

    for c in paper_trading.get_strategy_comparison():
        roi = "—" if c["roi_pct"] is None else f"{c['roi_pct']}%"
        print(
            f"  {c['strategy_id']}: balance=${c['balance']:.2f} roi={roi} "
            f"settled={c['settled_count']} pending={c['pending_count']}"
        )


if __name__ == "__main__":
    main()
