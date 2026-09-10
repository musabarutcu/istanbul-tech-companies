"""API istek/yanıt modelleri.

DB satırlarından ayrı tutuluyorlar: veritabanı şeması değişince arayüz sözleşmesi
kendiliğinden değişmesin diye. Tasarım turunda frontend baştan yazılsa bile bu
dosyadaki şekiller sabit kalır.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SourceRef(BaseModel):
    key: str
    name: str
    kind: str


class ContactOut(BaseModel):
    name: str | None = None
    role: str | None = None
    email: str | None = None
    phone: str | None = None
    contact_type: str
    source_url: str
    extraction_method: str


class JobOut(BaseModel):
    title: str
    location: str | None = None
    url: str | None = None
    is_internship: bool = False
    posted_at: str | None = None


class CompanyListItem(BaseModel):
    id: int
    name: str
    domain: str | None = None  # web sitesi alan adı — aşağıdaki `categories` ile karıştırma
    city: str | None = None
    website: str | None = None
    careers_url: str | None = None
    summary: str | None = None
    size_bucket: str | None = None
    sector: str | None = None
    # Kendi taksonomimizden (taxonomy.yaml) çok etiketli sınıflandırma: ai_ml,
    # web_saas, quantum_derin_tek... `sectors` alanından farklı — o, dizin
    # kaynağının kendi (tutarsız) sektör metnini taşır; bu, bizim kapalı
    # kümemizden LLM ile doğrulanmış etiketleri taşır.
    categories: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    open_jobs_count: int = 0
    has_internship: bool = False
    email_count: int = 0
    hr_email_count: int = 0
    detect_status: str | None = None
    user_status: str | None = None
    first_seen: str | None = None


class CompanyDetail(CompanyListItem):
    ats_provider: str | None = None
    detect_note: str | None = None
    user_note: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)
    contacts: list[ContactOut] = Field(default_factory=list)
    jobs: list[JobOut] = Field(default_factory=list)


class CompanyPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[CompanyListItem]


class FacetValue(BaseModel):
    value: str
    label: str
    count: int


class Facets(BaseModel):
    cities: list[FacetValue]
    sources: list[FacetValue]
    sectors: list[FacetValue]
    categories: list[FacetValue]
    statuses: list[FacetValue]


class CompanyPatch(BaseModel):
    user_status: str | None = None
    user_note: str | None = None


class Stats(BaseModel):
    companies: int
    with_domain: int
    with_careers_url: int
    with_ats: int
    with_email: int
    with_hr_email: int
    with_open_jobs: int
    with_internship: int
    contacts: int
    jobs_active: int
    labels: int
    sources: int
