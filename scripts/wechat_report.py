#!/usr/bin/env python3
"""prl-mm 6小时巡检报告 -> 企业微信 ops 频道(自包含,无 health 依赖)。

报告内容(按用户要求): 多少波交易 / 盈亏vs基线(真实到手) / 币价 / 桥损耗 / 是否有异常。
(2026-08-06 用户要求: 去掉腿式累计显示 —— 腿式不含桥成本+残差按spot折U偏乐观, 以vs基线为准。)
机械检查为主; CUM LOSS / 进程 errored / .STOP 等关键异常会标红提示人工核实。
由系统 cron 每 6h 调用。所有检查 try/except 包裹,绝不让 cron 崩。
"""
import os, sys, json, subprocess, time, re, datetime
from pathlib import Path
from decimal import Decimal

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# load creds from prl/.env
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

CORP_ID = os.getenv("WECHAT_CORP_ID", "")
OPS_SECRET = os.getenv("WECHAT_OPS_SECRET", "")
OPS_AGENT = os.getenv("WECHAT_OPS_AGENT_ID", "")
ADMIN = os.getenv("WECHAT_ADMIN_USER_ID", "")

TRADES = ROOT / "logs" / "trades.jsonl"
AUTORUN_LOG = ROOT / "logs" / "auto_run.log"
SUPPLY_LOG = ROOT / "logs" / "supply.log"
STATE = ROOT / "logs" / ".last_report.json"
STOP = ROOT / ".STOP"

# NOTE: 不放裸 "halt" —— "time budget exhausted -> halt" 是 16h 正常自重启(良性)。
# 真正要抓的永久熔断是 "CUM LOSS" / "permanent halt"。
# 去掉 "revert"(用户 2026-06-16): DEX 腿 reverted 是薄池滑点护栏正常触发, 系统自动反转
# CEX 腿保持中性, 仅耗 gas, 不需上报。反转也失败(裸敞口)走 "HEDGE ABORT", 仍在抓。
CRIT = ["HEDGE ABORT", "UNCONFIRMED", "nonce", "LOW GAS", "CUM LOSS", "permanent halt"]

ALERT_MAX_AGE_SEC = 6 * 3600   # 用户 2026-06-16: 超过 6h(报告窗口)的日志告警不再上报


def trades_summary():
    """(总波数, 腿式累计收益, 近6h腿式收益)。realized 求和 = 跨重启的真实累计。
    近6h 按 trade ts 过滤(与桥损耗窗口同口径), 不用 state 基准。"""
    n = 0
    cum = Decimal(0)
    win = Decimal(0)
    cutoff = time.time() - 6 * 3600
    try:
        for line in open(TRADES):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            rc = d.get("revert_cost_usd")
            if d.get("ok") or rc is not None:
                if d.get("ok"):
                    n += 1
                r = Decimal(str(d.get("realized", 0))) if d.get("ok") else Decimal(str(rc))
                cum += r
                try:
                    from datetime import datetime
                    if datetime.fromisoformat(d["ts"]).timestamp() >= cutoff:
                        win += r
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return n, cum, win


def pnl_coin_u():
    """累计 PnL 的两个无价数字 (币ΔX, UΔY) = (全资产含在途) - 基线。与 report.py --pnl 同口径。
    永不折算成单个 $(我们持币盈余, 折算会随 PRL 价波动)。失败返回 (None, None)。"""
    try:
        from config import load_config
        from safetrade import SafeTradeClient
        from dex import DexClient
        import arb
        cfg = load_config()
        st = SafeTradeClient(cfg.safetrade_api_key, cfg.safetrade_api_secret, cfg.safetrade_base_url)
        dex = DexClient(cfg)
        bl = json.load(open(ROOT / "baseline.json"))
        start_tok, start_usd = float(bl["start_token"]), float(bl["start_usdt"])
        cp, cu = float(st.balance("prl")), float(st.balance("usdt"))
        ew, eu = float(dex.balance(cfg.wprl_address)), float(dex.balance(cfg.quote_token_address))
        tc, te = arb._inflight(cfg, st, dex)
        u2evm = sum(float(w.get("amount") or 0) for w in st.withdraws(currency="usdt", limit=10)
                    if w.get("state", w.get("status")) not in ("failed", "rejected", "canceled", "errored")
                    and not w.get("completed_at"))
        u2cex = sum(float(d.get("amount") or 0) for d in st.deposits(currency="usdt", limit=10)
                    if not d.get("credited"))
        tot_tok = cp + ew + float(tc) + float(te)
        d_tok = tot_tok - start_tok
        d_usd = (cu + eu + u2evm + u2cex) - start_usd
        try:
            px = float(dex.wprl_price_in_quote())   # DEX 池 spot, WPRL/USDT
        except Exception:
            px = None
        return d_tok, d_usd, tot_tok, start_tok, px
    except Exception:
        return None, None, None, None, None


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(n, d_tok, d_usd, dev_since=None):
    try:
        prev = load_state()
        st = {"n": n, "ts": int(time.time())}
        if d_tok is not None:
            st["d_tok"], st["d_usd"] = float(d_tok), float(d_usd)
        elif "d_tok" in prev:                       # 读数存疑时保留上次好值, 6h增量基准不被假读污染
            st["d_tok"], st["d_usd"] = prev["d_tok"], prev["d_usd"]
        if dev_since is not None:                   # 偏离持续中: 记住首见时刻; 恢复正常则不写=清除
            st["dev_since"] = int(dev_since)
        json.dump(st, open(STATE, "w"))
    except Exception:
        pass


