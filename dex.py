"""Uniswap V3 client for WPRL pool: read spot price, swap exact-in.

Pool spot: slot0().sqrtPriceX96 → price of token1 in token0 units.
"""
import os as _os
import fcntl as _fcntl
import contextlib as _ctx
import time as _time
from decimal import Decimal
from web3 import Web3, HTTPProvider
from web3.exceptions import TransactionNotFound

# Cross-process lock: prl-auto-run and prl-supply share one EVM wallet, so their sends
# (swaps / ERC20 transfers / approvals / bridge burns) must not race the same nonce.
# Wrap only build-nonce -> send_raw_transaction (NOT the slow receipt wait).
_EVM_LOCK_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "logs", "evm_nonce.lock")


@_ctx.contextmanager
def evm_send_lock():
    _os.makedirs(_os.path.dirname(_EVM_LOCK_PATH), exist_ok=True)
    f = open(_EVM_LOCK_PATH, "w")
    try:
        _fcntl.flock(f, _fcntl.LOCK_EX)
        yield
    finally:
        try:
            _fcntl.flock(f, _fcntl.LOCK_UN)
        finally:
            f.close()


class SmartProvider(HTTPProvider):
    """方法路由 + 多端点容错。
      - `alchemy_*`(本项目仅 getAssetTransfers,每 ~3min 一次)走 Alchemy 链
        [新账号 -> PAYG],只有它需要专有方法。
      - 其余标准 JSON-RPC(价格/余额/gas/发交易/回执,每 4s 高频)走公共链
        [publicnode -> drpc],公共全挂才退到 Alchemy 兜底。
    => Alchemy CU 几乎只被 getAssetTransfers 消耗,免费账号基本满不了。
    合约 revert 等"合法错误"不算节点故障,不切换(否则会吞掉正常的报价 revert)。"""

    def __init__(self, public_urls, alchemy_urls):
        super().__init__((public_urls or alchemy_urls)[0])
        self._public = [HTTPProvider(u, request_kwargs={"timeout": 10}) for u in public_urls]
        self._alchemy = [HTTPProvider(u) for u in alchemy_urls]

    @staticmethod
    def _legit_error(r):
        """True = 合约层合法错误(如 execution reverted),是答案本身,不该切端点。"""
        err = r.get("error") if isinstance(r, dict) else None
        if not err:
            return None  # 无 error,正常返回
        msg = str(err).lower()
        return ("revert" in msg or "insufficient funds" in msg or "execution" in msg)

    @staticmethod
    def _log(msg):
        """只在 fallback/故障时写, 正常路径不写 -> 该文件行数即 fallback 次数。"""
        try:
            import os, time as _t
            with open(os.path.join(os.path.dirname(__file__), "logs", "rpc_fallback.log"), "a") as f:
                f.write(f"[{_t.strftime('%m-%d %H:%M:%S')} pid{os.getpid()}] {msg}\n")
        except Exception:
            pass

    def _try_chain(self, providers, method, params, label):
        last = None
        for i, p in enumerate(providers):
            try:
                r = p.make_request(method, params)
            except Exception as e:
                last = e
                self._log(f"{label}[{i}] EXC {method}: {str(e)[:60]}")
                continue
            le = self._legit_error(r)
            if le is None or le is True:
                return r          # 正常结果 或 合法 revert -> 直接返回
            last = Exception(str(r.get("error")))
            self._log(f"{label}[{i}] ERR {method}: {str(r.get('error'))[:60]}")   # 节点故障 -> 试下一个
        raise last if last else RuntimeError("no rpc provider")

    def make_request(self, method, params):
        if method.startswith("alchemy_"):
            return self._try_chain(self._alchemy, method, params, "ALC")
        try:
            return self._try_chain(self._public, method, params, "PUB")
        except Exception:
            self._log(f"PUBLIC-ALL-DOWN -> Alchemy 兜底: {method}")
            return self._try_chain(self._alchemy, method, params, "ALCfb")   # 公共全挂 -> Alchemy 兜底


ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "string"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    # NOTE: outputs intentionally empty. USDT (Tether) is non-standard and returns
    # NOTHING from transfer (not the bool the ERC20 spec mandates), so declaring a bool
    # output makes web3's eth_call decode fail with BadFunctionCallOutput. With no
    # declared output, the dry-run .call() still executes USDT's require()s (reverts on
    # insufficient balance) but doesn't try to decode a non-existent return value.
    {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": []},
]

