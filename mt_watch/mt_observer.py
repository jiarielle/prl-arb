#!/usr/bin/env python3
"""
margin.trade PRL 永续 套利观察器 —— 纯只读 / DRY ONLY / 完全独立。

设计原则（用户要求）:
  - 绝不侵入现有程序: 不 import auto_run/data_hub/supply, 不下单, 不碰 .env 密钥。
  - 只读两个来源:
      1) margin.trade 公开行情 (POST https://api.margin.trade/info, Hyperliquid schema, 无鉴权)
      2) 现有 ../state.json (data_hub 写的我们的现货参考价; 只读, 带 staleness 守护)
  - 输出: 算两个方向扣完 费+滑点 后的净 edge + funding carry, 到阈值才标记 SIGNAL,
          逐次 append 到本目录 mt_observer.jsonl, 同时打印一行人读 verdict。

跑法 (不进 systemd, 不进 cron, honor "不侵入"):
  一次性:   python3 mt_observer.py
  挂着跑:   nohup python3 mt_observer.py --loop 300 >> mt_watch.out 2>&1 &
  停:       kill 那个 pid (或 Ctrl-C)。纯观察, 随时可杀, 无副作用。
"""
import json, time, sys, os, urllib.request, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_JSON = os.path.join(HERE, "..", "state.json")
OUT_JSONL = os.path.join(HERE, "mt_observer.jsonl")

API = "https://api.margin.trade/info"
COIN = "PRL"

# --- 费率 (bps). margin.trade taker 0.045% = 4.5bps. 我们的现货腿费率沿用现有系统口径。
MT_TAKER_BPS = 4.5
SAFETRADE_TAKER_BPS = 20.0   # 与 config.py SAFETRADE_TAKER_FEE_BPS 一致
# DEX 的池子费+滑点已 embedded 在 state.json 的 dex_buy_px/dex_sell_px 里, 不再另扣。

# --- 信号阈值
EDGE_SIGNAL_BPS = 50.0          # 任一方向净 edge 超过这个才算"有肉", 触发 headline SIGNAL
INTEREST_FLOOR_APR = 11.4       # 观察到的 funding 利率底年化(%); premium 分量=0 时就是它
FUNDING_SIGNAL_APR = 25.0       # |funding 年化| 超过这个 = premium 分量在持续偏离 -> 有 carry 可吃
PREMIUM_NOTE = 0.005            # |瞬时 perp premium| 超过这个只记 note(薄盘常抖), 不触发 headline
NOTIONALS = [250.0, 500.0, 1000.0]  # 模拟下单的名义美元档位, 看容量衰减
STATE_STALE_SEC = 300


def _post(payload):
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (compatible; mt-observer/1.0)",
                 "Origin": "https://app.margin.trade"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def fetch_mt():
    """拉 PRL 的 mark/oracle/funding/OI/24h量 + l2 盘口。"""
    meta, ctxs = _post({"type": "metaAndAssetCtxs"})
    uni = meta["universe"]
    i = next(k for k, m in enumerate(uni) if m["name"] == COIN)
    c = ctxs[i]
    book = _post({"type": "l2Book", "coin": COIN})
    bids = [(float(l["px"]), float(l["sz"])) for l in book["levels"][0]]  # 降序
    asks = [(float(l["px"]), float(l["sz"])) for l in book["levels"][1]]  # 升序
    return {
        "markPx": float(c["markPx"]), "oraclePx": float(c["oraclePx"]),
        "midPx": float(c["midPx"]), "premium": float(c["premium"]),
        "funding": float(c["funding"]), "openInterest": float(c["openInterest"]),
        "dayNtlVlm": float(c["dayNtlVlm"]),
        "bids": bids, "asks": asks,
        "maxLeverage": uni[i]["maxLeverage"],
    }


