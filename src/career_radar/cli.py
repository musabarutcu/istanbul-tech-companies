"""Career Radar komut satırı arayüzü.

Boru hattı arayüzden bağımsız çalışabilir: FastAPI ayakta olmasa da her aşama
buradan tetiklenebilir. API'deki "Listeyi güncelle" butonu da aynı fonksiyonları çağırır.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

# Windows'un eski konsol kod sayfası (cp1254 vb.) bazı Unicode karakterlerini
# (ör. "İ"nin ayrışmış biçimi, U+0307 birleştirici nokta) kodlayamıyor ve
# UnicodeEncodeError fırlatıyor — gerçek bir koşuda bu, saatlerce süren bir
# taramayı sadece bir şirket adını yazdırırken çökertti. Çıktıyı en baştan
# UTF-8'e zorlamak, kozmetik bir konsol sorununun asla veri kaybına yol
# açmamasını garanti eder.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

from . import db, repo
from .net import Fetcher
from .pipeline import classify as classify_mod
from .pipeline import contacts as contacts_mod
from .pipeline import detect as detect_mod
from .pipeline import extraction as extraction_mod
from .pipeline import seed as seed_mod
from .pipeline.connectors import ari_teknokent as ari_mod
from .pipeline.connectors import ats
from .pipeline.connectors import directories as dirs_mod

app = typer.Typer(add_completion=False, help="Türkiye teknoloji şirketi keşif motoru")
console = Console()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "data" / "radar.db"
DEFAULT_CACHE = PROJECT_ROOT / "cache"
SEEDS_DIR = PROJECT_ROOT / "seeds"
DEFAULT_DIRS = PROJECT_ROOT / "seeds" / "directories.yaml"

# Kapsam kararı: proje İstanbul'a özelleştirildi. Bu kaynaklardan yalnızca
# Ankara'ya bağlı gelen ve başka hiçbir kaynakta görünmeyen şirketler
# `prune-ankara` ile temizlenebilir.
ANKARA_ONLY_SOURCES = ("cyberpark", "odtu_teknokent")


def _boot() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    db.configure(DEFAULT_DB)
    db.init()


@app.command("init")
def init_cmd() -> None:
    """Veritabanını oluşturur (idempotent)."""
    _boot()
    console.print(f"[green]Veritabanı hazır:[/] {DEFAULT_DB}")


@app.command("seed")
def seed_cmd(
    path: Path = typer.Option(
        None, "--path", help="Tek bir YAML dosyası (verilmezse seeds/companies*.yaml'ın hepsi)"
    ),
) -> None:
    """Elle yazılmış tohum listelerini yükler.

    Varsayılan olarak `seeds/companies*.yaml` deseniyle eşleşen HER dosya okunur —
    ana tohum listesi (companies.yaml) ve bölgesel ek listeler (ör.
    companies_maslak_atasehir.yaml) ayrı dosyalar olarak durabilir, her biri kendi
    `source.key`'iyle izlenir.
    """
    _boot()
    paths = [path] if path else sorted(SEEDS_DIR.glob("companies*.yaml"))
    if not paths:
        console.print("[yellow]seeds/companies*.yaml deseniyle eşleşen dosya yok.[/]")
        return

    total_new = total_upd = 0
    for p in paths:
        result = seed_mod.load_companies_yaml(p)
        console.print(
            f"[green]{p.name}:[/] {result['total']} şirket "
            f"(yeni: {result['created']}, güncellenen: {result['updated']})"
        )
        total_new += result["created"]
        total_upd += result["updated"]
    console.print(f"\n[bold]Toplam yeni: {total_new} · güncellenen: {total_upd}[/]")


@app.command("dirs")
def dirs_cmd(
    only: str = typer.Option(None, "--only", help="Yalnızca bu kaynak anahtarını çalıştır"),
    delay: float = typer.Option(1.0, "--delay"),
    details: bool = typer.Option(True, "--details/--no-details",
                                 help="Detay sayfalarına giderek firma sitesini çöz"),
) -> None:
    """Dizin kaynaklarını gezer ve şirketleri veritabanına ekler (Faz 1 keşif)."""
    _boot()
    sources = dirs_mod.load_sources(DEFAULT_DIRS)
    if only:
        sources = [s for s in sources if s.key == only]
        if not sources:
            console.print(f"[red]'{only}' anahtarlı kaynak yok.[/]")
            raise typer.Exit(1)

    grand_new = grand_total = 0
    with Fetcher(cache_dir=DEFAULT_CACHE, min_interval=delay) as fetcher:
        for source in sources:
            repo.upsert_source(source.key, source.kind, source.name,
                               source.list_url, f"directories:{source.mode}", source.enabled)
            if not source.enabled:
                console.print(f"[dim]— {source.name}: kapalı ({source.note or ''})[/]")
                continue

            console.print(f"\n[bold]{source.name}[/] [dim]({source.mode})[/]")
            try:
                entries = dirs_mod.crawl(
                    fetcher, source, resolve_details=details,
                    on_page=lambda p, n, t: console.print(
                        f"    sayfa {p}: {n} kayıt (toplam {t})", highlight=False),
                )
            except Exception as exc:  # tek kaynak boru hattını durdurmaz
                repo.mark_source_run(source.key, 0, "error", f"{type(exc).__name__}: {exc}")
                console.print(f"  [red]hata: {type(exc).__name__}: {exc}[/]")
                continue

            new_count = 0
            with_site = 0
            for entry in entries:
                company_id, created = repo.upsert_company(
                    name=entry.name, website=entry.website, city=source.city,
                    source_key=source.key, listed_url=entry.listed_url or source.list_url,
                )
                new_count += created
                with_site += bool(entry.website)
                if entry.sector:
                    repo.add_label(company_id, "directory_sector", entry.sector,
                                   evidence_url=entry.listed_url or source.list_url)

            health = "ok" if entries else "empty"
            repo.mark_source_run(source.key, len(entries), health)
            grand_new += new_count
            grand_total += len(entries)
            console.print(
                f"  [green]{len(entries)} kayıt[/] · yeni: {new_count} · "
                f"sitesi olan: {with_site}"
            )

    console.print(f"\n[bold]Dizin taraması bitti.[/] {grand_total} kayıt, {grand_new} yeni şirket.")
    console.print(f"[dim]HTTP: {fetcher.stats}[/]")


@app.command("prune-ankara")
def prune_ankara_cmd(
    apply: bool = typer.Option(False, "--apply", help="Varsayılan kuru çalıştırma; silmek için ver"),
) -> None:
    """Yalnızca kapatılan Ankara teknokent kaynaklarından gelen şirketleri temizler.

    Proje İstanbul'a özelleştirildiği için `directories.yaml`'da cyberpark ve
    odtu_teknokent kapatıldı (bkz. kaynaklardaki not). Bu komut, YALNIZCA bu iki
    kaynaktan gelmiş VE başka hiçbir kaynakta/tohum listesinde görünmeyen
    şirketleri siler — Ankara'daki ASELSAN/HAVELSAN gibi elle eklenmiş savunma
    sanayii şirketleri (manuel tohum listesinde oldukları için) etkilenmez.
    """
    _boot()
    candidates = repo.companies_exclusive_to_sources(ANKARA_ONLY_SOURCES, city="Ankara")

    if not candidates:
        console.print("[green]Temizlenecek Ankara-özel şirket yok.[/]")
        return

    console.print(f"[yellow]{len(candidates)} şirket yalnızca kapatılan Ankara "
                  f"kaynaklarından geliyor:[/]")
    for row in candidates[:10]:
        console.print(f"  · {row['name']}")
    if len(candidates) > 10:
        console.print(f"  … ve {len(candidates) - 10} tane daha")

    if not apply:
        console.print("\n[dim]Kuru çalıştırma. Silmek için: career-radar prune-ankara --apply[/]")
        return

    ids = [row["id"] for row in candidates]
    conn = db.connect()
    conn.execute(f"DELETE FROM companies WHERE id IN ({','.join('?' * len(ids))})", ids)
    console.print(f"[green]{len(ids)} şirket silindi.[/]")


def source_display_health(enabled: int, health: str | None) -> str:
    """Bir kaynağın panelde gösterilecek durum metni.

    enabled=0 her zaman öncelikli — yoksa kapatılmadan önceki son koşudan kalan
    health='ok' değeri, kapatılmış bir kaynağı hâlâ çalışıyor gibi gösterir.
    """
    if not enabled:
        return "kapalı"
    return health or "-"


@app.command("resolve-ari")
def resolve_ari_cmd(
    limit: int = typer.Option(0, "--limit", help="Kaç firma işlensin (0 = hepsi)"),
    delay: float = typer.Option(0.5, "--delay"),
) -> None:
    """İTÜ ARI Teknokent firmalarının web sitesini AJAX uç noktasından çözer.

    Genel dizin taraması (`dirs`) bu kaynaktan yalnızca isim+sektör alabiliyor —
    firma sitesi kartın modal'ında, ayrı bir istekle geliyor. Bulunan siteler
    isim eşleştirmesiyle mevcut şirket kayıtlarına yazılır (yeni kayıt açmaz).
    """
    _boot()
    with Fetcher(cache_dir=DEFAULT_CACHE, min_interval=delay) as fetcher:
        console.print("[dim]Firma satırları taranıyor...[/]")
        rows = ari_mod.crawl_row_ids(fetcher)
        console.print(f"[dim]{len(rows)} firma satırı bulundu.[/]\n")
        if limit:
            rows = rows[:limit]

        resolved = empty = 0
        for index, row in enumerate(rows, start=1):
            info = ari_mod.fetch_company_info(fetcher, row.row_id)
            website = ari_mod.resolve_website(info) if info else None
            if website:
                repo.upsert_company(name=row.title, website=website, source_key="ari_teknokent")
                resolved += 1
                console.print(f"  {index:>4}/{len(rows)}  {row.title[:40]:<40} [green]{website}[/]")
            else:
                empty += 1

    console.print(f"\n[bold]{resolved} site adresi bulundu[/] · {empty} boş.")


@app.command("sources")
def sources_cmd() -> None:
    """Kaynakların son durumu — hangisi çalışıyor, hangisi bozuk."""
    _boot()
    rows = db.connect().execute(
        "SELECT * FROM sources ORDER BY kind, key"
    ).fetchall()
    table = Table(box=None)
    for column in ("kaynak", "tür", "durum", "kayıt", "son koşu", "not"):
        table.add_column(column)
    colors = {"ok": "green", "error": "red", "empty": "yellow", "blocked": "yellow"}
    for row in rows:
        health = source_display_health(row["enabled"], row["health"])
        color = colors.get(health, "dim")
        table.add_row(
            row["name"], row["kind"], f"[{color}]{health}[/]",
            str(row["last_count"] if row["last_count"] is not None else "-"),
            (row["last_run_at"] or "-")[:16],
            (row["note"] or "")[:60],
        )
    console.print(table)


@app.command("detect")
def detect_cmd(
    limit: int = typer.Option(0, "--limit", help="Kaç şirket işlensin (0 = hepsi)"),
    recheck: bool = typer.Option(False, "--recheck", help="Daha önce bakılanları da tekrar tara"),
    delay: float = typer.Option(1.0, "--delay", help="Aynı siteye istekler arası saniye"),
) -> None:
    """Kariyer sayfalarını bulur ve ATS tespiti yapar."""
    _boot()
    conn = db.connect()
    if recheck:
        rows = conn.execute(
            "SELECT * FROM companies WHERE domain IS NOT NULL ORDER BY id"
        ).fetchall()
    else:
        rows = repo.companies_needing("detect")
    if limit:
        rows = rows[:limit]

    if not rows:
        console.print("[yellow]İşlenecek şirket yok.[/]")
        return

    found_ats = found_careers = blocked = failed = protected = 0
    with Fetcher(cache_dir=DEFAULT_CACHE, min_interval=delay) as fetcher:
        for index, row in enumerate(rows, start=1):
            result = detect_mod.detect_company(fetcher, row["domain"], row["website"])

            verified_provider = verified_slug = None
            note = None
            if result.found_ats:
                count = detect_mod.verify_ats(fetcher, result.ats_provider, result.ats_slug)
                if count is None:
                    # İmza bulundu ama uç nokta cevap vermedi: slug büyük olasılıkla yanlış.
                    note = f"{result.ats_provider}:{result.ats_slug} doğrulanamadı"
                else:
                    verified_provider, verified_slug = result.ats_provider, result.ats_slug

            if verified_provider:
                status_key, label = "ats", f"[green]{verified_provider}:{verified_slug}[/]"
                found_ats += 1
            elif result.blocked:
                status_key, label = "blocked", "[yellow]robots.txt engelledi[/]"
                blocked += 1
            elif result.protected:
                # Bot koruması. Tarayıcı/Firecrawl ile ayrı bir turda ele alınacak.
                status_key, label = "protected", "[yellow]bot koruması (403)[/]"
                note = result.error
                protected += 1
            elif result.error:
                status_key, label = "error", f"[red]{result.error}[/]"
                note = note or result.error
                failed += 1
            elif result.careers_url:
                status_key, label = "careers", "[cyan]kariyer sayfası var, ATS yok[/]"
            else:
                status_key, label = "none", "[dim]bulunamadı[/]"

            repo.save_detection(
                row["id"], result.careers_url, verified_provider, verified_slug,
                status=status_key, note=note,
            )
            if result.careers_url:
                found_careers += 1
            console.print(f"  {index:>4}/{len(rows)}  {row['name'][:34]:<34} {label}")

        console.print(
            f"\n[bold]Tespit bitti.[/] ATS: {found_ats} · kariyer sayfası: {found_careers} · "
            f"bot koruması: {protected} · robots engeli: {blocked} · hata: {failed}"
        )
        console.print(f"[dim]HTTP: {fetcher.stats}[/]")


@app.command("jobs")
def jobs_cmd(
    limit: int = typer.Option(0, "--limit", help="Kaç şirket işlensin (0 = hepsi)"),
    delay: float = typer.Option(1.0, "--delay"),
) -> None:
    """ATS'i tespit edilmiş şirketlerin açık ilanlarını çeker."""
    _boot()
    rows = db.connect().execute(
        "SELECT * FROM companies WHERE ats_provider IS NOT NULL ORDER BY id"
    ).fetchall()
    if limit:
        rows = rows[:limit]

    if not rows:
        console.print("[yellow]ATS'i tespit edilmiş şirket yok. Önce `detect` çalıştır.[/]")
        return

    total_jobs = total_interns = unreachable = 0
    with Fetcher(cache_dir=DEFAULT_CACHE, min_interval=delay) as fetcher:
        for row in rows:
            raw_jobs = ats.list_jobs(fetcher, row["ats_provider"], row["ats_slug"])
            if raw_jobs is None:
                # Uç nokta cevap vermedi. Mevcut ilanları pasife çekmiyoruz —
                # geçici bir kesinti tüm ilanları silmemeli.
                unreachable += 1
                console.print(f"  {row['name'][:34]:<34} [red]uç nokta cevap vermedi[/]")
                continue
            result = repo.sync_jobs(row["id"], raw_jobs)
            total_jobs += result["active"]
            total_interns += result["internships"]
            flag = f" [magenta]{result['internships']} staj[/]" if result["internships"] else ""
            console.print(f"  {row['name'][:34]:<34} {result['active']:>3} ilan{flag}")

    console.print(
        f"\n[bold]Toplam {total_jobs} aktif ilan, {total_interns} staj ilanı.[/]"
        + (f" [red]{unreachable} şirkette uç nokta hatası.[/]" if unreachable else "")
    )


