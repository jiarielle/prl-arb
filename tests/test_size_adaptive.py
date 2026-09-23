"""size_adaptive: marginal stop rule, caps, failure modes; and _size_* unchanged by the caps refactor."""
import sys, types, importlib.util
from decimal import Decimal as D
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import arb


class FakeDex:
    """Constant-product pool x*y=k, 1% fee on input. x = WPRL, y = USDT."""
    def __init__(self, x, y, fee=D("0.01")):
        self.x, self.y, self.fee = D(x), D(y), fee
    def quote_buy_wprl_full(self, q):
        if q >= self.x: raise RuntimeError("exceeds pool")
        net_in = self.x * self.y / (self.x - q) - self.y
        return net_in / (1 - self.fee), (self.y + net_in) / (self.x - q)
    def quote_sell_wprl_full(self, q):
        net = q * (1 - self.fee)
        out = self.y - self.x * self.y / (self.x + net)
        return out, (self.y - out) / (self.x + net)
    def quote_exact_out(self, tin, tout, q): return self.quote_buy_wprl_full(q)[0]
    def quote_exact_in(self, tin, tout, q): return self.quote_sell_wprl_full(q)[0]


def cfg(**kw):
    c = types.SimpleNamespace(
        safetrade_taker_fee_bps=20, pool_fee_tier=10000, max_trade_usd=250,
        min_prl_inventory=0, max_prl_inventory=10**6, min_wprl_inventory=0, max_wprl_inventory=10**6,
        min_usdt_inventory=0, min_usdc_inventory=0, bridge_fee_bps_prl_to_wprl=50,
        bridge_fee_bps_wprl_to_prl=0, quote_token_address="q", wprl_address="w")
    c.__dict__.update(kw); return c


def mp(**kw):
    m = {"bid1_px": D("1.09"), "bid1_qty": D(15000), "ask1_px": D("1.10"), "ask1_qty": D(15000),
         "spot": D("0.91"), "cex_balance_prl": D(2900), "cex_balance_usdt": D(4500),
         "dex_balance_wprl": D(2000), "dex_balance_quote": D(2500)}
    m.update(kw); return m


def test_big_window_hits_inventory_cap():
    dex = FakeDex(100000, 91000)                      # spot 0.91, deep pool
    r = arb.size_adaptive(cfg(), mp(), dex, arb.Direction.CEX_PREMIUM, 150, probes=12)
    # edge is ~1500bp everywhere, so the only stop is the EVM USDT we actually hold
    assert r["marginal_bps"] > 1000 and 2450 <= r["dex_usdt"] <= 2500
    r2 = arb.size_adaptive(cfg(), mp(dex_balance_quote=D(10**6)), dex, arb.Direction.CEX_PREMIUM, 150)
    assert r2["at_cap"] and r2["cap"] == "cex_prl_inv" and r2["qty"] == 2900.0


def test_stops_where_last_token_is_at_threshold():
    dex = FakeDex(20000, 20800)                       # spot 1.04 vs bid 1.09: thin edge
    c = cfg()
    r = arb.size_adaptive(c, mp(dex_balance_quote=D(10**6), cex_balance_prl=D(10**6)), dex,
                          arb.Direction.CEX_PREMIUM, 150, probes=12)
    assert 0 < r["qty"] < r["cap_qty"] and not r["at_cap"]
    assert r["marginal_bps"] >= 150 and r["cap_marginal_bps"] < 150
    _, p_after = dex.quote_buy_wprl_full(D(str(r["qty"])) * D("1.05"))   # 5% more must fail
    m = (D("1.09") * D("0.998") - p_after / D("0.99")) / D("1.09") * 10000
    assert m < 150


def test_income_gone_means_no_trade():
    dex = FakeDex(100000, 109000)                     # pool price == CEX bid -> no edge
    r = arb.size_adaptive(cfg(), mp(), dex, arb.Direction.CEX_PREMIUM, 150)
    assert r["qty"] == 0.0 and "reason" in r


def test_zero_inventory_means_no_trade():
    r = arb.size_adaptive(cfg(min_prl_inventory=5000), mp(), FakeDex(100000, 91000),
                          arb.Direction.CEX_PREMIUM, 150)
    assert r["qty"] == 0.0 and r["reason"].startswith("capped@0")


def test_never_spends_more_usdt_than_available():
    dex = FakeDex(3000, 2730)                         # thin pool: cost climbs fast
    r = arb.size_adaptive(cfg(), mp(dex_balance_quote=D(500)), dex, arb.Direction.CEX_PREMIUM, 150, probes=12)
    assert r["dex_usdt"] <= 500


def test_dex_premium_charges_restock_bridge():
    dex = FakeDex(20000, 23000)                       # pool 1.15 vs ask 1.10
    a = arb.size_adaptive(cfg(), mp(dex_balance_wprl=D(10**5)), dex, arb.Direction.DEX_PREMIUM, 150, probes=12)
    b = arb.size_adaptive(cfg(bridge_fee_bps_prl_to_wprl=0), mp(dex_balance_wprl=D(10**5)), dex,
                          arb.Direction.DEX_PREMIUM, 150, probes=12)
    assert 0 < a["qty"] < b["qty"]


def test_live_sizing_unchanged_by_caps_refactor():
    import os
    old_path = os.environ.get("ARB_BAK")
    if not old_path: return
    spec = importlib.util.spec_from_file_location("arb_old", old_path)
    old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
    dex = FakeDex(100000, 91000)
    for m in (mp(), mp(cex_balance_prl=D(100)), mp(bid1_qty=D(50), ask1_qty=D(70)), mp(dex_balance_quote=D(90))):
        for fn in ("_size_cex_premium", "_size_dex_premium"):
            a, b = getattr(arb, fn)(cfg(), m, dex), getattr(old, fn)(cfg(), m, dex)
            assert (a.qty, a.pnl_usd, a.net_bps, a.reason) == (b.qty, b.pnl_usd, b.net_bps, b.reason), fn