def walk(levels, notional_usd):
    """吃掉 notional_usd 美元名义, 返回 (成交均价, 实际成交名义). 书太薄则成交名义<目标。"""
    rem = notional_usd
    cost = 0.0
    filled = 0.0
    for px, sz in levels:
        lvl_ntl = px * sz
        take = min(rem, lvl_ntl)
        qty = take / px
        cost += qty * px
        filled += qty
        rem -= take
        if rem <= 1e-9:
            break
    if filled == 0:
        return None, 0.0
    return cost / filled, cost  # avg_px, filled_notional


def depth_summary(mt):
    """盘口深度快照: 顶档价差 + mid 上下各档位内的累计单边名义($)。深度长没长是核心领先指标。"""
    bids, asks = mt["bids"], mt["asks"]
    if not bids or not asks:
        return {}
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    def cum(levels, lo, hi):  # 在 [lo,hi] 价格区间内累计名义美元
        return round(sum(px * sz for px, sz in levels if lo <= px <= hi), 0)
    return {
        "bbo_spread_bps": round((best_ask - best_bid) / mid * 1e4, 1),
        "bid_usd_1pct": cum(bids, mid * 0.99, mid), "ask_usd_1pct": cum(asks, mid, mid * 1.01),
        "bid_usd_2pct": cum(bids, mid * 0.98, mid), "ask_usd_2pct": cum(asks, mid, mid * 1.02),
        "bid_usd_5pct": cum(bids, mid * 0.95, mid), "ask_usd_5pct": cum(asks, mid, mid * 1.05),
        "bid_usd_total": cum(bids, 0, mid), "ask_usd_total": cum(asks, mid, 9e9),
    }


