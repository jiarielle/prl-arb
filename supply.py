"""Auto-supply daemon — keeps inventory balanced, decoupled from trading.

Runs independently of auto_run.py and data_hub.py. Every BRIDGE_INTERVAL it tops up
the CEX token side toward TARGET_SHARE via WPRL->PRL bridge (free), and separately
withdraws USDT back to EVM once net USDT inflow on CEX reaches a threshold.

Design (per user):
  * share INCLUDES in-flight PRL deposits -> never double-bridges funds en route.
  * top up the full deficit (no per-round cap): after the one-off big rebalance,
    steady-state deficits are small (=what arb consumed in one interval) -> naturally
    a trickle. Skip if deficit < MIN_BRIDGE (too small to bother w/ gas+inflight slot).
  * USDT: withdraw one-shot when NET inflow (cumulative CEX usdt increase since last
    withdraw) >= USDT_NET_INFLOW_THRESHOLD. Net inflow (not abs balance) so a direction
    flip that drains CEX usdt never wrongly triggers a withdrawal.

Both bridge directions and both USDT directions are implemented; every fund-moving
call honours DRY_RUN from .env.
"""
import sys, time, json
from decimal import Decimal
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config import load_config
from safetrade import SafeTradeClient
from dex import DexClient
from bridge_evm import BridgeEVM
import arb

try:
    from pearlbridge_api import BURN_BAD as PB_BURN_BAD
except Exception:
    PB_BURN_BAD = {"failed", "cancelled", "rejected", "under_review", "submitted_stuck"}

# ---- params ----
TARGET_SHARE = Decimal("40")          # % of total token that should sit on CEX (matches arb; 2026-07-25 55->40, dex_prem机会6:1偏EVM侧)
BRIDGE_TRIGGER_DEVIATION = Decimal("15")  # only bridge when |share - target| > this (else let
                                      # arb's inventory-aware spread rebalance via trading first).
                                      # 20% was too slow -> kept hitting "waiting for funds".

def _env_str(key: str) -> str:
    """Identity / routing values must come from .env — no silent defaults (fail loud).
    In DRY_RUN they may be blank (nothing is withdrawn or transferred), so a fresh
    checkout can be dry-run before the exchange whitelist exists."""
    import os
    v = os.getenv(key, "").strip()
    if not v:
        if load_config().dry_run:
            print(f"supply: {key} not set (fine in DRY_RUN, required before going live)", flush=True)
            return ""
        raise SystemExit(f"supply: missing required env {key} (see the example env file)")
    return v


def _env_int(key: str) -> int:
    v = _env_str(key)
    return int(v) if v else 0

BRIDGE_INTERVAL_SEC = 300             # 5 min
MIN_BRIDGE = Decimal("200")           # skip WPRL->PRL bridges smaller than this (free direction)
# Reverse bridge PRL->WPRL (refill EVM when CEX token-heavy). Verified no-signature:
# whitelisted PRL withdrawal -> deposit address -> relayer auto-mints WPRL to EVM.
# Has a FLAT 4 PRL min fee, so single-leg must be large to amortize it.
PRL_WPRL_BENEFICIARY = _env_int("SAFETRADE_PRL_BRIDGE_BENEFICIARY_ID")  # whitelisted PRL beneficiary = your bridge deposit address (pearl-tokens)
MIN_REVERSE_BRIDGE = Decimal("800")   # don't reverse-bridge less than this (4 PRL fee = 0.5% here)
# USDT rebalance — symmetric to token bridge but INVERSE target. CEX_PREMIUM consumes
# CEX-PRL + EVM-USDT, so to sustain it CEX wants more token (55%) and EVM wants more
# USDT. Hence USDT target on CEX = 100 - token_target = 45%. Withdraw to rebalance USDT
# only when its share deviates past the gate (same idea as bridge deviation gate).
USDT_TARGET_SHARE = Decimal("60")     # % of total USDT that should sit on CEX (= 100 - TARGET_SHARE)
USDT_TRIGGER_DEVIATION = Decimal("15")  # only move USDT when |usdt_share - target| > this (was 20)
MIN_USDT_MOVE = Decimal("200")        # skip USDT moves smaller than this
# Bridge cooldown lock. NOTE: we do NOT block on "in-flight exists" — that would kill
# the intended trickle (a trickle by definition always has something on the way). The
# real repeat-bridge guard is that share INCLUDES in-flight (ledger + on-chain), so once
# the in-flight covers the deficit, deviation drops and it naturally stops. The cooldown
# only prevents firing again before on-chain confirmation of the last burn settles.
BRIDGE_COOLDOWN_SEC = 5 * 60          # no 2nd bridge within 5min of the last
OVERRIDE_FILE = Path(__file__).parent / ".bridge_override"   # user `touch`es this to force one
DRY_RUN = load_config().dry_run       # DRY_RUN in .env governs bridges, withdrawals and transfers here too

