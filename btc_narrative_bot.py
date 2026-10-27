#!/usr/bin/env python3
"""
BTC Narrative Bot — Professional Bitcoin News & Market Narratives (Upgraded)
===========================================================================
Fokus: menghasilkan narasi yang actionable untuk mendukung keputusan trading.

Sumber:
- cryptocurrency.cv (free) → berita BTC, breaking, whale, sentiment
- CoinGecko public API → harga, market cap, volume, dominance
- alternative.me → Fear & Greed

Fitur:
- /start, /help
- /narrative        → Bias + drivers + counter + impact ke sinyal (INTI)
- /briefing         → Narasi lengkap + scoring
- /news [n]         → Headline terfilter + relevance score
- /price            → Harga + market snapshot
- /whales           → Whale / institutional moves (lebih pintar)
- /sentiment        → Fear & Greed + interpretasi
- /subscribe        → Briefing otomatis (pagi & sore)
- /unsubscribe

Deploy: Railway / VPS
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ====================== CONFIG ======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "btc_narrative.db")

NEWS_BASE = "https://cryptocurrency.cv/api"
CG_BASE = "https://api.coingecko.com/api/v3"

MORNING_HOUR_UTC = 1   # ~08:00 WIB
EVENING_HOUR_UTC = 11  # ~18:00 WIB

REQUEST_TIMEOUT = 15
# ====================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("BTCNarrative")


# -------------------- Keyword Scoring --------------------

BULLISH_KEYWORDS = {
    "etf inflow": 3.0, "etf inflows": 3.0, "spot etf": 2.5,
    "blackrock": 2.0, "fidelity": 1.8, "ark invest": 1.5, "ibit": 2.0,
    "microstrategy": 2.2, "saylor": 2.0, "strategy buys": 2.5,
    "accumulation": 2.0, "whale buys": 2.0, "whale accumulation": 2.5,
    "exchange outflow": 2.3, "outflow": 1.5, "withdraw": 1.3,
    "miner accumulation": 1.8, "hashrate high": 1.2,
    "institutional": 1.8, "adoption": 1.5, "sovereign": 1.7,
    "bullish": 1.2, "all-time high": 1.5, "ath": 1.3,
    "breakout": 1.4, "rally": 1.3, "surge": 1.2,
    "approval": 1.5, "clarity": 1.3, "positive": 0.8,
}

BEARISH_KEYWORDS = {
    "etf outflow": -3.0, "etf outflows": -3.0, "outflows": -1.8,
    "selling pressure": -2.0, "whale sells": -2.2, "distribution": -2.0,
    "exchange inflow": -2.3, "inflow to exchange": -2.3,
    "miner selling": -2.0, "miner dump": -2.2,
    "hack": -2.5, "exploit": -2.0, "lawsuit": -1.8, "sec": -1.5,
    "ban": -2.5, "crackdown": -2.2, "regulation risk": -1.8,
    "bearish": -1.2, "crash": -2.0, "plunge": -1.8, "dump": -1.5,
    "liquidation": -1.7, "fear": -1.0, "capitulation": -1.8,
    "rejection": -1.3, "resistance": -0.8,
}

NOISE_KEYWORDS = [
    "xrp", "ripple", "solana", "sol ", "dogecoin", "doge", "shiba",
    "pepe", "meme coin", "memecoin", "apple", "iphone", "foldable",
    "nfl", "nba", "football", "soccer", "49ers", "kalshi", "prediction market",
    "c2fo", "payment delays", "sports", "celebrity",
]

WHALE_KEYWORDS = [
    "whale", "whales", "large transfer", "large transaction",
    "institutional", "blackrock", "fidelity", "microstrategy", "saylor",
    "etf", "ibit", "ark", "grayscale", "miner", "exchange inflow",
    "exchange outflow", "accumulation", "distribution", "otc",
]


def score_article(title: str, summary: str = "") -> Tuple[float, str]:
    text = f"{title} {summary}".lower()
    score = 0.0

    for noise in NOISE_KEYWORDS:
        if noise in text:
            return -99.0, "noise"

    for kw, weight in BULLISH_KEYWORDS.items():
        if kw in text:
            score += weight

    for kw, weight in BEARISH_KEYWORDS.items():
        if kw in text:
            score += weight

    if any(k in text for k in ["bitcoin", "btc", "satoshi"]):
        score += 0.4

    if score >= 2.5:
        label = "strong_bull"
    elif score >= 1.0:
        label = "bull"
    elif score <= -2.5:
        label = "strong_bear"
    elif score <= -1.0:
        label = "bear"
    else:
        label = "neutral"

    return score, label


def is_whale_related(title: str) -> bool:
    text = title.lower()
    return any(k in text for k in WHALE_KEYWORDS)


# -------------------- Database --------------------

def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id     INTEGER PRIMARY KEY,
                subscribed  INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def add_subscriber(chat_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO subscribers (chat_id, subscribed) VALUES (?, 1)",
            (chat_id,),
        )
        conn.commit()


def remove_subscriber(chat_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE subscribers SET subscribed = 0 WHERE chat_id = ?",
            (chat_id,),
        )
        conn.commit()


def get_subscribers() -> List[int]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT chat_id FROM subscribers WHERE subscribed = 1"
        ).fetchall()
    return [r[0] for r in rows]


# -------------------- Data Fetchers --------------------

async def fetch_json(session: aiohttp.ClientSession, url: str) -> Optional[Any]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
            if resp.status != 200:
                logger.warning("HTTP %s → %s", resp.status, url)
                return None
            return await resp.json()
    except Exception as e:
        logger.error("Fetch error %s: %s", url, e)
        return None


async def get_btc_price(session: aiohttp.ClientSession) -> Dict[str, Any]:
    url = (
        f"{CG_BASE}/simple/price"
        "?ids=bitcoin&vs_currencies=usd&include_market_cap=true"
        "&include_24hr_vol=true&include_24hr_change=true&include_last_updated_at=true"
    )
    data = await fetch_json(session, url)
    if not data or "bitcoin" not in data:
        return {}
    btc = data["bitcoin"]
    return {
        "price": btc.get("usd"),
        "market_cap": btc.get("usd_market_cap"),
        "volume_24h": btc.get("usd_24h_vol"),
        "change_24h": btc.get("usd_24h_change"),
        "updated": btc.get("last_updated_at"),
    }


async def get_global_data(session: aiohttp.ClientSession) -> Dict[str, Any]:
    data = await fetch_json(session, f"{CG_BASE}/global")
    if not data or "data" not in data:
        return {}
    d = data["data"]
    return {
        "btc_dominance": d.get("market_cap_percentage", {}).get("btc"),
        "total_market_cap": d.get("total_market_cap", {}).get("usd"),
        "total_volume": d.get("total_volume", {}).get("usd"),
        "market_cap_change_24h": d.get("market_cap_change_percentage_24h_usd"),
    }


async def get_raw_news(session: aiohttp.ClientSession, limit: int = 15) -> List[Dict]:
    data = await fetch_json(session, f"{NEWS_BASE}/bitcoin?limit={limit}")
    if not data:
        data = await fetch_json(session, f"{NEWS_BASE}/breaking?limit={limit}")
    if not data:
        return []
    articles = data.get("articles") if isinstance(data, dict) else data
    if not isinstance(articles, list):
        return []
    return articles[:limit]


async def get_scored_news(
    session: aiohttp.ClientSession, limit: int = 8, min_score: float = -5.0
) -> List[Dict]:
    raw = await get_raw_news(session, limit=18)
    scored = []
    for art in raw:
        title = (art.get("title") or art.get("headline") or "").strip()
        if not title:
            continue
        summary = art.get("summary") or art.get("description") or ""
        score, label = score_article(title, summary)
        if score <= -50:
            continue
        if score < min_score:
            continue
        art["_score"] = score
        art["_label"] = label
        scored.append(art)

    scored.sort(key=lambda x: (abs(x["_score"]), x["_score"]), reverse=True)
    return scored[:limit]


async def get_fear_greed(session: aiohttp.ClientSession) -> Optional[Dict]:
    data = await fetch_json(session, f"{NEWS_BASE}/fear-greed")
    if data and isinstance(data, dict) and data.get("value"):
        return data
    data = await fetch_json(session, "https://api.alternative.me/fng/?limit=1")
    if data and "data" in data and data["data"]:
        item = data["data"][0]
        return {
            "value": item.get("value"),
            "classification": item.get("value_classification"),
            "timestamp": item.get("timestamp"),
        }
    return None


# -------------------- Formatting Helpers --------------------

def fmt_usd(v: Optional[float], decimals: int = 0) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1_000_000_000:
        return f"${v/1_000_000_000:.2f}B"
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v:,.{decimals}f}"
    return f"${v:.{decimals}f}"


def fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def clean_title(title: str) -> str:
    return (title or "").strip()


def label_emoji(label: str) -> str:
    return {
        "strong_bull": "🟢🟢",
        "bull": "🟢",
        "neutral": "⚪",
        "bear": "🔴",
        "strong_bear": "🔴🔴",
    }.get(label, "⚪")


# -------------------- Narrative Engine --------------------

def compute_bias(
    price: Dict,
    fg: Optional[Dict],
    news: List[Dict],
) -> Dict[str, Any]:
    score = 0.0
    drivers_bull: List[str] = []
    drivers_bear: List[str] = []

    change = price.get("change_24h") or 0
    if change > 4:
        score += 1.8
        drivers_bull.append(f"Harga +{change:.1f}% (momentum kuat 24j)")
    elif change > 1.5:
        score += 0.9
        drivers_bull.append(f"Harga +{change:.1f}% (positif)")
    elif change < -4:
        score -= 1.8
        drivers_bear.append(f"Harga {change:.1f}% (tekanan jual signifikan)")
    elif change < -1.5:
        score -= 0.9
        drivers_bear.append(f"Harga {change:.1f}% (lemah)")

    if fg and fg.get("value"):
        try:
            v = int(fg["value"])
            if v <= 25:
                score += 1.2
                drivers_bull.append(f"Fear & Greed {v} (Extreme Fear → historis akumulasi)")
            elif v <= 40:
                score += 0.4
                drivers_bull.append(f"Fear & Greed {v} (Fear)")
            elif v >= 75:
                score -= 1.3
                drivers_bear.append(f"Fear & Greed {v} (Extreme Greed → risiko FOMO)")
            elif v >= 60:
                score -= 0.5
                drivers_bear.append(f"Fear & Greed {v} (Greed)")
        except Exception:
            pass

    if news:
        news_scores = [a.get("_score", 0) for a in news if a.get("_score", 0) > -50]
        if news_scores:
            avg = sum(news_scores) / len(news_scores)
            news_contrib = max(-2.0, min(2.0, avg * 0.45))
            score += news_contrib

            bull_news = sorted(
                [a for a in news if a.get("_score", 0) >= 1.2],
                key=lambda x: x["_score"],
                reverse=True,
            )[:3]
            bear_news = sorted(
                [a for a in news if a.get("_score", 0) <= -1.2],
                key=lambda x: x["_score"],
            )[:3]

            for a in bull_news:
                t = clean_title(a.get("title") or "")[:70]
                if t:
                    drivers_bull.append(t)
            for a in bear_news:
                t = clean_title(a.get("title") or "")[:70]
                if t:
                    drivers_bear.append(t)

    if score >= 2.2:
        bias = "Strongly Bullish"
        bias_emoji = "🟢🟢"
    elif score >= 0.8:
        bias = "Mildly Bullish"
        bias_emoji = "🟢"
    elif score <= -2.2:
        bias = "Strongly Bearish"
        bias_emoji = "🔴🔴"
    elif score <= -0.8:
        bias = "Mildly Bearish"
        bias_emoji = "🔴"
    else:
        bias = "Neutral / Range"
        bias_emoji = "⚪"

    return {
        "score": round(score, 2),
        "bias": bias,
        "bias_emoji": bias_emoji,
        "drivers_bull": drivers_bull[:4],
        "drivers_bear": drivers_bear[:4],
    }


def build_impact_text(bias_data: Dict, price: Dict) -> List[str]:
    bias = bias_data["bias"]
    px = price.get("price") or 0

    lines = []

    if "Bullish" in bias:
        lines.append("→ Support <b>long bias</b> pada pullback / retest support")
        if px:
            support = px * 0.985
            lines.append(f"→ Area menarik akumulasi: sekitar <b>{fmt_usd(support, 0)}</b> – {fmt_usd(px * 0.995, 0)}")
        lines.append("→ Breakout perlu konfirmasi volume & close di atas resistance terdekat")
        if "Strongly" in bias:
            lines.append("→ Bisa pertimbangkan size normal–sedikit agresif (tetap risk managed)")
        else:
            lines.append("→ Size tetap konservatif sampai ada konfirmasi lanjutan")
    elif "Bearish" in bias:
        lines.append("→ Support <b>short bias / reduce long</b> pada relief rally")
        lines.append("→ Hindari FOMO long di resistance")
        if "Strongly" in bias:
            lines.append("→ Momentum jual masih dominan — prioritaskan proteksi modal")
        else:
            lines.append("→ Tunggu konfirmasi breakdown atau reversal yang jelas")
    else:
        lines.append("→ Range / konsolidasi — prioritaskan mean-reversion atau tunggu break")
        lines.append("→ Hindari size besar sampai bias lebih jelas")
        lines.append("→ Fokus pada level key support & resistance")

    return lines


# -------------------- Builders --------------------

async def build_narrative(session: aiohttp.ClientSession) -> str:
    price_t = get_btc_price(session)
    global_t = get_global_data(session)
    news_t = get_scored_news(session, limit=10)
    fg_t = get_fear_greed(session)

    price, global_d, news, fg = await asyncio.gather(price_t, global_t, news_t, fg_t)

    now = datetime.now(timezone.utc)
    hour = now.hour
    if 0 <= hour < 8:
        session_name = "Asia"
    elif 8 <= hour < 16:
        session_name = "London / EU"
    else:
        session_name = "US"

    bias_data = compute_bias(price or {}, fg, news or [])

    lines = [
        f"🟠 <b>BTC NARRATIVE</b> — {now.strftime('%d %b %Y')} | {session_name} Session",
        "",
        f"<b>Bias:</b> {bias_data['bias_emoji']} <b>{bias_data['bias']}</b>   "
        f"<i>Score: {bias_data['score']:+.1f}</i>",
        "",
    ]

    if price:
        ch = price.get("change_24h") or 0
        em = "🟢" if ch >= 0 else "🔴"
        if global_d.get("btc_dominance"):
            lines.append(
                f"Harga: <b>{fmt_usd(price.get('price'), 0)}</b> {em} {fmt_pct(ch)}   "
                f"| Dom: {global_d.get('btc_dominance', 0):.1f}%"
            )
        else:
            lines.append(f"Harga: <b>{fmt_usd(price.get('price'), 0)}</b> {em} {fmt_pct(ch)}")
        lines.append("")

    if bias_data["drivers_bull"]:
        lines.append("<b>Key Drivers (Bullish)</b>")
        for d in bias_data["drivers_bull"]:
            lines.append(f"• {d}")
        lines.append("")

    if bias_data["drivers_bear"]:
        lines.append("<b>Counter / Risk</b>")
        for d in bias_data["drivers_bear"]:
            lines.append(f"• {d}")
        lines.append("")

    lines.append("<b>Impact ke Sinyal</b>")
    for imp in build_impact_text(bias_data, price or {}):
        lines.append(imp)

    lines.append("")
    lines.append("<i>Gunakan sebagai konfirmasi, bukan satu-satunya alasan entry.</i>")
    lines.append("Detail: /news • /whales • /sentiment • /price")

    return "\n".join(lines)


async def build_briefing(session: aiohttp.ClientSession) -> str:
    price_t = get_btc_price(session)
    global_t = get_global_data(session)
    news_t = get_scored_news(session, limit=8)
    fg_t = get_fear_greed(session)

    price, global_d, news, fg = await asyncio.gather(price_t, global_t, news_t, fg_t)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    bias_data = compute_bias(price or {}, fg, news or [])

    lines = [
        "🟠 <b>BTC NARRATIVE BRIEFING</b>",
        f"<i>{now}</i>",
        "",
        f"<b>Bias Saat Ini:</b> {bias_data['bias_emoji']} <b>{bias_data['bias']}</b> "
        f"(Score {bias_data['score']:+.1f})",
        "",
    ]

    if price:
        change = price.get("change_24h") or 0
        emoji = "🟢" if change >= 0 else "🔴"
        lines.extend([
            "📊 <b>Market Snapshot</b>",
            f"Harga: <b>{fmt_usd(price.get('price'), 0)}</b>  {emoji} {fmt_pct(change)}",
            f"Market Cap: {fmt_usd(price.get('market_cap'))}",
            f"Volume 24j: {fmt_usd(price.get('volume_24h'))}",
        ])
        if global_d.get("btc_dominance"):
            lines.append(f"Dominance: <b>{global_d['btc_dominance']:.1f}%</b>")
        lines.append("")

    if fg:
        val = fg.get("value") or "?"
        cls = fg.get("classification") or fg.get("value_classification") or ""
        lines.extend([
            "🌡️ <b>Sentiment</b>",
            f"Fear & Greed: <b>{val}</b> — {cls}",
            "",
        ])

    if news:
        lines.append("📰 <b>Top Relevant Headlines</b>")
        for i, art in enumerate(news[:5], 1):
            title = clean_title(art.get("title") or "")
            label = art.get("_label", "neutral")
            em = label_emoji(label)
            source = art.get("source") or art.get("sourceKey") or ""
            src = f" <i>({source})</i>" if source else ""
            lines.append(f"{i}. {em} {title}{src}")
        lines.append("")

    if bias_data["drivers_bull"] or bias_data["drivers_bear"]:
        lines.append("📝 <b>Ringkasan Narasi</b>")
        if bias_data["drivers_bull"]:
            lines.append("Bullish: " + " | ".join(bias_data["drivers_bull"][:2]))
        if bias_data["drivers_bear"]:
            lines.append("Risk: " + " | ".join(bias_data["drivers_bear"][:2]))
        lines.append("")

    lines.append("<i>Sumber: CoinGecko + scored news aggregator</i>")
    lines.append("Gunakan /narrative untuk bias + impact ke sinyal.")

    return "\n".join(lines)


async def build_news_list(session: aiohttp.ClientSession, n: int = 8) -> str:
    articles = await get_scored_news(session, limit=n)
    if not articles:
        return "Belum ada berita relevan yang lolos filter saat ini."

    lines = ["📰 <b>BTC News — Filtered & Scored</b>\n"]
    for i, art in enumerate(articles, 1):
        title = clean_title(art.get("title") or "")
        source = art.get("source") or ""
        link = art.get("link") or art.get("url") or ""
        time_ago = art.get("timeAgo") or ""
        label = art.get("_label", "neutral")
        em = label_emoji(label)
        score = art.get("_score", 0)

        if not title:
            continue

        extra = []
        if source:
            extra.append(source)
        if time_ago:
            extra.append(time_ago)
        meta = f" — <i>{' · '.join(extra)}</i>" if extra else ""

        lines.append(f"<b>{i}.</b> {em} {title}{meta}")
        lines.append(f"   <i>score: {score:+.1f}</i>")
        if link:
            lines.append(f"   <a href=\"{link}\">Baca</a>")
        lines.append("")

    lines.append("<i>Noise (XRP, meme, sports, dll) sudah difilter.</i>")
    return "\n".join(lines)


async def build_price_card(session: aiohttp.ClientSession) -> str:
    price, global_d = await asyncio.gather(
        get_btc_price(session), get_global_data(session)
    )
    if not price:
        return "Gagal mengambil data harga saat ini."

    change = price.get("change_24h") or 0
    emoji = "🟢" if change >= 0 else "🔴"

    lines = [
        "💰 <b>Bitcoin Price Card</b>",
        "",
        f"Harga: <b>{fmt_usd(price.get('price'), 0)}</b>  {emoji} <b>{fmt_pct(change)}</b>",
        f"Market Cap: {fmt_usd(price.get('market_cap'))}",
        f"Volume 24j: {fmt_usd(price.get('volume_24h'))}",
    ]
    if global_d.get("btc_dominance"):
        lines.append(f"BTC Dominance: <b>{global_d['btc_dominance']:.2f}%</b>")
    if global_d.get("total_market_cap"):
        lines.append(f"Total Crypto MCap: {fmt_usd(global_d['total_market_cap'])}")

    lines.append("")
    lines.append(f"<i>Update: {datetime.now(timezone.utc).strftime('%H:%M UTC')}</i>")
    return "\n".join(lines)


async def build_whales_section(session: aiohttp.ClientSession) -> str:
    lines = ["🐋 <b>Whale & Institutional Moves</b>\n"]

    data = await fetch_json(session, f"{NEWS_BASE}/whales?limit=8")
    if not data:
        data = await fetch_json(session, f"{NEWS_BASE}/whale-alerts?limit=8")

    items = []
    if isinstance(data, dict):
        items = (
            data.get("alerts")
            or data.get("transactions")
            or data.get("data")
            or data.get("articles")
            or []
        )
    elif isinstance(data, list):
        items = data

    if items:
        for item in items[:6]:
            title = (
                item.get("title")
                or item.get("description")
                or item.get("text")
                or str(item)[:110]
            )
            lines.append(f"• {title}")
        lines.append("")
        lines.append("<i>Data dari aggregator on-chain & news feed.</i>")
        return "\n".join(lines)

    news = await get_scored_news(session, limit=15, min_score=-10)
    whale_news = [a for a in news if is_whale_related(a.get("title") or "")]

    if whale_news:
        for a in whale_news[:6]:
            title = clean_title(a.get("title") or "")
            label = a.get("_label", "neutral")
            em = label_emoji(label)
            source = a.get("source") or ""
            src = f" <i>({source})</i>" if source else ""
            lines.append(f"• {em} {title}{src}")
        lines.append("")
        lines.append("<i>Disaring dari headline relevan (whale / ETF / institutional / miner).</i>")
        return "\n".join(lines)

    return (
        "🐋 <b>Whale & Institutional Moves</b>\n\n"
        "Tidak ada data whale yang lolos filter saat ini.\n"
        "Coba /news atau /narrative untuk gambaran lebih luas."
    )


async def build_sentiment(session: aiohttp.ClientSession) -> str:
    fg = await get_fear_greed(session)
    price = await get_btc_price(session)

    lines = ["🌡️ <b>BTC Sentiment</b>\n"]

    if fg:
        val = fg.get("value") or "?"
        cls = fg.get("classification") or fg.get("value_classification") or ""
        lines.append(f"Fear & Greed Index: <b>{val}</b> — <b>{cls}</b>")
        try:
            v = int(val)
            if v <= 25:
                lines.append("→ Zona Extreme Fear. Historis sering menjadi area akumulasi jangka menengah.")
            elif v <= 45:
                lines.append("→ Sentiment hati-hati / fear. Volatilitas masih bisa tinggi.")
            elif v <= 55:
                lines.append("→ Netral. Pasar menunggu katalis berikutnya.")
            elif v <= 75:
                lines.append("→ Greed mulai muncul. Waspadai koreksi jangka pendek.")
            else:
                lines.append("→ Extreme Greed. Risiko FOMO dan distribusi meningkat.")
        except Exception:
            pass
        lines.append("")
    else:
        lines.append("Fear & Greed Index: data sedang tidak tersedia.")
        lines.append("")

    if price and price.get("change_24h") is not None:
        ch = price["change_24h"]
        lines.append(f"Pergerakan harga 24j: <b>{fmt_pct(ch)}</b>")
        if ch > 5:
            lines.append("Momentum bullish kuat dalam 24 jam terakhir.")
        elif ch < -5:
            lines.append("Tekanan jual signifikan dalam 24 jam terakhir.")

    lines.append("")
    lines.append("<i>Gabungan Fear & Greed + price action sebagai proxy sentiment.</i>")
    return "\n".join(lines)


# -------------------- Handlers --------------------

WELCOME = """
🟠 <b>BTC Narrative Bot</b> <i>(Upgraded)</i>

