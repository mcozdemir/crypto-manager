# -*- coding: utf-8 -*-
"""
binance_client.py
-----------------
Kullanıcı adına Binance'te emir açan istemci ve emir planlayıcı.

Ortamlar:
  - demo : Binance Demo Trading (sahte bakiye). Spot https://demo-api.binance.com,
           Vadeli https://demo-fapi.binance.com. Anahtar demo.binance.com'dan alınır.
  - live : Gerçek hesap. Spot https://api.binance.com, Vadeli https://fapi.binance.com.

Piyasalar:
  - spot    : yalnızca LONG. Giriş piyasa emri (tutar kadar USDT ile alım) + ardından
              OCO satış (hedefte LIMIT_MAKER, stopta STOP_LOSS_LIMIT); ya da limit giriş
              için tek istekte OTOCO (giriş dolunca OCO otomatik kurulur).
  - futures : USDT-M vadeli, LONG ve SHORT. Marj tipi + kaldıraç ayarlanır, giriş
              emri (piyasa/limit) gönderilir, ardından hedef (TAKE_PROFIT_MARKET) ve
              stop (STOP_MARKET) Binance'in "algo order" uç noktasıyla
              pozisyonu kapatacak şekilde (closePosition=true) kurulur.

Güvenlik:
  - Canlı anahtarda çekim (withdraw) izni açıksa anahtar kabul edilmez.
  - Her emir önce `build_plan` ile doğrulanır ve kullanıcıya özet gösterilir; emir
    gönderilirken plan sunucuda güncel fiyatla YENİDEN hesaplanır.
"""

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.parse
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("scanner.binance_client")

URLS = {
    "live": {"spot": "https://api.binance.com", "futures": "https://fapi.binance.com"},
    "demo": {"spot": "https://demo-api.binance.com", "futures": "https://demo-fapi.binance.com"},
}
# Test ortamında sahte sunucuya yönlendirmek için: APP_BINANCE_URLS_JSON='{"demo": {...}}'
if os.environ.get("APP_BINANCE_URLS_JSON"):
    URLS.update(json.loads(os.environ["APP_BINANCE_URLS_JSON"]))

ENV_LABEL = {"demo": "DEMO (sahte bakiye)", "live": "CANLI (gerçek para)"}
MARKET_LABEL = {"spot": "Spot", "futures": "Vadeli (USDT-M)"}

SPOT_FEE = Decimal("0.001")        # %0,1 (tahmini, BNB indirimi hariç)
FUTURES_TAKER_FEE = Decimal("0.0005")  # %0,05 (tahmini)
MAINT_MARGIN_RATE = Decimal("0.005")   # tasfiye fiyatı tahmini için
STOP_LIMIT_SLIPPAGE = Decimal("0.003") # spot stop-limit emrinin limit fiyatı stop'un %0,3 ötesi
MAX_LEVERAGE = 20                  # ürün kuralı: en fazla 20x

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "crypto-manager/1.0"})


class BinanceError(Exception):
    def __init__(self, msg: str, code: Optional[int] = None, http: Optional[int] = None):
        super().__init__(msg)
        self.code = code
        self.http = http

    def __str__(self):
        base = super().__str__()
        return f"{base} (Binance kodu {self.code})" if self.code is not None else base


class PlanError(Exception):
    """Emir planı geçersiz (kullanıcıya gösterilecek anlaşılır mesaj)."""


