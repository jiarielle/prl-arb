# prl-arb：PRL ↔ WPRL 跨所套利机器人 + 本地控制台

在 **SafeTrade**（中心化交易所，`PRL/USDT`）和 **Uniswap V3**（以太坊，`WPRL/USDT`）之间做双腿吃单套利，通过 **PearlBridge**（PRL ↔ WPRL）自动在两边之间搬库存。内置网页控制台：实时双所行情、进程管理、密钥保存、参数热改、飞书推送。

## 结构

| 进程 | 入口 | 作用 |
|---|---|---|
| `prl-data-hub` | `data_hub.py` | 只读行情快照，几秒一次，写 `state.json` 和时序。随时可重启。 |
| `prl-auto-run` | `scripts/auto_run.py` | 交易主循环。发现 CEX/DEX 价差超过手续费加 gas 后，按实时盘口和 Uniswap 报价器算边际利润定数量，先打 CEX 腿，成交确认后再打 DEX 腿，然后对账、清残余敞口（DEX 腿失败就在 CEX 反向平掉）。 |
| `prl-supply` | `supply.py` | 库存管理。通过 PRL ↔ WPRL 过桥和搬 USDT，把币和 U 在两边的份额维持在目标附近；带在途台账和总币守恒守卫，防在途记账错乱。 |
| 控制台 | `dashboard.py` | 本地网页控制台（只绑 127.0.0.1），见下文。 |

辅助模块：`safetrade.py`（HMAC 客户端，走浏览器 TLS 指纹传输）、`dex.py`（Uniswap V3 报价与成交、私有中继提交）、`pearlbridge_api.py`（桥 relay 状态机、卡桥告警）、`arb.py`（机会评估、按链上铸销识别在途）、`bridge_fees.py`（桥费实收台账）、`scripts/report.py`（腿式盈亏）、`scripts/bridge_watchdog.py`（卡桥解除后自动恢复）、`scripts/pool_scan.py`（Uniswap 池子对手扫描）。

## 用真钱换来的设计要点

- **盈亏只看腿式，不看余额差。** 每笔盈亏来自成交记录加链上回执（`logs/trades.jsonl`）。爆发期和有在途时余额差会骗人。
- **交易所成交记录里的方向字段不代表你的订单方向。** SafeTrade 成交记录的 `side` 是逐笔的，可能和产生它的订单相反。方向一律按自己下的单定，否则赚钱的单会记成亏损、触发亏损熔断。
- **撤单后必须等到终态。** 撤掉的单几秒后还可能成交。`filled_amount` 只有在 `done` 或 `cancel` 后才可信；把非终态的 0 当成没成交，会留下裸腿。
- **桥两个方向都会卡。** 反向桥（PRL → WPRL）出现过 relay 对别人正常铸币、对我们一个地址连续多天停在 `pending`；正向桥出现过币已到账、状态还显示 `failed`。一律用交易所充值记录和链上铸币交叉核对，不信单一 API 状态。
- **在途记账有两种坏法。** 过期太早 → 假缺口 → 守恒守卫冻结补仓；留太久 → 假平衡 → 真实一侧空了也不补。`arb._inflight_compute` 按链上铸币和 relay 接口对账，过期前先问 relay。
- **提币只走白名单。** 机器人从不向裸地址提币；SafeTrade 的 beneficiary id 写在 `.env`。API key 泄露的爆炸半径就被限制住了。
- **缺配置就报错退出。** 路由身份（beneficiary id、充值地址）缺失直接不启动。任何动钱的参数都没有"安全默认值"。

## 已知风险

- **PearlBridge 在 Pearl 侧是托管的。** 支撑 WPRL 的 PRL 放在运营方持钥匙的普通地址上（Pearl 是 UTXO 链，没有合约）；公开的审计只覆盖以太坊合约和 relay 代码。2026 年 9 月 relay 对一个收款地址静默停铸超过一周，同时给其他人照常铸。过桥的敞口自己掂量。
- SafeTrade 的 API key 绑 IP 白名单，要从固定出口跑。
- WPRL 在 Uniswap 的流动性很薄（几十万美元级）。定数量的代码考虑了这一点，市场不一定配合。

## 部署

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

## 本地控制台

```bash
.venv/bin/python dashboard.py   # → http://127.0.0.1:8787（仅本机可访问）
```

- **实时行情**：独立线程每 5 秒抓 SafeTrade 盘口 + Uniswap V3 报价（公共 RPC），与机器人进程无关；含双所边际仪表、1 小时涨跌。
- **边际图**：双所溢价曲线 + 开枪阈值线，bot 时序缺失时自动回退用控制台自身采样。
- **资金持仓**：CEX / EVM 两侧余额、币分布比例条（对照目标分仓）、桥在途。
- **进程管理**：三个进程的启停重启（替代 pm2），页面内直接看进程日志。
- **参数热改**：策略参数网页保存进 `.env`，运行中的进程自动重启生效；非法值有范围校验。
- **密钥与身份**：网页填写 API key / 私钥 / RPC / 路由身份，保存后**永不回显**（只显示"已配置"状态，留空=保持不变）。
- **飞书推送**：群机器人 webhook，可订阅停机/进程退出/每笔成交/心跳丢失/边际触发，支持签名校验。

**安全模型**：只绑定 127.0.0.1；POST 操作带随机令牌防误触；密钥值从不下发到前端；累计亏损熔断（CUM_LOSS）拒绝从网页解除，必须人工核对 `report.py --pnl` 后手动 `rm .STOP`。

## 许可

MIT（详见 LICENSE 文件），不提供任何保证。动的是真钱，量力而行。
