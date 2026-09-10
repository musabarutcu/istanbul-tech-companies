"""İTÜ ARI Teknokent'in firma detay AJAX'ı — website alanını çözer.

Genel dizin listesi (`seeds/directories.yaml`'daki `ari_teknokent` kaynağı,
structured kip) firma web sitesini vermiyor — liste sayfası yalnızca isim ve
sektör taşıyor, gerçek site "firma kartına tıklanınca açılan modal" ile
gösteriliyor. Tarayıcıyla incelenip bulundu: her kartın `data-row-id`'si,
`GET /tr/getCompanyInformations?rowID=<id>` uç noktasına gönderiliyor ve bu
JSON döndürüyor: `{"company": {"title", "website", "website_url", "email", ...}}`.

Bu, tek bir kaynağa özgü iki aşamalı bir çözümleme olduğu için genel
`DirectorySource` soyutlamasına zorlanmadı — ayrı, küçük bir modül.
Bulunan siteler `repo.upsert_company`'nin isim eşleştirmeli zenginleştirme
yoluyla (COALESCE) mevcut kayıtlara yazılır; yeni şirket oluşturmaz.
"""

from __future__ import annotations

from dataclasses import dataclass

from selectolax.parser import HTMLParser

from ...net import Fetcher

LIST_URL = "https://www.ariteknokent.com.tr/tr/teknoloji-firmalari/teknokentli-firmalar"
INFO_URL = "https://www.ariteknokent.com.tr/tr/getCompanyInformations"


@dataclass
class AriRow:
    title: str
    row_id: str


def crawl_row_ids(fetcher: Fetcher, max_pages: int = 30) -> list[AriRow]:
    """Listeleme sayfalarını gezip (başlık, data-row-id) çiftlerini toplar.

    Sayfalama `seeds/directories.yaml`'daki ari_teknokent kaynağıyla aynı mantık
    (page= parametresi, yeni kayıt gelmeyince dur) — burada ayrıca yazılma
    nedeni, genel `crawl()`'ın `data-row-id` özniteliğini hiç saklamamasıdır.
    """
    rows: list[AriRow] = []
    seen_ids: set[str] = set()
    for page in range(1, max_pages + 1):
        url = LIST_URL if page == 1 else f"{LIST_URL}?page={page}"
        result = fetcher.get(url)
        if not result.ok:
            break
        tree = HTMLParser(result.text)
        cards = tree.css("div.company-card")
        if not cards:
            break
        new_this_page = 0
        for card in cards:
            row_id = card.attributes.get("data-row-id")
            title = (card.attributes.get("title") or "").strip()
            if row_id and title and row_id not in seen_ids:
                seen_ids.add(row_id)
                rows.append(AriRow(title=title, row_id=row_id))
                new_this_page += 1
        if new_this_page == 0:  # site sayfa parametresini yok sayıyor olabilir
            break
    return rows


def fetch_company_info(fetcher: Fetcher, row_id: str) -> dict | None:
    """Tek bir firmanın AJAX yanıtındaki ham `company` nesnesini döner."""
    data = fetcher.get_json(f"{INFO_URL}?rowID={row_id}", use_cache=True)
    if not isinstance(data, dict):
        return None
    company = data.get("company")
    return company if isinstance(company, dict) else None


def resolve_website(company_info: dict) -> str | None:
    """`website_url` (mutlak) tercih edilir; yoksa ham `website` alanına düşülür."""
    return company_info.get("website_url") or company_info.get("website") or None