def pm2_status():
    """{name: status} for the 3 prl daemons. 空 dict = pm2 读取失败。"""
    out = {}
    try:
        import shutil
        pm2 = shutil.which("pm2") or "/usr/local/bin/pm2"   # cron PATH is minimal
        j = json.loads(subprocess.run([pm2, "jlist"], capture_output=True, text=True, timeout=20).stdout)
        for a in j:
            if a.get("name", "").startswith("prl-"):
                out[a["name"]] = a.get("pm2_env", {}).get("status", "?")
    except Exception:
        pass
    return out


def _fresh(line, max_age=ALERT_MAX_AGE_SEC):
    """日志行 [HH:MM:SS] 距今是否在 max_age 内。无时间戳 -> 视为新(保留)。日志与 strftime 同为服务器本地时区。"""
    m = re.search(r"\[(\d{2}):(\d{2}):(\d{2})\]", line)
    if not m:
        return True
    now = datetime.datetime.now()
    h, mi, s = map(int, m.groups())
    t = now.replace(hour=h, minute=mi, second=s, microsecond=0)
    if t > now:                       # 时间戳晚于现在 -> 归到昨天(跨午夜)
        t -= datetime.timedelta(days=1)
    return (now - t).total_seconds() <= max_age


def tail_crit(path, lines=120):
    """关键异常模式在最近 N 行里的命中(去掉无害的 snapshot error / insufficient_balance,
    并丢弃超过 6h 的陈旧告警 —— 只报本窗口内的事)。"""
    hits = []
    try:
        tail = open(path).read().splitlines()[-lines:]
        for l in tail:
            low = l.lower()
            if "snapshot error" in low or "insufficient_balance" in low or "time budget" in low:
                continue
            if not _fresh(l):         # 超 6h 的告警不上报
                continue
            for c in CRIT:
                if c.lower() in low:
                    hits.append(l.strip()[:100])
                    break
    except Exception:
        pass
    return hits


def eth_gas():
    """(eth_balance, gas_usd_per_swap) 或 (None, None)。"""
    try:
        from config import load_config
        from dex import DexClient
        import arb
        c = load_config()
        dx = DexClient(c)
        eth = dx.w3.eth.get_balance(c.wallet_address) / 1e18
        gas = float(arb.gas_cost_usd(dx))
        return eth, gas
    except Exception:
        return None, None


def bridge_checks():
    """双向桥在途状态 — 走 PearlBridge 公开 API 按 txid 精确查 relay 状态机。
    返回告警行 list; API/CEX 不可达时返回 [](不报假异常, 守恒检查仍兜底)。"""
    try:
        import pearlbridge_api
        from config import load_config
        from safetrade import SafeTradeClient
        c = load_config()
        st = SafeTradeClient(c.safetrade_api_key, c.safetrade_api_secret, c.safetrade_base_url)
        out = pearlbridge_api.reverse_alerts(st) or []
        out += pearlbridge_api.forward_alerts()
        return out
    except Exception:
        return []


def bridge_cost(window_sec):
    """reverse 桥真实损耗(进-出, realized 口径) 累计 + 窗口。失败返回 None。"""
    try:
        import bridge_fees
        from config import load_config
        from safetrade import SafeTradeClient
        c = load_config()
        stc = SafeTradeClient(c.safetrade_api_key, c.safetrade_api_secret, c.safetrade_base_url)
        return bridge_fees.summarize(stc, window_sec, limit=30)
    except Exception:
        return None


