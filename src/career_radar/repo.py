"""Veritabanı yazma/okuma yardımcıları.

Dedup burada yaşıyor. Kural sırası:
  1. Alan adı eşleşmesi (en güvenilir) — kanonik eTLD+1 üzerinden
  2. Normalleştirilmiş isim üzerinde bulanık eşleşme (rapidfuzz), yüksek eşik
Eşleşme yoksa yeni kayıt açılır.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Iterable

from rapidfuzz import fuzz, process

from . import db
from .textutil import (
    classify_email,
    domain_from_url,
    looks_like_internship,
    normalize_company_name,
    registrable_domain,
)

NAME_MATCH_THRESHOLD = 93  # rapidfuzz token_sort_ratio; altında yeni kayıt açılır


# --------------------------------------------------------------------- sources


def upsert_source(key: str, kind: str, name: str, url: str | None = None,
                  parser: str | None = None, enabled: bool = True) -> None:
    db.connect().execute(
        """
        INSERT INTO sources (key, kind, name, url, parser, enabled)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            kind = excluded.kind, name = excluded.name,
            url = excluded.url, parser = excluded.parser
        """,
        (key, kind, name, url, parser, int(enabled)),
    )


def mark_source_run(key: str, count: int, health: str, note: str | None = None) -> None:
    db.connect().execute(
        "UPDATE sources SET last_run_at = ?, last_count = ?, health = ?, note = ? WHERE key = ?",
        (db.utcnow(), count, health, note, key),
    )


# ------------------------------------------------------------------- companies


def _find_by_domain(conn: sqlite3.Connection, domain: str) -> int | None:
    row = conn.execute("SELECT id FROM companies WHERE domain = ?", (domain,)).fetchone()
    if row:
        return row["id"]
    # Alt alan adı farkı: kariyer.acme.com.tr ile acme.com.tr aynı şirket.
    root = registrable_domain(domain)
    if root and root != domain:
        row = conn.execute("SELECT id FROM companies WHERE domain = ?", (root,)).fetchone()
        if row:
            return row["id"]
    return None


def _find_by_name(conn: sqlite3.Connection, name_norm: str) -> int | None:
    if len(name_norm) < 4:
        return None  # çok kısa isimlerde bulanık eşleşme güvenilmez
    rows = conn.execute(
        "SELECT id, name_norm FROM companies WHERE domain IS NULL OR name_norm = ?",
        (name_norm,),
    ).fetchall()
    exact = [r for r in rows if r["name_norm"] == name_norm]
    if exact:
        return exact[0]["id"]
    candidates = {r["id"]: r["name_norm"] for r in rows}
    if not candidates:
        return None
    match = process.extractOne(
        name_norm, candidates, scorer=fuzz.token_sort_ratio, score_cutoff=NAME_MATCH_THRESHOLD
    )
    return match[2] if match else None


def upsert_company(
    name: str,
    website: str | None = None,
    city: str | None = None,
    source_key: str | None = None,
    listed_url: str | None = None,
) -> tuple[int, bool]:
    """Şirketi ekler veya mevcut kaydı günceller. (company_id, created_mi) döner."""
    conn = db.connect()
    now = db.utcnow()
    name = name.strip()
    name_norm = normalize_company_name(name)
    domain = registrable_domain(domain_from_url(website))

    company_id = _find_by_domain(conn, domain) if domain else None
    if company_id is None:
        company_id = _find_by_name(conn, name_norm)

    created = False
    if company_id is None:
        cursor = conn.execute(
            """
            INSERT INTO companies (name, name_norm, domain, city, website, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, name_norm, domain, city, website, now, now),
        )
        company_id = int(cursor.lastrowid)
        created = True
    else:
        # Var olan kaydı yalnızca boş alanlarda zenginleştir; elde olanı ezme.
        conn.execute(
            """
            UPDATE companies SET
                last_seen = ?,
                domain  = COALESCE(domain, ?),
                city    = COALESCE(city, ?),
                website = COALESCE(website, ?)
            WHERE id = ?
            """,
            (now, domain, city, website, company_id),
        )

    if source_key:
        conn.execute(
            """
            INSERT OR IGNORE INTO company_sources (company_id, source_key, listed_url, first_seen)
            VALUES (?, ?, ?, ?)
            """,
            (company_id, source_key, listed_url, now),
        )
    return company_id, created


def save_detection(company_id: int, careers_url: str | None,
                   provider: str | None, slug: str | None,
                   status: str, note: str | None = None) -> None:
    db.connect().execute(
        """
        UPDATE companies SET
            careers_url = COALESCE(?, careers_url),
            ats_provider = ?, ats_slug = ?,
            detect_status = ?, detect_note = ?, detect_checked_at = ?
        WHERE id = ?
        """,
        (careers_url, provider, slug, status, note, db.utcnow(), company_id),
    )