UNIV3_POOL_ABI = [
    {"name": "slot0", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [
         {"name": "sqrtPriceX96", "type": "uint160"},
         {"name": "tick", "type": "int24"},
         {"name": "observationIndex", "type": "uint16"},
         {"name": "observationCardinality", "type": "uint16"},
         {"name": "observationCardinalityNext", "type": "uint16"},
         {"name": "feeProtocol", "type": "uint8"},
         {"name": "unlocked", "type": "bool"},
     ]},
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "fee", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "uint24"}]},
    {"name": "liquidity", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "uint128"}]},
]

# Uniswap V3 SwapRouter02 — exactInputSingle
ROUTER_ABI = [
    {"name": "exactInputSingle", "type": "function", "stateMutability": "payable",
     "inputs": [{
         "name": "params", "type": "tuple", "components": [
             {"name": "tokenIn", "type": "address"},
             {"name": "tokenOut", "type": "address"},
             {"name": "fee", "type": "uint24"},
             {"name": "recipient", "type": "address"},
             {"name": "amountIn", "type": "uint256"},
             {"name": "amountOutMinimum", "type": "uint256"},
             {"name": "sqrtPriceLimitX96", "type": "uint160"},
         ],
     }],
     "outputs": [{"name": "amountOut", "type": "uint256"}]},
    # SwapRouter02's exactInputSingle has no deadline of its own; multicall(deadline, data) adds one
    {"name": "multicall", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "deadline", "type": "uint256"}, {"name": "data", "type": "bytes[]"}],
     "outputs": [{"name": "results", "type": "bytes[]"}]},
]

# QuoterV2 (mainnet) — simulates a swap without executing it
QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
QUOTER_ABI = [
    {"name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{
         "name": "params", "type": "tuple", "components": [
             {"name": "tokenIn", "type": "address"},
             {"name": "tokenOut", "type": "address"},
             {"name": "amountIn", "type": "uint256"},
             {"name": "fee", "type": "uint24"},
             {"name": "sqrtPriceLimitX96", "type": "uint160"},
         ],
     }],
     "outputs": [
         {"name": "amountOut", "type": "uint256"},
         {"name": "sqrtPriceX96After", "type": "uint160"},
         {"name": "initializedTicksCrossed", "type": "uint32"},
         {"name": "gasEstimate", "type": "uint256"},
     ]},
    {"name": "quoteExactOutputSingle", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{
         "name": "params", "type": "tuple", "components": [
             {"name": "tokenIn", "type": "address"},
             {"name": "tokenOut", "type": "address"},
             {"name": "amount", "type": "uint256"},
             {"name": "fee", "type": "uint24"},
             {"name": "sqrtPriceLimitX96", "type": "uint160"},
         ],
     }],
     "outputs": [
         {"name": "amountIn", "type": "uint256"},
         {"name": "sqrtPriceX96After", "type": "uint160"},
         {"name": "initializedTicksCrossed", "type": "uint32"},
         {"name": "gasEstimate", "type": "uint256"},
     ]},
]


Q96 = Decimal(2) ** 96


