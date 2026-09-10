"""İTÜ ARI Teknokent AJAX çözümleyici testleri — ağsız."""

from career_radar.net import FetchResult
from career_radar.pipeline.connectors import ari_teknokent as ari


class FakeFetcher:
    def __init__(self, pages: dict[str, str] = None, json_by_url: dict[str, dict] = None):
        self.pages = pages or {}
        self.json_by_url = json_by_url or {}
        self.requested: list[str] = []

    def get(self, url, use_cache=True):
        self.requested.append(url)
        if url in self.pages:
            return FetchResult(url=url, status=200, text=self.pages[url])
        return FetchResult(url=url, status=404, text="")

    def get_json(self, url, use_cache=True):
        self.requested.append(url)
        return self.json_by_url.get(url)


def card(row_id: str, title: str) -> str:
    return f'<div class="company-card" data-row-id="{row_id}" title="{title}"></div>'


class TestCrawlRowIds:
    def test_single_page(self):
        html = card("1", "Acme A.Ş.") + card("2", "Beta Ltd.")
        fetcher = FakeFetcher({ari.LIST_URL: html})
        rows = ari.crawl_row_ids(fetcher)
        assert [(r.title, r.row_id) for r in rows] == [("Acme A.Ş.", "1"), ("Beta Ltd.", "2")]

    def test_stops_when_next_page_has_no_new_cards(self):
        page1 = card("1", "Acme")
        pages = {ari.LIST_URL: page1, f"{ari.LIST_URL}?page=2": "<html></html>"}
        fetcher = FakeFetcher(pages)
        rows = ari.crawl_row_ids(fetcher)
        assert len(rows) == 1

    def test_paginates_across_multiple_pages(self):
        pages = {
            ari.LIST_URL: card("1", "Acme"),
            f"{ari.LIST_URL}?page=2": card("2", "Beta"),
            f"{ari.LIST_URL}?page=3": "<html></html>",
        }
        fetcher = FakeFetcher(pages)
        rows = ari.crawl_row_ids(fetcher)
        assert [r.row_id for r in rows] == ["1", "2"]

    def test_cards_without_row_id_or_title_are_skipped(self):
        html = '<div class="company-card"></div>' + card("1", "Acme")
        fetcher = FakeFetcher({ari.LIST_URL: html})
        rows = ari.crawl_row_ids(fetcher)
        assert len(rows) == 1

    def test_duplicate_row_ids_are_deduplicated(self):
        html = card("1", "Acme") + card("1", "Acme")
        fetcher = FakeFetcher({ari.LIST_URL: html})
        assert len(ari.crawl_row_ids(fetcher)) == 1

    def test_unreachable_first_page_returns_empty(self):
        assert ari.crawl_row_ids(FakeFetcher({})) == []


class TestFetchCompanyInfo:
    def test_returns_company_dict(self):
        url = f"{ari.INFO_URL}?rowID=48975"
        fetcher = FakeFetcher(json_by_url={url: {"company": {"title": "Acme", "website": "http://a.com"}}})
        info = ari.fetch_company_info(fetcher, "48975")
        assert info == {"title": "Acme", "website": "http://a.com"}

    def test_missing_response_returns_none(self):
        assert ari.fetch_company_info(FakeFetcher(), "999") is None

    def test_malformed_response_returns_none(self):
        url = f"{ari.INFO_URL}?rowID=1"
        fetcher = FakeFetcher(json_by_url={url: {"beklenmeyen": True}})
        assert ari.fetch_company_info(fetcher, "1") is None


class TestResolveWebsite:
    def test_prefers_website_url_over_website(self):
        info = {"website": "acme.com", "website_url": "https://acme.com"}
        assert ari.resolve_website(info) == "https://acme.com"

    def test_falls_back_to_website_when_url_missing(self):
        assert ari.resolve_website({"website": "http://acme.com", "website_url": None}) == "http://acme.com"

    def test_both_missing_returns_none(self):
        assert ari.resolve_website({"website": None, "website_url": None}) is None
