# -*- coding: utf-8 -*-
import os, re, asyncio
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urlparse
from difflib import get_close_matches
from typing import Optional, List

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ===== Arabic shaping (preferred; falls back automatically) =====
try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    AR_SHAPING = True
except Exception:
    AR_SHAPING = False

# =================== Config / constants ===================
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")
HTTP_TIMEOUT = 8  # per request

AR_RANGE = r'\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF'
AR_REGEX = re.compile(f'[{AR_RANGE}]')
AR_KEYWORDS = ["كلمات", "كلمات أغنية", "كلمات الاغنية", "كلمات الأغنية", "Lyrics"]

# preferred Arabic domains (we try them before generic search)
AR_PREFERRED_SITES = [
    "lyricat.com", "lyricsarabic.com", "arabiclyrics.net",
    "arabiclyrics.co", "ghina4lyrics.com", "lyrics-jo.com", "lyricstranslate.com"
]
BLACKLIST = ("youtube.com","youtu.be","twitter.com","facebook.com","instagram.com",
             "tiktok.com","soundcloud.com","spotify.com","apple.com","deezer.com")

HELP_TEXT = (
    "أرسل اسم الفنان واسم الأغنية:\n"
    "`/lyrics Eminem - Superman`\n"
    "أو بالعربي: `/lyrics عمرو دياب - تملي معاك`\n"
    "تقدر كمان ترسل رسالة عادية فيها `فنان - أغنية` بدون أمر.\n"
)

# =================== helpers ===================
def http_get(url: str, timeout: int = HTTP_TIMEOUT) -> requests.Response:
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"}
    resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return resp

def looks_arabic(text: str) -> bool:
    return bool(AR_REGEX.search(text or ""))

def normalize_title(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'[\(\)\[\]\{\}\-–—_:;\'"!.?,/\\]+', ' ', s)
    s = s.replace('&', 'and')
    return s.strip()

def visual_ar_fallback(text: str) -> str:
    # Keep RTL direction; letters may be unshaped if libs missing.
    def reverse_ar_runs(line: str) -> str:
        out, buf, in_ar = [], [], None
        for ch in line:
            is_ar = bool(AR_REGEX.match(ch))
            if in_ar is None: in_ar = is_ar
            if is_ar == in_ar: buf.append(ch)
            else:
                out.append(''.join(reversed(buf)) if in_ar else ''.join(buf))
                buf, in_ar = [ch], is_ar
        if buf: out.append(''.join(reversed(buf)) if in_ar else ''.join(buf))
        return ''.join(out)
    return '\n'.join(reverse_ar_runs(ln) for ln in (text or "").splitlines())

def shape_arabic_for_display(text: str) -> str:
    if looks_arabic(text):
        if AR_SHAPING:
            try:
                return get_display(arabic_reshaper.reshape(text))
            except Exception:
                pass
        return visual_ar_fallback(text)
    return text or ""

# =================== AZLyrics ===================
def search_azlyrics_links(artist: str, song: str) -> List[str]:
    q_patterns = [
        f'site:azlyrics.com "{artist}" "{song}" lyrics',
        f'site:azlyrics.com {artist} {song} lyrics',
        f'{artist} {song} site:azlyrics.com lyrics'
    ]
    link_re = re.compile(r'https?://www\.azlyrics\.com/lyrics/[^"\'> ]+\.html')
    urls, seen = [], set()
    engines = ["https://www.bing.com/search?q=", "https://duckduckgo.com/html/?q="]
    for qp in q_patterns:
        for base in engines:
            try:
                html = http_get(base + quote_plus(qp)).text
            except Exception:
                continue
            for u in link_re.findall(html):
                if u not in seen:
                    seen.add(u); urls.append(u)
        if urls: break
    return urls[:5]

def guess_artist_from_az_index(artist_name: str) -> Optional[str]:
    if not artist_name: return None
    first_char = (artist_name.strip()[:1] or '').lower()
    if not first_char: return None
    if first_char.isdigit(): index_url = "https://www.azlyrics.com/19.html"
    else:
        first_char = re.sub(r'[^a-z]', '', first_char)
        if not first_char: return None
        index_url = f"https://www.azlyrics.com/{first_char}.html"
    try:
        html = http_get(index_url).text
    except Exception:
        return None
    soup = BeautifulSoup(html, "html.parser")
    names = []
    for a in soup.select("div.container.main-page a[href]"):
        txt = a.get_text(" ", strip=True)
        href = a.get("href", "")
        if not txt or not href or not href.endswith(".html"): continue
        if "/" not in href: continue
        names.append(txt)
    if not names: return None
    want = normalize_title(artist_name)
    norm_map = {normalize_title(n): n for n in names}
    matches = get_close_matches(want, list(norm_map.keys()), n=1, cutoff=0.6)
    return norm_map[matches[0]] if matches else None

