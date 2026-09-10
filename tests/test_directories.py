"""Dizin bağlayıcısı testleri — gerçek sitelerden alınmış yapılara göre yazıldı."""

import pytest
from selectolax.parser import HTMLParser

from career_radar.net import FetchResult
from career_radar.pipeline.connectors import directories as d


def parse(html: str) -> HTMLParser:
    return HTMLParser(html)


class TestCompanySiteFilter:
    def test_accepts_plain_company_domain(self):
        assert d.is_company_site("https://acme.com.tr/", "cyberpark.com.tr")

    def test_rejects_social_networks(self):
        for url in ["https://www.linkedin.com/company/x", "https://twitter.com/x",
                    "https://www.instagram.com/x", "https://www.youtube.com/c/x"]:
            assert not d.is_company_site(url, "cyberpark.com.tr")

    def test_rejects_the_directory_itself(self):
        assert not d.is_company_site("https://www.cyberpark.com.tr/hakkimizda",
                                     "www.cyberpark.com.tr")

    def test_rejects_official_registries_and_universities(self):
        # Dizinler sıklıkla MERSİS/ticaret sicil ve üniversite bağlantısı taşır.
        assert not d.is_company_site("https://mersis.ticaret.gov.tr/x", "ari.com")
        assert not d.is_company_site("https://www.itu.edu.tr", "ari.com")

    def test_rejects_relative_and_malformed(self):
        assert not d.is_company_site("/kariyer", "acme.com")
        assert not d.is_company_site("mailto:a@b.com", "acme.com")

    def test_rejects_documents_and_newsletter_forms(self):
        """Dizin sayfaları sık sık KID/GDPR PDF'i ve bülten kayıt bağlantısı taşır."""
        assert not d.is_company_site(
            "https://cdn.example.com/2021%2003%20Fund%20KID.pdf", "acme.com")
        assert not d.is_company_site(
            "https://acme.us7.list-manage.com/subscribe?u=x", "acme.com")
        assert not d.is_company_site("http://eepurl.com/dEcuVP", "acme.com")
        assert not d.is_company_site("https://medium.com/acme-blog", "acme.com")
        assert not d.is_company_site("http://app.junipersquare.com/i/212", "212.vc")

    def test_x_com_is_not_a_substring_trap(self):
        """'x.com' (Twitter/X) yalnızca gerçek alan adı sınırında elenmeli.

        Önceki regex düz metin arıyordu; "defensx.com" gibi meşru bir firma alan
        adı da "x.com" içerdiği için yanlışlıkla eleniyordu.
        """
        assert not d.is_company_site("https://x.com/someco", "acme.com")
        assert d.is_company_site("https://defensx.com", "acme.com")
        assert d.is_company_site("https://www.defensx.com/", "acme.com")


