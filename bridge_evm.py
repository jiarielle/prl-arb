"""PearlBridge EVM-side automation: redeem WPRL -> native PRL to any Pearl address.

ABI reverse-engineered from confirmed burn tx 0x5482...cf6e9:
  selector 0xd2f33ed6 = redeem(uint256 amount, string pearlAddress)
  - amount: WPRL raw (8 decimals)
  - pearlAddress: destination Pearl address string (can be a CEX deposit addr!)

WPRL->PRL is FREE and the redeem accepts an arbitrary recipient, so we can send
straight to SafeTrade's PRL deposit address -> fully automatable CEX refill.
"""
from decimal import Decimal
from web3 import Web3
from dex import evm_send_lock   # shared cross-process EVM nonce lock (same wallet as swaps)

BRIDGE_ADDR = "0xA6571B73489d4eBFA269a107208665dF7C80Aef5"
# Verified ABI: proxy 0xA657..->impl BridgeController 0xF73E..; selector 0xd2f33ed6
REDEEM_ABI = [{
    "name": "requestBurn", "type": "function", "stateMutability": "nonpayable",
    "inputs": [
        {"name": "amount", "type": "uint256"},
        {"name": "pearlAddress", "type": "string"},
    ],
    "outputs": [],
}]
WPRL_DECIMALS = 8


class BridgeEVM:
    def __init__(self, dex):
        self.dex = dex
        self.w3 = dex.w3
        self.cfg = dex.cfg
        self.contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(BRIDGE_ADDR), abi=REDEEM_ABI)

    def _ensure_allowance(self, amount_raw, dry_run):
        """Bridge must be approved to pull WPRL. Returns approve tx hash or None."""
        cur = self.dex._erc20(self.cfg.wprl_address).functions.allowance(
            self.dex.address, Web3.to_checksum_address(BRIDGE_ADDR)).call()
        if cur >= amount_raw:
            return None
        if dry_run:
            return "DRY: would approve bridge for WPRL"
        tok = self.dex._erc20(self.cfg.wprl_address)
        with evm_send_lock():
            tx = tok.functions.approve(Web3.to_checksum_address(BRIDGE_ADDR), 2**256 - 1).build_transaction({
                "from": self.dex.address,
                "nonce": self.w3.eth.get_transaction_count(self.dex.address, "pending"),
                "gas": 80_000,
                "maxFeePerGas": self.w3.eth.gas_price * 3 + self.w3.to_wei(1, "gwei"),
                "maxPriorityFeePerGas": min(self.w3.to_wei(1, "gwei"), self.w3.eth.gas_price),
                "chainId": self.w3.eth.chain_id,
            })
            signed = self.w3.eth.account.sign_transaction(tx, self.cfg.private_key)
            h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        self.w3.eth.wait_for_transaction_receipt(h, timeout=120)
        return h.hex()

    def redeem(self, amount_wprl: Decimal, pearl_address: str, max_amount: Decimal,
               dry_run: bool = True) -> dict:
        """Burn `amount_wprl` WPRL, sending native PRL to `pearl_address`.
        Hard cap `max_amount` (token units) — refuses above it."""
        if amount_wprl > max_amount:
            return {"error": f"amount {amount_wprl} > max {max_amount}; refused"}
        bal = self.dex.balance(self.cfg.wprl_address)
        if amount_wprl > bal:
            return {"error": f"amount {amount_wprl} > WPRL balance {bal}; refused"}

        amount_raw = int(amount_wprl * (Decimal(10) ** WPRL_DECIMALS))
        params = (amount_raw, pearl_address)

        # Allowance FIRST: on a fresh wallet the simulation below reverts for lack of
        # allowance, and bailing out there would mean the approve never happens.
        approve_res = self._ensure_allowance(amount_raw, dry_run)

        # then simulate (eth_call) to catch any other revert before spending gas
        try:
            self.contract.functions.requestBurn(*params).call({"from": self.dex.address})
            sim = "ok"
        except Exception as e:
            sim = f"SIMULATION REVERT: {str(e)[:200]}"
            if not dry_run:
                return {"error": sim, "aborted": True, "approve": approve_res}

        if dry_run:
            return {"dry_run": True, "amount_wprl": str(amount_wprl), "amount_raw": amount_raw,
                    "pearl_address": pearl_address, "simulation": sim, "approve": approve_res}

        fn = self.contract.functions.requestBurn(*params)
        with evm_send_lock():
            tx = fn.build_transaction({
                "from": self.dex.address,
                "nonce": self.w3.eth.get_transaction_count(self.dex.address, "pending"),
                "gas": 200_000,
                "maxFeePerGas": self.w3.eth.gas_price * 3 + self.w3.to_wei(1, "gwei"),
                "maxPriorityFeePerGas": min(self.w3.to_wei(1, "gwei"), self.w3.eth.gas_price),
                "chainId": self.w3.eth.chain_id,
                "value": 0,
            })
            signed = self.w3.eth.account.sign_transaction(tx, self.cfg.private_key)
            h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        r = self.w3.eth.wait_for_transaction_receipt(h, timeout=180)
        return {"tx_hash": h.hex(), "status": r.status, "gas_used": r.gasUsed,
                "amount_wprl": str(amount_wprl), "pearl_address": pearl_address,
                "approve": approve_res, "simulation": sim}