def directory_sector_hint(company_id: int) -> str | None:
    """Şirketin dizin kaynağından gelen (varsa en kısa) sektör etiketi, ham hali.

    Uzunluk/geçerlilik süzgeci burada değil `classify.clean_hint()`'te —
    bazı dizin kaynakları tek bir şirkete tüm kategori listesini (40+ sektör)
    etiket yazmış olabiliyor, o karar sınıflandırma modülünün sorumluluğu.
    """
    row = db.connect().execute(
        "SELECT label FROM company_labels WHERE company_id = ? AND dimension = 'directory_sector' "
        "ORDER BY length(label) ASC LIMIT 1",
        (company_id,),
    ).fetchone()
    return row["label"] if row else None


def save_classification(company_id: int, *, domain: Iterable[str] = (),
                        size_bucket: str | None = None, sector: str | None = None,
                        stack: Iterable[str] = (), evidence_url: str,
                        model: str | None = None, confidence: float | None = None) -> None:
    """Sınıflandırma sonucunu yazar. `classify_checked_at` her zaman ilerler —
    çıkarılan etiket olmasa bile şirket "denendi" sayılır (resume için şart).

    domain/stack çok etiketli olduğu için company_labels'a; size_bucket/sector
    tekil olduğu için doğrudan companies sütunlarına (COALESCE ile, var olanı
    ezmeden) yazılır.
    """
    conn = db.connect()
    for label in domain:
        add_label(company_id, "domain", label, confidence, evidence_url, model)
    for label in stack:
        add_label(company_id, "stack", label, confidence, evidence_url, model)
    if size_bucket or sector:
        # COALESCE(mevcut, yeni) — sırayla ilk NULL-olmayanı alır. Mevcut zaten
        # doluysa korunur; ancak boşsa yeni değer yazılır. Ters sıra (yeni, mevcut)
        # yeni değeri her zaman kazandırır ve "bir kez sınıflandır, sonra dokunma"
        # kuralını bozar (testlerle yakalandı).
        conn.execute(
            "UPDATE companies SET size_bucket = COALESCE(size_bucket, ?), "
            "sector = COALESCE(sector, ?) WHERE id = ?",
            (size_bucket, sector, company_id),
        )
    conn.execute(
        "UPDATE companies SET classify_checked_at = ? WHERE id = ?", (db.utcnow(), company_id)
    )


def mark_enrich_checked(company_id: int) -> None:
    """İsimli kişi çıkarımı bu şirket için denendi — bulunamamış olsa bile.

    Bu olmadan `enrich` komutu her koşuda id sırasına göre baştan başlar ve
    asla ilerlemez; `detect`/`contacts`'ın kurduğu checkpoint deseniyle tutarlı
    olması için ayrı bir sütun (enrich_checked_at) kullanılıyor — regex tabanlı
    iletişim taraması bitmiş olsa bile LLM çıkarımı henüz denenmemiş olabilir.
    """
    db.connect().execute(
        "UPDATE companies SET enrich_checked_at = ? WHERE id = ?", (db.utcnow(), company_id)
    )


def add_label(company_id: int, dimension: str, label: str,
              confidence: float | None = None, evidence_url: str | None = None,
              model: str | None = None) -> None:
    """Bir etiket yazar. Aynı (şirket, boyut, etiket) üçlüsü tekrar yazılmaz."""
    label = (label or "").strip()
    if not label:
        return
    db.connect().execute(
        """
        INSERT INTO company_labels
            (company_id, dimension, label, confidence, evidence_url, model, as_of)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id, dimension, label) DO UPDATE SET
            confidence = excluded.confidence, as_of = excluded.as_of
        """,
        (company_id, dimension, label, confidence, evidence_url, model, db.utcnow()),
    )


def companies_exclusive_to_sources(source_keys: tuple[str, ...],
                                    city: str | None = None) -> list[sqlite3.Row]:
    """Yalnızca verilen kaynaklardan gelmiş, başka hiçbir kaynakta görünmeyen şirketler.

    Kapsam daraltmalarında (ör. bir bölgesel dizin kapatıldığında) hangi şirketlerin
    güvenle temizlenebileceğini bulur: başka bir kaynakta da geçen ya da elle
    eklenmiş bir şirket, tek kaynağı kapansa bile silinmez.
    """
    placeholders = ",".join("?" * len(source_keys))
    sql = f"""
        SELECT c.* FROM companies c
        WHERE NOT EXISTS (
            SELECT 1 FROM company_sources cs
            WHERE cs.company_id = c.id AND cs.source_key NOT IN ({placeholders})
        )
    """
    params: list = list(source_keys)
    if city is not None:
        sql += " AND c.city = ?"
        params.append(city)
    sql += " ORDER BY c.name"
    return db.connect().execute(sql, params).fetchall()


