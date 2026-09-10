"""API testleri — geçici veritabanı, ağ yok."""

import pytest
from fastapi.testclient import TestClient

from career_radar import db, repo
from career_radar.api.app import create_app


@pytest.fixture
def client(tmp_path):
    db.close()
    app = create_app(tmp_path / "api.db")

    repo.upsert_source("cyberpark", "teknokent", "Bilkent CYBERPARK")
    repo.upsert_source("tubisad", "dernek", "TÜBİSAD")

    ankara, _ = repo.upsert_company("Acme Yazılım A.Ş.", website="https://acme.com.tr",
                                    city="Ankara", source_key="cyberpark")
    repo.add_label(ankara, "directory_sector", "Yazılım", evidence_url="https://d/1")
    repo.save_contacts(ankara, [
        {"email": "kariyer@acme.com.tr", "source_url": "https://acme.com.tr/iletisim"},
        {"email": "info@acme.com.tr", "source_url": "https://acme.com.tr/iletisim"},
    ])

    istanbul, _ = repo.upsert_company("Beta Robotik", website="https://beta.com",
                                      city="İstanbul", source_key="tubisad")
    repo.add_label(istanbul, "directory_sector", "Robotik", evidence_url="https://d/2")
    repo.save_classification(istanbul, domain=["robotik_otonom", "ai_ml"],
                             size_bucket="kucuk", sector="urun",
                             evidence_url="https://beta.com", model="test")

    class Job:
        def __init__(self, title):
            self.external_id, self.title = title, title
            self.location, self.url, self.posted_at, self.remote = "Ankara", None, None, None

    repo.sync_jobs(ankara, [Job("Yazılım Stajyeri"), Job("Backend Engineer")])

    with TestClient(app) as test_client:
        test_client.ids = {"ankara": ankara, "istanbul": istanbul}
        yield test_client
    db.close()


class TestListing:
    def test_returns_all_companies(self, client):
        data = client.get("/api/companies").json()
        assert data["total"] == 2

    def test_search_is_turkish_case_insensitive(self, client):
        # "Yazılım" ararken "yazilim" yazan da bulmalı.
        assert client.get("/api/companies", params={"q": "YAZILIM"}).json()["total"] == 1
        assert client.get("/api/companies", params={"q": "yazilim"}).json()["total"] == 1

    def test_filter_by_city(self, client):
        data = client.get("/api/companies", params={"city": "Ankara"}).json()
        assert [i["name"] for i in data["items"]] == ["Acme Yazılım A.Ş."]

    def test_filter_by_source(self, client):
        data = client.get("/api/companies", params={"source": "tubisad"}).json()
        assert data["total"] == 1

    def test_filter_by_sector(self, client):
        data = client.get("/api/companies", params={"sector": "Robotik"}).json()
        assert data["items"][0]["name"] == "Beta Robotik"

    def test_filter_by_category(self, client):
        """'category' kendi taksonomimiz (ai_ml...), 'sector' dizinin ham metni —
        ikisi karışmamalı."""
        data = client.get("/api/companies", params={"category": "ai_ml"}).json()
        assert data["items"][0]["name"] == "Beta Robotik"
        assert set(data["items"][0]["categories"]) == {"robotik_otonom", "ai_ml"}

    def test_category_and_sector_filters_are_independent(self, client):
        # Acme'nin dizin sektörü "Yazılım" ama hiç category etiketi yok.
        data = client.get("/api/companies", params={"category": "ai_ml"}).json()
        assert "Acme Yazılım A.Ş." not in [i["name"] for i in data["items"]]

    def test_size_bucket_and_sector_columns_exposed(self, client):
        data = client.get("/api/companies", params={"q": "beta"}).json()
        assert data["items"][0]["size_bucket"] == "kucuk"
        assert data["items"][0]["sector"] == "urun"

    def test_boolean_filters(self, client):
        assert client.get("/api/companies", params={"has_jobs": True}).json()["total"] == 1
        assert client.get("/api/companies", params={"has_jobs": False}).json()["total"] == 1
        assert client.get("/api/companies", params={"has_internship": True}).json()["total"] == 1
        assert client.get("/api/companies", params={"has_hr_email": True}).json()["total"] == 1

    def test_filters_combine_with_and(self, client):
        data = client.get("/api/companies",
                          params={"city": "Ankara", "has_internship": True}).json()
        assert data["total"] == 1
        data = client.get("/api/companies",
                          params={"city": "İstanbul", "has_internship": True}).json()
        assert data["total"] == 0

    def test_pagination_is_consistent(self, client):
        first = client.get("/api/companies", params={"page_size": 1, "page": 1}).json()
        second = client.get("/api/companies", params={"page_size": 1, "page": 2}).json()
        assert first["total"] == second["total"] == 2
        assert first["items"][0]["id"] != second["items"][0]["id"]

    def test_sorting_by_jobs(self, client):
        items = client.get("/api/companies", params={"sort": "jobs"}).json()["items"]
        assert items[0]["open_jobs_count"] >= items[1]["open_jobs_count"]

    def test_list_carries_sources_and_sectors(self, client):
        item = client.get("/api/companies", params={"city": "Ankara"}).json()["items"][0]
        assert item["sources"] == ["Bilkent CYBERPARK"]
        assert item["sectors"] == ["Yazılım"]


