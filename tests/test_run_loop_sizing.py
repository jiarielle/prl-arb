"""Main loop with fakes: scale-up, revert cost booking, fixed-cap hold after a revert, release on success."""
import sys, json, types, tempfile
from decimal import Decimal as D
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
import arb, auto_run


def run_scenario(swap_results, sized_qty=2000.0, sizing_raises=False):
    tmp = Path(tempfile.mkdtemp())
    auto_run.LOG, auto_run.LEDGER, auto_run.SIZING_LOG = tmp / "log", tmp / "trades.jsonl", tmp / "sizing.jsonl"
    alerts = []
    auto_run._alert = alerts.append
    auto_run.time.sleep = lambda s: None
    r = auto_run.Runner.__new__(auto_run.Runner)
    r.cfg = types.SimpleNamespace(dry_run=False, hard_max_trade_usd=0, quote_token_symbol="USDT", kill_switch_file=str(tmp / ".STOP"), min_gap_bps=150, max_trade_usd=250, slippage_bps=200,
                                  poll_interval_sec=0, quote_token_address="q", wprl_address="w")
    r.aprec, r.slip, r.private_ok = 4, D("0.02"), True
    r.dex = types.SimpleNamespace(allowance=lambda a: 2**256 - 1, balance=lambda a: D(5000), wprl_price_in_quote=lambda: D("0.91"), address="0x")
    r.st = types.SimpleNamespace(balance=lambda c: D(5000))
    r.w3 = None
    m = types.SimpleNamespace(bid1_px=D("1.09"), ask1_px=D("1.10"), spot=D("0.91"), bid1_qty=D(15000), ask1_qty=D(1000),
                              cex_balance_prl=D(3000), cex_balance_usdt=D(4000), dex_balance_wprl=D(1), dex_balance_quote=D(2500))
    plan = arb.TradePlan(arb.Direction.CEX_PREMIUM, D(250), D("229.3577"), 1500, D("38"), D(211), "t")
    fires, swaps = [], list(swap_results)
    orig_snapshot = arb.snapshot_market
    def snapshot(cfg, st, dex):
        if not swaps: Path(r.cfg.kill_switch_file).write_text("stop")
        return m
    arb.snapshot_market = snapshot
    none = arb.TradePlan(arb.Direction.NONE, D(0), D(0), 0, D(0), D(0), "done")
    r.engine = types.SimpleNamespace(evaluate=lambda mm: plan if swaps else none)
    def sizing(mm, p, q):
        if sizing_raises: return auto_run.Runner.adaptive_size(r, mm, p, q)
        return {"qty": sized_qty, "marginal_bps": 1100.0, "cap": "evm_quote_inv", "avg_bps": 1300.0, "exp_pnl_usd": 280.0}
    r.adaptive_size = sizing
    r.bal = lambda: {"cex_prl": D(1), "cex_usdt": D(1), "evm_wprl": D(1), "evm_usdt": D(1)}
    r.cancel_stale_orders = r.reconcile_unconfirmed = r.reclose_exposures = lambda: None
    r._gas_sufficient = lambda: (True, 1, 1)
    oid = [0]
    def cex_fill(side, qty, ref_px=None, cross_bps=80):
        oid[0] += 1; fires.append((side, float(qty))); return D(qty), oid[0]
    r.cex_fill = cex_fill
    r._order_usdt = lambda o, side: (D("1.09") if side == "sell" else D("-1.104")) * D(str(fires[o - 1][1]))
    def dex_swap(direction, amt, econ=None):
        res = swaps.pop(0)
        if res == "ok": return {"ok": True, "wprl_delta": amt, "usdt_delta": -amt * D("0.93"), "outcome": "success", "tx_hash": "0x1"}
        return {"ok": False, "wprl_delta": D(0), "outcome": res, "tx_hash": "0x2"}
    r.dex_swap = dex_swap
    try:
        r.run()
    finally:
        arb.snapshot_market = orig_snapshot
    rows = [json.loads(l) for l in open(auto_run.LEDGER)] if auto_run.LEDGER.exists() else []
    return fires, rows, alerts, open(auto_run.LOG).read()


def test_scale_up_then_revert_holds_fixed_cap_until_success():
    fires, rows, alerts, log = run_scenario(["ok", "reverted", "reverted", "ok", "ok"])
    sells = [q for s, q in fires if s == "sell"]
    assert sells == [2000.0, 2000.0, 229.3577, 229.3577, 2000.0], sells
    rev = [r for r in rows if r.get("outcome") == "reverted"]
    assert len(rev) == 2 and abs(rev[0]["revert_cost_usd"] - (-28.0)) < 0.01 and abs(rev[1]["revert_cost_usd"] - (-3.211)) < 0.01
    assert "cum revert $-31.21" in log and not alerts             # below the -40 push line
    assert "CUM LOSS" not in log                                  # revert cost stays out of the stop


