#!/usr/bin/env python3
"""
BTC Narrative Bot — Professional Bitcoin News & Market Narratives
================================================================
Mengambil data BTC dari sumber terpercaya (news aggregator + market data),
menyusun narasi profesional, dan mengirim ke Telegram.

Sumber:
- cryptocurrency.cv (free, no key) → berita BTC, breaking, whale, sentiment
- CoinGecko public API → harga, market cap, volume, dominance

Fitur:
- /start, /help
- /briefing          → Narasi lengkap BTC saat ini
- /news [n]          → Headline berita terbaru
- /price             → Harga + perubahan 24j
- /whales            → Aktivitas whale / large moves
- /sentiment         → Fear & Greed + ringkasan
- /subscribe         → Dapat briefing otomatis (pagi & sore)
- /unsubscribe
- Auto scheduled briefing (bisa diatur)

Deploy: Railway / VPS (sama seperti bot sebelumnya)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ====================== CONFIG ======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "btc_narrative.db")

# Sumber data (gratis, tanpa API key)
NEWS_BASE = "https://cryptocurrency.cv/api"
CG_BASE = "https://api.coingecko.com/api/v3"

# Jadwal briefing otomatis (UTC) — sesuaikan jika perlu
MORNING_HOUR_UTC = 1   # ~08:00 WIB
EVENING_HOUR_UTC = 11  # ~18:00 WIB

REQUEST_TIMEOUT = 15
# ====================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("BTCNarrative")


# -------------------- Database (subscribers) --------------------

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
    """CoinGecko simple price + market data."""
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


async def get_btc_news(session: aiohttp.ClientSession, limit: int = 8) -> List[Dict]:
    """Bitcoin-specific news from cryptocurrency.cv"""
    data = await fetch_json(session, f"{NEWS_BASE}/bitcoin?limit={limit}")
    if not data:
        return []
    # Response shape bisa "articles" atau langsung list
    articles = data.get("articles") if isinstance(data, dict) else data
    if not isinstance(articles, list):
        return []
    return articles[:limit]


async def get_breaking(session: aiohttp.ClientSession, limit: int = 5) -> List[Dict]:
    data = await fetch_json(session, f"{NEWS_BASE}/breaking?limit={limit}")
    if not data:
        return []
    articles = data.get("articles") if isinstance(data, dict) else data
    if not isinstance(articles, list):
        return []
    return articles[:limit]


async def get_fear_greed(session: aiohttp.ClientSession) -> Optional[Dict]:
    # Beberapa endpoint aggregator menyediakan fear-greed
    data = await fetch_json(session, f"{NEWS_BASE}/fear-greed")
    if data and isinstance(data, dict):
        return data
    # Fallback: alternatif endpoint populer (bisa berubah)
    data = await fetch_json(session, "https://api.alternative.me/fng/?limit=1")
    if data and "data" in data and data["data"]:
        item = data["data"][0]
        return {
            "value": item.get("value"),
            "classification": item.get("value_classification"),
            "timestamp": item.get("timestamp"),
        }
    return None


# -------------------- Narrative Builder --------------------

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


async def build_briefing(session: aiohttp.ClientSession) -> str:
    """Membangun narasi profesional lengkap."""
    price_task = get_btc_price(session)
    global_task = get_global_data(session)
    news_task = get_btc_news(session, 6)
    fg_task = get_fear_greed(session)

    price, global_d, news, fg = await asyncio.gather(
        price_task, global_task, news_task, fg_task
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Header
    lines = [
        "🟠 <b>BTC NARRATIVE BRIEFING</b>",
        f"<i>{now}</i>",
        "",
    ]

    # Market Snapshot
    if price:
        change = price.get("change_24h") or 0
        emoji = "🟢" if change >= 0 else "🔴"
        lines.extend([
            "📊 <b>Market Snapshot</b>",
            f"Harga: <b>{fmt_usd(price.get('price'), 0)}</b>  {emoji} {fmt_pct(change)} (24j)",
            f"Market Cap: {fmt_usd(price.get('market_cap'))}",
            f"Volume 24j: {fmt_usd(price.get('volume_24h'))}",
        ])
        if global_d.get("btc_dominance"):
            lines.append(f"Dominance: <b>{global_d['btc_dominance']:.1f}%</b>")
        lines.append("")

    # Fear & Greed
    if fg:
        val = fg.get("value") or fg.get("value")
        cls = fg.get("classification") or fg.get("value_classification") or ""
        lines.extend([
            "🌡️ <b>Sentiment</b>",
            f"Fear & Greed: <b>{val}</b> — {cls}",
            "",
        ])

    # Top Headlines
    if news:
        lines.append("📰 <b>Top BTC Headlines</b>")
        for i, art in enumerate(news[:5], 1):
            title = clean_title(art.get("title") or art.get("headline") or "")
            source = art.get("source") or art.get("sourceKey") or ""
            if title:
                src = f" <i>({source})</i>" if source else ""
                lines.append(f"{i}. {title}{src}")
        lines.append("")

    # Closing narrative
    change = (price or {}).get("change_24h") or 0
    if change > 3:
        tone = "Bullish momentum terasa kuat. Pasar merespons positif terhadap aliran berita dan data on-chain."
    elif change < -3:
        tone = "Tekanan jual masih dominan. Pelaku pasar menunggu katalis baru sebelum mengambil posisi besar."
    else:
        tone = "Pergerakan masih dalam rentang konsolidasi. Fokus tetap pada volume dan aliran institusi."

    lines.extend([
        "📝 <b>Narasi Singkat</b>",
        tone,
        "",
        "<i>Sumber: CoinGecko + crypto news aggregator (termasuk pantauan media & X)</i>",
        "Gunakan /news • /price • /whales • /sentiment untuk detail lebih lanjut.",
    ])

    return "\n".join(lines)


async def build_news_list(session: aiohttp.ClientSession, n: int = 8) -> str:
    articles = await get_btc_news(session, n)
    if not articles:
        return "Belum ada berita terbaru yang bisa diambil saat ini."

    lines = ["📰 <b>BTC News — Latest Headlines</b>\n"]
    for i, art in enumerate(articles, 1):
        title = clean_title(art.get("title") or "")
        source = art.get("source") or ""
        link = art.get("link") or art.get("url") or ""
        time_ago = art.get("timeAgo") or ""
        if not title:
            continue
        extra = []
        if source:
            extra.append(source)
        if time_ago:
            extra.append(time_ago)
        meta = f" — <i>{' · '.join(extra)}</i>" if extra else ""
        lines.append(f"<b>{i}.</b> {title}{meta}")
        if link:
            lines.append(f"   <a href=\"{link}\">Baca</a>")
        lines.append("")
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
    """Coba ambil data whale / large moves dari aggregator."""
    # Beberapa endpoint aggregator punya /whales atau /whale-alerts
    data = await fetch_json(session, f"{NEWS_BASE}/whales?limit=6")
    if not data:
        data = await fetch_json(session, f"{NEWS_BASE}/whale-alerts?limit=6")

    lines = ["🐋 <b>Whale & Large Moves</b>\n"]

    items = []
    if isinstance(data, dict):
        items = data.get("alerts") or data.get("transactions") or data.get("data") or data.get("articles") or []
    elif isinstance(data, list):
        items = data

    if not items:
        # Fallback: ambil berita yang mengandung kata whale / institutional
        news = await get_btc_news(session, 12)
        whale_news = [
            a for a in news
            if any(k in (a.get("title") or "").lower() for k in ["whale", "institution", "etf", "blackrock", "fidelity", "microstrategy", "saylor"])
        ]
        if whale_news:
            for a in whale_news[:5]:
                title = clean_title(a.get("title") or "")
                source = a.get("source") or ""
                lines.append(f"• {title} <i>({source})</i>")
            lines.append("")
            lines.append("<i>Disaring dari headline yang relevan dengan pergerakan besar / institusi.</i>")
            return "\n".join(lines)

        return (
            "🐋 <b>Whale & Large Moves</b>\n\n"
            "Tidak ada data whale real-time yang tersedia saat ini.\n"
            "Coba lagi nanti atau gunakan /news untuk headline terbaru."
        )

    for item in items[:6]:
        # Format fleksibel tergantung response
        title = item.get("title") or item.get("description") or item.get("text") or str(item)[:120]
        lines.append(f"• {title}")

    lines.append("")
    lines.append("<i>Data dari aggregator on-chain & news feed.</i>")
    return "\n".join(lines)


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
                lines.append("Pasar masih dalam zona ketakutan ekstrem. Historis sering jadi area akumulasi.")
            elif v <= 45:
                lines.append("Sentiment hati-hati / fear. Volatilitas masih mungkin tinggi.")
            elif v <= 55:
                lines.append("Netral. Pasar menunggu katalis berikutnya.")
            elif v <= 75:
                lines.append("Greed mulai muncul. Waspadai koreksi jangka pendek.")
            else:
                lines.append("Extreme Greed. Risiko FOMO tinggi.")
        except Exception:
            pass
        lines.append("")

    if price and price.get("change_24h") is not None:
        ch = price["change_24h"]
        lines.append(f"Pergerakan harga 24j: <b>{fmt_pct(ch)}</b>")
        if ch > 5:
            lines.append("Momentum bullish kuat dalam 24 jam terakhir.")
        elif ch < -5:
            lines.append("Tekanan jual signifikan dalam 24 jam terakhir.")

    lines.append("")
    lines.append("<i>Gabungan Fear & Greed + pergerakan harga sebagai proxy sentiment.</i>")
    return "\n".join(lines)


# -------------------- Handlers --------------------

WELCOME = """
🟠 <b>BTC Narrative Bot</b>

