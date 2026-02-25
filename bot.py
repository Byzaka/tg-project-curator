import os
import re
import json
import time
import random
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

STATE_PATH = "state.json"
SOURCES_PATH = "sources.yaml"

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}

TIMEOUT = 20


@dataclass
class Source:
    id: str
    name: str
    list_url: str
    paging: str


def load_sources() -> List[Source]:
    # tiny YAML reader for this simple structure (no extra dependency)
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    blocks = [b.strip() for b in text.split("\n- ") if b.strip()]
    sources: List[Source] = []
    for i, block in enumerate(blocks):
        if i == 0 and block.startswith("- "):
            block = block[2:]
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        data = {}
        for ln in lines:
            if ln.startswith("#"):
                continue
            if ":" in ln:
                k, v = ln.split(":", 1)
                data[k.strip()] = v.strip().strip('"').strip("'")
        sources.append(Source(
            id=data["id"],
            name=data["name"],
            list_url=data["list_url"],
            paging=data.get("paging", "wp_page"),
        ))
    return sources


def load_state() -> Dict:
    if not os.path.exists(STATE_PATH):
        return {}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def utc_mode() -> str:
    # Actions запускается в UTC. 05:00 UTC = утро Екатеринбурга, 15:00 UTC = вечер.
    hour = datetime.now(timezone.utc).hour
    return "morning" if hour < 12 else "evening"