# --------------------------------------------------------------------------
# Sayı yardımcıları (Binance filtrelerine uygun yuvarlama)
# --------------------------------------------------------------------------
def D(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def floor_step(value, step) -> Decimal:
    value, step = D(value), D(step)
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_tick(value, tick) -> Decimal:
    value, tick = D(value), D(tick)
    if tick <= 0:
        return value
    return (value / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


def fmt(value) -> str:
    """Binance'e gönderilecek düz sayı metni (bilimsel gösterim yok)."""
    s = format(D(value).normalize(), "f")
    return s if s not in ("-0", "") else "0"


# --------------------------------------------------------------------------
# Herkese açık veriler (anahtar gerekmez)
# --------------------------------------------------------------------------
_info_cache: Dict[Tuple[str, str], Tuple[float, Dict]] = {}
_info_lock = threading.Lock()


def _public_get(env: str, market: str, path: str, params: dict = None):
    url = URLS[env][market] + path
    resp = _SESSION.get(url, params=params, timeout=10)
    return _parse(resp)


def _parse(resp):
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code >= 400 or (isinstance(data, dict) and isinstance(data.get("code"), int)
                                   and data.get("code") < 0):
        msg = (data or {}).get("msg") if isinstance(data, dict) else resp.text[:200]
        code = (data or {}).get("code") if isinstance(data, dict) else None
        if resp.status_code == 451:
            msg = "Binance bu sunucu konumundan erişime izin vermiyor (HTTP 451)"
        raise BinanceError(msg or f"HTTP {resp.status_code}", code, resp.status_code)
    return data


def _nonzero(value, fallback) -> Decimal:
    v = D(value or "0")
    return v if v > 0 else D(fallback or "0")


def symbol_rules(env: str, market: str, symbol: str) -> Dict:
    """Sembolün işlem kuralları (fiyat adımı, miktar adımı, minimum tutar)."""
    key = (env + ":" + market, symbol)
    with _info_lock:
        hit = _info_cache.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
    path = "/api/v3/exchangeInfo" if market == "spot" else "/fapi/v1/exchangeInfo"
    params = {"symbol": symbol} if market == "spot" else None
    data = _public_get(env, market, path, params)
    info = next((s for s in data.get("symbols", []) if s.get("symbol") == symbol), None)
    if not info:
        raise PlanError(f"{symbol} {MARKET_LABEL[market]} piyasasında işlem görmüyor.")
    f = {x["filterType"]: x for x in info.get("filters", [])}
    lot = f.get("LOT_SIZE", {})
    mlot = f.get("MARKET_LOT_SIZE", {})
    notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
    rules = {
        "symbol": symbol,
        "status": info.get("status"),
        "base": info.get("baseAsset"),
        "quote": info.get("quoteAsset"),
        "tick": D(f.get("PRICE_FILTER", {}).get("tickSize", "0")),
        "step": D(lot.get("stepSize", "0")),
        "min_qty": D(lot.get("minQty", "0")),
        "max_qty": D(lot.get("maxQty", "0")),
        # Spot'ta MARKET_LOT_SIZE adımı çoğunlukla 0 gelir: o zaman LOT_SIZE adımı geçerlidir
        "market_step": _nonzero(mlot.get("stepSize"), lot.get("stepSize", "0")),
        "market_max_qty": _nonzero(mlot.get("maxQty"), lot.get("maxQty", "0")),
        "min_notional": D(notional.get("minNotional") or notional.get("notional") or "0"),
        "oco_allowed": bool(info.get("ocoAllowed", True)),
        "oto_allowed": bool(info.get("otoAllowed", True)),
        "contract": info.get("contractType"),
    }
    if market == "spot" and not info.get("isSpotTradingAllowed", True):
        raise PlanError(f"{symbol} için spot işlem kapalı.")
    if market == "futures" and rules["contract"] not in (None, "PERPETUAL"):
        raise PlanError(f"{symbol} süresiz (perpetual) vadeli sözleşme değil.")
    if rules["status"] != "TRADING":
        raise PlanError(f"{symbol} şu anda işleme kapalı ({rules['status']}).")
    with _info_lock:
        _info_cache[key] = (time.time(), rules)
    return rules


def current_price(env: str, market: str, symbol: str) -> Dict:
    if market == "spot":
        p = _public_get(env, "spot", "/api/v3/ticker/price", {"symbol": symbol})
        return {"price": D(p["price"]), "mark": None}
    p = _public_get(env, "futures", "/fapi/v1/premiumIndex", {"symbol": symbol})
    last = _public_get(env, "futures", "/fapi/v1/ticker/price", {"symbol": symbol})
    return {"price": D(last["price"]), "mark": D(p.get("markPrice") or last["price"]),
            "funding": D(p.get("lastFundingRate") or "0")}


# --------------------------------------------------------------------------
# İmzalı istemci
# --------------------------------------------------------------------------
class Client:
    def __init__(self, env: str, api_key: str, api_secret: str):
        if env not in URLS:
            raise ValueError("Geçersiz ortam")
        self.env = env
        self.api_key = api_key.strip()
        self.secret = api_secret.strip().encode("utf-8")
        self._offset = {"spot": 0, "futures": 0}

    def _signed(self, market: str, method: str, path: str, params: Optional[dict] = None,
                _retry: bool = True):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        params["timestamp"] = int(time.time() * 1000) + self._offset[market]
        params["recvWindow"] = 5000
        query = urllib.parse.urlencode(params)
        sig = hmac.new(self.secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
        url = f"{URLS[self.env][market]}{path}?{query}&signature={sig}"
        resp = _SESSION.request(method, url, headers={"X-MBX-APIKEY": self.api_key}, timeout=15)
        try:
            return _parse(resp)
        except BinanceError as exc:
            if exc.code == -1021 and _retry:  # saat farkı: sunucu saatine göre düzelt
                self._sync_time(market)
                return self._signed(market, method, path, params={k: v for k, v in params.items()
                                                                 if k not in ("timestamp", "recvWindow")},
                                    _retry=False)
            if exc.code in (-2014, -2015, -2008):
                raise BinanceError("API anahtarı geçersiz, IP kısıtlamasına takıldı veya bu işlem "
                                   "için izni yok", exc.code, exc.http) from exc
            raise

    def _sync_time(self, market: str):
        path = "/api/v3/time" if market == "spot" else "/fapi/v1/time"
        server = _public_get(self.env, market, path)["serverTime"]
        self._offset[market] = int(server) - int(time.time() * 1000)

    # --- doğrulama ------------------------------------------------------------
    def verify(self) -> Dict:
        """Anahtarı spot ve vadeli için dener; bakiye ve izinleri döndürür."""
        out: Dict = {"env": self.env, "checked_at": int(time.time())}
        try:
            acc = self._signed("spot", "GET", "/api/v3/account", {"omitZeroBalances": "true"})
            usdt = next((b for b in acc.get("balances", []) if b.get("asset") == "USDT"), {})
            out["spot"] = {"ok": True, "can_trade": bool(acc.get("canTrade")),
                           "usdt": float(usdt.get("free", 0) or 0)}
        except Exception as exc:  # noqa: BLE001
            out["spot"] = {"ok": False, "error": str(exc)}
        try:
            bal = self._signed("futures", "GET", "/fapi/v2/balance")
            usdt = next((b for b in bal if b.get("asset") == "USDT"), {})
            dual = self._signed("futures", "GET", "/fapi/v1/positionSide/dual")
            out["futures"] = {"ok": True, "usdt": float(usdt.get("availableBalance", 0) or 0),
                              "hedge_mode": bool(dual.get("dualSidePosition"))}
        except Exception as exc:  # noqa: BLE001
            out["futures"] = {"ok": False, "error": str(exc)}
        if self.env == "live":
            try:
                r = self._signed("spot", "GET", "/sapi/v1/account/apiRestrictions")
                out["restrictions"] = {
                    "withdrawals": bool(r.get("enableWithdrawals")),
                    "spot_trading": bool(r.get("enableSpotAndMarginTrading")),
                    "futures": bool(r.get("enableFutures")),
                    "ip_restricted": bool(r.get("ipRestrict")),
                }
            except Exception as exc:  # noqa: BLE001
                out["restrictions"] = {"error": str(exc)}
        return out

    def balance(self, market: str) -> Decimal:
        if market == "spot":
            acc = self._signed("spot", "GET", "/api/v3/account", {"omitZeroBalances": "true"})
            b = next((x for x in acc.get("balances", []) if x.get("asset") == "USDT"), {})
            return D(b.get("free", "0"))
        bal = self._signed("futures", "GET", "/fapi/v2/balance")
        b = next((x for x in bal if x.get("asset") == "USDT"), {})
        return D(b.get("availableBalance", "0"))

    def hedge_mode(self) -> bool:
        return bool(self._signed("futures", "GET", "/fapi/v1/positionSide/dual").get("dualSidePosition"))


# --------------------------------------------------------------------------
# Emir planı (önizleme / özet)
# --------------------------------------------------------------------------
def build_plan(client: Optional[Client], env: str, req: Dict) -> Dict:
    """
    Kullanıcının emir isteğini doğrular, Binance kurallarına göre yuvarlar ve
    gönderilecek emirlerin özetini üretir. `client` verilirse bakiye de kontrol edilir.
    """
    symbol = str(req.get("symbol", "")).upper().strip()
    direction = str(req.get("direction", "")).upper()
    market = str(req.get("market", "")).lower()
    entry_type = str(req.get("entry_type", "MARKET")).upper()
    if direction not in ("LONG", "SHORT"):
        raise PlanError("Yön LONG veya SHORT olmalı.")
    if market not in ("spot", "futures"):
        raise PlanError("Piyasa spot veya vadeli olmalı.")
    if market == "spot" and direction == "SHORT":
        raise PlanError("Spot piyasada SHORT açılamaz; Vadeli (USDT-M) seçin.")
    if entry_type not in ("MARKET", "LIMIT"):
        raise PlanError("Giriş emri piyasa veya limit olmalı.")
    try:
        amount = D(req.get("amount_usdt"))
        target = D(req.get("target"))
        stop = D(req.get("stop"))
        leverage = int(req.get("leverage") or 1) if market == "futures" else 1
    except Exception:  # noqa: BLE001
        raise PlanError("Tutar, hedef ve stop sayı olmalı.")
    if amount <= 0:
        raise PlanError("Yatırım tutarı sıfırdan büyük olmalı.")
    if not 1 <= leverage <= MAX_LEVERAGE:
        raise PlanError(f"Kaldıraç 1 ile {MAX_LEVERAGE} arasında olmalı.")
    margin_type = str(req.get("margin_type", "ISOLATED")).upper()
    if margin_type not in ("ISOLATED", "CROSSED"):
        raise PlanError("Marj tipi İzole veya Çapraz olmalı.")

    rules = symbol_rules(env, market, symbol)
    quote = current_price(env, market, symbol)
    now_price = quote["price"]

    if entry_type == "LIMIT":
        try:
            entry = round_tick(D(req.get("limit_price")), rules["tick"])
        except Exception:  # noqa: BLE001
            raise PlanError("Limit emir için giriş fiyatı girin.")
        if entry <= 0:
            raise PlanError("Limit giriş fiyatı sıfırdan büyük olmalı.")
    else:
        entry = now_price
    target = round_tick(target, rules["tick"])
    stop = round_tick(stop, rules["tick"])

    if direction == "LONG" and not (stop < entry < target):
        raise PlanError("LONG için stop < giriş fiyatı < hedef olmalı.")
    if direction == "SHORT" and not (target < entry < stop):
        raise PlanError("SHORT için hedef < giriş fiyatı < stop olmalı.")
    if entry_type == "MARKET":
        if direction == "LONG" and (now_price <= stop or now_price >= target):
            raise PlanError("Güncel fiyat hedef/stop aralığının dışında; sinyal bayatlamış olabilir.")
        if direction == "SHORT" and (now_price >= stop or now_price <= target):
            raise PlanError("Güncel fiyat hedef/stop aralığının dışında; sinyal bayatlamış olabilir.")

    notional = amount * leverage
    step = rules["market_step"] if entry_type == "MARKET" else rules["step"]
    qty = floor_step(notional / entry, step)
    if qty <= 0 or qty < rules["min_qty"]:
        min_q = max(rules["min_qty"], step)
        raise PlanError(f"Tutar çok düşük: en az {fmt(min_q)} {rules['base']} alınabilir "
                        f"(≈ {fmt(round_tick(min_q * entry, D('0.01')))} USDT pozisyon).")
    if rules["market_max_qty"] and entry_type == "MARKET" and qty > rules["market_max_qty"]:
        raise PlanError(f"Piyasa emri için en fazla {fmt(rules['market_max_qty'])} {rules['base']} "
                        "girilebilir; tutarı düşürün veya limit emir kullanın.")
    real_notional = qty * entry
    if rules["min_notional"] and real_notional < rules["min_notional"]:
        raise PlanError(f"Binance bu paritede en az {fmt(rules['min_notional'])} USDT'lik emir kabul "
                        f"ediyor; pozisyon büyüklüğü {fmt(round_tick(real_notional, D('0.01')))} USDT.")

    sign = 1 if direction == "LONG" else -1
    fee_rate = SPOT_FEE if market == "spot" else FUTURES_TAKER_FEE
    fees = real_notional * fee_rate * 2
    pnl_target = (target - entry) * qty * sign - fees
    pnl_stop = (stop - entry) * qty * sign - fees
    margin = real_notional / leverage
    rr = abs(target - entry) / abs(entry - stop)

    warnings: List[str] = []
    liq = None
    if market == "futures":
        mmr = MAINT_MARGIN_RATE
        liq = entry * (1 - D(1) / leverage + mmr) if direction == "LONG" else entry * (1 + D(1) / leverage - mmr)
        liq = round_tick(liq, rules["tick"])
        if (direction == "LONG" and stop <= liq) or (direction == "SHORT" and stop >= liq):
            raise PlanError(f"Stop ({fmt(stop)}) tahmini tasfiye fiyatının ({fmt(liq)}) ötesinde: "
                            "pozisyon stop'a ulaşmadan tasfiye olabilir. Kaldıracı düşürün.")
        if margin_type == "CROSSED":
            warnings.append("Çapraz marjda zarar tüm vadeli bakiyenizi etkileyebilir.")
        if leverage >= 10:
            warnings.append(f"{leverage}x kaldıraç: fiyatın %{float(100 / leverage):.1f} ters hareketi "
                            "marjın tamamını eritir.")
    if real_notional < notional * D("0.9"):
        warnings.append(f"Binance miktar adımı nedeniyle pozisyon {fmt(round_tick(real_notional, D('0.01')))} "
                        f"USDT'ye yuvarlandı (istenen {fmt(round_tick(notional, D('0.01')))} USDT).")
    if rr < 1:
        warnings.append(f"Risk/ödül 1:{float(rr):.2f} — potansiyel kazanç, potansiyel kayıptan küçük.")
    if entry_type == "LIMIT":
        dist = (entry / now_price - 1) * 100
        warnings.append(f"Limit emir güncel fiyattan %{float(dist):+.2f} uzakta; fiyat oraya gelmezse dolmaz.")

    balance = None
    if client is not None:
        balance = client.balance(market)
        if balance < margin + fees:
            raise PlanError(f"Yetersiz bakiye: {MARKET_LABEL[market]} hesabında kullanılabilir "
                            f"{fmt(round_tick(balance, D('0.01')))} USDT var, bu emir için yaklaşık "
                            f"{fmt(round_tick(margin + fees, D('0.01')))} USDT gerekiyor.")

    steps = _describe_steps(market, direction, entry_type, symbol, qty, entry, target, stop,
                            leverage, margin_type, amount)
    return {
        "env": env, "env_label": ENV_LABEL[env],
        "symbol": symbol, "base": rules["base"], "direction": direction,
        "market": market, "market_label": MARKET_LABEL[market],
        "entry_type": entry_type,
        "current_price": fmt(now_price),
        "mark_price": fmt(quote["mark"]) if quote.get("mark") is not None else None,
        "entry_price": fmt(entry), "target": fmt(target), "stop": fmt(stop),
        "quantity": fmt(qty), "notional": fmt(round_tick(real_notional, D("0.01"))),
        "amount_usdt": fmt(amount), "margin": fmt(round_tick(margin, D("0.01"))),
        "leverage": leverage, "margin_type": margin_type if market == "futures" else None,
        "est_fees": fmt(round_tick(fees, D("0.01"))),
        "pnl_target": fmt(round_tick(pnl_target, D("0.01"))),
        "pnl_stop": fmt(round_tick(pnl_stop, D("0.01"))),
        "pnl_target_pct": round(float(pnl_target / margin * 100), 2),
        "pnl_stop_pct": round(float(pnl_stop / margin * 100), 2),
        "rr": round(float(rr), 2),
        "liquidation": fmt(liq) if liq is not None else None,
        "balance": fmt(round_tick(balance, D("0.01"))) if balance is not None else None,
        "warnings": warnings,
        "steps": steps,
        "_raw": {"qty": qty, "entry": entry, "target": target, "stop": stop, "rules": rules,
                 "amount": amount},
    }


def _describe_steps(market, direction, entry_type, symbol, qty, entry, target, stop,
                    leverage, margin_type, amount) -> List[str]:
    if market == "spot":
        if entry_type == "MARKET":
            return [
                f"Piyasa emriyle {fmt(amount)} USDT tutarında {symbol} alımı",
                f"Alım dolunca OCO satış: hedef {fmt(target)} (limit), stop {fmt(stop)} (stop-limit)",
            ]
        return [
            f"{fmt(entry)} fiyatından {fmt(qty)} adet limit alım (OTOCO)",
            f"Alım dolunca otomatik OCO satış: hedef {fmt(target)}, stop {fmt(stop)}",
        ]
    side = "Alış (LONG)" if direction == "LONG" else "Satış (SHORT)"
    entry_txt = "piyasa fiyatından" if entry_type == "MARKET" else f"{fmt(entry)} limit fiyatından"
    return [
        f"Marj tipi {'İzole' if margin_type == 'ISOLATED' else 'Çapraz'}, kaldıraç {leverage}x olarak ayarlanır",
        f"{side}: {fmt(qty)} adet {symbol} {entry_txt}",
        f"Kâr al: fiyat {fmt(target)} olunca pozisyonun tamamı kapanır (TAKE_PROFIT_MARKET)",
        f"Zarar kes: fiyat {fmt(stop)} olunca pozisyonun tamamı kapanır (STOP_MARKET, işaret fiyatı)",
    ]


# --------------------------------------------------------------------------
# Emir gönderme
# --------------------------------------------------------------------------
def place(client: Client, plan: Dict) -> Dict:
    """Planı Binance'e gönderir. Her adımın sonucunu döndürür (hata olsa bile)."""
    raw = plan["_raw"]
    if plan["market"] == "spot":
        return _place_spot(client, plan, raw)
    return _place_futures(client, plan, raw)


def _place_spot(client: Client, plan: Dict, raw: Dict) -> Dict:
    rules, symbol = raw["rules"], plan["symbol"]
    target, stop = raw["target"], raw["stop"]
    stop_limit = round_tick(stop * (1 - STOP_LIMIT_SLIPPAGE), rules["tick"])
    result: Dict = {"steps": [], "ok": False, "protected": False}
    if plan["entry_type"] == "LIMIT":
        pending_qty = floor_step(raw["qty"] * (1 - SPOT_FEE), rules["step"])
        r = client._signed("spot", "POST", "/api/v3/orderList/otoco", {
            "symbol": symbol,
            "workingType": "LIMIT", "workingSide": "BUY",
            "workingPrice": fmt(raw["entry"]), "workingQuantity": fmt(raw["qty"]),
            "workingTimeInForce": "GTC",
            "pendingSide": "SELL", "pendingQuantity": fmt(pending_qty),
            "pendingAboveType": "LIMIT_MAKER", "pendingAbovePrice": fmt(target),
            "pendingBelowType": "STOP_LOSS_LIMIT", "pendingBelowStopPrice": fmt(stop),
            "pendingBelowPrice": fmt(stop_limit), "pendingBelowTimeInForce": "GTC",
        })
        result["steps"].append({"name": "OTOCO (limit alım + OCO satış)", "ok": True,
                                "id": r.get("orderListId"), "response": r})
        result.update(ok=True, protected=True, quantity=fmt(raw["qty"]))
        return result

    # Piyasa alımı
    r = client._signed("spot", "POST", "/api/v3/order", {
        "symbol": symbol, "side": "BUY", "type": "MARKET",
        "quoteOrderQty": fmt(floor_step(raw["amount"], D("0.01"))), "newOrderRespType": "FULL",
    })
    filled = D(r.get("executedQty", "0"))
    fee_base = sum((D(f.get("commission", "0")) for f in r.get("fills", [])
                    if f.get("commissionAsset") == rules["base"]), D(0))
    avg = (D(r.get("cummulativeQuoteQty", "0")) / filled) if filled > 0 else raw["entry"]
    result["steps"].append({"name": "Piyasa alımı", "ok": True, "id": r.get("orderId"),
                            "filled_qty": fmt(filled), "avg_price": fmt(round_tick(avg, rules["tick"])),
                            "response": r})
    result.update(ok=True, quantity=fmt(filled), avg_price=fmt(round_tick(avg, rules["tick"])))
    sell_qty = floor_step(filled - fee_base, rules["step"])
    try:
        oco = client._signed("spot", "POST", "/api/v3/orderList/oco", {
            "symbol": symbol, "side": "SELL", "quantity": fmt(sell_qty),
            "aboveType": "LIMIT_MAKER", "abovePrice": fmt(target),
            "belowType": "STOP_LOSS_LIMIT", "belowStopPrice": fmt(stop),
            "belowPrice": fmt(stop_limit), "belowTimeInForce": "GTC",
        })
        result["steps"].append({"name": "OCO satış (hedef + stop)", "ok": True,
                                "id": oco.get("orderListId"), "response": oco})
        result["protected"] = True
    except Exception as exc:  # noqa: BLE001
        result["steps"].append({"name": "OCO satış (hedef + stop)", "ok": False, "error": str(exc)})
        result["critical"] = ("Alım gerçekleşti ancak hedef/stop emirleri kurulamadı. Pozisyon "
                              "KORUMASIZ — Binance'ten hedef ve stop emirlerini elle girin.")
    return result


def _place_futures(client: Client, plan: Dict, raw: Dict) -> Dict:
    symbol, direction = plan["symbol"], plan["direction"]
    side = "BUY" if direction == "LONG" else "SELL"
    close_side = "SELL" if direction == "LONG" else "BUY"
    result: Dict = {"steps": [], "ok": False, "protected": False}

    hedge = client.hedge_mode()
    pos_side = direction if hedge else None

    try:
        client._signed("futures", "POST", "/fapi/v1/marginType",
                       {"symbol": symbol, "marginType": plan["margin_type"]})
        result["steps"].append({"name": f"Marj tipi: {plan['margin_type']}", "ok": True})
    except BinanceError as exc:
        if exc.code == -4046:  # zaten bu marj tipinde
            result["steps"].append({"name": f"Marj tipi: {plan['margin_type']}", "ok": True,
                                    "note": "zaten ayarlı"})
        else:
            raise BinanceError(f"Marj tipi ayarlanamadı: {exc}", exc.code) from exc
    lev = client._signed("futures", "POST", "/fapi/v1/leverage",
                         {"symbol": symbol, "leverage": plan["leverage"]})
    result["steps"].append({"name": f"Kaldıraç: {lev.get('leverage')}x", "ok": True})

    params = {"symbol": symbol, "side": side, "type": plan["entry_type"],
              "quantity": fmt(raw["qty"]), "positionSide": pos_side, "newOrderRespType": "RESULT"}
    if plan["entry_type"] == "LIMIT":
        params.update(price=fmt(raw["entry"]), timeInForce="GTC")
    r = client._signed("futures", "POST", "/fapi/v1/order", params)
    result["steps"].append({"name": "Giriş emri", "ok": True, "id": r.get("orderId"),
                            "status": r.get("status"), "avg_price": r.get("avgPrice"),
                            "response": r})
    result.update(ok=True, quantity=r.get("executedQty") or fmt(raw["qty"]),
                  avg_price=r.get("avgPrice"))

    failures = []
    for name, typ, trigger in (("Zarar kes (STOP_MARKET)", "STOP_MARKET", raw["stop"]),
                               ("Kâr al (TAKE_PROFIT_MARKET)", "TAKE_PROFIT_MARKET", raw["target"])):
        try:
            a = client._signed("futures", "POST", "/fapi/v1/algoOrder", {
                "algoType": "CONDITIONAL", "symbol": symbol, "side": close_side,
                "type": typ, "triggerPrice": fmt(trigger), "closePosition": "true",
                "workingType": "MARK_PRICE", "priceProtect": "true", "positionSide": pos_side,
            })
            result["steps"].append({"name": name, "ok": True, "id": a.get("algoId"), "response": a})
        except Exception as exc:  # noqa: BLE001
            failures.append(name)
            result["steps"].append({"name": name, "ok": False, "error": str(exc)})
    result["protected"] = not failures
    if failures:
        result["critical"] = ("Giriş emri gönderildi ancak şu koruma emirleri kurulamadı: "
                              + ", ".join(failures) + ". Binance'ten elle girin.")
    return result