def test_revert_costs_push_once():
    fires, rows, alerts, log = run_scenario(["reverted", "ok", "reverted", "ok", "reverted"])
    assert len(alerts) == 1 and "回滚" in alerts[0], alerts


def test_sizing_error_falls_back_to_fixed_cap_loudly():
    fires, rows, alerts, log = run_scenario(["ok"], sizing_raises=True)
    assert [q for s, q in fires if s == "sell"] == [229.3577] and "adaptive sizing failed" in log


def test_smaller_marginal_qty_never_shrinks_the_shot():
    fires, *_ = run_scenario(["ok"], sized_qty=50.0)
    assert [q for s, q in fires if s == "sell"] == [229.3577]


def test_unconfirmed_big_shot_pushes_and_holds():
    fires, rows, alerts, log = run_scenario(["unconfirmed", "ok"])
    assert len(alerts) == 1 and "未确认" in alerts[0]
    assert [q for s, q in fires if s == "sell"] == [2000.0, 229.3577]


def test_expired_deadline_is_reversed_and_booked_like_a_revert():
    fires, rows, alerts, log = run_scenario(["expired", "ok"])
    assert [q for s, q in fires] == [2000.0, 2000.0, 229.3577]      # sell, buy back, next shot held small
    assert rows[0]["outcome"] == "expired" and rows[0]["revert_cost_usd"] < 0


def _runner():
    r = auto_run.Runner.__new__(auto_run.Runner)
    r.cfg = types.SimpleNamespace(min_gap_bps=150, safetrade_taker_fee_bps=20)
    r.slip = D("0.02")
    return r


def test_min_out_follows_the_shot_profit():
    r = _runner(); econ = {"bid": D("1.09"), "ask": D("1.10")}
    # 17% edge: pool can move a lot and the shot still nets 150bp
    floor, loose = r.econ_tolerance(arb.Direction.CEX_PREMIUM, D(2000), D(1860), econ)
    assert loose and D(1700) < floor < D(1800)
    short = D(2000) - floor                                         # worst fill still leaves the threshold
    pnl = D(2000) * D("1.09") * D("0.998") - D(1860) - short * D("1.10") * D("1.002") * D("1.01")
    assert pnl >= D(2000) * D("1.09") * D("0.998") * D("0.015") - D("0.01")
    # thin edge: never stricter than the fixed 2%
    floor, loose = r.econ_tolerance(arb.Direction.CEX_PREMIUM, D(2000), D(2130), econ)
    assert not loose and floor == D(1960)
    # edge already gone after the CEX fill: fixed 2%, as before
    floor, loose = r.econ_tolerance(arb.Direction.CEX_PREMIUM, D(2000), D(2300), econ)
    assert not loose and floor == D(1960)
    # absurd edge: tolerance capped at half the shot
    floor, loose = r.econ_tolerance(arb.Direction.CEX_PREMIUM, D(2000), D(200), econ)
    assert loose and floor == D(1000)
    # selling on DEX: must return CEX cost + threshold
    floor, loose = r.econ_tolerance(arb.Direction.DEX_PREMIUM, D(1000), D(1300), econ)
    assert loose and abs(floor - D(1000) * D("1.10") * D("1.002") * D("1.015")) < D("0.0001")
    floor, loose = r.econ_tolerance(arb.Direction.DEX_PREMIUM, D(1000), D(1125), econ)
    assert not loose and floor == D(1125) * D("0.98")


def test_deadline_proof():
    import dex as dexmod
    from web3.exceptions import TransactionNotFound
    class Eth:
        def __init__(s, head_ts, rc): s.head_ts, s.rc = head_ts, rc
        def get_block(s, _): return types.SimpleNamespace(timestamp=s.head_ts)
        def get_transaction_receipt(s, h):
            if s.rc is None: raise TransactionNotFound("nf")
            return s.rc
    import time
    real_sleep = time.sleep
    d = dexmod.DexClient.__new__(dexmod.DexClient)
    now = int(time.time())
    # head past the deadline on the SAME node that has no receipt -> expired
    d.w3 = types.SimpleNamespace(eth=Eth(0, None)); d._alc_w3 = [types.SimpleNamespace(eth=Eth(now, None))]
    assert d.wait_until_deadline("0x", now - 100, poll=0) == "expired"
    # node's head is still before the deadline -> not a proof -> keeps waiting -> None at the hard cap
    d._alc_w3 = [types.SimpleNamespace(eth=Eth(now - 500, None))]
    assert d.wait_until_deadline("0x", now - 100, poll=0, hard_cap_sec=101) is None
    # receipt wins over everything
    d._alc_w3 = [types.SimpleNamespace(eth=Eth(now, "RC"))]
    assert d.wait_until_deadline("0x", now - 100, poll=0) == "RC"
    # first node errors, second proves
    class Bad:
        def get_block(s, _): raise RuntimeError("429")
    d._alc_w3 = [types.SimpleNamespace(eth=Bad()), types.SimpleNamespace(eth=Eth(now, None))]
    assert d.wait_until_deadline("0x", now - 100, poll=0) == "expired"
