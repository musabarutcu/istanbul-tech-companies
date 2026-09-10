"""Dedup ve ilan senkronizasyonu testleri — geçici, bellekteki bir veritabanı üzerinde."""

from dataclasses import dataclass

import pytest

from career_radar import db, repo


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    db.close()
    db.configure(tmp_path / "test.db")
    db.init()
    yield
    db.close()


@dataclass
class Job:
    external_id: str
    title: str
    location: str | None = None
    url: str | None = None
    posted_at: str | None = None
    remote: bool | None = None


class TestDeduplication:
    def test_same_domain_is_one_company(self):
        first, created_a = repo.upsert_company("ACME A.Ş.", website="https://acme.com.tr")
        second, created_b = repo.upsert_company("ACME", website="https://www.acme.com.tr/tr")
        assert first == second
        assert created_a and not created_b

    def test_subdomain_merges_into_root(self):
        first, _ = repo.upsert_company("ACME", website="https://acme.com.tr")
        second, _ = repo.upsert_company("ACME Kariyer", website="https://kariyer.acme.com.tr")
        assert first == second

    def test_same_name_without_domain_merges(self):
        first, _ = repo.upsert_company("Öztürk Yazılım San. ve Tic. A.Ş.")
        second, _ = repo.upsert_company("OZTURK YAZILIM")
        assert first == second

    def test_different_companies_stay_separate(self):
        first, _ = repo.upsert_company("Alfa Teknoloji", website="https://alfa.com")
        second, _ = repo.upsert_company("Beta Teknoloji", website="https://beta.com")
        assert first != second

    def test_existing_fields_are_not_overwritten_by_blanks(self):
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com", city="Ankara")
        repo.upsert_company("ACME", website="https://acme.com", city=None)
        row = db.connect().execute(
            "SELECT city FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert row["city"] == "Ankara"

    def test_source_attribution_is_recorded_for_each_directory(self):
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com",
                                            source_key="odtu_teknokent")
        repo.upsert_company("ACME", website="https://acme.com", source_key="tubisad")
        count = db.connect().execute(
            "SELECT COUNT(*) FROM company_sources WHERE company_id = ?", (company_id,)
        ).fetchone()[0]
        assert count == 2


