"""pearlbridge_api — PearlBridge 公开只读 REST API (https://api.pearlbridge.xyz/v1) 的
轻量客户端 + 告警检查。无鉴权、CORS 全开、服务端缓存 ~30s。

用途：在途桥超时不再靠"时效窗口 + 链上 mint 匹配"瞎猜，直接查 relay 的状态机。
一次事故的根因补丁：一笔反向桥 relayer 十几小时未 mint，在途计提 6h 过期后
告警消失，只剩"总币偏离基线 -32%"这种间接信号。现在每笔桥按 txid 精确可查：

  反向 PRL->WPRL:  GET /v1/mints/{pearlTxid}    pearlTxid = SafeTrade 提现的 `txid`
  正向 WPRL->PRL:  GET /v1/burns/{ethTxHash}    ethTxHash = 我们 requestBurn 的交易 hash
  人工入口:        https://pearlbridge.xyz/order/<pearlTxid>  (老的 r_<uuid> 链接已废弃)

mint 状态机 (relay pipeline): pending → attested/signing → submitted → minted。
旁路状态: queued(慢车道 24h 时间锁) / submitted_stuck(已广播未打包, relay 声称会自动重发
——6/10 实测 13h 没重发, 别信) / under_review / cancelled / rejected / failed / refunded。
"""
from __future__ import annotations
import json
import time
import urllib.request
from pathlib import Path

BASE = "https://api.pearlbridge.xyz"
ORDER_URL = "https://pearlbridge.xyz/order/{txid}"

# 反向桥正常 ~12-19min (fast lane)。超过这个分钟数且 state 还不是 minted 就告警。
REVERSE_WARN_MIN = 30
# 正向桥(烧 WPRL -> CEX 收 PRL)瓶颈是 CEX 入账, 全程 ~50min; 桥侧 unlock 本身应 <30min。
FORWARD_WARN_MIN = 75

# state -> (是否终态, 中文一句话)。终态不再重复查询。
MINT_STATES = {
    "minted":          (True,  "已mint到账"),
    "refunded":        (True,  "已退款回Pearl"),
    "pending":         (False, "等Pearl确认数"),
    "signing":         (False, "relay签名中"),
    "attesting":       (False, "relay签名中"),
    "attested":        (False, "已签名待广播"),
    "submitted":       (False, "已广播等打包"),
    "submitted_stuck": (False, "已广播未打包(gas/mempool, relay称会重发,实测会卡)"),
    "queued":          (False, "慢车道24h时间锁排队"),
    "under_review":    (False, "被标记人工审核"),
    "cancelled":       (False, "relay已取消,等退款"),
    "rejected":        (False, "存款被拒,等退款"),
    "failed":          (False, "mint失败,需联系operator"),
}
# 这些状态一出现就该告警, 不用等超时。
MINT_BAD = {"under_review", "cancelled", "rejected", "failed", "submitted_stuck"}

# 正向桥(burn) 状态。OK = 桥侧已放款(成功终态, 实测 API 返 finalized/unlocked 都算成功);
# BAD = 卡死/失败, 必须保持告警且不许在账本里标 closed(曾有三笔 failed 被误关, 藏掉了几千 WPRL);
# refunded = WPRL 已退回 EVM(资金已回, 可关账, 但 PRL 没到 CEX, 不能当"到账")。
BURN_OK = {"unlocked", "finalized"}
BURN_BAD = {"failed", "cancelled", "rejected", "under_review", "submitted_stuck"}

# 进程内缓存: txid -> (fetched_at, payload)。终态永久缓存, 非终态 TTL 内复用。
_cache: dict[str, tuple[float, dict | None]] = {}
_TTL = 240  # 4min — 在途变化是分钟级, 且服务端本身缓 30s


def _get(path: str, timeout: int = 10):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "prl-mm/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _cached(key: str, path: str, is_terminal):
    now = time.time()
    hit = _cache.get(key)
    if hit is not None:
        ts, val = hit
        if val is not None and is_terminal(val):
            return val
        if now - ts < _TTL:
            return val
    try:
        val = _get(path)
    except Exception:
        # API 不可达: 保留旧值(若有), 否则 None — 调用方降级到老的启发式。
        if hit is not None:
            return hit[1]
        _cache[key] = (now, None)
        return None
    _cache[key] = (now, val)
    return val


def mint_status(pearl_txid: str) -> dict | None:
    """反向桥状态 by Pearl txid。404(relay 还没索引到, 广播后 ~1-2min 内正常) -> None。"""
    return _cached("m:" + pearl_txid, f"/v1/mints/{pearl_txid}",
                   lambda v: MINT_STATES.get(v.get("state"), (False, ""))[0])