def find_on_azlyrics_artist_page(artist: str, song: str) -> Optional[str]:
    artist_key = re.sub(r'[^a-z0-9]', '', (artist or "").lower())
    if not artist_key: return None
    first = artist_key[0]
    artist_page = f"https://www.azlyrics.com/{first}/{artist_key}.html"
    try:
        html = http_get(artist_page).text
    except Exception:
        return None
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        text = a.get_text(" ", strip=True)
        if "lyrics/" in href and href.endswith(".html"):
            if href.startswith(".."): href = "https://www.azlyrics.com/" + href.replace("../", "")
            elif href.startswith("/"): href = "https://www.azlyrics.com" + href
            candidates.append((text, href))
    if not candidates: return None
    wanted = normalize_title(song or "")
    titles = [normalize_title(t[0]) for t in candidates if t[0]]
    matches = get_close_matches(wanted, titles, n=3, cutoff=0.6)
    if not matches: return None
    for (raw_title, url) in candidates:
        if normalize_title(raw_title) == matches[0]:
            return url
    return None

def parse_azlyrics_page(url: str) -> str:
    html = http_get(url).text
    soup = BeautifulSoup(html, "html.parser")
    divs = [div for div in soup.find_all("div") if not div.attrs.get('class') and not div.attrs.get('id')]
    if not divs: raise ValueError("Lyrics block not found on AZLyrics.")
    lyrics_div = max(divs, key=lambda d: len(d.get_text(strip=True)))
    return lyrics_div.get_text(separator="\n").strip()

# =================== Genius ===================
def search_genius_link(artist: str, song: str) -> Optional[str]:
    q = f'site:genius.com "{artist}" "{song}" lyrics'
    engines = ["https://www.bing.com/search?q=", "https://duckduckgo.com/html/?q="]
    link_re = re.compile(r'https?://genius\.com/[^"\'> ]+-lyrics')
    for base in engines:
        try:
            html = http_get(base + quote_plus(q)).text
        except Exception:
            continue
        m = link_re.findall(html)
        if m: return m[0]
    return None

def parse_genius_page(url: str) -> str:
    html = http_get(url).text
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.find_all(attrs={"data-lyrics-container": "true"})
    if not containers:
        lyr = soup.select_one(".lyrics")
        if lyr: return lyr.get_text("\n", strip=True)
        raise ValueError("Lyrics container not found on Genius.")
    parts = []
    for c in containers:
        text = ""
        for el in c.descendants:
            if getattr(el, "name", None) == "br": text += "\n"
            elif isinstance(el, str): text += el
        parts.append(text.strip())
    return "\n\n".join([p for p in parts if p])

# =================== Arabic sources ===================
def search_ar_lyrics_links(artist: str, song: str) -> List[str]:
    results: List[str] = []
    link_re = re.compile(r'https?://[^\s"\'<>]+')
    # 1) preferred domains
    for dom in AR_PREFERRED_SITES:
        q = f'site:{dom} "{artist}" "{song}" كلمات'
        for base in ("https://www.bing.com/search?q=", "https://duckduckgo.com/html/?q="):
            try:
                html = http_get(base + quote_plus(q)).text
            except Exception:
                continue
            for url in link_re.findall(html):
                if any(b in url for b in BLACKLIST): continue
                if url not in results: results.append(url)
        if results: break
    # 2) generic arabic search
    if not results:
        q1 = f'"كلمات أغنية" "{artist}" "{song}"'
        q2 = f'"كلمات" "{artist}" "{song}"'
        for q in (q1, q2):
            for base in ("https://www.bing.com/search?q=", "https://duckduckgo.com/html/?q="):
                try:
                    html = http_get(base + quote_plus(q)).text
                except Exception:
                    continue
                for url in link_re.findall(html):
                    if any(b in url for b in BLACKLIST): continue
                    if re.search(r'\.(pdf|zip|rar|mp3|m4a|apk)($|\?)', url, re.I): continue
                    if url not in results: results.append(url)
            if results: break
    # sort pref
    def score(u):
        host = urlparse(u).netloc
        return 0 if any(host.endswith(d) for d in AR_PREFERRED_SITES) else 1
    results.sort(key=score)
    return results[:8]

