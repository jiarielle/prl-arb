"""Session/window PnL report from the append-only trade ledger (logs/trades.jsonl).

Usage:
  python scripts/report.py [N]     report the last N trades (default: 12)
  python scripts/report.py --since HH:MM   report trades since a local time today
  python scripts/report.py --all   whole ledger
  python scripts/report.py --pnl   TRUE PnL: all assets (incl in-flight) vs baseline
"""
import sys, json
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
LEDGER = ROOT / "logs" / "trades.jsonl"
BASELINE = ROOT / "baseline.json"


def true_pnl():
    """Full-asset reconciliation: (all balances + all in-flight) - baseline.
    The reliable number — counts money on the bridge/withdrawals too, and strips out
    price moves by valuing token deltas at current spot."""
    sys.path.insert(0, str(ROOT))
    from config import load_config
    from safetrade import SafeTradeClient
    from dex import DexClient
    import arb
    cfg = load_config()
    st = SafeTradeClient(cfg.safetrade_api_key, cfg.safetrade_api_secret, cfg.safetrade_base_url)
    dex = DexClient(cfg)
    bl = json.load(open(BASELINE))
    start_tok, start_usd = float(bl["start_token"]), float(bl["start_usdt"])

    cp, cu = float(st.balance("prl")), float(st.balance("usdt"))
    ew, eu = float(dex.balance(cfg.wprl_address)), float(dex.balance(cfg.quote_token_address))
    spot = float(dex.wprl_price_in_quote())
    tc, te = arb._inflight(cfg, st, dex); tc, te = float(tc), float(te)
    # USDT in-flight
    u2evm = sum(float(w.get("amount") or 0) for w in st.withdraws(currency="usdt", limit=10)
                if w.get("state", w.get("status")) not in ("failed", "rejected", "canceled", "errored")
                and not w.get("completed_at"))
    u2cex = sum(float(d.get("amount") or 0) for d in st.deposits(currency="usdt", limit=10)
                if not d.get("credited"))
    tot_tok = cp + ew + tc + te
    tot_usd = cu + eu + u2evm + u2cex
    d_tok, d_usd = tot_tok - start_tok, tot_usd - start_usd
    # Report PnL as TWO SEPARATE numbers — coin and USDT — with NO price conversion.
    # We hold a token surplus, so any spot-converted "$" figure breathes with PRL price;
    # the user wants the price-free split only.
    print("=== PnL (all assets incl in-flight vs baseline) ===")
    print(f"  现货 CEX: PRL {cp:.0f} USDT {cu:.0f} | EVM: WPRL {ew:.0f} USDT {eu:.0f}")
    print(f"  在途 token ->CEX {tc:.0f} ->EVM {te:.0f} | USDT ->EVM {u2evm:.0f} ->CEX {u2cex:.0f}")
    print(f"  总币   {tot_tok:.1f}  (start {start_tok:.0f})")
    print(f"  总USDT {tot_usd:.1f}  (start {start_usd:.0f})")
    print(f"  >>> 赚了:  币 {d_tok:+.1f} 个  |  U {d_usd:+.1f}")


def main():
    if "--pnl" in sys.argv:
        true_pnl(); return
    if not LEDGER.exists():
        print("no trade ledger yet (logs/trades.jsonl) — starts recording from next fill")
        return
    all_rows = [json.loads(l) for l in open(LEDGER) if l.strip()]
    # Only true trade rows drive PnL. Exposure/flat markers — new exposure_qty rows AND old
    # marker rows that lack realized/dir — are filtered out here and surfaced separately, so
    # report.py can't KeyError precisely when an exposure marker exists.
    rows = [r for r in all_rows if "realized" in r and "dir" in r and "exposure_qty" not in r]
    exposures = [r for r in all_rows if "exposure_qty" in r]
    reverts = [r for r in all_rows if r.get("revert_cost_usd") is not None]
    if not rows and reverts:
        rc = sum(r["revert_cost_usd"] for r in reverts)
        print(f"no completed trades yet; {len(reverts)} reverted leg(s) cost ${rc:+.2f} all-time (gas/bridge fees not included)")
        return
    if not rows:
        print("ledger empty (no trade rows)")
        if exposures:
            print(f"  ⚠ {len(exposures)} unhedged-exposure marker(s) present — see logs/trades.jsonl")
        return

    args = sys.argv[1:]
    sel = rows
    label = ""
    if "--all" in args:
        label = "ALL"
    elif "--since" in args:
        hhmm = args[args.index("--since") + 1]
        today = datetime.now().strftime("%Y-%m-%d")
        # ledger ts is UTC iso; compare loosely by local clock string match is unreliable,
        # so just filter by ts >= the given UTC time today
        sel = [r for r in rows if r["ts"][11:16] >= hhmm]
        label = f"since {hhmm} UTC"
    else:
        n = int(args[0]) if args and args[0].isdigit() else 12
        sel = rows[-n:]
        label = f"last {len(sel)}"

    if not sel:
        print(f"no trades in window ({label})"); return
    if "--all" in args:
        sel_rev = reverts
    elif "--since" in args:
        sel_rev = [r for r in reverts if r["ts"][11:16] >= hhmm]
    else:
        sel_rev = [r for r in reverts if r["ts"] >= sel[0]["ts"]]
    rev_win = sum(r["revert_cost_usd"] for r in sel_rev)

    realized = sum(r["realized"] for r in sel)
    wins = [r for r in sel if r["realized"] > 0]
    losses = [r for r in sel if r["realized"] <= 0]
    by_dir = {}
    for r in sel:
        by_dir.setdefault(r["dir"], {"n": 0, "pnl": 0.0})
        by_dir[r["dir"]]["n"] += 1
        by_dir[r["dir"]]["pnl"] += r["realized"]

    print(f"=== REPORT ({label}) ===")
    print(f"  {sel[0]['ts'][11:19]} -> {sel[-1]['ts'][11:19]} UTC")
    print(f"  trades: {len(sel)}  (wins {len(wins)} / non-win {len(losses)})")
    print(f"  realized PnL: ${realized:+.2f}")
    if sel_rev:
        print(f"  reverted legs in window: {len(sel_rev)}, cost ${rev_win:+.2f}  -> NET ${realized + rev_win:+.2f}")
    print(f"  avg/trade: ${realized/len(sel):+.2f}  best ${max(r['realized'] for r in sel):+.2f}  worst ${min(r['realized'] for r in sel):+.2f}")
    for d, v in by_dir.items():
        print(f"    {d}: {v['n']} trades, ${v['pnl']:+.2f}")
    print(f"  (ledger total: {len(rows)} trades, ${sum(r['realized'] for r in rows):+.2f} all-time)")
    if reverts:
        rc = sum(r["revert_cost_usd"] for r in reverts)
        print(f"  revert cost (DEX leg failed -> CEX leg reversed): {len(reverts)} events, ${rc:+.2f} all-time")
        print(f"  NET all-time incl. reverts: ${sum(r['realized'] for r in rows) + rc:+.2f}  (gas and bridge fees not included)")
    if exposures:
        tot = sum(e.get("exposure_qty", 0) for e in exposures)
        print(f"  ⚠ unhedged-exposure markers: {len(exposures)} (total ~{tot:.2f} token) — see logs/trades.jsonl")


if __name__ == "__main__":
    main()
