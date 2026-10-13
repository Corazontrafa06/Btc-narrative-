# BTC Narrative Bot

Bot Telegram profesional untuk **narasi & berita Bitcoin**.

Mengambil data dari:
- CoinGecko (harga, market cap, volume, dominance)
- cryptocurrency.cv (berita BTC, breaking, whale/institutional headlines, sentiment)

Tanpa API key berbayar.

## Fitur

| Command | Fungsi |
|---------|--------|
| `/briefing` | Narasi lengkap BTC (harga + headline + sentiment) |
| `/news [n]` | Headline berita terbaru |
| `/price` | Price card + market snapshot |
| `/whales` | Pergerakan whale & institusi |
| `/sentiment` | Fear & Greed + interpretasi |
| `/subscribe` | Dapat briefing otomatis pagi & sore |
| `/unsubscribe` | Berhenti berlangganan |

## Deploy ke Railway

1. Buat bot baru di @BotFather (atau pakai bot yang sudah ada)
2. Upload file-file ini ke repo GitHub
3. Deploy di Railway
4. Tambah Variable:
   ```
   BOT_TOKEN = token_dari_BotFather
   ```
5. Deploy → selesai

## File

- `btc_narrative_bot.py` — bot utama
- `btc_requirements.txt` → rename jadi `requirements.txt` saat deploy
- `btc_Procfile` → rename jadi `Procfile`

## Catatan

- Briefing otomatis dikirim ke yang sudah `/subscribe`
- Jadwal default: 01:00 UTC & 11:00 UTC (sesuaikan di kode jika perlu)
- Database subscriber disimpan di SQLite (disarankan pakai Volume di Railway)