LOG = Path(__file__).parent / "logs" / "supply.log"
# Ledger of transfers SUPPLY itself initiated, to cover the "left source, not yet at
# destination" blind spot (e.g. WPRL burned on EVM but PRL not yet at SafeTrade).
# Only covers SUPPLY's own actions — manual transfers by the user aren't tracked here
# (their short blind-spot window is accepted). Entries cleared once destination arrives.
PENDING = Path(__file__).parent / "logs" / "pending_supply.jsonl"
PRL_DEPOSIT_ADDR = _env_str("SAFETRADE_PRL_DEPOSIT_ADDR")    # your SafeTrade native PRL deposit address (prl1...)
BENEFICIARY_ID = _env_int("SAFETRADE_USDT_BENEFICIARY_ID")   # whitelisted USDT beneficiary = your EVM wallet
# SafeTrade's on-chain USDT (ERC20) deposit address — the EVM->CEX top-up target.
# This is a plain wallet transfer (only gas, no bridge/withdraw fee), not a whitelisted
# beneficiary withdrawal, so the opsec whitelist rule doesn't apply (we're SENDING to
# the CEX, not letting the CEX send out). Hardcoded + verified by the user.
CEX_USDT_DEPOSIT_ADDR = _env_str("SAFETRADE_USDT_DEPOSIT_ADDR")  # your SafeTrade ERC20 USDT deposit address

# Token-conservation sanity bound for bridge decisions. Total tokens (CEX PRL + EVM WPRL +
# in-flight both ways) is physically conserved — the arb is token-neutral, only bridge fees
# and tiny inventory trades move it — so it can't stray far from baseline. If the computed
# total IS way off, the in-flight number is CORRUPTED (double-count / mint-mismatch / RPC
# fail-safe over-count) and must NOT drive a bridge. This is the ROOT-CAUSE guard: it catches
# ALL in-flight over-count bugs (past and future) at the decision point, regardless of cause.
try:
    import json as _json
    _bl = _json.load(open(Path(__file__).parent / "baseline.json"))
    # Conservation reference is decoupled from the PnL baseline (start_token): when a forward
    # bridge is stuck/lost (e.g. a forward bridge stuck in `failed` awaiting the operator), the live token
    # total legitimately sits well below start_token. Run at the reduced total by centering the
    # band on `bridge_ref_token` (current real holdings) while start_token stays put so cumulative
    # PnL still reflects the unrecovered coins. Restore bridge_ref_token to start_token once the
    # stuck bridge lands. Falls back to start_token when the field is absent.
    BASELINE_TOKEN = float(_bl.get("bridge_ref_token") or _bl["start_token"])
except Exception:
    BASELINE_TOKEN = 0.0
TOKEN_CONSERVATION_BAND = Decimal("0.25")   # total must be within ±25% of baseline
if BASELINE_TOKEN <= 0 and not DRY_RUN:
    raise SystemExit("supply: baseline.json missing or start_token=0 -> the token-conservation guard would be "
                     "disabled. Copy baseline.json.example, set start_token/start_usdt after first funding, "
                     "or run with DRY_RUN=true.")


