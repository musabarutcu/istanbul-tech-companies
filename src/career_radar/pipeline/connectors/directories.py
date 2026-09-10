"""Dizin bağlayıcısı: teknokent kiracı listeleri, VC portföyleri, dernek üye listeleri.

Bu dosyada her site için ayrı parser YOK. Onun yerine YAML ile yapılandırılan tek bir
çıkarıcı var; yeni kaynak eklemek = `seeds/directories.yaml`'a birkaç satır yazmak.
18 farklı siteye 18 ayrı Python modülü yazmak, siteler değiştiğinde 18 ayrı bakım borcu
demek olurdu.

Üç çıkarma kipi:
  structured  — listede hem isim hem site var (Dijitalpark, Yıldız Teknopark)
  detail      — listede isim + detay bağlantısı var, site detay sayfasında (Teknopol)
  outbound    — yapı bilinmiyor: sayfadaki dış bağlantılardan firma sitesi olabilecekleri
                ayıklar. Düşük hassasiyet, yüksek kapsama; elle haritalanmamış kaynaklar için.

Sayfalama, sayfa numarası artırılarak keşfedilir; site parametreyi yok sayarsa (aynı
içerik tekrar gelirse) durulur. Böylece her kaynak için "kaç sayfa var" bilgisini elle
tutmak gerekmiyor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlencode, urljoin, urlparse, urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from ...net import Fetcher
from ...textutil import domain_from_url

# Firma sitesi olamayacak barındırıcılar: sosyal medya, altyapı, resmî kayıt siteleri.
# medium.com/substack.com: blog barındırma, firmanın kendi sitesi olamaz.
# junipersquare/carta: yatırımcı portalı SaaS'ı, dizin sahibinin kendi aracı.
# list-manage/eepurl/mailchimp: bülten kayıt formu; fliphtml5: rapor/flipbook görüntüleyici.
_NOT_A_COMPANY_SITE = re.compile(
    r"(facebook|twitter|(?<![a-z0-9])x\.com|instagram|linkedin|youtube|whatsapp|pinterest|tiktok|"
    r"google\.|gstatic|googleapis|cloudflare|jquery|bootstrap|fontawesome|"
    r"\.gov\.tr|\.edu\.tr|mersis|ticaretsicil|mkk\.com\.tr|ttr\.com\.tr|"
    r"apple\.com|play\.google|adobe\.com|w3\.org|schema\.org|"
    r"medium\.com|substack\.com|junipersquare\.com|carta\.com|"
    r"list-manage\.com|eepurl\.com|mailchimp\.com|fliphtml5\.com)",
    re.I,
)

# Firma sitesi değil, indirilebilir belge: KID, GDPR formu, sunum vb.
_DOCUMENT_FILE = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip)(\?|#|$)", re.I)

_URLISH_TEXT = re.compile(r"^\s*(https?://|www\.)", re.I)


@dataclass
class DirectoryEntry:
    name: str
    website: str | None = None
    sector: str | None = None
    detail_url: str | None = None
    listed_url: str | None = None


@dataclass
class DirectorySource:
    key: str
    kind: str
    name: str
    list_url: str
    city: str | None = None
    mode: str = "structured"                 # structured | detail | outbound
    item_selector: str | None = None
    name_selector: str | None = None
    name_attr: str | None = None             # ör. anchor üzerindeki "title"
    website_selector: str = "a"
    sector_selector: str | None = None
    sector_attr: str | None = None           # sektör tam metni çoğu zaman title'da olur
    detail_selector: str = "a"
    page_param: str | None = None            # ör. "page" -> ?page=2
    max_pages: int = 40
    enabled: bool = True
    note: str | None = None
    extra: dict = field(default_factory=dict)


def _text(node) -> str | None:
    if node is None:
        return None
    value = re.sub(r"\s+", " ", node.text() or "").strip()
    return value or None


def _attr(node, name: str) -> str | None:
    if node is None:
        return None
    value = (node.attributes.get(name) or "").strip()
    return value or None


def is_company_site(url: str, directory_host: str) -> bool:
    """Bir dış bağlantı firma sitesi olabilir mi?"""
    if not url.startswith(("http://", "https://")):
        return False
    if _NOT_A_COMPANY_SITE.search(url):
        return False
    if _DOCUMENT_FILE.search(urlparse(url).path):
        return False
    host = (urlparse(url).netloc or "").lower().replace("www.", "")
    if not host or "." not in host:
        return False
    directory_root = directory_host.replace("www.", "")
    if host == directory_root or host.endswith("." + directory_root):
        return False
    return True


def _page_url(source: DirectorySource, page: int) -> str:
    if page <= 1 or not source.page_param:
        return source.list_url
    parts = urlsplit(source.list_url)
    query = parts.query
    joined = f"{query}&" if query else ""
    joined += urlencode({source.page_param: page})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, joined, parts.fragment))


# ------------------------------------------------------------------ çıkarıcılar


def extract_structured(tree: HTMLParser, source: DirectorySource,
                       base_url: str) -> list[DirectoryEntry]:
    """Listede hem isim hem (varsa) site bulunan kaynaklar."""
    host = urlparse(base_url).netloc
    entries: list[DirectoryEntry] = []

    for item in tree.css(source.item_selector or "body"):
        anchor = item.css_first(source.website_selector) if source.website_selector else None

        name = None
        if source.name_selector:
            name = _text(item.css_first(source.name_selector))
        if not name and source.name_attr and anchor is not None:
            name = _attr(anchor, source.name_attr)
        if not name and anchor is not None:
            name = _attr(anchor, "title") or _text(anchor)
        if not name:
            image = item.css_first("img")
            name = _attr(image, "alt")

        website = None
        href = _attr(anchor, "href") if anchor is not None else None
        if href:
            absolute = urljoin(base_url, href)
            if is_company_site(absolute, host):
                website = absolute

        # Bazı dizinler firma adını yalnızca logo görselinde taşıyor (Yıldız Teknopark
        # arka plan görseli kullanıyor, alt metni yok). Site elimizdeyken kaydı düşürmek
        # yerine alan adını geçici isim yapıyoruz; sınıflandırma aşaması düzeltir.
        if not name and website:
            name = domain_from_url(website)
        if not name:
            continue

        entries.append(DirectoryEntry(
            name=name,
            website=website,
            sector=_read_sector(item, source),
            listed_url=base_url,
        ))
    return entries


def _read_sector(item, source: DirectorySource) -> str | None:
    """Sektör metni. Görünen metin çoğu dizinde kısaltılmış ('... +1'); tam hali title'da."""
    if not source.sector_selector:
        return None
    node = item.css_first(source.sector_selector)
    if node is None:
        return None
    if source.sector_attr:
        value = _attr(node, source.sector_attr)
        if value:
            return value
    return _text(node)


