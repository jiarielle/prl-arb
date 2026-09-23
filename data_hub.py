"""Data hub — decoupled, always-on data collection. Runs independently of trading.

Polls every INTERVAL and appends a full snapshot to a JSONL time-series, and
overwrites state.json (single source of truth other components read).

Captures everything we need for later analysis — crucially the EXPOSURE DIRECTION
time-series (cex_prem vs dex_prem net bps) to answer: does the edge oscillate
(self-balancing inventory, little bridging) or stay one-directional (must bridge)?

Never trades. Safe to run 24/7 even when arbitrage is stopped.
"""
import os, sys, time, json
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent) if (Path(__file__).parent.name=="scripts") else str(Path(__file__).parent))

from config import load_config
from safetrade import SafeTradeClient
from dex import DexClient
import arb
import pearlbridge_api

ROOT = Path(__file__).parent
STATE = ROOT / "state.json"
SERIES = ROOT / "logs" / "timeseries.jsonl"
INTERVAL = 15


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def collect(cfg, st, dex):
    """One full snapshot. Best-effort per-field; partial failures don't abort."""
    snap = {"ts": now_iso()}
    # --- market via arb engine (gives sized net bps both directions) ---
    try:
        m = arb.snapshot_market(cfg, st, dex)
        snap["spot"] = float(m.spot)
        snap["bid1_px"] = float(m.bid1_px); snap["bid1_qty"] = float(m.bid1_qty)
        snap["ask1_px"] = float(m.ask1_px); snap["ask1_qty"] = float(m.ask1_qty)
        # inventory-AWARE net bps (what we can actually trade right now; -99999 if a
        # side is blocked by inventory). Useful for the live engine, NOT for market analysis.
        snap["cex_prem_net_bps"] = float(m.cex_prem.net_bps)
        snap["dex_prem_net_bps"] = float(m.dex_prem.net_bps)
        # inventory-INDEPENDENT raw market spread for a fixed probe size — this is the
        # CLEAN signal for "is the edge one-directional or oscillating", unaffected by
        # how our own inventory happens to be distributed.
        try:
            from decimal import Decimal
            probe_usd = Decimal("250")
            probe_qty = probe_usd / m.spot if m.spot > 0 else Decimal(0)
            taker = Decimal(cfg.safetrade_taker_fee_bps) / Decimal(10000)
            # CEX_PREM raw: sell PRL @ bid1*(1-taker), buy WPRL on DEX (quote_exact_out)
            cex_recv = probe_qty * m.bid1_px * (Decimal(1) - taker)
            dex_cost = dex.quote_exact_out(cfg.quote_token_address, cfg.wprl_address, probe_qty)
            raw_cex_prem = float((cex_recv - dex_cost) / (probe_qty * m.bid1_px) * Decimal(10000)) if probe_qty > 0 else None
            # DEX_PREM raw: sell WPRL on DEX (quote_exact_in), buy PRL @ ask1*(1+taker)
            dex_recv = dex.quote_exact_in(cfg.wprl_address, cfg.quote_token_address, probe_qty)
            cex_cost = probe_qty * m.ask1_px * (Decimal(1) + taker)
            raw_dex_prem = float((dex_recv - cex_cost) / (probe_qty * m.ask1_px) * Decimal(10000)) if probe_qty > 0 else None
            snap["raw_cex_prem_bps"] = raw_cex_prem
            snap["raw_dex_prem_bps"] = raw_dex_prem
            # the 4 effective leg prices (USDT per PRL/WPRL) for a 4-leg mid:
            #   CEX bid (sell), CEX ask (buy), DEX buy (cost/qty), DEX sell (recv/qty)
            if probe_qty > 0:
                snap["dex_buy_px"] = float(dex_cost / probe_qty)
                snap["dex_sell_px"] = float(dex_recv / probe_qty)
            # market direction from CLEAN signal (inventory-independent)
            if raw_cex_prem is not None and raw_dex_prem is not None:
                if raw_cex_prem >= raw_dex_prem:
                    snap["mkt_dir"] = "cex_prem"; snap["mkt_edge_bps"] = raw_cex_prem
                else:
                    snap["mkt_dir"] = "dex_prem"; snap["mkt_edge_bps"] = raw_dex_prem
        except Exception as e:
            snap["raw_spread_error"] = str(e)[:120]
        # which tradeable direction (inventory-aware) is best now
        if m.cex_prem.net_bps >= m.dex_prem.net_bps:
            snap["edge_dir"] = "cex_prem"; snap["edge_bps"] = float(m.cex_prem.net_bps)
        else:
            snap["edge_dir"] = "dex_prem"; snap["edge_bps"] = float(m.dex_prem.net_bps)
        snap["inv"] = {
            "cex_prl": float(m.cex_balance_prl), "cex_usdt": float(m.cex_balance_usdt),
            "evm_wprl": float(m.dex_balance_wprl), "evm_usdt": float(m.dex_balance_quote),
        }
        # BIDIRECTIONAL in-flight (computed by snapshot_market): WPRL->PRL arriving at
        # CEX adds to CEX side; PRL->WPRL that left CEX adds to EVM side. Counting both
        # keeps share correct mid-bridge (else it misreads and mis-triggers rebalancing).
        to_cex = float(getattr(m, "inflight_to_cex", 0))
        to_evm = float(getattr(m, "inflight_to_evm", 0))
        snap["inflight_to_cex"] = to_cex
        snap["inflight_to_evm"] = to_evm
        snap["inflight_prl"] = to_cex   # back-compat
        cex_eff = float(m.cex_balance_prl) + to_cex
        evm_eff = float(m.dex_balance_wprl) + to_evm
        tot = cex_eff + evm_eff
        snap["tokens_total"] = tot
        # usdt_total MUST include USDT in-flight (same as report.py) — else a mid-flight
        # CEX<->EVM USDT transfer (left one venue, not yet on the other) shows as a phantom
        # USDT deficit on the live line. ->EVM: withdrawals not yet completed; ->CEX:
        # deposits not yet credited.
        u_inflight = 0.0
        try:
            for w in st.withdraws(currency="usdt", limit=10):
                if w.get("state", w.get("status")) not in ("failed", "rejected", "canceled", "errored") \
                        and not w.get("completed_at"):
                    u_inflight += float(w.get("amount") or 0)
            for d in st.deposits(currency="usdt", limit=10):
                if not d.get("credited"):
                    u_inflight += float(d.get("amount") or 0)
        except Exception:
            pass
        snap["usdt_inflight"] = u_inflight
        snap["usdt_total"] = float(m.cex_balance_usdt + m.dex_balance_quote) + u_inflight
        snap["cex_token_share_pct"] = float(cex_eff / tot * 100) if tot > 0 else None
        tot_settled = float(m.cex_balance_prl + m.dex_balance_wprl)
        snap["cex_token_share_settled_pct"] = float(m.cex_balance_prl) / tot_settled * 100 if tot_settled > 0 else None
    except Exception as e:
        snap["market_error"] = f"{type(e).__name__}: {e}"[:160]
    # --- bridge / transfer state (in-flight visibility) ---
    try:
        deps = st.deposits(currency="prl", limit=8)
        snap["prl_deposits_inflight"] = [
            {"amount": d["amount"], "status": d["status"], "credited": d["credited"],
             "created": d.get("created_at", "")[:19]}
            for d in deps if not d.get("credited")
        ]
    except Exception as e:
        snap["deposits_error"] = str(e)[:120]
    try:
        wds = st.withdraws(currency="usdt", limit=5)
        snap["usdt_withdraws_recent"] = [
            {"amount": w.get("amount"), "state": w.get("state", w.get("status")),
             "created": w.get("created_at", "")[:19]}
            for w in wds[:3]
        ]
    except Exception as e:
        snap["withdraws_error"] = str(e)[:120]
    try:
        snap["eth_balance"] = float(dex.w3.from_wei(dex.w3.eth.get_balance(dex.address), "ether"))
        snap["gas_gwei"] = float(dex.w3.eth.gas_price) / 1e9
    except Exception as e:
        snap["evm_error"] = str(e)[:120]
    return snap


