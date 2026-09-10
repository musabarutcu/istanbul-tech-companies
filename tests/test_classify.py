"""Şirket sınıflandırma testleri — ağsız, sahte API istemcisiyle."""

import json

import pytest

from career_radar.pipeline import classify as cl

TAXONOMY = {
    "domain": ["ai_ml", "web_saas", "quantum_derin_tek"],
    "size_bucket": ["kucuk", "orta", "buyuk"],
    "sector": ["urun", "danismanlik"],
}


def item(**overrides):
    defaults = dict(company_id=1, name="ACME AI", page_url="https://acme.com",
                    page_text="ACME, yapay zeka tabanlı ürünler geliştiren bir şirkettir.")
    defaults.update(overrides)
    return cl.ClassificationInput(**defaults)


class TestLoadTaxonomy:
    def test_loads_real_taxonomy_file(self):
        taxonomy = cl.load_taxonomy("taxonomy.yaml")
        assert "ai_ml" in taxonomy["domain"]
        assert "kucuk" in taxonomy["size_bucket"]


class TestCleanHint:
    def test_short_hint_is_kept(self):
        assert cl.clean_hint("Yazılım") == "Yazılım"

    def test_none_is_kept_as_none(self):
        assert cl.clean_hint(None) is None

    def test_overly_long_hint_is_rejected(self):
        """Gerçek veride görülen hata: bazı dizin kaynakları tek bir şirkete
        derneğin TÜM kategori listesini (40+ sektör) etiket yazmıştı."""
        garbage = ", ".join(f"Kategori{i}" for i in range(30))
        assert cl.clean_hint(garbage) is None


class TestBuildPrompt:
    def test_includes_name_and_page_text(self):
        prompt = cl.build_prompt(item(), TAXONOMY)
        assert "ACME AI" in prompt
        assert "yapay zeka" in prompt

    def test_includes_allowed_domain_list(self):
        prompt = cl.build_prompt(item(), TAXONOMY)
        assert "ai_ml" in prompt and "web_saas" in prompt and "quantum_derin_tek" in prompt

    def test_includes_directory_hint_when_present(self):
        prompt = cl.build_prompt(item(directory_hint="Yazılım"), TAXONOMY)
        assert "Yazılım" in prompt

    def test_truncates_long_page_text(self):
        long_text = "a" * (cl.MAX_INPUT_CHARS + 500)
        prompt = cl.build_prompt(item(page_text=long_text), TAXONOMY)
        assert len(prompt) < len(long_text) + 2000  # tam metin geçmemiş olmalı


class TestValidateClassification:
    def test_keeps_valid_labels(self):
        result = cl.CompanyClassification(domain=["ai_ml"], size_bucket="kucuk", sector="urun")
        validated = cl.validate_classification(result, TAXONOMY)
        assert validated.domain == ["ai_ml"]
        assert validated.size_bucket == "kucuk"
        assert validated.sector == "urun"

    def test_drops_invented_domain_not_in_taxonomy(self):
        """Model talimata uymayıp taksonomide olmayan bir kategori uydursa bile
        bu asla veritabanına yazılmamalı."""
        result = cl.CompanyClassification(domain=["ai_ml", "blockchain_web3"])
        validated = cl.validate_classification(result, TAXONOMY)
        assert validated.domain == ["ai_ml"]

    def test_drops_invalid_size_bucket(self):
        result = cl.CompanyClassification(size_bucket="devasa")  # taksonomide yok
        assert cl.validate_classification(result, TAXONOMY).size_bucket is None

    def test_stack_is_never_filtered(self):
        """stack serbest metin — taksonomiye karşı doğrulanmaz."""
        result = cl.CompanyClassification(stack=["Python", "Rust", "ObscureFramework"])
        validated = cl.validate_classification(result, TAXONOMY)
        assert validated.stack == ["Python", "Rust", "ObscureFramework"]


class FakeParseResponse:
    def __init__(self, classification: cl.CompanyClassification):
        self.parsed_output = classification


class FakeClient:
    def __init__(self, responses_by_model: dict[str, list]):
        self.responses_by_model = responses_by_model
        self.calls: list[str] = []

        class _Messages:
            def parse(inner_self, model, **kwargs):
                self.calls.append(model)
                return FakeParseResponse(self.responses_by_model[model].pop(0))

        self.messages = _Messages()


class TestClassifyApi:
    def test_high_confidence_does_not_escalate(self):
        client = FakeClient({cl.HAIKU_MODEL: [cl.CompanyClassification(domain=["ai_ml"], confidence=0.9)]})
        result = cl.classify_api(item(), TAXONOMY, client=client)
        assert client.calls == [cl.HAIKU_MODEL]
        assert result.domain == ["ai_ml"]

    def test_low_confidence_escalates_to_opus(self):
        client = FakeClient({
            cl.HAIKU_MODEL: [cl.CompanyClassification(domain=[], confidence=0.3)],
            cl.OPUS_MODEL: [cl.CompanyClassification(domain=["ai_ml"], confidence=0.85)],
        })
        result = cl.classify_api(item(), TAXONOMY, client=client)
        assert client.calls == [cl.HAIKU_MODEL, cl.OPUS_MODEL]
        assert result.domain == ["ai_ml"]

    def test_invented_label_from_api_is_still_filtered(self):
        client = FakeClient({cl.HAIKU_MODEL: [
            cl.CompanyClassification(domain=["ai_ml", "uydurma_kategori"], confidence=0.9)
        ]})
        result = cl.classify_api(item(), TAXONOMY, client=client)
        assert result.domain == ["ai_ml"]


class TestExportImportRoundTrip:
    def test_round_trip_preserves_valid_classification(self, tmp_path):
        out = tmp_path / "batch.jsonl"
        written = cl.export_batch([item()], TAXONOMY, out)
        assert written == 1

        line = json.loads(out.read_text(encoding="utf-8").strip())
        line["classification"] = {"domain": ["ai_ml"], "size_bucket": None,
                                  "sector": "urun", "stack": ["Python"],
                                  "reasoning": "test", "confidence": 0.9}
        result_path = tmp_path / "results.jsonl"
        result_path.write_text(json.dumps(line, ensure_ascii=False), encoding="utf-8")

        results = cl.import_results(result_path, TAXONOMY)
        classification, page_url = results[1]
        assert classification.domain == ["ai_ml"]
        assert page_url == "https://acme.com"

    def test_import_filters_invented_labels_even_from_result_file(self, tmp_path):
        out = tmp_path / "batch.jsonl"
        cl.export_batch([item()], TAXONOMY, out)
        line = json.loads(out.read_text(encoding="utf-8").strip())
        line["classification"] = {"domain": ["ai_ml", "olmayan_kategori"], "confidence": 0.9}
        result_path = tmp_path / "results.jsonl"
        result_path.write_text(json.dumps(line, ensure_ascii=False), encoding="utf-8")

        classification, _ = cl.import_results(result_path, TAXONOMY)[1]
        assert classification.domain == ["ai_ml"]

    def test_export_skips_companies_without_page_text(self, tmp_path):
        out = tmp_path / "batch.jsonl"
        written = cl.export_batch([item(page_text="")], TAXONOMY, out)
        assert written == 0