def companies_needing(stage: str, limit: int | None = None,
                      only_with_domain: bool = True) -> list[sqlite3.Row]:
    """Bir aşamadan henüz geçmemiş şirketler — checkpoint/resume'un temeli."""
    column = {
        "detect": "detect_checked_at",
        "contacts": "contacts_checked_at",
        "classify": "classify_checked_at",
        "jobs": "jobs_checked_at",
        "enrich": "enrich_checked_at",
    }[stage]
    sql = f"SELECT * FROM companies WHERE {column} IS NULL"
    if only_with_domain:
        sql += " AND domain IS NOT NULL"
    if stage == "jobs":
        sql += " AND ats_provider IS NOT NULL"
    sql += " ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.connect().execute(sql).fetchall()


# ------------------------------------------------------------------------ jobs


def job_fingerprint(company_id: int, title: str, location: str | None) -> str:
    raw = f"{company_id}|{normalize_company_name(title)}|{(location or '').strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sync_jobs(company_id: int, raw_jobs: Iterable[Any]) -> dict:
    """İlanları senkronlar: yenileri ekler, görülenleri tazeler, kaybolanları pasifler."""
    conn = db.connect()
    now = db.utcnow()
    seen: set[str] = set()
    added = 0

    for job in raw_jobs:
        fingerprint = job_fingerprint(company_id, job.title, job.location)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        is_internship = int(looks_like_internship(job.title))
        cursor = conn.execute(
            """
            INSERT INTO jobs (company_id, external_id, title, location, remote,
                              is_internship, url, posted_at, fingerprint,
                              first_seen, last_seen, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(fingerprint) DO UPDATE SET
                last_seen = excluded.last_seen, is_active = 1,
                url = COALESCE(excluded.url, jobs.url)
            """,
            (company_id, job.external_id, job.title, job.location,
             int(bool(job.remote)) if job.remote is not None else None,
             is_internship, job.url, job.posted_at, fingerprint, now, now),
        )
        if cursor.rowcount == 1 and conn.total_changes:
            added += 1

    if seen:
        placeholders = ",".join("?" * len(seen))
        conn.execute(
            f"UPDATE jobs SET is_active = 0 WHERE company_id = ? "
            f"AND fingerprint NOT IN ({placeholders})",
            (company_id, *seen),
        )
    else:
        conn.execute("UPDATE jobs SET is_active = 0 WHERE company_id = ?", (company_id,))

    counts = conn.execute(
        """
        SELECT COUNT(*) AS total, COALESCE(SUM(is_internship), 0) AS interns
        FROM jobs WHERE company_id = ? AND is_active = 1
        """,
        (company_id,),
    ).fetchone()

    conn.execute(
        """
        UPDATE companies SET open_jobs_count = ?, has_internship = ?, jobs_checked_at = ?
        WHERE id = ?
        """,
        (counts["total"], int(counts["interns"] > 0), now, company_id),
    )
    return {"active": counts["total"], "internships": counts["interns"], "added": added}


# -------------------------------------------------------------------- contacts


def save_contacts(company_id: int, records: Iterable[dict]) -> int:
    """İletişim kayıtlarını yazar. Her kaydın source_url'i zorunlu — doğrulanabilirlik şartı."""
    conn = db.connect()
    now = db.utcnow()
    written = 0
    for record in records:
        email = (record.get("email") or "").strip().lower() or None
        name = (record.get("name") or "").strip() or None
        if not email and not name:
            continue
        source_url = record.get("source_url")
        if not source_url:
            raise ValueError("source_url olmayan iletişim kaydı yazılamaz")
        contact_type = record.get("contact_type") or (
            classify_email(email) if email else "executive"
        )
        if contact_type == "personal":
            contact_type = "executive" if name else "generic"
        conn.execute(
            """
            INSERT OR IGNORE INTO contacts
                (company_id, name, role, email, phone, contact_type,
                 source_url, extraction_method, confidence, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (company_id, name, record.get("role"), email, record.get("phone"),
             contact_type, source_url, record.get("extraction_method", "regex"),
             record.get("confidence"), now),
        )
        written += 1

    totals = conn.execute(
        """
        SELECT COUNT(DISTINCT email) AS emails,
               COUNT(DISTINCT CASE WHEN contact_type = 'hr' THEN email END) AS hr_emails
        FROM contacts WHERE company_id = ? AND email IS NOT NULL
        """,
        (company_id,),
    ).fetchone()
    conn.execute(
        """
        UPDATE companies SET email_count = ?, hr_email_count = ?, contacts_checked_at = ?
        WHERE id = ?
        """,
        (totals["emails"], totals["hr_emails"], now, company_id),
    )
    return written