class DexClient:
    def __init__(self, cfg):
        self.cfg = cfg
        _pub = [u.strip() for u in (getattr(cfg, "rpc_public", "") or "").split(",") if u.strip()]
        _alc = [u for u in (cfg.rpc_url, getattr(cfg, "rpc_url_fallback", "")) if u]
        self.w3 = Web3(SmartProvider(_pub or _alc, _alc or _pub))
        self.account = self.w3.eth.account.from_key(cfg.private_key) if cfg.private_key else None
        self.address = Web3.to_checksum_address(cfg.wallet_address) if cfg.wallet_address else None

        self.wprl = self._erc20(cfg.wprl_address)
        self.quote = self._erc20(cfg.quote_token_address)
        self.router = self.w3.eth.contract(
            address=Web3.to_checksum_address(cfg.router_address), abi=ROUTER_ABI
        )
        self.quoter = self.w3.eth.contract(
            address=Web3.to_checksum_address(QUOTER_V2), abi=QUOTER_ABI
        )
        self.pool = None
        self._pool_meta = None
        if cfg.pool_address:
            self.pool = self.w3.eth.contract(
                address=Web3.to_checksum_address(cfg.pool_address), abi=UNIV3_POOL_ABI
            )

        self._decimals_cache: dict[str, int] = {}

    def _erc20(self, addr: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ERC20_ABI)

    # ---------- introspection ----------
    def decimals(self, token_addr: str) -> int:
        k = token_addr.lower()
        if k not in self._decimals_cache:
            self._decimals_cache[k] = self._erc20(token_addr).functions.decimals().call()
        return self._decimals_cache[k]

    def balance(self, token_addr: str) -> Decimal:
        raw = self._erc20(token_addr).functions.balanceOf(self.address).call()
        return Decimal(raw) / (Decimal(10) ** self.decimals(token_addr))

    def pool_meta(self) -> dict:
        if self._pool_meta is None:
            t0 = self.pool.functions.token0().call().lower()
            t1 = self.pool.functions.token1().call().lower()
            fee = self.pool.functions.fee().call()
            self._pool_meta = {"token0": t0, "token1": t1, "fee": fee}
        return self._pool_meta

    def wprl_price_in_quote(self) -> Decimal:
        """Spot price of 1 WPRL in quote token units."""
        slot0 = self.pool.functions.slot0().call()
        return self.price_from_sqrt(slot0[0])

    def price_from_sqrt(self, sqrt_price_x96) -> Decimal:
        """sqrtPriceX96 -> price of 1 WPRL in quote token units (no fee)."""
        meta = self.pool_meta()
        sqrt_p = Decimal(sqrt_price_x96)
        # price1_per_0 = (sqrtPriceX96 / 2^96)^2 * 10^(dec0 - dec1)
        wprl = self.cfg.wprl_address.lower()
        quote = self.cfg.quote_token_address.lower()
        d0 = self.decimals(meta["token0"])
        d1 = self.decimals(meta["token1"])
        ratio = (sqrt_p / Q96) ** 2  # token1 per token0 (raw)
        # adjust for decimals: price of 1 unit token0 (human) in token1 (human)
        price_0_in_1 = ratio * (Decimal(10) ** (d0 - d1))
        if meta["token0"] == wprl and meta["token1"] == quote:
            return price_0_in_1
        elif meta["token0"] == quote and meta["token1"] == wprl:
            return Decimal(1) / price_0_in_1
        else:
            raise RuntimeError(
                f"Pool tokens {meta} don't match WPRL={wprl}/quote={quote}"
            )

    def quote_exact_in(self, token_in: str, token_out: str, amount_in_human: Decimal) -> Decimal:
        """Simulate exact-in swap via QuoterV2 (no tx). Returns human amount out.

        Accounts for pool fee + slippage from the size moving the price.
        """
        d_in = self.decimals(token_in)
        d_out = self.decimals(token_out)
        amount_in = int(amount_in_human * (Decimal(10) ** d_in))
        params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            amount_in,
            self.cfg.pool_fee_tier,
            0,  # no sqrtPriceLimit
        )
        # QuoterV2.quoteExactInputSingle is `nonpayable` but pure-view via revert pattern;
        # .call() simulates without signing.
        result = self.quoter.functions.quoteExactInputSingle(params).call()
        amount_out_raw = result[0]
        return Decimal(amount_out_raw) / (Decimal(10) ** d_out)

    def quote_exact_out(self, token_in: str, token_out: str, amount_out_human: Decimal) -> Decimal:
        """Simulate: to receive exactly amount_out_human of token_out, how much token_in?
        Returns human token_in amount (incl pool fee + price impact)."""
        d_in = self.decimals(token_in)
        d_out = self.decimals(token_out)
        amount_out = int(amount_out_human * (Decimal(10) ** d_out))
        params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            amount_out,
            self.cfg.pool_fee_tier,
            0,
        )
        result = self.quoter.functions.quoteExactOutputSingle(params).call()
        amount_in_raw = result[0]
        return Decimal(amount_in_raw) / (Decimal(10) ** d_in)

    def quote_buy_wprl_full(self, wprl_out_human: Decimal):
        """Buy exactly `wprl_out_human` WPRL: returns (quote token cost, pool price after)."""
        d_in = self.decimals(self.cfg.quote_token_address)
        d_out = self.decimals(self.cfg.wprl_address)
        params = (Web3.to_checksum_address(self.cfg.quote_token_address),
                  Web3.to_checksum_address(self.cfg.wprl_address),
                  int(wprl_out_human * (Decimal(10) ** d_out)), self.cfg.pool_fee_tier, 0)
        r = self.quoter.functions.quoteExactOutputSingle(params).call()
        return Decimal(r[0]) / (Decimal(10) ** d_in), self.price_from_sqrt(r[1])

    def quote_sell_wprl_full(self, wprl_in_human: Decimal):
        """Sell exactly `wprl_in_human` WPRL: returns (quote token proceeds, pool price after)."""
        d_in = self.decimals(self.cfg.wprl_address)
        d_out = self.decimals(self.cfg.quote_token_address)
        params = (Web3.to_checksum_address(self.cfg.wprl_address),
                  Web3.to_checksum_address(self.cfg.quote_token_address),
                  int(wprl_in_human * (Decimal(10) ** d_in)), self.cfg.pool_fee_tier, 0)
        r = self.quoter.functions.quoteExactInputSingle(params).call()
        return Decimal(r[0]) / (Decimal(10) ** d_out), self.price_from_sqrt(r[1])

    # ---------- allowance / approve ----------
    def allowance(self, token_addr: str) -> int:
        return self._erc20(token_addr).functions.allowance(
            self.address, self.router.address
        ).call()

    def approve_max(self, token_addr: str, dry_run: bool = False) -> str:
        token = self._erc20(token_addr)
        max_uint = 2**256 - 1
        if dry_run:
            return f"DRY: approve {token_addr} -> router {self.router.address} for max"
        with evm_send_lock():
            tx = token.functions.approve(self.router.address, max_uint).build_transaction({
                "from": self.address,
                "nonce": self.w3.eth.get_transaction_count(self.address, "pending"),
                "gas": 80_000,
                "maxFeePerGas": self.w3.eth.gas_price * 3 + self.w3.to_wei(1, "gwei"),
                "maxPriorityFeePerGas": min(self.w3.to_wei(1, "gwei"), self.w3.eth.gas_price),
                "chainId": self.w3.eth.chain_id,
            })
            signed = self.w3.eth.account.sign_transaction(tx, self.cfg.private_key)
            h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return h.hex()

    # ---------- plain ERC20 transfer (e.g. EVM->CEX USDT deposit) ----------
    def transfer_erc20(self, token_addr: str, to_addr: str, amount_human: Decimal,
                       dry_run: bool = False) -> dict:
        """Send `amount_human` of an ERC20 to `to_addr` (a plain transfer, NOT a swap).
        Used to top up the CEX by depositing USDT to SafeTrade's on-chain deposit address.
        Returns {tx_hash, status, gas_used} once mined, or a dry-run preview."""
        token = self._erc20(token_addr)
        d = self.decimals(token_addr)
        amount = int(amount_human * (Decimal(10) ** d))
        to = Web3.to_checksum_address(to_addr)
        if dry_run:
            # eth_call simulates the transfer without broadcasting — surfaces reverts
            # (insufficient balance, etc.) the same way the dry-run swap does.
            token.functions.transfer(to, amount).call({"from": self.address})
            return {"dry_run": True, "to": to, "amount_raw": amount}
        fn = token.functions.transfer(to, amount)
        with evm_send_lock():
            tx = fn.build_transaction({
                "from": self.address,
                "nonce": self.w3.eth.get_transaction_count(self.address, "pending"),
                "gas": 100_000,
                "maxFeePerGas": self.w3.eth.gas_price * 3 + self.w3.to_wei(1, "gwei"),
                "maxPriorityFeePerGas": min(self.w3.to_wei(1, "gwei"), self.w3.eth.gas_price),
                "chainId": self.w3.eth.chain_id,
                "value": 0,
            })
            signed = self.w3.eth.account.sign_transaction(tx, self.cfg.private_key)
            h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(h, timeout=120, poll_latency=12)
        return {"tx_hash": h.hex(), "status": receipt.status, "gas_used": receipt.gasUsed}

    # ---------- receipt confirmation ----------
    def wait_receipt(self, tx_hash, attempts: int = 8, delay: float = 8.0):
        """Read a tx receipt with RETRIES, swallowing transient RPC errors (429/timeout).
        A broadcast tx is ON-CHAIN regardless of whether the FIRST receipt read 429s; the
        old code let that 429 propagate and the caller wrongly concluded the swap failed
        (then reverse-hedged a leg that had actually executed). Returns the receipt, or
        None only if it's genuinely unreadable after all attempts (tx may be pending)."""
        import time as _t
        for i in range(attempts):
            try:
                rc = self.w3.eth.get_transaction_receipt(tx_hash)
                if rc is not None:
                    return rc
            except Exception:
                pass  # TransactionNotFound / 429 / timeout — retry
            if i < attempts - 1:
                _t.sleep(delay)
        return None

    def erc20_delta_from_receipt(self, receipt, token_addr: str, me: str = None) -> Decimal:
        """Net amount of `token_addr` moved to/from our wallet in a tx receipt, in human
        units (signed: +received / -sent). Uses the token's own decimals (WPRL 8, USDT 6).
        Reading the swap's actual Transfer logs makes leg PnL immune to concurrent balance
        moves (supply transfers/bridges) — same principle as the per-leg token residual."""
        me = (me or self.address).lower()
        tok = token_addr.lower()
        dec = self.decimals(token_addr)
        tsig = self.w3.keccak(text="Transfer(address,address,uint256)").hex()
        tsig = tsig if tsig.startswith("0x") else "0x" + tsig
        delta = Decimal(0)
        for lg in receipt.logs:
            t0 = lg["topics"][0].hex(); t0 = t0 if t0.startswith("0x") else "0x" + t0
            if t0 != tsig or lg["address"].lower() != tok:
                continue
            frm = "0x" + lg["topics"][1].hex()[-40:]; to = "0x" + lg["topics"][2].hex()[-40:]
            amt = Decimal(int(lg["data"].hex(), 16)) / (Decimal(10) ** dec)
            if to.lower() == me: delta += amt
            if frm.lower() == me: delta -= amt
        return delta

    def wprl_delta_from_receipt(self, receipt, me: str = None) -> Decimal:
        """Net WPRL moved to/from our wallet (signed: +bought / -sold)."""
        return self.erc20_delta_from_receipt(receipt, self.cfg.wprl_address, me)

    # ---------- swap ----------
    def _alchemy_nodes(self):
        if getattr(self, "_alc_w3", None) is None:
            urls = [u for u in (self.cfg.rpc_url, getattr(self.cfg, "rpc_url_fallback", "")) if u]
            self._alc_w3 = [Web3(HTTPProvider(u, request_kwargs={"timeout": 10})) for u in urls]
        return self._alc_w3

    def wait_until_deadline(self, tx_hash, deadline_ts: int, poll: float = 3.0,
                            margin_sec: int = 24, hard_cap_sec: int = 180):
        """Outcome of a swap that carries an on-chain deadline. Returns the receipt, the
        string "expired", or None (could not determine).

        "expired" is a PROOF, not a timeout: one and the same node reports a head block
        whose timestamp is past deadline+margin AND has no receipt for the hash. After the
        deadline the tx can only revert, so it can never move tokens. This replaces the
        "not in the public mempool = dropped" rule, which is meaningless for a privately
        relayed tx (it is never in the public mempool)."""
        import time as _t
        stop = deadline_ts + hard_cap_sec
        while _t.time() < stop:
            try:
                rc = self.w3.eth.get_transaction_receipt(tx_hash)
                if rc is not None:
                    return rc
            except Exception:
                pass
            if _t.time() > deadline_ts + margin_sec:
                for node in self._alchemy_nodes():
                    try:
                        head_ts = node.eth.get_block("latest").timestamp
                        try:
                            rc = node.eth.get_transaction_receipt(tx_hash)
                        except TransactionNotFound:
                            rc = None
                        if rc is not None:
                            return rc
                        if head_ts > deadline_ts + margin_sec:
                            return "expired"
                    except Exception:
                        continue
            _t.sleep(poll)
        return None

    def swap_exact_in(self, token_in: str, token_out: str, amount_in_human: Decimal,
                      min_out_human: Decimal, dry_run: bool = False,
                      deadline_sec: int = None, private: bool = False, tip_gwei=None) -> dict:
        """deadline_sec: wrap the swap in multicall(deadline) and wait with the deadline
        proof. private: broadcast through cfg.private_tx_rpc only (never the public mempool);
        a send error there does NOT mean the tx is dead, so the caller still gets the hash
        and the deadline decides. Defaults reproduce the original public path exactly."""
        d_in = self.decimals(token_in)
        d_out = self.decimals(token_out)
        amount_in = int(amount_in_human * (Decimal(10) ** d_in))
        min_out = int(min_out_human * (Decimal(10) ** d_out))
        params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            self.cfg.pool_fee_tier,
            self.address,
            amount_in,
            min_out,
            0,
        )
        if dry_run:
            return {"dry_run": True, "params": params}
        fn = self.router.functions.exactInputSingle(params)
        deadline_ts = None
        if deadline_sec:
            import time as _t0
            deadline_ts = int(_t0.time()) + int(deadline_sec)
            inner = self.router.encode_abi("exactInputSingle", args=[params])
            fn = self.router.functions.multicall(deadline_ts, [inner])
        if private and not deadline_ts:
            raise ValueError("private send requires a deadline")
        # NONCE RESEND: this wallet is shared (auto_run swaps + supply bridge/transfer),
        # so a nonce can go stale between build and send ("nonce too low"). That's a
        # trivial, transient condition — just refetch the nonce and resend, don't let it
        # bubble up as a failed leg (which used to reverse-hedge + book a phantom loss).
        # Use the 'pending' count so back-to-back sends pick the next free nonce.
        import time as _t
        tx_hash = None
        send_error = None
        for attempt in range(3):
            try:
                with evm_send_lock():
                    tx = fn.build_transaction({
                        "from": self.address,
                        "nonce": self.w3.eth.get_transaction_count(self.address, "pending"),
                        "gas": 250_000 if not deadline_ts else 280_000,
                        "maxFeePerGas": self.w3.eth.gas_price * 3 + self.w3.to_wei(tip_gwei or 1, "gwei"),
                        "maxPriorityFeePerGas": (self.w3.to_wei(tip_gwei, "gwei") if tip_gwei else
                                                 min(self.w3.to_wei(1, "gwei"), self.w3.eth.gas_price)),
                        "chainId": self.w3.eth.chain_id,
                        "value": 0,
                    })
                    signed = self.w3.eth.account.sign_transaction(tx, self.cfg.private_key)
                    if private:
                        # hash is known from the signed bytes, so a relay timeout can't lose it
                        tx_hash = signed.hash.hex()
                        try:
                            Web3(HTTPProvider(self.cfg.private_tx_rpc, request_kwargs={"timeout": 10})
                                 ).eth.send_raw_transaction(signed.raw_transaction)
                        except Exception as e:
                            send_error = f"{type(e).__name__} {str(e).split('http')[0][:80]}"
                        break
                    # Once send returns, the tx IS broadcast — capture the hash BEFORE confirming
                    # so a failed receipt-read can't lose it.
                    h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
                tx_hash = h.hex()
                break
            except Exception as e:
                if "nonce" in str(e).lower() and attempt < 2:
                    _t.sleep(1.0)   # let the competing tx settle, refetch nonce, resend
                    continue
                raise
        extra = {"deadline": deadline_ts, "private": bool(private), "send_error": send_error}
        if deadline_ts:
            receipt = self.wait_until_deadline(tx_hash, deadline_ts)
            if receipt == "expired":
                return {"tx_hash": tx_hash, "status": None, "gas_used": None, "expired": True, **extra}
        else:
            receipt = self.wait_receipt(tx_hash)   # retries, swallows transient 429/timeout
        if receipt is None:
            return {"tx_hash": tx_hash, "status": None, "gas_used": None, **extra}
        return {"tx_hash": tx_hash, "status": receipt.status, "gas_used": receipt.gasUsed, **extra}
