"""ATS (işe alım yazılımı) bağlayıcıları.

Kariyer sayfalarının çoğu bir ATS'nin gömülü halidir ve bu sistemlerin kimlik doğrulaması
istemeyen, yapılandırılmış veri dönen uç noktaları vardır. HTML kazımak yerine bunları
kullanmak hem kırılmaz hem de robots.txt açısından temiz.

Her sağlayıcı üç şey tanımlar:
  1. patterns  — HTML içinde kendi imzasını arar, şirketin slug'ını çıkarır (tespit)
  2. jobs_url  — o slug için veri uç noktası
  3. parse     — dönen gövdeyi RawJob listesine çevirir

ÖNEMLİ: list_jobs, uç nokta cevap vermediğinde `None` döner; boş ilan panosunda ise `[]`.
Bu ayrım kritik — yanlış yakalanmış bir slug ile gerçekten ilanı olmayan bir şirket
aksi halde birbirine karışır ve tespit doğrulaması işe yaramaz.

Sekiz sağlayıcı tek dosyada; her biri ~20 satır olduğu için ayrı modüllere bölmek
okumayı zorlaştırmaktan başka işe yaramıyor. Yeni sağlayıcı = alta bir sınıf + REGISTRY.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from ...net import Fetcher


@dataclass
class RawJob:
    external_id: str
    title: str
    location: str | None = None
    url: str | None = None
    posted_at: str | None = None
    remote: bool | None = None


def _epoch_ms_to_iso(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class Provider:
    key: str = ""
    patterns: tuple[re.Pattern, ...] = ()
    # Slug olarak yakalanmaması gereken jenerik yollar.
    slug_blacklist: frozenset[str] = frozenset(
        {"embed", "job", "jobs", "board", "boards", "api", "www", "search", "widget",
         "company", "companies", "static", "assets", "images", "v1", "en", "tr"}
    )

    def detect(self, html: str) -> str | None:
        for pattern in self.patterns:
            for match in pattern.finditer(html):
                slug = match.group(1).strip("/").lower()
                if slug and slug not in self.slug_blacklist and len(slug) > 1:
                    return slug
        return None

    def jobs_url(self, slug: str) -> str:
        raise NotImplementedError

    def parse(self, payload: Any, slug: str) -> list[RawJob]:
        raise NotImplementedError

    def list_jobs(self, fetcher: Fetcher, slug: str) -> list[RawJob] | None:
        """İlanları döner. Uç nokta cevap vermezse None (= slug muhtemelen yanlış)."""
        result = fetcher.get(self.jobs_url(slug), use_cache=False)
        if not result.ok:
            return None
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError:
            return None
        try:
            jobs = self.parse(payload, slug)
        except (AttributeError, TypeError, KeyError):
            return None
        return [j for j in jobs if j.title]


class Greenhouse(Provider):
    key = "greenhouse"
    patterns = (
        re.compile(r"boards\.greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]+)", re.I),
        re.compile(r"(?:job-)?boards\.greenhouse\.io/([A-Za-z0-9_-]+)", re.I),
        re.compile(r"greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I),
    )

    def jobs_url(self, slug: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

    def parse(self, payload: Any, slug: str) -> list[RawJob]:
        return [
            RawJob(
                external_id=str(item.get("id")),
                title=_clean(item.get("title")) or "",
                location=_clean((item.get("location") or {}).get("name")),
                url=item.get("absolute_url"),
                posted_at=_clean(item.get("updated_at")),
            )
            for item in payload["jobs"]
        ]


class Lever(Provider):
    key = "lever"
    patterns = (
        re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_-]+)", re.I),
        re.compile(r"api\.lever\.co/v0/postings/([A-Za-z0-9_-]+)", re.I),
    )

    def jobs_url(self, slug: str) -> str:
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"

    def parse(self, payload: Any, slug: str) -> list[RawJob]:
        jobs = []
        for item in payload:
            categories = item.get("categories") or {}
            location = _clean(categories.get("location"))
            jobs.append(
                RawJob(
                    external_id=str(item.get("id")),
                    title=_clean(item.get("text")) or "",
                    location=location,
                    url=item.get("hostedUrl"),
                    posted_at=_epoch_ms_to_iso(item.get("createdAt")),
                    remote="remote" in (location or "").lower(),
                )
            )
        return jobs


class Ashby(Provider):
    key = "ashby"
    patterns = (
        re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)", re.I),
        re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.-]+)", re.I),
    )

    def jobs_url(self, slug: str) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{slug}"

    def parse(self, payload: Any, slug: str) -> list[RawJob]:
        return [
            RawJob(
                external_id=str(item.get("id")),
                title=_clean(item.get("title")) or "",
                location=_clean(item.get("location")),
                url=item.get("jobUrl") or item.get("applyUrl"),
                posted_at=_clean(item.get("publishedAt")),
                remote=bool(item.get("isRemote")),
            )
            for item in payload["jobs"]
        ]


class Workable(Provider):
    key = "workable"
    patterns = (
        re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)", re.I),
        re.compile(r"([A-Za-z0-9_-]+)\.workable\.com", re.I),
    )

    def jobs_url(self, slug: str) -> str:
        return f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"

    def parse(self, payload: Any, slug: str) -> list[RawJob]:
        jobs = []
        for item in payload["jobs"]:
            parts = [_clean(item.get("city")), _clean(item.get("country"))]
            code = str(item.get("shortcode") or item.get("id") or "")
            jobs.append(
                RawJob(
                    external_id=code,
                    title=_clean(item.get("title")) or "",
                    location=", ".join(p for p in parts if p) or None,
                    url=item.get("url") or f"https://apply.workable.com/{slug}/j/{code}/",
                    posted_at=_clean(item.get("published_on")),
                    remote=bool(item.get("telecommuting")),
                )
            )
        return jobs


class Recruitee(Provider):
    key = "recruitee"
    patterns = (re.compile(r"([A-Za-z0-9_-]+)\.recruitee\.com", re.I),)

    def jobs_url(self, slug: str) -> str:
        return f"https://{slug}.recruitee.com/api/offers/"

    def parse(self, payload: Any, slug: str) -> list[RawJob]:
        return [
            RawJob(
                external_id=str(item.get("id")),
                title=_clean(item.get("title")) or "",
                location=_clean(item.get("location")),
                url=item.get("careers_url") or item.get("careers_apply_url"),
                posted_at=_clean(item.get("published_at")),
                remote=bool(item.get("remote")),
            )
            for item in payload["offers"]
        ]


class SmartRecruiters(Provider):
    key = "smartrecruiters"
    patterns = (
        re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I),
        re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)", re.I),
    )

    def jobs_url(self, slug: str) -> str:
        return f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100"

    def parse(self, payload: Any, slug: str) -> list[RawJob]:
        jobs = []
        for item in payload["content"]:
            loc = item.get("location") or {}
            parts = [_clean(loc.get("city")), _clean(loc.get("country"))]
            jobs.append(
                RawJob(
                    external_id=str(item.get("id")),
                    title=_clean(item.get("name")) or "",
                    location=", ".join(p for p in parts if p) or None,
                    url=f"https://jobs.smartrecruiters.com/{slug}/{item.get('id')}",
                    posted_at=_clean(item.get("releasedDate")),
                    remote=bool(loc.get("remote")),
                )
            )
        return jobs


class Breezy(Provider):
    key = "breezy"
    patterns = (re.compile(r"([A-Za-z0-9_-]+)\.breezy\.hr", re.I),)

    def jobs_url(self, slug: str) -> str:
        return f"https://{slug}.breezy.hr/json"

    def parse(self, payload: Any, slug: str) -> list[RawJob]:
        jobs = []
        for item in payload:
            location = item.get("location") or {}
            city = location.get("city") if isinstance(location, dict) else location
            jobs.append(
                RawJob(
                    external_id=str(item.get("id") or item.get("_id") or ""),
                    title=_clean(item.get("name")) or "",
                    location=_clean(city),
                    url=item.get("url"),
                    posted_at=_clean(item.get("published_date")),
                )
            )
        return jobs


class Personio(Provider):
    """Personio JSON değil XML döner; list_jobs'u kendi ezer."""

    key = "personio"
    patterns = (re.compile(r"([A-Za-z0-9_-]+)\.jobs\.personio\.(?:de|com)", re.I),)

    def jobs_url(self, slug: str) -> str:
        return f"https://{slug}.jobs.personio.de/xml"

    def list_jobs(self, fetcher: Fetcher, slug: str) -> list[RawJob] | None:
        result = fetcher.get(self.jobs_url(slug), use_cache=False)
        if not result.ok:
            return None
        try:
            root = ET.fromstring(result.text)
        except ET.ParseError:
            return None
        jobs = []
        for position in root.iter("position"):
            title = (position.findtext("name") or "").strip()
            if not title:
                continue
            job_id = (position.findtext("id") or "").strip()
            jobs.append(
                RawJob(
                    external_id=job_id,
                    title=title,
                    location=_clean(position.findtext("office")),
                    url=f"https://{slug}.jobs.personio.de/job/{job_id}",
                    posted_at=_clean(position.findtext("createdAt")),
                )
            )
        return jobs


REGISTRY: dict[str, Provider] = {
    p.key: p
    for p in (
        Greenhouse(), Lever(), Ashby(), Workable(),
        Recruitee(), SmartRecruiters(), Breezy(), Personio(),
    )
}


def detect_in_html(html: str) -> tuple[str, str] | None:
    """HTML içinde ATS imzası arar. (provider_key, slug) veya None."""
    for key, provider in REGISTRY.items():
        slug = provider.detect(html)
        if slug:
            return key, slug
    return None


def list_jobs(fetcher: Fetcher, provider_key: str, slug: str) -> list[RawJob] | None:
    provider = REGISTRY.get(provider_key)
    if provider is None:
        return None
    return provider.list_jobs(fetcher, slug)


def provider_keys() -> Iterable[str]:
    return REGISTRY.keys()
