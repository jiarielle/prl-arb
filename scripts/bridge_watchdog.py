#!/usr/bin/env python3
"""prl-bridge-watchdog — recover from a stuck forward bridge (WPRL->PRL) once it clears.

Scenario: forward bridges enter relay state `failed` (WPRL burned on-chain, PRL never
credited at the CEX). The token-conservation guard freezes supply, the operator halts
the trader with an empty `.STOP` (or a `.STOP` containing the word `bridge`), and may
lower the conservation reference (`bridge_ref_token` in baseline.json) so supply can
keep running at the reduced total while the bridge operator is chased.

This watchdog polls every 5 min and, after N_CONFIRM consecutive passes, restores the
normal state, pushes an alert and deletes its own pm2 entry. Two recoverable states,
alone or together:
  * bridge halt: a `.STOP` containing the word `bridge` -> rm .STOP -> pm2 start prl-auto-run prl-supply.
    A `.STOP` written by a CUM_LOSS halt is NEVER lifted here; that is a human decision.
  * reduced reference: baseline.json has bridge_ref_token < start_token -> remove it and
    restart prl-supply so the conservation band returns to the full inventory.

Recovery conditions (both required):
  1) every stuck forward bridge snapshotted at start has left the BURN_BAD states
     (unlocked/finalized/refunded, or the PRL arrived). An unknown/None relay state
     or an unreachable relay API counts as still bad.
  2) state.json is fresh (< STATE_MAX_AGE_SEC) and its tokens_total >= RECOVER_FRAC *
     baseline.start_token. No baseline.json -> the watchdog refuses to run.

Safety: every loop iteration is wrapped so the process never crashes out; the recovery
action runs once, then the process removes itself from pm2. Alerts reuse wechat_report.push.
"""
import os, sys, json, time, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

POLL_SEC = 300          # 5min
N_CONFIRM = 2           # 连续满足次数
RECOVER_FRAC = 0.90     # tokens_total 回到 baseline 的 90% 视为库存复原
STOP_FILE = ROOT / ".STOP"
STATE_FILE = ROOT / "state.json"
STATE_MAX_AGE_SEC = 10 * 60   # data_hub refreshes every ~15s; anything older is a dead hub, not a fresh reading
CLEARED = {"unlocked", "finalized", "refunded"}   # the only relay states that count as "this bridge is no longer stuck"
BASELINE_FILE = ROOT / "baseline.json"
LOG = ROOT / "logs" / "bridge_watchdog.log"
MARKER = ROOT / "logs" / ".bridge_recovered"
PM2_NAME = "prl-bridge-watchdog"
LOOKBACK_SEC = 30 * 24 * 3600   # 卡桥可能拖数天/周等 operator, 窗口放宽到 30d 才不丢失追踪的 stuck 笔
                                # (stuck_forward_hashes 只留当前仍 BURN_BAD 的, 老的已解除笔不会混入)

try:
    from pearlbridge_api import burn_status, BURN_BAD
except Exception:
    burn_status = None
    BURN_BAD = {"failed", "cancelled", "rejected", "under_review", "submitted_stuck"}


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def baseline_token():
    try:
        return float(json.load(open(BASELINE_FILE))["start_token"])
    except Exception:
        return None   # no baseline -> nothing to anchor recovery to; main() refuses to run


def tokens_total():
    """data_hub 每 15s 刷新 state.json; 读 tokens_total。读不到返回 None。"""
    try:
        st = json.load(open(STATE_FILE))
        ts = st.get("ts")
        if not ts:
            log("state.json has no timestamp -> not trusting tokens_total")
            return None
        from datetime import datetime, timezone
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()
        if age < 0 or age > STATE_MAX_AGE_SEC:
            log(f"state.json timestamp is {age/60:.0f} min from now (allowed 0..{STATE_MAX_AGE_SEC//60}) -> not trusting tokens_total")
            return None
        return float(st.get("tokens_total"))
    except Exception:
        return None


