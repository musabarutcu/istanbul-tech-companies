"""Ortak sayfa-keşif mekanizması: bir web sitesinde anahtar kelimeye uyan bağlantıları bulur.

Hem `detect.py` (kariyer sayfası arar) hem `contacts.py` (iletişim/hakkımızda/ekip
sayfası arar) aynı problemi çözüyor: bir HTML'de belirli anahtar kelimelere uyan
bağlantıları çıkarmak, gerekirse sitemap.xml'e düşmek. Mantık burada bir kere yazılır.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from ..net import Fetcher
from ..textutil import fold

# Peşine düşülmeyecek bağlantılar: sosyal medya, medya dosyaları, haber/blog içeriği.
SKIP_LINK = re.compile(
    r"(facebook|twitter|x\.com|instagram|linkedin\.com/(?!jobs)|youtube|whatsapp|"
    r"mailto:|tel:|javascript:|\.pdf$|\.jpg$|\.png$|/blog/|/haber|/news/)",
    re.I,
)


def host_of(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    return host[4:] if host.startswith("www.") else host


def same_site(url: str, domain: str) -> bool:
    host = host_of(url)
    return host == domain or host.endswith("." + domain)


def match_haystack(*parts: str) -> str:
    """Eşleştirme metni: Türkçe katlanmış, ayraçlar boşluğa çevrilmiş.

    URL'ler 'insan-kaynaklari' yazarken anahtar kelime 'insan kaynak' olduğu için
    tire/alt tire/eğik çizgi boşluğa dönüştürülmeden eşleşme kaçıyor (Faz 0'da
    kariyer sayfası bulmada tam bu yüzden gerçek bir hata çıkmıştı).
    """
    text = fold(" ".join(p for p in parts if p))
    return re.sub(r"[-_/.]+", " ", text)


def find_links(html: str, base_url: str, domain: str, keywords: tuple[str, ...],
               allow_external: bool = False) -> list[str]:
    """Sayfadaki, verilen anahtar kelimelerden birine uyan bağlantıları çıkarır.

    `allow_external=True` iken kendi alan adı dışındaki bağlantılar da kabul edilir
    (kariyer sayfası tespitinde dış ATS barındırıcıları için gerekiyordu); iletişim
    sayfası aramasında bu yanlış eşleşme riski taşıdığı için varsayılan kapalı.
    """
    try:
        tree = HTMLParser(html)
    except Exception:  # selectolax nadiren bozuk HTML'de patlar
        return []

    found: dict[str, None] = {}
    for node in tree.css("a"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith("#") or SKIP_LINK.search(href):
            continue
        haystack = match_haystack(href, node.text() or "")
        if not any(keyword in haystack for keyword in keywords):
            continue
        absolute = urljoin(base_url, href).split("#")[0]
        if not absolute.startswith(("http://", "https://")):
            continue
        if not allow_external and not same_site(absolute, domain):
            continue
        found.setdefault(absolute, None)
    return list(found)


def find_in_sitemap(fetcher: Fetcher, domain: str, keywords: tuple[str, ...],
                    limit: int = 3) -> list[str]:
    """Ana sayfada bağlantı yoksa sitemap.xml'de anahtar kelimeye uyan yolları arar.

    JS ile menü kuran sitelerde ana sayfanın HTML'inde hiç <a> olmayabiliyor;
    sitemap bu durumda tek metin tabanlı kaynak.
    """
    result = fetcher.get(f"https://{domain}/sitemap.xml")
    if not result.ok:
        return []
    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", result.text, re.I)
    hits = []
    for url in urls:
        if any(keyword in match_haystack(url) for keyword in keywords):
            hits.append(url)
            if len(hits) >= limit:
                break
    return hits
