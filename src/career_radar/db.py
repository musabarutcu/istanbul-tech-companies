"""SQLite erişim katmanı.

Tasarım notları:
- WAL modu şart: boru hattı saatlerce yazarken FastAPI aynı dosyadan okuyacak.
  WAL olmadan uzun koşu sırasında arayüz "database is locked" ile kilitlenir.
- Bağlantılar thread-local. Boru hattı kendi thread'inde, API kendi thread'inde çalışır;
  sqlite3 bağlantısı thread'ler arası paylaşılamaz.
- ORM yok. Şema burada, tek yerde; sorgular çağıran modülde açıkça yazılır.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

_local = threading.local()
_db_path: Path | None = None


def utcnow() -> str:
    """Her yerde aynı zaman formatı: ISO 8601, UTC, saniye hassasiyeti."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def configure(db_path: str | Path) -> None:
    """Süreç genelinde kullanılacak veritabanı dosyasını belirler."""
    global _db_path
    _db_path = Path(db_path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)


def get_db_path() -> Path:
    if _db_path is None:
        raise RuntimeError("db.configure() çağrılmadan veritabanı kullanılamaz")
    return _db_path


def connect() -> sqlite3.Connection:
    """Bu thread'e ait bağlantıyı döndürür, yoksa açar."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn

    conn = sqlite3.connect(get_db_path(), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    # Yazma kilidi görürse hemen hata vermek yerine 30 sn bekle.
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    _local.conn = conn
    return conn


def close() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    key            TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,          -- teknokent | vc | dernek | manual | kap | ats
    name           TEXT NOT NULL,
    url            TEXT,
    parser         TEXT,
    robots_ok      INTEGER,                -- NULL = henüz bakılmadı
    enabled        INTEGER NOT NULL DEFAULT 1,
    last_run_at    TEXT,
    last_count     INTEGER,
    health         TEXT,                   -- ok | empty | error | blocked
    note           TEXT
);

CREATE TABLE IF NOT EXISTS companies (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    name_norm           TEXT NOT NULL,
    domain              TEXT,
    city                TEXT,
    website             TEXT,
    careers_url         TEXT,
    careers_form_url    TEXT,              -- mail yoksa başvurulacak form
    summary             TEXT,
    size_bucket         TEXT,
    sector              TEXT,
    ats_provider        TEXT,
    ats_slug            TEXT,
    detect_status       TEXT,              -- ats | careers | none | blocked | error
    detect_note         TEXT,
    open_jobs_count     INTEGER NOT NULL DEFAULT 0,
    has_internship      INTEGER NOT NULL DEFAULT 0,
    email_count         INTEGER NOT NULL DEFAULT 0,
    hr_email_count      INTEGER NOT NULL DEFAULT 0,
    detect_checked_at   TEXT,
    contacts_checked_at TEXT,
    classify_checked_at TEXT,
    jobs_checked_at     TEXT,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    user_status         TEXT,              -- listemde | yazdim | cevap_geldi | ilgilenmiyorum
    user_note           TEXT,
    user_updated_at     TEXT
);

-- Domain tekilliği dedup'ın belkemiği. NULL domainler çakışmaz (partial index).
CREATE UNIQUE INDEX IF NOT EXISTS ux_companies_domain
    ON companies(domain) WHERE domain IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_companies_name_norm ON companies(name_norm);
CREATE INDEX IF NOT EXISTS ix_companies_city      ON companies(city);

-- Bir şirket birden fazla dizinde listelenmiş olabilir; hangi kaynaklardan geldiğini tutar.
CREATE TABLE IF NOT EXISTS company_sources (
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    source_key  TEXT NOT NULL,
    listed_url  TEXT,
    first_seen  TEXT NOT NULL,
    PRIMARY KEY (company_id, source_key)
);

CREATE TABLE IF NOT EXISTS company_labels (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    dimension     TEXT NOT NULL,           -- domain | sector | size | stack
    label         TEXT NOT NULL,
    confidence    REAL,
    evidence_url  TEXT,
    model         TEXT,
    as_of         TEXT NOT NULL,
    UNIQUE (company_id, dimension, label)
);
CREATE INDEX IF NOT EXISTS ix_labels_lookup ON company_labels(dimension, label);

CREATE TABLE IF NOT EXISTS contacts (
    id                 INTEGER PRIMARY KEY,
    company_id         INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name               TEXT,
    role               TEXT,
    email              TEXT,
    phone              TEXT,
    contact_type       TEXT NOT NULL,      -- hr | executive | generic
    source_url         TEXT NOT NULL,      -- her kayıt doğrulanabilir olmalı
    extraction_method  TEXT NOT NULL,      -- regex | llm | kap
    confidence         REAL,
    fetched_at         TEXT NOT NULL,
    UNIQUE (company_id, email, name)
);
CREATE INDEX IF NOT EXISTS ix_contacts_company ON contacts(company_id);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    external_id   TEXT,
    title         TEXT NOT NULL,
    location      TEXT,
    remote        INTEGER,
    is_internship INTEGER NOT NULL DEFAULT 0,
    url           TEXT,
    posted_at     TEXT,
    fingerprint   TEXT NOT NULL UNIQUE,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_jobs_company ON jobs(company_id, is_active);

-- Değişmez ham yanıt deposu. Parser değişince ağa tekrar çıkmadan yeniden işlenir;
-- 1-3 saatlik koşuyu tekrarlamamanın tek yolu bu.
CREATE TABLE IF NOT EXISTS raw_documents (
    id          INTEGER PRIMARY KEY,
    url         TEXT NOT NULL,
    company_id  INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    kind        TEXT,
    status      INTEGER,
    sha256      TEXT NOT NULL,
    body        TEXT,
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_raw_url ON raw_documents(url);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    state         TEXT NOT NULL,           -- running | done | failed | cancelled
    stages_json   TEXT NOT NULL,
    current_stage TEXT,
    processed     INTEGER NOT NULL DEFAULT 0,
    total         INTEGER NOT NULL DEFAULT 0,
    error_count   INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    message       TEXT
);

CREATE TABLE IF NOT EXISTS run_events (
    id       INTEGER PRIMARY KEY,
    run_id   INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts       TEXT NOT NULL,
    level    TEXT NOT NULL,                -- info | warn | error
    stage    TEXT,
    message  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_run_events_run ON run_events(run_id, id);
"""


