# -*- coding: utf-8 -*-
import os, re, html, asyncio, logging, time
from typing import List, Optional, Tuple, Dict, Callable

import aiohttp
from bs4 import BeautifulSoup, NavigableString
from rapidfuzz import fuzz
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ============ إعدادات عامة ============
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("lyrics-bot-ar")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=25)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "ar,en;q=0.9"}

HELP_TEXT = (
    "أرسل اسم الأغنية والفنان بأي صيغة، وسأجلب الكلمات — بدون أي API.\n"
    "أمثلة:\n"
    "• عمرو دياب تملي معاك\n"
    "• Elissa - Aa Bali Habibi\n"
    "• lyrics eminem venom\n"
    "أو استخدم: /lyrics عمرو دياب تملي معاك"
)

# ============ تطبيع عربي + تحليل ============
_AR_DIAC = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u06D6-\u06ED]")
_PUNCT   = re.compile(r"[^\w\s\u0600-\u06FF]+", re.UNICODE)
_AR_CHARS= re.compile(r"[\u0600-\u06FF]")

def normalize_ar(s: str) -> str:
    s = str(s or "").strip()
    s = _AR_DIAC.sub("", s)
    s = (s.replace("أ","ا").replace("إ","ا").replace("آ","ا")
           .replace("ى","ي").replace("ئ","ي").replace("ؤ","و")
           .replace("ة","ه").replace("گ","ك").replace("پ","ب")
           .replace("چ","ج").replace("ژ","ز"))
    s = _PUNCT.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    s = re.sub(r"\b(كلمات|اغنيه|أغنيه|أغنية|اغنية|lyrics|by|song|feat|ft)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def split_artist_title(q: str) -> Tuple[str, str]:
    q_norm = normalize_ar(q)
    parts = re.split(r"\s[-–—]\s|[-–—]|:|\|", q)
    if len(parts) >= 2:
        return normalize_ar(parts[0]), normalize_ar(" ".join(parts[1:]))
    tokens = q_norm.split()
    if len(tokens) >= 3:
        return " ".join(tokens[:-2]), " ".join(tokens[-2:])
    return "", q_norm

# ============ كاش ============
_CACHE: Dict[str, Tuple[float, Tuple[Optional[str], Optional[str]]]] = {}
CACHE_TTL = 60 * 60  # 1h

def cache_get(key: str):
    item = _CACHE.get(key)
    if not item: return None
    ts, val = item
    if time.time() - ts > CACHE_TTL:
        _CACHE.pop(key, None); return None
    return val

def cache_set(key: str, val):
    _CACHE[key] = (time.time(), val)

# ============ مصادر / نطاقات ============
# عرب + عالميين (الأسماء تقريبية؛ “generic_ar” سيحاول استنتاج النص الأكبر بالعربية)
DOMAIN_KIND = {
    "genius.com": "genius",
    "www.genius.com": "genius",
    "azlyrics.com": "azlyrics",
    "www.azlyrics.com": "azlyrics",
    "lyricstranslate.com": "lyricstranslate",
    "www.lyricstranslate.com": "lyricstranslate",

    # عرب (يُجلب عبر Generic Arabic Fetcher)
    "klyric.com": "generic_ar",
    "www.klyric.com": "generic_ar",
    "arabiclyrics.net": "generic_ar",
    "www.arabiclyrics.net": "generic_ar",
    "shen3a.com": "generic_ar",
    "www.shen3a.com": "generic_ar",
    "nogomi.com": "generic_ar",
    "www.nogomi.com": "generic_ar",
    "el7l7.com": "generic_ar",
    "www.el7l7.com": "generic_ar",
    "fay3.com": "generic_ar",
    "www.fay3.com": "generic_ar",
    "mawaly.com": "generic_ar",
    "www.mawaly.com": "generic_ar",
    "rotana.net": "generic_ar",
    "www.rotana.net": "generic_ar",
}

ALLOWED_HOSTS = set(DOMAIN_KIND.keys())

# ============ بحث DuckDuckGo (بدون API) ============
async def ddg_search(session: aiohttp.ClientSession, query: str, limit: int = 12) -> List[Tuple[str,str]]:
    try:
        url = "https://duckduckgo.com/html/"
        async with session.get(url, params={"q": query + " كلمات اغنية lyrics"}) as r:
            r.raise_for_status()
            text = await r.text()
        soup = BeautifulSoup(text, "html.parser")
        out = []
        for a in soup.select(".result__a"):
            title = a.get_text(" ", strip=True)
            href  = a.get("href") or ""
            if not href: continue
            out.append((title, href))
            if len(out) >= limit: break
        return out
    except Exception as e:
        log.debug("DDG search failed: %s", e)
        return []

def pick_from_ddg(query_norm: str, hits: List[Tuple[str,str]]) -> Optional[Tuple[str,str,str]]:
    ranked = []
    for title, url in hits:
        host = re.sub(r"^https?://", "", url).split("/")[0].lower()
        if host not in ALLOWED_HOSTS:
            continue
        score = fuzz.WRatio(query_norm, normalize_ar(title))
        ranked.append((score, DOMAIN_KIND[host], title, url))
    if not ranked: return None
    ranked.sort(key=lambda x: x[0], reverse=True)
    if ranked[0][0] < 58:  # عتبة معقولة
        return None
    _, kind, title, url = ranked[0]
    return kind, title, url

# ============ Fetchers خاصة ============
async def fetch_genius(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(url) as r:
            r.raise_for_status()
            html_text = await r.text()
        soup = BeautifulSoup(html_text, "html.parser")
        blocks = soup.select("[data-lyrics-container='true']")
        if blocks:
            lines = []
            for b in blocks:
                for br in b.find_all("br"): br.replace_with("\n")
                t = b.get_text("\n", strip=True)
                if t: lines.append(t)
            text = "\n".join(lines).strip()
            return html.unescape(text) if text else None
        old = soup.find("div", class_="lyrics")
        if old:
            return html.unescape(old.get_text("\n", strip=True))
    except Exception as e:
        log.debug("genius fetch err: %s", e)
    return None

async def fetch_azlyrics(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(url) as r:
            r.raise_for_status()
            html_text = await r.text()
        soup = BeautifulSoup(html_text, "html.parser")
        candidates = [d for d in soup.select("div") if not d.get("class")]
        best = ""
        for d in candidates:
            t = d.get_text("\n", strip=True)
            if t and len(t) > len(best): best = t
        return best or None
    except Exception:
        return None

async def fetch_lyricstranslate(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(url) as r:
            r.raise_for_status()
            html_text = await r.text()
        soup = BeautifulSoup(html_text, "html.parser")
        blocks = soup.select(".lyrics_text") or soup.select(".lt-lyrics")
        lines = []
        for b in blocks:
            for br in b.find_all("br"): br.replace_with("\n")
            t = b.get_text("\n", strip=True)
            if t: lines.append(t)
        return "\n".join(lines).strip() or None
    except Exception:
        return None

# ============ Fetcher عربي عام (Density Heuristic) ============
def _strip_noise(soup: BeautifulSoup) -> None:
    for tag in soup(["script","style","noscript","iframe","svg","header","footer","nav","aside"]):
        tag.decompose()
    # عناصر شائعة مزعجة
    for cls in ["breadcrumb","sidebar","menu","share","social","comments","ads","ad","related","tags","author","copyright"]:
        for t in soup.select(f".{cls}"):
            t.decompose()
    for idn in ["breadcrumb","sidebar","menu","share","comments","ads","related","footer","header","nav"]:
        for t in soup.select(f"#{idn}"):
            t.decompose()

def _block_score(elem) -> int:
    # يعطي نقاط أعلى لكتل عربية أكبر
    if isinstance(elem, NavigableString): return 0
    txt = elem.get_text("\n", strip=True)
    if not txt: return 0
    if not _AR_CHARS.search(txt):  # لازم يحتوي أحرف عربية
        return 0
    # درجة = طول النص - عدد الروابط
    links = len(elem.find_all("a"))
    return max(0, len(txt) - links * 20)

async def fetch_generic_ar(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(url) as r:
            r.raise_for_status()
            html_text = await r.text()
        soup = BeautifulSoup(html_text, "html.parser")
        _strip_noise(soup)
        # جرّب كتل مرشحة
        candidates = soup.select("article, .post, .content, .entry, .lyrics, .post-content, .entry-content, .page-content, .single-content, main") or soup.select("div,section")
        best_block, best_score = None, -1
        for el in candidates:
            sc = _block_score(el)
            if sc > best_score:
                best_score, best_block = sc, el
        if not best_block:
            return None
        # معالجة الأسطر
        for br in best_block.find_all("br"): br.replace_with("\n")
        text = best_block.get_text("\n", strip=True)
        # فلترة بسيطة: نرمي سطور قصيرة جدًا
        lines = [ln for ln in text.splitlines() if len(ln.strip()) >= 2]
        text = "\n".join(lines).strip()
        # تأكد وجود قدر كافٍ من العربية
        if not _AR_CHARS.search(text) or len(text) < 60:
            return None
        return text
    except Exception as e:
        log.debug("generic_ar fetch err: %s", e)
        return None

FETCHERS: Dict[str, Callable[[aiohttp.ClientSession, str], asyncio.Future]] = {
    "genius": fetch_genius,
    "azlyrics": fetch_azlyrics,
    "lyricstranslate": fetch_lyricstranslate,
    "generic_ar": fetch_generic_ar,
}

# ============ منطق الجلب الرئيسي ============
async def get_lyrics(query: str) -> Tuple[Optional[str], Optional[str]]:
    qkey = normalize_ar(query)
    cached = cache_get(qkey)
    if cached: return cached

    artist, title = split_artist_title(query)
    query_norm = normalize_ar(f"{artist} {title}".strip() or query)

    async with aiohttp.ClientSession(headers=HEADERS, timeout=HTTP_TIMEOUT) as session:
        # 1) Genius أولاً (أفضل جودة تنسيق)
        try:
            url_api = "https://genius.com/api/search/multi"
            async with session.get(url_api, params={"q": f"{artist} {title}".strip() or query}) as r:
                if r.status == 200:
                    data = await r.json()
                    secs = (data.get("response") or {}).get("sections") or []
                    song_hits = []
                    for sec in secs:
                        if sec.get("type") == "song":
                            for hit in sec.get("hits", []):
                                res = hit.get("result") or {}
                                song_hits.append({
                                    "title": res.get("title",""),
                                    "artist": ((res.get("primary_artist") or {}).get("name")) or "",
                                    "url": res.get("url",""),
                                })
                    # Pick best by fuzzy
                    ranked = []
                    for it in song_hits:
                        key = normalize_ar(f"{it['artist']} {it['title']}")
                        ranked.append((fuzz.WRatio(query_norm, key), it))
                    ranked.sort(key=lambda x: x[0], reverse=True)
                    if ranked and ranked[0][0] >= 70:
                        best = ranked[0][1]
                        lyr = await fetch_genius(session, best["url"])
                        if lyr:
                            out = (lyr, f"{best['artist']} – {best['title']} (Genius)")
                            cache_set(qkey, out)
                            return out
        except Exception as e:
            log.debug("Genius failed: %s", e)

        # 2) DuckDuckGo → اختيار أفضل نطاق (عربي/عالمي) → جلب
        patterns = [f"{artist} {title}".strip(), f"{title} {artist}".strip(), query]
        for pat in patterns:
            if not pat: continue
            hits = await ddg_search(session, pat, limit=14)
            pick = pick_from_ddg(query_norm, hits)
            if not pick:
                continue
            kind, title_txt, url = pick
            fetcher = FETCHERS.get(kind)
            if not fetcher:
                continue
            lyr = await fetcher(session, url)
            if lyr:
                out = (lyr, f"{title_txt} — {url}")
                cache_set(qkey, out)
                return out

    out = (None, None)
    cache_set(qkey, out)
    return out

# ============ تيليجرام ============
def extract_query(text: str) -> str:
    t = text or ""
    t = re.sub(r"^/(lyric|lyrics)\s*", "", t, flags=re.I).strip()
    t = re.sub(r"^\s*(كلمات|اغنيه|أغنيه|أغنية)\s*", "", t, flags=re.I).strip()
    return t

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! 👋\nSend the song name (artist + title) in Arabic or English and I'll fetch the lyrics — no API.\n\n"
        + HELP_TEXT + "\n\nDeveloped by @Ghostnosd"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def lyrics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = extract_query(" ".join(context.args)) if context.args else ""
    if not query:
        await update.message.reply_text("اكتب اسم الأغنية والفنان، مثال: عمرو دياب تملي معاك")
        return
    await run_lookup(update, query)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if not txt: return
    if txt.startswith("/lyrics"):
        q = extract_query(txt)
        if not q:
            await update.message.reply_text("اكتب اسم الأغنية بعد /lyrics")
            return
        await run_lookup(update, q)
    else:
        await run_lookup(update, txt)

async def run_lookup(update: Update, query: str):
    status = await update.message.reply_text("⏳ يبحث عن الكلمات…")
    try:
        lyrics, credit = await get_lyrics(query)
        if lyrics:
            MAX = 3800
            chunks = [lyrics[i:i+MAX] for i in range(0, len(lyrics), MAX)]
            header = f"🎵 {credit}\n\n"
            await status.edit_text(header + chunks[0])
            for ch in chunks[1:]:
                await update.message.reply_text(ch)
        else:
            await status.edit_text(
                "❌ ما لقيت كلمات تلقائياً.\n"
                "جرّب صيغة أوضح: *الفنان – اسم الأغنية* أو *عمرو دياب تملي معاك*."
            )
    except Exception as e:
        log.exception("lookup failed")
        await status.edit_text(f"تعذّر الجلب: {e}")

# ============ تشغيل ============
def main():
    if not BOT_TOKEN:
        raise SystemExit("ضبط متغير البيئة BOT_TOKEN بتوكن البوت.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("lyrics", lyrics_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    log.info("Lyrics bot (AR, no API) running…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()def guess_artist_from_az_index(artist_name: str) -> Optional[str]:
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
