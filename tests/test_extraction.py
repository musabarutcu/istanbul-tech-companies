"""İsimli kişi çıkarımı testleri — ağsız, sahte API istemcisiyle."""

import json

import pytest

from career_radar.pipeline import extraction as ex


def person(**overrides):
    defaults = dict(name="Ahmet Yılmaz", role="Kurucu Ortak", email=None,
                    contact_type="executive", evidence_quote="Kurucu Ortağımız Ahmet Yılmaz",
                    confidence=0.9)
    defaults.update(overrides)
    return ex.PersonRecord(**defaults)


class TestHtmlToPageText:
    def test_strips_tags_and_scripts(self):
        html = "<html><script>var x=1;</script><body><h1>Ekip</h1><p>Ahmet Yılmaz</p></body></html>"
        page = ex.html_to_page_text("https://acme.com/ekip", html)
        assert "var x" not in page.text
        assert "Ahmet Yılmaz" in page.text

    def test_strips_embedded_json_data_island(self):
        """Gerçek veride görülen hata: Next.js'in `__NEXT_DATA__` script'i
        (application/json, body içinde asıl içerikle birlikte) selectolax'ın
        varsayılan .text()'inde dışarıda kalmıyordu — ~90KB gürültü prompt'a
        ve kanıt karşılaştırmasına karışıyordu."""
        html = (
            '<html><body>'
            '<script id="__NEXT_DATA__" type="application/json">{"props":{"a":1}}</script>'
            '<p>Kurucu Ortağımız Ahmet Yılmaz</p>'
            '</body></html>'
        )
        page = ex.html_to_page_text("https://getir.com/hakkimizda", html)
        assert '"props"' not in page.text
        assert "Ahmet Yılmaz" in page.text

    def test_strips_style_tags(self):
        html = "<html><body><style>.x{color:red}</style><p>metin</p></body></html>"
        page = ex.html_to_page_text("https://acme.com/x", html)
        assert "color" not in page.text

    def test_truncates_to_max_chars(self):
        html = "<p>" + ("a" * 100) + "</p>"
        page = ex.html_to_page_text("https://acme.com/x", html, max_chars=10)
        assert len(page.text) == 10

    def test_malformed_html_does_not_crash(self):
        page = ex.html_to_page_text("https://acme.com/x", "<<<not html###")
        assert isinstance(page.text, str)


class TestVerifyEvidence:
    def test_quote_found_verbatim_in_page(self):
        pages = [ex.PageText(url="https://acme.com/ekip",
                             text="Şirketimizin Kurucu Ortağımız Ahmet Yılmaz 2015'te...")]
        assert ex.verify_evidence(person(), pages)

    def test_quote_not_found_is_rejected(self):
        pages = [ex.PageText(url="https://acme.com/ekip", text="Bambaşka bir metin.")]
        assert not ex.verify_evidence(person(), pages)

    def test_turkish_folding_applied_to_match(self):
        # Sayfa "İ"yi büyük harfle yazmış olabilir; fold() olmadan eşleşme kaçar.
        pages = [ex.PageText(url="https://acme.com/x", text="KURUCU ORTAĞIMIZ AHMET YILMAZ")]
        assert ex.verify_evidence(person(), pages)

    def test_too_short_quote_is_rejected_even_if_technically_present(self):
        pages = [ex.PageText(url="https://acme.com/x", text="ve ve ve ve")]
        assert not ex.verify_evidence(person(evidence_quote="ve"), pages)

    def test_empty_pages_never_verify(self):
        assert not ex.verify_evidence(person(), [])


class TestFindSourcePage:
    def test_returns_url_of_matching_page(self):
        pages = [
            ex.PageText(url="https://acme.com/hakkimizda", text="alakasız içerik"),
            ex.PageText(url="https://acme.com/ekip", text="Kurucu Ortağımız Ahmet Yılmaz"),
        ]
        assert ex.find_source_page(person(), pages) == "https://acme.com/ekip"

    def test_returns_none_when_not_found(self):
        pages = [ex.PageText(url="https://acme.com/x", text="ilgisiz")]
        assert ex.find_source_page(person(), pages) is None


class TestBuildPrompt:
    def test_empty_pages_yield_empty_prompt(self):
        assert ex.build_prompt([]) == ""

    def test_blank_text_pages_are_skipped(self):
        pages = [ex.PageText(url="https://acme.com/x", text="   ")]
        assert ex.build_prompt(pages) == ""

    def test_includes_instructions_and_page_content(self):
        pages = [ex.PageText(url="https://acme.com/ekip", text="Ahmet Yılmaz - CEO")]
        prompt = ex.build_prompt(pages)
        assert "UYDURMA" in prompt
        assert "https://acme.com/ekip" in prompt
        assert "Ahmet Yılmaz - CEO" in prompt


class TestToContactRecords:
    def test_verified_person_gets_its_source_page_url(self):
        pages = [ex.PageText(url="https://acme.com/ekip", text="Kurucu Ortağımız Ahmet Yılmaz")]
        records = ex.to_contact_records([person()], pages)
        assert records[0]["source_url"] == "https://acme.com/ekip"
        assert records[0]["extraction_method"] == "llm"

    def test_unverifiable_person_is_dropped_not_written_without_source(self):
        """source_url'i olmayan kayıt asla döndürülmemeli — repo bunu reddeder."""
        pages = [ex.PageText(url="https://acme.com/x", text="alakasız")]
        assert ex.to_contact_records([person()], pages) == []

    def test_person_without_email_is_kept(self):
        pages = [ex.PageText(url="https://acme.com/ekip", text="Kurucu Ortağımız Ahmet Yılmaz")]
        records = ex.to_contact_records([person(email=None)], pages)
        assert records[0]["email"] is None
        assert records[0]["name"] == "Ahmet Yılmaz"


