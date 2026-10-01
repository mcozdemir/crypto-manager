# -*- coding: utf-8 -*-
"""
binance_account.py
------------------
"İşlemlerim" paneli için kullanıcının Binance hesabını okur ve yönetir:

  - Vadeli (USDT-M): açık pozisyonlar (giriş, işaret fiyatı, anlık K/Z, tasfiye),
    açık emirler, koşullu (Algo) hedef/stop emirleri, son 7 günün gerçekleşen K/Z'si
  - Spot: USDT bakiyesi, uygulamadan alınmış / açık emri olan varlıklar, açık emirler (OCO)
  - Müdahale: pozisyonu kapat (piyasa), emri iptal et, hedef/stop değiştir

Tüm müdahalelerde "korumasız kalmama" ilkesi: hedef/stop değiştirilirken yeni emir
kurulamazsa eski fiyatlarla geri kurulmaya çalışılır; o da olmazsa açıkça bildirilir.
"""

import logging
import time
from decimal import Decimal
from typing import Dict, List, Optional

from binance_client import (BinanceError, Client, D, PlanError, STOP_LIMIT_SLIPPAGE, current_price,
                            floor_step, fmt, round_tick, symbol_rules, _public_get)

logger = logging.getLogger("scanner.binance_account")

TP_TYPES = ("TAKE_PROFIT_MARKET", "TAKE_PROFIT")
SL_TYPES = ("STOP_MARKET", "STOP")
REALIZED_DAYS = 7
DUST_USDT = Decimal("1")

_realized_cache: Dict[tuple, tuple] = {}


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(a: Decimal, b: Decimal) -> Optional[float]:
    return round(float((a / b - 1) * 100), 2) if b else None


# --------------------------------------------------------------------------
# Okuma
# --------------------------------------------------------------------------
def _futures_positions(client: Client) -> List[Dict]:
    try:
        raw = client._signed("futures", "GET", "/fapi/v2/positionRisk")
    except BinanceError as exc:
        if exc.http != 404:
            raise
        raw = client._signed("futures", "GET", "/fapi/v3/positionRisk")
    out = []
    for p in raw:
        amt = D(p.get("positionAmt") or "0")
        if amt == 0:
            continue
        ps = (p.get("positionSide") or "BOTH").upper()
        direction = ps if ps in ("LONG", "SHORT") else ("LONG" if amt > 0 else "SHORT")
        entry, mark = D(p.get("entryPrice") or "0"), D(p.get("markPrice") or "0")
        upnl = D(p.get("unRealizedProfit") or "0")
        notional = abs(D(p.get("notional") or "0")) or abs(amt) * mark
        lev = int(D(p.get("leverage") or "0")) or None
        mtype = (p.get("marginType") or "").lower()
        iso_wallet = D(p.get("isolatedWallet") or "0")
        init_margin = D(p.get("initialMargin") or p.get("positionInitialMargin") or "0")
        if mtype == "isolated" and iso_wallet > 0:
            margin = iso_wallet
        elif init_margin > 0:
            margin = init_margin
        else:
            margin = notional / lev if lev else notional
        if not lev and init_margin > 0:
            lev = int(round(notional / init_margin))
        out.append({
            "symbol": p["symbol"], "direction": direction, "position_side": ps,
            "quantity": fmt(abs(amt)), "entry_price": fmt(entry), "mark_price": fmt(mark),
            "break_even": fmt(D(p["breakEvenPrice"])) if p.get("breakEvenPrice") else None,
            "liquidation": fmt(D(p.get("liquidationPrice") or "0")) if _f(p.get("liquidationPrice")) else None,
            "notional": fmt(round_tick(notional, D("0.01"))), "margin": fmt(round_tick(margin, D("0.01"))),
            "leverage": lev, "margin_type": "İzole" if mtype == "isolated" else ("Çapraz" if mtype else None),
            "pnl": fmt(round_tick(upnl, D("0.01"))),
            "pnl_pct": round(float(upnl / margin * 100), 2) if margin else None,
            "move_pct": _pct(mark, entry) if direction == "LONG" else (_pct(entry, mark) if mark else None),
            "updated": p.get("updateTime"),
            "tp": None, "sl": None,
        })
    return out