def emit(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def _ledger_load():
    import json
    if not PENDING.exists():
        return []
    out = []
    for l in open(PENDING):
        l = l.strip()
        if l:
            try: out.append(json.loads(l))
            except Exception: pass
    return out


def _ledger_save(entries):
    import json
    with open(PENDING, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _ledger_add(kind, amount, tx_hash=None, wid=None):
    """Record a SUPPLY-initiated transfer that has left its source but not yet arrived.
    kind: 'wprl_to_prl' (burn->CEX PRL) | 'usdt_to_evm' (withdraw->EVM USDT)
        | 'prl_to_wprl' (reverse bridge->EVM WPRL) | 'usdt_to_cex' (EVM transfer->CEX USDT).
    tx_hash: wprl_to_prl 的 requestBurn 交易 hash — pearlbridge_api.forward_alerts 用它
    查 /v1/burns/{hash} 的 relay 状态(超时不再只能干等 3h 时效窗口)。
    wid: prl_to_wprl 的 SafeTrade 提现 id — reconcile 到期前用它拿链上 txid 查
    /v1/mints 的 relay 状态(同上, 反向方向)。"""
    import time as _t
    es = _ledger_load()
    e = {"kind": kind, "amount": float(amount), "ts": int(_t.time()), "open": True}
    if tx_hash:
        e["tx_hash"] = str(tx_hash)
    if wid is not None:
        e["wid"] = wid
    es.append(e)
    _ledger_save(es)


def _ep(s):
    """ISO -> epoch seconds; 0 on any parse failure."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


class Supply:
    def __init__(self):
        self.cfg = load_config()
        self.st = SafeTradeClient(self.cfg.safetrade_api_key, self.cfg.safetrade_api_secret, self.cfg.safetrade_base_url)
        self.dex = DexClient(self.cfg)
        self.bridge = BridgeEVM(self.dex)
        # baseline for NET usdt inflow tracking — set on first loop
        self.cex_usdt_baseline = None

    def inflight_prl(self):
        try:
            return sum(Decimal(str(d["amount"])) for d in self.st.deposits(currency="prl", limit=10)
                       if not d.get("credited"))
        except Exception as e:
            emit(f"  WARN inflight read: {str(e)[:80]}")
            return Decimal(0)

    def _last_bridge_ts(self):
        f = Path(__file__).parent / "logs" / ".last_bridge_ts"
        try:
            return float(f.read_text().strip())
        except Exception:
            return None

    def _mark_bridge_ts(self):
        import time as _t
        f = Path(__file__).parent / "logs" / ".last_bridge_ts"
        try:
            f.write_text(str(_t.time()))
        except Exception as e:
            emit(f"  WARN: could not persist bridge cooldown ({e}); a restart may bridge again before the last one lands")

    def reconcile_ledger(self):
        """Close ledger entries whose destination has now arrived, so we stop counting
        them as in-flight (double-count guard). Matching is by-kind + age:
          wprl_to_prl: closed once a PRL deposit (any state) created AFTER the burn
                       appears — i.e. the bridge produced the CEX-side deposit.
          usdt_to_evm: closed once the matching USDT withdrawal has completed_at set
                       (already covered by usdt_inflight's completed_at check, so we
                       just age these out after a safety window).
          usdt_to_cex: EVM->CEX USDT transfer; closed once a matching USDT deposit
                       appears at SafeTrade (any state) — usdt_inflight's to_cex then
                       takes over until credited. Safety expiry after 60min.
        Returns (open_wprl_to_prl, open_usdt_to_evm, open_prl_to_wprl, open_usdt_to_cex)."""
        import time as _t
        es = _ledger_load()
        if not es:
            return Decimal(0), Decimal(0), Decimal(0), Decimal(0)
        # PRL deposits seen (to match wprl_to_prl arrivals) — newest first w/ created ts
        try:
            deps = self.st.deposits(currency="prl", limit=20)
        except Exception:
            deps = []
        # USDT deposits seen (to match usdt_to_cex arrivals)
        try:
            usdt_deps = self.st.deposits(currency="usdt", limit=20)
        except Exception:
            usdt_deps = []
        # on-chain WPRL mints (to match prl_to_wprl arrivals)
        try:
            mints = [(float(a), ts) for a, ts in arb._wprl_mints_since(self.dex, from_block=0, lookback=80000)]
        except Exception:
            mints = []
        changed = False
        now = _t.time()
        # prl_to_wprl arrivals: batch through the shared matcher (each mint consumed at
        # most once, so one mint can't close two same-size entries — the per-entry loop
        # this replaces allowed exactly that).
        pw = [e for e in es if e.get("open") and e["kind"] == "prl_to_wprl"]
        pw_arrived = {id(e): ok for e, ok in
                      zip(pw, arb.match_bridge_arrivals([(e["amount"], e["ts"]) for e in pw], mints))}
        wds_prl = None   # lazy: PRL withdrawals, fetched only if an expiry decision needs relay state
        for e in es:
            if not e.get("open"):
                continue
            if e["kind"] == "wprl_to_prl":
                # a PRL deposit roughly == amount*(1-0.5%) created after the burn => arrived
                expected = e["amount"] * 0.995
                for d in deps:
                    try:
                        amt = float(d.get("amount") or 0)
                    except Exception:
                        amt = 0
                    if abs(amt - expected) <= expected * 0.02:
                        e["open"] = False; changed = True; break
                # NOT arrived as a deposit yet — consult the bridge relay state before any
                # age-based close. A `failed`/stuck burn (WPRL already burned, PRL never
                # credited) must NOT be silently closed: doing so hid thousands of WPRL on
                # 2026-06-13 (the only signal left was the conservation guard). Keep such
                # entries open so they stay counted + keep alerting until operator/refund.
                if e["open"]:
                    bstate = None
                    h = e.get("tx_hash")
                    if h:
                        try:
                            import pearlbridge_api as _pb
                            bstate = (_pb.burn_status(h) or {}).get("state")
                        except Exception:
                            bstate = None
                    if bstate in ("unlocked", "finalized", "refunded"):
                        e["open"] = False; changed = True   # bridge delivered / WPRL refunded
                    elif bstate in PB_BURN_BAD:
                        pass                                 # stuck -> keep open (no expiry)
                    elif now - e["ts"] > 3 * 3600:
                        # unknown state (legacy entry w/o hash, or API down) -> old safety
                        # expiry so a genuinely-missed match never sticks forever.
                        e["open"] = False; changed = True
            elif e["kind"] == "prl_to_wprl":
                # closes once a WPRL mint ~= ledger amount appears AFTER this bridge was
                # initiated (batch-matched above via the shared matcher).
                if pw_arrived.get(id(e)):
                    e["open"] = False; changed = True
                elif now - e["ts"] > 3 * 3600:
                    # Consult the relay BEFORE any age-based close (mirror of the forward
                    # branch's burn_status gate). The unconditional 3h expiry silently
                    # closed a still-pending reverse bridge once — books
                    # said "no in-flight" while the coins sat at the relay for ~3 days.
                    # Keep a non-terminal bridge open; expire only when the relay says
                    # terminal (minted/refunded) or when we can't resolve it at all
                    # (legacy entry w/o wid+txid, or API down -> old safety expiry).
                    state = None
                    try:
                        import pearlbridge_api as _pb
                        if wds_prl is None:
                            wds_prl = self.st.withdraws(currency="prl", limit=25)
                        txid = self._find_pearl_txid(e, wds_prl)
                        if txid:
                            state = (_pb.mint_status(txid) or {}).get("state")
                        terminal = _pb.MINT_STATES.get(state, (False, ""))[0] if state else None
                    except Exception:
                        state, terminal = None, None
                    if state is None or terminal:
                        e["open"] = False; changed = True
                    else:
                        emit(f"  ledger: prl_to_wprl {e['amount']:.0f} 已 {(now-e['ts'])/3600:.1f}h 未 mint "
                             f"(relay state={state}), 保持 open 不过期")
            elif e["kind"] == "usdt_to_evm":
                # ERC20 withdrawal lands in minutes; expire after 30min (completed_at
                # check in usdt_inflight is the primary signal anyway).
                if now - e["ts"] > 1800:
                    e["open"] = False; changed = True
            elif e["kind"] == "usdt_to_cex":
                # close once a USDT deposit ~= amount shows up at SafeTrade (any state) —
                # usdt_inflight.to_cex then counts it until credited (clean handoff, no
                # double-count). Safety expiry after 60min if a match is somehow missed.
                for d in usdt_deps:
                    try:
                        amt = float(d.get("amount") or 0)
                    except Exception:
                        amt = 0
                    if abs(amt - e["amount"]) <= max(2.0, e["amount"] * 0.02):
                        e["open"] = False; changed = True; break
                if e["open"] and now - e["ts"] > 3600:
                    e["open"] = False; changed = True
        if changed:
            _ledger_save(es)
        owp = sum(Decimal(str(e["amount"])) for e in es if e.get("open") and e["kind"] == "wprl_to_prl")
        ous = sum(Decimal(str(e["amount"])) for e in es if e.get("open") and e["kind"] == "usdt_to_evm")
        opw = sum(Decimal(str(e["amount"])) for e in es if e.get("open") and e["kind"] == "prl_to_wprl")
        ouc = sum(Decimal(str(e["amount"])) for e in es if e.get("open") and e["kind"] == "usdt_to_cex")
        return owp, ous, opw, ouc

    def _find_pearl_txid(self, e, wds):
        """Pearl txid (= SafeTrade PRL withdrawal's chain `txid`) for a prl_to_wprl
        ledger entry, to query /v1/mints relay state. New entries carry the withdrawal
        id (`wid`); legacy ones match by amount+time (entry amount = withdrawal - 4,
        the flat-fee convention used at _ledger_add time). None if unresolvable."""
        for w in wds or []:
            try:
                if e.get("wid") is not None and w.get("id") == e["wid"]:
                    return w.get("txid")
                if abs(float(w.get("amount") or 0) - (e["amount"] + 4)) < 0.011 and \
                        abs(_ep(w.get("created_at") or "") - e["ts"]) < 900:
                    return w.get("txid")
            except Exception:
                continue
        return None

    def bridge_round(self):
        cp = Decimal(str(self.st.balance("prl")))
        ew = Decimal(str(self.dex.balance(self.cfg.wprl_address)))
        # bidirectional in-flight (on-chain reconciled) — same as arb/data_hub
        to_cex, to_evm = arb._inflight(self.cfg, self.st, self.dex)
        # PLUS our own just-burned WPRL->PRL not yet showing as a CEX deposit (the blind
        # spot: left EVM, PRL deposit record not yet created). Ledger covers SUPPLY's
        # own burns; closes once the deposit appears.
        # NOTE: do NOT add the ledger's open bridge amounts to to_cex/to_evm — arb._inflight
        # now counts BOTH bridge directions ON-CHAIN (WPRL burns for to_cex, PRL withdrawals
        # for to_evm), so adding the ledger here DOUBLE-COUNTS the same bridge -> to_evm/to_cex
        # spike -> share misreads -> a WRONG bridge (this caused a spurious 935 forward bridge).
        # The ~12-30s on-chain-visibility blind spot is covered by the 5-min bridge cooldown.
        self.reconcile_ledger()        # still run it to close/expire ledger entries
        cex_eff = cp + to_cex
        evm_eff = ew + to_evm
        total = cex_eff + evm_eff
        if total <= 0:
            emit("  bridge: zero total token, skip"); return
        # ROOT-CAUSE GUARD: token conservation. If total strays >±25% from baseline, the
        # in-flight is corrupted (over/under-count) — do NOT bridge on bad data (a phantom
        # in-flight is exactly what mis-drove the two spurious bridges). Skip + alert; it
        # self-clears once the in-flight reconciles (RPC recovers / mint matches).
        if BASELINE_TOKEN > 0 and abs(total - Decimal(str(BASELINE_TOKEN))) > Decimal(str(BASELINE_TOKEN)) * TOKEN_CONSERVATION_BAND:
            emit(f"  bridge: ⚠ total token {float(total):.0f} implausible vs baseline {BASELINE_TOKEN:.0f} "
                 f"(>±{int(TOKEN_CONSERVATION_BAND*100)}%) -> in-flight corrupted, SKIP (no bridge on bad data)")
            return
        share = cex_eff / total * Decimal(100)
        deviation = abs(share - TARGET_SHARE)
        target_tok = total * TARGET_SHARE / Decimal(100)
        deficit = target_tok - cex_eff
        emit(f"  bridge: share={float(share):.1f}% (to_cex {float(to_cex):.0f} to_evm {float(to_evm):.0f}) "
             f"target={float(TARGET_SHARE):.0f}% dev={float(deviation):.1f}% deficit={float(deficit):.0f} WPRL")
        # deviation gate: let arb's inventory-aware spread rebalance via TRADING first;
        # only bridge (slow + the reverse leg has cost) when severely imbalanced.
        if deviation <= BRIDGE_TRIGGER_DEVIATION:
            emit(f"  bridge: dev {float(deviation):.1f}% <= {float(BRIDGE_TRIGGER_DEVIATION):.0f}%, skip (let arb rebalance)")
            return
        if deficit < 0:
            # share HIGH (CEX token-heavy) -> reverse PRL->WPRL to refill EVM. We're already
            # past the 15% gate, so ACT — bridge max(MIN_REVERSE_BRIDGE, deficit). The flat
            # 4 PRL fee means a single shot must be >= 800 to stay <=0.5%; when the real
            # deficit is under 800 we still bridge 800 (slight overshoot below target, then
            # dev drops <15% and it stops). Once the book is larger and the deficit exceeds
            # 800, max() picks the true deficit. (Old behavior waited when deficit<800, which
            # made the 15% gate a no-op — it'd trip but never bridge.)
            want = max(MIN_REVERSE_BRIDGE, -deficit)
            self._reverse_bridge(cp, want)
            return
        if deficit < MIN_BRIDGE:
            emit(f"  bridge: deficit {float(deficit):.0f} < {float(MIN_BRIDGE):.0f}, skip (trickle floor)")
            return
        if ew < deficit:
            emit(f"  bridge: EVM WPRL {float(ew):.0f} < deficit {float(deficit):.0f}; bridging what we have")
            deficit = ew - Decimal(1)
            if deficit < MIN_BRIDGE:
                emit("  bridge: not enough WPRL, skip"); return
        amt = deficit.quantize(Decimal("1"))

        # ===== COOLDOWN LOCK (share already counts in-flight, so this only stops firing
        # again before the last burn confirms on-chain; trickle continues normally) =====
        override = OVERRIDE_FILE.exists()
        import time as _t
        last = self._last_bridge_ts()
        if not override and last and (_t.time() - last) < BRIDGE_COOLDOWN_SEC:
            mins = (BRIDGE_COOLDOWN_SEC - (_t.time() - last)) / 60
            emit(f"  bridge COOLDOWN: last bridge {int((_t.time()-last)/60)}min ago, "
                 f"{mins:.0f}min left. (touch {OVERRIDE_FILE.name} to force)")
            return
        if override:
            emit(f"  bridge OVERRIDE: manual override file present, bypassing cooldown")

        if DRY_RUN:
            r = self.bridge.redeem(amt, PRL_DEPOSIT_ADDR, max_amount=amt + 1, dry_run=True)
            emit(f"  bridge DRY: would requestBurn {float(amt):.0f} WPRL -> CEX (sim={r.get('simulation')})")
        else:
            r = self.bridge.redeem(amt, PRL_DEPOSIT_ADDR, max_amount=amt + 1, dry_run=False)
            emit(f"  bridge LIVE: {float(amt):.0f} WPRL status={r.get('status')} tx={str(r.get('tx_hash'))[:14]}")
            if r.get("status") == 1:
                _ledger_add("wprl_to_prl", amt, tx_hash=r.get("tx_hash"))   # track until PRL shows up at CEX
                self._mark_bridge_ts()             # arm the cooldown lock
                if override:
                    try: OVERRIDE_FILE.unlink()    # one-shot: consume the override
                    except Exception: pass

    def usdt_inflight(self):
        """USDT in-flight, both directions (so usdt_share doesn't misread mid-transfer).
          to_evm: CEX->EVM USDT withdrawals NOT yet completed. A withdrawal is in-flight
                  only until SafeTrade sets completed_at + broadcasts the ERC20 tx; once
                  completed_at is set, the USDT lands on EVM within ~1 block, so it's
                  already reflected in the EVM balance — must NOT keep counting it (that
                  was the time-window bug: a long-completed withdrawal stayed 'in-flight').
          to_cex: SafeTrade USDT deposits not yet credited (EVM->CEX en route).
        """
        to_evm = Decimal(0); to_cex = Decimal(0)
        try:
            for w in self.st.withdraws(currency="usdt", limit=10):
                if w.get("state", w.get("status")) in ("failed", "rejected", "canceled", "errored"):
                    continue
                # completed_at set => already on-chain/landed => NOT in-flight.
                if not w.get("completed_at"):
                    to_evm += Decimal(str(w.get("amount") or 0))
        except Exception:
            pass
        try:
            for d in self.st.deposits(currency="usdt", limit=10):
                if not d.get("credited"):
                    to_cex += Decimal(str(d.get("amount") or 0))
        except Exception:
            pass
        return to_cex, to_evm

    def usdt_round(self):
        """Rebalance USDT toward USDT_TARGET_SHARE on CEX, symmetric to the token bridge.
        Looks at the USDT SHARE (incl in-flight), not an isolated inflow counter; moves
        the surplus side toward the deficit side only when deviation exceeds the gate."""
        cu = Decimal(str(self.st.balance("usdt")))           # CEX USDT
        eu = Decimal(str(self.dex.balance(self.cfg.quote_token_address)))  # EVM USDT
        to_cex, to_evm = self.usdt_inflight()
        # + supply's own in-flight not yet visible in balances/API:
        #   open_us: just-withdrawn USDT not yet on EVM (blind spot before completed_at)
        #   open_uc: just-sent EVM->CEX USDT not yet showing as a SafeTrade deposit
        _open_wp, open_us, _open_pw, open_uc = self.reconcile_ledger()
        to_evm = to_evm + open_us
        to_cex = to_cex + open_uc
        cex_eff = cu + to_cex
        evm_eff = eu + to_evm
        total = cex_eff + evm_eff
        if total <= 0:
            emit("  usdt: zero total, skip"); return
        share = cex_eff / total * Decimal(100)
        deviation = abs(share - USDT_TARGET_SHARE)
        target_cex = total * USDT_TARGET_SHARE / Decimal(100)
        surplus = cex_eff - target_cex   # >0: CEX has too much USDT -> send to EVM
        emit(f"  usdt: share={float(share):.1f}% (cex_eff {float(cex_eff):.0f} evm_eff {float(evm_eff):.0f}, "
             f"inflight to_cex {float(to_cex):.0f} to_evm {float(to_evm):.0f}) target={float(USDT_TARGET_SHARE):.0f}% dev={float(deviation):.1f}%")
        if deviation <= USDT_TRIGGER_DEVIATION:
            emit(f"  usdt: dev {float(deviation):.1f}% <= {float(USDT_TRIGGER_DEVIATION):.0f}%, balanced — no move")
            return
        if surplus > 0:
            # CEX has surplus USDT -> withdraw to EVM. Cap by actual CEX balance.
            amt = min(surplus, cu - Decimal(5)).quantize(Decimal("1"))
            if amt < MIN_USDT_MOVE:
                emit(f"  usdt: CEX surplus move {float(amt):.0f} < {float(MIN_USDT_MOVE):.0f}, skip"); return
            if DRY_RUN:
                emit(f"  usdt DRY: would withdraw {float(amt):.0f} USDT CEX->EVM"); return
            try:
                w = self.st.create_withdraw("usdt", int(amt), BENEFICIARY_ID)
                emit(f"  usdt LIVE: withdraw {float(amt):.0f} CEX->EVM status={w.get('status')} id={w.get('id')}")
                _ledger_add("usdt_to_evm", amt)   # track until it lands on EVM
            except Exception as e:
                emit(f"  usdt withdraw ERR: {str(e)[:150]}")
        else:
            # CEX short on USDT -> send EVM USDT to SafeTrade's deposit address. Plain
            # ERC20 transfer (only gas, no fee), symmetric to the CEX side: leave the
            # same $5 dust, no special EVM buffer. Cap by actual EVM USDT balance.
            need = -surplus
            amt = min(need, eu - Decimal(5)).quantize(Decimal("1"))
            if amt < MIN_USDT_MOVE:
                emit(f"  usdt: EVM->CEX move {float(amt):.0f} < {float(MIN_USDT_MOVE):.0f}, skip"); return
            if DRY_RUN:
                emit(f"  usdt DRY: would transfer {float(amt):.0f} USDT EVM->CEX"); return
            try:
                r = self.dex.transfer_erc20(self.cfg.quote_token_address, CEX_USDT_DEPOSIT_ADDR, amt)
                emit(f"  usdt LIVE: transferred {float(amt):.0f} EVM->CEX status={r.get('status')} tx={r.get('tx_hash')}")
                _ledger_add("usdt_to_cex", amt)   # track until SafeTrade shows the deposit
            except Exception as e:
                emit(f"  usdt EVM->CEX ERR: {str(e)[:150]}")

    def _reverse_bridge(self, cex_prl, want):
        """PRL->WPRL: withdraw `want` PRL from CEX to the bridge deposit address (whitelisted
        beneficiary), relayer auto-mints WPRL to EVM. ONE-SHOT, not a trickle — the flat
        4 PRL min fee means we only fire when the move is >= MIN_REVERSE_BRIDGE.
        Verified no-signature (the EIP-712 sig is a frontend gate, not required)."""
        # don't drain CEX below what arb needs to keep selling; leave a floor
        amt = min(want, cex_prl - Decimal(self.cfg.min_prl_inventory))
        if amt < MIN_REVERSE_BRIDGE:
            emit(f"  reverse PRL->WPRL: want {float(want):.0f} but movable {float(amt):.0f} "
                 f"< min {float(MIN_REVERSE_BRIDGE):.0f} (flat 4 PRL fee) — wait, no trickle")
            return
        # cooldown applies (same lock as forward) so we don't double-fire before arrival
        override = OVERRIDE_FILE.exists()
        import time as _t
        last = self._last_bridge_ts()
        if not override and last and (_t.time() - last) < BRIDGE_COOLDOWN_SEC:
            emit(f"  reverse PRL->WPRL COOLDOWN: {int((BRIDGE_COOLDOWN_SEC-(_t.time()-last))/60)}min left")
            return
        amt = amt.quantize(Decimal("1"))
        if DRY_RUN:
            emit(f"  reverse DRY: would withdraw {float(amt):.0f} PRL -> bridge (mints ~{float(amt)-4:.0f} WPRL)")
            return
        try:
            w = self.st.create_withdraw("prl", int(amt), beneficiary_id=PRL_WPRL_BENEFICIARY)
            emit(f"  reverse LIVE: withdrew {float(amt):.0f} PRL -> bridge (mints ~{float(amt)-4:.0f} WPRL) "
                 f"status={w.get('status')} id={w.get('id')}")
            _ledger_add("prl_to_wprl", float(amt) - 4, wid=w.get("id"))   # track the ~WPRL that'll mint to EVM
            self._mark_bridge_ts()
            if override:
                try: OVERRIDE_FILE.unlink()
                except Exception: pass
        except Exception as e:
            emit(f"  reverse PRL->WPRL ERR: {str(e)[:150]}")

    def run(self):
        emit(f"=== SUPPLY daemon start. token_target={float(TARGET_SHARE):.0f}% "
             f"usdt_target={float(USDT_TARGET_SHARE):.0f}% interval={BRIDGE_INTERVAL_SEC}s "
             f"bridge_dev_gate={float(BRIDGE_TRIGGER_DEVIATION):.0f}% usdt_dev_gate={float(USDT_TRIGGER_DEVIATION):.0f}% "
             f"DRY_RUN={DRY_RUN} ===")
        while True:
            try:
                self.bridge_round()   # forward WPRL->PRL trickle AND reverse PRL->WPRL (inside)
                self.usdt_round()     # USDT rebalance, both directions
            except Exception as e:
                emit(f"  loop error: {type(e).__name__}: {str(e)[:120]}")
            time.sleep(BRIDGE_INTERVAL_SEC)


if __name__ == "__main__":
    Supply().run()