class FakeParseResponse:
    def __init__(self, people):
        self.parsed_output = ex.PageExtraction(people=people)


class FakeClient:
    """messages.parse çağrılarını sırayla döndüren sahte istemci."""

    def __init__(self, responses_by_model: dict[str, list]):
        self.responses_by_model = responses_by_model
        self.calls: list[str] = []

        class _Messages:
            def parse(inner_self, model, **kwargs):
                self.calls.append(model)
                queue = self.responses_by_model[model]
                return FakeParseResponse(queue.pop(0))

        self.messages = _Messages()


class TestExtractPeopleApi:
    def test_high_confidence_result_does_not_escalate(self):
        pages = [ex.PageText(url="https://acme.com/ekip", text="Kurucu Ortağımız Ahmet Yılmaz")]
        client = FakeClient({ex.HAIKU_MODEL: [[person(confidence=0.95)]]})
        result = ex.extract_people_api(pages, client=client)
        assert len(result) == 1
        assert client.calls == [ex.HAIKU_MODEL]  # Opus'a hiç gidilmedi

    def test_low_confidence_escalates_to_opus(self):
        pages = [ex.PageText(url="https://acme.com/ekip", text="Kurucu Ortağımız Ahmet Yılmaz")]
        client = FakeClient({
            ex.HAIKU_MODEL: [[person(confidence=0.4)]],
            ex.OPUS_MODEL: [[person(confidence=0.9)]],
        })
        result = ex.extract_people_api(pages, client=client)
        assert client.calls == [ex.HAIKU_MODEL, ex.OPUS_MODEL]
        assert result[0].confidence == 0.9

    def test_unverifiable_record_triggers_escalation(self):
        """Kanıtı sayfada bulunamayan bir kayıt — confidence yüksek olsa bile
        Opus'a eskalasyon tetiklenmeli, çünkü olası bir halüsinasyon işareti."""
        pages = [ex.PageText(url="https://acme.com/ekip", text="alakasız içerik")]
        client = FakeClient({
            ex.HAIKU_MODEL: [[person(confidence=0.95)]],  # kanıtı sayfada yok
            ex.OPUS_MODEL: [[]],
        })
        result = ex.extract_people_api(pages, client=client)
        assert client.calls == [ex.HAIKU_MODEL, ex.OPUS_MODEL]
        assert result == []

    def test_empty_pages_never_calls_the_api(self):
        client = FakeClient({})
        assert ex.extract_people_api([], client=client) == []
        assert client.calls == []


class TestExportImportRoundTrip:
    def test_export_then_import_preserves_verified_people(self, tmp_path):
        pages = [ex.PageText(url="https://acme.com/ekip", text="Kurucu Ortağımız Ahmet Yılmaz")]
        out = tmp_path / "batch.jsonl"
        written = ex.export_batch([(1, "ACME", pages)], out)
        assert written == 1

        # Dışa aktarılan satırı oku, "people" ekleyip sonuç dosyası gibi geri yaz
        # (gerçek akışta bunu bir Claude Code oturumu veya --backend api yapar).
        line = json.loads(out.read_text(encoding="utf-8").strip())
        line["people"] = [person().model_dump()]
        result_path = tmp_path / "results.jsonl"
        result_path.write_text(json.dumps(line, ensure_ascii=False), encoding="utf-8")

        results = ex.import_results(result_path)
        assert len(results[1]) == 1
        assert results[1][0]["name"] == "Ahmet Yılmaz"
        assert results[1][0]["source_url"] == "https://acme.com/ekip"

    def test_import_rejects_hallucinated_evidence_even_if_present_in_result_file(self, tmp_path):
        """Sonuç dosyasını üreten süreç kurallara uymamış olabilir — import yine
        de kanıtı kontrol eder ve uydurulmuş kaydı eler."""
        pages = [ex.PageText(url="https://acme.com/ekip", text="alakasız içerik")]
        out = tmp_path / "batch.jsonl"
        ex.export_batch([(1, "ACME", pages)], out)

        line = json.loads(out.read_text(encoding="utf-8").strip())
        line["people"] = [person().model_dump()]  # kanıtı bu sayfada yok
        result_path = tmp_path / "results.jsonl"
        result_path.write_text(json.dumps(line, ensure_ascii=False), encoding="utf-8")

        results = ex.import_results(result_path)
        assert results[1] == []

    def test_export_skips_companies_with_no_page_content(self, tmp_path):
        out = tmp_path / "batch.jsonl"
        written = ex.export_batch([(1, "Boş Şirket", [])], out)
        assert written == 0
        assert out.read_text(encoding="utf-8") == ""

    def test_import_raises_on_malformed_json_line(self, tmp_path):
        bad = tmp_path / "bad.jsonl"
        bad.write_text("{bu gecerli json degil", encoding="utf-8")
        with pytest.raises(ValueError):
            ex.import_results(bad)