def http_get(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def build_paged_url(source: Source, page: int) -> str:
    if page <= 1:
        return source.list_url

    if source.paging == "wp_page":
        base = source.list_url.rstrip("/")
        return f"{base}/page/{page}/"

    if source.paging == "dezeen_page":
        base = source.list_url.rstrip("/")
        return f"{base}/page/{page}/"

    if source.paging == "archdaily_search_page":
        if "?" in source.list_url:
            if re.search(r"([?&])page=\d+", source.list_url):
                return re.sub(r"([?&])page=\d+", rf"\1page={page}", source.list_url)
            return f"{source.list_url}&page={page}"
        return f"{source.list_url}?page={page}"

    if source.paging == "archdaily_category_page":
        base = source.list_url.rstrip("/")
        return f"{base}/page/{page}"

    base = source.list_url.rstrip("/")
    return f"{base}/page/{page}/"


def normalize_url(url: str) -> str:
    url = url.strip()
    url = url.split("#")[0]
    # remove tracking params for stability (keeps main ones for ArchDaily search pages)
    if "?" in url and not ("archdaily.com/search/" in url):
        url = url.split("?")[0]
    return url


def extract_links_from_list(source, html):
    soup = BeautifulSoup(html, "html.parser")
    links = set()

    # --- RSS режим ---
    if getattr(source, "paging", "") == "rss":
        import xml.etree.ElementTree as ET
        root = ET.fromstring(html)
        for item in root.findall(".//item"):
            link = item.findtext("link")
            if link:
                links.add(link.strip())
        return list(links)

    # базовый домен для urljoin
    from urllib.parse import urljoin, urlparse

    parsed = urlparse(source.list_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        if href.startswith("javascript:") or href.startswith("mailto:"):
            continue

        href = urljoin(base, href)
        if href.startswith("//"):
    href = "https:" + href
        href = href.split("#")[0]

      # Leibal: только посты на leibal.com (не store.leibal.com)
from urllib.parse import urlparse

if "leibal.com" in base:
    p = urlparse(href)

    # только основной домен leibal.com
    if p.netloc == "leibal.com":
        # только посты вида /interiors/<slug>/ или /architecture/<slug>/
        if re.search(r"^/(interiors|architecture)/[^/]+/?$", p.path):
            links.add(href)

    continue

        # ArchDaily: проекты с числовым id
        if "archdaily.com" in base:
            if "archdaily.com" in href and re.search(r"archdaily\.com/\d{6,}/", href) and "/search/" not in href:
                links.add(href)
            continue

        # Dezeen: статьи с датой /YYYY/MM/DD/
        if "dezeen.com" in base:
            if re.search(r"dezeen\.com/\d{4}/\d{2}/\d{2}/", href):
                links.add(href)
            continue

        # Landezine: обычно /slug/
        if "landezine.com" in base:
            if "landezine.com" in href and re.search(r"landezine\.com/[^/]+/?$", href):
                if not any(x in href for x in ["/about", "/contact", "/privacy", "/terms"]):
                    links.add(href)
            continue

        # WLA: посты, не теги/категории
        if "worldlandscapearchitect.com" in base:
            if "worldlandscapearchitect.com" in href and not any(x in href for x in ["/category/", "/tag/"]):
                links.add(href)
            continue

    return list(links)


def meta_content(soup: BeautifulSoup, prop: str) -> Optional[str]:
    tag = soup.find("meta", property=prop)
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


def find_first_paragraph(soup: BeautifulSoup) -> Optional[str]:
    # prefer article content
    for sel in ["article p", ".article__body p", ".post-content p", ".entry-content p", "main p"]:
        p = soup.select_one(sel)
        if p:
            txt = " ".join(p.get_text(" ", strip=True).split())
            if len(txt) >= 50:
                return txt
    return None


def find_credit(text: str, patterns: List[str]) -> Optional[str]:
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            # stop at line breaks / separators
            val = re.split(r"[\n\r|•·]", val)[0].strip()
            # avoid absurdly long captures
            if 2 <= len(val) <= 120:
                return val
    return None


def parse_project(url: str) -> Dict[str, Optional[str]]:
    html = http_get(url)
    soup = BeautifulSoup(html, "html.parser")

    title = meta_content(soup, "og:title") or (soup.find("h1").get_text(strip=True) if soup.find("h1") else url)
    desc = meta_content(soup, "og:description") or find_first_paragraph(soup)
    preview = meta_content(soup, "og:image")

    full_text = soup.get_text("\n", strip=True)

    # heuristics for authors/bureau + photographer
    authors = None
    photographer = None

    domain = url

    if "leibal.com" in domain:
        authors = find_credit(full_text, [
            r"Architects?:\s*([^\n]+)",
            r"Design(?:er|ers)?:\s*([^\n]+)",
            r"Studio:\s*([^\n]+)",
        ])
        photographer = find_credit(full_text, [
            r"Photographs?:\s*([^\n]+)",
            r"Photography:\s*([^\n]+)",
            r"Photo(?:s)?\s*by\s*([^\n]+)",
        ])

    elif "archdaily.com" in domain:
        authors = find_credit(full_text, [
            r"Architects?:\s*([^\n]+)",
            r"Architecture(?:\s*Firm)?:\s*([^\n]+)",
            r"Design(?:\s*Team)?:\s*([^\n]+)",
        ])
        photographer = find_credit(full_text, [
            r"Photographs?:\s*([^\n]+)",
            r"Photography:\s*([^\n]+)",
        ])

    elif "dezeen.com" in domain:
        # Dezeen often has "Photography by ..." in text
        authors = find_credit(full_text, [
            r"by\s+([A-Z][^\n]{2,80})",  # rough: catches "by Studio"
            r"Architecture\s+by\s*([^\n]+)",
            r"Design\s+by\s*([^\n]+)",
        ])
        photographer = find_credit(full_text, [
            r"Photography\s+by\s*([^\n]+)",
            r"Photographs?\s+by\s*([^\n]+)",
            r"Photos?\s+by\s*([^\n]+)",
        ])

    elif "landezine" in domain:
        authors = find_credit(full_text, [
            r"Landscape\s+Architect(?:ure)?\s*:\s*([^\n]+)",
            r"Office\s*:\s*([^\n]+)",
            r"Design\s*:\s*([^\n]+)",
        ])
        photographer = find_credit(full_text, [
            r"Photographer\s*:\s*([^\n]+)",
            r"Photography\s*:\s*([^\n]+)",
            r"Photo(?:s)?\s*:\s*([^\n]+)",
        ])

    elif "worldlandscapearchitect" in domain:
        authors = find_credit(full_text, [
            r"Landscape\s+Architect(?:ure)?\s*:\s*([^\n]+)",
            r"Designer\s*:\s*([^\n]+)",
            r"Firm\s*:\s*([^\n]+)",
        ])
        photographer = find_credit(full_text, [
            r"Photographer\s*:\s*([^\n]+)",
            r"Photography\s*:\s*([^\n]+)",
            r"Photo(?:s)?\s*:\s*([^\n]+)",
        ])

    # Trim description to a short snippet
    if desc:
        desc = desc.strip()
        desc = re.sub(r"\s+", " ", desc)
        if len(desc) > 320:
            desc = desc[:317].rstrip() + "…"

    return {
        "title": title.strip() if title else url,
        "desc": desc,
        "authors": authors,
        "photographer": photographer,
        "preview": preview,
        "url": url,
    }


def tg_api(method: str) -> str:
    return f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"


def tg_send_photo(photo_url: str, caption: str) -> bool:
    data = {
        "chat_id": TG_CHAT_ID,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(tg_api("sendPhoto"), data=data, timeout=TIMEOUT)
    return r.ok


def tg_send_message(text: str) -> bool:
    data = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    r = requests.post(tg_api("sendMessage"), data=data, timeout=TIMEOUT)
    return r.ok


def format_caption(source_name: str, project: Dict[str, Optional[str]]) -> str:
    title = project.get("title") or "Untitled"
    desc = project.get("desc") or ""
    authors = project.get("authors") or "—"
    photographer = project.get("photographer") or "—"
    url = project.get("url") or ""

    # HTML safe minimal escaping
    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    lines = [
        f"<b>{esc(title)}</b>",
        f"<i>{esc(source_name)}</i>",
        "",
    ]
    if desc:
        lines.append(esc(desc))
        lines.append("")
    lines.append(f"<b>Авторы/бюро:</b> {esc(authors)}")
    lines.append(f"<b>Фотограф:</b> {esc(photographer)}")
    lines.append(f"<b>Источник:</b> {esc(url)}")
    return "\n".join(lines)


def ensure_source_state(state: Dict, source_id: str) -> None:
    if source_id not in state:
        state[source_id] = {
            "sent_urls": [],
            "queue_new": [],
            "archive_page": 1
        }


def add_new_links_to_queue(state: Dict, source: Source, links: List[str]) -> None:
    s = state[source.id]
    sent = set(s["sent_urls"])
    queued = set(s["queue_new"])
    for u in links:
        if u not in sent and u not in queued:
            s["queue_new"].append(u)


def pick_old_link(state: Dict, source: Source) -> Optional[str]:
    s = state[source.id]
    sent = set(s["sent_urls"])

    # Try scanning deeper pages until we find an unseen link
    for _ in range(1, 6):  # at most 5 pages per run (polite)
        page = s.get("archive_page", 1)
        url = build_paged_url(source, page)
        try:
            html = http_get(url)
        except Exception:
            return None
        links = extract_links_from_list(source, html)
        random.shuffle(links)
        for u in links:
            if u not in sent:
                s["archive_page"] = page + 1
                return u
        s["archive_page"] = page + 1

    return None


def pick_link_for_run(state: Dict, source: Source, mode: str) -> Optional[str]:
    s = state[source.id]
    # morning: prefer new
    if mode == "morning":
        if s["queue_new"]:
            return s["queue_new"].pop(0)
        return pick_old_link(state, source)

    # evening: prefer old
    old = pick_old_link(state, source)
    if old:
        return old
    if s["queue_new"]:
        return s["queue_new"].pop(0)
    return None


def main():
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("TG_BOT_TOKEN / TG_CHAT_ID are not set in env (GitHub Secrets).")

    mode = utc_mode()  # morning/evening
    sources = load_sources()
    state = load_state()

    # 1) Ensure state + refresh new queues
    for src in sources:
        ensure_source_state(state, src.id)
        try:
            html = http_get(src.list_url)
            links = extract_links_from_list(src, html)
            add_new_links_to_queue(state, src, links)
        except Exception:
            # skip refresh for this source on errors
            continue
        time.sleep(1.0)  # polite delay

    # 2) For each source pick one project and send
    for src in sources:
        ensure_source_state(state, src.id)
        link = pick_link_for_run(state, src, mode)
        if not link:
            tg_send_message(f"<b>{src.name}</b>\nНе удалось подобрать проект (ошибка парсинга или нет доступных ссылок).")
            continue

        try:
            project = parse_project(link)
            caption = format_caption(src.name, project)
            preview = project.get("preview")
            sent_ok = False
            if preview and preview.startswith("http"):
                sent_ok = tg_send_photo(preview, caption)
            if not sent_ok:
                tg_send_message(caption)
        except Exception:
            tg_send_message(f"<b>{src.name}</b>\nОшибка при разборе проекта: {link}")
            continue

        # mark as sent
        st = state[src.id]
        st["sent_urls"].append(link)
        # keep sent list from growing infinitely
        if len(st["sent_urls"]) > 5000:
            st["sent_urls"] = st["sent_urls"][-5000:]

        time.sleep(1.2)

    save_state(state)


if __name__ == "__main__":
    main()
