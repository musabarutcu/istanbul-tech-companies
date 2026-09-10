"""Şirket listeleme, filtreleme, detay ve dışa aktarma uç noktaları.

Listeleme N+1 sorgu yapmaz: önce sayfadaki şirketler çekilir, sonra o kimlikler için
etiketler ve kaynaklar tek sorguda toplanır. 1265 satırda fark etmez ama liste
büyüdükçe (hedef 3000+) tek tek sorgulamak arayüzü gözle görülür yavaşlatır.
"""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from .. import db
from ..textutil import fold
from .schemas import (
    CompanyDetail,
    CompanyListItem,
    CompanyPage,
    CompanyPatch,
    ContactOut,
    FacetValue,
    Facets,
    JobOut,
    SourceRef,
    Stats,
)

router = APIRouter(prefix="/api", tags=["companies"])

SORTS = {
    "name": "c.name_norm ASC",
    "jobs": "c.open_jobs_count DESC, c.name_norm ASC",
    "newest": "c.first_seen DESC, c.id DESC",
    "contacts": "c.hr_email_count DESC, c.email_count DESC, c.name_norm ASC",
}

USER_STATUSES = {"listemde", "yazdim", "cevap_geldi", "ilgilenmiyorum"}


def _build_filters(
    q: str | None, city: str | None, source: str | None, sector: str | None,
    category: list[str] | None, status: str | None, has_domain: bool | None,
    has_careers: bool | None, has_jobs: bool | None, has_internship: bool | None,
    has_email: bool | None, has_hr_email: bool | None,
) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []

    if q:
        # Arama katlanmış isim üzerinde: "İnsan" yazan da "insan" yazan da bulur.
        clauses.append("c.name_norm LIKE ?")
        params.append(f"%{fold(q)}%")
    if city:
        clauses.append("c.city = ?")
        params.append(city)
    if status:
        clauses.append("c.user_status = ?")
        params.append(status)
    if source:
        clauses.append(
            "EXISTS (SELECT 1 FROM company_sources cs "
            "WHERE cs.company_id = c.id AND cs.source_key = ?)"
        )
        params.append(source)
    if sector:
        clauses.append(
            "EXISTS (SELECT 1 FROM company_labels cl WHERE cl.company_id = c.id "
            "AND cl.dimension = 'directory_sector' AND cl.label = ?)"
        )
        params.append(sector)
    if category:
        # 'category' = taxonomy.yaml'daki kendi sınıflandırmamız (ai_ml, web_saas...);
        # 'sector' (yukarıda) = dizin kaynağının kendi ham/tutarsız sektör metni.
        # Birden fazla kategori seçilebilir (OR): herhangi birine sahip şirket eşleşir.
        placeholders = ",".join("?" * len(category))
        clauses.append(
            "EXISTS (SELECT 1 FROM company_labels cl WHERE cl.company_id = c.id "
            f"AND cl.dimension = 'domain' AND cl.label IN ({placeholders}))"
        )
        params.extend(category)

    booleans = {
        "c.domain IS NOT NULL": has_domain,
        "c.careers_url IS NOT NULL": has_careers,
        "c.open_jobs_count > 0": has_jobs,
        "c.has_internship = 1": has_internship,
        "c.email_count > 0": has_email,
        "c.hr_email_count > 0": has_hr_email,
    }
    for expression, wanted in booleans.items():
        if wanted is None:
            continue
        clauses.append(expression if wanted else f"NOT ({expression})")

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _decorate(rows: list) -> list[CompanyListItem]:
    """Sayfadaki şirketlere etiket ve kaynak bilgisini toplu halde ekler."""
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    placeholders = ",".join("?" * len(ids))
    conn = db.connect()

    sectors: dict[int, list[str]] = {}
    for label in conn.execute(
        f"SELECT company_id, label FROM company_labels "
        f"WHERE dimension = 'directory_sector' AND company_id IN ({placeholders})",
        ids,
    ):
        sectors.setdefault(label["company_id"], []).append(label["label"])

    categories: dict[int, list[str]] = {}
    for label in conn.execute(
        f"SELECT company_id, label FROM company_labels "
        f"WHERE dimension = 'domain' AND company_id IN ({placeholders})",
        ids,
    ):
        categories.setdefault(label["company_id"], []).append(label["label"])

    sources: dict[int, list[str]] = {}
    for link in conn.execute(
        f"SELECT cs.company_id, COALESCE(s.name, cs.source_key) AS name "
        f"FROM company_sources cs LEFT JOIN sources s ON s.key = cs.source_key "
        f"WHERE cs.company_id IN ({placeholders})",
        ids,
    ):
        sources.setdefault(link["company_id"], []).append(link["name"])

    items = []
    for row in rows:
        items.append(CompanyListItem(
            id=row["id"],
            name=row["name"],
            domain=row["domain"],
            city=row["city"],
            website=row["website"],
            careers_url=row["careers_url"],
            summary=row["summary"],
            size_bucket=row["size_bucket"],
            sector=row["sector"],
            categories=categories.get(row["id"], []),
            sectors=sectors.get(row["id"], []),
            sources=sources.get(row["id"], []),
            open_jobs_count=row["open_jobs_count"],
            has_internship=bool(row["has_internship"]),
            email_count=row["email_count"],
            hr_email_count=row["hr_email_count"],
            detect_status=row["detect_status"],
            user_status=row["user_status"],
            first_seen=row["first_seen"],
        ))
    return items


