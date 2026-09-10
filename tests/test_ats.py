"""ATS bağlayıcı testleri — ağ erişimi yok, hepsi sahte yanıtlarla."""

import json

import pytest

from career_radar.net import FetchResult
from career_radar.pipeline.connectors import ats


class FakeFetcher:
    """Fetcher yerine geçer: URL -> (status, body) eşlemesi verilir."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.requested: list[str] = []

    def get(self, url: str, use_cache: bool = True) -> FetchResult:
        self.requested.append(url)
        status, body = self.responses.get(url, (404, ""))
        return FetchResult(url=url, status=status, text=body)


class TestDetection:
    def test_greenhouse_embed_form(self):
        html = '<script src="https://boards.greenhouse.io/embed/job_board?for=acmeco"></script>'
        assert ats.detect_in_html(html) == ("greenhouse", "acmeco")

    def test_lever_link(self):
        assert ats.detect_in_html('<a href="https://jobs.lever.co/iyzico">') == ("lever", "iyzico")

    def test_recruitee_subdomain(self):
        assert ats.detect_in_html('href="https://acme.recruitee.com/o/x"') == ("recruitee", "acme")

    def test_generic_paths_are_not_mistaken_for_slugs(self):
        # "boards.greenhouse.io/embed" gibi jenerik yollar slug sayılmamalı.
        assert ats.detect_in_html('<a href="https://boards.greenhouse.io/embed">') is None

    def test_html_without_any_ats(self):
        assert ats.detect_in_html("<html><body>Kariyer sayfamız</body></html>") is None


class TestJobParsing:
    def test_greenhouse_payload(self):
        payload = {"jobs": [
            {"id": 1, "title": "Backend Engineer",
             "location": {"name": "İstanbul"},
             "absolute_url": "https://example.com/1", "updated_at": "2026-01-05"},
        ]}
        fetcher = FakeFetcher({
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs": (200, json.dumps(payload))
        })
        jobs = ats.list_jobs(fetcher, "greenhouse", "acme")
        assert len(jobs) == 1
        assert jobs[0].title == "Backend Engineer"
        assert jobs[0].location == "İstanbul"

    def test_lever_payload_converts_epoch(self):
        payload = [{"id": "abc", "text": "Stajyer", "hostedUrl": "https://x/1",
                    "createdAt": 1700000000000, "categories": {"location": "Remote"}}]
        fetcher = FakeFetcher({
            "https://api.lever.co/v0/postings/acme?mode=json": (200, json.dumps(payload))
        })
        jobs = ats.list_jobs(fetcher, "lever", "acme")
        assert jobs[0].posted_at.startswith("2023-11-")
        assert jobs[0].remote is True

    def test_titleless_entries_are_dropped(self):
        payload = {"jobs": [{"id": 1, "title": "  "}, {"id": 2, "title": "QA"}]}
        fetcher = FakeFetcher({
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs": (200, json.dumps(payload))
        })
        assert [j.title for j in ats.list_jobs(fetcher, "greenhouse", "acme")] == ["QA"]


class TestEmptyVersusUnreachable:
    """Bu ayrım tespit doğrulamasının temeli — karışırsa yanlış slug'lar geçerli sayılır."""

    def test_empty_board_returns_empty_list(self):
        fetcher = FakeFetcher({
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs": (200, '{"jobs": []}')
        })
        assert ats.list_jobs(fetcher, "greenhouse", "acme") == []

    def test_missing_board_returns_none(self):
        fetcher = FakeFetcher({})  # 404
        assert ats.list_jobs(fetcher, "greenhouse", "yanlis-slug") is None

    def test_malformed_json_returns_none(self):
        fetcher = FakeFetcher({
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs": (200, "<html>hata</html>")
        })
        assert ats.list_jobs(fetcher, "greenhouse", "acme") is None

    def test_unexpected_shape_returns_none(self):
        fetcher = FakeFetcher({
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs": (200, '{"beklenmeyen": 1}')
        })
        assert ats.list_jobs(fetcher, "greenhouse", "acme") is None


@pytest.mark.parametrize("key", list(ats.REGISTRY))
def test_every_provider_builds_a_https_url(key):
    assert ats.REGISTRY[key].jobs_url("acme").startswith("https://")
