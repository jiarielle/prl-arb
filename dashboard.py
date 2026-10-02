"""prl-arb 运行控制台 v2（read + control）。

在看板 v1 只读的基础上增加：
  1. 实时双所行情线程 —— SafeTrade 公共接口 + Uniswap V3 报价（公共 RPC 兜底），
     不依赖任何机器人进程，5s 刷新，内存滚动历史供图表使用。
  2. 进程管理 —— 以子进程方式拉起/停止/重启 data_hub / auto_run / supply（替代 pm2），
     日志落 logs/dashboard_<name>.log。看板退出不杀子进程（start_new_session）。
  3. 控制接口 —— 急停(touch .STOP)/解除、改 .env 参数（原子写入，保留注释）、
     飞书 webhook 设置与测试。POST 一律要求 X-Dash-Token（服务启动时生成注入页面）。
  4. 飞书推送线程 —— 停机、进程退出、新成交、心跳丢失、边际触发 等事件推到群机器人。

安全约定：只绑 127.0.0.1；不外发任何密钥；亏损熔断(CUM_LOSS)的 .STOP 拒绝从网页解除
（这是作者的原设计：必须人工核对 report.py 后手动 rm）；.env 只允许白名单键被修改。

启动：  .venv/bin/python dashboard.py
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
STATE = ROOT / "state.json"
SERIES = ROOT / "logs" / "timeseries.jsonl"
TRADES = ROOT / "logs" / "trades.jsonl"
STOP = ROOT / ".STOP"
ENVF = ROOT / ".env"
BASELINE = ROOT / "baseline.json"
HTML = ROOT / "dashboard.html"
DASHCFG = ROOT / "dashboard.json"          # 看板自身设置（飞书等），非机器人配置
LOGDIR = ROOT / "logs"
PY = str(ROOT / ".venv" / "bin" / "python")

PORT = int(os.environ.get("DASHBOARD_PORT", "8787"))
TOKEN = secrets.token_hex(16)              # 防 CSRF：POST 必须携带
LIVE_INTERVAL = 5                          # 实时行情刷新秒数
LIVE_HISTORY = 1440                        # 内存滚动点数（~2h @5s）

sys.path.insert(0, str(ROOT))

SERIES_KEEP = (
    "ts", "spot", "raw_cex_prem_bps", "raw_dex_prem_bps",
    "edge_bps", "edge_dir", "mkt_edge_bps", "mkt_dir",
    "cex_token_share_pct", "gas_gwei", "eth_balance",
    "tokens_total", "usdt_total",
)

# 允许从网页修改的 .env 键。
# kind: int/float 数字(带范围) | url | privkey | address | digits | text
# secret=True 的键：值永不下发到前端（只回"已配置"状态），响应里打码，留空=保持不变。
EDITABLE_KEYS = {
    "MIN_GAP_BPS":       {"kind": "int",   "lo": 10, "hi": 10000, "desc": "开枪最小净边际(bp)"},
    "MAX_TRADE_USD":     {"kind": "float", "lo": 1,  "hi": 100000, "desc": "基础单笔规模($, 自适应会向上加)"},
    "HARD_MAX_TRADE_USD": {"kind": "float", "lo": 0, "hi": 100000, "desc": "单笔绝对上限($, 0=不限)"},
    "MIN_TRADE_PNL_USD": {"kind": "float", "lo": 0,  "hi": 100000, "desc": "最小预期盈利($, 低于不打)"},
    "POLL_INTERVAL_SEC": {"kind": "int",   "lo": 2,  "hi": 3600, "desc": "轮询间隔(秒)"},
    "GAS_BUFFER_USD":    {"kind": "float", "lo": 0,  "hi": 1000, "desc": "gas 缓冲($)"},
    "SLIPPAGE_TOLERANCE_BPS": {"kind": "int", "lo": 1, "hi": 10000, "desc": "DEX 滑点容忍(bp)"},
    "HTTPS_PROXY":       {"kind": "url",   "lo": None, "hi": None, "desc": "出口代理(如 http://127.0.0.1:7890)"},
    # ---- 密钥与身份（写入后不展示） ----
    "SAFETRADE_API_KEY":   {"kind": "text", "secret": True, "desc": "SafeTrade API Key（先只开 trade 权限）"},
    "SAFETRADE_API_SECRET": {"kind": "text", "secret": True, "desc": "SafeTrade API Secret"},
    "EVM_PRIVATE_KEY":     {"kind": "privkey", "secret": True, "desc": "钱包私钥（hex，建议专用小钱包）"},
    "EVM_WALLET_ADDRESS":  {"kind": "address", "secret": False, "desc": "钱包地址(0x…，非机密)"},
    "EVM_RPC_URL":         {"kind": "url", "secret": True, "desc": "以太坊 RPC(Alchemy 等，可空=用公共节点)"},
    "SAFETRADE_PRL_BRIDGE_BENEFICIARY_ID": {"kind": "digits", "secret": False, "desc": "PRL 桥提币白名单 ID(补币用)"},
    "SAFETRADE_USDT_BENEFICIARY_ID":      {"kind": "digits", "secret": False, "desc": "USDT 提币白名单 ID(补币用)"},
    "SAFETRADE_PRL_DEPOSIT_ADDR":  {"kind": "text", "secret": False, "desc": "SafeTrade PRL 充值地址(prl1…)"},
    "SAFETRADE_USDT_DEPOSIT_ADDR": {"kind": "text", "secret": False, "desc": "SafeTrade USDT 充值地址(0x…)"},
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- .env 读写
def parse_env() -> dict:
    d = {}
    if ENVF.exists():
        for line in ENVF.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            d[k.strip()] = v.strip().strip('"').strip("'")
    return d


def _validate(kind: str, lo, hi, v: str):
    """返回 (规范化值, 错误信息)。"""
    v = v.strip()
    if kind in ("int", "float"):
        parser = int if kind == "int" else float
        try:
            n = parser(v)
        except (TypeError, ValueError):
            return None, "不是合法数字"
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            return None, f"超出范围 [{lo}, {hi}]"
        return str(n), None
    if kind == "url":
        if not re.match(r"^https?://", v):
            return None, "需为 http(s):// 地址"
        return v, None
    if kind == "privkey":
        h = v[2:] if v.startswith("0x") else v
        if len(h) != 64 or not re.fullmatch(r"[0-9a-fA-F]+", h):
            return None, "需为 64 位 hex（可带 0x 前缀）"
        return ("0x" + h).lower(), None
    if kind == "address":
        if not (v.startswith("0x") and len(v) == 42 and re.fullmatch(r"[0-9a-fA-F]+", v[2:])):
            return None, "需为 0x 开头的 42 位地址"
        return v.lower(), None
    if kind == "digits":
        if not v.isdigit():
            return None, "需为数字 ID"
        return v, None
    if kind == "text":
        return (v, None) if v else (None, "不能为空")
    return v, None


def env_write(changes: dict) -> dict:
    """原子地更新 .env 里白名单键。空值=跳过不改。返回 {ok, updated, refused}。
    密钥类键在 updated 里只出现键名与 ●●●，不带明文。"""
    updated, refused, secret_touched = [], [], False
    clean = {}
    for k, v in changes.items():
        d = EDITABLE_KEYS.get(k)
        if d is None:
            refused.append(f"{k} 不在可改白名单")
            continue
        if str(v).strip() == "":
            continue                       # 留空 = 保持不变（前端占位符亦如此）
        val, err = _validate(d["kind"], d.get("lo"), d.get("hi"), str(v))
        if err:
            refused.append(f"{k} {err}")
            continue
        clean[k] = val
        if d.get("secret"):
            secret_touched = True

    if not clean:
        return {"ok": False, "updated": updated, "refused": refused,
                "secret_touched": False}
    text = ENVF.read_text() if ENVF.exists() else ""
    lines = text.split("\n")
    for k, v in clean.items():
        pat = re.compile(rf"^{re.escape(k)}=.*$")
        hit = False
        for i, ln in enumerate(lines):
            if pat.match(ln.strip()):
                lines[i] = f"{k}={v}"
                hit = True
                break
        if not hit:
            lines.append(f"{k}={v}")
        updated.append(k if EDITABLE_KEYS[k].get("secret") else f"{k}={v}")
    tmp = ENVF.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines))
    tmp.chmod(0o600)
    tmp.replace(ENVF)
    return {"ok": not refused, "updated": updated, "refused": refused,
            "secret_touched": secret_touched}


def cfg_view() -> dict:
    env = parse_env()
    def f(key, default):
        try:
            return float(env.get(key) or default)
        except Exception:
            return default
    return {
        "min_gap_bps": f("MIN_GAP_BPS", 200),
        "max_trade_usd": f("MAX_TRADE_USD", 250),
        "hard_max_trade_usd": f("HARD_MAX_TRADE_USD", 0),
        "min_trade_pnl_usd": f("MIN_TRADE_PNL_USD", 1),
        "poll_interval_sec": f("POLL_INTERVAL_SEC", 10),
        "gas_buffer_usd": f("GAS_BUFFER_USD", 5),
        "slippage_bps": f("SLIPPAGE_TOLERANCE_BPS", 100),
        "share_target_pct": f("SHARE_TARGET_PCT", 55),
        "https_proxy": env.get("HTTPS_PROXY", ""),
        "editable": {
            k: ({"filled": bool(env.get(k, "").strip()) and "<" not in env.get(k, "")
                     and "your_" not in env.get(k, "").lower(),
                 "secret": True, "desc": d["desc"]}
                if d.get("secret")
                else {"value": env.get(k, ""), "desc": d["desc"]})
            for k, d in EDITABLE_KEYS.items()
        },
    }


# ---------------------------------------------------------------- 实时行情
LIVE = {
    "ts": None, "cex": {}, "dex": {}, "prem": {}, "bal": {},
    "errors": {}, "history": [], "spot": None,
}
LIVE_LOCK = threading.Lock()


def _valid_addr(a: str) -> bool:
    return bool(a) and a.startswith("0x") and len(a) == 42 and a[2:].isalnum()


def live_thread():
    """5s 一次直接抓 CEX ticker/depth + Uniswap 报价；余额尽力而为。"""
    # 代理：.env 里的 HTTPS_PROXY 对 libcurl(curl_cffi) 与 requests(web3) 都生效
    env = parse_env()
    proxy = env.get("HTTPS_PROXY") or os.environ.get("HTTPS_PROXY")
    if proxy:
        os.environ["HTTPS_PROXY"] = proxy
        os.environ["https_proxy"] = proxy

    from config import load_config
    from safetrade import SafeTradeClient
    from dex import DexClient
    from web3 import Web3

    cfg0 = load_config()
    # 钱包/私钥是占位符时消毒成 None/零地址，让 DexClient 能构造（报价不需要钱包）
    wallet_valid = _valid_addr(cfg0.wallet_address)
    cfg = replace(cfg0, wallet_address=cfg0.wallet_address if wallet_valid else "",
                  private_key=cfg0.private_key if env.get("EVM_PRIVATE_KEY") else "")
    st = SafeTradeClient(cfg0.safetrade_api_key, cfg0.safetrade_api_secret,
                         cfg0.safetrade_base_url)
    dex = DexClient(cfg)
    has_keys = bool(env.get("SAFETRADE_API_KEY")) and bool(env.get("SAFETRADE_API_SECRET"))

    from decimal import Decimal as D
    taker = D(cfg0.safetrade_taker_fee_bps) / D(10000)
    wprl_a, quote_a = cfg.wprl_address, cfg.quote_token_address

    while True:
        snap = {"ts": now_iso(), "cex": {}, "dex": {}, "prem": {}, "bal": {}, "errors": {}}
        # --- CEX 公共行情 ---
        try:
            t = st.ticker(cfg0.safetrade_market)
            d = st.depth(cfg0.safetrade_market, limit=5)
            bids, asks = d.get("bids") or [], d.get("asks") or []
            snap["cex"] = {
                "last": float(t.get("last")) if t.get("last") else None,
                "bid": float(bids[0][0]) if bids else None,
                "ask": float(asks[0][0]) if asks else None,
                "bid_qty": float(bids[0][1]) if bids else None,
                "ask_qty": float(asks[0][1]) if asks else None,
            }
        except Exception as e:
            snap["errors"]["cex"] = f"{type(e).__name__}: {e}"[:140]
        # --- DEX 报价（双向 $100 探针） ---
        spot = None
        c = snap["cex"]
        if c.get("bid") and c.get("ask"):
            spot = (c["bid"] + c["ask"]) / 2
        elif c.get("last"):
            spot = c["last"]
        snap["spot"] = spot
        if spot:
            qty = D(100) / D(str(spot))
            try:
                dex_cost = dex.quote_exact_out(quote_a, wprl_a, qty)      # 买 WPRL 花的 U
                dex_recv = dex.quote_exact_in(wprl_a, quote_a, qty)      # 卖 WPRL 收的 U
                snap["dex"] = {"buy_px": float(dex_cost / qty),
                               "sell_px": float(dex_recv / qty)}
                if c.get("bid"):
                    recv = qty * D(str(c["bid"])) * (D(1) - taker)
                    snap["prem"]["cex_prem_bps"] = float(
                        (recv - dex_cost) / (qty * D(str(c["bid"]))) * D(10000))
                if c.get("ask"):
                    cost = qty * D(str(c["ask"])) * (D(1) + taker)
                    snap["prem"]["dex_prem_bps"] = float(
                        (dex_recv - cost) / (qty * D(str(c["ask"]))) * D(10000))
            except Exception as e:
                snap["errors"]["dex"] = f"{type(e).__name__}: {e}"[:140]
        # --- 余额（尽力而为） ---
        if wallet_valid:
            try:
                snap["bal"]["evm"] = {
                    "wprl": float(dex.balance(wprl_a)),
                    "usdt": float(dex.balance(quote_a)),
                    "eth": float(dex.w3.from_wei(dex.w3.eth.get_balance(
                        Web3.to_checksum_address(cfg0.wallet_address)), "ether")),
                }
            except Exception as e:
                snap["errors"]["bal_evm"] = f"{type(e).__name__}: {e}"[:140]
        if has_keys:
            try:
                snap["bal"]["cex"] = {"prl": float(st.balance("prl")),
                                      "usdt": float(st.balance("usdt"))}
            except Exception as e:
                snap["errors"]["bal_cex"] = f"{type(e).__name__}: {e}"[:140]

        with LIVE_LOCK:
            LIVE.update(snap)
            h = LIVE["history"]
            h.append({"ts": snap["ts"], "spot": spot,
                      "raw_cex_prem_bps": snap["prem"].get("cex_prem_bps"),
                      "raw_dex_prem_bps": snap["prem"].get("dex_prem_bps")})
            del h[:-LIVE_HISTORY]
        time.sleep(LIVE_INTERVAL)


# ---------------------------------------------------------------- 进程管理
PROCS = {
    "data_hub": {"cmd": [PY, "-u", "data_hub.py"], "label": "行情"},
    "auto_run": {"cmd": [PY, "-u", "scripts/auto_run.py"], "label": "交易"},
    "supply":   {"cmd": [PY, "-u", "supply.py"], "label": "补币"},
}
CHILDREN: dict[str, subprocess.Popen] = {}
PROC_LOCK = threading.Lock()


def _pgrep(pattern: str):
    try:
        r = subprocess.run(["pgrep", "-fl", pattern], capture_output=True, text=True, timeout=5)
        return [int(l.split()[0]) for l in r.stdout.strip().split("\n") if l.strip()]
    except Exception:
        return []


def proc_status(name: str):
    with PROC_LOCK:
        child = CHILDREN.get(name)
        if child and child.poll() is None:
            return "running"
    return "running" if _pgrep(PROCS[name]["cmd"][-1]) else "stopped"


def proc_start(name: str):
    if name not in PROCS:
        return {"ok": False, "err": "unknown proc"}
    if proc_status(name) == "running":
        return {"ok": True, "note": "already running"}
    LOGDIR.mkdir(exist_ok=True)
    logf = open(LOGDIR / f"dashboard_{name}.log", "ab")
    with PROC_LOCK:
        CHILDREN[name] = subprocess.Popen(
            PROCS[name]["cmd"], cwd=str(ROOT), stdout=logf, stderr=logf,
            start_new_session=True)
    return {"ok": True, "pid": CHILDREN[name].pid}


def proc_stop(name: str):
    if name not in PROCS:
        return {"ok": False, "err": "unknown proc"}
    with PROC_LOCK:
        child = CHILDREN.pop(name, None)
    victims = []
    if child and child.poll() is None:
        victims.append(child.pid)
    else:
        victims = _pgrep(PROCS[name]["cmd"][-1])
    if not victims:
        return {"ok": True, "note": "not running"}
    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(20):                    # 等优雅退出最多 2s
        if all(_gone(p) for p in victims):
            return {"ok": True}
        time.sleep(0.1)
    for pid in victims:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return {"ok": True, "note": "SIGKILLed"}


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return False
    except OSError:
        return True


def proc_restart(name: str):
    r = proc_stop(name)
    time.sleep(1)
    r2 = proc_start(name)
    return {"ok": r2.get("ok", False), "stop": r, "start": r2}


# ---------------------------------------------------------------- 飞书推送
def dashcfg_load() -> dict:
    if DASHCFG.exists():
        try:
            return json.loads(DASHCFG.read_text())
        except Exception:
            pass
    return {}


def dashcfg_save(cfgd: dict):
    DASHCFG.write_text(json.dumps(cfgd, indent=1))


def feishu_send(text: str) -> dict:
    c = dashcfg_load()
    url = (c.get("feishu_url") or "").strip()
    if not url:
        return {"ok": False, "err": "未配置 webhook"}
    secret = (c.get("feishu_secret") or "").strip()
    body = {"msg_type": "text", "content": {"text": text}}
    if secret:
        ts = str(int(time.time()))
        string_to_sign = f"{ts}\n{secret}"
        sign = base64.b64encode(
            hmac.new(string_to_sign.encode(), digestmod=hashlib.sha256).digest()
        ).decode()
        body["timestamp"] = ts
        body["sign"] = sign
    try:
        import urllib.request
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        # 飞书自定义机器人: {"code":0,...} 或旧版 {"StatusCode":0}
        code = data.get("code", data.get("StatusCode"))
        return {"ok": code == 0, "resp": data}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"[:200]}


def watcher_thread():
    """状态跃迁 → 飞书推送。只推跃迁不推稳态，防刷屏。"""
    prev = {"stop": None, "procs": {}, "trades_n": None, "hb_bad": False,
            "edge_hot": 0, "edge_last_push": 0.0}
    while True:
        cfgd = dashcfg_load()
        ev = cfgd.get("events", {"halt": True, "procs": True, "trades": True,
                                 "heartbeat": True, "edge": False})
        msgs = []

        s = stop_state()
        if ev.get("halt"):
            if prev["stop"] is None:
                prev["stop"] = s
            elif s.get("exists") != prev["stop"].get("exists") or \
                    s.get("kind") != prev["stop"].get("kind"):
                if s.get("exists"):
                    kind = {"cum_loss": "亏损熔断", "bridge": "桥停机", "manual": "人工停机"}.get(
                        s["kind"], s["kind"])
                    msgs.append(f"⏸ prl-arb 已停机：{kind}\n{s.get('content','')[:200]}")
                else:
                    msgs.append("▶ prl-arb 停机已解除")
                prev["stop"] = s

        running = {n: proc_status(n) for n in PROCS}
        if ev.get("procs"):
            for n, st in running.items():
                if n in prev["procs"] and prev["procs"][n] == "running" and st == "stopped":
                    msgs.append(f"⚠ 进程退出：{PROCS[n]['label']} {n}")
            prev["procs"] = running

        if ev.get("trades") and TRADES.exists():
            n = count_lines(TRADES)
            if prev["trades_n"] is None:
                prev["trades_n"] = n
            elif n > prev["trades_n"]:
                for row in tail_jsonl(TRADES, n - prev["trades_n"])[-3:]:
                    msgs.append(_trade_msg(row))
                prev["trades_n"] = n

        if ev.get("heartbeat") and STATE.exists():
            try:
                age = time.time() - STATE.stat().st_mtime
            except OSError:
                age = 0
            bad = age > 180
            if bad and not prev["hb_bad"]:
                msgs.append("⚠ data_hub 心跳丢失 >3 分钟（state.json 不再更新）")
            prev["hb_bad"] = bad

        if ev.get("edge"):
            with LIVE_LOCK:
                p = LIVE["prem"]
            best = max([v for v in (p.get("cex_prem_bps"), p.get("dex_prem_bps"))
                        if v is not None], default=0)
            gap = cfg_view()["min_gap_bps"]
            prev["edge_hot"] = prev["edge_hot"] + 1 if best >= gap else 0
            if prev["edge_hot"] >= 3 and time.time() - prev["edge_last_push"] > 600:
                d = "卖CEX买DEX" if p.get("cex_prem_bps", 0) >= p.get("dex_prem_bps", 0) \
                    else "卖DEX买CEX"
                msgs.append(f"💰 边际触发：{best:.0f}bp ≥ {gap:.0f}bp（{d}）")
                prev["edge_last_push"] = time.time()
                prev["edge_hot"] = 0

        for m in msgs:
            feishu_send(m)
        time.sleep(5)


def _trade_msg(r: dict) -> str:
    t = (r.get("ts") or "")[11:19]
    d = r.get("dir") or r.get("kind") or ""
    q = r.get("qty") if r.get("qty") is not None else r.get("exposure_qty")
    o = r.get("outcome") or ("完成" if r.get("ok") else ("敞口" if r.get("ok") is False else ""))
    note = (r.get("note") or "")[:80]
    return f"📋 成交 {t}UTC {d} {q if q is not None else ''} {o} {note}".strip()


# ---------------------------------------------------------------- 只读数据
def tail_jsonl(path: Path, n: int):
    if not path.exists():
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n * 1500))
            data = f.read().decode("utf-8", "replace")
        rows = []
        for line in data.split("\n"):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
        return rows[-n:]
    except Exception:
        return []


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def stop_state() -> dict:
    if not STOP.exists():
        return {"exists": False}
    try:
        content = STOP.read_text().strip()
    except Exception:
        content = ""
    if "CUM_LOSS" in content.upper():
        kind = "cum_loss"
    elif content == "bridge":
        kind = "bridge"
    else:
        kind = "manual"
    return {"exists": True, "kind": kind, "content": content[:300]}


def api_overview() -> dict:
    st, age = None, None
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text())
            age = round(time.time() - STATE.stat().st_mtime, 1)
        except Exception:
            pass
    env = parse_env()

    def filled(key):
        v = env.get(key, "")
        return bool(v) and "<" not in v and "your_" not in v.lower()

    wallet = env.get("EVM_WALLET_ADDRESS", "")
    redacted = wallet[:6] + "…" + wallet[-4:] if _valid_addr(wallet) else ""
    bl = {"set": False}
    if BASELINE.exists():
        try:
            b = json.loads(BASELINE.read_text())
            bl = {"set": (float(b.get("start_token") or 0) > 0
                          or float(b.get("start_usdt") or 0) > 0),
                  "start_token": b.get("start_token"), "start_usdt": b.get("start_usdt")}
        except Exception:
            pass
    return {
        "now": time.time(),
        "state": st, "state_age": age,
        "stop": stop_state(),
        "procs": {n: proc_status(n) for n in PROCS},
        "env": {
            "api_key": filled("SAFETRADE_API_KEY"),
            "api_secret": filled("SAFETRADE_API_SECRET"),
            "private_key": filled("EVM_PRIVATE_KEY"),
            "rpc": filled("EVM_RPC_URL"),
            "wallet_redacted": redacted,
            "wallet_valid": _valid_addr(wallet),
            "routing_ids": filled("SAFETRADE_PRL_BRIDGE_BENEFICIARY_ID")
            and filled("SAFETRADE_USDT_BENEFICIARY_ID"),
            "deposit_addrs": filled("SAFETRADE_PRL_DEPOSIT_ADDR")
            and filled("SAFETRADE_USDT_DEPOSIT_ADDR"),
            "dry_run": env.get("DRY_RUN", "true").lower() != "false",
        },
        "baseline": bl,
        "cfg": cfg_view(),
        "counts": {"trades": count_lines(TRADES), "series": count_lines(SERIES)},
    }


def api_series(n: int) -> dict:
    rows = tail_jsonl(SERIES, n)
    # bot 时序可能存在但字段全空（如未填 key 时的 401 局部快照）——不足两条可用点就退回实时线程
    usable = [r for r in rows if r.get("raw_cex_prem_bps") is not None
              or r.get("raw_dex_prem_bps") is not None]
    if len(usable) >= 2:
        return {"source": "bot", "rows": [{k: r.get(k) for k in SERIES_KEEP} for r in rows]}
    with LIVE_LOCK:
        h = LIVE["history"][-n:]
    return {"source": "live", "rows": list(h)}


def api_live() -> dict:
    with LIVE_LOCK:
        snap = {k: (dict(v) if isinstance(v, dict) else v) for k, v in LIVE.items()
                if k != "history"}
    snap["procs"] = {n: proc_status(n) for n in PROCS}
    return snap


def api_ctl(body: dict) -> dict:
    action = body.get("action")
    if action == "halt":
        STOP.write_text(f"manual via dashboard {now_iso()}\n")
        return {"ok": True, "msg": "已写入 .STOP，交易循环将在下个轮询停下"}
    if action == "unhalt":
        s = stop_state()
        if not s.get("exists"):
            return {"ok": True, "msg": "本就没有停机"}
        if s["kind"] == "cum_loss":
            return {"ok": False,
                    "msg": "亏损熔断不允许从网页解除：先 .venv/bin/python scripts/report.py --pnl 核对，再手动 rm .STOP"}
        if s["kind"] == "bridge":
            return {"ok": False,
                    "msg": "桥停机由 bridge_watchdog 在桥恢复后自动解除，不建议手动删"}
        STOP.unlink(missing_ok=True)
        return {"ok": True, "msg": "已删除 .STOP"}
    if action in ("start", "stop", "restart"):
        name = body.get("proc")
        if name not in PROCS:
            return {"ok": False, "msg": "未知进程"}
        fn = {"start": proc_start, "stop": proc_stop, "restart": proc_restart}[action]
        return {"ok": fn(name).get("ok", False),
                "msg": f"{PROCS[name]['label']}({name}) {action} 已执行"}
    if action == "save_config":
        r = env_write(body.get("changes") or {})
        # 需要重启的进程：密钥类改动影响所有三个进程；纯参数只影响 auto_run
        if r.get("secret_touched"):
            restarts = [n for n in PROCS if proc_status(n) == "running"]
        else:
            restarts = ["auto_run"] if proc_status("auto_run") == "running" else []
        if not r["updated"] and r["refused"]:
            msg = "未写入任何改动：" + "；".join(r["refused"])
        else:
            msg = "已写入 .env（" + "、".join(r["updated"]) + "）"
        return {"ok": r["ok"], "updated": r["updated"], "refused": r["refused"],
                "restarts": restarts, "msg": msg}
    if action == "set_feishu":
        c = dashcfg_load()
        c["feishu_url"] = (body.get("url") or "").strip()
        c["feishu_secret"] = (body.get("secret") or "").strip()
        if isinstance(body.get("events"), dict):
            c["events"] = body.get("events")
        dashcfg_save(c)
        return {"ok": True, "msg": "飞书设置已保存"}
    if action == "test_feishu":
        r = feishu_send("✅ prl-arb 看板测试消息 " + now_iso()[:19])
        return r if r["ok"] else {"ok": False, "msg": f"发送失败：{r.get('err')}"}
    return {"ok": False, "msg": f"未知 action: {action}"}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str).encode(),
                   "application/json; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                if HTML.exists():
                    page = HTML.read_bytes().replace(b"__DASH_TOKEN__", TOKEN.encode())
                    self._send(200, page, "text/html; charset=utf-8")
                else:
                    self._send(404, b"dashboard.html missing", "text/plain")
            elif u.path == "/api/overview":
                self._json(api_overview())
            elif u.path == "/api/live":
                self._json(api_live())
            elif u.path == "/api/series":
                n = min(max(int(q.get("n", ["600"])[0]), 10), 3000)
                self._json(api_series(n))
            elif u.path == "/api/feishu":
                c = dashcfg_load()
                self._json({"url": c.get("feishu_url", ""),
                            "secret_set": bool(c.get("feishu_secret")),
                            "events": c.get("events", {"halt": True, "procs": True,
                                                       "trades": True, "heartbeat": True,
                                                       "edge": False})})
            elif u.path == "/api/trades":
                n = min(max(int(q.get("n", ["60"])[0]), 5), 300)
                rows = tail_jsonl(TRADES, n)
                rows.reverse()
                self._json({"rows": rows, "total": count_lines(TRADES)})
            elif u.path == "/api/proclog":
                name = q.get("name", ["auto_run"])[0]
                if name not in PROCS:
                    self._json({"err": "unknown proc"})
                    return
                p = LOGDIR / f"dashboard_{name}.log"
                lines = []
                if p.exists():
                    try:
                        with open(p, "rb") as f:
                            f.seek(0, os.SEEK_END)
                            sz = f.tell()
                            f.seek(max(0, sz - 16000))
                            lines = f.read().decode("utf-8", "replace").split("\n")
                        lines = [l for l in lines if l.strip()][-60:]
                    except Exception:
                        pass
                self._json({"lines": lines})
            else:
                self._send(404, b"not found", "text/plain")
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json({"error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass

    def do_POST(self):
        if urlparse(self.path).path != "/api/ctl":
            self._json({"ok": False, "msg": "not found"}, 404)
            return
        if self.headers.get("X-Dash-Token") != TOKEN:
            self._json({"ok": False, "msg": "token 校验失败"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            self._json({"ok": False, "msg": "请求体不是合法 JSON"})
            return
        try:
            self._json(api_ctl(body))
        except Exception as e:
            self._json({"ok": False, "msg": f"{type(e).__name__}: {e}"})


def main():
    for t, name in ((live_thread, "live"), (watcher_thread, "watcher")):
        threading.Thread(target=t, name=name, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"prl-arb 控制台: http://127.0.0.1:{PORT}  (仅本机; 实时行情/推送/进程管理已启用)",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n控制台已停止（子进程不受影响）")


if __name__ == "__main__":
    main()
