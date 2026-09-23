"""bridge_links — on-demand: list recent bridges in BOTH directions with real txs +
clickable URLs, so you never have to hunt for a tx.

Both directions are really "submit a tx on one chain, wait for the mint/credit on the other":

  REVERSE  PRL->WPRL  (refill EVM):
     tx1 = CEX PRL withdrawal to the bridge deposit addr (SafeTrade `txid`)
     tx2 = relayer mints WPRL on Ethereum (etherscan, from 0x0 -> us)
     -> if stuck, CLAIM on the PearlBridge frontend.

  FORWARD  WPRL->PRL  (refill CEX):
     tx1 = our requestBurn on Ethereum (etherscan tx hash — we sent it)
     tx2 = relayer mints PRL -> lands as a CEX deposit (SafeTrade `txid`)
     -> fully automatic; the wait is SafeTrade crediting (~46min), no claim needed.

Run:  /opt/miniconda3/bin/python -u bridge_links.py
"""
import sys
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config import load_config
from safetrade import SafeTradeClient
from dex import DexClient
import arb
import pearlbridge_api

ETHERSCAN = "https://etherscan.io"
PEARLBRIDGE = "https://pearlbridge.xyz"


def _ep(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _age_min(s, now):
    try:
        return (now - _ep(s)) / 60.0
    except Exception:
        return None


def _burns_with_hash(dex, lookback=80000):
    """Recent WPRL burns (me->0x0) as [(amount, ts, tx_hash)] — like arb._wprl_burns_since
    but also captures the tx hash so we can link the burn on etherscan."""
    w3 = dex.w3
    me = arb.Web3_checksum(dex.address)
    wprl = arb.Web3_checksum(dex.cfg.wprl_address)
    zero = "0x0000000000000000000000000000000000000000"
    latest = w3.eth.block_number
    start = max(0, latest - lookback)
    out, page_key = [], None
    for _ in range(20):
        params = {
            "fromBlock": hex(start), "toBlock": "latest",
            "fromAddress": me, "toAddress": zero,
            "contractAddresses": [wprl], "category": ["erc20"],
            "withMetadata": True, "order": "desc", "maxCount": "0x64",
        }
        if page_key:
            params["pageKey"] = page_key
        resp = arb._alchemy_xfers(w3, params)
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
                    ts = int(_ep(bts))
                except Exception:
                    ts = 0
            out.append((amt, ts, t.get("hash")))
        page_key = result.get("pageKey")
        if not page_key:
            break
    return out


def main():
    cfg = load_config()
    st = SafeTradeClient(cfg.safetrade_api_key, cfg.safetrade_api_secret, cfg.safetrade_base_url)
    dex = DexClient(cfg)
    me = dex.address
    now = datetime.now(timezone.utc).timestamp()

    print(f"\nEVM 地址: {me}")
    print(f"PearlBridge(连此钱包看 pending mint / claim): {PEARLBRIDGE}/?ethAddress={me}")
    print(f"我们地址全部代币流水(看 WPRL 进出): {ETHERSCAN}/address/{me}#tokentxns\n")

    # ---- REVERSE  PRL->WPRL (CEX -> EVM) ----
    print("══ 反向桥 PRL→WPRL (补 EVM):CEX 提币 → 以太坊铸 WPRL ══")
    try:
        wds = [w for w in st.withdraws(currency="prl", limit=15)
               if w.get("state", w.get("status")) not in ("failed", "rejected", "canceled", "errored")]
        # 24h 窗口: 6/10 那笔卡了 13h, 旧的 6h 窗口让它从这个工具里消失, 排查时误导
        recent = [w for w in wds if (a := _age_min(w.get("created_at") or "", now)) is not None and a < 24 * 60]
        # on-chain mints to decide which withdrawals have already minted WPRL
        mints = arb._wprl_mints_since(dex, from_block=0, lookback=80000)
        # sort mints ASCENDING by ts so each (oldest-first) withdrawal claims the EARLIEST
        # valid mint — else an older withdrawal grabs a younger same-size mint that actually
        # belongs to a later withdrawal, falsely flagging the later one as stuck (two ~1700
        # bridges hours apart is exactly this trap).
        mrem = sorted(mints, key=lambda x: x[1])
        fee_rate = Decimal(cfg.bridge_fee_bps_prl_to_wprl) / Decimal(10000)
        min_fee = Decimal(str(cfg.bridge_min_prl))
        if not recent:
            print("  (近 24h 无反向桥)")
        for w in sorted(recent, key=lambda x: x.get("created_at") or ""):
            amt = Decimal(str(w.get("amount") or 0))
            expected = amt - max(amt * fee_rate, min_fee)
            wts = _ep(w.get("completed_at") or w.get("created_at"))
            age = _age_min(w.get("created_at") or "", now) or 0
            matched = None
            for i, (mv, mt) in enumerate(mrem):
                if mt and wts and mt < wts - 120:
                    continue
                if abs(mv - expected) <= expected * Decimal("0.02"):
                    matched = i; break
            txid = w.get("txid") or ""
            if matched is not None:
                mrem.pop(matched)
                print(f"  ✓ {float(amt):.0f} PRL  已铸 WPRL  ({age:.0f}min前)  PRL链上tx={txid}")
            else:
                flag = "🚨卡住,去claim" if age > 60 else "⏳在途(正常~19min)"
                # relay 状态机实查(比链上 mint 匹配更准确): state + 中文一句话
                state_s = ""
                if txid:
                    m = pearlbridge_api.mint_status(txid)
                    if m:
                        s = m.get("state")
                        state_s = f"  relay状态={s}({pearlbridge_api.MINT_STATES.get(s, (0, '?'))[1]})"
                print(f"  {flag}  {float(amt):.0f} PRL→~{float(expected):.0f} WPRL  已等 {age:.0f}min{state_s}")
                print(f"     PRL链上tx(提到桥)={txid}")
                # /order/<pearlTxid> 是官方逐单状态页(取代了不可推导的 r_<uuid> 链接)
                print(f"     claim: {pearlbridge_api.ORDER_URL.format(txid=txid) if txid else f'{PEARLBRIDGE}/?ethAddress={me}'}")
    except Exception as e:
        print(f"  反向桥读取失败: {type(e).__name__}: {str(e)[:120]}")

    # ---- FORWARD  WPRL->PRL (EVM -> CEX) ----
    print("\n══ 正向桥 WPRL→PRL (补 CEX):以太坊烧 WPRL → CEX 充值 PRL ══")
    try:
        burns = _burns_with_hash(dex)
        recent_b = [(a, t, h) for a, t, h in burns if t and (now - t) < 6 * 3600]
        # credited PRL deposits = arrived forwards (with their CEX txid)
        deps = []
        for d in st.deposits(currency="prl", limit=20):
            deps.append((Decimal(str(d.get("amount") or 0)), _ep(d.get("created_at") or "1970-01-01T00:00:00Z"),
                         d.get("credited"), d.get("txid") or ""))
        drem = sorted(deps, key=lambda x: x[1])   # earliest-first match (same ordering fix as reverse)
        if not recent_b:
            print("  (近 6h 无正向桥)")
        for bamt, bts, bhash in sorted(recent_b, key=lambda x: x[1]):
            age = (now - bts) / 60.0
            matched = None
            for i, (camt, cts, cred, ctxid) in enumerate(drem):
                if cts and bts and cts < bts - 120:
                    continue
                if abs(camt - bamt) <= bamt * Decimal("0.02"):
                    matched = i; break
            burn_url = f"{ETHERSCAN}/tx/{bhash}" if bhash else "(无hash)"
            if matched is not None:
                camt, cts, cred, ctxid = drem.pop(matched)
                if cred:
                    print(f"  ✓ {float(bamt):.0f} WPRL  PRL已入账CEX  ({age:.0f}min前)")
                    print(f"     烧币tx: {burn_url}   PRL入账tx={ctxid}")
                else:
                    print(f"  ⏳ {float(bamt):.0f} WPRL  PRL已到CEX但未入账(等~46min crediting)  已等{age:.0f}min")
                    print(f"     烧币tx: {burn_url}   PRL入账tx={ctxid}")
            else:
                flag = "🚨久未到" if age > 90 else "⏳在途(正常~50min,瓶颈=CEX入账)"
                print(f"  {flag}  {float(bamt):.0f} WPRL→~PRL  已等 {age:.0f}min")
                print(f"     烧币tx: {burn_url}")
    except Exception as e:
        print(f"  正向桥读取失败: {type(e).__name__}: {str(e)[:120]}")
    print()


if __name__ == "__main__":
    main()