class TestStructuredExtraction:
    def test_dijitalpark_shape(self):
        """Ad anchor'ın title'ında, site href'inde."""
        html = """
        <div class="firma-card"><a href="https://2sworks.com/"
             title="2sworks Bilişim Yazılım Ticaret Sanayi Anonim Şirketi"></a></div>
        """
        source = d.DirectorySource(key="k", kind="teknokent", name="n", list_url="x",
                                   item_selector="div.firma-card", name_attr="title")
        entries = d.extract_structured(parse(html), source, "https://dp.com.tr/firmalar")
        assert len(entries) == 1
        assert entries[0].name.startswith("2sworks")
        assert entries[0].website == "https://2sworks.com/"

    def test_ari_shape_reads_full_sector_from_title(self):
        """Görünen sektör metni kısaltılmış ('+1'); tam hali title'da."""
        html = """
        <div class="company-card">
          <div class="company-card__title">25 PROJE TEKNOLOJİ LİMİTED ŞİRKETİ</div>
          <div class="company-card__sector" title="Bilgisayar ve İletişim Teknolojileri, Yazılım">
            <p>Bilgisayar ve İletişim Teknolojileri +1</p>
          </div>
        </div>
        """
        source = d.DirectorySource(
            key="k", kind="teknokent", name="n", list_url="x",
            item_selector="div.company-card", name_selector="div.company-card__title",
            sector_selector="div.company-card__sector", sector_attr="title",
        )
        entries = d.extract_structured(parse(html), source, "https://ari.com.tr/f")
        assert entries[0].sector == "Bilgisayar ve İletişim Teknolojileri, Yazılım"
        assert entries[0].website is None

    def test_falls_back_to_domain_when_name_missing(self):
        """Yıldız Teknopark logoyu arka plan görseli yapıyor; alt metni yok."""
        html = '<div class="block"><a href="http://www.etiya.com"></a></div>'
        source = d.DirectorySource(key="k", kind="teknokent", name="n", list_url="x",
                                   item_selector="div.block")
        entries = d.extract_structured(parse(html), source, "https://ytp.com.tr/f")
        assert entries[0].name == "etiya.com"

    def test_bilisim_vadisi_shape(self):
        """Ad title div'inde, site anchor href'inde — Vadistanbul kampüsü listesi."""
        html = """
        <div class="ndr-grid-item">
          <a href="https://4cteknoloji.com" target="_blank">
            <div class="ndr-grid-item-image"></div>
            <div class="ndr-grid-item-content">
              <div class="ndr-grid-item-title">4C Teknoloji</div>
            </div>
          </a>
        </div>
        """
        source = d.DirectorySource(key="k", kind="teknokent", name="n", list_url="x",
                                   item_selector="div.ndr-grid-item",
                                   name_selector="div.ndr-grid-item-title")
        entries = d.extract_structured(parse(html), source, "https://bilisimvadisi.com.tr/f")
        assert entries[0].name == "4C Teknoloji"
        assert entries[0].website == "https://4cteknoloji.com"

    def test_item_without_name_or_site_is_dropped(self):
        html = '<div class="block"><span>—</span></div>'
        source = d.DirectorySource(key="k", kind="teknokent", name="n", list_url="x",
                                   item_selector="div.block")
        assert d.extract_structured(parse(html), source, "https://x.com/f") == []


class TestOutboundExtraction:
    def test_collects_company_links_and_skips_noise(self):
        html = """
        <a href="https://www.etiya.com">Etiya</a>
        <a href="https://www.linkedin.com/company/x">LinkedIn</a>
        <a href="https://www.ardbilisim.com.tr" title="ARD Bilişim">ARD</a>
        <a href="/hakkimizda">Hakkımızda</a>
        """
        source = d.DirectorySource(key="k", kind="dernek", name="n",
                                   list_url="x", mode="outbound")
        entries = d.extract_outbound(parse(html), source, "https://tubisad.org.tr/uyeler")
        names = sorted(e.name for e in entries)
        assert names == ["ARD Bilişim", "Etiya"]

    def test_deduplicates_by_domain(self):
        html = ('<a href="https://acme.com/a">Acme</a>'
                '<a href="https://acme.com/b">Acme Yazılım</a>')
        source = d.DirectorySource(key="k", kind="dernek", name="n",
                                   list_url="x", mode="outbound")
        assert len(d.extract_outbound(parse(html), source, "https://x.org/")) == 1

    def test_url_like_text_falls_back_to_image_alt_then_domain(self):
        html = '<a href="https://acme.com">https://acme.com</a>'
        source = d.DirectorySource(key="k", kind="dernek", name="n",
                                   list_url="x", mode="outbound")
        assert d.extract_outbound(parse(html), source, "https://x.org/")[0].name == "acme.com"

    def test_item_selector_scopes_the_scan_to_one_container(self):
        """ScaleX/Revo/Endeavor gibi sitelerde sayfa geneli tarama, alıntı/haber/
        footer bağlantılarını da (co-investor adı, "site by" kredisi, basın linki)
        firma sanıyordu. item_selector verilince yalnızca o kapsayıcı taranır.
        """
        html = """
        <div class="footer"><a href="https://wideeye.co">Wide Eye</a></div>
        <div class="featured"><a href="https://airalo.com">Airalo</a></div>
        """
        source = d.DirectorySource(key="k", kind="vc", name="n", list_url="x",
                                   mode="outbound", item_selector="div.featured")
        entries = d.extract_outbound(parse(html), source, "https://endeavor.org.tr/")
        assert [e.name for e in entries] == ["Airalo"]

    def test_without_item_selector_scans_whole_document(self):
        """Geriye dönük uyum: item_selector verilmezse eski davranış korunur."""
        html = '<a href="https://acme.com">Acme</a>'
        source = d.DirectorySource(key="k", kind="dernek", name="n",
                                   list_url="x", mode="outbound")
        entries = d.extract_outbound(parse(html), source, "https://x.org/")
        assert [e.name for e in entries] == ["Acme"]


