"""Autonomous overnight arb runner — self-orchestrating, self-healing.

Design decisions baked in from tonight's debugging:
  * CEX leg FIRST (it's the failable, synchronous one). Confirm actual fill via
    balance delta, THEN fire the irreversible on-chain DEX leg sized to that fill.
    If the DEX leg fails after CEX filled, reverse the CEX fill (hedge) immediately.
  * After every round, reconcile to FLAT using real balances; any residual token
    imbalance is hedged on CEX (fast venue). No naked exposure left overnight.
  * Hard safety rails: cumulative-loss stop, per-trade cap, max completed trades,
    time budget, .STOP kill file.

Success = round trip completed, position flat, realized P&L > 0.
Target = 5 successes.
"""
import sys, time, json, threading
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import load_config
from safetrade import SafeTradeClient
from dex import DexClient
import arb

# ---- goal / rails ----
TARGET_SUCCESS   = 9999        # run continuously all day
MAX_COMPLETED    = 9999
CUM_LOSS_STOP    = Decimal("-40")  # wider daily drawdown stop
TIME_BUDGET_SEC  = 16 * 3600       # ~all day
HEDGE_TOL        = Decimal("0.5")  # token units; flatten if |delta| exceeds
ALERT_TOL        = Decimal("10")   # token units; a cleanup SHORTFALL ≥ this = real exposure
                                   # -> alert + ledger + not-clean. Below it: log-only, acceptable
                                   # (user 2026-06-20: "<10 token 多大点事, 别找我").
SETTLE_CEX_POLL  = 0.5             # poll CEX fill every 0.5s
SETTLE_CEX_MAX   = 3               # up to 3s; then cancel unfilled (don't chase a moved book)
CANCEL_RETRY_S   = 5               # 撤单后订单不到终态, 每 5s 再发一次撤单
CANCEL_TERMINAL_MAX_S = 60         # 撤单后等终态的硬顶; 到顶仍非终态 -> CRIT + 成交记录兜底
LOG = Path(__file__).parent.parent / "logs" / "auto_run.log"
LOG.parent.mkdir(exist_ok=True)
LEDGER = Path(__file__).parent.parent / "logs" / "trades.jsonl"   # append-only, never cleared
RESOLVED = Path(__file__).parent.parent / "logs" / "reconcile_resolved.jsonl"  # unconfirmed legs we've settled
EXP_RESOLVED = Path(__file__).parent.parent / "logs" / "exposure_resolved.jsonl"  # shortfall re-close journal
SIZING_LOG = Path(__file__).parent.parent / "logs" / "sizing.jsonl"   # one row per fire: marginal sizing vs the fixed cap
ADAPTIVE_SIZING = True             # size each shot to where the LAST token still nets min_gap_bps (2026-09-20)
ALLOWANCE_FLOOR = 10**30           # router allowance below this = not the unlimited approve we expect -> refuse to trade
REVERT_ALERT_USD = Decimal("-40")  # push once per run when booked revert costs reach this
# When the shot's own profit allows a looser DEX min-out than the fixed slippage, the swap is
# relayed privately (a loose min-out in the public mempool is a sandwich invitation; the pool
# fee makes the fixed 2% unprofitable to sandwich, anything wider is not) with an on-chain deadline.
PRIVATE_DEADLINE_SEC = 60
PRIVATE_TIP_GWEI = 2               # enough to be picked up within the deadline, not a race
HEDGE_PX_PAD = Decimal("1.01")     # residual hedge assumed 1% worse than the touch
RECONCILE_EVERY_SEC = 120          # how often the loop re-checks unconfirmed legs / stale orders
UNCONFIRMED_DROP_SEC = 60          # min age before a not-in-mempool tx is treated as dropped


def now():
    return datetime.now().strftime("%H:%M:%S")


def emit(msg):
    # write ONLY to the log file. Do NOT also print() — when the process is launched
    # with stdout redirected to the same log (>> auto_run.log), print()+file write would
    # land the same line twice (the duplicate-line symptom).
    line = f"[{now()}] {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")


def _alert(text):
    """Best-effort WeChat ops alert. NEVER raises — must not break the trade loop."""
    try:
        import os
        import requests
        tok = requests.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                           params={"corpid": os.getenv("WECHAT_CORP_ID"),
                                   "corpsecret": os.getenv("WECHAT_OPS_SECRET")},
                           timeout=15).json().get("access_token")
        requests.post(f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={tok}",
                      json={"touser": os.getenv("WECHAT_ADMIN_USER_ID"), "msgtype": "text",
                            "agentid": int(os.getenv("WECHAT_OPS_AGENT_ID")),
                            "text": {"content": text}}, timeout=15)
    except Exception as e:
        emit(f"  alert push failed: {str(e)[:80]}")


def qd(x, prec):
    return Decimal(x).quantize(Decimal(10) ** (-prec), rounding=ROUND_DOWN)