@router.get("/companies", response_model=CompanyPage)
def list_companies(
    q: str | None = None,
    city: str | None = None,
    source: str | None = None,
    sector: str | None = None,
    category: list[str] | None = Query(None),
    status: str | None = None,
    has_domain: bool | None = None,
    has_careers: bool | None = None,
    has_jobs: bool | None = None,
    has_internship: bool | None = None,
    has_email: bool | None = None,
    has_hr_email: bool | None = None,
    sort: str = Query("name", pattern="^(name|jobs|newest|contacts)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> CompanyPage:
    where, params = _build_filters(q, city, source, sector, category, status, has_domain,
                                   has_careers, has_jobs, has_internship,
                                   has_email, has_hr_email)
    conn = db.connect()
    total = conn.execute(f"SELECT COUNT(*) FROM companies c{where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM companies c{where} ORDER BY {SORTS[sort]} LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return CompanyPage(total=total, page=page, page_size=page_size, items=_decorate(rows))


@router.get("/companies/{company_id}", response_model=CompanyDetail)
def get_company(company_id: int) -> CompanyDetail:
    conn = db.connect()
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Şirket bulunamadı")

    base = _decorate([row])[0]
    detail = CompanyDetail(**base.model_dump())
    detail.ats_provider = row["ats_provider"]
    detail.detect_note = row["detect_note"]
    detail.user_note = row["user_note"]

    detail.source_refs = [
        SourceRef(key=r["key"], name=r["name"], kind=r["kind"])
        for r in conn.execute(
            "SELECT s.key, COALESCE(s.name, cs.source_key) AS name, "
            "COALESCE(s.kind, '?') AS kind "
            "FROM company_sources cs LEFT JOIN sources s ON s.key = cs.source_key "
            "WHERE cs.company_id = ?",
            (company_id,),
        )
    ]
    detail.contacts = [
        ContactOut(
            name=r["name"], role=r["role"], email=r["email"], phone=r["phone"],
            contact_type=r["contact_type"], source_url=r["source_url"],
            extraction_method=r["extraction_method"],
        )
        # İK kutuları önce: spontane staj başvurusunun doğru muhatabı onlar.
        for r in conn.execute(
            "SELECT * FROM contacts WHERE company_id = ? "
            "ORDER BY CASE contact_type WHEN 'hr' THEN 0 WHEN 'executive' THEN 1 "
            "ELSE 2 END, email",
            (company_id,),
        )
    ]
    detail.jobs = [
        JobOut(title=r["title"], location=r["location"], url=r["url"],
               is_internship=bool(r["is_internship"]), posted_at=r["posted_at"])
        for r in conn.execute(
            "SELECT * FROM jobs WHERE company_id = ? AND is_active = 1 "
            "ORDER BY is_internship DESC, title",
            (company_id,),
        )
    ]
    return detail


@router.patch("/companies/{company_id}", response_model=CompanyDetail)
def patch_company(company_id: int, patch: CompanyPatch) -> CompanyDetail:
    if patch.user_status is not None and patch.user_status != "" \
            and patch.user_status not in USER_STATUSES:
        raise HTTPException(status_code=422, detail=f"Geçersiz durum: {patch.user_status}")

    conn = db.connect()
    if conn.execute("SELECT 1 FROM companies WHERE id = ?", (company_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="Şirket bulunamadı")

    fields, values = [], []
    if patch.user_status is not None:
        fields.append("user_status = ?")
        values.append(patch.user_status or None)
    if patch.user_note is not None:
        fields.append("user_note = ?")
        values.append(patch.user_note or None)
    if fields:
        fields.append("user_updated_at = ?")
        values.append(db.utcnow())
        conn.execute(
            f"UPDATE companies SET {', '.join(fields)} WHERE id = ?", [*values, company_id]
        )
    return get_company(company_id)


@router.get("/facets", response_model=Facets)
def get_facets() -> Facets:
    conn = db.connect()

    def rows_to_facets(sql: str) -> list[FacetValue]:
        return [FacetValue(value=r[0], label=r[1], count=r[2])
                for r in conn.execute(sql) if r[0]]

    return Facets(
        cities=rows_to_facets(
            "SELECT city, city, COUNT(*) FROM companies WHERE city IS NOT NULL "
            "GROUP BY city ORDER BY COUNT(*) DESC"
        ),
        sources=rows_to_facets(
            "SELECT cs.source_key, COALESCE(s.name, cs.source_key), COUNT(DISTINCT cs.company_id) "
            "FROM company_sources cs LEFT JOIN sources s ON s.key = cs.source_key "
            "GROUP BY cs.source_key ORDER BY COUNT(DISTINCT cs.company_id) DESC"
        ),
        sectors=rows_to_facets(
            "SELECT label, label, COUNT(*) FROM company_labels "
            "WHERE dimension = 'directory_sector' GROUP BY label "
            "ORDER BY COUNT(*) DESC LIMIT 60"
        ),
        categories=rows_to_facets(
            "SELECT label, label, COUNT(*) FROM company_labels "
            "WHERE dimension = 'domain' GROUP BY label ORDER BY COUNT(*) DESC"
        ),
        statuses=rows_to_facets(
            "SELECT user_status, user_status, COUNT(*) FROM companies "
            "WHERE user_status IS NOT NULL GROUP BY user_status"
        ),
    )


@router.get("/stats", response_model=Stats)
def get_stats() -> Stats:
    return Stats(**db.stats())


@router.get("/export.csv")
def export_csv(
    q: str | None = None, city: str | None = None, source: str | None = None,
    sector: str | None = None, category: list[str] | None = Query(None), status: str | None = None,
    has_domain: bool | None = None, has_careers: bool | None = None,
    has_jobs: bool | None = None, has_internship: bool | None = None,
    has_email: bool | None = None, has_hr_email: bool | None = None,
    sort: str = Query("name", pattern="^(name|jobs|newest|contacts)$"),
):
    """Aktif filtrenin tamamını CSV olarak indirir (sayfalama uygulanmaz)."""
    where, params = _build_filters(q, city, source, sector, category, status, has_domain,
                                   has_careers, has_jobs, has_internship,
                                   has_email, has_hr_email)
    rows = db.connect().execute(
        f"SELECT * FROM companies c{where} ORDER BY {SORTS[sort]}", params
    ).fetchall()
    items = _decorate(rows)

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")  # Excel TR ayracı
    writer.writerow(["Şirket", "Alan adı", "Şehir", "Kategori (AI/Web/...)", "Sektör (dizin)",
                     "Kaynak", "Kariyer sayfası", "Açık ilan", "Staj ilanı",
                     "E-posta sayısı", "İK e-postası", "Durum"])
    for item in items:
        writer.writerow([
            item.name, item.domain or "", item.city or "",
            ", ".join(item.categories), ", ".join(item.sectors), ", ".join(item.sources),
            item.careers_url or "", item.open_jobs_count,
            "evet" if item.has_internship else "",
            item.email_count, item.hr_email_count, item.user_status or "",
        ])

    buffer.seek(0)
    return StreamingResponse(
        # BOM: Excel'in UTF-8'i doğru açması için gerekli, yoksa Türkçe karakterler bozulur.
        iter(["﻿" + buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="career-radar.csv"'},
    )
