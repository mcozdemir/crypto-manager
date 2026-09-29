# -*- coding: utf-8 -*-
"""
fundamentals.py
---------------
"Temel Analiz" güven endeksi: kriptonun bilanço benzeri verileri.

Bileşenler (önce "proje ne kadar sağlam" açısından 0-100 hesaplanır):
  - Şeffaflık         : whitepaper, GitHub deposu, web sitesi var mı
  - Geliştirme        : GitHub'daki son kod güncellemesi ne kadar yakın
  - Token ekonomisi   : dolaşımdaki arz / toplam arz, FDV / piyasa değeri
                        (ileride piyasaya çıkacak token = satış baskısı)
  - Olgunluk          : piyasa değeri sıralaması ve proje yaşı
  - DeFi metrikleri   : (varsa) kilitli varlık (TVL) trendi ve protokol geliri

Sonra puan sinyal YÖNÜNE çevrilir: sağlam temel LONG'u destekler; zayıf
temel (yüksek dilüsyon, terk edilmiş geliştirme, çok küçük/yeni proje)
SHORT'u destekler.

Veri kaynakları (ücretsiz):
  - CoinGecko  : /coins/markets (toplu), /coins/{id} (ayrıntı), /search
  - GitHub API : depo son güncelleme tarihi
  - DefiLlama  : /protocols (TVL), /overview/fees (gelir)

Sonuçlar Turso'daki coin_fundamentals tablosunda 24 saat önbelleklenir;
böylece her taramada tekrar istek atılmaz ve sunucu yeniden başlasa da
kaybolmaz. Ücretsiz API limitlerini aşmamak için her taramada en fazla
DETAIL_BUDGET coin için ayrıntı çekilir; kalanlar sonraki taramalarda
tamamlanır.

İsteğe bağlı: .env / Render ortamında APP_COINGECKO_API_KEY (ücretsiz
"Demo" anahtar) tanımlanırsa CoinGecko istekleri bu anahtarla yapılır.
"""

import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("scanner.fundamentals")

CG_URL = "https://api.coingecko.com/api/v3"
GH_URL = "https://api.github.com"
LLAMA_URL = "https://api.llama.fi"

CACHE_TTL_HOURS = 24
DETAIL_BUDGET = 12          # tarama başına en fazla bu kadar coin için ayrıntı isteği
MARKETS_PAGES = 4           # /coins/markets: 4 x 250 = ilk 1000 coin

WEIGHTS = {
    "transparency": 0.15,
    "development": 0.20,
    "tokenomics": 0.30,
    "maturity": 0.20,
    "defi": 0.15,
}
NAMES = {
    "transparency": "Şeffaflık",
    "development": "Geliştirme aktivitesi",
    "tokenomics": "Token ekonomisi",
    "maturity": "Olgunluk ve büyüklük",
    "defi": "DeFi metrikleri",
}

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "crypto-manager/1.0", "Accept": "application/json"})


def _cg_headers() -> Dict:
    key = os.environ.get("APP_COINGECKO_API_KEY", "").strip()
    return {"x-cg-demo-api-key": key} if key else {}


def _get(url: str, params: dict = None, headers: dict = None, timeout: float = 12.0):
    resp = _SESSION.get(url, params=params, headers=headers or {}, timeout=timeout)
    if resp.status_code == 429:
        raise RateLimited(url)
    resp.raise_for_status()
    return resp.json()


class RateLimited(Exception):
    pass


