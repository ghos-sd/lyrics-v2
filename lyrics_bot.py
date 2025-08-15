# -*- coding: utf-8 -*-
import os
import re
import html
import logging
from typing import Optional, Tuple, List

import aiohttp
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ----------------- إعدادات عامة -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("lyrics-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "ar,en;q=0.9"}

HELP_TEXT = (
    "أرسل اسم الأغنية والفنان بأي صيغة، وأنا أجيب لك الكلمات.\n"
    "أمثلة:\n"
    "• عمرو دياب تملي معاك\n"
    "• lyrics eminem venom\n"
    "• Taylor Swift - Love Story\n"
    "أو استخدم الأمر: /lyrics عمرو دياب تملي معاك"
)

# ----------------- أدوات نصية -----------------
_AR_DIAC = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u06D6-\u06ED]")
_PUNCT  = re.compile(r"[^\w\s\u0600-\u06FF]+", re.UNICODE)
AR_RANGE = re.compile(r"[\u0600-\u06FF]")

def normalize_ar(s: str) -> str:
    s = str(s or "").strip()
    s = _AR_DIAC.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ئ", "ي").replace("ؤ", "و")
    s = s.replace("ة", "ه")
    s = s.replace("گ", "ك").replace("پ", "ب").replace("چ", "ج").replace("ژ", "ز")
    s = _PUNCT.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    s = re.sub(r"\b(كلمات|اغنيه|أغنيه|أغنية|lyrics|by|song|feat|ft)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def is_arabic_query(s: str) -> bool:
    arab = len(AR_RANGE.findall(s or ""))
    return arab >= 2 or (arab / max(len(s or ""), 1) >= 0.2)

def split_artist_title(q: str) -> Tuple[str, str]:
    q_clean = normalize_ar(q)
    parts = re.split(r"\s[-–—]\s|[-–—]|:|\|", q)
    if len(parts) >= 2:
        return normalize_ar(parts[0]), normalize_ar(" ".join(parts[1:]))
    tokens = q_clean.split()
    if len(tokens) >= 3:
        return " ".join(tokens[:-2]), " ".join(tokens[-2:])
    return "", q_clean

# ----------------- HTTP helper -----------------
async def fetch_text(session: aiohttp.ClientSession, url: str, **kwargs) -> Optional[str]:
    try:
        async with session.get(url, headers=HEADERS, timeout=25, **kwargs) as r:
            r.raise_for_status()
            return await r.text()
    except Exception as e:
        log.debug("GET fail %s: %s", url, e)
        return None

# ----------------- مزوّدات عربية (بدون مفاتيح) -----------------
async def provider_lyricstranslate_ar(session: aiohttp.ClientSession, query: str):
    """LyricsTranslate (AR)."""
    search_url = "https://lyricstranslate.com/ar/search"
    html_text = await fetch_text(session, search_url, params={"q": query})
    if not html_text:
        return None
    soup = BeautifulSoup(html_text, "html.parser")

    # الترتيب: أول نتيجة عربية واضحة
    for a in soup.select("a[href*='/ar/song/'], a[href*='/ar/%D8%A3%D8%BA%D9%86%D9%8A%D8%A9/']"):
        song_url = "https://lyricstranslate.com" + (a.get("href") or "")
        page = await fetch_text(session, song_url)
        if not page:
            continue
        sp = BeautifulSoup(page, "html.parser")
        block = (
            sp.select_one(".lyrics, .lt-lyrics, #song-body, .song-node .field-lyrics")
            or sp.find("div", class_="lyrics")
        )
        if not block:
            continue
        for br in block.find_all("br"):
            br.replace_with("\n")
        text = block.get_text("\n", strip=True)
        if text and len(text) > 40:
            title = sp.find("h1")
            credit = (title.get_text(strip=True) if title else "LyricsTranslate (AR)") + " — LyricsTranslate"
            return html.unescape(text), credit
    return None

async def provider_arabiclyrics(session: aiohttp.ClientSession, query: str):
    """ArabicLyrics.net"""
    q = "+".join((query or "").split())
    url = f"https://www.arabiclyrics.net/?s={q}"
    html_text = await fetch_text(session, url)
    if not html_text:
        return None
    soup = BeautifulSoup(html_text, "html.parser")
    first = soup.select_one("h2.entry-title a, h3.entry-title a, .post-title a")
    if not first:
        return None
    page = await fetch_text(session, first.get("href") or "")
    if not page:
        return None
    sp = BeautifulSoup(page, "html.parser")
    block = sp.select_one(".entry-content, .post-content, .lyrics, article .content") or sp.find("div", class_="entry")
    if not block:
        return None
    for br in block.find_all("br"):
        br.replace_with("\n")
    text = block.get_text("\n", strip=True)
    if text and len(text) > 40:
        title = sp.find(["h1", "h2"])
        credit = (title.get_text(strip=True) if title else "ArabicLyrics") + " — ArabicLyrics"
        return html.unescape(text), credit
    return None

async def provider_klyric(session: aiohttp.ClientSession, query: str):
    """KLyric.com"""
    html_text = await fetch_text(session, "https://klyric.com/search", params={"q": query})
    if not html_text:
        return None
    soup = BeautifulSoup(html_text, "html.parser")
    first = soup.select_one("a[href*='/song/'], a.result-title")
    if not first:
        return None
    page = await fetch_text(session, first.get("href") or "")
    if not page:
        return None
    sp = BeautifulSoup(page, "html.parser")
    block = sp.select_one(".lyrics, .post-content, #lyrics, article .content")
    if not block:
        return None
    for br in block.find_all("br"):
        br.replace_with("\n")
    text = block.get_text("\n", strip=True)
    if text and len(text) > 40:
        title = sp.find(["h1", "h2"])
        credit = (title.get_text(strip=True) if title else "KLyric") + " — KLyric"
        return html.unescape(text), credit
    return None

# ----------------- احتياطي أجنبي (Genius بدون مفتاح) -----------------
async def genius_search(session: aiohttp.ClientSession, query: str) -> List[dict]:
    url = "https://genius.com/api/search/multi"
    try:
        async with session.get(url, params={"q": query}, headers=HEADERS, timeout=20) as r:
            r.raise_for_status()
            data = await r.json()
            secs = (data.get("response") or {}).get("sections") or []
            out = []
            for sec in secs:
                if sec.get("type") == "song":
                    for hit in sec.get("hits", []):
                        res = hit.get("result") or {}
                        out.append({
                            "title": res.get("title", ""),
                            "artist": ((res.get("primary_artist") or {}).get("name")) or "",
                            "url": res.get("url", ""),
                        })
            return out
    except Exception as e:
        log.debug("Genius search err: %s", e)
        return []

async def genius_fetch(session: aiohttp.ClientSession, page_url: str) -> Optional[str]:
    page = await fetch_text(session, page_url)
    if not page:
        return None
    soup = BeautifulSoup(page, "html.parser")
    blocks = soup.select("[data-lyrics-container='true']")
    if blocks:
        lines = []
        for b in blocks:
            for br in b.find_all("br"):
                br.replace_with("\n")
            t = b.get_text("\n", strip=True)
            if t:
                lines.append(t)
        txt = "\n".join(lines).strip()
        return html.unescape(txt) if txt else None
    old = soup.find("div", class_="lyrics")
    return old.get_text("\n", strip=True) if old else None

# ----------------- المنطق الذكي -----------------
async def get_lyrics(query: str) -> Tuple[Optional[str], Optional[str]]:
    """
    إن كان الاستعلام عربيًا نقدّم المزوّدات العربية أولاً،
    وإلا نلجأ إلى Genius كاحتياطي.
    """
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        if is_arabic_query(query):
            for provider in (provider_lyricstranslate_ar, provider_arabiclyrics, provider_klyric):
                try:
                    res = await provider(session, query)
                    if res:
                        return res  # (lyrics, credit)
                except Exception as e:
                    log.debug("AR provider fail: %s", e)

        # احتياطي: Genius
        artist, title = split_artist_title(query)
        words = (f"{artist} {title}".strip() or query).strip()
        results = await genius_search(session, words) or await genius_search(session, f"{title} {artist}".strip())
        if results:
            qn = normalize_ar(words)
            best = max(
                results,
                key=lambda it: fuzz.WRatio(qn, normalize_ar(f"{it.get('artist','')} {it.get('title','')}"))
            )
            lyr = await genius_fetch(session, best.get("url") or "")
            if lyr:
                return lyr, f"{best.get('artist','')} – {best.get('title','')} (Genius)"
    return None, None

# ----------------- Telegram Handlers -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! 👋\nSend song name (Arabic or English) and I’ll fetch the lyrics.\n\n"
        + HELP_TEXT + "\n\nDeveloped by @Ghostnosd"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

def extract_query(text: str) -> str:
    t = text or ""
    t = re.sub(r"^/(lyric|lyrics)\s*", "", t, flags=re.I).strip()
    t = re.sub(r"^\s*(كلمات|اغنيه|أغنيه|أغنية)\s*", "", t, flags=re.I).strip()
    return t

async def lyrics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = extract_query(" ".join(context.args)) if context.args else ""
    if not query:
        await update.message.reply_text("اكتب اسم الأغنية والفنان، مثال: عمرو دياب تملي معاك")
        return
    await run_lookup(update, query)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    if text.startswith("/lyrics"):
        query = extract_query(text)
        if not query:
            await update.message.reply_text("اكتب اسم الأغنية بعد /lyrics")
            return
        await run_lookup(update, query)
    else:
        await run_lookup(update, text)

async def run_lookup(update: Update, query: str):
    msg = await update.message.reply_text("⏳ يبحث عن الكلمات…")
    try:
        lyrics, credit = await get_lyrics(query)
        if lyrics:
            MAX = 3800
            chunks = [lyrics[i:i + MAX] for i in range(0, len(lyrics), MAX)]
            await msg.edit_text(f"🎵 {credit}\n\n{chunks[0]}")
            for ch in chunks[1:]:
                await update.message.reply_text(ch)
        else:
            await msg.edit_text(
                "❌ ما لقيت كلمات مناسبة تلقائياً.\n"
                "جرّب تكتبها بصيغة أوضح: *الفنان – اسم الأغنية* أو *عمرو دياب تملي معاك*."
            )
    except Exception as e:
        log.exception("Lookup failed")
        await msg.edit_text(f"تعذّر الجلب: {e}")

# ----------------- تشغيل البوت -----------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("ضبط BOT_TOKEN في المتغيّرات.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("lyrics", lyrics_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    log.info("Lyrics bot running…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