def build_report(persist=True):
    n, _, win_pnl = trades_summary()
    bc = bridge_cost(6 * 3600)                   # reverse 桥 realized 损耗(PRL)
    d_tok, d_usd, tot_tok, start_tok, px = pnl_coin_u()
    st = load_state()
    dn = n - st.get("n", 0)

    anomalies = []
    # 桥在途精确检查(PearlBridge API): 卡桥直接报 state + /order/<txid> 入口。
    bridge_alerts = bridge_checks()
    anomalies.extend(bridge_alerts)
    # 守恒检查: 总币(含在途)偏离基线 >25% -> 币读数不可信。API 检查已给出具体哪笔卡,
    # 这条降为读数可信度标记; 若 API 没报任何桥问题, 保留老的人工核实提示兜底。
    pnl_suspect = False
    dev_since = None
    if tot_tok is not None and start_tok:
        dev = (tot_tok - start_tok) / start_tok * 100
        if abs(dev) > 25:
            pnl_suspect = True
            # 持续时长: 首见时刻存 state, 偏离恢复即清零。用户 2026-07-24: 必须每次附时间,
            # 几小时的他不管, 两三天的他要介入。
            dev_since = st.get("dev_since") or int(time.time())
            age = int(time.time()) - dev_since
            dur = f"{age // 86400}天{age % 86400 // 3600}小时" if age >= 86400 else f"{age // 3600}小时"
            since_s = time.strftime("%m-%d %H:%M", time.localtime(dev_since))
            dur_tag = f"自{since_s}起已持续{dur}"
            if not bridge_alerts:
                anomalies.append(f"总币{tot_tok:.0f}偏离基线{dev:+.0f}%,{dur_tag}(疑交易所→钱包卡住未claim/在途未计入,去 pearlbridge 手动claim核实)")
            else:
                anomalies.append(f"总币{tot_tok:.0f}偏离基线{dev:+.0f}%,{dur_tag}(对应上面卡桥,币读数暂不可信)")
    # 进程
    ps = pm2_status()
    if not ps:
        anomalies.append("pm2 读取失败")
    else:
        for name in ("prl-auto-run", "prl-data-hub", "prl-supply"):
            s = ps.get(name)
            if s != "online":
                anomalies.append(f"{name}={s or '缺失'}")
    # .STOP / CUM-LOSS park
    if STOP.exists():
        anomalies.append(".STOP 存在(CUM-LOSS? 需 report.py 核实腿式盈亏)")
    # 关键日志模式
    for h in tail_crit(AUTORUN_LOG) + tail_crit(SUPPLY_LOG):
        anomalies.append(h)
    # gas
    eth, gas = eth_gas()
    if eth is not None and eth < 0.01:
        anomalies.append(f"ETH 低 {eth:.4f}(<0.01,需补)")

    # 桥费率复检告警(realized 偏离 0.5% -> 费率可能变了/部分铸/退款)
    if bc and bc.get("alerts"):
        anomalies.extend(bc["alerts"])

    head = time.strftime("%m-%d %H:%M")
    lines = [f"📊 PRL套利巡检 {head}",
             f"波数: 总 {n} 笔 (近6h +{dn} / ${win_pnl:+.1f})"]
    # 盈亏(头条) = 全资产(含在途) vs 基线, 两个无价数字: U 侧 + 币 侧 (同 report.py --pnl)。
    # 真实到手口径(基线比多/少了多少 U、多少币), 不被腿式残差×spot credit 高估。
    # 被卡桥/在途污染时标"读数存疑"(异常区有具体哪笔), 不静默给假数。
    if d_tok is not None:
        mark = " (读数存疑,见异常)" if pnl_suspect else ""
        u_side = f"U {d_usd:+.0f}" if d_usd is not None else "U n/a"
        # 近6h 到手增量 = 本次 d_usd - 上次推送存档的 d_usd (真实到手口径,
        # 与头条一致; 手动补库/追加资本落在窗口内时该数会被动作本身带偏, 属实情非 bug)
        prev_du = st.get("d_usd")
        if d_usd is not None and prev_du is not None and not pnl_suspect:
            u_side += f" (近6h {d_usd - prev_du:+.0f})"
        lines.append(f"盈亏(vs基线): {u_side} | 币 {d_tok:+.0f} PRL{mark}")
    else:
        lines.append("盈亏(vs基线): 读数不可用(见异常)")
    if px is not None:
        lines.append(f"币价: ${px:.4f}")
    # PRL 桥损耗诊断 = 交易所→钱包 桥 realized(进-出); trading 侧 token 中性≈0
    if bc is not None:
        lines.append(f"桥损耗(实测进-出): 累计 -{bc['cum_fee']:.0f} PRL | 近6h -{bc['win_fee']:.1f}({bc['win_n']}笔)")
    if ps:
        lines.append("进程: " + " ".join(f"{k.replace('prl-','')}={v}" for k, v in ps.items()))
    if eth is not None:
        lines.append(f"ETH: {eth:.4f} | gas ${gas:.2f}/笔")
    lines.append("异常: " + ("✅ 无" if not anomalies else "⚠️ " + "; ".join(anomalies[:6])))

    # 读数存疑时不更新币基准(传 None), 避免假读污染下个窗口的增量
    # persist=False (如 --dry) 不落盘, 避免手动跑污染 cron 的 6h 基准。
    if persist:
        save_state(n, None if pnl_suspect else d_tok, None if pnl_suspect else d_usd, dev_since)
    return "\n".join(lines)


def push(text):
    import requests
    r = requests.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                     params={"corpid": CORP_ID, "corpsecret": OPS_SECRET}, timeout=15)
    tok = r.json().get("access_token")
    if not tok:
        raise RuntimeError(f"gettoken failed: {r.text[:200]}")
    r = requests.post(f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={tok}",
                      json={"touser": ADMIN, "msgtype": "text", "agentid": int(OPS_AGENT),
                            "text": {"content": text}}, timeout=15)
    return r.json()


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    report = build_report(persist=not dry)
    print(report)
    if not dry:
        try:
            print("push ->", push(report))
        except Exception as e:
            print(f"push ERR: {e}")