def _futures_orders(client: Client) -> List[Dict]:
    out = []
    for o in client._signed("futures", "GET", "/fapi/v1/openOrders"):
        out.append({
            "kind": "order", "id": o.get("orderId"), "symbol": o["symbol"], "side": o.get("side"),
            "position_side": o.get("positionSide"), "type": o.get("type"),
            "price": o.get("price"), "trigger": o.get("stopPrice") if _f(o.get("stopPrice")) else None,
            "quantity": o.get("origQty"), "filled": o.get("executedQty"),
            "reduce_only": bool(o.get("reduceOnly")), "close_position": bool(o.get("closePosition")),
            "time": o.get("time"),
        })
    return out


def _futures_algo(client: Client) -> List[Dict]:
    out = []
    for o in client._signed("futures", "GET", "/fapi/v1/openAlgoOrders"):
        out.append({
            "kind": "algo", "id": o.get("algoId"), "symbol": o["symbol"], "side": o.get("side"),
            "position_side": o.get("positionSide"), "type": o.get("orderType") or o.get("type"),
            "price": o.get("price") if _f(o.get("price")) else None, "trigger": o.get("triggerPrice"),
            "quantity": o.get("quantity") if _f(o.get("quantity")) else None,
            "close_position": str(o.get("closePosition")).lower() == "true" or not _f(o.get("quantity")),
            "status": o.get("algoStatus"), "time": o.get("createTime"),
        })
    return out


def _attach_tpsl(positions: List[Dict], conditional: List[Dict]):
    """Pozisyonlara ilgili kâr al / zarar kes emirlerini bağlar."""
    for p in positions:
        close_side = "SELL" if p["direction"] == "LONG" else "BUY"
        for o in conditional:
            if o["symbol"] != p["symbol"] or o["side"] != close_side:
                continue
            if p["position_side"] in ("LONG", "SHORT") and o.get("position_side") not in (p["position_side"],):
                continue
            slot = "tp" if o["type"] in TP_TYPES else ("sl" if o["type"] in SL_TYPES else None)
            if slot and p[slot] is None:
                trig = D(o["trigger"])
                mark = D(p["mark_price"])
                p[slot] = {"id": o["id"], "kind": o["kind"], "trigger": fmt(trig),
                           "distance_pct": _pct(trig, mark)}


def _realized(client: Client, user_key) -> Dict:
    key = (user_key, client.env)
    hit = _realized_cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    start = int((time.time() - REALIZED_DAYS * 86400) * 1000)
    rows = client._signed("futures", "GET", "/fapi/v1/income",
                          {"incomeType": "REALIZED_PNL", "startTime": start, "limit": 1000})
    items, total = [], D(0)
    by_symbol: Dict[str, Decimal] = {}
    for r in rows:
        v = D(r.get("income") or "0")
        total += v
        by_symbol[r["symbol"]] = by_symbol.get(r["symbol"], D(0)) + v
        items.append({"symbol": r["symbol"], "income": fmt(round_tick(v, D("0.0001"))), "time": r.get("time")})
    res = {"days": REALIZED_DAYS, "total": fmt(round_tick(total, D("0.01"))),
           "by_symbol": [{"symbol": s, "income": fmt(round_tick(v, D("0.01")))}
                         for s, v in sorted(by_symbol.items(), key=lambda x: x[1])],
           "items": sorted(items, key=lambda x: -(x["time"] or 0))[:50]}
    _realized_cache[key] = (time.time(), res)
    return res


