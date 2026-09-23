"""Verify SafeTrade connectivity: public depth + authenticated balances."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import load_config
from safetrade import SafeTradeClient


def main():
    cfg = load_config()
    st = SafeTradeClient(cfg.safetrade_api_key, cfg.safetrade_api_secret, cfg.safetrade_base_url)

    print(f"[public] depth {cfg.safetrade_market}")
    try:
        d = st.depth(cfg.safetrade_market, limit=5)
        print(f"  bids: {d.get('bids', [])[:3]}")
        print(f"  asks: {d.get('asks', [])[:3]}")
        bid, ask = st.best_bid_ask(cfg.safetrade_market)
        spread_bps = (ask - bid) / bid * 10000 if bid else 0
        print(f"  bid={bid} ask={ask} spread={float(spread_bps):.1f}bps")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        return 1

    print("\n[auth] balances")
    if not cfg.safetrade_api_key:
        print("  SKIP — no API key in .env")
        return 0
    try:
        bals = st.balances()
        for b in bals:
            bal = float(b.get("balance", 0))
            lock = float(b.get("locked", 0))
            if bal > 0 or lock > 0:
                print(f"  {b['currency']:>8}: bal={bal:.6f} locked={lock:.6f}")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        print("  Hint: check API key, nonce drift, or IP whitelist")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