def extract_detail_links(tree: HTMLParser, source: DirectorySource,
                         base_url: str) -> list[DirectoryEntry]:
    """Listede isim + detay sayfası bağlantısı olan kaynaklar."""
    entries: list[DirectoryEntry] = []
    for item in tree.css(source.item_selector or "body"):
        anchor = item.css_first(source.detail_selector)
        if anchor is None:
            continue
        href = _attr(anchor, "href")
        name = _attr(anchor, "title") or _text(anchor)
        if not href or not name:
            continue
        entries.append(DirectoryEntry(
            name=name,
            detail_url=urljoin(base_url, href),
            sector=_read_sector(item, source),
            listed_url=base_url,
        ))
    return entries


def extract_outbound(tree: HTMLParser, source: DirectorySource,
                     base_url: str) -> list[DirectoryEntry]:
    """Yapı bilinmeyen kaynaklar: dış bağlantılardan firma sitesi adaylarını ayıklar.

    İsim olarak bağlantının title'ı, metni veya görselinin alt'ı kullanılır; hiçbiri
    yoksa alan adının kendisi isim olur (sınıflandırma aşaması sonra düzeltir).

    `item_selector` verilirse tarama sayfanın tamamı yerine yalnızca o kapsayıcının
    içindeki bağlantılarla sınırlanır (ör. bir portföy logosu karuseli) — nav/footer/
    haber bağlantılarının şirket sanılmasını önler. Verilmezse eski davranış: tüm sayfa.
    """
    host = urlparse(base_url).netloc
    seen: dict[str, DirectoryEntry] = {}

    if source.item_selector:
        anchors = [a for root in tree.css(source.item_selector) for a in root.css("a")]
    else:
        anchors = tree.css("a")

    for anchor in anchors:
        href = _attr(anchor, "href")
        if not href:
            continue
        absolute = urljoin(base_url, href)
        if not is_company_site(absolute, host):
            continue
        domain = domain_from_url(absolute)
        if not domain or domain in seen:
            continue

        name = _attr(anchor, "title") or _text(anchor)
        if not name or _URLISH_TEXT.match(name):
            image = anchor.css_first("img")
            name = _attr(image, "alt") or _attr(image, "title") or name
        if not name or _URLISH_TEXT.match(name or ""):
            name = domain
        if len(name) < 2 or len(name) > 120:
            continue

        seen[domain] = DirectoryEntry(name=name.strip(), website=absolute, listed_url=base_url)
    return list(seen.values())


