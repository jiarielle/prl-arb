#!/usr/bin/env python3
"""reverse 桥 (PRL->WPRL) 真实 fee —— realized 口径: 进(离开CEX的PRL) − 出(链上铸到的WPRL)。

为什么不用公式: 代码里假设 "flat 4 PRL fee", 但实测大额都是 0.5% 比例费, flat-4 在大额低估。realized = 进-出 才是
ground truth, 还能反查他们费率有没有改。公式只做交叉复检: realized 偏离 0.5% 超容忍 -> 告警
(可能费率变了 / 部分铸 / 退款 / 卡桥)。

数据源(都已有, 只读):
  进  = SafeTrade PRL 提现 amount + fee  (离开 CEX 的总 PRL)
  出  = /v1/mints/{txid}.amountGrains / 1e8  (链上实际铸到的 WPRL, 8 位精度)
落盘 logs/bridge_fees.jsonl, 每笔只算一次(minted 终态后), 可人工复检。
"""
import json, time, sys
from pathlib import Path
from datetime import datetime

import pearlbridge_api

ROOT = Path(__file__).resolve().parent
LEDGER = ROOT / "logs" / "bridge_fees.jsonl"
WPRL_DECIMALS = 8
EXPECTED_PCT = 0.005      # 实测费率 0.5%
PCT_TOL = 0.001           # realized 偏离 0.5% 超过这个 -> 复检告警


def _ep(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _load():
    d = {}
    try:
        for l in LEDGER.read_text().splitlines():
            if l.strip():
                r = json.loads(l)
                d[r["id"]] = r
    except FileNotFoundError:
        pass
    return d


def update(st, limit=30):
    """拉最近 reverse 桥提现, 对已 minted 且未记账的算 realized fee 落盘。
    返回 (ledger dict, 复检告警 list)。"""
    led = _load()
    alerts = []
    try:
        wds = st.withdraws(currency="prl", limit=limit)
    except Exception:
        return led, alerts
    new = []
    for w in wds:
        wid = w.get("id")
        if wid is None or wid in led:
            continue
        txid = w.get("txid")
        if not txid:
            continue                                  # 还没广播链上, 下轮再算
        m = pearlbridge_api.mint_status(txid)
        if not m or m.get("state") != "minted":
            continue                                  # 未到账/卡住 -> 不计 realized(在途, 非损耗)
        grains = m.get("amountGrains")
        if grains is None:
            continue
        out = float(grains) / (10 ** WPRL_DECIMALS)
        amt = float(w.get("amount") or 0)
        cexfee = float(w.get("fee") or 0)
        inn = amt + cexfee                            # 离开 CEX 的总 PRL
        fee = inn - out                               # realized 总损耗(桥 + CEX 提现费)
        pct = fee / inn if inn else 0
        rec = {"id": wid, "ts": _ep(w["created_at"]), "txid": txid,
               "in": round(inn, 4), "out": round(out, 5), "fee": round(fee, 4),
               "pct": round(pct, 5), "cex_fee": cexfee,
               "formula_05pct": round(inn * EXPECTED_PCT, 4)}
        led[wid] = rec
        new.append(rec)
        if abs(pct - EXPECTED_PCT) > PCT_TOL:
            alerts.append(f"桥费率异常 id={wid} {inn:.0f}PRL realized={fee:.2f}"
                          f"({pct*100:.2f}%≠0.5%) -> 去复检")
    if new:
        with open(LEDGER, "a") as f:
            for r in sorted(new, key=lambda x: x["ts"]):
                f.write(json.dumps(r) + "\n")
    return led, alerts


def summarize(st, window_sec, limit=30):
    """report 用: 返回累计 + 窗口内 realized 桥费 + 复检告警。"""
    led, alerts = update(st, limit)
    now = time.time()
    cum = sum(r["fee"] for r in led.values())
    win = sum(r["fee"] for r in led.values() if now - r["ts"] <= window_sec)
    win_n = sum(1 for r in led.values() if now - r["ts"] <= window_sec)
    return {"cum_fee": cum, "win_fee": win, "n": len(led), "win_n": win_n, "alerts": alerts}


if __name__ == "__main__":
    # 手动: backfill + 打印明细。 python3 bridge_fees.py [limit]
    from config import load_config
    from safetrade import SafeTradeClient
    c = load_config()
    st = SafeTradeClient(c.safetrade_api_key, c.safetrade_api_secret, c.safetrade_base_url)
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    led, alerts = update(st, limit)
    rows = sorted(led.values(), key=lambda x: x["ts"])
    print(f"{'date':16} {'in':>9} {'out':>10} {'fee':>7} {'pct':>6} {'flat4?':>7}")
    for r in rows:
        d = datetime.utcfromtimestamp(r["ts"]).strftime("%m-%d %H:%M")
        flat4_gap = r["fee"] - 4
        print(f"{d:16} {r['in']:>9.2f} {r['out']:>10.3f} {r['fee']:>7.3f} "
              f"{r['pct']*100:>5.2f}% {flat4_gap:>+6.2f}")
    cum = sum(r["fee"] for r in rows)
    formula_flat4 = 4 * len(rows)
    formula_05 = sum(r["formula_05pct"] for r in rows)
    print("-" * 60)
    print(f"reverse 桥 {len(rows)} 笔 | realized 累计 fee = {cum:.2f} PRL")
    print(f"  交叉复检: 公式flat-4 会算成 {formula_flat4:.0f} PRL (差 {cum-formula_flat4:+.1f});"
          f" 公式0.5% = {formula_05:.2f} PRL (差 {cum-formula_05:+.2f})")
    if alerts:
        print("复检告警:", *alerts, sep="\n  ")