def futures_snapshot(client: Client, user_key=None) -> Dict:
    positions = _futures_positions(client)
    orders = _futures_orders(client)
    algo = _futures_algo(client)
    _attach_tpsl(positions, algo + [o for o in orders if o["type"] in TP_TYPES + SL_TYPES])
    linked = {(p.get("tp") or {}).get("id") for p in positions} | {(p.get("sl") or {}).get("id") for p in positions}
    pos_syms = {p["symbol"] for p in positions}
    other = []
    for o in algo + orders:
        if o["id"] in linked:
            continue
        o["orphan"] = (o["symbol"] not in pos_syms) and (o.get("close_position") or o.get("reduce_only"))
        other.append(o)
    bal = client._signed("futures", "GET", "/fapi/v2/balance")
    usdt = next((b for b in bal if b.get("asset") == "USDT"), {})
    realized = None
    try:
        realized = _realized(client, user_key)
    except Exception as exc:  # noqa: BLE001
        logger.info("Gerçekleşen K/Z alınamadı: %s", exc)
    total_upnl = sum((D(p["pnl"]) for p in positions), D(0))
    return {
        "ok": True,
        "balance": fmt(round_tick(D(usdt.get("balance") or "0"), D("0.01"))),
        "available": fmt(round_tick(D(usdt.get("availableBalance") or "0"), D("0.01"))),
        "unrealized": fmt(round_tick(total_upnl, D("0.01"))),
        "positions": sorted(positions, key=lambda p: p["symbol"]),
        "orders": sorted(other, key=lambda o: -(o.get("time") or 0)),
        "realized": realized,
    }


def spot_snapshot(client: Client, app_entries: Dict[str, Decimal]) -> Dict:
    acc = client._signed("spot", "GET", "/api/v3/account", {"omitZeroBalances": "true"})
    orders_raw = client._signed("spot", "GET", "/api/v3/openOrders")
    prices = {t["symbol"]: D(t["price"]) for t in _public_get(client.env, "spot", "/api/v3/ticker/price")}

    orders = []
    for o in orders_raw:
        orders.append({
            "kind": "list" if int(o.get("orderListId", -1)) >= 0 else "order",
            "id": o.get("orderListId") if int(o.get("orderListId", -1)) >= 0 else o.get("orderId"),
            "order_id": o.get("orderId"), "symbol": o["symbol"], "side": o.get("side"),
            "type": o.get("type"), "price": o.get("price"),
            "trigger": o.get("stopPrice") if _f(o.get("stopPrice")) else None,
            "quantity": o.get("origQty"), "filled": o.get("executedQty"), "time": o.get("time"),
        })
    order_syms = {o["symbol"] for o in orders}

    usdt = D(0)
    holdings, others = [], 0
    for b in acc.get("balances", []):
        asset = b.get("asset")
        qty = D(b.get("free") or "0") + D(b.get("locked") or "0")
        if asset == "USDT":
            usdt = D(b.get("free") or "0")
            continue
        sym = f"{asset}USDT"
        price = prices.get(sym)
        if price is None or qty <= 0:
            continue
        value = qty * price
        if sym not in app_entries and sym not in order_syms:
            if value >= DUST_USDT:
                others += 1
            continue
        if value < DUST_USDT and sym not in order_syms:
            continue
        entry = app_entries.get(sym)
        sells = [o for o in orders if o["symbol"] == sym and o["side"] == "SELL"]
        tp = next((o for o in sells if o["type"] in ("LIMIT_MAKER", "LIMIT", "TAKE_PROFIT_LIMIT")), None)
        sl = next((o for o in sells if o["type"] in ("STOP_LOSS_LIMIT", "STOP_LOSS")), None)
        holdings.append({
            "symbol": sym, "asset": asset, "quantity": fmt(qty), "free": b.get("free"),
            "price": fmt(price), "value": fmt(round_tick(value, D("0.01"))),
            "entry_price": fmt(entry) if entry else None,
            "pnl": fmt(round_tick((price - entry) * qty, D("0.01"))) if entry else None,
            "pnl_pct": _pct(price, entry) if entry else None,
            "tp": {"trigger": tp["price"], "id": tp["id"], "kind": tp["kind"],
                   "distance_pct": _pct(D(tp["price"]), price)} if tp else None,
            "sl": {"trigger": sl["trigger"], "id": sl["id"], "kind": sl["kind"],
                   "distance_pct": _pct(D(sl["trigger"]), price)} if sl and sl["trigger"] else None,
        })
    linked = {(h.get("tp") or {}).get("id") for h in holdings} | {(h.get("sl") or {}).get("id") for h in holdings}
    return {
        "ok": True,
        "usdt": fmt(round_tick(usdt, D("0.01"))),
        "holdings": sorted(holdings, key=lambda h: h["symbol"]),
        "orders": [o for o in orders if not (o["kind"] == "list" and o["id"] in linked)],
        "other_assets": others,
    }