class TestPagination:
    def test_page_url_building(self):
        source = d.DirectorySource(key="k", kind="t", name="n",
                                   list_url="https://x.com/list", page_param="page")
        assert d._page_url(source, 1) == "https://x.com/list"
        assert d._page_url(source, 3) == "https://x.com/list?page=3"

    def test_page_url_preserves_existing_query(self):
        source = d.DirectorySource(key="k", kind="t", name="n",
                                   list_url="https://x.com/list?a=1", page_param="page")
        assert d._page_url(source, 2) == "https://x.com/list?a=1&page=2"

    def test_crawl_stops_when_site_ignores_page_param(self):
        """Sayfa parametresini yok sayan siteler aynı içeriği sonsuza kadar döndürür."""
        calls = []

        class RepeatingFetcher:
            def get(self, url, use_cache=True):
                calls.append(url)
                return FetchResult(url=url, status=200,
                                   text='<div class="i"><a href="https://a.com">A</a></div>')

        source = d.DirectorySource(key="k", kind="t", name="n", list_url="https://x.com/l",
                                   item_selector="div.i", page_param="page", max_pages=50)
        entries = d.crawl(RepeatingFetcher(), source, resolve_details=False)
        assert len(entries) == 1
        assert len(calls) == 2  # sayfa 1 ve 2; ikisi aynı olunca durdu

    def test_crawl_stops_on_empty_page(self):
        class TwoPageFetcher:
            def get(self, url, use_cache=True):
                body = ('<div class="i"><a href="https://a.com">A</a></div>'
                        if "page=" not in url else "<html></html>")
                return FetchResult(url=url, status=200, text=body)

        source = d.DirectorySource(key="k", kind="t", name="n", list_url="https://x.com/l",
                                   item_selector="div.i", page_param="page", max_pages=50)
        assert len(d.crawl(TwoPageFetcher(), source, resolve_details=False)) == 1


class TestDetailResolution:
    def test_prefers_link_whose_text_is_a_raw_url(self):
        """Teknopol firma sitesini ham URL metni olarak basıyor; en güvenilir ipucu bu."""
        html = """
        <a href="https://argeportal.sbu.edu.tr">Argeportal</a>
        <a href="https://www.teknopolistto.com/">Teknoloji Transfer Ofisi</a>
        <a href="https://www.24teknoloji.com/">https://www.24teknoloji.com/</a>
        """

        class F:
            def get(self, url, use_cache=True):
                return FetchResult(url=url, status=200, text=html)

        found = d.resolve_website_from_detail(F(), "https://teknopolistanbul.com/firma/x/")
        assert found == "https://www.24teknoloji.com/"

    def test_returns_none_when_page_unreachable(self):
        class F:
            def get(self, url, use_cache=True):
                return FetchResult(url=url, status=404, text="")

        assert d.resolve_website_from_detail(F(), "https://x.com/f") is None

    def test_prefers_bare_domain_text_over_sitewide_promo_link(self):
        """SASAD firma detay sayfaları adresi şemasız basar: "Web Adresi: adik.com.tr".

        Böyle sayfalarda ayrıca sitewide bir reklam/sponsor bağlantısı (ör. bir etkinlik
        sitesi) da bulunabilir ve dokümanda firma adresinden önce geçer; eski kod bunu
        ilk-eşleşme varsayımıyla yanlışlıkla seçiyordu.
        """
        html = """
        <div class="sticky-btn"><a href="https://www.futuresoldier.com.tr/"><img alt=""></a></div>
        <p>Web Adresi: <a href="https://www.adik.com.tr/">adik.com.tr</a></p>
        """

        class F:
            def get(self, url, use_cache=True):
                return FetchResult(url=url, status=200, text=html)

        found = d.resolve_website_from_detail(F(), "https://www.sasad.org.tr/adik-anadolu-tersanesi")
        assert found == "https://www.adik.com.tr/"