def stuck_forward_hashes():
    """扫 pending_supply.jsonl 最近 72h 的 wprl_to_prl(带 tx_hash), 返回当前仍在 BURN_BAD 的 hash 集合。"""
    p = ROOT / "logs" / "pending_supply.jsonl"
    now = time.time()
    out = {}
    try:
        for l in p.read_text().splitlines():
            if not l.strip():
                continue
            try:
                e = json.loads(l)
            except Exception:
                continue
            if e.get("kind") != "wprl_to_prl" or not e.get("tx_hash"):
                continue
            if now - e.get("ts", 0) > LOOKBACK_SEC:
                continue
            h = e["tx_hash"]
            if burn_status is None:
                continue
            try:
                st = (burn_status(h) or {}).get("state") or "__unknown__"
            except Exception:
                st = "__api_down__"
            # Anything not positively cleared is watched: BAD states, unknown, API down.
            if st not in CLEARED:
                out[h] = (e.get("amount"), st)
    except Exception as ex:
        log(f"stuck_forward_hashes ERR: {ex}")
    return out


def still_bad(hashes):
    """给定 hash 集合, 返回其中现在仍 BURN_BAD 的。空 = 全部已解除。"""
    bad = {}
    for h in hashes:
        try:
            st = (burn_status(h) or {}).get("state") or "__unknown__"   # None = relay does not know it: NOT cleared
        except Exception:
            st = "__api_down__"   # API 挂了当作仍未解除, 不误判恢复
        if st not in CLEARED:   # only a positive terminal state clears a bridge
            bad[h] = st
    return bad


def bridge_halt():
    """True only if the stop file is the marker this watchdog owns: it contains the word
    `bridge` (write it with `echo bridge > .STOP`). An empty file, a CUM_LOSS halt written
    by the trader, or any other content is somebody else's decision and is never lifted."""
    if not STOP_FILE.exists():
        return False
    try:
        txt = STOP_FILE.read_text().strip().lower()
    except Exception:
        return False
    if "cum_loss" in txt:
        return False
    return "bridge" in txt


def push_wechat(text):
    try:
        from wechat_report import push
        log(f"wechat push -> {push(text)}")
    except Exception as ex:
        log(f"wechat push ERR: {ex}")


def reduced_ref():
    """baseline.json 是否处于"降总量"态: 有 bridge_ref_token 且明显 < start_token。
    (卡桥期间把守恒参考降到当前真实持币, 见 supply.py 的 BASELINE_TOKEN。)"""
    try:
        bl = json.load(open(BASELINE_FILE))
        ref = bl.get("bridge_ref_token")
        return ref is not None and float(ref) < float(bl["start_token"]) - 1
    except Exception:
        return False


def restore_bridge_ref():
    """卡桥到账后恢复满仓: 删掉 baseline.json 的 bridge_ref_token/bridge_ref_note ->
    supply 的守恒参考回落到 start_token。原子写。返回 (ok, old_ref)。"""
    try:
        bl = json.load(open(BASELINE_FILE))
        old = bl.pop("bridge_ref_token", None)
        bl.pop("bridge_ref_note", None)
        tmp = str(BASELINE_FILE) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(bl, f, ensure_ascii=False, indent=2)
        os.replace(tmp, BASELINE_FILE)
        return True, (float(old) if old is not None else None)
    except Exception as ex:
        log(f"restore_bridge_ref ERR: {ex}")
        return False, None