class TestSaveClassification:
    def test_domain_labels_are_written(self):
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com")
        repo.save_classification(company_id, domain=["ai_ml", "web_saas"],
                                 evidence_url="https://acme.com", model="haiku")
        labels = {r["label"] for r in db.connect().execute(
            "SELECT label FROM company_labels WHERE company_id = ? AND dimension = 'domain'",
            (company_id,),
        )}
        assert labels == {"ai_ml", "web_saas"}

    def test_size_and_sector_written_to_company_columns_not_labels(self):
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com")
        repo.save_classification(company_id, size_bucket="kucuk", sector="urun",
                                 evidence_url="https://acme.com")
        row = db.connect().execute(
            "SELECT size_bucket, sector FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert row["size_bucket"] == "kucuk"
        assert row["sector"] == "urun"

    def test_existing_size_bucket_is_not_overwritten(self):
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com")
        repo.save_classification(company_id, size_bucket="kucuk", evidence_url="https://a")
        repo.save_classification(company_id, size_bucket="buyuk", evidence_url="https://a")
        row = db.connect().execute(
            "SELECT size_bucket FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert row["size_bucket"] == "kucuk"

    def test_stamps_classify_checked_at_even_with_no_labels(self):
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com")
        repo.save_classification(company_id, evidence_url="https://acme.com")
        row = db.connect().execute(
            "SELECT classify_checked_at FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert row["classify_checked_at"] is not None


class TestEnrichCheckpoint:
    """enrich komutunun checkpoint'i — olmadan her koşu id sırasına göre baştan başlar."""

    def test_company_needing_enrich_is_returned_by_default(self):
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com")
        assert company_id in [r["id"] for r in repo.companies_needing("enrich")]

    def test_marked_company_is_excluded_from_next_batch(self):
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com")
        repo.mark_enrich_checked(company_id)
        assert company_id not in [r["id"] for r in repo.companies_needing("enrich")]

    def test_marking_does_not_require_any_contacts_found(self):
        """'Kimse bulunamadı' da geçerli bir sonuçtur — checkpoint yine ilerlemeli."""
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com")
        repo.mark_enrich_checked(company_id)
        row = db.connect().execute(
            "SELECT enrich_checked_at FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert row["enrich_checked_at"] is not None

    def test_contacts_checkpoint_is_independent_of_enrich_checkpoint(self):
        """Regex geçişi bitmiş olması, LLM çıkarımının da bittiği anlamına gelmez."""
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com")
        repo.save_contacts(company_id, [])  # contacts_checked_at'i ilerletir
        needing_enrich = [r["id"] for r in repo.companies_needing("enrich")]
        assert company_id in needing_enrich


class TestExclusiveToSources:
    """Kapsam daraltmalarında (ör. Ankara teknokenti kapatılması) güvenli temizlik."""

    def test_company_only_in_closed_source_is_returned(self):
        company_id, _ = repo.upsert_company("Sadece Cyberpark", website="https://a.com",
                                            city="Ankara", source_key="cyberpark")
        found = repo.companies_exclusive_to_sources(("cyberpark", "odtu_teknokent"),
                                                     city="Ankara")
        assert [row["id"] for row in found] == [company_id]

    def test_company_also_seen_elsewhere_is_kept(self):
        """Cyberpark'ta da geçse, TÜBİSAD'da da geçen bir şirket silinmemeli."""
        company_id, _ = repo.upsert_company("İki Kaynaklı", website="https://b.com",
                                            city="Ankara", source_key="cyberpark")
        repo.upsert_company("İki Kaynaklı", website="https://b.com",
                            city="Ankara", source_key="tubisad_kurumsal")
        found = repo.companies_exclusive_to_sources(("cyberpark", "odtu_teknokent"),
                                                     city="Ankara")
        assert company_id not in [row["id"] for row in found]

    def test_manually_seeded_company_is_kept(self):
        """ASELSAN gibi elle eklenmiş bir Ankara şirketi 'manual_seed' kaynaklı — silinmez."""
        company_id, _ = repo.upsert_company("ASELSAN", website="https://aselsan.com.tr",
                                            city="Ankara", source_key="manual_seed")
        found = repo.companies_exclusive_to_sources(("cyberpark", "odtu_teknokent"),
                                                     city="Ankara")
        assert company_id not in [row["id"] for row in found]

    def test_city_filter_excludes_other_cities(self):
        repo.upsert_company("İstanbul Şirketi", website="https://c.com",
                            city="İstanbul", source_key="cyberpark")
        found = repo.companies_exclusive_to_sources(("cyberpark",), city="Ankara")
        assert found == []


class TestJobSync:
    def test_inserts_and_flags_internships(self):
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com")
        result = repo.sync_jobs(company_id, [
            Job("1", "Backend Engineer", "İstanbul"),
            Job("2", "Yazılım Stajyeri", "Ankara"),
        ])
        assert result["active"] == 2
        assert result["internships"] == 1

    def test_disappeared_jobs_become_inactive(self):
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com")
        repo.sync_jobs(company_id, [Job("1", "Backend Engineer"), Job("2", "QA Engineer")])
        result = repo.sync_jobs(company_id, [Job("1", "Backend Engineer")])
        assert result["active"] == 1
        inactive = db.connect().execute(
            "SELECT COUNT(*) FROM jobs WHERE company_id = ? AND is_active = 0", (company_id,)
        ).fetchone()[0]
        assert inactive == 1

    def test_rerun_does_not_duplicate(self):
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com")
        jobs = [Job("1", "Backend Engineer", "İstanbul")]
        repo.sync_jobs(company_id, jobs)
        repo.sync_jobs(company_id, jobs)
        total = db.connect().execute(
            "SELECT COUNT(*) FROM jobs WHERE company_id = ?", (company_id,)
        ).fetchone()[0]
        assert total == 1

    def test_company_counters_are_updated(self):
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com")
        repo.sync_jobs(company_id, [Job("1", "Stajyer Yazılım Mühendisi")])
        row = db.connect().execute(
            "SELECT open_jobs_count, has_internship FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert row["open_jobs_count"] == 1
        assert row["has_internship"] == 1


class TestContacts:
    def test_source_url_is_mandatory(self):
        """Kaynağı gösterilemeyen iletişim kaydı yazılamaz — doğrulanabilirlik şartı."""
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com")
        with pytest.raises(ValueError):
            repo.save_contacts(company_id, [{"email": "ik@acme.com"}])

    def test_empty_list_still_stamps_checked_at(self):
        """save_contacts([]) çağrısı checkpoint'i ilerletmeli — yoksa hiç kayıt
        bulunamayan şirketler her koşuda sonsuza kadar yeniden taranır."""
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com")
        repo.save_contacts(company_id, [])
        row = db.connect().execute(
            "SELECT contacts_checked_at FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert row["contacts_checked_at"] is not None

    def test_hr_mailbox_is_counted_separately(self):
        company_id, _ = repo.upsert_company("ACME", website="https://acme.com")
        repo.save_contacts(company_id, [
            {"email": "kariyer@acme.com", "source_url": "https://acme.com/iletisim"},
            {"email": "info@acme.com", "source_url": "https://acme.com/iletisim"},
        ])
        row = db.connect().execute(
            "SELECT email_count, hr_email_count FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert row["email_count"] == 2
        assert row["hr_email_count"] == 1