class TestDetail:
    def test_includes_contacts_jobs_and_provenance(self, client):
        detail = client.get(f"/api/companies/{client.ids['ankara']}").json()
        assert detail["name"] == "Acme Yazılım A.Ş."
        assert len(detail["jobs"]) == 2
        assert {c["email"] for c in detail["contacts"]} == {
            "kariyer@acme.com.tr", "info@acme.com.tr"}
        # Her iletişim kaydı kaynağını taşımalı — doğrulanabilirlik şartı.
        assert all(c["source_url"] for c in detail["contacts"])

    def test_hr_contact_is_listed_first(self, client):
        detail = client.get(f"/api/companies/{client.ids['ankara']}").json()
        assert detail["contacts"][0]["contact_type"] == "hr"

    def test_missing_company_returns_404(self, client):
        assert client.get("/api/companies/999999").status_code == 404


class TestPatch:
    def test_status_and_note_persist(self, client):
        company_id = client.ids["istanbul"]
        response = client.patch(f"/api/companies/{company_id}",
                                json={"user_status": "yazdim", "user_note": "CV yolladım"})
        assert response.status_code == 200
        again = client.get(f"/api/companies/{company_id}").json()
        assert again["user_status"] == "yazdim"
        assert again["user_note"] == "CV yolladım"

    def test_invalid_status_is_rejected(self, client):
        response = client.patch(f"/api/companies/{client.ids['istanbul']}",
                                json={"user_status": "olmayan_durum"})
        assert response.status_code == 422

    def test_status_can_be_cleared(self, client):
        company_id = client.ids["istanbul"]
        client.patch(f"/api/companies/{company_id}", json={"user_status": "listemde"})
        client.patch(f"/api/companies/{company_id}", json={"user_status": ""})
        assert client.get(f"/api/companies/{company_id}").json()["user_status"] is None


class TestFacetsAndExport:
    def test_facets_have_counts(self, client):
        facets = client.get("/api/facets").json()
        assert {f["value"] for f in facets["cities"]} == {"Ankara", "İstanbul"}
        assert all(f["count"] > 0 for f in facets["sources"])

    def test_category_facet_lists_domain_labels(self, client):
        facets = client.get("/api/facets").json()
        assert {f["value"] for f in facets["categories"]} == {"robotik_otonom", "ai_ml"}

    def test_csv_respects_active_filter(self, client):
        response = client.get("/api/export.csv", params={"city": "Ankara"})
        assert response.status_code == 200
        body = response.content.decode("utf-8-sig")
        assert "Acme Yazılım A.Ş." in body
        assert "Beta Robotik" not in body

    def test_csv_starts_with_bom_for_excel(self, client):
        # BOM olmadan Excel Türkçe karakterleri bozuk açıyor.
        assert client.get("/api/export.csv").content.startswith(b"\xef\xbb\xbf")


class TestConcurrency:
    def test_reads_work_while_another_connection_writes(self, client, tmp_path):
        """Boru hattı saatlerce yazarken arayüz okumaya devam edebilmeli (WAL doğrulaması)."""
        import sqlite3

        writer = sqlite3.connect(db.get_db_path(), timeout=10)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO companies (name, name_norm, first_seen, last_seen) "
            "VALUES ('Yazan', 'yazan', '2026-01-01', '2026-01-01')"
        )
        try:
            response = client.get("/api/companies")
            assert response.status_code == 200      # "database is locked" gelmemeli
        finally:
            writer.rollback()
            writer.close()
