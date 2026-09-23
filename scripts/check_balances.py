"""Quick combined inventory snapshot across both venues."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import load_config
from safetrade import SafeTradeClient
from dex import DexClient


def main():
    cfg = load_config()
    st = SafeTradeClient(cfg.safetrade_api_key, cfg.safetrade_api_secret, cfg.safetrade_base_url)
    dex = DexClient(cfg)

    print("=== SafeTrade ===")
    try:
        for b in st.balances():
            bal = float(b.get("balance", 0)) + float(b.get("locked", 0))
            if bal > 0:
                print(f"  {b['currency']:>8}: {bal:.6f}")
    except Exception as e:
        print(f"  ERROR: {e}")

    print("\n=== EVM ===")
    try:
        eth = float(dex.w3.from_wei(dex.w3.eth.get_balance(dex.address), 'ether'))
        wprl = float(dex.balance(cfg.wprl_address))
        quote = float(dex.balance(cfg.quote_token_address))
        print(f"  ETH:   {eth:.6f}")
        print(f"  WPRL:  {wprl:.6f}")
        print(f"  {cfg.quote_token_symbol}: {quote:.6f}")

        if cfg.pool_address:
            price = float(dex.wprl_price_in_quote())
            print(f"\n  WPRL spot: {price:.4f} {cfg.quote_token_symbol}")
            print(f"  WPRL value: ${wprl * price:.2f}")
    except Exception as e:
        print(f"  ERROR: {e}")


if __name__ == "__main__":
    main()