def base_asset(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def _num(v) -> Optional[float]:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _interp(x: float, xs: List[float], ys: List[float]) -> float:
    if x <= xs[0]:
        return ys[0]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            t = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
            return ys[i - 1] + t * (ys[i] - ys[i - 1])
    return ys[-1]


def _fmt_big(v: Optional[float]) -> str:
    if v is None:
        return "-"
    for div, unit in ((1e12, "trilyon"), (1e9, "milyar"), (1e6, "milyon")):
        if v >= div:
            return f"{v / div:.1f} {unit} $".replace(".", ",")
    return f"{v:,.0f} $".replace(",", ".")


# --------------------------------------------------------------------------
# Önbellek (Turso / yerel SQLite)
# --------------------------------------------------------------------------
_table_ready = False


def _ensure_table():
    global _table_ready
    if _table_ready:
        return
    from db import get_db
    get_db().execute("""
        CREATE TABLE IF NOT EXISTS coin_fundamentals (
            base TEXT PRIMARY KEY,
            cg_id TEXT,
            data_json TEXT NOT NULL,
            complete INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    """)
    _table_ready = True


def _load_cache(bases: List[str]) -> Dict[str, Dict]:
    if not bases:
        return {}
    from db import get_db
    _ensure_table()
    marks = ",".join("?" for _ in bases)
    rows = get_db().execute(
        f"SELECT base, data_json, complete, updated_at FROM coin_fundamentals WHERE base IN ({marks})", bases)
    out = {}
    for r in rows:
        try:
            data = json.loads(r["data_json"])
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(r["updated_at"])).total_seconds() / 3600
            data["_fresh"] = age_h < CACHE_TTL_HOURS
            data["_complete"] = bool(r["complete"])
            out[r["base"]] = data
        except Exception:  # noqa: BLE001
            continue
    return out


def _save_cache(items: Dict[str, Dict]):
    if not items:
        return
    from db import get_db
    _ensure_table()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stmts = []
    for base, data in items.items():
        clean = {k: v for k, v in data.items() if not k.startswith("_")}
        stmts.append(("""
            INSERT INTO coin_fundamentals (base, cg_id, data_json, complete, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(base) DO UPDATE SET cg_id=excluded.cg_id, data_json=excluded.data_json,
                complete=excluded.complete, updated_at=excluded.updated_at
        """, (base, clean.get("cg_id"), json.dumps(clean, ensure_ascii=False),
              1 if data.get("_complete") else 0, now)))
    get_db().execute_many(stmts)


# --------------------------------------------------------------------------
# Veri toplama
# --------------------------------------------------------------------------
def _fetch_markets() -> Dict[str, Dict]:
    """İlk 1000 coinin toplu piyasa verisi; sembol -> en büyük piyasa değerli coin."""
    by_symbol: Dict[str, Dict] = {}
    for page in range(1, MARKETS_PAGES + 1):
        rows = _get(CG_URL + "/coins/markets", {
            "vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": page,
        }, headers=_cg_headers())
        for c in rows:
            sym = (c.get("symbol") or "").upper()
            if sym and sym not in by_symbol:  # liste piyasa değerine göre sıralı: ilk eşleşme en büyük
                by_symbol[sym] = c
        if len(rows) < 250:
            break
        time.sleep(1.0)
    return by_symbol


def _search_id(base: str) -> Optional[str]:
    data = _get(CG_URL + "/search", {"query": base}, headers=_cg_headers())
    for c in data.get("coins", []):
        if (c.get("symbol") or "").upper() == base:
            return c.get("id")
    return None


def _from_market(base: str, m: Dict) -> Dict:
    return {
        "base": base,
        "cg_id": m.get("id"),
        "name": m.get("name"),
        "rank": m.get("market_cap_rank"),
        "market_cap": _num(m.get("market_cap")),
        "fdv": _num(m.get("fully_diluted_valuation")),
        "circulating": _num(m.get("circulating_supply")),
        "total_supply": _num(m.get("total_supply")),
        "max_supply": _num(m.get("max_supply")),
    }


def _fetch_detail(data: Dict) -> Dict:
    d = _get(CG_URL + f"/coins/{data['cg_id']}", {
        "localization": "false", "tickers": "false", "market_data": "true",
        "community_data": "false", "developer_data": "false", "sparkline": "false",
    }, headers=_cg_headers())
    links = d.get("links") or {}
    md = d.get("market_data") or {}
    data.update({
        "name": d.get("name") or data.get("name"),
        "rank": d.get("market_cap_rank") or data.get("rank"),
        "genesis_date": d.get("genesis_date"),
        "categories": [c for c in (d.get("categories") or []) if c][:6],
        "whitepaper": (links.get("whitepaper") or "").strip() or None,
        "homepage": next((u for u in (links.get("homepage") or []) if u), None),
        "github": next((u for u in ((links.get("repos_url") or {}).get("github") or []) if u), None),
    })
    if data.get("market_cap") is None:
        data["market_cap"] = _num((md.get("market_cap") or {}).get("usd"))
        data["fdv"] = _num((md.get("fully_diluted_valuation") or {}).get("usd"))
        data["circulating"] = _num(md.get("circulating_supply"))
        data["total_supply"] = _num(md.get("total_supply"))
        data["max_supply"] = _num(md.get("max_supply"))
    return data