def burn_status(eth_tx_hash: str) -> dict | None:
    """正向桥状态 by 我们 requestBurn 的 eth tx hash。未知 hash 返回 state:null(可轮询)。
    hash 归一化为 0x 前缀 — 本环境 hexbytes .hex() 不带前缀, API 要求带。"""
    h = eth_tx_hash if eth_tx_hash.startswith("0x") else "0x" + eth_tx_hash
    return _cached("b:" + h, f"/v1/burns/{h}",
                   lambda v: v.get("state") in ("unlocked", "finalized", "refunded"))


def reverse_alerts(st, now: float | None = None) -> list[str] | None:
    """扫最近 24h 的 PRL 提现(= 反向桥), 逐笔查 /v1/mints/{txid}。
    返回告警行 list(空 = 全部正常); None = API/CEX 不可达, 调用方降级老启发式。
    st: SafeTradeClient。"""
    from datetime import datetime
    now = now or time.time()

    def ep(s):
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

    try:
        wds = [w for w in st.withdraws(currency="prl", limit=15)
               if w.get("status") not in ("failed", "rejected", "canceled", "errored")
               and w.get("created_at") and now - ep(w["created_at"]) < 24 * 3600]
    except Exception:
        return None

    alerts, api_ok = [], False
    for w in wds:
        txid, age = w.get("txid"), (now - ep(w["created_at"])) / 60.0
        if not txid:
            # CEX 还没广播链上 tx; 超过 30min 还没 txid 是 CEX 侧问题
            if age > REVERSE_WARN_MIN:
                alerts.append(f"交易所→钱包{float(w.get('amount') or 0):.0f}PRL: 交易所提现{age:.0f}min仍无txid(卡在交易所侧)")
            continue
        m = mint_status(txid)
        # 卡 pending/未索引 的自动唤醒 + 结果推送(一笔一次, 见 _nudge_check docstring)
        alerts.extend(_nudge_check(txid, m.get("state") if m else None,
                                   age, now))
        if m is None:
            continue   # 单笔查询失败/未索引, 由 api_ok 判断整体降级
        api_ok = True
        state = m.get("state")
        terminal, zh = MINT_STATES.get(state, (False, f"未知状态{state}"))
        if terminal:
            continue
        if state in MINT_BAD or age > REVERSE_NOTIFY_MIN:
            sev = "🚨" if (state in MINT_BAD or age > 90) else "⚠"
            alerts.append(f"{sev}交易所→钱包{float(w.get('amount') or 0):.0f}PRL卡{age:.0f}min "
                          f"state={state}({zh}) -> {ORDER_URL.format(txid=txid)}")
    if not api_ok and wds and not alerts:
        return None   # 有在途但一笔都没查成 — 降级
    return alerts


# ---------------------------------------------------------------------------
# 卡桥自动唤醒 (2026-07-03, 用户批准)。逆向 pearlbridge.xyz 前端确认: 用户手工"释放"
# 卡 pending 反向桥的全部有效动作 = 让后端收到 GET /v1/pearl-tx/{txid}(索引器路径,
# 收到未索引 txid 会去 Pearl 链上找这笔充值)。EIP-712 签名和输入金额都只在浏览器本地,
# 不参与铸造。我们监控一直轮的 /v1/mints 是只读 relay 状态, 唤不醒 — 所以单独打这两个
# GET。机制推断自用户历史观察(粘贴 txid 后几十秒 relayer 就动)+端点排除法, 首次实战验证
# 时生效/无效都推企业微信。每笔 txid 只试一次(一笔一档, 落盘 logs/bridge_nudges.jsonl),
# 无效则回到人工流程。
NUDGE_LOG = Path(__file__).parent / "logs" / "bridge_nudges.jsonl"
NUDGE_OUTCOME_WAIT_MIN = 720  # nudge 后仍 pending 超过这个分钟数(12h)才推"需要看一下"。
# 2026-09-16 owner: 唤醒静默做, 中途 minted 不推; 之前 30min 判无效的 21 笔事后全部自己到账, 是误报。
REVERSE_NOTIFY_MIN = 720      # 仅 pending(非坏状态)的告警行同样 12h 起报; 坏状态(MINT_BAD)仍即时


def _nudge_history() -> dict:
    """txid -> {nudge_ts, closed}。文件很小(每笔卡桥 2 行), 整读。"""
    hist: dict[str, dict] = {}
    try:
        for l in NUDGE_LOG.read_text().splitlines():
            try:
                e = json.loads(l)
            except Exception:
                continue
            t = e.get("txid")
            if not t:
                continue
            h = hist.setdefault(t, {"nudge_ts": 0.0, "closed": False})
            if e.get("ev") == "nudge":
                h["nudge_ts"] = float(e.get("ts") or 0)
            elif e.get("ev") == "outcome":
                h["closed"] = True
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return hist