@app.command("contacts")
def contacts_cmd(
    limit: int = typer.Option(0, "--limit", help="Kaç şirket işlensin (0 = hepsi)"),
    recheck: bool = typer.Option(False, "--recheck", help="Daha önce bakılanları da tekrar tara"),
    delay: float = typer.Option(1.0, "--delay", help="Aynı siteye istekler arası saniye"),
) -> None:
    """Şirketlerin kendi sitesinden e-posta/telefon toplar (regex geçişi).

    Yalnızca şirketin kendi alan adından toplanır; hiçbir e-posta türetilmez.
    İsimli kişi (CEO/İK) çıkarımı ayrı bir LLM adımı gerektirir — bkz. `enrich`.
    """
    _boot()
    conn = db.connect()
    if recheck:
        rows = conn.execute(
            "SELECT * FROM companies WHERE domain IS NOT NULL ORDER BY id"
        ).fetchall()
    else:
        rows = repo.companies_needing("contacts")
    if limit:
        rows = rows[:limit]

    if not rows:
        console.print("[yellow]İşlenecek şirket yok.[/]")
        return

    found = blocked = failed = 0
    total_contacts = total_hr = 0
    with Fetcher(cache_dir=DEFAULT_CACHE, min_interval=delay) as fetcher:
        for index, row in enumerate(rows, start=1):
            seed = (row["careers_url"],) if row["careers_url"] else ()
            harvest = contacts_mod.harvest_regex(fetcher, row["domain"], row["website"], seed)
            records = contacts_mod.to_contact_records(harvest)
            # Kayıt boş olsa bile save_contacts çağrılır: contacts_checked_at ancak
            # böyle yazılır. Çağrılmazsa checkpoint hiç ilerlemez, --recheck vermeden
            # bile aynı şirketler her koşuda yeniden taranır.
            written = repo.save_contacts(row["id"], records)

            hr_count = sum(1 for r in records if r["contact_type"] == "hr")
            total_contacts += written
            total_hr += hr_count

            if harvest.blocked:
                blocked += 1
                status = "[yellow]robots.txt engelledi[/]"
            elif harvest.error:
                failed += 1
                status = f"[red]{harvest.error}[/]"
            elif written:
                found += 1
                tag = f"[green]{hr_count} İK[/] + {written - hr_count} genel" if hr_count \
                    else f"{written} e-posta"
                status = tag
            else:
                status = "[dim]bulunamadı[/]"

            console.print(f"  {index:>4}/{len(rows)}  {row['name'][:34]:<34} {status}")

    console.print(
        f"\n[bold]İletişim taraması bitti.[/] {found} şirkette e-posta bulundu "
        f"({total_hr} İK, {total_contacts - total_hr} genel) · "
        f"engellenen: {blocked} · hata: {failed}"
    )
    console.print(f"[dim]HTTP: {fetcher.stats}[/]")


