#!/usr/bin/env python3
"""WPRL/USDT 池子竞争对手扫描 (默认回看 2 天)。

输出机械事实, 供人工或 LLM 解读:
- 全池 swap 笔数/量, 我们的占比
- 已知演员 (logs/pool_actors.json) 逐个: 笔数/量/中位单笔/方向一致率/成交时我们的净边际
- 新地址 (笔数>=5 或 量>=$2k) 自动标记为待研究
- 肥边际 run (edge>=150bps) 分析: 段数/时长/谁吃掉的/没人碰自己漂回去的
- 80-150bps 带 (对手带) 占比

用法:
  pool_scan.py            # 扫描并打印报告
  pool_scan.py --days 7   # 自定回看窗口
  pool_scan.py push-text  # 从 stdin 读文本推到企业微信 ops 频道 (供 skill 推合成报告)

只读链上 + 只读本地日志, 无任何交易动作。tx.from 解析有持久缓存
(logs/.txfrom_cache.json), 重复运行不重复打 RPC。
"""
import json
import os
import sys
import time
import bisect
import datetime
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

UTC = datetime.timezone.utc
SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
EDGE_THRESHOLD = 150          # 我们的下单门槛 bps
RIVAL_BAND = (80, 150)        # 对手带
RUN_GAP_S = 120               # tick 间隔超过这个秒数则断 run
RUN_EATEN_WINDOW_S = 90       # run 结束 ±此秒数内有 swap 视为被吃
NEW_ACTOR_MIN_N = 5
NEW_ACTOR_MIN_VOL = 2000
BLOCKS_PER_DAY = 7200

ACTORS_FILE = ROOT / "logs" / "pool_actors.json"
TXFROM_CACHE = ROOT / "logs" / ".txfrom_cache.json"
STATE_FILE = ROOT / "logs" / "pool_scan_state.json"
TIMESERIES = ROOT / "logs" / "timeseries.jsonl"


def _load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def fetch_swaps(w3, pool, from_block, to_block):
    logs, step, b = [], 10000, from_block
    while b <= to_block:
        e = min(b + step - 1, to_block)
        try:
            logs.extend(w3.eth.get_logs(
                {"address": pool, "topics": [SWAP_TOPIC],
                 "fromBlock": b, "toBlock": e}))
        except Exception as ex:
            print(f"  get_logs {b}-{e} err: {str(ex)[:80]}, retry", file=sys.stderr)
            time.sleep(3)
            continue
        b = e + 1
        time.sleep(0.3)
    out = []
    for lg in logs:
        data = bytes(lg["data"])

        def i256(x):
            v = int.from_bytes(x, "big")
            return v - (1 << 256) if v >= (1 << 255) else v

        out.append({
            "block": lg["blockNumber"],
            "tx": lg["transactionHash"].hex(),
            "wprl": i256(data[0:32]) / 1e8,    # WPRL = 8 decimals (token0, confirmed on-chain) — NOT 18
            "usdt": i256(data[32:64]) / 1e6,
        })
    return out


def resolve_tx_from(w3, txs, min_block):
    cache = _load_json(TXFROM_CACHE, {})
    # 清掉老条目, 缓存只为窗口重叠服务
    cache = {h: v for h, v in cache.items()
             if isinstance(v, dict) and v.get("block", 0) >= min_block - BLOCKS_PER_DAY}
    for h, blk in txs:
        if h in cache:
            continue
        try:
            t = w3.eth.get_transaction(h)
            cache[h] = {"from": t["from"].lower(), "block": blk}
        except Exception:
            cache[h] = {"from": "?", "block": blk}
        time.sleep(0.12)
    json.dump(cache, open(TXFROM_CACHE, "w"))
    return {h: v["from"] for h, v in cache.items()}


def load_timeseries(since_ts):
    """(t[], edge[], raw_cex[], raw_dex[]) — 只读 since_ts 之后的 tick。"""
    t, edge, rc, rd = [], [], [], []
    # 文件可能很大, 从尾部按需读: 估算 1.5KB/tick, 16s/tick
    need_bytes = int((time.time() - since_ts) / 16 * 1500 * 1.5) + 1_000_000
    size = TIMESERIES.stat().st_size
    with open(TIMESERIES) as f:
        if size > need_bytes:
            f.seek(size - need_bytes)
            f.readline()  # 跳过半行
        for line in f:
            try:
                d = json.loads(line)
                ts = datetime.datetime.fromisoformat(d["ts"]).timestamp()
            except Exception:
                continue
            if ts < since_ts or d.get("edge_bps") is None:
                continue
            t.append(ts)
            edge.append(d["edge_bps"])
            rc.append(d.get("raw_cex_prem_bps"))
            rd.append(d.get("raw_dex_prem_bps"))
    return t, edge, rc, rd


