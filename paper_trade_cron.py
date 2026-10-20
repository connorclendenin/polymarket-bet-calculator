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
    rows = app_module._run_scan(None)  # all leagues, same as the live app's paper trader
    _, _, underdog_rows, lock_rows, prop_rows = app_module._ranked_signal_lists(rows)
    placed = paper_trading.run_all_strategies(underdog_rows, lock_rows, prop_rows)

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
