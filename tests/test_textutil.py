from career_radar import textutil as tu


class TestTurkishLowering:
    def test_dotted_capital_i_becomes_plain_i(self):
        # Python'ın .lower()'ı burada birleşik noktalı karakter üretir; fold() üretmemeli.
        assert tu.fold("İnsan Kaynakları") == "insan kaynaklari"

    def test_dotless_capital_i_becomes_dotless(self):
        assert tu.tr_lower("IŞIK") == "ışık"

    def test_folding_strips_all_turkish_diacritics(self):
        assert tu.fold("ÇĞİÖŞÜ") == "cgiosu"


class TestCompanyNameNormalization:
    def test_strips_legal_suffixes(self):
        assert tu.normalize_company_name("ACME Bilişim A.Ş.") == "acme bilisim"
        assert tu.normalize_company_name("ACME Bilişim Ltd. Şti.") == "acme bilisim"

    def test_keeps_descriptive_words(self):
        # "teknoloji"/"yazılım" atılırsa farklı şirketler birbirine karışır.
        assert "teknoloji" in tu.normalize_company_name("Beta Teknoloji A.Ş.")

    def test_same_company_two_spellings_match(self):
        assert (tu.normalize_company_name("Öztürk Yazılım San. ve Tic. A.Ş.")
                == tu.normalize_company_name("OZTURK YAZILIM"))


class TestDomains:
    def test_strips_www_and_scheme(self):
        assert tu.domain_from_url("https://www.acme.com.tr/kariyer") == "acme.com.tr"

    def test_bare_domain_without_scheme(self):
        assert tu.domain_from_url("acme.com") == "acme.com"

    def test_turkish_two_level_tld_is_kept_whole(self):
        assert tu.registrable_domain("kariyer.acme.com.tr") == "acme.com.tr"

    def test_plain_subdomain_reduces_to_root(self):
        assert tu.registrable_domain("jobs.acme.com") == "acme.com"

    def test_rejects_non_domains(self):
        assert tu.domain_from_url("") is None
        assert tu.domain_from_url("localhost") is None


class TestEmails:
    def test_finds_and_deduplicates(self):
        text = "ik@acme.com veya İK: ik@acme.com, ayrıca info@acme.com"
        assert tu.find_emails(text) == ["ik@acme.com", "info@acme.com"]

    def test_ignores_image_filenames(self):
        assert tu.find_emails("logo@2x.png") == []

    def test_classifies_hr_mailboxes(self):
        assert tu.classify_email("kariyer@acme.com") == "hr"
        assert tu.classify_email("insankaynaklari@acme.com") == "hr"
        assert tu.classify_email("staj@acme.com") == "hr"

    def test_classifies_generic_and_personal(self):
        assert tu.classify_email("info@acme.com") == "generic"
        assert tu.classify_email("ahmet.yilmaz@acme.com") == "personal"


class TestInternshipDetection:
    def test_turkish_titles(self):
        assert tu.looks_like_internship("Yazılım Stajyeri")
        assert tu.looks_like_internship("Uzun Dönem Staj Programı")

    def test_english_titles(self):
        assert tu.looks_like_internship("Software Engineering Intern")
        assert tu.looks_like_internship("Working Student - Backend")

    def test_rejects_regular_roles(self):
        assert not tu.looks_like_internship("Senior Backend Engineer")
        assert not tu.looks_like_internship("İç Denetim Uzmanı")