# --------------------------------------------------------------------------
# Müdahale
# --------------------------------------------------------------------------
def cancel(client: Client, market: str, kind: str, symbol: str, oid) -> Dict:
    symbol = symbol.upper()
    if market == "futures":
        if kind == "algo":
            return client._signed("futures", "DELETE", "/fapi/v1/algoOrder", {"algoId": int(oid)})
        return client._signed("futures", "DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": int(oid)})
    if kind == "list":
        return client._signed("spot", "DELETE", "/api/v3/orderList", {"symbol": symbol, "orderListId": int(oid)})
    return client._signed("spot", "DELETE", "/api/v3/order", {"symbol": symbol, "orderId": int(oid)})


def _find_futures_position(client: Client, symbol: str, position_side: str) -> Dict:
    for p in _futures_positions(client):
        if p["symbol"] == symbol and (position_side in ("", "BOTH") or p["position_side"] == position_side
                                      or p["direction"] == position_side):
            return p
    raise PlanError(f"{symbol} için açık vadeli pozisyon bulunamadı (kapanmış olabilir).")


def close_futures(client: Client, symbol: str, position_side: str = "BOTH") -> Dict:
    symbol = symbol.upper()
    p = _find_futures_position(client, symbol, position_side)
    hedge = p["position_side"] in ("LONG", "SHORT")
    side = "SELL" if p["direction"] == "LONG" else "BUY"
    params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": p["quantity"],
              "newOrderRespType": "RESULT"}
    if hedge:
        params["positionSide"] = p["position_side"]
    else:
        params["reduceOnly"] = "true"
    r = client._signed("futures", "POST", "/fapi/v1/order", params)
    steps = [{"name": f"Piyasa emriyle kapatıldı ({p['quantity']} {symbol})", "ok": True,
              "avg_price": r.get("avgPrice")}]
    # Pozisyon kapandıktan sonra kalan hedef/stop emirleri yeni pozisyonları etkilemesin
    for o in _futures_algo(client):
        if o["symbol"] == symbol and o["side"] == side and (not hedge or o.get("position_side") == p["position_side"]):
            try:
                cancel(client, "futures", "algo", symbol, o["id"])
                steps.append({"name": f"{o['type']} {o['trigger']} iptal edildi", "ok": True})
            except Exception as exc:  # noqa: BLE001
                steps.append({"name": f"{o['type']} {o['trigger']} iptal edilemedi", "ok": False, "error": str(exc)})
    pnl_est = (D(r.get("avgPrice") or p["mark_price"]) - D(p["entry_price"])) * D(p["quantity"]) * \
        (1 if p["direction"] == "LONG" else -1)
    return {"ok": True, "steps": steps, "avg_price": r.get("avgPrice"),
            "pnl_estimate": fmt(round_tick(pnl_est, D("0.01")))}


