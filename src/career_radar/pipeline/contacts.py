"""İletişim katmanı: şirketin kendi sitesinden e-posta/telefon/isim toplar.

Kaynak politikası (plan'da kararlaştırıldı, burada uygulanıyor):
  - Veri YALNIZCA şirketin kendi alan adından toplanır. Üçüncü taraf toplayıcı,
    LinkedIn kazıma, e-posta tahmin/doğrulama servisi YOK.
  - Her kayıt `source_url` taşır — panoda "nereden geldi" bağlantısı gösterilebilir.
  - E-posta ADRESİ TÜRETİLMEZ. Sayfada birebir yazmayan hiçbir şey çıkarılmaz.

İki geçiş:
  1. Regex geçişi (bu dosyada, ağsız test edilebilir, API anahtarı gerektirmez):
     mailto: bağlantıları + sayfa metnindeki e-postalar + telefon numaraları.
     Rol tabanlı kutuları (`ik@`, `kariyer@`, `staj@`) ve genel kutuları (`info@`)
     yakalar — planın en yüksek kapsamalı, en güvenilir katmanı budur.
  2. İsimli kişi geçişi (CEO, kurucu, İK yetkilisi) yargı gerektirir: sayfadaki
     hangi ismin hangi role ait olduğunu anlamak context'e bakmayı gerektiriyor,
     regex bunu güvenilir yapamaz. Bu yüzden ayrı bir LLM adımına bırakılıyor
     (`extraction.py`) — API anahtarı yoksa bu adım atlanır, regex sonucu kalır.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from ..net import Fetcher
from ..textutil import classify_email, find_emails, registrable_domain
from . import linkfind

# İletişim bilgisi taşıma ihtimali yüksek sayfalar. "kariyer" burada da var çünkü
# kariyer sayfalarının çoğu altına bir İK e-postası/telefonu koyuyor.
CONTACT_KEYWORDS = (
    "iletisim", "contact", "hakkimizda", "about", "ekip", "team", "yonetim",
    "kurumsal", "insan kaynak", "kariyer", "career", "bize ulasin", "get in touch",
)

# Bir sayfanın "kurumsal hub" olma ihtimalini CONTACT_KEYWORDS'ten daha güçlü
# işaret eden alt küme — ikinci atlamada genişletilecek adayları seçerken
# kullanılır. Örn. büyük e-ticaret sitelerinde "kariyer" eşleşmesiyle yakalanan
# bir ürün kategori sayfası genişletilmemeli; asıl hedef "kurumsal.site.com"
# gibi hub'lar.
HUB_KEYWORDS = ("kurumsal", "hakkimizda", "about", "corporate", "yonetim")

CONTACT_PATHS = (
    "/iletisim", "/contact", "/hakkimizda", "/about", "/about-us",
    "/ekibimiz", "/team", "/kurumsal", "/tr/iletisim",
)

MAX_CANDIDATE_PAGES = 8
# Ana sayfadan bulunan bağlantılardan kaçı "hub" sayılıp bir adım daha izlenecek.
# Büyük kurumsal sitelerde ("kurumsal.hepsiburada.com" gibi) gerçek yönetim/ekip
# sayfası ana sayfadan değil, önce bir "Hakkımızda/Kurumsal" hub'ından linkleniyor;
# tek atlamalı keşif bu yüzden yeterli değildi (gerçek veride görüldü — Hepsiburada'nın
# "Üst Yönetim" sayfası hiç aday listesine girmiyordu).
MAX_HUB_PAGES_TO_EXPAND = 2

# Basit TR/uluslararası telefon deseni: +90 ile veya 0 ile başlayan, ayraçlı 10-11 haneli.
_PHONE_RE = re.compile(
    r"(?:\+90|0)?\s?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}\b"
)
# Ters eğik çizgi de dışlanıyor: gömülü JSON'da `mailto:info@acme.com\"` gibi
# kaçışlı bir href, çizgi hariç tutulmazsa adresin sonuna yapışıp
# `info@acme.com\` gibi bozuk bir sonuç üretiyordu (gerçek veride görüldü).
_MAILTO_RE = re.compile(r'mailto:([^"\'\s?\\]+)', re.I)


def is_own_domain_email(email: str, company_domain: str) -> bool:
    """E-posta gerçekten bu şirketin alan adından mı?

    Tek kural, tek seferde şu gürültü kaynaklarının hepsini eler: gömülü üçüncü
    taraf script'lerden sızan adresler (Sentry ingest ID'leri, ortak platform
    sağlayıcıları), sayfadaki yer tutucu/örnek adresler (`ornek@mail.com`,
    `email@example.com`) — hiçbiri şirketin kendi alan adıyla eşleşmez.
    KEP (Kayıtlı Elektronik Posta) adresleri istisna: `*.kep.tr` farklı bir
    alan adında barınır ama şirketin resmî ve gerçek iletişim kutusudur.
    """
    email_domain = email.rsplit("@", 1)[-1].lower()
    if email_domain.endswith(".kep.tr") or email_domain == "kep.tr":
        return True
    root = registrable_domain(company_domain)
    return email_domain == company_domain or (bool(root) and email_domain.endswith("." + root)) \
        or email_domain == root


@dataclass
class ContactCandidate:
    email: str
    phone: str | None = None
    source_url: str = ""


@dataclass
class ContactHarvest:
    domain: str
    pages_fetched: list[str] = field(default_factory=list)
    contacts: list[ContactCandidate] = field(default_factory=list)
    blocked: bool = False
    error: str | None = None


def find_candidate_pages(fetcher: Fetcher, domain: str, website: str | None,
                         seed_urls: tuple[str, ...] = ()) -> tuple[list[str], bool, str | None]:
    """İletişim bilgisi taşıması muhtemel sayfaları bulur.

    `seed_urls`: zaten bilinen sayfalar (ör. detect aşamasından gelen careers_url) —
    onlardan da e-posta çıkabilir, ayrıca fetch etmeye gerek kalmadan aday listesine
    eklenir.
    """
    base = website or f"https://{domain}/"
    home = fetcher.get(base)
    if home.blocked_by_robots:
        return [], True, None
    if not home.ok:
        home = fetcher.get(f"https://www.{domain}/")
        if not home.ok:
            return [], False, home.error or f"HTTP {home.status}"

    candidates = linkfind.find_links(home.text, home.url, domain, CONTACT_KEYWORDS)
    if not candidates:
        candidates = linkfind.find_in_sitemap(fetcher, domain, CONTACT_KEYWORDS)

    # İkinci atlama: ana sayfadan bulunan ilk birkaç sayfayı da tara — gerçek
    # yönetim/ekip sayfası çoğu zaman doğrudan ana sayfadan değil, bir "Hakkımızda/
    # Kurumsal" hub'ından linkleniyor. Genişletilecek adaylar belge sırasına göre
    # DEĞİL, HUB_KEYWORDS eşleşmesine göre öncelendirilir — yoksa büyük e-ticaret
    # sitelerinde "kariyer" eşleşmesiyle yakalanmış bir ürün kategori sayfası
    # (ör. "muzik", "projeksiyon") ilk sıraya geçip asıl hub'ı devre dışı bırakır
    # (gerçek veride tam olarak bu yaşandı — Hepsiburada'nın "kurumsal" hub'ı
    # listede vardı ama önce gelen alakasız sayfalar bütçeyi tüketiyordu).
    prioritized = sorted(
        candidates,
        key=lambda url: 0 if any(kw in linkfind.match_haystack(url) for kw in HUB_KEYWORDS) else 1,
    )
    for hub_url in prioritized[:MAX_HUB_PAGES_TO_EXPAND]:
        hub = fetcher.get(hub_url)
        if not hub.ok:
            continue
        for deeper in linkfind.find_links(hub.text, hub.url, domain, CONTACT_KEYWORDS):
            if deeper not in candidates:
                candidates.append(deeper)

    for path in CONTACT_PATHS:
        url = urljoin(f"https://{domain}/", path)
        if url not in candidates:
            candidates.append(url)
    for url in seed_urls:
        if url not in candidates:
            candidates.insert(0, url)

    all_pages = [home.url] + [c for c in candidates if c != home.url]
    return all_pages[:MAX_CANDIDATE_PAGES], False, None


def extract_regex_contacts(html: str, page_url: str,
                          company_domain: str | None = None) -> list[ContactCandidate]:
    """Bir sayfadan mailto bağlantıları, metindeki e-postalar ve telefonları çıkarır.

    Yalnızca sayfada birebir yazan değerler döner — hiçbir alan türetilmez/tahmin
    edilmez. Dosya adı gibi görünen sahte e-postalar (`logo@2x.png`) textutil'de
    zaten eleniyor. `company_domain` verilirse yalnızca o alan adından (veya
    `.kep.tr`'den) gelen adresler tutulur — bu tek kural, gömülü üçüncü taraf
    script'lerden sızan adresleri (Sentry ID'leri, platform sağlayıcıları) ve
    sayfadaki yer tutucu örnek adresleri (`ornek@mail.com`) tek seferde eler.
    Telefon tek başına saklanmaz (isimsiz/e-postasız bir kayıt `repo.save_contacts`
    tarafından zaten reddedilir); bulunursa aynı sayfadaki ilk e-postaya
    iliştirilir — "bu kutuya yaz ya da bu numarayı ara" bir arada.
    """
    emails: dict[str, None] = {}
    for match in _MAILTO_RE.finditer(html):
        address = match.group(1).split("?")[0].strip().lower()
        if "@" in address:
            emails.setdefault(address, None)
    for address in find_emails(html):
        emails.setdefault(address, None)

    if company_domain:
        emails = {e: None for e in emails if is_own_domain_email(e, company_domain)}

    if not emails:
        return []

    phone_match = _PHONE_RE.search(html)
    phone = phone_match.group(0).strip() if phone_match else None

    found = [ContactCandidate(email=email, source_url=page_url) for email in emails]
    if phone:
        found[0].phone = phone  # sayfadaki ilk e-postaya iliştir
    return found


def harvest_regex(fetcher: Fetcher, domain: str, website: str | None,
                  seed_urls: tuple[str, ...] = ()) -> ContactHarvest:
    """Bir şirket için regex tabanlı iletişim taramasını uçtan uca çalıştırır."""
    result = ContactHarvest(domain=domain)
    pages, blocked, error = find_candidate_pages(fetcher, domain, website, seed_urls)
    result.blocked = blocked
    result.error = error
    if not pages:
        return result

    seen_emails: set[str] = set()
    for url in pages:
        page = fetcher.get(url)
        if page.blocked_by_robots or not page.ok:
            continue
        result.pages_fetched.append(page.url)
        for candidate in extract_regex_contacts(page.text, page.url, company_domain=domain):
            if candidate.email in seen_emails:
                continue
            seen_emails.add(candidate.email)
            result.contacts.append(candidate)
    return result


def to_contact_records(harvest: ContactHarvest) -> list[dict]:
    """`repo.save_contacts`'ın beklediği kayıt biçimine çevirir."""
    return [
        {
            "email": candidate.email,
            "phone": candidate.phone,
            "contact_type": classify_email(candidate.email),
            "source_url": candidate.source_url,
            "extraction_method": "regex",
        }
        for candidate in harvest.contacts
    ]