_GH_RE = re.compile(r"github\.com/([^/?#]+)(?:/([^/?#]+))?", re.I)


def _fetch_github(data: Dict) -> Dict:
    m = _GH_RE.search(data.get("github") or "")
    if not m:
        return data
    owner, repo = m.group(1), (m.group(2) or "").removesuffix(".git")
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("APP_GITHUB_API_TOKEN", "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    if repo:
        r = _get(f"{GH_URL}/repos/{owner}/{repo}", headers=headers)
        data["gh_pushed_at"] = r.get("pushed_at")
        data["gh_archived"] = bool(r.get("archived"))
        data["gh_stars"] = r.get("stargazers_count")
    else:  # organizasyon sayfası: en son güncellenen depo
        rows = _get(f"{GH_URL}/orgs/{owner}/repos", {"sort": "pushed", "per_page": 1}, headers=headers)
        if rows:
            data["gh_pushed_at"] = rows[0].get("pushed_at")
            data["gh_archived"] = bool(rows[0].get("archived"))
            data["gh_stars"] = rows[0].get("stargazers_count")
    return data


_llama_cache: Dict = {"ts": 0.0, "protocols": None, "fees": None}


def _llama_tables():
    """DefiLlama protokol ve gelir tabloları (bellekte 6 saat önbellek)."""
    if _llama_cache["protocols"] is not None and time.time() - _llama_cache["ts"] < 6 * 3600:
        return _llama_cache["protocols"], _llama_cache["fees"]
    protocols = {}
    for p in _get(LLAMA_URL + "/protocols", timeout=25):
        gid = p.get("gecko_id")
        if gid and (gid not in protocols or (p.get("tvl") or 0) > (protocols[gid].get("tvl") or 0)):
            protocols[gid] = {"tvl": _num(p.get("tvl")), "change_7d": _num(p.get("change_7d")),
                              "name": p.get("name"), "id": str(p.get("id")), "slug": p.get("slug")}
    fees = {}
    try:
        ov = _get(LLAMA_URL + "/overview/fees", {"excludeTotalDataChart": "true",
                                                 "excludeTotalDataChartBreakdown": "true"}, timeout=25)
        for p in ov.get("protocols", []):
            rec = {"fees_30d": _num(p.get("total30d"))}
            for key in (p.get("gecko_id"), str(p.get("defillamaId") or ""), p.get("slug"), p.get("name")):
                if key:
                    fees[key] = rec
    except Exception as exc:  # noqa: BLE001
        logger.debug("DefiLlama gelir verisi alınamadı: %s", exc)
    _llama_cache.update({"ts": time.time(), "protocols": protocols, "fees": fees})
    return protocols, fees


def _attach_defi(data: Dict):
    try:
        protocols, fees = _llama_tables()
    except Exception as exc:  # noqa: BLE001
        logger.debug("DefiLlama alınamadı: %s", exc)
        return
    p = protocols.get(data.get("cg_id") or "")
    if not p:
        data["defi"] = None
        return
    f = fees.get(data["cg_id"]) or fees.get(p["id"]) or fees.get(p.get("slug") or "") or fees.get(p["name"]) or {}
    data["defi"] = {"tvl": p["tvl"], "tvl_change_7d": p["change_7d"], "fees_30d": f.get("fees_30d"),
                    "name": p["name"]}


def collect(symbols: List[str], budget: int = DETAIL_BUDGET) -> Dict[str, Dict]:
    """
    Verilen Binance sembolleri için temel verileri döndürür (baz varlık -> veri).
    Önbellekte taze olanlar doğrudan kullanılır.
    """
    bases = sorted({base_asset(s) for s in symbols})
    try:
        cache = _load_cache(bases)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Temel analiz önbelleği okunamadı: %s", exc)
        cache = {}

    result = {b: d for b, d in cache.items() if d.get("_fresh")}
    need = [b for b in bases if b not in result or not result[b].get("_complete")]
    if not need:
        return result

    updated: Dict[str, Dict] = {}
    markets = None
    stale_or_missing = [b for b in need if b not in result]
    if stale_or_missing:
        try:
            markets = _fetch_markets()
        except Exception as exc:  # noqa: BLE001
            logger.warning("CoinGecko piyasa listesi alınamadı: %s", exc)
            markets = {}
        for b in stale_or_missing:
            if b in markets:
                data = _from_market(b, markets[b])
                data["_complete"] = False
                result[b] = data
                updated[b] = data
            elif b in cache:  # eski de olsa önbellektekini kullan
                result[b] = cache[b]

    # Ayrıntı (whitepaper, GitHub, DeFi): bütçe kadar coin
    detail_targets = [b for b in need if not result.get(b, {}).get("_complete")][:budget]
    for b in detail_targets:
        data = result.get(b) or {"base": b}
        try:
            if not data.get("cg_id"):
                data["cg_id"] = _search_id(b)
                if not data["cg_id"]:
                    data["not_found"] = True
                    data["_complete"] = True
                    result[b] = updated[b] = data
                    continue
            _fetch_detail(data)
            try:
                _fetch_github(data)
            except Exception as exc:  # noqa: BLE001
                logger.debug("GitHub verisi alınamadı %s: %s", b, exc)
            _attach_defi(data)
            data["_complete"] = True
            result[b] = updated[b] = data
            time.sleep(0.7 if _cg_headers() else 2.5)
        except RateLimited:
            logger.warning("CoinGecko istek limiti doldu; kalan coinlerin ayrıntısı sonraki taramada alınacak.")
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Temel veri alınamadı %s: %s", b, exc)
            result.setdefault(b, data)

    try:
        _save_cache(updated)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Temel analiz önbelleğe yazılamadı: %s", exc)
    return result


# --------------------------------------------------------------------------
# Puanlama
# --------------------------------------------------------------------------
def _score_transparency(d: Dict) -> Dict:
    if not d.get("_complete"):
        return {"score": None, "note": "Ayrıntı sonraki taramada alınacak"}
    have = [("whitepaper", d.get("whitepaper")), ("GitHub", d.get("github")), ("web sitesi", d.get("homepage"))]
    score = 20 + (40 if d.get("whitepaper") else 0) + (30 if d.get("github") else 0) + (10 if d.get("homepage") else 0)
    present = [n for n, v in have if v]
    missing = [n for n, v in have if not v]
    note = ("Var: " + ", ".join(present)) if present else "Whitepaper, GitHub ve web sitesi bulunamadı"
    if present and missing:
        note += " · Yok: " + ", ".join(missing)
    return {"score": min(100, score), "note": note}


def _score_development(d: Dict) -> Dict:
    if not d.get("_complete"):
        return {"score": None, "note": "Ayrıntı sonraki taramada alınacak"}
    if not d.get("github"):
        return {"score": None, "note": "Açık kaynak depo bilgisi yok"}
    pushed = d.get("gh_pushed_at")
    if not pushed:
        return {"score": None, "note": "GitHub verisi alınamadı"}
    try:
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(pushed.replace("Z", "+00:00"))).days
    except ValueError:
        return {"score": None, "note": "GitHub tarihi okunamadı"}
    score = _interp(days, [7, 30, 90, 365, 730], [95, 80, 55, 30, 10])
    if d.get("gh_archived"):
        score = 5
    note = f"Son kod güncellemesi {days} gün önce" + (" (depo arşivlenmiş)" if d.get("gh_archived") else "")
    return {"score": round(score), "note": note}