def nearest(ts_arr, vals, t, tol=60):
    i = bisect.bisect_left(ts_arr, t)
    for j in (i - 1, i):
        if 0 <= j < len(ts_arr) and abs(ts_arr[j] - t) <= tol:
            return vals[j]
    return None


def fmt_addr(a):
    return a[:10] + "…"


def scan(days):
    from config import load_config
    from dex import DexClient
    cfg = load_config()
    dx = DexClient(cfg)
    w3 = dx.w3
    pool = w3.to_checksum_address(cfg.pool_address)
    our = cfg.wallet_address.lower()

    now_block = w3.eth.block_number
    from_block = now_block - days * BLOCKS_PER_DAY
    t0 = w3.eth.get_block(from_block)["timestamp"]
    t1 = w3.eth.get_block(now_block)["timestamp"]

    def blk_ts(b):
        return t0 + (b - from_block) * (t1 - t0) / max(1, now_block - from_block)

    swaps = fetch_swaps(w3, pool, from_block, now_block)
    for s in swaps:
        s["t"] = blk_ts(s["block"])
    txfrom = resolve_tx_from(
        w3, sorted({(s["tx"], s["block"]) for s in swaps}), from_block)

    ts_t, ts_edge, ts_rc, ts_rd = load_timeseries(t0)

    by_from = defaultdict(list)
    for s in swaps:
        by_from[txfrom.get(s["tx"], "?")].append(s)

    actors = _load_json(ACTORS_FILE, {})
    lines = []
    win = f"{datetime.datetime.fromtimestamp(t0, UTC):%m-%d %H:%M}→{datetime.datetime.fromtimestamp(t1, UTC):%m-%d %H:%M}"
    total_vol = sum(abs(s["usdt"]) for s in swaps)
    lines.append(f"WPRL池扫描 {win} UTC ({days}d)")
    lines.append(f"全池: {len(swaps)}笔 ${total_vol:,.0f}")

    # edge 带占比
    if ts_edge:
        n = len(ts_edge)
        hi = sum(1 for e in ts_edge if e >= EDGE_THRESHOLD)
        mid = sum(1 for e in ts_edge if RIVAL_BAND[0] <= e < RIVAL_BAND[1])
        lines.append(f"edge占比: >={EDGE_THRESHOLD}bps {hi/n*100:.1f}% | "
                     f"{RIVAL_BAND[0]}-{RIVAL_BAND[1]} {mid/n*100:.1f}% (tick={n})")
    else:
        lines.append("edge时序: 无覆盖 (data_hub日志缺这段)")

    # 逐演员统计
    def actor_stats(f, ss):
        vol = sum(abs(x["usdt"]) for x in ss)
        sizes = sorted(abs(x["usdt"]) for x in ss)
        med = sizes[len(sizes) // 2]
        cons = tot = 0
        edges = []
        for s in ss:
            e = nearest(ts_t, ts_edge, s["t"])
            if e is not None:
                edges.append(e)
            rc = nearest(ts_t, ts_rc, s["t"])
            rd = nearest(ts_t, ts_rd, s["t"])
            if rc is None or rd is None:
                continue
            tot += 1
            if (s["wprl"] < 0 and rc > 0) or (s["wprl"] > 0 and rd > 0):
                cons += 1
        med_edge = sorted(edges)[len(edges) // 2] if edges else None
        return {"n": len(ss), "vol": vol, "med": med,
                "cons": cons, "cons_tot": tot,
                "med_edge": med_edge}

    state = _load_json(STATE_FILE, {})
    prev = state.get("actors", {})
    lines.append("")
    lines.append("已知演员:")
    cur_actors = {}
    for f, label in actors.items():
        ss = by_from.get(f)
        if not ss:
            lines.append(f"  {fmt_addr(f)} {label}: 0笔")
            continue
        st = actor_stats(f, ss)
        cur_actors[f] = st
        me = f"边际中位{st['med_edge']:+.0f}bps" if st["med_edge"] is not None else "无edge覆盖"
        delta = ""
        p = prev.get(f)
        if p and p.get("med_edge") is not None and st["med_edge"] is not None:
            delta = f" (上期{p['med_edge']:+.0f})"
        tag = " ←我们" if f == our else ""
        lines.append(f"  {fmt_addr(f)} {label}: {st['n']}笔 ${st['vol']:,.0f} "
                     f"中位${st['med']:.0f} 方向一致{st['cons']}/{st['cons_tot']} {me}{delta}{tag}")

    # 新地址
    news = []
    for f, ss in by_from.items():
        if f in actors or f == "?":
            continue
        vol = sum(abs(x["usdt"]) for x in ss)
        if len(ss) >= NEW_ACTOR_MIN_N or vol >= NEW_ACTOR_MIN_VOL:
            st = actor_stats(f, ss)
            news.append((f, st))
    if news:
        lines.append("")
        lines.append("新地址 (待研究):")
        for f, st in sorted(news, key=lambda x: -x[1]["vol"])[:6]:
            me = f"边际中位{st['med_edge']:+.0f}bps" if st["med_edge"] is not None else ""
            lines.append(f"  {f}: {st['n']}笔 ${st['vol']:,.0f} 中位${st['med']:.0f} "
                         f"方向一致{st['cons']}/{st['cons_tot']} {me}")

    # 肥边际 run 分析
    if ts_edge:
        runs, cur = [], None
        for t, e in zip(ts_t, ts_edge):
            if e >= EDGE_THRESHOLD:
                if cur and t - cur[1] <= RUN_GAP_S:
                    cur = (cur[0], t)
                else:
                    if cur:
                        runs.append(cur)
                    cur = (t, t)
            else:
                if cur:
                    runs.append(cur)
                    cur = None
        if cur:
            runs.append(cur)
        sw = sorted((s["t"], txfrom.get(s["tx"], "?")) for s in swaps)
        sw_t = [x[0] for x in sw]
        eaters = Counter()
        drift = 0
        for a, b in runs:
            i = bisect.bisect_left(sw_t, b - RUN_EATEN_WINDOW_S)
            run_eaters = set()
            while i < len(sw) and sw_t[i] <= b + RUN_EATEN_WINDOW_S:
                run_eaters.add(sw[i][1])
                i += 1
            for f in run_eaters:
                eaters[f] += 1
            if not run_eaters:
                drift += 1
        durs = sorted((b - a) / 60 for a, b in runs)
        lines.append("")
        if runs:
            longest = max((b - a) / 60 for a, b in runs)
            our_share = eaters.get(our, 0)
            top_rival = max(((f, n) for f, n in eaters.items() if f != our),
                            key=lambda x: x[1], default=None)
            rival_s = f" 最强对手{fmt_addr(top_rival[0])}:{top_rival[1]}" if top_rival else ""
            lines.append(f"肥边际run(>= {EDGE_THRESHOLD}bps): {len(runs)}段 "
                         f"中位{durs[len(durs)//2]:.1f}min 最长{longest:.0f}min | "
                         f"我们参与收口{our_share}段{rival_s} 没人碰{drift}段")
        else:
            lines.append(f"肥边际run(>={EDGE_THRESHOLD}bps): 0段 (窗口内无机会)")

    # 我们的占比
    ours = by_from.get(our, [])
    our_vol = sum(abs(s["usdt"]) for s in ours)
    lines.append(f"我们: {len(ours)}笔 ${our_vol:,.0f} "
                 f"(占全池量 {our_vol/total_vol*100:.0f}%)" if total_vol else "我们: 0笔")

    json.dump({"ts": int(time.time()), "actors": cur_actors}, open(STATE_FILE, "w"))
    return "\n".join(lines)


def push_text(text):
    import requests
    corp = os.getenv("WECHAT_CORP_ID", "")
    secret = os.getenv("WECHAT_OPS_SECRET", "")
    agent = os.getenv("WECHAT_OPS_AGENT_ID", "")
    admin = os.getenv("WECHAT_ADMIN_USER_ID", "")
    r = requests.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                     params={"corpid": corp, "corpsecret": secret}, timeout=15)
    tok = r.json().get("access_token")
    if not tok:
        raise RuntimeError(f"gettoken failed: {r.text[:200]}")
    r = requests.post(
        f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={tok}",
        json={"touser": admin, "msgtype": "text", "agentid": int(agent),
              "text": {"content": text}}, timeout=15)
    return r.json()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "push-text":
        print(push_text(sys.stdin.read().strip()))
        sys.exit(0)
    days = 2
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    print(scan(days))