def parse_arabic_lyrics_page(url: str) -> str:
    html = http_get(url).text
    soup = BeautifulSoup(html, "html.parser")

    def collect_text(container):
        lines = []
        for node in container.descendants:
            name = getattr(node, "name", None)
            if name == "br": lines.append("\n")
            elif isinstance(node, str): lines.append(node)
        raw = "".join(lines)
        raw = re.sub(r'\r', '', raw)
        raw = re.sub(r'\n{3,}', '\n\n', raw)
        return raw.strip()

    heading = None
    for tag in ["h1","h2","h3","strong"]:
        for el in soup.find_all(tag):
            text = el.get_text(" ", strip=True)
            if any(kw in text for kw in AR_KEYWORDS):
                heading = el; break
        if heading: break
    if heading:
        buf = []
        for sib in heading.next_siblings:
            nm = getattr(sib, "name", "")
            if nm in ["h1","h2","h3"]: break
            if nm in ["p","div","section","article","span"]:
                txt = collect_text(sib).strip()
                if txt: buf.append(txt)
        candidate = "\n\n".join(buf).strip()
        if len(candidate) > 40 and looks_arabic(candidate): return candidate

    selectors = [
        "[class*='lyrics']","[id*='lyrics']",
        "[class*='lyric']","[id*='lyric']",
        "[class*='post-content']","[class*='entry-content']",
        "article"
    ]
    for sel in selectors:
        for box in soup.select(sel):
            txt = collect_text(box).strip()
            if txt and (looks_arabic(txt) or any(kw in txt for kw in AR_KEYWORDS)):
                parts = [p.strip() for p in re.split(r'\n{2,}', txt)]
                parts = [p for p in parts if len(p) > 10 and (looks_arabic(p) or "كلمات" in p)]
                if parts:
                    joined = "\n\n".join(parts)
                    if len(joined) > 40: return joined
    raise ValueError("لم أستطع استخراج كلمات عربية من الصفحة.")

# =================== Orchestrator ===================
def smart_get_lyrics(artist: str, song: str) -> str:
    corrected_artist = artist
    if artist and not looks_arabic(artist):
        try:
            maybe = guess_artist_from_az_index(artist)
            if maybe: corrected_artist = maybe
        except Exception:
            pass

    if looks_arabic(artist) or looks_arabic(song):
        for u in search_ar_lyrics_links(artist, song):
            try: return parse_arabic_lyrics_page(u)
            except Exception: continue

    for url in search_azlyrics_links(corrected_artist, song):
        try: return parse_azlyrics_page(url)
        except Exception: continue

    alt = find_on_azlyrics_artist_page(corrected_artist, song)
    if alt:
        try: return parse_azlyrics_page(alt)
        except Exception: pass

    for u in search_ar_lyrics_links(artist, song):
        try: return parse_arabic_lyrics_page(u)
        except Exception: continue

    g = search_genius_link(corrected_artist, song)
    if g:
        try: return parse_genius_page(g)
        except Exception as e:
            return f"Found Genius page but failed to parse lyrics: {e}"

    return ("Lyrics not found automatically. Try adjusting the artist/song names, "
            "or this song may not be available on the searched sites.")

# =================== Telegram bot ===================
def parse_artist_song(arg_str: str) -> Optional[tuple[str, str]]:
    parts = re.split(r"\s*[-–—]\s*", arg_str, maxsplit=1)
    if len(parts) != 2: return None
    a, s = parts[0].strip(), parts[1].strip()
    return (a, s) if a and s else None

def split_chunks(text: str, limit: int = 4096) -> list[str]:
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text); break
        cut = text.rfind("\n\n", 0, limit)
        if cut == -1: cut = text.rfind("\n", 0, limit)
        if cut == -1: cut = limit
        chunks.append(text[:cut]); text = text[cut:].lstrip()
    return chunks

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("أهلاً! أنا بوت كلمات الأغاني 🎵\n" + HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

async def lyrics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = " ".join(context.args).strip()
    parsed = parse_artist_song(args)
    if not parsed:
        await update.message.reply_text("صيغة غير صحيحة.\n" + HELP_TEXT, parse_mode=ParseMode.MARKDOWN)
        return
    artist, song = parsed
    await update.effective_chat.send_message("⏳ يبحث عن الكلمات…")
    lyrics = await asyncio.to_thread(smart_get_lyrics, artist, song)
    display = shape_arabic_for_display(lyrics)
    for chunk in split_chunks(display):
        await update.effective_chat.send_message(chunk)

async def free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    parsed = parse_artist_song(text)
    if not parsed: return
    artist, song = parsed
    await update.effective_chat.send_message("⏳ يبحث عن الكلمات…")
    lyrics = await asyncio.to_thread(smart_get_lyrics, artist, song)
    display = shape_arabic_for_display(lyrics)
    for chunk in split_chunks(display):
        await update.effective_chat.send_message(chunk)

def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Set BOT_TOKEN in environment variables.")
    app = Application.builder().token(token).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("lyrics", lyrics_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))
    print("Bot is running (polling).")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