@app.command("enrich")
def enrich_cmd(
    backend: str = typer.Option("export", "--backend",
                                help="'api' (ANTHROPIC_API_KEY gerekir) veya 'export'"),
    limit: int = typer.Option(0, "--limit", help="Kaç şirket işlensin (0 = hepsi)"),
    delay: float = typer.Option(1.0, "--delay"),
    recheck: bool = typer.Option(False, "--recheck",
                                 help="Daha önce denenmiş şirketleri de tekrar işle"),
    out: Path = typer.Option(None, "--out", help="export backend'in JSONL çıktı yolu"),
    import_from: Path = typer.Option(
        None, "--import", help="İşlenmiş sonuç dosyasını okur ve veritabanına yazar"
    ),
) -> None:
    """İsimli kişi (CEO/kurucu/İK) çıkarımı — regex'in yapamadığı yargı adımı.

    Üç mod:
      --import <dosya>     bir sonuç JSONL'ini veritabanına yazar (ağ yok);
                            işlenen her şirket checkpoint'e işlenir
      --backend export     aday sayfaları JSONL'e döker, API anahtarı gerekmez;
                            checkpoint yalnızca --import'ta ilerler — aynı
                            parti iki kez export edilip henüz import edilmediyse
                            aynı şirketler tekrar dökülebilir, bu zararsızdır
      --backend api        ANTHROPIC_API_KEY ile canlı çalışır (pip install
                            "career-radar[llm]" gerekir); her şirket işlendikçe
                            (bulunamamış olsa bile) checkpoint'e işlenir

    Varsayılan olarak yalnızca `enrich_checked_at` boş şirketler işlenir —
    `detect`/`contacts` ile aynı resume deseni. `--recheck` bunu atlar.
    """
    _boot()

    if import_from is not None:
        results = extraction_mod.import_results(import_from)
        total_people = total_companies = 0
        for company_id, records in results.items():
            if records:
                repo.save_contacts(company_id, records)
                total_companies += 1
                total_people += len(records)
            repo.mark_enrich_checked(company_id)  # bulunamamış olsa da checkpoint ilerler
        console.print(
            f"[green]{total_people} kişi, {total_companies} şirkete yazıldı.[/] "
            f"({len(results)} şirket checkpoint'e işlendi)"
        )
        return

    if recheck:
        rows = db.connect().execute(
            "SELECT * FROM companies WHERE domain IS NOT NULL ORDER BY id"
        ).fetchall()
    else:
        rows = repo.companies_needing("enrich")
    if limit:
        rows = rows[:limit]
    if not rows:
        console.print("[yellow]İşlenecek şirket yok.[/]")
        return

    if backend == "api":
        try:
            import anthropic
        except ImportError:
            console.print(
                "[red]anthropic paketi kurulu değil.[/] "
                'Kurulum: pip install "career-radar[llm]"'
            )
            raise typer.Exit(1)
        client = anthropic.Anthropic()  # ANTHROPIC_API_KEY ortam değişkeninden okunur

    written_companies = written_people = 0
    export_rows: list[tuple[int, str, list]] = []

    with Fetcher(cache_dir=DEFAULT_CACHE, min_interval=delay) as fetcher:
        for index, row in enumerate(rows, start=1):
            seed = (row["careers_url"],) if row["careers_url"] else ()
            page_urls, blocked, error = contacts_mod.find_candidate_pages(
                fetcher, row["domain"], row["website"], seed
            )
            pages = []
            for url in page_urls:
                result = fetcher.get(url)
                if result.ok:
                    pages.append(extraction_mod.html_to_page_text(result.url, result.text))

            if backend == "export":
                export_rows.append((row["id"], row["name"], pages))
                console.print(f"  {index:>4}/{len(rows)}  {row['name'][:34]:<34} "
                              f"{'toplandı' if pages else '[dim]sayfa yok[/]'}")
                continue

            people = extraction_mod.extract_people_api(pages, client=client)
            records = extraction_mod.to_contact_records(people, pages)
            if records:
                repo.save_contacts(row["id"], records)
                written_companies += 1
                written_people += len(records)
            repo.mark_enrich_checked(row["id"])  # bulunamamış olsa da checkpoint ilerler
            label = f"[green]{len(records)} kişi[/]" if records else "[dim]bulunamadı[/]"
            console.print(f"  {index:>4}/{len(rows)}  {row['name'][:34]:<34} {label}")

    if backend == "export":
        out_path = out or (PROJECT_ROOT / "out" / "enrich_batch.jsonl")
        count = extraction_mod.export_batch(export_rows, out_path)
        console.print(
            f"\n[bold]{count} şirket dışa aktarıldı:[/] {out_path}\n"
            f"[dim]Bu dosyayı bir Claude Code oturumunda işletip sonucu "
            f"`career-radar enrich --import <sonuç.jsonl>` ile geri yükle.[/]"
        )
    else:
        console.print(
            f"\n[bold]{written_people} kişi, {written_companies} şirkette bulundu.[/]"
        )


