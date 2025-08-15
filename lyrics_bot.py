# -*- coding: utf-8 -*-
import os, re, json, asyncio, logging
from typing import Optional, Tuple, Dict, Any

import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters
import aiohttp

# ================== إعدادات عامة ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("pin-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}
HTTP_TIMEOUT = 25
PIN_HOSTS = (
    "pinterest.com","www.pinterest.com","pin.it",
    "in.pinterest.com","www.pinterest.co.uk","ar.pinterest.com"
)

# ================== أدوات استخراج (تُستخدم في Thread) ==================
def _pick_best_video(vlist: Dict[str, Any]) -> Optional[str]:
    if not isinstance(vlist, dict): return None
    order = ["V_720P","V_640P","V_480P","V_360P","V_240P","V_EXP4"]
    for q in order:
        item = vlist.get(q)
        if isinstance(item, dict) and item.get("url"):
            url = item["url"]
            if url.endswith(".mp4"):
                return url
    # أية mp4 اخرى
    for v in vlist.values():
        if isinstance(v, dict) and str(v.get("url","")).endswith(".mp4"):
            return v["url"]
    return None

def _pick_best_image(images: Dict[str, Any]) -> Optional[str]:
    if not isinstance(images, dict): return None
    # الأصلية أولاً
    if "orig" in images and isinstance(images["orig"], dict):
        u = images["orig"].get("url")
        if u: return u
    # وإلاّ أكبر مساحة
    best, area = None, -1
    for it in images.values():
        if isinstance(it, dict):
            u = it.get("url"); h = it.get("height") or 0; w = it.get("width") or 0
            a = (h*w) if (h and w) else 0
            if u and a >= area:
                best, area = u, a
    return best

def _expand_url(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True)
        final_url = r.url or url
        if "/pin/" in final_url:
            return final_url
        soup = BeautifulSoup(r.text, "html.parser")
        can = soup.find("link", rel="canonical")
        if can and "/pin/" in (can.get("href") or ""):
            return can["href"]
        og = soup.find("meta", property="og:url")
        if og and "/pin/" in (og.get("content") or ""):
            return og["content"]
        return final_url
    except Exception:
        return url

def _pin_id(url: str) -> Optional[str]:
    m = re.search(r"/pin/(\d+)", url)
    return m.group(1) if m else None

def _get_html(url: str) -> str:
    with requests.Session() as s:
        s.headers.update(HEADERS)
        r = s.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.text

def _try_pidgets(pin_id: str) -> Tuple[Optional[str], Optional[str]]:
    """
    API قديم غير موثّق:
      https://widgets.pinterest.com/v3/pidgets/pins/info/?pin_ids=...
    لو لقى video_list (mp4) يرجع فيديو؛ وإلاّ أعلى صورة.
    """
    try:
        r = requests.get(
            "https://widgets.pinterest.com/v3/pidgets/pins/info/",
            params={"pin_ids": pin_id}, headers=HEADERS, timeout=HTTP_TIMEOUT
        )
        if r.status_code != 200:
            return None, None
        data = r.json()
        pins = ((data or {}).get("data") or {}).get("pins") or []
        if not pins:
            return None, None
        pin = pins[0]
        vurl = _pick_best_video(((pin.get("videos") or {}).get("video_list")) or {})
        if vurl: return vurl, "video"
        img = _pick_best_image(pin.get("images") or {})
        if img: return img, "image"
    except Exception:
        pass
    return None, None

