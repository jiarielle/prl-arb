"""Verify EVM connectivity: RPC, wallet, pool meta, balances, allowance."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import load_config
from dex import DexClient


def main():
    cfg = load_config()
    dex = DexClient(cfg)

    print(f"[rpc] block: {dex.w3.eth.block_number}")
    print(f"[wallet] address: {dex.address}")
    print(f"  ETH:        {dex.w3.from_wei(dex.w3.eth.get_balance(dex.address), 'ether'):.6f}")
    print(f"  WPRL:       {dex.balance(cfg.wprl_address):.6f}")
    print(f"  {cfg.quote_token_symbol:<11} {dex.balance(cfg.quote_token_address):.6f}")

    if not cfg.pool_address:
        print("\n[pool] WPRL_POOL_ADDRESS not set in .env — skip pool checks")
        print("  Find on DexScreener with WPRL CA, copy the V3 pool address, set fee tier")
        return 0

    meta = dex.pool_meta()
    print(f"\n[pool] {cfg.pool_address}")
    print(f"  token0: {meta['token0']}")
    print(f"  token1: {meta['token1']}")
    print(f"  fee:    {meta['fee']}")
    price = dex.wprl_price_in_quote()
    print(f"  WPRL price: {price} {cfg.quote_token_symbol}")

    print(f"\n[allowance] router {cfg.router_address}")
    print(f"  WPRL:  {dex.allowance(cfg.wprl_address)}")
    print(f"  quote: {dex.allowance(cfg.quote_token_address)}")
    print("  (if 0, run scripts/approve_router.py)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