Bot narasi Bitcoin yang fokus ke <b>sinyal yang actionable</b>.

<b>Perintah utama:</b>
/narrative   — Bias + drivers + counter + impact ke sinyal
/briefing    — Narasi lengkap + scoring
/news        — Headline terfilter & scored
/price       — Harga + market snapshot
/whales      — Whale & institutional moves
/sentiment   — Fear & Greed + interpretasi
/subscribe   — Briefing otomatis
/unsubscribe

Langsung ketik /narrative untuk mulai.
"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def cmd_narrative(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await update.message.reply_text("⏳ Menyusun narrative bias…")
    session: aiohttp.ClientSession = context.application.bot_data["session"]
    text = await build_narrative(session)
    await status.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await update.message.reply_text("⏳ Menyusun narasi BTC…")
    session: aiohttp.ClientSession = context.application.bot_data["session"]
    text = await build_briefing(session)
    await status.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = 8
    if context.args:
        try:
            n = max(3, min(12, int(context.args[0])))
        except ValueError:
            pass
    status = await update.message.reply_text("⏳ Mengambil & scoring headline…")
    session: aiohttp.ClientSession = context.application.bot_data["session"]
    text = await build_news_list(session, n)
    await status.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await update.message.reply_text("⏳ Mengambil harga…")
    session: aiohttp.ClientSession = context.application.bot_data["session"]
    text = await build_price_card(session)
    await status.edit_text(text, parse_mode=ParseMode.HTML)