def do_recover(watch, base, tt, halted, reduced):
    """卡桥解除 + 总币回到 baseline 后的恢复动作。两种态可并存:
      reduced -> 恢复守恒参考到 start_token + 重启 prl-supply 让它重读 baseline.json;
      halted  -> rm .STOP + pm2 start prl-auto-run prl-supply。"""
    log(f"=== RECOVERY CONFIRMED -> halted={halted} reduced={reduced} ===")
    actions = []
    # 1) 降总量态: 恢复满仓守恒参考 (supply 在 import 时读 baseline, 必须重启才生效)
    if reduced:
        ok, old = restore_bridge_ref()
        if ok:
            log(f"restored bridge_ref_token (was {old}) -> 守恒参考回 start_token {base:.0f}")
            try:
                r = subprocess.run(["pm2", "restart", "prl-supply"],
                                   capture_output=True, text=True, timeout=60)
                log(f"pm2 restart prl-supply rc={r.returncode} {r.stderr.strip()[-200:]}")
                actions.append(f"守恒参考已恢复 start_token {base:.0f} (原降至 {old:.0f}), 已重启 prl-supply 重读")
            except Exception as ex:
                log(f"pm2 restart prl-supply ERR: {ex}")
                actions.append(f"守恒参考已恢复 (原 {old:.0f}), 但 prl-supply 重启失败 -> 需手动 pm2 restart prl-supply")
        else:
            actions.append("⚠ 守恒参考恢复失败, 需手动删 baseline.json 的 bridge_ref_token")
    # 2) 停机态: rm .STOP + 重启交易/库存
    if halted:
        try:
            if STOP_FILE.exists():
                STOP_FILE.unlink()
                log("rm .STOP ok")
        except Exception as ex:
            log(f"rm .STOP ERR: {ex}")
        try:
            r = subprocess.run(["pm2", "start", "prl-auto-run", "prl-supply"],
                               capture_output=True, text=True, timeout=60)
            log(f"pm2 start rc={r.returncode} {r.stdout.strip()[-300:]} {r.stderr.strip()[-200:]}")
            actions.append("已 rm .STOP + pm2 start prl-auto-run prl-supply")
        except Exception as ex:
            log(f"pm2 start ERR: {ex}")
    # 3) marker
    try:
        MARKER.write_text(json.dumps({
            "recovered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "baseline": base, "tokens_total": tt, "halted": halted, "reduced": reduced,
            "watched": {h: a for h, (a, s) in watch.items()},
        }, ensure_ascii=False, indent=1))
    except Exception:
        pass
    # 4) wechat 通知用户
    amts = " + ".join(f"{a:.0f}" for (a, s) in watch.values()) if watch else "(无)"
    push_wechat(
        "✅ PRL 卡桥已解除\n"
        f"解除的正向桥: {amts} WPRL\n"
        f"总币 {tt:.0f} 已回到 start_token {base:.0f} 的 {tt/base*100:.0f}%\n"
        + "\n".join("- " + a for a in actions) + "\n"
        f"时间 {time.strftime('%Y-%m-%d %H:%M:%S')} (UTC)")
    log("recover done.")


def self_delete():
    try:
        subprocess.run(["pm2", "delete", PM2_NAME], capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def main():
    base = baseline_token()   # = start_token; 恢复阈值锚到满仓, 不锚降后的 ref
    if not base:
        log("baseline.json missing/unreadable -> no recovery anchor, watchdog refuses to run")
        self_delete()
        return
    if STOP_FILE.exists() and not bridge_halt():
        log("STOP file is a CUM_LOSS (or unknown) halt, not a bridge halt -> this watchdog will NOT lift it")
    watch = stuck_forward_hashes()
    halted0, reduced0 = bridge_halt(), reduced_ref()
    log(f"watchdog start. start_token={base:.0f} recover_thresh={RECOVER_FRAC*base:.0f} "
        f"halted={halted0} reduced_ref={reduced0} "
        f"watching {len(watch)} stuck forward bridge(s): "
        + ", ".join(f"{a:.0f}WPRL({s})" for (a, s) in watch.values()))
    if not halted0 and not reduced0:
        log("既未停机也未降总量 -> 无事可守, 退出。")
        self_delete()
        return
    if not watch:
        log("启动时无 stuck 正向桥 (可能已解除); 仍会按 tokens_total 校验后恢复。")
    confirm = 0
    while True:
        try:
            halted, reduced = bridge_halt(), reduced_ref()
            # 停机已被(用户/本程序)解除 且 已无降总量态 -> 无事可守, 退出
            if not halted and not reduced:
                log("停机已解除且无降总量态 -> 看门狗退出, 不重复操作。")
                self_delete()
                return
            bad = still_bad(set(watch.keys())) if watch else {}
            tt = tokens_total()
            tt_ok = (tt is not None) and (tt >= RECOVER_FRAC * base)
            bridges_ok = (len(bad) == 0)
            if bridges_ok and tt_ok:
                confirm += 1
                log(f"recovery check PASS ({confirm}/{N_CONFIRM}): "
                    f"stuck桥已清, tokens_total={tt:.0f} (>= {RECOVER_FRAC*base:.0f})")
                if confirm >= N_CONFIRM:
                    do_recover(watch, base, tt, halted, reduced)
                    self_delete()
                    return
            else:
                if confirm:
                    log("recovery check 回退, confirm 计数清零")
                confirm = 0
                badtxt = ",".join(f"{h[:10]}…={s}" for h, s in bad.items()) if bad else "none"
                log(f"仍未恢复: stuck剩={len(bad)}({badtxt}) "
                    f"tokens_total={tt if tt is None else f'{tt:.0f}'} (需>= {RECOVER_FRAC*base:.0f}); "
                    f"halted={halted} reduced={reduced}")
        except Exception as ex:
            log(f"loop ERR (continue): {ex}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
