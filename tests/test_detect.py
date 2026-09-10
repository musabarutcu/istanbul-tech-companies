"""Kariyer sayfası bulma testleri.

Buradaki ilk sınıf gerçek bir hatanın regresyon testi: anahtar kelimeler boşluklu
("insan kaynak") ama URL'ler tireli ("insan-kaynaklari") olduğu ve eşleştirme
Python'ın Türkçe'de bozuk çalışan .lower()'ı ile yapıldığı için, Roketsan ve Turkcell
gibi sitelerde kariyer bağlantısı HTML'de dururken bulunamıyordu.
"""

from career_radar.pipeline import detect


def links(html: str, domain: str = "acme.com.tr", base: str | None = None):
    return detect.career_links_in(html, base or f"https://{domain}/", domain)


class TestTurkishLinkMatching:
    def test_hyphenated_turkish_path(self):
        html = '<a href="/tr/insan-kaynaklari">İnsan Kaynakları</a>'
        assert links(html) == ["https://acme.com.tr/tr/insan-kaynaklari"]

    def test_dotted_capital_i_in_link_text(self):
        html = '<a href="/x">İNSAN KAYNAKLARI</a>'
        assert links(html) == ["https://acme.com.tr/x"]

    def test_plain_kariyer_path(self):
        html = '<a href="https://www.acme.com.tr/kariyer">Kariyer</a>'
        assert links(html) == ["https://www.acme.com.tr/kariyer"]

    def test_underscore_separator(self):
        html = '<a href="/acik_pozisyonlar">Açık Pozisyonlar</a>'
        assert links(html) == ["https://acme.com.tr/acik_pozisyonlar"]


class TestLinkFiltering:
    def test_social_links_are_skipped(self):
        html = '<a href="https://linkedin.com/company/acme">Kariyer</a>'
        assert links(html) == []

    def test_unrelated_external_links_are_skipped(self):
        html = '<a href="https://baskasite.com/kariyer">Kariyer</a>'
        assert links(html) == []

    def test_known_external_career_hosts_are_kept(self):
        # Getir gibi şirketler kariyer sayfasını dış bir platformda barındırıyor.
        html = '<a href="https://www.careers-page.com/getir-2">Kariyer</a>'
        assert links(html, domain="getir.com") == ["https://www.careers-page.com/getir-2"]

    def test_ats_hosts_are_kept_even_when_external(self):
        html = '<a href="https://jobs.lever.co/acme">Open positions</a>'
        assert links(html) == ["https://jobs.lever.co/acme"]

    def test_fragments_are_stripped_and_deduplicated(self):
        html = ('<a href="/kariyer#top">Kariyer</a>'
                '<a href="/kariyer">Career</a>')
        assert links(html) == ["https://acme.com.tr/kariyer"]

    def test_non_career_links_ignored(self):
        html = '<a href="/urunler">Ürünler</a><a href="/iletisim">İletişim</a>'
        assert links(html) == []


class TestSitemapFallback:
    def test_extracts_career_urls_from_sitemap(self):
        from career_radar.net import FetchResult

        class FakeFetcher:
            def get(self, url, use_cache=True):
                body = (
                    "<urlset><url><loc>https://acme.com.tr/urunler</loc></url>"
                    "<url><loc>https://acme.com.tr/insan-kaynaklari</loc></url>"
                    "<url><loc>https://acme.com.tr/kariyer</loc></url></urlset>"
                )
                return FetchResult(url=url, status=200, text=body)

        found = detect.career_links_in_sitemap(FakeFetcher(), "acme.com.tr")
        assert found == [
            "https://acme.com.tr/insan-kaynaklari",
            "https://acme.com.tr/kariyer",
        ]
