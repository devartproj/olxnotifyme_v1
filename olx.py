import re
from dataclasses import dataclass
from typing import Optional, List, Tuple

import httpx
from bs4 import BeautifulSoup


@dataclass
class Listing:
    key: str
    title: str
    url: str


def soupify(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def normalize_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.olx.ua" + url
    return url


async def fetch_html(client: httpx.AsyncClient, url: str) -> Tuple[str, str]:
    r = await client.get(url, timeout=25, follow_redirects=True)
    r.raise_for_status()
    return r.text, str(r.url)


def parse_list_page(html: str) -> List[Listing]:
    soup = soupify(html)

    out: list[Listing] = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "obyavlenie" not in href or ".html" not in href:
            continue

        url = normalize_url(href)
        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = a.get_text(" ", strip=True) or "Новое объявление"

        m = re.search(r"ID([A-Za-z0-9]+)\.html", url)
        key = m.group(1) if m else url

        out.append(Listing(key=key, title=title[:120], url=url))

    return out


def extract_image_from_listing_page(html: str) -> Optional[str]:
    soup = soupify(html)
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return normalize_url(og["content"])
    return None