# Şemaya sonradan eklenen kolonlar. CREATE TABLE IF NOT EXISTS var olan tabloyu
# değiştirmediği için, mevcut veritabanlarına bunlar ALTER ile eklenir.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "companies": {
        "detect_status": "TEXT",
        "detect_note": "TEXT",
        # İsimli kişi (CEO/kurucu/İK) çıkarımı — regex tabanlı contacts_checked_at'tan
        # bağımsız bir aşama, kendi checkpoint'iyle ayrıca resume edilebilmeli.
        "enrich_checked_at": "TEXT",
    },
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, decl in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init() -> None:
    """Şemayı kurar. Idempotent — her açılışta güvenle çağrılabilir."""
    conn = connect()
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def stats() -> dict:
    conn = connect()

    def one(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    return {
        "companies": one("SELECT COUNT(*) FROM companies"),
        "with_domain": one("SELECT COUNT(*) FROM companies WHERE domain IS NOT NULL"),
        "with_careers_url": one("SELECT COUNT(*) FROM companies WHERE careers_url IS NOT NULL"),
        "with_ats": one("SELECT COUNT(*) FROM companies WHERE ats_provider IS NOT NULL"),
        "with_email": one("SELECT COUNT(*) FROM companies WHERE email_count > 0"),
        "with_hr_email": one("SELECT COUNT(*) FROM companies WHERE hr_email_count > 0"),
        "with_open_jobs": one("SELECT COUNT(*) FROM companies WHERE open_jobs_count > 0"),
        "with_internship": one("SELECT COUNT(*) FROM companies WHERE has_internship = 1"),
        "contacts": one("SELECT COUNT(*) FROM contacts"),
        "jobs_active": one("SELECT COUNT(*) FROM jobs WHERE is_active = 1"),
        "labels": one("SELECT COUNT(*) FROM company_labels"),
        "sources": one("SELECT COUNT(*) FROM sources"),
    }