def _score_tokenomics(d: Dict) -> Dict:
    parts, notes = [], []
    circ = d.get("circulating")
    supply_cap = d.get("max_supply") or d.get("total_supply")
    if circ and supply_cap and supply_cap > 0:
        ratio = min(1.0, circ / supply_cap)
        parts.append((0.5, _interp(ratio, [0.1, 0.3, 0.5, 0.8, 0.95], [10, 35, 60, 85, 95])))
        notes.append(f"dolaşımdaki arz %{ratio * 100:.0f}")
    mc, fdv = d.get("market_cap"), d.get("fdv")
    if mc and fdv and mc > 0:
        r = fdv / mc
        parts.append((0.5, _interp(r, [1.05, 1.5, 2, 4, 10], [95, 75, 60, 30, 5])))
        notes.append(f"FDV/piyasa değeri {r:.1f}x".replace(".", ","))
    if not parts:
        return {"score": None, "note": "Arz verisi yok"}
    score = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
    return {"score": round(score), "note": ", ".join(notes)}


def _score_maturity(d: Dict) -> Dict:
    parts, notes = [], []
    rank = d.get("rank")
    if rank:
        parts.append((0.7, _interp(rank, [10, 50, 100, 300, 1000], [95, 85, 70, 45, 20])))
        notes.append(f"piyasa değeri sırası #{rank} ({_fmt_big(d.get('market_cap'))})")
    gd = d.get("genesis_date")
    if gd:
        try:
            years = (datetime.now() - datetime.fromisoformat(gd)).days / 365.25
            parts.append((0.3, _interp(years, [0.5, 1, 3, 6], [15, 35, 70, 95])))
            notes.append(f"{years:.1f} yıllık".replace(".", ","))
        except ValueError:
            pass
    if not parts:
        return {"score": None, "note": "Sıralama bilgisi yok"}
    score = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
    return {"score": round(score), "note": ", ".join(notes)}