@app.command("classify")
def classify_cmd(
    backend: str = typer.Option("export", "--backend",
                                help="'api' (ANTHROPIC_API_KEY gerekir) veya 'export'"),
    limit: int = typer.Option(0, "--limit", help="Kaç şirket işlensin (0 = hepsi)"),
    delay: float = typer.Option(1.0, "--delay"),
    recheck: bool = typer.Option(False, "--recheck"),
    out: Path = typer.Option(None, "--out", help="export backend'in JSONL çıktı yolu"),
    import_from: Path = typer.Option(
        None, "--import", help="İşlenmiş sonuç dosyasını okur ve veritabanına yazar"
    ),
) -> None:
    """Şirketi domain (yapay zeka/web/quantum/...), büyüklük, sektör olarak
    sınıflandırır — `taxonomy.yaml`'daki kapalı kümeden, `enrich` ile aynı
    export/api/import deseniyle. Kapalı küme dışı etiketler her zaman elenir."""
    _boot()
    taxonomy = classify_mod.load_taxonomy(PROJECT_ROOT / "taxonomy.yaml")

    if import_from is not None:
        results = classify_mod.import_results(import_from, taxonomy)
        total_domain_tags = total_companies = 0
        for company_id, (result, page_url) in results.items():
            repo.save_classification(
                company_id, domain=result.domain, size_bucket=result.size_bucket,
                sector=result.sector, stack=result.stack, evidence_url=page_url,
                model="import", confidence=result.confidence,
            )
            total_companies += 1
            total_domain_tags += len(result.domain)
        console.print(
            f"[green]{total_companies} şirket sınıflandırıldı[/] "
            f"({total_domain_tags} domain etiketi)."
        )
        return

    if recheck:
        rows = db.connect().execute(
            "SELECT * FROM companies WHERE domain IS NOT NULL ORDER BY id"
        ).fetchall()
    else:
        rows = repo.companies_needing("classify")
    if limit:
        rows = rows[:limit]
    if not rows:
        console.print("[yellow]İşlenecek şirket yok.[/]")
        return

    if backend == "api":
        try:
            import anthropic
        except ImportError:
            console.print(
                "[red]anthropic paketi kurulu değil.[/] "
                'Kurulum: pip install "career-radar[llm]"'
            )
            raise typer.Exit(1)
        client = anthropic.Anthropic()

    export_items: list[classify_mod.ClassificationInput] = []
    tagged = 0

    with Fetcher(cache_dir=DEFAULT_CACHE, min_interval=delay) as fetcher:
        for index, row in enumerate(rows, start=1):
            base = row["website"] or f"https://{row['domain']}/"
            page = fetcher.get(base)
            if not page.ok:
                page = fetcher.get(f"https://www.{row['domain']}/")
            page_text = extraction_mod.html_to_page_text(
                page.url if page.ok else base, page.text if page.ok else ""
            )
            hint = classify_mod.clean_hint(repo.directory_sector_hint(row["id"]))
            item = classify_mod.ClassificationInput(
                company_id=row["id"], name=row["name"],
                page_url=page_text.url, page_text=page_text.text, directory_hint=hint,
            )

            if backend == "export":
                if page_text.text:
                    export_items.append(item)
                    console.print(f"  {index:>4}/{len(rows)}  {row['name'][:34]:<34} toplandı")
                else:
                    # Sayfa hiç çekilemedi (kalıcı bot koruması, DNS hatası vb.) —
                    # checkpoint yine de ilerletilir; yoksa bu şirket HER export
                    # koşusunda tekrar denenir ve gerçekten sınıflandırılabilecek
                    # şirketlerin yerini işgal eder (gerçek koşuda görüldü: Trendyol/
                    # ASELSAN gibi hep-engellenen birkaç şirket her turu dolduruyordu).
                    repo.save_classification(row["id"], evidence_url=item.page_url)
                    console.print(f"  {index:>4}/{len(rows)}  {row['name'][:34]:<34} "
                                  f"[dim]sayfa yok[/]")
                continue

            result = classify_mod.classify_api(item, taxonomy, client=client)
            repo.save_classification(
                row["id"], domain=result.domain, size_bucket=result.size_bucket,
                sector=result.sector, stack=result.stack, evidence_url=item.page_url,
                model=classify_mod.HAIKU_MODEL, confidence=result.confidence,
            )
            tagged += len(result.domain)
            label = f"[green]{', '.join(result.domain)}[/]" if result.domain else "[dim]etiketsiz[/]"
            console.print(f"  {index:>4}/{len(rows)}  {row['name'][:34]:<34} {label}")

    if backend == "export":
        out_path = out or (PROJECT_ROOT / "out" / "classify_batch.jsonl")
        count = classify_mod.export_batch(export_items, taxonomy, out_path)
        console.print(
            f"\n[bold]{count} şirket dışa aktarıldı:[/] {out_path}\n"
            f"[dim]İşleyip sonucu `career-radar classify --import <sonuç.jsonl>` "
            f"ile geri yükle.[/]"
        )
    else:
        console.print(f"\n[bold]{tagged} domain etiketi yazıldı.[/]")