def close_spot(client: Client, symbol: str) -> Dict:
    symbol = symbol.upper()
    rules = symbol_rules(client.env, "spot", symbol)
    steps = []
    open_orders = client._signed("spot", "GET", "/api/v3/openOrders", {"symbol": symbol})
    if open_orders:
        client._signed("spot", "DELETE", "/api/v3/openOrders", {"symbol": symbol})
        steps.append({"name": f"{len(open_orders)} açık emir iptal edildi (hedef/stop dahil)", "ok": True})
    free = D(0)
    for _ in range(3):  # iptal sonrası bakiyenin serbest kalması
        acc = client._signed("spot", "GET", "/api/v3/account", {"omitZeroBalances": "true"})
        free = D(next((b.get("free") for b in acc.get("balances", []) if b.get("asset") == rules["base"]), "0"))
        if free > 0:
            break
        time.sleep(0.5)
    qty = floor_step(free, rules["market_step"])
    price = current_price(client.env, "spot", symbol)["price"]
    if qty <= 0 or (rules["min_notional"] and qty * price < rules["min_notional"]):
        raise PlanError(f"Satılabilecek {rules['base']} miktarı Binance'in en düşük emir tutarının altında "
                        f"({fmt(qty)} {rules['base']}).")
    r = client._signed("spot", "POST", "/api/v3/order", {
        "symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": fmt(qty), "newOrderRespType": "FULL"})
    filled = D(r.get("executedQty") or "0")
    avg = D(r.get("cummulativeQuoteQty") or "0") / filled if filled > 0 else price
    steps.append({"name": f"{fmt(filled)} {rules['base']} piyasa fiyatından satıldı", "ok": True,
                  "avg_price": fmt(round_tick(avg, rules["tick"]))})
    return {"ok": True, "steps": steps, "avg_price": fmt(round_tick(avg, rules["tick"])),
            "proceeds": fmt(round_tick(D(r.get("cummulativeQuoteQty") or "0"), D("0.01")))}


def _validate_levels(direction: str, price: Decimal, target: Optional[Decimal], stop: Optional[Decimal]):
    if direction == "LONG":
        if target is not None and target <= price:
            raise PlanError(f"LONG için hedef güncel fiyatın ({fmt(price)}) üstünde olmalı.")
        if stop is not None and stop >= price:
            raise PlanError(f"LONG için stop güncel fiyatın ({fmt(price)}) altında olmalı.")
    else:
        if target is not None and target >= price:
            raise PlanError(f"SHORT için hedef güncel fiyatın ({fmt(price)}) altında olmalı.")
        if stop is not None and stop <= price:
            raise PlanError(f"SHORT için stop güncel fiyatın ({fmt(price)}) üstünde olmalı.")


def _place_algo(client: Client, symbol, close_side, typ, trigger, position_side):
    return client._signed("futures", "POST", "/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL", "symbol": symbol, "side": close_side, "type": typ,
        "triggerPrice": fmt(trigger), "closePosition": "true", "workingType": "MARK_PRICE",
        "priceProtect": "true", "positionSide": position_side if position_side in ("LONG", "SHORT") else None,
    })


def edit_futures_tpsl(client: Client, symbol: str, position_side: str,
                      target: Optional[str], stop: Optional[str]) -> Dict:
    symbol = symbol.upper()
    p = _find_futures_position(client, symbol, position_side)
    rules = symbol_rules(client.env, "futures", symbol)
    tgt = round_tick(D(target), rules["tick"]) if target not in (None, "") else None
    stp = round_tick(D(stop), rules["tick"]) if stop not in (None, "") else None
    if tgt is None and stp is None:
        raise PlanError("Yeni hedef veya stop girin.")
    mark = current_price(client.env, "futures", symbol)["mark"]
    _validate_levels(p["direction"], mark, tgt, stp)
    if stp is not None and p.get("liquidation"):
        liq = D(p["liquidation"])
        if liq > 0 and ((p["direction"] == "LONG" and stp <= liq) or (p["direction"] == "SHORT" and stp >= liq)):
            raise PlanError(f"Stop tasfiye fiyatının ({fmt(liq)}) ötesinde olamaz.")
    close_side = "SELL" if p["direction"] == "LONG" else "BUY"
    algo = [o for o in _futures_algo(client) if o["symbol"] == symbol and o["side"] == close_side
            and (p["position_side"] not in ("LONG", "SHORT") or o.get("position_side") == p["position_side"])]
    steps, critical = [], []
    for label, new, types, typ in (("Kâr al", tgt, TP_TYPES, "TAKE_PROFIT_MARKET"),
                                   ("Zarar kes", stp, SL_TYPES, "STOP_MARKET")):
        if new is None:
            continue
        old = [o for o in algo if o["type"] in types]
        # Binance aynı yönde ikinci closePosition emrine izin vermez (-4130): önce eskiyi iptal et
        for o in old:
            cancel(client, "futures", "algo", symbol, o["id"])
        try:
            _place_algo(client, symbol, close_side, typ, new, p["position_side"])
            steps.append({"name": f"{label}: {', '.join(o['trigger'] for o in old) or 'yok'} → {fmt(new)}",
                          "ok": True})
        except Exception as exc:  # noqa: BLE001
            restored = False
            for o in old:
                try:
                    _place_algo(client, symbol, close_side, typ, D(o["trigger"]), p["position_side"])
                    restored = True
                except Exception:  # noqa: BLE001
                    pass
            steps.append({"name": f"{label} {fmt(new)} kurulamadı", "ok": False, "error": str(exc),
                          "note": "eski fiyat geri kuruldu" if restored else None})
            if old and not restored:
                critical.append(label)
    res = {"ok": all(s["ok"] for s in steps), "steps": steps}
    if critical:
        res["critical"] = (f"{', '.join(critical)} emri iptal edildi ancak yenisi kurulamadı. Pozisyon bu "
                           "yönde KORUMASIZ — panelden tekrar deneyin veya Binance'ten girin.")
    return res