async def cmd_whales(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await update.message.reply_text("⏳ Mencari pergerakan besar…")
    session: aiohttp.ClientSession = context.application.bot_data["session"]
    text = await build_whales_section(session)
    await status.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_sentiment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await update.message.reply_text("⏳ Menganalisis sentiment…")
    session: aiohttp.ClientSession = context.application.bot_data["session"]
    text = await build_sentiment(session)
    await status.edit_text(text, parse_mode=ParseMode.HTML)


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    add_subscriber(update.effective_chat.id)
    await update.message.reply_text(
        "✅ Kamu berlangganan briefing otomatis.\n"
        "Bot akan kirim narasi BTC secara berkala (pagi & sore UTC).\n"
        "Ketik /unsubscribe untuk berhenti."
    )


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("✅ Berhasil berhenti berlangganan.")


# -------------------- Scheduled --------------------

async def scheduled_briefing(context: ContextTypes.DEFAULT_TYPE) -> None:
    subs = get_subscribers()
    if not subs:
        return

    session: aiohttp.ClientSession = context.application.bot_data["session"]
    text = await build_narrative(session)

    for chat_id in subs:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning("Gagal kirim ke %s: %s", chat_id, e)


# -------------------- Main --------------------

async def post_init(app: Application) -> None:
    session = aiohttp.ClientSession()
    app.bot_data["session"] = session

    from datetime import time as dt_time

    app.job_queue.run_daily(
        scheduled_briefing,
        time=dt_time(hour=MORNING_HOUR_UTC, minute=0, tzinfo=timezone.utc),
        name="morning_briefing",
    )
    app.job_queue.run_daily(
        scheduled_briefing,
        time=dt_time(hour=EVENING_HOUR_UTC, minute=0, tzinfo=timezone.utc),
        name="evening_briefing",
    )
    logger.info(
        "Scheduled briefings registered (UTC %02d:00 & %02d:00)",
        MORNING_HOUR_UTC,
        EVENING_HOUR_UTC,
    )


async def post_shutdown(app: Application) -> None:
    session: aiohttp.ClientSession = app.bot_data.get("session")
    if session:
        await session.close()


def main() -> None:
    if not BOT_TOKEN:
        print("❌ Set environment variable BOT_TOKEN dulu!")
        print("   export BOT_TOKEN='your_token'")
        return

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("narrative", cmd_narrative))
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("whales", cmd_whales))
    app.add_handler(CommandHandler("sentiment", cmd_sentiment))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))

    logger.info("BTC Narrative Bot (Upgraded) starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
