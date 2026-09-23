import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)   # .env 为准: 改了 key 后 pm2 restart 即生效(pm2 会注入旧 env, 须覆盖)


_REPO_DIR = Path(__file__).resolve().parent


def _repo_path(p: str) -> str:
    """Relative paths in .env resolve against the repo directory, not the process cwd, so
    `.STOP` means the same file for the trader, the reporter and the watchdog."""
    q = Path(p)
    return str(q if q.is_absolute() else _REPO_DIR / q)


def _env(key: str, default=None, cast=str):
    v = os.getenv(key, default)
    if v is None or v == "":
        return default
    return cast(v) if cast is not str else v


@dataclass
class Config:
    # SafeTrade
    safetrade_api_key: str
    safetrade_api_secret: str
    safetrade_base_url: str
    safetrade_market: str
    safetrade_taker_fee_bps: int

    # EVM
    rpc_url: str            # Alchemy 主(新免费账号)—— 仅 alchemy_getAssetTransfers 必须走它
    rpc_url_fallback: str   # Alchemy 备(PAYG)—— 主限额时兜底
    rpc_public: str         # 公共节点(逗号分隔)—— 标准高频调用走这里, 省 Alchemy CU
    private_tx_rpc: str     # 私有中继: 只用来广播"放宽了最低收到量"的 swap, 不进公开内存池
    private_key: str
    wallet_address: str

    # Token addresses
    wprl_address: str
    usdt_address: str
    usdc_address: str
    weth_address: str
    router_address: str

    # Pool config
    pool_address: str
    pool_fee_tier: int
    quote_token_symbol: str  # USDT | USDC | WETH
    dex_pool_fee_bps: int

    # Strategy
    min_gap_bps: int
    rebalance_gap_bps: int        # (legacy) discounted threshold; superseded by linear decay
    share_target_pct: float       # target CEX token share
    share_deadband_pct: float     # only go asymmetric when share drifts beyond this
    bridge_inflight_window_sec: int  # treat a PRL withdrawal as still-bridging within this age
    min_trade_pnl_usd: float       # skip trades whose expected pnl is below this (gas floor)
    max_trade_usd: float
    hard_max_trade_usd: float   # absolute per-shot USD cap applied AFTER adaptive sizing; 0 = no cap
    gas_buffer_usd: float
    slippage_bps: int

    min_prl_inventory: float
    max_prl_inventory: float
    min_wprl_inventory: float
    max_wprl_inventory: float
    min_usdt_inventory: float
    min_usdc_inventory: float

    # Bridge
    bridge_fee_bps_prl_to_wprl: int
    bridge_fee_bps_wprl_to_prl: int
    bridge_min_prl: float
    bridge_advisory_interval_sec: int
    bridge_skew_high_pct: float
    bridge_skew_low_pct: float

    cex_amount_precision: int
    poll_interval_sec: int
    dry_run: bool
    kill_switch_file: str

    @property
    def quote_token_address(self) -> str:
        m = {"USDT": self.usdt_address, "USDC": self.usdc_address, "WETH": self.weth_address}
        return m[self.quote_token_symbol]


def load_config() -> Config:
    return Config(
        safetrade_api_key=_env("SAFETRADE_API_KEY", ""),
        safetrade_api_secret=_env("SAFETRADE_API_SECRET", ""),
        safetrade_base_url=_env("SAFETRADE_BASE_URL", "https://safe.trade/api/v2"),
        safetrade_market=_env("SAFETRADE_MARKET", "prlusdt"),
        safetrade_taker_fee_bps=_env("SAFETRADE_TAKER_FEE_BPS", 20, int),
        rpc_url=_env("EVM_RPC_URL", ""),
        rpc_url_fallback=_env("EVM_RPC_URL_FALLBACK", ""),
        rpc_public=_env("EVM_RPC_PUBLIC", "https://ethereum-rpc.publicnode.com,https://eth.drpc.org"),
        private_tx_rpc=_env("PRIVATE_TX_RPC_URL", "https://rpc.flashbots.net/fast"),
        private_key=_env("EVM_PRIVATE_KEY", ""),
        wallet_address=_env("EVM_WALLET_ADDRESS", ""),
        wprl_address=_env("WPRL_ADDRESS", ""),
        usdt_address=_env("USDT_ADDRESS", ""),
        usdc_address=_env("USDC_ADDRESS", ""),
        weth_address=_env("WETH_ADDRESS", ""),
        router_address=_env("UNISWAP_ROUTER", ""),
        pool_address=_env("WPRL_POOL_ADDRESS", ""),
        pool_fee_tier=_env("WPRL_POOL_FEE_TIER", 3000, int),
        quote_token_symbol=_env("WPRL_QUOTE_TOKEN", "USDT"),
        dex_pool_fee_bps=_env("DEX_POOL_FEE_BPS", 30, int),
        min_gap_bps=_env("MIN_GAP_BPS", 200, int),
        rebalance_gap_bps=_env("REBALANCE_GAP_BPS", 50, int),
        share_target_pct=_env("SHARE_TARGET_PCT", 75, float),
        share_deadband_pct=_env("SHARE_DEADBAND_PCT", 10, float),
        bridge_inflight_window_sec=_env("BRIDGE_INFLIGHT_WINDOW_SEC", 5400, int),
        min_trade_pnl_usd=_env("MIN_TRADE_PNL_USD", 1, float),
        max_trade_usd=_env("MAX_TRADE_USD", 500, float),
        hard_max_trade_usd=_env("HARD_MAX_TRADE_USD", 0, float),
        gas_buffer_usd=_env("GAS_BUFFER_USD", 5, float),
        slippage_bps=_env("SLIPPAGE_TOLERANCE_BPS", 100, int),
        min_prl_inventory=_env("MIN_PRL_INVENTORY", 2000, float),
        max_prl_inventory=_env("MAX_PRL_INVENTORY", 20000, float),
        min_wprl_inventory=_env("MIN_WPRL_INVENTORY", 2000, float),
        max_wprl_inventory=_env("MAX_WPRL_INVENTORY", 20000, float),
        min_usdt_inventory=_env("MIN_USDT_INVENTORY", 200, float),
        min_usdc_inventory=_env("MIN_USDC_INVENTORY", 200, float),
        bridge_fee_bps_prl_to_wprl=_env("BRIDGE_FEE_BPS_PRL_TO_WPRL", 50, int),
        bridge_fee_bps_wprl_to_prl=_env("BRIDGE_FEE_BPS_WPRL_TO_PRL", 0, int),
        bridge_min_prl=_env("BRIDGE_MIN_PRL", 4, float),
        bridge_advisory_interval_sec=_env("BRIDGE_ADVISORY_INTERVAL_SEC", 3600, int),
        bridge_skew_high_pct=_env("BRIDGE_SKEW_HIGH_PCT", 70, float),
        bridge_skew_low_pct=_env("BRIDGE_SKEW_LOW_PCT", 30, float),
        cex_amount_precision=_env("CEX_AMOUNT_PRECISION", 4, int),
        poll_interval_sec=_env("POLL_INTERVAL_SEC", 10, int),
        dry_run=_env("DRY_RUN", "true").lower() in ("1", "true", "yes"),
        kill_switch_file=_repo_path(_env("KILL_SWITCH_FILE", ".STOP")),
    )
