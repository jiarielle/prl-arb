# prl-arb: PRL ↔ WPRL cross-venue arbitrage bot

**If this saved you time, donations are welcome / 如果这个项目帮到了你，欢迎捐赠：**

- EVM (ETH / WPRL / USDT on Ethereum): `0xFe33c67e9BBbE826Ab2D843f66A8FcAC9d6ED156`
- PRL (Pearl native): `prl1pmmsmu9mhe0tgrfwgtctgvfwjlcs8f6wp4numxrn3mxskvv54fwyq209whd`

---

[English](#english) · [中文](#中文)

## English

Two-leg taker arbitrage between **SafeTrade** (centralized exchange, `PRL/USDT`) and **Uniswap V3** (Ethereum, `WPRL/USDT`), with automatic inventory rebalancing across **PearlBridge** (PRL ↔ WPRL).

It ran unattended on real money from 2026-05 to 2026-09. It is published as-is: the edge it traded has mostly been competed away, and the bridge it depends on is a single custodial relay (see *Known risks*). Read the code before you run it.

### How it works

| Process | Entry | Role |
|---|---|---|
| `prl-data-hub` | `data_hub.py` | Read-only market snapshot every few seconds → `state.json` + timeseries. Safe to restart any time. |
| `prl-auto-run` | `scripts/auto_run.py` | Trading loop. Detects a CEX/DEX price gap above fees + gas, sizes the trade by marginal profit against the live order book and Uniswap quoter, fires the CEX leg first and the DEX leg once the CEX fill is confirmed, then reconciles and cleans up residual exposure (a failed DEX leg is reversed on the CEX). |
| `prl-supply` | `supply.py` | Inventory manager. Keeps token and USDT split between the two venues near target shares by bridging PRL ↔ WPRL and moving USDT, with an in-flight ledger and a token-conservation guard against corrupted in-flight accounting. |

Supporting modules: `safetrade.py` (HMAC client behind a browser-TLS transport), `dex.py` (Uniswap V3 quoter / swap, private relay submission), `pearlbridge_api.py` (bridge relay state machine, stuck-bridge alerts), `arb.py` (opportunity evaluation, in-flight detection from on-chain mints/burns), `bridge_fees.py` (realized bridge fee ledger), `scripts/report.py` (leg-based PnL), `scripts/bridge_watchdog.py` (auto-resume after a stuck bridge clears), `scripts/pool_scan.py` (competitor scan of the Uniswap pool).

### Design notes that cost real money to learn

- **PnL is leg-based, never balance-based.** Every trade's PnL comes from fills + on-chain receipts (`logs/trades.jsonl`). Balance deltas lie during bursts and while bridges are in flight.
- **Exchange fill records do not carry your order side.** The `side` field on a SafeTrade trade record is per-fill and can disagree with the order that produced it. Sign every fill from *your own* order side, or a profitable trade books as a loss and trips the loss cap.
- **After cancelling, wait for a terminal state.** A cancelled order can still fill seconds later. `filled_amount` is only trustworthy once the order is `done` or `cancel`; treating a non-terminal `0` as "nothing filled" leaves you naked.
- **Bridges get stuck, in both directions.** Reverse bridges (PRL → WPRL) have sat in relay state `pending` for days while the relay minted normally for other recipients; forward bridges have shown `state=failed` after the PRL actually arrived. Always cross-check with the exchange deposit history and on-chain mints; never trust a single API state.
- **In-flight accounting has two failure modes.** Aged-out too early → phantom deficit → conservation guard freezes rebalancing. Kept too long → phantom balance → rebalancing never happens while the real side is empty. `arb._inflight_compute` reconciles against on-chain mints and the relay API, with the relay consulted before any age-based close.
- **Withdraw only to whitelisted beneficiaries.** The bot never withdraws to a raw address; SafeTrade beneficiary ids are configured in `.env`. This bounds the blast radius of a leaked API key.
- **Fail loud.** Missing routing identities (beneficiary ids, deposit addresses) abort startup. There are no "safe" defaults for anything that moves money.

### Known risks

- **PearlBridge is custodial on the Pearl side.** PRL backing WPRL sits in operator-controlled addresses on a UTXO chain with no smart contracts; the published audits cover only the Ethereum contracts and relay code. In 2026-09 the relay silently stopped minting for one recipient for over a week while serving everyone else. Size your bridge exposure accordingly.
- SafeTrade's API keys are IP-whitelisted; run from a fixed egress IP.
- Uniswap liquidity for WPRL is thin (a few hundred thousand USD). The sizing code accounts for it; the market may not.

### Setup

Requirements: Python 3.11+, Linux or macOS (uses `fcntl`), `web3` 7 or 8, optional `pm2`.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill in keys, RPC, contract/pool addresses, routing identities; keep DRY_RUN=true
cp baseline.json.example baseline.json   # set start_token / start_usdt after first funding
.venv/bin/python scripts/check_safetrade.py   # smoke tests
.venv/bin/python scripts/check_evm.py
.venv/bin/python scripts/approve_router.py   # once per fresh wallet: UNLIMITED router allowance for WPRL and the quote token (the trader refuses to start below that)
pm2 start ecosystem.config.js   # or run the three entry points under any supervisor
```

Run with `DRY_RUN=true` first; every fund-moving entry point (orders, swaps, bridges, withdrawals) reads it. Flip to `false` only after the dry-run log shows the shots you expect.

`ecosystem.config.js` resolves paths relative to the repo directory. Alerts go to WeCom (企业微信) if the `WECHAT_*` variables are set; otherwise they are logged only.

**Stopping.** `touch .STOP` halts the trader at its next poll (the path is resolved against the repo directory, so it is the same file for every process); `supply.py` does not read it, stop that process itself. A cumulative-loss halt writes a `CUM_LOSS` line into `.STOP`; nothing lifts that automatically. `scripts/bridge_watchdog.py` only lifts a `.STOP` you wrote with `echo bridge > .STOP`, only after every watched bridge reaches a cleared relay state, and only with a fresh `state.json`.

**Exchange permissions.** The trader needs a trade-only key. `supply.py` additionally needs withdraw permission and withdraws only to the whitelisted beneficiary ids in `.env`. `MAX_TRADE_USD` is the base shot, not a ceiling; set `HARD_MAX_TRADE_USD` if you want an absolute cap.

### License

MIT. No warranty. This moved real money for its author; it may lose yours.

---

## 中文

**SafeTrade**（中心化交易所，`PRL/USDT`）和 **Uniswap V3**（以太坊，`WPRL/USDT`）之间的双腿吃单套利机器人，通过 **PearlBridge**（PRL ↔ WPRL）自动在两边之间搬库存。

2026 年 5 月到 9 月用真钱无人值守跑过。现在原样公开：它吃的价差基本已经被卷平，它依赖的桥是单一托管 relay（见「已知风险」）。跑之前先读代码。

### 结构

| 进程 | 入口 | 作用 |
|---|---|---|
| `prl-data-hub` | `data_hub.py` | 只读行情快照，几秒一次，写 `state.json` 和时序。随时可重启。 |
| `prl-auto-run` | `scripts/auto_run.py` | 交易主循环。发现 CEX/DEX 价差超过手续费加 gas 后，按实时盘口和 Uniswap 报价器算边际利润定数量，先打 CEX 腿，成交确认后再打 DEX 腿，然后对账、清残余敞口（DEX 腿失败就在 CEX 反向平掉）。 |
| `prl-supply` | `supply.py` | 库存管理。通过 PRL ↔ WPRL 过桥和搬 USDT，把币和 U 在两边的份额维持在目标附近；带在途台账和总币守恒守卫，防在途记账错乱。 |

辅助模块：`safetrade.py`（HMAC 客户端，走浏览器 TLS 指纹传输）、`dex.py`（Uniswap V3 报价与成交、私有中继提交）、`pearlbridge_api.py`（桥 relay 状态机、卡桥告警）、`arb.py`（机会评估、按链上铸销识别在途）、`bridge_fees.py`（桥费实收台账）、`scripts/report.py`（腿式盈亏）、`scripts/bridge_watchdog.py`（卡桥解除后自动恢复）、`scripts/pool_scan.py`（Uniswap 池子对手扫描）。

### 用真钱换来的设计要点

- **盈亏只看腿式，不看余额差。** 每笔盈亏来自成交记录加链上回执（`logs/trades.jsonl`）。爆发期和有在途时余额差会骗人。
- **交易所成交记录里的方向字段不代表你的订单方向。** SafeTrade 成交记录的 `side` 是逐笔的，可能和产生它的订单相反。方向一律按自己下的单定，否则赚钱的单会记成亏损、触发亏损熔断。
- **撤单后必须等到终态。** 撤掉的单几秒后还可能成交。`filled_amount` 只有在 `done` 或 `cancel` 后才可信；把非终态的 0 当成没成交，会留下裸腿。
- **桥两个方向都会卡。** 反向桥（PRL → WPRL）出现过 relay 对别人正常铸币、对我们一个地址连续多天停在 `pending`；正向桥出现过币已到账、状态还显示 `failed`。一律用交易所充值记录和链上铸币交叉核对，不信单一 API 状态。
- **在途记账有两种坏法。** 过期太早 → 假缺口 → 守恒守卫冻结补仓；留太久 → 假平衡 → 真实一侧空了也不补。`arb._inflight_compute` 按链上铸币和 relay 接口对账，过期前先问 relay。
- **提币只走白名单。** 机器人从不向裸地址提币；SafeTrade 的 beneficiary id 写在 `.env`。API key 泄露的爆炸半径就被限制住了。
- **缺配置就报错退出。** 路由身份（beneficiary id、充值地址）缺失直接不启动。任何动钱的参数都没有"安全默认值"。

### 已知风险

- **PearlBridge 在 Pearl 侧是托管的。** 支撑 WPRL 的 PRL 放在运营方持钥匙的普通地址上（Pearl 是 UTXO 链，没有合约）；公开的审计只覆盖以太坊合约和 relay 代码。2026 年 9 月 relay 对一个收款地址静默停铸超过一周，同时给其他人照常铸。过桥的敞口自己掂量。
- SafeTrade 的 API key 绑 IP 白名单，要从固定出口跑。
- WPRL 在 Uniswap 的流动性很薄（几十万美元级）。定数量的代码考虑了这一点，市场不一定配合。

### 部署

环境：Python 3.11 以上，Linux 或 macOS（用到 `fcntl`），`web3` 7 或 8，`pm2` 可选。

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # 填 key、RPC、合约和池子地址、路由身份；先保持 DRY_RUN=true
cp baseline.json.example baseline.json   # 首次入金后填 start_token / start_usdt
.venv/bin/python scripts/check_safetrade.py   # 冒烟自检
.venv/bin/python scripts/check_evm.py
.venv/bin/python scripts/approve_router.py   # 新钱包跑一次：给 router 无限额授权 WPRL 和计价币（低于无限额交易器拒绝启动）
pm2 start ecosystem.config.js   # 或者用任意进程管理器跑三个入口
```

先用 `DRY_RUN=true` 跑：下单、换币、过桥、提现每一个动钱的入口都读它。看到干跑日志里的枪是你预期的样子，再改成 `false`。

`ecosystem.config.js` 的路径相对仓库目录解析。配了 `WECHAT_*` 变量就推企业微信，没配只写日志。

**停机。** `touch .STOP` 让交易器在下一轮停下（路径相对仓库目录解析，所有进程看的是同一个文件）；`supply.py` 不读它，要停就停那个进程。累计亏损熔断会往 `.STOP` 里写一行 `CUM_LOSS`，没有任何东西会自动解除它。`scripts/bridge_watchdog.py` 只解除你用 `echo bridge > .STOP` 写的那种停机文件，而且要等所有被盯的桥都到达已放款的终态、`state.json` 还得是新鲜的。

**交易所权限。** 交易器只需要交易权限的 key。`supply.py` 额外需要提币权限，且只向 `.env` 里的白名单 beneficiary id 提。`MAX_TRADE_USD` 是基础枪量不是上限；要绝对上限就设 `HARD_MAX_TRADE_USD`。

### 许可

MIT，不提供任何保证。它替作者动过真钱，也可能亏掉你的。