class Runner:
    def __init__(self):
        self.cfg = load_config()
        self.st = SafeTradeClient(self.cfg.safetrade_api_key, self.cfg.safetrade_api_secret, self.cfg.safetrade_base_url)
        self.dex = DexClient(self.cfg)
        self.w3 = self.dex.w3                      # used by _wprl_delta_from_tx
        self.engine = arb.ArbEngine(self.cfg)
        self.slip = Decimal(self.cfg.slippage_bps) / Decimal(10000)
        self.private_ok = bool(getattr(self.cfg, "private_tx_rpc", ""))
        self.aprec = self.cfg.cex_amount_precision

    # ---- balances ----
    def bal(self):
        return {
            "cex_prl": self.st.balance("prl"),
            "cex_usdt": self.st.balance("usdt"),
            "evm_wprl": self.dex.balance(self.cfg.wprl_address),
            "evm_usdt": self.dex.balance(self.cfg.quote_token_address),
        }

    def tokens(self, b):
        return b["cex_prl"] + b["evm_wprl"]

    def usd(self, b):
        return b["cex_usdt"] + b["evm_usdt"]

    # ---- CEX marketable order. Returns actual EXECUTED base volume read from the
    # ORDER itself (executed_volume), NOT a global balance delta — so a deposit/
    # withdrawal landing mid-fill cannot corrupt the measured fill. ----
    def cex_fill(self, side, qty, ref_px=None, cross_bps=80):
        """Place a limit at ref_px (default: current touch). cross_bps=0 means EXACTLY at
        the level — fills at that price or better, never walks the book. The MAIN arb leg
        passes the snapshot bid1/ask1 (the price the edge was computed from) with cross=0,
        so it fills at exactly our target or not at all (no thin-book slippage). Cleanup
        legs (hedge/reverse) use the default small cross to make sure they fill."""
        if self.cfg.dry_run:
            emit(f"  DRY RUN: CEX {side} {float(qty):.4f} NOT sent (DRY_RUN=true)")
            return Decimal(0), None
        if ref_px is None or ref_px <= 0:
            bid, ask = self.st.best_bid_ask(self.cfg.safetrade_market)
            ref_px = ask if side == "buy" else bid
        try:
            resp = self.st.place_marketable_limit(self.cfg.safetrade_market, side, qty, ref_price=ref_px,
                                                  cross_bps=cross_bps, amount_precision=self.aprec)
        except Exception as e:
            emit(f"  CEX {side} REJECTED: {str(e)[:150]}")
            return Decimal(0), None
        oid = resp.get("id")
        # poll the order's fill every SETTLE_CEX_POLL up to SETTLE_CEX_MAX; a marketable
        # limit usually fills within ~1s, so this returns far faster than a fixed sleep.
        filled = Decimal(0)
        waited = 0.0
        while waited < SETTLE_CEX_MAX:
            time.sleep(SETTLE_CEX_POLL); waited += SETTLE_CEX_POLL
            try:
                o = self.st.order(oid)
                filled = Decimal(str(o.get("filled_amount") or o.get("executed_volume") or "0"))
                state = o.get("state")
                if filled > 0 and (state in ("done", "cancel") or filled >= qty * Decimal("0.999")):
                    break
            except Exception as e:
                emit(f"  WARN: read order {oid}: {str(e)[:60]}")
        # cancel only OUR order's unfilled remainder — never touch foreign/manual orders
        if oid:
            try:
                self.st.cancel_order(oid)
            except Exception as e:
                emit(f"  WARN: cancel order {oid} failed: {str(e)[:80]} (order may still be live)")
            # 终查 (2026-07-05): 撤单后必须读到订单终态才能采信 filled。事故: order
            # 某笔订单的成交登记慢于 3s 轮询窗口, 轮询一直读到 filled=0, 引擎判
            # "did not fill, no exposure" 跳过 —— 实际卖腿已成交, 未对冲未
            # 记账, 留下裸腿。撤单后订单必达终态(done=全成 / cancel=撤掉余量), 终态
            # 的 filled_amount 才可信; 非终态的 0 只是登记滞后。
            # 2026-09-17 加固: 原来终查最多读 6 次(3s), 读不到终态就按最后一次的 0 采信。
            # 三次同型事故都漏在这里: case A (撤单 1s 后登记成交),
            # case B (撤单没生效, 9s 后全额成交, 终态 done),
            # case C (放弃后 0.4s 登记全成, 终态 done)。合计数百枚
            # 裸卖。现在: 不到终态不放行, 每 CANCEL_RETRY_S 再发一次撤单, 硬顶
            # CANCEL_TERMINAL_MAX_S; 到顶仍非终态 -> CRIT + 成交记录兜底, 绝不按 0。
            final = None
            terminal = False
            t_start = time.time()
            last_cancel = t_start
            while True:
                try:
                    o = self.st.order(oid)
                    f2 = Decimal(str(o.get("filled_amount") or o.get("executed_volume") or "0"))
                    final = f2 if final is None else max(final, f2)
                    if o.get("state") in ("done", "cancel", "reject"):
                        terminal = True
                        break
                except Exception as e:
                    emit(f"  WARN: read order {oid} after cancel: {str(e)[:60]}")
                elapsed = time.time() - t_start
                if elapsed >= CANCEL_TERMINAL_MAX_S:
                    break
                if time.time() - last_cancel >= CANCEL_RETRY_S:
                    last_cancel = time.time()
                    try:
                        self.st.cancel_order(oid)
                        emit(f"  re-sent cancel for order {oid} (still not terminal after {elapsed:.1f}s)")
                    except Exception as e:
                        emit(f"  WARN: re-cancel order {oid} failed: {str(e)[:80]}")
                time.sleep(SETTLE_CEX_POLL)
            if not terminal:
                # 硬顶仍非终态: 订单可能还挂在簿上, 之后任何成交都是裸腿。大声报, 并用
                # 成交记录兜底取最大已知成交量; reconcile 那边看到 CRIT 词会标红。
                emit(f"  ⚠ CRIT: order {oid} NOT terminal after {CANCEL_TERMINAL_MAX_S}s "
                     f"(state unknown, seen filled {float(final or 0):.4f}) — VERIFY ORDER/BALANCES")
                try:
                    tsum = sum((Decimal(str(t.get("amount") or 0))
                                for t in self.st.my_trades(self.cfg.safetrade_market, limit=50)
                                if t.get("order_id") == oid), Decimal(0))
                    final = tsum if final is None else max(final, tsum)
                except Exception:
                    pass
            if final is None:
                # order 端点不可用 -> 成交记录兜底; 都失败则大声报 UNCONFIRMED
                # (CRIT 词, 6h 报告会标红), 绝不静默按 0 处理。
                try:
                    final = sum((Decimal(str(t.get("amount") or 0))
                                 for t in self.st.my_trades(self.cfg.safetrade_market, limit=50)
                                 if t.get("order_id") == oid), Decimal(0))
                except Exception:
                    emit(f"  ⚠ UNCONFIRMED CEX leg: order {oid} fill unknown after cancel; "
                         f"assuming {float(filled):.4f} — VERIFY BALANCES")
            if final is not None and final > filled:
                emit(f"  CEX late fill after cancel: {float(final):.4f} (poll saw {float(filled):.4f})")
                filled = final
        return filled, oid

    def _order_usdt(self, oid, order_side):
        """Signed USDT delta of a filled order, from its TRADES (execution records, not
        balances): sell -> +received, buy -> -spent. Immune to concurrent supply/bridge
        USDT moves. Returns None if it can't be determined (caller falls back).

        The sign comes from OUR order's side (we placed it, we know it) — NOT from the
        trade record's own `side` field. That field is per-fill and does not track our
        side: a SELL of ~200 once came back as two fills,
        one labelled sell and one labelled buy. Trusting the label flipped the sign,
        booked a phantom loss on a trade that actually made money, and tripped CUM_LOSS
        into a FALSE permanent halt (18h of downtime).
        Fee currency confirms the label is junk: that 'buy' fill charged its fee in USDT,
        while real buys are charged in PRL."""
        if not oid:
            return None
        try:
            trades = self.st.my_trades(self.cfg.safetrade_market, limit=50)
        except Exception:
            return None
        is_buy = (order_side == "buy")
        usdt = Decimal(0); found = False
        for t in trades:
            if t.get("order_id") != oid:
                continue
            found = True
            total = Decimal(str(t.get("total") or 0))
            fee = Decimal(str(t.get("fee") or 0))
            fee_cur = (t.get("fee_currency") or "").lower()
            if is_buy:
                usdt -= total                       # spent USDT (buy fee is in PRL -> token side)
            else:
                usdt += total                       # received USDT
            if fee_cur == "usdt":
                usdt -= fee                         # USDT-denominated fee, whichever side
        return usdt if found else None

    # ---- DEX swap, returns True on success ----
    def econ_tolerance(self, direction, amount, dex_usdt_quote, econ):
        """Loosest DEX fill that still leaves min_gap_bps on THIS shot, from the CEX leg that
        already filled. Returns (min_out, loosened): never stricter than the fixed slippage.
          CEX_PREMIUM: received R on CEX, spend the quoted USDT, tokens short are bought back
                       on CEX near ask -> short <= (R*(1-th) - quoted) / hedge_px
          DEX_PREMIUM: spent C on CEX -> DEX must return >= C*(1+th)"""
        th = Decimal(self.cfg.min_gap_bps) / Decimal(10000)
        taker = Decimal(self.cfg.safetrade_taker_fee_bps) / Decimal(10000)
        if direction == arb.Direction.CEX_PREMIUM:
            std = amount * (Decimal(1) - self.slip)
            if not econ:
                return std, False
            received = amount * econ["bid"] * (Decimal(1) - taker)
            hedge_px = econ["ask"] * (Decimal(1) + taker) * HEDGE_PX_PAD
            short_ok = (received * (Decimal(1) - th) - dex_usdt_quote) / hedge_px
            floor = max(amount - short_ok, amount / 2)      # never grant more than half the shot
        else:
            std = dex_usdt_quote * (Decimal(1) - self.slip)
            if not econ:
                return std, False
            floor = amount * econ["ask"] * (Decimal(1) + taker) * (Decimal(1) + th)
        return (floor, True) if Decimal(0) < floor < std else (std, False)

    def dex_swap(self, direction, amount, econ=None):
        # min_out MUST be based on the Quoter's real output (incl 1% pool fee +
        # price impact), then haircut by slip. Using spot*(1-slip) was too high
        # for a thin pool -> "Too little received" revert.
        try:
            if direction == arb.Direction.CEX_PREMIUM:  # buy `amount` WPRL with USDT
                # exact-in USDT: spend EXACTLY the quoted cost of `amount` WPRL. Do NOT
                # inflate amt_in by slip — on exact-in that buffer isn't protection, it
                # just BUYS ~slip more WPRL every trade (hundreds of trades: residual always
                # positive, a large cumulative overbuy, each hedge-sold back on CEX as an extra
                # unpriced order). min_out is the real protection: if the pool moves
                # against us mid-flight the fill comes in slightly under `amount` and
                # the residual hedge tops it up; if it moves >slip the tx reverts.
                usdt_for = self.dex.quote_exact_out(self.cfg.quote_token_address, self.cfg.wprl_address, amount)
                amt_in = usdt_for
                min_out, loose = self.econ_tolerance(direction, amount, usdt_for, econ)
                loose = loose and self.private_ok
                if not loose:
                    min_out = amount * (Decimal(1) - self.slip)
                r = self.dex.swap_exact_in(self.cfg.quote_token_address, self.cfg.wprl_address,
                                           amt_in, min_out, dry_run=self.cfg.dry_run, **self._private_kw(loose))
                tol_tokens = amount - min_out
            else:  # DEX_PREMIUM: sell `amount` WPRL for USDT
                usdt_q = self.dex.quote_exact_in(self.cfg.wprl_address, self.cfg.quote_token_address, amount)
                min_out, loose = self.econ_tolerance(direction, amount, usdt_q, econ)
                loose = loose and self.private_ok
                if not loose:
                    min_out = usdt_q * (Decimal(1) - self.slip)
                r = self.dex.swap_exact_in(self.cfg.wprl_address, self.cfg.quote_token_address,
                                           amount, min_out, dry_run=self.cfg.dry_run, **self._private_kw(loose))
                tol_tokens = Decimal(0)
            status = r.get("status")
            tx_hash = r.get("tx_hash")
            verb = 'buy' if direction == arb.Direction.CEX_PREMIUM else 'sell'
            if loose:
                emit(f"  DEX {verb} relayed privately: min_out={float(min_out):.2f} (fixed slippage would be "
                     f"{float((amount if verb == 'buy' else usdt_q) * (Decimal(1) - self.slip)):.2f}), "
                     f"deadline {PRIVATE_DEADLINE_SEC}s tx={str(tx_hash)[:14]}")
            if r.get("send_error"):
                # the relay call errored; the tx may still have reached it, so the deadline decides
                # this shot — but stop using the relay for the rest of the run.
                self.private_ok = False
                emit(f"  WARN: private relay send error ({r['send_error']}) -> public path for the rest of this run")
            if r.get("expired"):
                emit(f"  DEX {verb} {float(amount):.4f} -> deadline passed with no receipt tx={str(tx_hash)[:14]} "
                     f"(can no longer execute)")
                return {"ok": False, "wprl_delta": Decimal(0), "outcome": "expired", "tx_hash": tx_hash,
                        "deadline": r.get("deadline")}
            # status semantics from dex.swap_exact_in:
            #   1    -> mined OK            (success)
            #   0    -> mined & REVERTED    (genuinely failed; CEX leg is naked -> reverse)
            #   None -> broadcast but receipt unreadable after retries -> VERIFY on-chain
            #           by hash before concluding anything (the old 429-eats-the-hash bug).
            if status == 1:
                wprl_delta, usdt_delta = self._safe_delta(tx_hash, direction)
                emit(f"  DEX {verb} {float(amount):.4f} wprl_delta={float(wprl_delta):+.4f} "
                     f"min_out={float(min_out):.2f} -> status=1 tx={str(tx_hash)[:14]}")
                return {"ok": True, "wprl_delta": wprl_delta, "usdt_delta": usdt_delta,
                        "outcome": "success", "tx_hash": tx_hash, "tol_tokens": tol_tokens}
            if status == 0:
                emit(f"  DEX {verb} {float(amount):.4f} -> status=0 REVERTED tx={str(tx_hash)[:14]} (leg truly failed)")
                return {"ok": False, "wprl_delta": Decimal(0), "outcome": "reverted", "tx_hash": tx_hash}
            # status is None -> indeterminate. The tx WAS broadcast (we have a hash); a
            # receipt 429/timeout does NOT mean it failed. Verify on-chain by hash.
            emit(f"  DEX {verb} {float(amount):.4f} -> receipt unreadable, verifying tx {str(tx_hash)[:14]} on-chain…")
            wprl_delta = self._verify_swap_onchain(tx_hash, direction)
            if wprl_delta is not None and abs(wprl_delta) >= Decimal("0.5"):
                emit(f"  DEX VERIFIED on-chain: wprl_delta={float(wprl_delta):+.4f} -> swap DID execute (treating as success)")
                return {"ok": True, "wprl_delta": wprl_delta, "outcome": "success", "tx_hash": tx_hash}
            # Could not positively confirm a WPRL move. Do NOT assume failed (assuming
            # failed -> reverse-hedge a possibly-live leg = the churn-loss trap). Flag
            # UNCONFIRMED: the run loop will refuse to reverse and leave reconciliation
            # to the next snapshot / supply share-rebalance.
            emit(f"  DEX UNCONFIRMED: tx {str(tx_hash)[:14]} not confirmed on-chain; NOT reversing (avoid blind churn)")
            return {"ok": False, "wprl_delta": Decimal(0), "outcome": "unconfirmed", "tx_hash": tx_hash,
                    "deadline": r.get("deadline")}
        except Exception as e:
            # Exception BEFORE/at broadcast (build/sign/send) -> no tx hash -> the swap
            # never went out -> CEX leg is genuinely naked -> safe to reverse.
            emit(f"  DEX swap EXCEPTION (pre-broadcast): {str(e)[:150]}")
            return {"ok": False, "wprl_delta": Decimal(0), "outcome": "not_sent", "tx_hash": None}

    def _private_kw(self, loose):
        if not loose:
            return {}
        return {"deadline_sec": PRIVATE_DEADLINE_SEC, "private": True, "tip_gwei": PRIVATE_TIP_GWEI}

    def _safe_delta(self, tx_hash, direction):
        """From a confirmed-OK swap tx, decode BOTH the WPRL delta and the USDT delta from
        the receipt's Transfer logs (signed: +received/-sent). Returns (wprl, usdt). On a
        read hiccup returns (0, None): wprl 0 lets the caller fall back to the intended
        amount; usdt None makes realized fall back to the (guarded) balance method."""
        try:
            rc = self.dex.wait_receipt(tx_hash, attempts=3, delay=4)
            if rc is not None:
                w = self.dex.erc20_delta_from_receipt(rc, self.cfg.wprl_address)
                u = self.dex.erc20_delta_from_receipt(rc, self.cfg.quote_token_address)
                return (w if abs(w) >= Decimal("0.5") else Decimal(0)), u
        except Exception as e:
            emit(f"  WARN: could not decode swap deltas: {str(e)[:80]}")
        return Decimal(0), None   # caller falls back (wprl->intended, usdt->balance)

    def _verify_swap_onchain(self, tx_hash, direction):
        """Positively confirm whether a broadcast swap actually moved WPRL, INDEPENDENT of
        receipt readability. Tries the receipt again (more retries), then falls back to
        alchemy_getAssetTransfers scanning recent WPRL transfers for this exact tx hash.
        Returns signed WPRL delta if confirmed, or None if no on-chain evidence found."""
        if not tx_hash:
            return None
        # 1) one more, more-patient receipt read (handles slow indexing / transient 429)
        try:
            rc = self.dex.wait_receipt(tx_hash, attempts=6, delay=10)
            if rc is not None:
                if rc.status == 0:
                    return Decimal(0)   # confirmed reverted -> no move
                d = self.dex.wprl_delta_from_receipt(rc)
                if abs(d) >= Decimal("0.5"):
                    return d
        except Exception:
            pass
        # 2) backstop: scan WPRL transfers (both directions) for this tx hash
        try:
            txh = tx_hash.lower() if isinstance(tx_hash, str) else tx_hash.hex().lower()
            if not txh.startswith("0x"):
                txh = "0x" + txh
            me = self.dex.address
            wprl = self.cfg.wprl_address
            latest = self.w3.eth.block_number
            frm_block = hex(max(0, latest - 200))
            delta = Decimal(0); found = False
            for keyfield in ("toAddress", "fromAddress"):
                params = {"fromBlock": frm_block, "toBlock": "latest", keyfield: me,
                          "contractAddresses": [wprl], "category": ["erc20"],
                          "withMetadata": False, "order": "desc", "maxCount": "0x64"}
                resp = self.w3.provider.make_request("alchemy_getAssetTransfers", [params])
                for t in (resp.get("result") or {}).get("transfers", []):
                    if (t.get("hash") or "").lower() != txh:
                        continue
                    rc20 = t.get("rawContract") or {}
                    raw = rc20.get("value")
                    if raw is None:
                        continue
                    amt = Decimal(int(raw, 16)) / Decimal(10**8)
                    if keyfield == "toAddress":
                        delta += amt
                    else:
                        delta -= amt
                    found = True
            return delta if found else None
        except Exception as e:
            emit(f"  WARN: on-chain verify failed: {str(e)[:80]}")
            return None

    def adaptive_size(self, m, plan, qty):
        """Marginal-profit size for this snapshot (arb.size_adaptive), logged next to the
        fixed-cap qty. Returns the sizing dict, or None on any error (caller keeps the
        fixed-cap qty — the proven path — and the error is logged, not swallowed)."""
        try:
            mp = {k: getattr(m, k) for k in ("bid1_px", "bid1_qty", "ask1_px", "ask1_qty", "spot",
                                             "cex_balance_prl", "cex_balance_usdt",
                                             "dex_balance_wprl", "dex_balance_quote")}
            t0 = time.time()
            r = arb.size_adaptive(self.cfg, mp, self.dex, plan.direction, self.cfg.min_gap_bps)
            px = mp["bid1_px"] if plan.direction == arb.Direction.CEX_PREMIUM else mp["ask1_px"]
            r.update({"ts": datetime.now(timezone.utc).isoformat(), "secs": round(time.time() - t0, 2),
                      "cex_px": float(px), "spot": float(m.spot), "cap_qty_fixed": float(qty),
                      "cap_exp_pnl_usd": float(plan.expected_pnl_usd), "cap_exp_bps": plan.expected_gap_bps})
            with open(SIZING_LOG, "a") as f:
                f.write(json.dumps(r) + "\n")
            return r
        except Exception as e:
            emit(f"  WARN: adaptive sizing failed ({type(e).__name__} {str(e).split('http')[0][:80]}) -> fixed cap qty")
            return None

    def _book_exposure(self, kind, side, qty):
        """A cleanup leg (reverse/hedge) did NOT fill — record the real residual exposure
        to the ledger + WeChat alert. Per user policy (2026-06-20): alert, do NOT park
        (small single-leg exposure is acceptable; never silently book it as flat)."""
        try:
            with open(LEDGER, "a") as lf:
                lf.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "kind": kind, "side": side, "exposure_qty": float(qty),
                    "realized": 0.0, "ok": False,   # report-compatible schema (marker, not a trade)
                    "note": "cleanup leg short-filled; REAL exposure, NOT flat",
                }) + "\n")
        except Exception as e:
            emit(f"CRIT: trade ledger write failed ({e}) -> PnL/exposure record LOST, check logs/trades.jsonl permissions")
        _alert(f"⚠ PRL {kind}: {side} {float(qty):.4f} 裸敞口未补上(账本已记, 未停机)")

    # ---- hedge residual from the TWO LEGS' own fills (deposit/withdraw-immune) ----
    def flatten_leg_residual(self, residual, px, allowed_tokens=Decimal(0)):
        """residual = (DEX token delta) + (CEX token delta) for THIS trade only.
        For a token-neutral arb it should be ~0; any leftover is hedged on CEX.
        Because it's computed from per-leg fills (not global balances), a deposit
        or withdrawal landing mid-trade cannot pollute it.
        residual > 0  -> we ended net LONG tokens -> sell the excess
        residual < 0  -> net SHORT -> buy it back
        """
        if abs(residual) <= HEDGE_TOL:
            return True
        # a real arb leftover is tiny; a residual near a full trade size means a leg
        # measurement went wrong — refuse rather than trade blindly.
        if abs(residual) * px > max(Decimal(self.cfg.max_trade_usd), allowed_tokens * px * Decimal("1.2")):
            emit(f"  HEDGE ABORT: leg residual {float(residual):+.2f} (${float(abs(residual) * px):.0f}) too large; refusing")
            return False
        side = "sell" if residual > 0 else "buy"
        need = abs(residual)
        emit(f"  HEDGE leg residual {float(residual):+.4f} -> {side} {float(need):.4f}")
        hf, _ = self.cex_fill(side, need)
        short = need - hf
        if short > ALERT_TOL:                                  # ≥10 tok: real exposure -> flag
            emit(f"  ⚠ HEDGE SHORTFALL: filled {float(hf):.4f}/{float(need):.4f}; "
                 f"~{float(short):.4f} still naked (alert + ledger, no park)")
            self._book_exposure("hedge_shortfall", side, short)
            return False                                       # not a clean flat
        if short > HEDGE_TOL:                                  # <10 tok: acceptable, log only, no alert
            emit(f"  hedge under-filled ~{float(short):.4f} (<{float(ALERT_TOL):.0f} tok, acceptable)")
        return True

    # ---- reconciler: settle 'unconfirmed' DEX legs after the truth is on-chain ----
    # dex_swap deliberately does NOT reverse-hedge on an 'unconfirmed' swap (a 429 on the
    # receipt read isn't a failure — reversing a live leg is the churn-loss trap). The
    # cost of that choice: if the swap was ACTUALLY a failure (reverted / dropped), the
    # CEX leg sits naked with nothing to clean it up. This reconciler is that cleanup —
    # it runs at startup and periodically (same thread as the trade loop, so it never
    # races a live trade), verifies each unconfirmed tx on-chain, and reverses the naked
    # CEX leg only once the failure is CONFIRMED.
    def _load_resolved(self):
        s = set()
        if RESOLVED.exists():
            for l in open(RESOLVED):
                l = l.strip()
                if l:
                    try: s.add(json.loads(l)["tx_hash"])
                    except Exception: pass
        return s

    def _mark_resolved(self, txh, verdict, action):
        with open(RESOLVED, "a") as f:
            f.write(json.dumps({"tx_hash": txh, "verdict": verdict, "action": action,
                                "ts": datetime.now(timezone.utc).isoformat()}) + "\n")

    def _entry_age(self, e):
        try:
            return (datetime.now(timezone.utc) - datetime.fromisoformat(e["ts"])).total_seconds()
        except Exception:
            return None

    def _classify_tx(self, txh):
        """Definitively classify a broadcast swap tx:
          'success'  - mined OK (returns signed WPRL delta)
          'reverted' - mined and reverted
          'pending'  - still in the node's mempool (will mine; don't touch)
          'dropped'  - not mined AND not in any mempool (nonce abandoned -> safe to reverse)
        """
        try:
            rc = self.dex.wait_receipt(txh, attempts=3, delay=4)
            if rc is not None:
                if rc.status == 1:
                    return "success", self.dex.wprl_delta_from_receipt(rc)
                return "reverted", Decimal(0)
        except Exception:
            pass
        # no receipt -> is the tx still known to the node (pending) or gone (dropped)?
        try:
            self.w3.eth.get_transaction(txh)   # raises TransactionNotFound if gone
            return "pending", Decimal(0)
        except Exception:
            pass
        # gone from mempool — last check: did it actually move WPRL (mined then pruned)?
        d = self._verify_swap_onchain(txh, None)
        if d is not None and abs(d) >= Decimal("0.5"):
            return "success", d
        return "dropped", Decimal(0)

    def reconcile_unconfirmed(self):
        """Settle every still-open 'unconfirmed' leg in the trade ledger."""
        try:
            if not LEDGER.exists():
                return
            resolved = self._load_resolved()
            pending = []
            for l in open(LEDGER):
                l = l.strip()
                if not l:
                    continue
                try: e = json.loads(l)
                except Exception: continue
                if e.get("outcome") == "unconfirmed" and e.get("tx_hash") and e["tx_hash"] not in resolved:
                    pending.append(e)
            for e in pending:
                txh = e["tx_hash"]; qty = Decimal(str(e.get("qty") or 0)); direction = e.get("dir")
                if e.get("deadline") and time.time() < float(e["deadline"]) + 60:
                    continue   # privately relayed: absent from the public mempool by design; wait out the deadline
                verdict, delta = self._classify_tx(txh)
                if verdict == "success":
                    emit(f"  RECONCILE: unconfirmed {txh[:14]} VERIFIED executed (delta {float(delta):+.2f}) "
                         f"-> was balanced, no action")
                    self._mark_resolved(txh, "success", "none")
                    continue
                if verdict == "pending":
                    continue   # still in mempool, will mine — leave it
                if verdict == "dropped":
                    age = self._entry_age(e)
                    if age is not None and age < UNCONFIRMED_DROP_SEC:
                        continue   # just broadcast, give propagation a moment
                # reverted or dropped -> CEX leg never got offset -> reverse it now
                if qty < Decimal("0.5"):
                    self._mark_resolved(txh, verdict, "skip-dust"); continue
                rev_side = "buy" if direction == "cex_premium" else "sell"
                emit(f"  RECONCILE: unconfirmed {txh[:14]} {verdict.upper()} -> CEX leg was naked, "
                     f"reversing {rev_side} {float(qty):.4f}")
                try:
                    rf, _ = self.cex_fill(rev_side, qty)
                    if qty - rf > ALERT_TOL:                       # ≥10 tok: real exposure -> flag
                        short = qty - rf
                        emit(f"  ⚠ RECONCILE reverse SHORTFALL: {float(rf):.4f}/{float(qty):.4f}; "
                             f"~{float(short):.4f} still naked (alert + ledger, no park)")
                        self._book_exposure("reconcile_reverse_shortfall", rev_side, short)
                    # mark resolved with the actual fill so a retry can't re-reverse the full qty
                    self._mark_resolved(txh, verdict, f"reversed {rev_side} {float(rf):.4f}/{float(qty):.4f}")
                except Exception as ex:
                    emit(f"  RECONCILE hedge ERR (retry next pass): {str(ex)[:100]}")
        except Exception as e:
            emit(f"  reconcile error: {str(e)[:120]}")

    def _mark_exposure(self, key, filled_amt, done, note=None):
        rec = {"key": key, "filled": float(filled_amt), "done": bool(done),
               "ts": datetime.now(timezone.utc).isoformat()}
        if note:
            rec["note"] = note
        with open(EXP_RESOLVED, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def reclose_exposures(self):
        """Retry-close naked exposures booked by _book_exposure (cleanup legs that
        short-filled). Before this, a shortfall got ONE 3s window and was then left
        standing forever — alert-only, nothing retried (2026-06-30: reverse buy filled
        54/454, the 400 PRL sold on CEX was never bought back; token conservation
        drifted until the supply guard froze). Each pass re-attempts the UNFILLED
        remainder at the current touch; cumulative fills are journaled to EXP_RESOLVED
        so a restart can never re-close more than the true remainder. This completes
        the reverse the trade already owed — it is not a park."""
        try:
            if not LEDGER.exists():
                return
            filled = {}
            done = set()
            if EXP_RESOLVED.exists():
                for l in open(EXP_RESOLVED):
                    l = l.strip()
                    if not l:
                        continue
                    try:
                        e = json.loads(l)
                    except Exception:
                        continue
                    k = e.get("key")
                    filled[k] = filled.get(k, Decimal(0)) + Decimal(str(e.get("filled") or 0))
                    if e.get("done"):
                        done.add(k)
            for l in open(LEDGER):
                l = l.strip()
                if not l:
                    continue
                try:
                    e = json.loads(l)
                except Exception:
                    continue
                if not e.get("kind") or "exposure_qty" not in e or "side" not in e:
                    continue
                key = f'{e["ts"]}|{e["kind"]}'
                if key in done:
                    continue
                need = Decimal(str(e["exposure_qty"])) - filled.get(key, Decimal(0))
                if need <= HEDGE_TOL:
                    self._mark_exposure(key, 0, True, note="remainder within tolerance")
                    continue
                side = e["side"]
                emit(f"  RECLOSE {e['kind']}: {side} {float(need):.4f} still naked -> retry at touch")
                f, _ = self.cex_fill(side, qd(need, self.aprec))
                if f <= 0:
                    continue   # no fill this pass; next reconcile retries
                remaining = need - f
                closed = remaining <= HEDGE_TOL
                self._mark_exposure(key, f, closed)
                if closed:
                    emit(f"  RECLOSE {e['kind']}: CLOSED (this pass {side} {float(f):.4f}, "
                         f"remainder {float(remaining):.4f})")
                    _alert(f"PRL 敞口已补平: {e['kind']} {side} 累计补回, 残余 {float(remaining):.2f} 在容差内")
                else:
                    emit(f"  RECLOSE {e['kind']}: partial {float(f):.4f}, {float(remaining):.4f} left")
        except Exception as e:
            emit(f"  reclose error: {str(e)[:120]}")

    def cancel_stale_orders(self):
        """auto_run uses marketable limits and cancels remainders inside cex_fill, so at
        the top of a loop iteration there should be NO resting order. If one exists, an API
        hiccup orphaned it — cancel it before it fills later into an unaccounted naked leg.
        INVARIANT: this SafeTrade market is bot-exclusive — no manual/other-strategy orders
        while auto_run is live (this cancels every resting order on the market)."""
        if self.cfg.dry_run:
            return   # never mutate the account in dry-run (it may be a shared/manual account)
        try:
            for o in self.st.open_orders(self.cfg.safetrade_market):
                oid = o.get("id")
                emit(f"  RECONCILE: stale open order {oid} ({o.get('side')}) -> cancel")
                try: self.st.cancel_order(oid)
                except Exception: pass
        except Exception:
            pass

    def _gas_sufficient(self):
        """True if the EVM wallet can afford a DEX swap. The node rejects a tx PRE-broadcast
        unless balance >= gasLimit * maxFeePerGas (it reserves the worst case, even though
        actual gas used is far less). If we fire the CEX leg first and THEN the swap bounces
        for gas, we churn (CEX fill -> reverse) with zero chance of completing the arb. So
        check gas BEFORE firing and skip if short — no CEX leg, no churn. Mirrors the
        gas params in dex.swap_exact_in (gasLimit 250k, maxFee = gas_price*3 + 1 gwei)."""
        try:
            bal = self.w3.eth.get_balance(self.dex.address)
            gp = self.w3.eth.gas_price
            need = 250_000 * (gp * 3 + self.w3.to_wei(1, "gwei"))
            return bal >= int(need * 1.1), bal, need
        except Exception:
            return True, 0, 0   # can't check (RPC) -> don't block here; swap's own path handles it

    def run(self):
        emit(f"mode: {'DRY RUN (no orders, no swaps)' if self.cfg.dry_run else 'LIVE'}  "
             f"stop file: {self.cfg.kill_switch_file}  base shot: ${self.cfg.max_trade_usd:.0f}  "
             f"hard cap: {('$%.0f' % self.cfg.hard_max_trade_usd) if self.cfg.hard_max_trade_usd > 0 else 'none'}")
        # Halt BEFORE any network work. The in-loop check below runs only after bal() +
        # startup reconcile, so a halted engine under pm2 still hammered SafeTrade and the
        # RPC once every ~12s (thousands of restarts during one false halt).
        if Path(self.cfg.kill_switch_file).exists():
            emit("STOP file present -> halt (pre-boot)")
            return
        if not self.cfg.dry_run:
            # An insufficient router allowance means the CEX leg would fill and the DEX leg
            # would revert on the first shot. Refuse to start rather than churn. We expect
            # the unlimited approve that scripts/approve_router.py grants.
            for tok, name in ((self.cfg.wprl_address, "WPRL"),
                              (self.cfg.quote_token_address, self.cfg.quote_token_symbol)):
                if self.dex.allowance(tok) < ALLOWANCE_FLOOR:
                    emit(f"ABORT: Uniswap router allowance for {name} is below the unlimited approve -> run scripts/approve_router.py first")
                    return 4

        b0 = self.bal()
        start_tokens = self.tokens(b0)
        start_usd = self.usd(b0)
        emit(f"=== AUTO RUN start. target {TARGET_SUCCESS} successes, min_gap {self.cfg.min_gap_bps}bps, "
             f"max_trade ${self.cfg.max_trade_usd} ===")
        emit(f"start tokens {float(start_tokens):.2f}  usd {float(start_usd):.2f}")
        emit(f"sizing: marginal>={self.cfg.min_gap_bps}bps {'ON' if ADAPTIVE_SIZING else 'OFF'} | fixed slippage "
             f"{self.cfg.slippage_bps}bps, profit-derived min-out via private relay {'ON' if self.private_ok else 'OFF'} "
             f"(deadline {PRIVATE_DEADLINE_SEC}s, tip {PRIVATE_TIP_GWEI} gwei)")

        # STARTUP reconcile: clean up anything a prior crash/restart left dangling
        # (orphaned orders, unconfirmed legs) BEFORE trading fresh.
        emit("startup reconcile…")
        self.cancel_stale_orders()
        self.reconcile_unconfirmed()
        self.reclose_exposures()
        last_reconcile = time.time()
        last_lowgas_log = 0.0

        success = 0
        completed = 0
        cum = Decimal(0)
        cum_revert = Decimal(0)   # CEX round-trip cost of reverted DEX legs (outside CUM_LOSS_STOP)
        revert_alerted = False
        small_after_revert = False
        deadline = time.time() + TIME_BUDGET_SEC
        rounds = 0

        while success < TARGET_SUCCESS:
            rounds += 1
            # PERIODIC reconcile (runs between trades, never mid-trade -> no race)
            if time.time() - last_reconcile > RECONCILE_EVERY_SEC:
                self.reconcile_unconfirmed()
                self.cancel_stale_orders()
                self.reclose_exposures()
                last_reconcile = time.time()
            if Path(self.cfg.kill_switch_file).exists():
                emit("STOP file present -> halt"); break
            if time.time() > deadline:
                emit("time budget exhausted -> halt"); break
            if cum <= CUM_LOSS_STOP:
                emit(f"CUM LOSS {float(cum):.2f} <= {CUM_LOSS_STOP} -> permanent halt")
                # SERVER/systemd guard: write the .STOP file so a restart re-halts immediately
                # (systemd StartLimitBurst then parks the unit in failed state) instead of
                # blindly resuming trading into a real loss. Investigate leg-based PnL first;
                # if it's a false balance-based halt, delete .STOP and restart. (Deploy-only
                # addition vs the Mac dev copy.)
                try:
                    Path(self.cfg.kill_switch_file).write_text(
                        "CUM_LOSS permanent halt — verify with report.py (LEG-BASED, not balance). "
                        "Delete this file to resume only after confirming it was a false/accounting halt.\n")
                except Exception as e:
                    emit(f"CRIT: could not write stop file {self.cfg.kill_switch_file} ({e}); "
                         "halting IN PLACE so a supervisor restart cannot resume trading")
                    _alert(f"PRL CUM_LOSS halt: stop file write failed ({str(e)[:80]}); process holding, fix by hand")
                    while True:
                        time.sleep(3600)
                break
            if completed >= MAX_COMPLETED:
                emit(f"reached {MAX_COMPLETED} completed trades -> halt"); break

            try:
                m = arb.snapshot_market(self.cfg, self.st, self.dex)
                plan = self.engine.evaluate(m)
            except Exception as e:
                emit(f"snapshot error: {str(e)[:120]}"); time.sleep(self.cfg.poll_interval_sec); continue

            if plan.direction == arb.Direction.NONE:
                time.sleep(self.cfg.poll_interval_sec); continue

            qty = qd(plan.base_qty, self.aprec)
            if qty <= 0:
                time.sleep(self.cfg.poll_interval_sec); continue

            # GAS GUARD: don't fire the CEX leg if the EVM wallet can't afford the DEX leg
            # (else: CEX fills -> swap bounces for gas -> reverse -> churn, no arb done).
            gas_ok, gbal, gneed = self._gas_sufficient()
            if not gas_ok:
                if time.time() - last_lowgas_log > 60:
                    emit(f"  LOW GAS: EVM {float(self.w3.from_wei(gbal,'ether')):.6f} ETH < "
                         f"~{float(self.w3.from_wei(gneed,'ether')):.6f} needed for a swap — SKIP (no churn). "
                         f"Top up ETH to {self.dex.address}")
                    last_lowgas_log = time.time()
                time.sleep(self.cfg.poll_interval_sec); continue

            # Snapshot ONLY the deposit/withdraw-immune quantities:
            #   EVM WPRL + EVM USDT (CEX deposits never touch these)
            #   USDT snapshots for PnL; legs measured from their own execution records.
            evm_usdt_0 = self.dex.balance(self.cfg.quote_token_address)
            cex_usdt_0 = self.st.balance("usdt")
            # Marginal sizing only ever scales the shot UP from the fixed-cap qty that already
            # cleared the thresholds. After a reverted DEX leg the next shot goes back to the
            # fixed cap until one succeeds, so back-to-back reverts can't repeat at full size.
            exp_bps, exp_pnl = plan.expected_gap_bps, plan.expected_pnl_usd
            if ADAPTIVE_SIZING:
                sized = self.adaptive_size(m, plan, qty)
                if sized and qd(sized["qty"], self.aprec) > qty:
                    if small_after_revert:
                        emit(f"  sizing: marginal qty {sized['qty']:.0f} held at fixed cap {float(qty):.0f} (last DEX leg reverted)")
                    else:
                        emit(f"  sizing: fixed cap {float(qty):.0f} -> {sized['qty']:.0f} (last token {sized['marginal_bps']:.0f}bps, limit {sized['cap']})")
                        qty = qd(sized["qty"], self.aprec)
                        exp_bps, exp_pnl = int(sized["avg_bps"]), Decimal(str(sized["exp_pnl_usd"]))
            if self.cfg.hard_max_trade_usd > 0:
                px_ref = m.bid1_px if plan.direction == arb.Direction.CEX_PREMIUM else m.ask1_px
                cap_qty = qd(Decimal(str(self.cfg.hard_max_trade_usd)) / Decimal(str(px_ref)), self.aprec)
                if cap_qty <= 0:
                    emit(f"  sizing: HARD_MAX_TRADE_USD={self.cfg.hard_max_trade_usd} rounds to zero qty -> SKIP (never trade uncapped)")
                    time.sleep(self.cfg.poll_interval_sec); continue
                if qty > cap_qty:
                    # Shrinking the shot shrinks its profit; the expected values were computed
                    # for the larger size, so scale them and re-apply the profit floor. The
                    # linear scale understates a smaller shot's edge (marginal bps only improve
                    # as size drops), so this check is conservative.
                    scale = cap_qty / qty
                    exp_pnl = Decimal(str(exp_pnl)) * scale
                    emit(f"  sizing: {float(qty):.0f} capped to {float(cap_qty):.0f} by HARD_MAX_TRADE_USD={self.cfg.hard_max_trade_usd:.0f} "
                         f"(exp_pnl scaled to ${float(exp_pnl):.2f})")
                    qty = cap_qty
                    if exp_pnl < Decimal(str(self.cfg.min_trade_pnl_usd)):
                        emit(f"  sizing: capped shot exp_pnl ${float(exp_pnl):.2f} < MIN_TRADE_PNL_USD {self.cfg.min_trade_pnl_usd} -> SKIP")
                        time.sleep(self.cfg.poll_interval_sec); continue
            if self.cfg.dry_run:
                emit(f"DRY RUN: would fire {plan.direction.value} qty={float(qty):.4f} exp_net={exp_bps}bps "
                     f"exp_pnl=${float(exp_pnl):.2f} (set DRY_RUN=false to trade)")
                time.sleep(self.cfg.poll_interval_sec); continue
            emit(f"FIRING {plan.direction.value} qty={float(qty):.4f} exp_net={exp_bps}bps exp_pnl=${float(exp_pnl):.2f}")

            # leg 1: CEX (failable, synchronous). filled = order filled_amount (clean).
            # Place EXACTLY at the snapshot price the edge was computed from (cross=0): sell
            # PRL @ bid1, buy PRL @ ask1. Fills at that price or better, never walks a thin/
            # moving book. If the level moved away or depth < qty -> partial/no fill, cancel
            # after 3s; the DEX leg sizes to the ACTUAL fill, so it's a clean smaller arb
            # instead of a full-but-slipped one.
            cex_side = "sell" if plan.direction == arb.Direction.CEX_PREMIUM else "buy"
            target_px = m.ask1_px if cex_side == "buy" else m.bid1_px
            filled, cex_oid = self.cex_fill(cex_side, qty, ref_px=target_px, cross_bps=0)
            if filled < Decimal("0.5"):
                emit(f"  CEX leg did not fill @ {float(target_px):.4f} (level moved/thin); skipping, no exposure")
                time.sleep(self.cfg.poll_interval_sec); continue
            emit(f"  CEX {cex_side} filled {float(filled):.4f} @ ~{float(target_px):.4f}")

            # leg 2: DEX (irreversible), sized to actual CEX fill
            dex_amt = qd(filled, 4)
            swap = self.dex_swap(plan.direction, dex_amt, econ={"bid": m.bid1_px, "ask": m.ask1_px})
            ok = swap["ok"]
            outcome = swap.get("outcome")
            dex_token_delta = swap["wprl_delta"]   # signed, decoded from tx receipt (no race)
            if not ok:
                # Reverse the CEX leg ONLY when the DEX leg DEFINITELY did not execute
                # (mined-and-reverted, or never broadcast). On 'unconfirmed' the swap may
                # have actually gone through (the 429 false-fail that booked a phantom
                # -$494 and reverse-hedged a live position) — do NOT reverse blindly;
                # leave it for snapshot/supply reconciliation and flag it loudly.
                if outcome in ("reverted", "not_sent", "expired"):
                    emit(f"  DEX leg FAILED ({outcome}) -> reversing CEX leg to avoid exposure")
                    rev = "buy" if cex_side == "sell" else "sell"
                    rf, rev_oid = self.cex_fill(rev, filled)
                    rev_short = filled - rf
                    if rev_short > ALERT_TOL:                       # ≥10 tok: real exposure -> flag
                        emit(f"  ⚠ REVERSE SHORTFALL: {float(rf):.4f}/{float(filled):.4f}; "
                             f"~{float(rev_short):.4f} still naked (alert + ledger, no park)")
                        self._book_exposure("reverse_shortfall", rev, rev_short)
                    # Cost of the round trip on CEX, from the two orders' own trades. Booked to
                    # the ledger and a separate running total — NOT into `cum`: a revert is a
                    # known, priced-in cost of firing, not the anomaly CUM_LOSS_STOP guards.
                    revert_cost = None
                    if rev_short <= ALERT_TOL:
                        u0 = self._order_usdt(cex_oid, cex_side)
                        u1 = self._order_usdt(rev_oid, rev)
                        if u0 is not None and u1 is not None:
                            revert_cost = u0 + u1
                            cum_revert += revert_cost
                            emit(f"  revert cost ${float(revert_cost):+.2f}  cum revert ${float(cum_revert):+.2f}")
                    if revert_cost is None:
                        emit("  revert cost UNKNOWN (order trades unreadable or reverse short) — not booked")
                    small_after_revert = True
                    if cum_revert <= REVERT_ALERT_USD and not revert_alerted:
                        revert_alerted = True
                        _alert(f"PRL 链上回滚的买回成本本轮累计 {float(cum_revert):+.2f} U (不计入停机线)")
                    # Position is FLAT again after the reverse. Do NOT fall through to the
                    # realized/PnL math: residual would be ±filled (dex_delta=0) and
                    # residual*spot is a PHANTOM number — that's exactly what booked a fake
                    # -$517 and tripped CUM_LOSS into a FALSE permanent halt. The real churn
                    # cost (2 taker fees) is captured by true PnL (report.py), not invented here.
                    try:
                        with open(LEDGER, "a") as lf:
                            lf.write(json.dumps({
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "dir": plan.direction.value, "qty": float(filled),
                                "outcome": outcome,
                                "revert_cost_usd": (None if revert_cost is None else float(revert_cost)),
                                "note": ("DEX leg failed; CEX reversed; flat" if rev_short <= ALERT_TOL
                                         else f"DEX failed; CEX reverse PARTIAL, ~{float(rev_short):.4f} still exposed"),
                            }) + "\n")
                    except Exception as e:
                        emit(f"CRIT: trade ledger write failed ({e}) -> PnL/exposure record LOST, check logs/trades.jsonl permissions")
                    time.sleep(self.cfg.poll_interval_sec)
                    continue
                else:  # 'unconfirmed'
                    emit(f"  ⚠ DEX leg UNCONFIRMED -> NOT reversing CEX (avoid churn); "
                         f"CEX {cex_side} {float(filled):.4f} stands, tx={str(swap.get('tx_hash'))[:14]} — "
                         f"reconcile via next snapshot/supply")
                    small_after_revert = True
                    if filled * target_px > Decimal(self.cfg.max_trade_usd) * Decimal("1.5"):
                        _alert(f"PRL 链上腿结果未知: CEX 已{cex_side} {float(filled):.0f} 枚, 链上交易未确认, 引擎每 2 分钟自动复核")
                    # Outcome genuinely unknown -> skip residual/hedge/realized accounting
                    # (any number we compute here would be phantom, like the old -$494).
                    # Don't touch cum/counters; just record a marker and move on.
                    try:
                        with open(LEDGER, "a") as lf:
                            lf.write(json.dumps({
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "dir": plan.direction.value, "qty": float(filled),
                                "outcome": "unconfirmed", "tx_hash": str(swap.get("tx_hash")),
                                "deadline": swap.get("deadline"),
                                "note": "DEX receipt unreadable + no on-chain confirm; not reversed",
                            }) + "\n")
                    except Exception as e:
                        emit(f"CRIT: trade ledger write failed ({e}) -> PnL/exposure record LOST, check logs/trades.jsonl permissions")
                    time.sleep(self.cfg.poll_interval_sec)
                    continue

            # FALLBACK: DEX leg succeeded but decode returned ~0 -> decode hiccup, not a
            # real zero. We KNOW we swapped ~dex_amt, so trust the intended signed amount.
            if ok and abs(dex_token_delta) < Decimal("0.5"):
                signed = dex_amt if plan.direction == arb.Direction.CEX_PREMIUM else -dex_amt
                emit(f"  (wprl_delta decode ~0; using intended {float(signed):+.4f})")
                dex_token_delta = signed

            # residual from each leg's OWN execution record (receipt + order), not balances:
            cex_token_delta = filled if cex_side == "buy" else -filled
            residual = dex_token_delta + cex_token_delta   # token-neutral arb -> ~0
            hedged_ok = self.flatten_leg_residual(residual, target_px, swap.get("tol_tokens") or Decimal(0))

            # realized PnL from EXECUTION RECORDS, not balances — immune to concurrent
            # supply/bridge USDT moves (the balance-delta method booked a large phantom loss from
            # a mid-trade EVM->CEX transfer and tripped a FALSE permanent halt):
            #   CEX leg USDT: the order's trades (sell -> +received, buy -> -spent)
            #   DEX leg USDT: the swap receipt's USDT Transfer (signed)
            #   + any tiny leftover token (residual) at spot — the hedge converts it to USDT
            spot = m.spot
            cex_usdt = self._order_usdt(cex_oid, cex_side)
            dex_usdt = swap.get("usdt_delta")
            if cex_usdt is not None and dex_usdt is not None:
                realized = cex_usdt + dex_usdt + residual * spot
            else:
                # FALLBACK (trades/decode unavailable, or rare verified-unconfirmed path):
                # balance deltas WITH a pollution guard so a concurrent transfer can't fake
                # a huge loss. True PnL always lives in report.py (full balances).
                evm_usdt_1 = self.dex.balance(self.cfg.quote_token_address)
                cex_usdt_1 = self.st.balance("usdt")
                usdt_delta = (evm_usdt_1 - evm_usdt_0) + (cex_usdt_1 - cex_usdt_0)
                realized = usdt_delta + residual * spot
                cap = max(Decimal(self.cfg.max_trade_usd) * Decimal("1.5"), filled * target_px * Decimal("0.3"))
                if abs(realized) > cap:
                    emit(f"  ⚠ realized ${float(realized):+.2f} > ${float(cap):.0f} (balance-delta pollution) -> booking 0")
                    realized = Decimal(0)
            cum += realized
            completed += 1
            tag = ""
            if ok:
                small_after_revert = False
            if ok and hedged_ok and realized > 0:
                success += 1
                tag = f"  ✓ SUCCESS {success}/{TARGET_SUCCESS}"
            elif ok and not hedged_ok:
                tag = "  ⚠ COMPLETED w/ UNHEDGED EXPOSURE (see ledger, NOT counted clean)"
            elif ok:
                tag = "  (completed, not profitable)"
            else:
                tag = "  (failed/hedged)"
            emit(f"  realized ${float(realized):+.2f}  cum ${float(cum):+.2f}{tag}")
            # append-only trade ledger — survives restarts/log clears, source for reports
            try:
                with open(LEDGER, "a") as lf:
                    lf.write(json.dumps({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "dir": plan.direction.value, "qty": float(filled),
                        "realized": float(realized), "ok": bool(ok), "cleanup_ok": bool(hedged_ok),
                        "cex_usdt": (None if cex_usdt is None else float(cex_usdt)),
                        "dex_usdt": (None if dex_usdt is None else float(dex_usdt)),
                        "residual_token": float(residual), "spot": float(spot),
                    }) + "\n")
            except Exception as e:
                emit(f"CRIT: trade ledger write failed ({e}) -> PnL/exposure record LOST, check logs/trades.jsonl permissions")

        bf = self.bal()
        emit(f"=== DONE: {success}/{TARGET_SUCCESS} successes, {completed} completed, cum ${float(cum):+.2f} ===")
        emit(f"end tokens {float(self.tokens(bf)):.2f} (start {float(start_tokens):.2f})  "
             f"usd {float(self.usd(bf)):.2f} (start {float(start_usd):.2f})")
        spot_f = self.dex.wprl_price_in_quote()
        pnl = (self.usd(bf) - start_usd) + (self.tokens(bf) - start_tokens) * spot_f
        emit(f"portfolio net P&L ~${float(pnl):+.2f}")
        return 0 if success >= TARGET_SUCCESS else 1


def acquire_singleton_lock():
    """Refuse to start if another auto_run is already alive (prevents double-fills
    AND the duplicate-log-line symptom). PID file under logs/."""
    import os, errno
    lockfile = Path(__file__).parent.parent / "logs" / "auto_run.pid"
    lockfile.parent.mkdir(exist_ok=True)
    if lockfile.exists():
        try:
            old = int(lockfile.read_text().strip())
            os.kill(old, 0)   # raises if pid is dead
            emit(f"ABORT: another auto_run is alive (pid {old}). Refusing to start a 2nd.")
            sys.exit(3)
        except (ValueError, ProcessLookupError):
            pass  # stale pid file -> overwrite
        except PermissionError:
            emit(f"ABORT: pid {old} alive (perm). Refusing 2nd."); sys.exit(3)
    lockfile.write_text(str(os.getpid()))
    return lockfile


if __name__ == "__main__":
    _lock = acquire_singleton_lock()
    try:
        sys.exit(Runner().run())
    finally:
        try: _lock.unlink()
        except Exception: pass
