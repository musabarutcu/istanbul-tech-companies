"""Kariyer sayfası bulma ve ATS tespiti.

Akış:  domain -> ana sayfa -> kariyer bağlantısı adayları -> HTML'de ATS imzası -> slug
        (bağlantı bulunamazsa) -> sabit yollar -> sitemap.xml

Tespit bir kez yapılır ve kalıcı yazılır; sonraki hasat koşuları doğrudan ATS uç
noktasına gider, siteyi tekrar dolaşmaz.

Sayfa keşif mekanizması (`linkfind.py`) `contacts.py` ile ortak — burada yalnızca
kariyer'e özgü anahtar kelimeler ve dış ATS barındırıcı listesi tanımlı.
"""

from __future__ import annotations

from urllib.parse import urljoin

from ..net import Fetcher
from . import linkfind
from .connectors import ats

# Sık kullanılan sabit yollar. Ana sayfada bağlantı bulunamazsa bunlar denenir.
CAREER_PATHS = (
    "/kariyer",
    "/careers",
    "/career",
    "/jobs",
    "/tr/kariyer",
    "/en/careers",
    "/insan-kaynaklari",
    "/tr/insan-kaynaklari",
    "/acik-pozisyonlar",
    "/join-us",
)

# Anahtar kelimeler ASCII'ye indirgenmiş halde tutulur; eşleştirme de fold() ile yapılır.
CAREER_KEYWORDS = (
    "kariyer", "career", "jobs", "job", "is ilan", "acik pozisyon",
    "insan kaynak", "join us", "work with us", "bize katil", "aramiza katil",
    "staj", "we are hiring", "is basvuru",
)

# Kendi alan adımızda olmasa bile kariyer sayfası sayılan barındırıcılar.
_CAREER_HOSTS = (
    "careers-page.com", "kariyer.net", "youthall.com", "toptalent.co",
    "successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu",
    "myworkdayjobs.com", "taleo.net", "icims.com", "isbasi.com",
)

MAX_CANDIDATE_PAGES = 6


class DetectResult:
    def __init__(self, domain: str):
        self.domain = domain
        self.careers_url: str | None = None
        self.ats_provider: str | None = None
        self.ats_slug: str | None = None
        self.pages_fetched: list[str] = []
        self.blocked = False
        self.protected = False          # 403/429 — bot koruması, tarayıcı gerekir
        self.error: str | None = None

    @property
    def found_ats(self) -> bool:
        return bool(self.ats_provider and self.ats_slug)


def is_known_career_host(url: str) -> bool:
    """Dış alan adı olsa bile kariyer sayfası/ATS barındırdığını bildiğimiz adresler."""
    host = linkfind.host_of(url)
    if any(known in host for known in _CAREER_HOSTS):
        return True
    return ats.detect_in_html(url) is not None


def career_links_in(html: str, base_url: str, domain: str) -> list[str]:
    """Sayfadaki kariyer/İK bağlantılarını çıkarır (href veya bağlantı metni eşleşmesi)."""
    candidates = linkfind.find_links(html, base_url, domain, CAREER_KEYWORDS,
                                     allow_external=True)
    # allow_external=True dış siteleri de getirir; yalnızca bilinen ATS/kariyer
    # barındırıcılarını tutuyoruz, alakasız dış bağlantıları eliyoruz.
    return [url for url in candidates
            if linkfind.same_site(url, domain) or is_known_career_host(url)]


def career_links_in_sitemap(fetcher: Fetcher, domain: str, limit: int = 3) -> list[str]:
    """Ana sayfada bağlantı yoksa sitemap.xml'de kariyer yollarını arar."""
    return linkfind.find_in_sitemap(fetcher, domain, CAREER_KEYWORDS, limit=limit)


def detect_company(fetcher: Fetcher, domain: str, website: str | None = None) -> DetectResult:
    """Bir şirketin kariyer sayfasını ve varsa ATS'sini tespit eder."""
    result = DetectResult(domain=domain)
    base = website or f"https://{domain}/"

    home = fetcher.get(base)
    if home.blocked_by_robots:
        result.blocked = True
        return result
    if not home.ok:
        if home.status in (403, 429):
            result.protected = True
            result.error = f"HTTP {home.status}"
            return result
        home = fetcher.get(f"https://www.{domain}/")  # www farkını bir kez dene
        if not home.ok:
            result.protected = home.status in (403, 429)
            result.error = home.error or f"HTTP {home.status}"
            return result

    result.pages_fetched.append(home.url)

    # 1) Ana sayfada doğrudan ATS imzası olabilir (footer/nav'daki board bağlantısı).
    hit = ats.detect_in_html(home.text)
    if hit:
        result.ats_provider, result.ats_slug = hit

    # 2) Adayları topla: sayfadaki bağlantılar > sabit yollar > sitemap.
    candidates = career_links_in(home.text, home.url, domain)
    if not candidates:
        candidates = career_links_in_sitemap(fetcher, domain)
    for path in CAREER_PATHS:
        candidate = urljoin(f"https://{domain}/", path)
        if candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates[:MAX_CANDIDATE_PAGES]:
        page = fetcher.get(candidate)
        if page.blocked_by_robots or not page.ok:
            continue
        result.pages_fetched.append(page.url)
        if result.careers_url is None:
            result.careers_url = page.url
        if not result.found_ats:
            hit = ats.detect_in_html(page.text)
            if hit:
                result.ats_provider, result.ats_slug = hit
        if result.found_ats and result.careers_url:
            break

    return result


def verify_ats(fetcher: Fetcher, provider: str, slug: str) -> int | None:
    """Tespit edilen ATS canlıda gerçekten veri dönüyor mu?

    İlan sayısını döner; uç nokta cevap vermiyorsa None. Tespit regex'i yanlış slug
    yakalayabildiği için (ör. footer'daki başka bir şirketin board bağlantısı) bu adım
    şart. `list_jobs`'un boş liste ile None ayrımı tam da bunun için var: 0 ilan geçerli
    bir cevaptır, None ise slug'ın yanlış olduğunu söyler.
    """
    try:
        jobs = ats.list_jobs(fetcher, provider, slug)
    except Exception:
        return None
    return None if jobs is None else len(jobs)
