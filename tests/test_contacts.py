"""İletişim katmanı (regex geçişi) testleri — ağsız, sahte fetcher ile."""

from career_radar.net import FetchResult
from career_radar.pipeline import contacts as c


class FakePage:
    def __init__(self, status=200, text=""):
        self.status, self.text = status, text


class FakeFetcher:
    """URL -> FetchResult eşlemesi veren sahte fetcher."""

    def __init__(self, pages: dict[str, str], robots_blocked: set[str] = frozenset()):
        self.pages = pages
        self.robots_blocked = robots_blocked
        self.requested: list[str] = []

    def get(self, url, use_cache=True):
        self.requested.append(url)
        if url in self.robots_blocked:
            return FetchResult(url=url, status=None, text="", blocked_by_robots=True)
        if url in self.pages:
            return FetchResult(url=url, status=200, text=self.pages[url])
        return FetchResult(url=url, status=404, text="")


class TestExtractRegexContacts:
    def test_finds_mailto_link(self):
        html = '<a href="mailto:kariyer@acme.com">Bize yazın</a>'
        found = c.extract_regex_contacts(html, "https://acme.com/iletisim")
        assert [f.email for f in found] == ["kariyer@acme.com"]

    def test_finds_plain_text_email(self):
        html = "<p>Bize ulaşın: ik@acme.com</p>"
        found = c.extract_regex_contacts(html, "https://acme.com/iletisim")
        assert found[0].email == "ik@acme.com"

    def test_deduplicates_same_address_from_mailto_and_text(self):
        html = '<a href="mailto:info@acme.com">info@acme.com</a>'
        found = c.extract_regex_contacts(html, "https://acme.com/iletisim")
        assert len(found) == 1

    def test_ignores_image_filenames_that_look_like_emails(self):
        html = '<img src="logo@2x.png">'
        assert c.extract_regex_contacts(html, "https://acme.com/x") == []

    def test_phone_is_attached_to_first_email_not_stored_alone(self):
        """Telefon tek başına asla dönmez — isimsiz/e-postasız kayıt repo'da reddedilir."""
        html = "<p>Tel: 0212 555 66 77</p>"  # e-posta yok
        assert c.extract_regex_contacts(html, "https://acme.com/x") == []

    def test_phone_found_alongside_email_is_attached(self):
        html = "<p>ik@acme.com &mdash; Tel: 0212 555 66 77</p>"
        found = c.extract_regex_contacts(html, "https://acme.com/iletisim")
        assert found[0].email == "ik@acme.com"
        assert "555" in found[0].phone

    def test_multiple_emails_only_first_gets_phone(self):
        html = "<p>ik@acme.com, info@acme.com. Tel: 0212 555 66 77</p>"
        found = c.extract_regex_contacts(html, "https://acme.com/iletisim")
        assert found[0].phone is not None
        assert found[1].phone is None

    def test_no_contact_page_content_returns_empty(self):
        assert c.extract_regex_contacts("<p>Ürünlerimiz harika.</p>", "https://acme.com/x") == []

    def test_json_unicode_escape_does_not_glue_onto_email(self):
        """Gerçek veride görülen hata: '\\u003elegal@acme.com' -> 'u003elegal@acme.com'."""
        html = r"<script>{>legal@acme.com<}</script>"
        found = c.extract_regex_contacts(html, "https://acme.com/x")
        assert [f.email for f in found] == ["legal@acme.com"]

    def test_mailto_with_escaped_quote_does_not_leave_trailing_backslash(self):
        """Gerçek veride görülen hata: gömülü JSON'da mailto:x@y.com\\" -> 'x@y.com\\'."""
        html = r'<a href="mailto:info@acme.com\"">yaz</a>'
        found = c.extract_regex_contacts(html, "https://acme.com/x")
        assert found[0].email == "info@acme.com"

    def test_third_party_and_placeholder_emails_are_dropped_with_company_domain(self):
        html = (
            "<p>ik@acme.com sentrymonitoring@o12345.ingest.sentry.io "
            "support@partnerbrand.example ornek@mail.com email@example.com</p>"
        )
        found = c.extract_regex_contacts(html, "https://acme.com/x", company_domain="acme.com")
        assert [f.email for f in found] == ["ik@acme.com"]

    def test_kep_address_is_kept_despite_different_domain(self):
        html = "<p>acme@hs01.kep.tr</p>"
        found = c.extract_regex_contacts(html, "https://acme.com/x", company_domain="acme.com")
        assert [f.email for f in found] == ["acme@hs01.kep.tr"]

    def test_subdomain_of_company_domain_is_kept(self):
        html = "<p>ik@kariyer.acme.com</p>"
        found = c.extract_regex_contacts(html, "https://acme.com/x", company_domain="acme.com")
        assert [f.email for f in found] == ["ik@kariyer.acme.com"]

    def test_without_company_domain_no_filtering_applied(self):
        """Domain verilmezse (geriye dönük uyum) filtre uygulanmaz."""
        html = "<p>ik@acme.com destek@baskasite.com</p>"
        found = c.extract_regex_contacts(html, "https://acme.com/x")
        assert len(found) == 2