def main():
    cfg = load_config()
    st = SafeTradeClient(cfg.safetrade_api_key, cfg.safetrade_api_secret, cfg.safetrade_base_url)
    dex = DexClient(cfg)
    SERIES.parent.mkdir(exist_ok=True)
    # baseline for live PnL on the MKT line (price-free: coin delta + USDT delta vs start)
    try:
        bl = json.load(open(ROOT / "baseline.json"))
        start_tok, start_usd = float(bl["start_token"]), float(bl["start_usdt"])
    except Exception:
        start_tok = start_usd = None
    print(f"data_hub start — every {INTERVAL}s -> state.json + {SERIES.name}", flush=True)
    n = 0
    while True:
        snap = collect(cfg, st, dex)
        # single source of truth — atomic write so readers never see a partial state.json
        _tmp = STATE.with_suffix(".json.tmp")
        _tmp.write_text(json.dumps(snap, indent=1, default=str))
        os.replace(_tmp, STATE)
        # append to immutable time-series for later analysis
        with open(SERIES, "a") as f:
            f.write(json.dumps(snap, default=str) + "\n")
        n += 1
        if n % 4 == 0 or "market_error" in snap:
            inv = snap.get("inv", {})
            # BOTH directions of bridge in-flight (the old `inflight=` only counted PRL
            # deposits heading to CEX = to_cex, and printed 0 even while a big PRL->WPRL
            # reverse bridge was arriving at EVM — misleading. Show both amounts.)
            i_cex = snap.get("inflight_to_cex", 0) or 0
            i_evm = snap.get("inflight_to_evm", 0) or 0
            cprem = snap.get("raw_cex_prem_bps") or 0
            dprem = snap.get("raw_dex_prem_bps") or 0
            mbps = snap.get("mkt_edge_bps")
            share = snap.get("cex_token_share_pct")
            # 4-leg mid price (USDT/PRL): avg of CEX bid, CEX ask, DEX buy, DEX sell
            legs = [snap.get("bid1_px"), snap.get("ask1_px"), snap.get("dex_buy_px"), snap.get("dex_sell_px")]
            mid = (sum(legs) / 4) if all(x is not None for x in legs) else None
            # live PnL vs baseline, price-free: coin delta + USDT delta (token total incl in-flight)
            if start_tok is not None and snap.get("tokens_total") is not None:
                d_tok = snap["tokens_total"] - start_tok
                d_usd = (snap.get("usdt_total") or 0) - start_usd
                pnl_s = f"  PnL 币{d_tok:+.0f}/U{d_usd:+.0f}"
            else:
                pnl_s = ""
            # STUCK-BRIDGE ALERT — PearlBridge 公开 API 按 txid 精确查 relay 状态机, 不再
            # 依赖在途计提推断 (那个有 6h 时效窗口: 6/10 那笔卡 13h, 过期后 i_evm 归零、
            # 告警消失, 只剩"总币偏离基线"的间接信号)。所以这里不 gate 在 i_evm 上, 每次
            # 都扫最近 24h 的提现; API 自带 4min 缓存, 不会打爆。API 不可达时降级回老的
            # 链上 mint 匹配启发式。正向桥(烧 WPRL->CEX)同样查, 数据源是 supply 的账本。
            alert = ""
            try:
                ralerts = pearlbridge_api.reverse_alerts(st)
                if ralerts is None and i_evm > 0:   # API 降级: 老启发式兜底
                    age = arb.reverse_inflight_age_min(cfg, st, dex)
                    if age > 60:
                        ralerts = [f"🚨反向桥卡{age:.0f}min未mint(API不可达,启发式) 去claim: "
                                   f"https://pearlbridge.xyz/?ethAddress={dex.address}"]
                falerts = pearlbridge_api.forward_alerts()
                for a in (ralerts or []) + falerts:
                    alert += f"\n  {a}"
            except Exception:
                pass
            print(f"[{datetime.now().strftime('%H:%M:%S')}] MKT [cex_prem={cprem:.0f} dex_prem={dprem:.0f}]  "
                  f"mid={'?' if mid is None else f'{mid:.4f}'}  share={share:.0f}%  "
                  f"CEXprl={inv.get('cex_prl',0):.0f} EVMprl={inv.get('evm_wprl',0):.0f} "
                  f"Inflight→CEX{i_cex:.0f}/→EVM{i_evm:.0f}{pnl_s}{alert}"
                  if mbps is not None and share is not None else
                  f"[{datetime.now().strftime('%H:%M:%S')}] (partial snapshot)", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