def _parse_pws_json(html: str) -> Tuple[Optional[str], Optional[str]]:
    """
    يحاول استخراج video_list أو images من سكربت __PWS_DATA__ أو أي JSON مشابه.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        sc = soup.find("script", id="__PWS_DATA__")
        if not sc or not sc.string:
            # fallback: أي سكربت فيه initialReduxState
            for s in soup.find_all("script"):
                if s.string and ("initialReduxState" in s.string or "__PWS_DATA__" in s.string):
                    sc = s; break
        if not sc or not sc.string:
            return None, None
        txt = sc.string.strip()
        # نظّف أي نص زائد قبل JSON
        txt = re.sub(r"^[^{]*", "", txt)
        txt = re.sub(r";?\s*$", "", txt)
        data = json.loads(txt)

        def deep_find(o):
            # يدور على video_list أو images أينما كانت
            if isinstance(o, dict):
                if "video_list" in o: return ("video", o["video_list"])
                if "videos" in o and isinstance(o["videos"], dict):
                    vl = o["videos"].get("video_list") or o["videos"]
                    return ("video", vl)
                if "images" in o: return ("image", o["images"])
                for v in o.values():
                    r = deep_find(v)
                    if r: return r
            elif isinstance(o, list):
                for it in o:
                    r = deep_find(it)
                    if r: return r
            return None

        found = deep_find(data)
        if found:
            kind, payload = found
            if kind == "video":
                v = _pick_best_video(payload or {})
                if v: return v, "video"
            elif kind == "image":
                u = _pick_best_image(payload or {})
                if u: return u, "image"
    except Exception:
        pass
    return None, None

def _meta_or_regex(html: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        soup = BeautifulSoup(html, "html.parser")
        # فيديو من الميتا
        mv = soup.find("meta", property="og:video") or \
             soup.find("meta", property="og:video:url") or \
             soup.find("meta", property="twitter:player:stream")
        if mv and mv.get("content") and mv["content"].endswith(".mp4"):
            return mv["content"], "video"
        # صورة من الميتا
        mi = soup.find("meta", property="og:image") or soup.find("meta", property="og:image:secure_url")
        if mi and mi.get("content"):
            return mi["content"], "image"
        # Regex مباشر
        m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.mp4", html, re.I)
        if m: return m.group(0), "video"
        m = re.search(r"https?://[a-z0-9.-]*pinimg\.com/[^\s'\"<>]+\.(?:jpg|jpeg|png|webp)", html, re.I)
        if m: return m.group(0), "image"
    except Exception:
        pass
    return None, None

def extract_media_sync(pin_url: str) -> Tuple[Optional[str], Optional[str], str]:
    """
    يُرجع (media_url, media_type, debug_source)
    media_type إما "video" أو "image"
    """
    url = _expand_url(pin_url)
    pid = _pin_id(url)

    # 1) pidgets أولاً (أقوى طريقة للعام)
    if pid:
        u, t = _try_pidgets(pid)
        if u: return u, t, "pidgets"

    # 2) صفحة الـ pin: JSON داخلي
    html = _get_html(url)
    u, t = _parse_pws_json(html)
    if u: return u, t, "__PWS_DATA__"

    # 3) ميتا/Regex
    u, t = _meta_or_regex(html)
    if u: return u, t, "meta/regex"

    return None, None, "none"

# ================== Telegram Bot ==================
PIN_URL_RE = re.compile(r"https?://(?:www\.)?(?:pin\.it|[a-z]{0,3}\.?pinterest\.com)/[^\s]+", re.I)

async def fetch_head_content_type(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.head(url, timeout=20) as r:
            if r.status in (200, 206):
                return r.headers.get("Content-Type","")
    except Exception:
        pass
    # fallback GET صغير
    try:
        async with session.get(url, timeout=20) as r:
            if r.status in (200, 206):
                return r.headers.get("Content-Type","")
    except Exception:
        pass
    return None

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send a Pinterest Pin URL and I’ll download it.\n"
        "• Priority: video first.\n"
        "• If no video is found, I’ll send the image.\n\n"
        "Developed by @Ghostnosd"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    m = PIN_URL_RE.search(text)
    if not m:
        return
    pin_url = m.group(0)
    status = await update.message.reply_text("⏳ Processing…")

    # شغّل الاستخراج المتزامن في Thread حتى لا يعلق loop
    media_url, media_type, source = await asyncio.to_thread(extract_media_sync, pin_url)

    if not media_url:
        await status.edit_text("❌ No media found on this Pin (maybe private or image-only).")
        return

    # تأكد من نوع الكونتنت (عشان ما يرسل الفيديو كصورة والعكس)
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        ctype = await fetch_head_content_type(session, media_url) or ""
    log.info("Found %s from %s | ctype=%s | %s", media_type, source, ctype, media_url)

    try:
        if media_type == "video" or "video" in ctype.lower() or media_url.lower().endswith(".mp4"):
            # فيديو أولاً
            await update.message.reply_video(
                video=media_url,
                supports_streaming=True,
                caption="Downloaded ✅"
            )
            await status.delete()
            return
        # وإلا نرسل صورة
        await update.message.reply_photo(
            photo=media_url,
            caption="Downloaded ✅ (image)"
        )
        await status.delete()
    except Exception as e:
        log.exception("Send failed")
        await status.edit_text(f"Failed to send: {e}")

def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN env var.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Pinterest bot is running…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()            return html.unescape(text), credit
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
