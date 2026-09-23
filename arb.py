"""Cross-venue DEX-CEX arbitrage — dynamic top-of-book sizing.

Each tick: target = eat the CEX top-of-book quantity, match that qty against the
DEX via Quoter (exact-out for the buy leg, exact-in for the sell leg). Trade is
token-neutral (sell N on the rich venue, buy N on the cheap venue); profit is the
USD spread. min_gap_bps is the safety margin that covers ~12s execution drift.

Directions (named by which venue is more expensive):
  CEX_PREMIUM: CEX bid > DEX cost  → SELL PRL on CEX @ bid1, BUY WPRL on DEX
               consumes: CEX PRL, EVM quote(USDT) ; refill via WPRL→PRL bridge (free)
  DEX_PREMIUM: DEX proceeds > CEX ask → SELL WPRL on DEX, BUY PRL on CEX @ ask1
               consumes: EVM WPRL, CEX USDT ; refill via PRL→WPRL bridge (0.5%)

If the gap oscillates between the two, inventory self-balances and no bridge is needed.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class Direction(str, Enum):
    CEX_PREMIUM = "cex_premium"
    DEX_PREMIUM = "dex_premium"
    NONE = "none"


# --- live gas pricing for the per-trade profit floor: min_pnl = max(min_trade_pnl, gas) ---
# A trade's only on-chain leg is ONE Uniswap V3 swap; the CEX legs are API (no gas). We price
# that swap live (gas_price x gas_units x ETH/USD) and require pnl to clear it, so a marginal
# trade can never net-negative after gas — replaces the old fixed "$1 assumes gas~$0.03" floor.
_CHAINLINK_ETH_USD = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419"  # mainnet ETH/USD feed
_DEX_SWAP_GAS = 200_000        # conservative single exactOutput swap (errs high = safer floor)
_GAS_CACHE_TTL = 30            # s; gas price + ETH/USD don't move much intra-minute
_GAS_FALLBACK_USD = Decimal("1.5")  # used only if RPC can't price gas AND no cached value yet
_gas_cache = {"ts": 0.0, "usd": None}


def gas_cost_usd(dex) -> Decimal:
    """Live USD cost of the one on-chain swap per trade. Cached briefly. On RPC failure
    returns the last good value (or a conservative fallback) so a transient blip TIGHTENS
    the floor rather than removing it."""
    import time as _t
    now = _t.time()
    if _gas_cache["usd"] is not None and now - _gas_cache["ts"] < _GAS_CACHE_TTL:
        return _gas_cache["usd"]
    try:
        w3 = dex.w3
        gp = w3.eth.gas_price                          # wei per gas
        feed = w3.eth.contract(
            address=w3.to_checksum_address(_CHAINLINK_ETH_USD),
            abi=[{"inputs": [], "name": "latestAnswer", "outputs": [{"type": "int256"}],
                  "stateMutability": "view", "type": "function"}])
        eth_usd = Decimal(feed.functions.latestAnswer().call()) / Decimal(10**8)
        usd = Decimal(gp) * Decimal(_DEX_SWAP_GAS) / Decimal(10**18) * eth_usd
        _gas_cache.update(ts=now, usd=usd)
        return usd
    except Exception:
        return _gas_cache["usd"] if _gas_cache["usd"] is not None else _GAS_FALLBACK_USD


@dataclass
class Candidate:
    direction: Direction
    qty: Decimal = Decimal(0)          # base qty (PRL == WPRL units)
    proceeds_usd: Decimal = Decimal(0)
    cost_usd: Decimal = Decimal(0)
    net_bps: Decimal = Decimal(-99999)
    pnl_usd: Decimal = Decimal(0)
    dex_quote_amount: Decimal = Decimal(0)  # USDT spent (buy) or received (sell) on DEX
    reason: str = ""


@dataclass
class MarketView:
    bid1_px: Decimal
    bid1_qty: Decimal
    ask1_px: Decimal
    ask1_qty: Decimal
    spot: Decimal
    cex_balance_prl: Decimal
    cex_balance_usdt: Decimal
    dex_balance_wprl: Decimal
    dex_balance_quote: Decimal
    cex_prem: Candidate
    dex_prem: Candidate
    inflight_to_cex: Decimal = Decimal(0)   # WPRL->PRL deposits en route to CEX (not yet credited)
    inflight_to_evm: Decimal = Decimal(0)   # PRL->WPRL left CEX, WPRL not yet minted on EVM
    gas_usd: Decimal = Decimal(0)           # live USD cost of the trade's one on-chain swap


@dataclass
class TradePlan:
    direction: Direction
    notional_usd: Decimal
    base_qty: Decimal
    expected_gap_bps: int
    expected_pnl_usd: Decimal
    dex_quote_amount: Decimal
    reason: str


def _hard_caps(cfg, m_partial, direction) -> dict:
    """Quantity limits that come from the book and inventory (no per-trade USD ceiling)."""
    if direction == Direction.CEX_PREMIUM:
        return {
            "top_of_book": m_partial["bid1_qty"],
            "cex_prl_inv": m_partial["cex_balance_prl"] - Decimal(cfg.min_prl_inventory),
            "evm_quote_inv": (m_partial["dex_balance_quote"] - Decimal(cfg.min_usdc_inventory)) / m_partial["spot"]
                             if m_partial["spot"] > 0 else Decimal(0),
            "evm_wprl_room": Decimal(cfg.max_wprl_inventory) - m_partial["dex_balance_wprl"],
        }
    Pa = m_partial["ask1_px"]
    return {
        "top_of_book": m_partial["ask1_qty"],
        "evm_wprl_inv": m_partial["dex_balance_wprl"] - Decimal(cfg.min_wprl_inventory),
        "cex_usdt_inv": (m_partial["cex_balance_usdt"] - Decimal(cfg.min_usdt_inventory)) / Pa
                        if Pa > 0 else Decimal(0),
        "cex_prl_room": Decimal(cfg.max_prl_inventory) - m_partial["cex_balance_prl"],
    }


def size_adaptive(cfg, m_partial, dex, direction, th_bps, probes: int = 7) -> dict:
    """Largest qty whose LAST token still nets >= th_bps, within book/inventory limits.

    CEX side is one flat level (bid1/ask1, the main leg never walks the book); the DEX
    side is the pool curve, whose marginal price is the pool price AFTER the swap
    (QuoterV2 sqrtPriceX96After) adjusted for the pool fee. Marginal edge per token:
      CEX_PREMIUM: bid1*(1-taker) - price_after/(1-fee)       (restock bridge: WPRL->PRL)
      DEX_PREMIUM: price_after*(1-fee) - ask1*(1+taker)       (restock bridge: PRL->WPRL)
    minus the restock bridge fee on the token's value. Edge falls as qty grows, so
    bisect. Returns a dict for logging; qty 0 with a reason when nothing qualifies."""
    caps = _hard_caps(cfg, m_partial, direction)
    cap_name = min(caps, key=caps.get)
    hi = caps[cap_name]
    out = {"dir": direction.value, "cap": cap_name, "cap_qty": float(max(hi, Decimal(0))),
           "th_bps": float(th_bps), "qty": 0.0}
    if hi <= 0:
        out["reason"] = "capped@0: " + cap_name
        return out
    taker = Decimal(cfg.safetrade_taker_fee_bps) / Decimal(10000)
    fee = Decimal(cfg.pool_fee_tier) / Decimal(1_000_000)
    th = Decimal(th_bps) / Decimal(10000)
    if direction == Direction.CEX_PREMIUM:
        px = m_partial["bid1_px"]
        bridge = Decimal(cfg.bridge_fee_bps_wprl_to_prl) / Decimal(10000)
        budget = m_partial["dex_balance_quote"] - Decimal(cfg.min_usdc_inventory)
    else:
        px = m_partial["ask1_px"]
        bridge = Decimal(cfg.bridge_fee_bps_prl_to_wprl) / Decimal(10000)
        budget = None

    def probe(q):
        if direction == Direction.CEX_PREMIUM:
            dex_usdt, p_after = dex.quote_buy_wprl_full(q)
            pnl = q * px * (Decimal(1) - taker) - dex_usdt
            marginal = px * (Decimal(1) - taker) - p_after / (Decimal(1) - fee)
            affordable = dex_usdt <= budget
        else:
            dex_usdt, p_after = dex.quote_sell_wprl_full(q)
            pnl = dex_usdt - q * px * (Decimal(1) + taker)
            marginal = p_after * (Decimal(1) - fee) - px * (Decimal(1) + taker)
            affordable = True
        m_bps = (marginal / px - bridge) if px > 0 else Decimal(-1)
        return {"qty": q, "dex_usdt": dex_usdt, "pnl": pnl - q * px * bridge,
                "marginal_bps": m_bps * Decimal(10000), "ok": affordable and m_bps >= th}

    best = None
    top = probe(hi)
    n = 1
    if top["ok"]:
        best = top
    else:
        lo, up = Decimal(0), hi
        for _ in range(probes):
            mid = (lo + up) / 2
            r = probe(mid)
            n += 1
            if r["ok"]:
                best, lo = r, mid
            else:
                up = mid
    out["probes"] = n
    out["cap_marginal_bps"] = float(top["marginal_bps"])
    if best is None:
        out["reason"] = "first token below threshold"
        return out
    notional = best["qty"] * px
    out.update({"qty": float(best["qty"]), "notional_usd": float(notional),
                "dex_usdt": float(best["dex_usdt"]), "exp_pnl_usd": float(best["pnl"]),
                "avg_bps": float(best["pnl"] / notional * Decimal(10000)) if notional > 0 else None,
                "marginal_bps": float(best["marginal_bps"]),
                "at_cap": best is top})
    return out


def _size_cex_premium(cfg, m_partial, dex) -> Candidate:
    """Sell PRL on CEX @ bid1, buy equal WPRL on DEX. Token-neutral, profit=USD spread."""
    c = Candidate(Direction.CEX_PREMIUM)
    Pb = m_partial["bid1_px"]
    taker = Decimal(cfg.safetrade_taker_fee_bps) / Decimal(10000)
    ceiling_qty = Decimal(cfg.max_trade_usd) / Pb if Pb > 0 else Decimal(0)

    caps = {"ceiling": ceiling_qty, **_hard_caps(cfg, m_partial, Direction.CEX_PREMIUM)}
    qty = min(caps.values())
    if qty <= 0:
        c.reason = "capped@0: " + min(caps, key=caps.get)
        return c

    proceeds = qty * Pb * (Decimal(1) - taker)              # USDT received on CEX
    cost = dex.quote_exact_out(cfg.quote_token_address, cfg.wprl_address, qty)  # USDT to buy qty WPRL
    pnl = proceeds - cost
    notional = qty * Pb
    c.qty, c.proceeds_usd, c.cost_usd, c.dex_quote_amount = qty, proceeds, cost, cost
    c.pnl_usd = pnl
    c.net_bps = pnl / notional * Decimal(10000) if notional > 0 else Decimal(-99999)
    c.reason = f"qty {float(qty):.0f} (cap={min(caps, key=caps.get)})"
    return c


def _size_dex_premium(cfg, m_partial, dex) -> Candidate:
    """Sell WPRL on DEX, buy equal PRL on CEX @ ask1. Token-neutral, profit=USD spread."""
    c = Candidate(Direction.DEX_PREMIUM)
    Pa = m_partial["ask1_px"]
    taker = Decimal(cfg.safetrade_taker_fee_bps) / Decimal(10000)
    ceiling_qty = Decimal(cfg.max_trade_usd) / Pa if Pa > 0 else Decimal(0)

    caps = {"ceiling": ceiling_qty, **_hard_caps(cfg, m_partial, Direction.DEX_PREMIUM)}
    qty = min(caps.values())
    if qty <= 0:
        c.reason = "capped@0: " + min(caps, key=caps.get)
        return c

    proceeds = dex.quote_exact_in(cfg.wprl_address, cfg.quote_token_address, qty)  # USDT from selling qty WPRL
    cost = qty * Pa * (Decimal(1) + taker)                  # USDT to buy qty PRL on CEX
    pnl = proceeds - cost
    notional = qty * Pa
    c.qty, c.proceeds_usd, c.cost_usd, c.dex_quote_amount = qty, proceeds, cost, proceeds
    c.pnl_usd = pnl
    c.net_bps = pnl / notional * Decimal(10000) if notional > 0 else Decimal(-99999)
    c.reason = f"qty {float(qty):.0f} (cap={min(caps, key=caps.get)})"
    return c


def _alchemy_xfers(w3, params, retries=4):
    """alchemy_getAssetTransfers with retry on transient RPC errors (503/429/timeout).
    A single transient failure here used to make in-flight detection wrong, which
    mis-drove supply's share -> a wrong bridge. Retrying makes that path robust; only
    if ALL retries fail does the (now age-bounded) fail-safe kick in."""
    import time as _t
    last = None
    for i in range(retries):
        try:
            resp = w3.provider.make_request("alchemy_getAssetTransfers", [params])
            if isinstance(resp, dict) and resp.get("error"):
                raise RuntimeError(str(resp["error"])[:100])
            return resp
        except Exception as e:
            last = e
            if i < retries - 1:
                _t.sleep(min(2 ** i, 8))   # exponential backoff: 1, 2, 4, 8s (capped)
    raise last


def _wprl_mints_since(dex, from_block, lookback=7200, chunk=45000):  # ~24h, in-flight 匹配只看<6h
    """All WPRL mint amounts (Transfer from 0x0 to our wallet) within `lookback`
    blocks. Uses Alchemy's `alchemy_getAssetTransfers` rather than eth_getLogs:
    the bridge relayer mints WPRL by transferring from 0x0, and getAssetTransfers
    lets us filter `fromAddress=0x0 + toAddress=us` SERVER-SIDE in one call. This
    avoids eth_getLogs entirely — Alchemy's free tier caps getLogs at a 10-block
    range, which silently broke the old scan and made in-flight detection return 0.
    A single arb buy comes `from` the Uniswap pool (0x89a67c6d…), never 0x0, so
    those are naturally excluded.

    Returns list of (Decimal amount, int unix_ts) — the timestamp lets callers match a
    mint ONLY to a withdrawal that left CEX BEFORE it. Without that, a new withdrawal
    gets false-matched to an OLD same-size mint (e.g. two ~800 reverse bridges hours
    apart), making in-flight wrongly read 0 while the new bridge is still in transit.

    `chunk` is unused now (kept for signature compatibility)."""
    from datetime import datetime, timezone
    w3 = dex.w3
    me = Web3_checksum(dex.address)
    wprl = Web3_checksum(dex.cfg.wprl_address)
    zero = "0x0000000000000000000000000000000000000000"
    latest = w3.eth.block_number
    start = max(from_block, latest - lookback)
    mints = []
    page_key = None
    for _ in range(20):  # defensive pagination cap; mints are rare, 1 page is typical
        params = {
            "fromBlock": hex(start), "toBlock": "latest",
            "fromAddress": zero, "toAddress": me,
            "contractAddresses": [wprl], "category": ["erc20"],
            "withMetadata": True, "order": "desc", "maxCount": "0x64",
        }
        if page_key:
            params["pageKey"] = page_key
        resp = _alchemy_xfers(w3, params)
        result = resp.get("result") or {}
        for t in result.get("transfers", []):
            rc = t.get("rawContract") or {}
            raw, dec = rc.get("value"), rc.get("decimal")
            if raw is None:
                continue
            decimals = int(dec, 16) if dec else 8
            amt = Decimal(int(raw, 16)) / Decimal(10) ** decimals
            ts = 0
            bts = (t.get("metadata") or {}).get("blockTimestamp")
            if bts:
                try:
                    ts = int(datetime.fromisoformat(bts.replace("Z", "+00:00")).timestamp())
                except Exception:
                    ts = 0
            mints.append((amt, ts))
        page_key = result.get("pageKey")
        if not page_key:
            break
    return mints


def _wprl_burns_since(dex, lookback=7200):
    """WPRL burned to the bridge = forward bridges (WPRL->PRL): a Transfer FROM our wallet
    TO 0x0. Arb DEX sells go to the Uniswap pool (0x89a67c6d…), NOT 0x0, so they're cleanly
    excluded. Returns [(Decimal amount, int unix_ts)]. Lets to_cex count a forward bridge
    from the moment WPRL leaves EVM (not from when the PRL deposit later registers at the
    CEX) — closing the burn->deposit-appears gap that showed as a phantom token deficit."""
    from datetime import datetime, timezone
    w3 = dex.w3
    me = Web3_checksum(dex.address)
    wprl = Web3_checksum(dex.cfg.wprl_address)
    zero = "0x0000000000000000000000000000000000000000"
    latest = w3.eth.block_number
    start = max(0, latest - lookback)
    burns = []
    page_key = None
    for _ in range(20):
        params = {
            "fromBlock": hex(start), "toBlock": "latest",
            "fromAddress": me, "toAddress": zero,
            "contractAddresses": [wprl], "category": ["erc20"],
            "withMetadata": True, "order": "desc", "maxCount": "0x64",
        }
        if page_key:
            params["pageKey"] = page_key
        resp = _alchemy_xfers(w3, params)
        result = resp.get("result") or {}
        for t in result.get("transfers", []):
            rc = t.get("rawContract") or {}
            raw, dec = rc.get("value"), rc.get("decimal")
            if raw is None:
                continue
            decimals = int(dec, 16) if dec else 8
            amt = Decimal(int(raw, 16)) / Decimal(10) ** decimals
            ts = 0
            bts = (t.get("metadata") or {}).get("blockTimestamp")
            if bts:
                try:
                    ts = int(datetime.fromisoformat(bts.replace("Z", "+00:00")).timestamp())
                except Exception:
                    ts = 0
            burns.append((amt, ts))
        page_key = result.get("pageKey")
        if not page_key:
            break
    return burns


def Web3_checksum(addr):
    from web3 import Web3
    return Web3.to_checksum_address(addr)


def match_bridge_arrivals(expectations, events, tol_abs=2.0, tol_rel=0.02, grace_sec=120):
    """Greedy oldest-first matcher for "did transfer X arrive" reconciliation — THE single
    implementation for both bridge directions. This logic used to live as 4 divergent
    copies (_inflight_compute x2 directions, reverse_inflight_age_min, supply's
    prl_to_wprl close), and its two bugfixes each had to be synced by hand across them:
      * process expectations OLDEST-first, each claiming the EARLIEST valid event —
        else an old expectation steals a younger same-size event and the newest
        transfer falsely reads arrived (the in-flight=0 double-bridge bug);
      * an event predating the expectation by > grace_sec can't be its arrival —
        else a fresh bridge matches an OLD same-size event (same family).
    Each event is consumed at most once (one mint can't close two same-size entries).

      expectations: [(expected_amount, initiated_ts), ...]  (extra tuple fields allowed)
      events:       [(amount, ts), ...]  — observed arrivals (mints / credited deposits)
    Returns list[bool] aligned with `expectations`. Amounts may be Decimal or float
    (compared as float — tolerance max(tol_abs, expected*tol_rel) is coarse by design)."""
    order = sorted(range(len(expectations)), key=lambda i: expectations[i][1] or 0)
    remaining = sorted(((float(a), t or 0) for a, t in events), key=lambda x: x[1])
    matched = [False] * len(expectations)
    for i in order:
        exp, its = float(expectations[i][0]), expectations[i][1] or 0
        tol = max(tol_abs, exp * tol_rel)
        for j, (ev_amt, ev_ts) in enumerate(remaining):
            if ev_ts and its and ev_ts < its - grace_sec:
                continue
            if abs(ev_amt - exp) <= tol:
                matched[i] = True
                remaining.pop(j)
                break
    return matched


def reverse_inflight_age_min(cfg, st, dex):
    """Minutes the OLDEST still-in-flight reverse bridge (PRL->WPRL) has been waiting for
    its WPRL mint. 0 if none in-flight. A value >60 means the bridge relayer hasn't minted
    for over an hour — likely needs a manual claim on the PearlBridge frontend. Used to
    surface a stuck-bridge alert on the data_hub line. Best-effort; returns 0 on any error."""
    from datetime import datetime, timezone
    try:
        now = datetime.now(timezone.utc)
        def ep(s):
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        wds = []
        for w in st.withdraws(currency="prl", limit=10):
            if w.get("state", w.get("status")) in ("failed", "rejected", "canceled", "errored"):
                continue
            ca = w.get("created_at")
            if not ca or (now.timestamp() - ep(ca)) >= 6 * 3600:
                continue
            wds.append(w)
        if not wds:
            return 0.0
        mints = _wprl_mints_since(dex, from_block=0, lookback=7200)
        fee_rate = Decimal(cfg.bridge_fee_bps_prl_to_wprl) / Decimal(10000)
        min_fee = Decimal(str(cfg.bridge_min_prl))
        # match constraint uses completed_at (mint can't precede the PRL leaving CEX);
        # ordering/anti-steal rules live in the shared matcher.
        exps = []
        for w in wds:
            amt = Decimal(str(w.get("amount") or 0))
            exps.append((amt - max(amt * fee_rate, min_fee),
                         int(ep(w.get("completed_at") or w.get("created_at")))))
        oldest = 0.0
        for w, ok in zip(wds, match_bridge_arrivals(exps, mints)):
            if ok:
                continue
            # age from INITIATION (created_at) to match the ~19min median we quote
            cts = ep(w.get("created_at") or w.get("completed_at"))
            oldest = max(oldest, (now.timestamp() - cts) / 60.0)
        return oldest
    except Exception:
        return 0.0


_inflight_cache = {"ts": 0.0, "val": None}
_INFLIGHT_TTL = 180   # 3min. in-flight 以过桥分钟级变化, 不必每 4s 重算。


def _inflight(cfg, st, dex=None):
    """Cached wrapper. _inflight_compute 每次做 2 次重型 Alchemy getAssetTransfers
    (burns + mints, ~80k 块); auto_run 每 4s 调一次会把 RPC 配额烧穿。缓存 3min:
    在途变化是过桥时间尺度(分钟), 3min 陈旧完全可接受。每个进程各自缓存。"""
    import time as _t
    now = _t.time()
    c = _inflight_cache
    if c["val"] is not None and now - c["ts"] < _INFLIGHT_TTL:
        return c["val"]
    val = _inflight_compute(cfg, st, dex)
    c["ts"], c["val"] = now, val
    return val


def _inflight_compute(cfg, st, dex=None):
    """Detect token in-flight on the bridge in BOTH directions.

      to_cex: WPRL->PRL deposits not yet credited (PRL arriving at CEX).
      to_evm: PRL->WPRL withdrawals (succeed) whose WPRL has NOT yet minted on EVM.
              Reconciled against ON-CHAIN mint events (Transfer from 0x0) — immune to
              arb consuming WPRL afterward and to bridge timing. A withdrawal counts as
              arrived once a mint of ~amount*(1-bridge_fee) appears after its tx.
    """
    from datetime import datetime, timezone
    to_cex = Decimal(0); to_evm = Decimal(0)
    now = datetime.now(timezone.utc)

    def _ts(s):
        try:
            return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
        except Exception:
            return 0

    # WPRL->PRL (to_cex): count forward bridges from the on-chain WPRL BURN, not from when
    # the PRL deposit later registers at the CEX. A burn is still in-flight until a CREDITED
    # PRL deposit (~same amount, free 1:1 bridge) appears AFTER it. This closes the
    # burn->deposit-appears gap that made a fresh forward bridge read as a phantom deficit.
    try:
        if dex is not None:
            burns = _wprl_burns_since(dex)            # [(amt, ts)] me->0x0, recent first
            now_ep = int(now.timestamp())
            recent_burns = [(a, t) for a, t in burns if t and (now_ep - t) < 6 * 3600]
            cred = []                                  # credited PRL deposits = arrived forwards
            for d in st.deposits(currency="prl", limit=20):
                if d.get("credited"):
                    cred.append((Decimal(str(d.get("amount") or 0)), _ts(d.get("created_at") or "")))
            # a burn with no credited deposit after it (shared matcher) is still in-flight
            for (bamt, _), ok in zip(recent_burns, match_bridge_arrivals(recent_burns, cred)):
                if not ok:
                    to_cex += bamt
        else:
            raise RuntimeError("no dex")
    except Exception:
        # FAIL-SAFE: can't read burns on-chain -> fall back to uncredited-deposit count
        # (never silently 0). Overcount is the safe direction.
        try:
            for dp in st.deposits(currency="prl", limit=10):
                if not dp.get("credited"):
                    to_cex += Decimal(str(dp["amount"]))
        except Exception:
            pass

    # PRL->WPRL legs: match each succeed withdrawal to an on-chain mint
    try:
        wds = [w for w in st.withdraws(currency="prl", limit=25)
               if w.get("state", w.get("status")) not in ("failed", "rejected", "canceled", "errored")]
        now = datetime.now(timezone.utc)
        recent = []   # <6h: reconcile against the on-chain mint scan (below)
        older = []    # 6h..7d: outside the mint-scan window -> ask the bridge relay directly
        for w in wds:
            ca = w.get("created_at")
            if not ca:
                continue
            try:
                age = (now - datetime.fromisoformat(ca.replace("Z", "+00:00"))).total_seconds()
            except Exception:
                continue
            if age < 6 * 3600:
                recent.append(w)
            elif age < 7 * 24 * 3600:
                older.append(w)
        # >6h withdrawals: nearly all landed long ago, so count one ONLY on positive relay
        # evidence it's still stuck (non-terminal /v1/mints state). This keeps a stuck
        # reverse bridge on the books past the 6h scan window — once a
        # bridge stuck ~3d silently left tokens_total and the conservation guard froze
        # supply. No txid / API down -> assume landed (counting a landed bridge again
        # would trip the same guard from the other side).
        for w in older:
            txid = w.get("txid")
            if not txid:
                continue
            try:
                import pearlbridge_api as _pb
                state = (_pb.mint_status(txid) or {}).get("state")
                if state and not _pb.MINT_STATES.get(state, (False, ""))[0]:
                    to_evm += Decimal(str(w.get("amount") or 0))
            except Exception:
                pass
        if recent and dex is not None:
            # FAIL-SAFE: if the on-chain mint fetch errors even after retries, don't return
            # to_evm=0 (understates -> double-bridge) BUT don't count ALL recent either —
            # counting long-landed bridges as in-flight massively OVERstated to_evm (a 503
            # once made it 6197 from 5 already-landed bridges -> supply share read 26% ->
            # a WRONG forward bridge). Only count withdrawals YOUNG enough to plausibly still
            # be in-flight (< max bridge time ~35min); older ones have certainly minted.
            try:
                mints = _wprl_mints_since(dex, from_block=0, lookback=7200)  # [(amount, ts), ...]
            except Exception:
                for w in recent:
                    ca = w.get("completed_at") or w.get("created_at")
                    try:
                        age_min = (now.timestamp() - datetime.fromisoformat(ca.replace("Z", "+00:00")).timestamp()) / 60
                    except Exception:
                        age_min = 999
                    if age_min < 35:
                        to_evm += Decimal(str(w.get("amount") or 0))
                raise   # jump to outer except (to_evm already conservatively counted)
            # PRL->WPRL fee is max(0.5%, 4 PRL min) — for tiny amounts the flat 4-PRL
            # floor dominates, so a percentage-only model mis-predicts the mint size and
            # never matches it. A withdrawal's mint must postdate its completed_at
            # (fallback created_at); ordering/anti-steal rules live in the shared matcher.
            fee_rate = Decimal(cfg.bridge_fee_bps_prl_to_wprl) / Decimal(10000)
            min_fee = Decimal(str(cfg.bridge_min_prl))
            exps = []
            for w in recent:
                amt = Decimal(str(w.get("amount") or 0))
                expected = amt - max(amt * fee_rate, min_fee)
                exps.append((expected, _ts(w.get("completed_at") or w.get("created_at") or "")))
            for w, ok in zip(recent, match_bridge_arrivals(exps, mints)):
                if not ok:
                    to_evm += Decimal(str(w.get("amount") or 0))   # no mint yet -> in-flight
    except Exception:
        pass
    return to_cex, to_evm


def snapshot_market(cfg, st, dex) -> MarketView:
    # PARALLEL fetch of all independent inputs. These are read-only network round-trips to
    # two separate venues (SafeTrade REST + EVM RPC); fired concurrently they collapse a
    # ~13-24s serial scan to ~2-3s, so detection keeps up with ~12s blocks. This is purely
    # the DATA-ACQUISITION step — the MarketView it returns is identical, and evaluate /
    # execution / accounting downstream are unchanged. (SafeTrade nonce is lock+monotonic,
    # so concurrent authed CEX calls don't collide.)
    from concurrent.futures import ThreadPoolExecutor
    quote_sym = cfg.quote_token_symbol.lower() if cfg.quote_token_symbol != "WETH" else "usdt"
    with ThreadPoolExecutor(max_workers=6) as ex:
        f_depth = ex.submit(st.depth, cfg.safetrade_market, 5)
        f_bals = ex.submit(st.balances)                          # ONE call for both prl+usdt
        f_spot = ex.submit(dex.wprl_price_in_quote)
        f_wprl = ex.submit(dex.balance, cfg.wprl_address)
        f_quote = ex.submit(dex.balance, cfg.quote_token_address)
        f_infl = ex.submit(_inflight, cfg, st, dex)
        d = f_depth.result()
        bals = f_bals.result()
        spot = f_spot.result()
        dex_wprl = f_wprl.result()
        dex_quote = f_quote.result()
        infl_to_cex, infl_to_evm = f_infl.result()

    bid1_px = Decimal(str(d["bids"][0][0])) if d.get("bids") else Decimal(0)
    bid1_qty = Decimal(str(d["bids"][0][1])) if d.get("bids") else Decimal(0)
    ask1_px = Decimal(str(d["asks"][0][0])) if d.get("asks") else Decimal(0)
    ask1_qty = Decimal(str(d["asks"][0][1])) if d.get("asks") else Decimal(0)

    def _bal(cur):
        for b in bals:
            if b["currency"].lower() == cur.lower():
                return Decimal(str(b["balance"]))
        return Decimal(0)
    cex_prl = _bal("prl")
    cex_usdt = _bal(quote_sym)

    partial = {
        "bid1_px": bid1_px, "bid1_qty": bid1_qty, "ask1_px": ask1_px, "ask1_qty": ask1_qty,
        "spot": spot, "cex_balance_prl": cex_prl, "cex_balance_usdt": cex_usdt,
        "dex_balance_wprl": dex_wprl, "dex_balance_quote": dex_quote,
    }
    # each sizing makes one Quoter call (depends on its qty) — run the two concurrently
    with ThreadPoolExecutor(max_workers=2) as ex:
        fc = ex.submit(_size_cex_premium, cfg, partial, dex)
        fd = ex.submit(_size_dex_premium, cfg, partial, dex)
        cex_prem = fc.result()
        dex_prem = fd.result()

    return MarketView(
        bid1_px=bid1_px, bid1_qty=bid1_qty, ask1_px=ask1_px, ask1_qty=ask1_qty, spot=spot,
        cex_balance_prl=cex_prl, cex_balance_usdt=cex_usdt,
        dex_balance_wprl=dex_wprl, dex_balance_quote=dex_quote,
        cex_prem=cex_prem, dex_prem=dex_prem,
        inflight_to_cex=infl_to_cex, inflight_to_evm=infl_to_evm,
        gas_usd=gas_cost_usd(dex),   # cached; priced live so the floor tracks real gas
    )


class ArbEngine:
    def __init__(self, cfg):
        self.cfg = cfg

    def _thresholds(self, m: MarketView):
        """Inventory-aware ASYMMETRIC thresholds with LINEAR decay.

        Within target ± deadband: both directions use full min_gap_bps.
        Beyond the band, the direction that REPLENISHES the scarce side has its
        threshold decay LINEARLY from full -> 0 as share goes from band-edge -> the
        extreme (0% or 100%). At full imbalance the threshold is 0 (take any positive
        spread to rebalance). Trading that direction *is* the rebalance — no bridge,
        no wait, still earns the spread. Other direction keeps full threshold.

          share LOW  (CEX short PRL)  -> decay DEX_PREMIUM thresh (buys PRL back)
          share HIGH (CEX heavy PRL)  -> decay CEX_PREMIUM thresh (sells PRL off)
        Returns (cex_prem_thresh, dex_prem_thresh, note).
        """
        full = Decimal(self.cfg.min_gap_bps)
        target = Decimal(self.cfg.share_target_pct)
        band = Decimal(self.cfg.share_deadband_pct)

        # CEX-side tokens include WPRL->PRL in-flight (arriving at CEX);
        # EVM-side tokens include PRL->WPRL in-flight (will mint as EVM WPRL).
        to_cex = getattr(m, "inflight_to_cex", Decimal(0))
        to_evm = getattr(m, "inflight_to_evm", Decimal(0))
        cex_eff = m.cex_balance_prl + to_cex
        evm_eff = m.dex_balance_wprl + to_evm
        total = cex_eff + evm_eff
        if total <= 0:
            return full, full, "no inventory"
        share = cex_eff / total * Decimal(100)

        hi = target + band      # above -> CEX heavy -> decay CEX_PREM
        lo = target - band      # below -> CEX short -> decay DEX_PREM

        if share > hi:
            # linear: at share=hi -> full; at share=100 -> 0
            frac = (share - hi) / (Decimal(100) - hi) if (Decimal(100) - hi) > 0 else Decimal(1)
            frac = min(Decimal(1), max(Decimal(0), frac))
            cex_th = full * (Decimal(1) - frac)
            return cex_th, full, f"share {float(share):.0f}%>{float(hi):.0f}% -> CEX_PREM decayed {int(cex_th)} (frac {float(frac):.2f})"
        if share < lo:
            # linear: at share=lo -> full; at share=0 -> 0
            frac = (lo - share) / lo if lo > 0 else Decimal(1)
            frac = min(Decimal(1), max(Decimal(0), frac))
            dex_th = full * (Decimal(1) - frac)
            return full, dex_th, f"share {float(share):.0f}%<{float(lo):.0f}% -> DEX_PREM decayed {int(dex_th)} (frac {float(frac):.2f})"
        return full, full, f"share {float(share):.0f}% in band [{float(lo):.0f}-{float(hi):.0f}] -> both {int(full)}"

    def evaluate(self, m: MarketView) -> TradePlan:
        cex_th, dex_th, note = self._thresholds(m)
        # each direction judged against its OWN threshold
        cex_ok = m.cex_prem.net_bps >= cex_th and m.cex_prem.qty > 0
        dex_ok = m.dex_prem.net_bps >= dex_th and m.dex_prem.qty > 0

        # among directions that clear their threshold, take the most profitable
        candidates = []
        if cex_ok:
            candidates.append((m.cex_prem, m.cex_prem.net_bps - cex_th))
        if dex_ok:
            candidates.append((m.dex_prem, m.dex_prem.net_bps - dex_th))
        if candidates:
            best = max(candidates, key=lambda x: x[1])[0]
            # min per-trade profit floor = max(configured floor, LIVE gas cost of the swap).
            # A trade must clear its own on-chain gas, so a marginal trade can never net-negative
            # after gas — the floor now tracks real gas instead of assuming a fixed ~$0.03.
            min_pnl = max(Decimal(self.cfg.min_trade_pnl_usd), m.gas_usd)
            if best.pnl_usd < min_pnl:
                return TradePlan(
                    Direction.NONE, Decimal(0), Decimal(0), int(best.net_bps), Decimal(0), Decimal(0),
                    f"{best.direction.value} pnl ${float(best.pnl_usd):.2f} < min ${float(min_pnl):.2f} "
                    f"(gas ${float(m.gas_usd):.2f}) (qty {float(best.qty):.1f} capped by {best.reason}) — skip",
                )
            return TradePlan(
                best.direction, best.qty * (m.bid1_px if best.direction == Direction.CEX_PREMIUM else m.ask1_px),
                best.qty, int(best.net_bps), best.pnl_usd, best.dex_quote_amount,
                f"{best.direction.value} net {int(best.net_bps)}bps pnl ${float(best.pnl_usd):.2f} "
                f"(gas ${float(m.gas_usd):.2f}) {best.reason} [{note}]",
            )
        best_dir = max([m.cex_prem, m.dex_prem], key=lambda c: c.net_bps)
        return TradePlan(
            Direction.NONE, Decimal(0), Decimal(0), int(best_dir.net_bps), Decimal(0), Decimal(0),
            f"[{note}] CEX_PREM net={float(m.cex_prem.net_bps):.1f}/th{int(cex_th)} | "
            f"DEX_PREM net={float(m.dex_prem.net_bps):.1f}/th{int(dex_th)}"
        )


def execute(plan: TradePlan, st_client, dex_client, cfg) -> dict:
    # DEAD/DEPRECATED — live execution lives in scripts/auto_run.py. No callers; guarded
    # so it can't run by accident and silently diverge from the hardened auto_run path
    # (parallel-implementation footgun).
    raise RuntimeError("arb.execute() is deprecated; use scripts/auto_run.py for live execution")
    out = {"plan": plan.__dict__.copy(), "dry_run": cfg.dry_run, "legs": []}
    if plan.direction == Direction.NONE:
        return out

    slip = Decimal(cfg.slippage_bps) / Decimal(10000)

    # Quantize qty to CEX amount_precision (4) so BOTH legs use the exact same
    # base amount and the CEX leg won't 422. Round DOWN to stay within inventory.
    from decimal import ROUND_DOWN
    qprec = Decimal(10) ** (-cfg.cex_amount_precision)
    plan.base_qty = Decimal(plan.base_qty).quantize(qprec, rounding=ROUND_DOWN)
    if plan.base_qty <= 0:
        out["aborted"] = "qty rounded to 0 at cex precision"
        return out

    if plan.direction == Direction.CEX_PREMIUM:
        # Sell PRL on CEX first? No — DEX leg is the slow on-chain one. Fire DEX buy first.
        # Buy `qty` WPRL on DEX: spend up to dex_quote_amount*(1+slip), require >= qty out.
        max_in = plan.dex_quote_amount * (Decimal(1) + slip)
        min_out = plan.base_qty * (Decimal(1) - slip)
        leg1 = dex_client.swap_exact_in(
            cfg.quote_token_address, cfg.wprl_address,
            max_in, min_out, dry_run=cfg.dry_run,
        )
        out["legs"].append({"venue": "dex", "side": "buy_wprl", "qty": str(plan.base_qty),
                            "spend_usdt": str(max_in), "result": leg1})
        if cfg.dry_run:
            out["legs"].append({"venue": "cex", "side": "sell_prl", "qty": str(plan.base_qty), "result": {"dry_run": True}})
        else:
            # SAFETY: do not fire CEX leg if the on-chain leg reverted (status != 1)
            if leg1.get("status") != 1:
                out["aborted"] = "dex leg failed (status!=1); CEX leg skipped to avoid naked exposure"
                return out
            leg2 = st_client.place_market_order(cfg.safetrade_market, "sell", plan.base_qty)
            out["legs"].append({"venue": "cex", "side": "sell_prl", "qty": str(plan.base_qty), "result": leg2})

    elif plan.direction == Direction.DEX_PREMIUM:
        # Sell WPRL on DEX (slow leg) first, then buy PRL on CEX.
        min_out = plan.dex_quote_amount * (Decimal(1) - slip)
        leg1 = dex_client.swap_exact_in(
            cfg.wprl_address, cfg.quote_token_address,
            plan.base_qty, min_out, dry_run=cfg.dry_run,
        )
        out["legs"].append({"venue": "dex", "side": "sell_wprl", "qty": str(plan.base_qty), "result": leg1})
        if cfg.dry_run:
            out["legs"].append({"venue": "cex", "side": "buy_prl", "qty": str(plan.base_qty), "result": {"dry_run": True}})
        else:
            if leg1.get("status") != 1:
                out["aborted"] = "dex leg failed (status!=1); CEX leg skipped to avoid naked exposure"
                return out
            leg2 = st_client.place_market_order(cfg.safetrade_market, "buy", plan.base_qty)
            out["legs"].append({"venue": "cex", "side": "buy_prl", "qty": str(plan.base_qty), "result": leg2})

    return out
