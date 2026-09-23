"""One-off: approve the Uniswap router to spend WPRL and the quote token from the trading wallet.

Run once per fresh wallet BEFORE starting the trader (the trader refuses to start with a zero
allowance, because the CEX leg would fill and the DEX leg would revert). Honours DRY_RUN.
Usage: .venv/bin/python scripts/approve_router.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import load_config
from dex import DexClient

ALLOWANCE_FLOOR = 10**30   # same floor the trader checks at startup


def main():
    cfg = load_config()
    dex = DexClient(cfg)
    print(f"wallet {dex.address}  router {cfg.router_address}  mode {'DRY RUN' if cfg.dry_run else 'LIVE'}")
    for addr, name in ((cfg.wprl_address, "WPRL"), (cfg.quote_token_address, cfg.quote_token_symbol)):
        cur = dex.allowance(addr)
        if cur >= ALLOWANCE_FLOOR:
            print(f"  {name}: allowance already unlimited ({cur}) -> skip")
            continue
        res = dex.approve_max(addr, dry_run=cfg.dry_run)   # grants an UNLIMITED allowance to the router
        print(f"  {name}: {res}")
        if not cfg.dry_run:
            after = dex.allowance(addr)
            print(f"  {name}: allowance now {after} -> {'OK' if after >= ALLOWANCE_FLOOR else 'NOT applied, check the tx'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