def edit_spot_tpsl(client: Client, symbol: str, target: Optional[str], stop: Optional[str]) -> Dict:
    symbol = symbol.upper()
    rules = symbol_rules(client.env, "spot", symbol)
    price = current_price(client.env, "spot", symbol)["price"]
    orders = client._signed("spot", "GET", "/api/v3/openOrders", {"symbol": symbol})
    legs = [o for o in orders if o.get("side") == "SELL" and int(o.get("orderListId", -1)) >= 0]
    old_tp = next((o for o in legs if o.get("type") in ("LIMIT_MAKER", "LIMIT")), None)
    old_sl = next((o for o in legs if o.get("type") in ("STOP_LOSS_LIMIT", "STOP_LOSS")), None)
    if not (old_tp and old_sl):
        raise PlanError(f"{symbol} için düzenlenecek hedef/stop (OCO) emri bulunamadı.")
    tgt = round_tick(D(target), rules["tick"]) if target not in (None, "") else D(old_tp["price"])
    stp = round_tick(D(stop), rules["tick"]) if stop not in (None, "") else D(old_sl["stopPrice"])
    _validate_levels("LONG", price, tgt, stp)
    qty = floor_step(D(old_tp["origQty"]) - D(old_tp.get("executedQty") or "0"), rules["step"])

    def oco(t, s):
        return client._signed("spot", "POST", "/api/v3/orderList/oco", {
            "symbol": symbol, "side": "SELL", "quantity": fmt(qty),
            "aboveType": "LIMIT_MAKER", "abovePrice": fmt(t),
            "belowType": "STOP_LOSS_LIMIT", "belowStopPrice": fmt(s),
            "belowPrice": fmt(round_tick(s * (1 - STOP_LIMIT_SLIPPAGE), rules["tick"])), "belowTimeInForce": "GTC",
        })

    client._signed("spot", "DELETE", "/api/v3/orderList", {"symbol": symbol, "orderListId": int(old_tp["orderListId"])})
    steps = [{"name": "Eski OCO (hedef + stop) iptal edildi", "ok": True}]
    try:
        oco(tgt, stp)
        steps.append({"name": f"Yeni OCO: hedef {fmt(tgt)}, stop {fmt(stp)} ({fmt(qty)} {rules['base']})", "ok": True})
        return {"ok": True, "steps": steps}
    except Exception as exc:  # noqa: BLE001
        steps.append({"name": "Yeni OCO kurulamadı", "ok": False, "error": str(exc)})
        try:
            oco(D(old_tp["price"]), D(old_sl["stopPrice"]))
            steps.append({"name": "Eski hedef/stop geri kuruldu", "ok": True})
            return {"ok": False, "steps": steps}
        except Exception as exc2:  # noqa: BLE001
            steps.append({"name": "Eski hedef/stop geri kurulamadı", "ok": False, "error": str(exc2)})
            return {"ok": False, "steps": steps,
                    "critical": f"{symbol} hedef/stop emirleri iptal edildi ve yeniden kurulamadı. Varlık "
                                "KORUMASIZ — panelden tekrar deneyin veya Binance'ten girin."}