def _nudge_append(rec: dict):
    try:
        NUDGE_LOG.parent.mkdir(exist_ok=True)
        with open(NUDGE_LOG, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def _wecom_push(text: str) -> bool:
    """ops 频道推送(凭据同 wechat_report, prl/.env)。尽力而为, 任何失败静默 —
    推不出去时告警行仍会进 data_hub stdout 和 6h 报告。"""
    try:
        import os
        try:
            from dotenv import load_dotenv
            load_dotenv(Path(__file__).parent / ".env")
        except Exception:
            pass
        corp = os.getenv("WECHAT_CORP_ID", "")
        sec = os.getenv("WECHAT_OPS_SECRET", "")
        agent = os.getenv("WECHAT_OPS_AGENT_ID", "")
        admin = os.getenv("WECHAT_ADMIN_USER_ID", "")
        if not (corp and sec and agent and admin):
            return False
        req = urllib.request.Request(
            f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corp}&corpsecret={sec}")
        with urllib.request.urlopen(req, timeout=15) as r:
            tok = json.loads(r.read().decode()).get("access_token")
        if not tok:
            return False
        body = json.dumps({"touser": admin, "msgtype": "text", "agentid": int(agent),
                           "text": {"content": text}}).encode()
        req = urllib.request.Request(
            f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={tok}",
            data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode()).get("errcode") == 0
    except Exception:
        return False


def _nudge_check(txid: str, state: str | None, age_min: float, now: float) -> list[str]:
    """反向桥卡 pending/未索引 的一次性唤醒 + 结果判定。返回要并入告警的行。
    state=None 表示 /v1/mints 查不到(未索引或 API 失败)。
    唤醒本身和"唤醒后自己到账"都只落盘不推; 只有唤醒后 NUDGE_OUTCOME_WAIT_MIN 仍 pending 才推一条。"""
    lines: list[str] = []
    h = _nudge_history().get(txid)
    still_stuck = state in (None, "pending")
    if h is not None:
        if h["closed"]:
            return lines
        waited = (now - h["nudge_ts"]) / 60.0
        if not still_stuck:
            _nudge_append({"ts": now, "txid": txid, "ev": "outcome", "ok": True, "state": state})
        elif waited > NUDGE_OUTCOME_WAIT_MIN:
            _nudge_append({"ts": now, "txid": txid, "ev": "outcome", "ok": False, "state": state})
            msg = (f"反向桥 {txid[:12]}… 唤醒后{waited/60:.1f}h仍{state or '未被索引'}, "
                   f"需要看一下 -> {ORDER_URL.format(txid=txid)}")
            lines.append(msg)
            _wecom_push(msg)
        return lines
    if still_stuck and age_min > REVERSE_WARN_MIN:
        try:
            pt = _get(f"/v1/pearl-tx/{txid}")
        except Exception:
            pt = None
        try:
            from config import load_config
            dr = _get(f"/v1/deposits/recent?ethAddress={load_config().wallet_address}")
        except Exception:
            dr = None
        _nudge_append({"ts": now, "txid": txid, "ev": "nudge", "state": state,
                       "age_min": round(age_min), "pearl_tx": pt, "deposits_recent": dr})
    return lines


PENDING_LEDGER = Path(__file__).parent / "logs" / "pending_supply.jsonl"


def forward_alerts(ledger_entries=None, now: float | None = None) -> list[str]:
    """检查 open 的 wprl_to_prl 账目(supply 烧币后写入, 新条目带 tx_hash)。
    桥侧 unlock 正常 <30min, 全程含 CEX 入账 ~50min; 超 FORWARD_WARN_MIN 才查 API。
    老条目没有 tx_hash 字段 -> 跳过(只能等 3h 时效, 行为同旧版)。
    ledger_entries 不传时直接读 supply 的账本文件(只读, 不 import supply)。"""
    now = now or time.time()
    if ledger_entries is None:
        ledger_entries = []
        try:
            for l in PENDING_LEDGER.read_text().splitlines():
                if l.strip():
                    try:
                        ledger_entries.append(json.loads(l))
                    except Exception:
                        pass
        except Exception:
            return []
    alerts = []
    for e in ledger_entries:
        if not e.get("open") or e.get("kind") != "wprl_to_prl":
            continue
        age = (now - e["ts"]) / 60.0
        if age <= FORWARD_WARN_MIN:
            continue
        h = e.get("tx_hash")
        if not h:
            alerts.append(f"⚠钱包→交易所{e['amount']:.0f}WPRL已{age:.0f}min未到交易所(老条目无hash,只能链上查)")
            continue
        b = burn_status(h)
        state = (b or {}).get("state")
        if state in BURN_OK:
            # 桥已放款, 卡的是 交易所 入账 — 给 Pearl txid 方便去交易所侧对
            alerts.append(f"⚠钱包→交易所{e['amount']:.0f}WPRL已{age:.0f}min: 桥已放款(pearlTx={str((b or {}).get('pearlTxId'))[:16]}…) 卡在交易所入账")
        else:
            h0x = h if h.startswith("0x") else "0x" + h
            alerts.append(f"🚨钱包→交易所{e['amount']:.0f}WPRL卡{age:.0f}min state={state}"
                          f"(桥侧未放款) tx=https://etherscan.io/tx/{h0x}")
    return alerts