_EXTRACTORS = {
    "structured": extract_structured,
    "detail": extract_detail_links,
    "outbound": extract_outbound,
}


# -------------------------------------------------------------------- yürütücü


def _is_url_like(text: str | None, absolute: str) -> bool:
    """Bağlantı metni, kendi hedefinin adresi mi? ("https://x.com" ya da çıplak "x.com")

    Bazı dizinler (ör. SASAD) adresi şema olmadan basar: "Web Adresi: adik.com.tr".
    Yalnızca şema kontrolü bu durumu kaçırır ve site genelindeki sabit bir tanıtım
    bağlantısı (reklam, sponsor) ilk-eşleşme olarak yanlışlıkla seçilebilir.
    """
    if not text:
        return False
    if _URLISH_TEXT.match(text):
        return True
    domain = domain_from_url(absolute)
    if not domain:
        return False
    candidate = text.strip().lower().rstrip("/")
    candidate = candidate.removeprefix("www.")
    return candidate == domain


def resolve_website_from_detail(fetcher: Fetcher, detail_url: str) -> str | None:
    """Detay sayfasında firma sitesini bulur.

    En güvenilir ipucu: bağlantı metninin kendisinin bir URL olması (siteler firma
    adresini çoğu zaman ham URL olarak basıyor). Bulunamazsa ilk makul dış bağlantı.
    """
    page = fetcher.get(detail_url)
    if not page.ok:
        return None
    tree = HTMLParser(page.text)
    host = urlparse(detail_url).netloc

    fallback = None
    for anchor in tree.css("a"):
        href = _attr(anchor, "href")
        if not href:
            continue
        absolute = urljoin(detail_url, href)
        if not is_company_site(absolute, host):
            continue
        if _is_url_like(_text(anchor), absolute):
            return absolute
        if fallback is None:
            fallback = absolute
    return fallback


def crawl(fetcher: Fetcher, source: DirectorySource,
          resolve_details: bool = True,
          on_page=None) -> list[DirectoryEntry]:
    """Bir dizin kaynağını baştan sona gezer ve firma kayıtlarını döndürür."""
    extractor = _EXTRACTORS[source.mode]
    collected: dict[str, DirectoryEntry] = {}
    previous_signature: tuple | None = None

    for page in range(1, source.max_pages + 1):
        url = _page_url(source, page)
        result = fetcher.get(url)
        if not result.ok:
            break

        tree = HTMLParser(result.text)
        entries = extractor(tree, source, result.url)
        if not entries:
            break

        # Site sayfa parametresini yok sayıyorsa aynı içerik döner; sonsuz döngüyü kes.
        signature = tuple(e.name for e in entries[:10])
        if signature == previous_signature:
            break
        previous_signature = signature

        for entry in entries:
            key = (domain_from_url(entry.website) or entry.name.lower().strip())
            collected.setdefault(key, entry)

        if on_page:
            on_page(page, len(entries), len(collected))
        if not source.page_param:
            break

    entries = list(collected.values())

    if resolve_details:
        for entry in entries:
            if entry.website is None and entry.detail_url:
                entry.website = resolve_website_from_detail(fetcher, entry.detail_url)

    return entries


def load_sources(path) -> list[DirectorySource]:
    import yaml
    from pathlib import Path

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [DirectorySource(**entry) for entry in data.get("sources", [])]