Bot berita & narasi Bitcoin profesional.
Data diambil dari sumber terpercaya + aggregator yang memantau media & aktivitas besar.

<b>Perintah:</b>
/briefing   — Narasi lengkap BTC saat ini
/news       — Headline berita terbaru
/price      — Harga + market snapshot
/whales     — Pergerakan whale & institusi
/sentiment  — Fear & Greed + ringkasan
/subscribe  — Dapat briefing otomatis
/unsubscribe
/help

Langsung ketik perintah di atas untuk mulai.
"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await update.message.reply_text("⏳ Menyusun narasi BTC…")
    session: aiohttp.ClientSession = context.application.bot_data["session"]
    text = await build_briefing(session)
    await status.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = 8
    if context.args:
        try:
            n = max(3, min(15, int(context.args[0])))
        except ValueError:
            pass
    status = await update.message.reply_text("⏳ Mengambil headline…")
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


# -------------------- Scheduled Briefing --------------------

async def scheduled_briefing(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kirim briefing ke semua subscriber."""
    subs = get_subscribers()
    if not subs:
        return

    session: aiohttp.ClientSession = context.application.bot_data["session"]
    text = await build_briefing(session)

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

    # Jadwal briefing (setiap hari pada jam tertentu UTC)
    # Kita pakai run_daily
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
    logger.info("Scheduled briefings registered (UTC %02d:00 & %02d:00)", MORNING_HOUR_UTC, EVENING_HOUR_UTC)


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
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("whales", cmd_whales))
    app.add_handler(CommandHandler("sentiment", cmd_sentiment))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))

    logger.info("BTC Narrative Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
