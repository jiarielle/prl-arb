"""SafeTrade REST client (Peatio API).

Auth: HMAC-SHA256(secret, nonce + apikey) hex, sent as X-Auth-Signature.
"""
import hmac
import hashlib
import time
import requests  # kept only for requests.HTTPError exception type
from curl_cffi import requests as cr  # browser-fingerprint transport (Cloudflare JA3 gate)
from decimal import Decimal


class SafeTradeClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str):
        import threading
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.base_url = base_url.rstrip("/")
        # SafeTrade sits behind Cloudflare, which blocks plain requests/curl by TLS (JA3)
        # fingerprint with a 403 "Attention Required" page (NOT an IP block — verified: AWS
        # IP direct gets 200 with a browser fingerprint). curl_cffi impersonates Chrome's
        # TLS handshake so the edge lets us through. EVM/web3 path is unaffected (direct).
        self.session = cr.Session(impersonate="chrome")
        # nonce must be unique; concurrent authed calls (parallel snapshot fetch) can land
        # in the same millisecond -> collision. Lock + monotonic bump keeps each unique.
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0

    def _auth_headers(self) -> dict:
        with self._nonce_lock:
            n = int(time.time() * 1000)
            if n <= self._last_nonce:
                n = self._last_nonce + 1
            self._last_nonce = n
        nonce = str(n)
        msg = (nonce + self.api_key).encode()
        sig = hmac.new(self.api_secret, msg, hashlib.sha256).hexdigest()
        return {
            "X-Auth-Apikey": self.api_key,
            "X-Auth-Nonce": nonce,
            "X-Auth-Signature": sig,
        }

    def _get(self, path: str, params=None, auth=False) -> dict:
        # Retry transient 403 (Cloudflare/rate-limit) and 5xx; fresh nonce each try.
        import time as _t
        last = None
        for attempt in range(3):
            h = self._auth_headers() if auth else {}
            r = self.session.get(self.base_url + path, params=params, headers=h, timeout=15)
            if r.ok:
                return r.json()
            last = r
            if r.status_code in (403, 429, 500, 502, 503, 504) and attempt < 2:
                _t.sleep(1.5 * (attempt + 1))
                continue
            break
        last.raise_for_status()

    def _post(self, path: str, data=None, auth=True) -> dict:
        # SafeTrade expects JSON bodies (Content-Type application/json), not form data.
        h = self._auth_headers() if auth else {}
        h["Content-Type"] = "application/json;charset=utf-8"
        r = self.session.post(self.base_url + path, json=data, headers=h, timeout=15)
        if not r.ok:
            # surface the API's rejection reason instead of a bare status code
            raise requests.HTTPError(f"{r.status_code} {r.text[:300]} (sent: {data})", response=r)
        return r.json()

    # ---------- public ----------
    def ticker(self, market: str) -> dict:
        """Latest ticker. Returns {at, ticker: {last, low, high, vol, ...}}."""
        return self._get(f"/peatio/public/markets/{market}/tickers")

    def depth(self, market: str, limit: int = 20) -> dict:
        """Order book. {asks: [[price, amount], ...], bids: [...], timestamp}."""
        return self._get(f"/peatio/public/markets/{market}/depth", params={"limit": limit})

    def best_bid_ask(self, market: str) -> tuple[Decimal, Decimal]:
        d = self.depth(market, limit=5)
        bid = Decimal(str(d["bids"][0][0])) if d.get("bids") else None
        ask = Decimal(str(d["asks"][0][0])) if d.get("asks") else None
        return bid, ask

    def simulate_market_buy(self, market: str, base_qty: Decimal, depth_limit: int = 50) -> tuple[Decimal, Decimal]:
        """Walk asks to fill base_qty PRL. Returns (effective_avg_price, qty_fillable)."""
        d = self.depth(market, limit=depth_limit)
        asks = sorted([(Decimal(str(p)), Decimal(str(a))) for p, a in d.get("asks", [])])
        filled = Decimal(0)
        spent = Decimal(0)
        for price, avail in asks:
            need = base_qty - filled
            if need <= 0:
                break
            take = min(need, avail)
            spent += take * price
            filled += take
        if filled <= 0:
            return Decimal(0), Decimal(0)
        return spent / filled, filled

    def simulate_market_sell(self, market: str, base_qty: Decimal, depth_limit: int = 50) -> tuple[Decimal, Decimal]:
        """Walk bids to dump base_qty PRL. Returns (effective_avg_price, qty_fillable)."""
        d = self.depth(market, limit=depth_limit)
        bids = sorted([(Decimal(str(p)), Decimal(str(a))) for p, a in d.get("bids", [])], reverse=True)
        filled = Decimal(0)
        proceeds = Decimal(0)
        for price, avail in bids:
            need = base_qty - filled
            if need <= 0:
                break
            take = min(need, avail)
            proceeds += take * price
            filled += take
        if filled <= 0:
            return Decimal(0), Decimal(0)
        return proceeds / filled, filled

    # ---------- private ----------
    # NOTE: SafeTrade private endpoints use the /trade/ prefix, NOT /peatio/.
    # Only public market data lives under /peatio/public/.
    def balances(self) -> list[dict]:
        """[{currency, balance, locked}, ...]"""
        return self._get("/trade/account/balances", auth=True)

    def balance(self, currency: str) -> Decimal:
        for b in self.balances():
            if b["currency"].lower() == currency.lower():
                return Decimal(str(b["balance"]))
        return Decimal(0)

    def place_marketable_limit(self, market: str, side: str, volume: Decimal,
                               ref_price: Decimal, cross_bps: int = 500,
                               amount_precision: int = 4, price_precision: int = 2) -> dict:
        """SafeTrade has no 'market' ord_type (422 market.order.type_doesnt_exist),
        so we emulate it with an aggressive LIMIT that crosses the spread and fills
        immediately at resting maker prices.
          buy : price = ref_price * (1 + cross_bps)  -> fills against asks at THEIR price
          sell: price = ref_price * (1 - cross_bps)  -> fills against bids at THEIR price
        Crossing far doesn't overpay: a limit buy executes at the ask, not at our cap.
        """
        from decimal import ROUND_DOWN, ROUND_UP
        aq = Decimal(10) ** (-amount_precision)
        pq = Decimal(10) ** (-price_precision)
        vol_q = Decimal(volume).quantize(aq, rounding=ROUND_DOWN)
        cross = Decimal(cross_bps) / Decimal(10000)
        # Quantize AWAY from the touch, or the cross evaporates: at price_precision=2 and
        # price <$1.25 one tick (0.01) is >80bps, so a buy at ask*(1+0.008) rounded DOWN
        # lands exactly back on the ask — a zero-cross limit that misses in a moving book
        # (a reverse buy once filled only ~12%, leaving most of the leg naked). Buys round UP,
        # sells round DOWN, so the placed price always clears at least the intended cross.
        # cross_bps=0 (main leg) is unaffected: book prices are already on-tick.
        if side == "buy":
            price = (ref_price * (Decimal(1) + cross)).quantize(pq, rounding=ROUND_UP)
        else:
            price = (ref_price * (Decimal(1) - cross)).quantize(pq, rounding=ROUND_DOWN)
        # SafeTrade field names: amount (not volume), type (not ord_type)
        data = {
            "market": market,
            "side": side,
            "type": "limit",
            "amount": format(vol_q, "f"),
            "price": format(price, "f"),
        }
        return self._post("/trade/market/orders", data=data, auth=True)

    # back-compat shim: callers used to call place_market_order
    def place_market_order(self, market: str, side: str, volume: Decimal,
                           amount_precision: int = 4) -> dict:
        bid, ask = self.best_bid_ask(market)
        ref = ask if side == "buy" else bid
        return self.place_marketable_limit(market, side, volume, ref,
                                           amount_precision=amount_precision)

    def cancel_order(self, order_id) -> dict:
        return self._post(f"/trade/market/orders/{order_id}/cancel", auth=True)

    def order(self, order_id: str) -> dict:
        return self._get(f"/trade/market/orders/{order_id}", auth=True)

    def open_orders(self, market: str = None, limit: int = 100) -> list[dict]:
        params = {"limit": limit, "state": "wait"}
        if market:
            params["market"] = market
        return self._get("/trade/market/orders", params=params, auth=True)

    def deposits(self, currency: str = None, limit: int = 25) -> list[dict]:
        """Deposit history. Each: {id, currency, amount, fee, credited(bool),
        status(submitted/accepted/...), txid, from_address, completed_at}.
        credited=True means the funds are usable (tradeable). Use this to detect
        bridge arrivals before acting on them."""
        params = {"limit": limit}
        if currency:
            params["currency"] = currency.lower()
        return self._get("/trade/account/deposits", params=params, auth=True)

    def pending_deposit(self, currency: str) -> dict | None:
        """Most recent not-yet-credited deposit of `currency`, or None."""
        for d in self.deposits(currency=currency, limit=10):
            if not d.get("credited"):
                return d
        return None

    def withdraws(self, currency: str = None, limit: int = 25) -> list[dict]:
        params = {"limit": limit}
        if currency:
            params["currency"] = currency.lower()
        return self._get("/trade/account/withdraws", params=params, auth=True)

    def beneficiaries(self) -> list[dict]:
        return self._get("/trade/account/beneficiaries", auth=True)

    def my_trades(self, market: str, limit: int = 50) -> list[dict]:
        """Recent OWN trades. Each: {price, amount, total(=quote spent/received), side,
        order_id, fee, fee_currency, ...}. `total` is the EXACT executed quote (USDT) for
        the fill — used to compute realized PnL from execution records, not balances."""
        return self._get("/trade/market/trades", params={"market": market, "limit": limit}, auth=True)

    def create_withdraw(self, currency: str, amount, beneficiary_id: int, otp: str = None) -> dict:
        """Create an on-chain withdrawal to a WHITELISTED beneficiary ONLY.
        Direct-address (rid) withdrawal is deliberately NOT supported — it bypasses the
        beneficiary whitelist, which is the opsec guard limiting blast radius if the API
        key leaks. If a needed beneficiary isn't whitelisted yet, wait for the user to
        add it; do not work around with rid."""
        data = {"currency": currency.lower(), "amount": str(amount), "beneficiary_id": beneficiary_id}
        if otp:
            data["otp"] = otp
        return self._post("/trade/account/withdraws", data=data, auth=True)