@app.command("serve")
def serve_cmd(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    reload: bool = typer.Option(False, "--reload", help="Geliştirme için otomatik yeniden yükle"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """Web arayüzünü başlatır."""
    import uvicorn

    _boot()
    url = f"http://{host}:{port}"
    console.print(f"[green]Career Radar:[/] {url}   [dim](durdurmak için Ctrl+C)[/]")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run("career_radar.api.app:app", host=host, port=port,
                reload=reload, log_level="warning")


@app.command("stats")
def stats_cmd() -> None:
    """Veritabanının mevcut durumu."""
    _boot()
    data = db.stats()
    labels = {
        "companies": "Şirket",
        "with_domain": "  alan adı olan",
        "with_careers_url": "  kariyer sayfası bulunan",
        "with_ats": "  ATS tespit edilen",
        "with_email": "  en az bir e-postası olan",
        "with_hr_email": "  İK/kariyer e-postası olan",
        "with_open_jobs": "  aktif ilanı olan",
        "with_internship": "  staj ilanı olan",
        "contacts": "İletişim kaydı",
        "jobs_active": "Aktif ilan",
        "labels": "Etiket",
        "sources": "Kaynak",
    }
    table = Table(show_header=False, box=None)
    total = max(data["companies"], 1)
    for key, label in labels.items():
        value = data[key]
        share = f"  ({value * 100 // total}%)" if label.startswith("  ") else ""
        table.add_row(label, str(value), share)
    console.print(table)


if __name__ == "__main__":
    app()