class TestIsOwnDomainEmail:
    def test_exact_domain_match(self):
        assert c.is_own_domain_email("ik@acme.com", "acme.com")

    def test_subdomain_match(self):
        assert c.is_own_domain_email("ik@kariyer.acme.com", "acme.com")

    def test_kep_tr_always_allowed(self):
        assert c.is_own_domain_email("acme@hs01.kep.tr", "acme.com")

    def test_unrelated_domain_rejected(self):
        assert not c.is_own_domain_email("info@baskasirket.com", "acme.com")

    def test_lookalike_domain_without_dot_boundary_rejected(self):
        # "notacme.com" ham metin olarak "acme.com" ile bitmiyor (aradaki nokta şart);
        # saf .endswith("acme.com") kullansaydık bu yanlışlıkla eşleşirdi.
        assert not c.is_own_domain_email("x@notacme.com", "acme.com")


class TestFindCandidatePages:
    def test_discovers_turkish_contact_link(self):
        home = '<a href="/iletisim">İletişim</a>'
        fetcher = FakeFetcher({"https://acme.com/": home})
        pages, blocked, error = c.find_candidate_pages(fetcher, "acme.com", None)
        assert not blocked and error is None
        assert "https://acme.com/iletisim" in pages

    def test_seed_urls_are_included_without_extra_fetch(self):
        """Kariyer sayfası detect aşamasından zaten biliniyorsa yeniden aranmaz."""
        fetcher = FakeFetcher({"https://acme.com/": "<html></html>"})
        pages, _, _ = c.find_candidate_pages(
            fetcher, "acme.com", None, seed_urls=("https://acme.com/kariyer",)
        )
        assert "https://acme.com/kariyer" in pages

    def test_robots_blocked_home_page_reported(self):
        fetcher = FakeFetcher({}, robots_blocked={"https://acme.com/"})
        pages, blocked, error = c.find_candidate_pages(fetcher, "acme.com", None)
        assert blocked is True
        assert pages == []

    def test_unreachable_domain_reports_error_not_crash(self):
        fetcher = FakeFetcher({})  # her şey 404
        pages, blocked, error = c.find_candidate_pages(fetcher, "yok-boyle-bir-site.com", None)
        assert pages == []
        assert not blocked
        assert error is not None

    def test_second_hop_finds_pages_linked_from_a_hub_page(self):
        """Gerçek veride görülen hata: Hepsiburada'nın gerçek yönetim sayfası ana
        sayfadan değil, 'kurumsal' hub'ından linkleniyordu — tek atlamalı keşif
        bunu hiç bulamıyordu."""
        pages = {
            "https://acme.com/": '<a href="/kurumsal">Kurumsal</a>',
            "https://acme.com/kurumsal": '<a href="/kurumsal/ust-yonetim">Üst Yönetim</a>',
        }
        fetcher = FakeFetcher(pages)
        found, _, _ = c.find_candidate_pages(fetcher, "acme.com", None)
        assert "https://acme.com/kurumsal/ust-yonetim" in found

    def test_second_hop_is_bounded_and_skips_unreachable_hubs(self):
        """Ulaşılamayan bir hub sayfası akışı durdurmamalı."""
        pages = {"https://acme.com/": '<a href="/kurumsal">Kurumsal</a>'}
        fetcher = FakeFetcher(pages)  # /kurumsal 404 döner
        found, blocked, error = c.find_candidate_pages(fetcher, "acme.com", None)
        assert not blocked and error is None
        assert "https://acme.com/kurumsal" in found

    def test_falls_back_to_fixed_paths_when_no_links_found(self):
        fetcher = FakeFetcher({"https://acme.com/": "<html><body>boş</body></html>"})
        pages, _, _ = c.find_candidate_pages(fetcher, "acme.com", None)
        assert any("iletisim" in p or "contact" in p for p in pages)


class TestHarvestRegex:
    def test_end_to_end_collects_and_dedupes(self):
        pages = {
            "https://acme.com/": '<a href="/iletisim">İletişim</a>',
            "https://acme.com/iletisim": (
                '<a href="mailto:kariyer@acme.com">Kariyer</a>'
                '<p>info@acme.com de yazabilirsiniz. Tel: 0212 111 22 33</p>'
            ),
        }
        fetcher = FakeFetcher(pages)
        harvest = c.harvest_regex(fetcher, "acme.com", None)
        emails = {contact.email for contact in harvest.contacts}
        assert emails == {"kariyer@acme.com", "info@acme.com"}

    def test_to_contact_records_classifies_hr_vs_generic(self):
        pages = {
            "https://acme.com/": '<a href="/iletisim">İletişim</a>',
            "https://acme.com/iletisim": (
                '<a href="mailto:kariyer@acme.com"></a><a href="mailto:info@acme.com"></a>'
            ),
        }
        harvest = c.harvest_regex(FakeFetcher(pages), "acme.com", None)
        records = {r["email"]: r["contact_type"] for r in c.to_contact_records(harvest)}
        assert records["kariyer@acme.com"] == "hr"
        assert records["info@acme.com"] == "generic"

    def test_records_carry_source_url_for_every_contact(self):
        pages = {
            "https://acme.com/": '<a href="/iletisim">İletişim</a>',
            "https://acme.com/iletisim": '<a href="mailto:ik@acme.com"></a>',
        }
        harvest = c.harvest_regex(FakeFetcher(pages), "acme.com", None)
        assert all(r["source_url"] for r in c.to_contact_records(harvest))

    def test_blocked_home_page_yields_no_contacts_not_a_crash(self):
        fetcher = FakeFetcher({}, robots_blocked={"https://acme.com/"})
        harvest = c.harvest_regex(fetcher, "acme.com", None)
        assert harvest.blocked is True
        assert harvest.contacts == []
