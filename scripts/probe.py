#!/usr/bin/env python3
"""临时 桥+交易 探针(用后即弃)。事件即推企业微信 ops 频道:
  - 新成交成功
  - 桥发起 / 桥到账(成功) / 桥超时未到账(失败,疑卡需 claim)
后台跑:  nohup <repo>/.venv/bin/python scripts/probe.py >> logs/probe.log 2>&1 &
用完即弃:  pkill -f scripts/probe.py
"""
import os, sys, re, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
import requests
from config import load_config
from safetrade import SafeTradeClient
from dex import DexClient
import arb
import json

CORP = os.getenv("WECHAT_CORP_ID"); SEC = os.getenv("WECHAT_OPS_SECRET")
AG = os.getenv("WECHAT_OPS_AGENT_ID"); ADMIN = os.getenv("WECHAT_ADMIN_USER_ID")
SUPPLY = ROOT / "logs" / "supply.log"; TRADES = ROOT / "logs" / "trades.jsonl"
POLL = 60
BRIDGE_TIMEOUT = 75 * 60          # 75min 未到账 -> 判超时/卡
MAX_RUNTIME = 12 * 3600           # 跑满 12h 自动退出(用后即弃)

cfg = load_config()
st = SafeTradeClient(cfg.safetrade_api_key, cfg.safetrade_api_secret, cfg.safetrade_base_url)
dex = DexClient(cfg)


def push(text):
    try:
        tok = requests.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                           params={"corpid": CORP, "corpsecret": SEC}, timeout=15).json().get("access_token")
        requests.post(f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={tok}",
                      json={"touser": ADMIN, "msgtype": "text", "agentid": int(AG), "text": {"content": text}}, timeout=15)
        print(time.strftime("%H:%M:%S"), "PUSH:", text.replace("\n", " | "))
    except Exception as e:
        print("push err", e)


def trades_rows():
    try:
        return [json.loads(l) for l in open(TRADES) if l.strip()]
    except Exception:
        return []


def supply_lines():
    try:
        return open(SUPPLY).read().splitlines()[-300:]
    except Exception:
        return []


def mints():
    try:
        return arb._wprl_mints_since(dex, from_block=0, lookback=80000)
    except Exception:
        return []


def prl_deps():
    try:
        return st.deposits(currency="prl", limit=20)
    except Exception:
        return []


def main():
    seen_trades = len(trades_rows())
    seen_init = set(l for l in supply_lines() if "LIVE:" in l)   # 已存在的桥不补报
    seen_mint = set((round(float(a)), int(ts or 0)) for a, ts in mints())
    seen_dep = set(str(d.get("id")) for d in prl_deps())
    pending = []   # {kind, amt, expect, ts, line}
    re_rev = re.compile(r"reverse LIVE: withdrew ([\d.]+) PRL")
    re_fwd = re.compile(r"bridge LIVE: ([\d.]+) WPRL")
    start = time.time()
    push("🔎 临时探针已启动:新成交 / 桥发起·到账·超时 即推。12h 后自动结束;手动停:pkill -f scripts/probe.py")

    while True:
        if time.time() - start > MAX_RUNTIME:
            push("🔚 临时探针已运行 12h,自动结束。需要再起:nohup .venv/bin/python scripts/probe.py >> logs/probe.log 2>&1 &")
            break
        try:
            # --- 成交 ---
            rows = trades_rows()
            if len(rows) > seen_trades:
                new = rows[seen_trades:]
                seen_trades = len(rows)
                msgs = []
                for r in new:
                    if r.get("ok"):
                        msgs.append(f"  {r.get('dir')} +${float(r.get('realized',0)):.2f}")
                if msgs:
                    tot = sum(float(r.get("realized", 0)) for r in new if r.get("ok"))
                    push(f"✅ 新成交 {len(msgs)} 笔 (累计第{seen_trades}笔)\n" + "\n".join(msgs) + f"\n本批 +${tot:.2f}")

            # --- 桥发起 ---
            for l in supply_lines():
                if "LIVE:" not in l or l in seen_init:
                    continue
                seen_init.add(l)
                m = re_rev.search(l)
                if m:
                    amt = float(m.group(1)); exp = amt - 4
                    pending.append({"kind": "反向 PRL→WPRL", "amt": amt, "expect": exp, "ts": time.time(), "settle": "mint"})
                    push(f"🌉 桥发起 反向 PRL→WPRL: 提 {amt:.0f} PRL,等铸 ~{exp:.0f} WPRL 回 EVM")
                    continue
                m = re_fwd.search(l)
                if m:
                    amt = float(m.group(1)); exp = amt
                    pending.append({"kind": "正向 WPRL→PRL", "amt": amt, "expect": exp, "ts": time.time(), "settle": "dep"})
                    push(f"🌉 桥发起 正向 WPRL→PRL: 烧 {amt:.0f} WPRL,等 ~{exp:.0f} PRL 到 CEX")

            # --- 桥结算 / 超时 ---
            if pending:
                cur_mints = mints()
                cur_deps = prl_deps()
                still = []
                for p in pending:
                    done = False
                    if p["settle"] == "mint":
                        for a, ts in cur_mints:
                            key = (round(float(a)), int(ts or 0))
                            if key in seen_mint:
                                continue
                            if abs(float(a) - p["expect"]) <= max(5, p["expect"] * 0.05):
                                seen_mint.add(key); done = True; break
                    else:  # dep
                        for d in cur_deps:
                            if str(d.get("id")) in seen_dep:
                                continue
                            if not d.get("credited"):
                                continue
                            if abs(float(d.get("amount") or 0) - p["expect"]) <= max(5, p["expect"] * 0.05):
                                seen_dep.add(str(d.get("id"))); done = True; break
                    if done:
                        push(f"✅ 桥到账 {p['kind']}: ~{p['expect']:.0f} 已落地({int((time.time()-p['ts'])/60)}min)")
                    elif time.time() - p["ts"] > BRIDGE_TIMEOUT:
                        push(f"⚠️ 桥超时未到账 {p['kind']}: ~{p['expect']:.0f} 已 {int((time.time()-p['ts'])/60)}min 未见!疑卡住→去 pearlbridge 手动 claim")
                    else:
                        still.append(p)
                # 把刚出现的 mint/dep 都记入 seen,避免下轮误判
                for a, ts in cur_mints:
                    seen_mint.add((round(float(a)), int(ts or 0)))
                for d in cur_deps:
                    seen_dep.add(str(d.get("id")))
                pending = still
        except Exception as e:
            print("loop err", type(e).__name__, str(e)[:100])
        time.sleep(POLL)


if __name__ == "__main__":
    main()