def read_state():
    try:
        d = json.load(open(STATE_JSON))
        ts = datetime.datetime.fromisoformat(d["ts"])
        age = (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds()
        return {
            "cex_bid": d["bid1_px"], "cex_ask": d["ask1_px"],
            "dex_buy": d["dex_buy_px"], "dex_sell": d["dex_sell_px"],
            "spot": d["spot"], "age_sec": age, "stale": age > STATE_STALE_SEC,
        }
    except Exception as e:
        return {"error": str(e)}


def edges_for(mt, st, notional):
    """两个 delta-neutral 方向, 净 edge(bps of perp mid)。spot 腿取两个现货场所里更优的。"""
    mid = mt["midPx"]
    out = {}

    # 方向 A: perp 便宜 -> LONG perp(吃 asks, +taker) + SELL 现货(收 bid, SafeTrade 扣 taker)
    perp_buy, fa = walk(mt["asks"], notional)
    if perp_buy:
        perp_buy_eff = perp_buy * (1 + MT_TAKER_BPS / 1e4)
        cex_sell = st.get("cex_bid", 0) * (1 - SAFETRADE_TAKER_BPS / 1e4) if "cex_bid" in st else 0
        dex_sell = st.get("dex_sell", 0)  # 已 net
        spot_sell = max(cex_sell, dex_sell)
        venue = "SafeTrade" if cex_sell >= dex_sell else "DEX"
        out["A_long_perp_sell_spot"] = {
            "net_bps": (spot_sell - perp_buy_eff) / mid * 1e4,
            "perp_fill": round(perp_buy, 5), "spot_sell": round(spot_sell, 5),
            "spot_venue": venue, "filled_usd": round(fa, 1),
        }

    # 方向 B: perp 贵 -> SHORT perp(吃 bids, -taker) + BUY 现货(付 ask/付 dex_buy)
    perp_sell, fb = walk(mt["bids"], notional)
    if perp_sell:
        perp_sell_eff = perp_sell * (1 - MT_TAKER_BPS / 1e4)
        cex_buy = st.get("cex_ask", 9e9) * (1 + SAFETRADE_TAKER_BPS / 1e4) if "cex_ask" in st else 9e9
        dex_buy = st.get("dex_buy", 9e9)
        spot_buy = min(cex_buy, dex_buy)
        venue = "SafeTrade" if cex_buy <= dex_buy else "DEX"
        out["B_short_perp_buy_spot"] = {
            "net_bps": (perp_sell_eff - spot_buy) / mid * 1e4,
            "perp_fill": round(perp_sell, 5), "spot_buy": round(spot_buy, 5),
            "spot_venue": venue, "filled_usd": round(fb, 1),
        }
    return out


def funding_apr(hourly):
    return hourly * 24 * 365 * 100  # %/yr


def run_once():
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        mt = fetch_mt()
    except Exception as e:
        line = {"ts": now, "error": f"mt fetch failed: {e}"}
        print(f"[{now}] ERROR mt fetch: {e}")
        with open(OUT_JSONL, "a") as f:
            f.write(json.dumps(line) + "\n")
        return
    st = read_state()

    depth = depth_summary(mt)
    by_size = {}
    best_edge = -1e9
    if "error" not in st and not st["stale"]:
        for n in NOTIONALS:
            e = edges_for(mt, st, n)
            by_size[str(int(n))] = e
            for d in e.values():
                best_edge = max(best_edge, d["net_bps"])

    apr = funding_apr(mt["funding"])
    # 用户决定: funding 在这种规模下可忽略(现在就是 ~11% 利率底, carry 微不足道)。
    # headline SIGNAL 只由"走盘口深度、扣费后能实际执行的净 edge"决定。funding 仅留作日志上下文。
    edge_flag = (best_edge >= EDGE_SIGNAL_BPS) if by_size else False
    funding_note = abs(apr) >= FUNDING_SIGNAL_APR     # 只记 note, 不触发 headline
    premium_note = abs(mt["premium"]) > PREMIUM_NOTE  # 只记 note(薄盘常抖), 不触发 headline
    signal = edge_flag

    rec = {
        "ts": now,
        "mt": {k: mt[k] for k in
               ["markPx", "oraclePx", "midPx", "premium", "funding",
                "openInterest", "dayNtlVlm", "maxLeverage"]},
        "funding_apr_pct": round(apr, 2),
        "oi_usd": round(mt["openInterest"] * mt["markPx"], 1),
        "depth": depth,
        "state": st,
        "edges_by_notional_bps": by_size,
        "best_edge_bps": round(best_edge, 1) if by_size else None,
        "SIGNAL": signal,  # 只看可执行 edge; funding 已按用户决定排除
        "signal_reason": (["edge>=%.0fbps" % EDGE_SIGNAL_BPS] if edge_flag else []),
        "note_premium_diverge": premium_note,
        "note_funding_carry": funding_note,
    }
    with open(OUT_JSONL, "a") as f:
        f.write(json.dumps(rec) + "\n")

    # 人读一行
    tag = "*** SIGNAL ***" if signal else ("no edge (premium wobble)" if premium_note else "no edge")
    oi = rec["oi_usd"]
    be = rec["best_edge_bps"]
    stale = "" if ("error" not in st and not st["stale"]) else " [spot stale/missing -> edge skipped]"
    dsp = (f"spread={depth['bbo_spread_bps']}bps depth±2%=${depth['bid_usd_2pct']:.0f}/"
           f"{depth['ask_usd_2pct']:.0f}") if depth else "depth=n/a"
    print(f"[{now}] {tag} | perp mid={mt['midPx']} oracle={mt['oraclePx']} "
          f"prem={mt['premium']} fund({apr:.1f}%/yr) "
          f"OI=${oi} vol24h=${mt['dayNtlVlm']:.0f} | {dsp} | best_net_edge={be}bps{stale}")
    if signal and by_size:
        for n, e in by_size.items():
            for k, d in e.items():
                if d["net_bps"] >= EDGE_SIGNAL_BPS:
                    print(f"    ${n}: {k} net={d['net_bps']:.1f}bps "
                          f"(perp {d['perp_fill']} vs spot {d.get('spot_sell') or d.get('spot_buy')} "
                          f"@{d['spot_venue']}, filled ${d['filled_usd']})")


def main():
    loop = None
    if "--loop" in sys.argv:
        loop = int(sys.argv[sys.argv.index("--loop") + 1])
    if loop:
        print(f"# mt_observer loop every {loop}s -> {OUT_JSONL}  (DRY, read-only, Ctrl-C to stop)")
        while True:
            run_once()
            time.sleep(loop)
    else:
        run_once()


if __name__ == "__main__":
    main()