def _score_defi(d: Dict) -> Dict:
    defi = d.get("defi")
    if not defi:
        return {"score": None, "note": "DeFi protokolü değil ya da veri yok"}
    parts, notes = [], []
    if defi.get("tvl_change_7d") is not None:
        ch = defi["tvl_change_7d"]
        parts.append((0.6, 50 + 45 * math.tanh(ch / 15.0)))
        sign = "+" if ch > 0 else "−" if ch < 0 else ""
        notes.append(f"TVL {_fmt_big(defi.get('tvl'))}, 7 gün {sign}%{abs(ch):.1f}".replace(".", ","))
    if defi.get("fees_30d") is not None:
        fees = defi["fees_30d"]
        mc = d.get("market_cap")
        if mc:
            yearly_ratio = fees * 12 / mc  # yıllık ücret / piyasa değeri
            parts.append((0.4, _interp(yearly_ratio, [0.001, 0.01, 0.05, 0.15], [20, 45, 75, 95])))
        notes.append(f"30 günlük ücret geliri {_fmt_big(fees)}")
    if not parts:
        return {"score": None, "note": "DeFi verisi eksik"}
    score = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
    return {"score": round(score), "note": ", ".join(notes)}


def score(data: Optional[Dict], direction: str) -> Dict:
    """Temel analiz puanı (sinyal yönüne göre 0-100) ve alt kırılım."""
    if not data or data.get("not_found"):
        return {"score": None, "verdict": "veri yok", "components": [],
                "note": "Coin CoinGecko'da bulunamadı" if data else "Temel veri alınamadı"}
    comps = {
        "transparency": _score_transparency(data),
        "development": _score_development(data),
        "tokenomics": _score_tokenomics(data),
        "maturity": _score_maturity(data),
        "defi": _score_defi(data),
    }
    num = den = 0.0
    components = []
    for key, c in comps.items():
        s = c["score"]
        dir_s = None if s is None else (s if direction == "LONG" else 100 - s)
        if dir_s is not None:
            num += WEIGHTS[key] * dir_s
            den += WEIGHTS[key]
        components.append({"key": key, "name": NAMES[key], "score": dir_s, "quality": s,
                           "weight": WEIGHTS[key], "note": c["note"]})
    total = round(num / den) if den else None
    from confidence import verdict
    for comp in components:
        comp["verdict"] = verdict(comp["score"])
    quality = round(sum(WEIGHTS[c["key"]] * c["quality"] for c in components if c["quality"] is not None)
                    / den) if den else None
    return {
        "score": total,
        "verdict": verdict(total),
        "quality": quality,          # yönden bağımsız "proje sağlamlığı"
        "components": components,
        "name": data.get("name"),
        "categories": data.get("categories") or [],
        "links": {
            "coingecko": f"https://www.coingecko.com/en/coins/{data['cg_id']}" if data.get("cg_id") else None,
            "whitepaper": data.get("whitepaper"),
            "github": data.get("github"),
            "homepage": data.get("homepage"),
        },
        "partial": not data.get("_complete", True),
    }
