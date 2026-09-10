"""Şirket sınıflandırma: domain (çok etiketli), büyüklük, sektör, teknoloji yığını.

"Bu şirket yapay zeka mı, web şirketi mi, quantum mu?" sorusu bağlam ve yargı
gerektirir — regex bunu yapamaz. Girdi olarak şirketin ana sayfası (+ varsa
dizinden gelen kısa/temiz bir sektör etiketi) verilir; model `taxonomy.yaml`'daki
KAPALI kümeden seçim yapmak zorundadır, yeni kategori icat edemez — çıktı ayrıca
bu kümeye karşı doğrulanır (model talimata uymasa bile geçersiz etiket süzülür).

İki backend, `extraction.py` ile aynı desen:
  - api:    ANTHROPIC_API_KEY ile Haiku 4.5 üzerinden gerçek zamanlı çalışır.
  - export: Aday metni JSONL'e döker; API anahtarı olmadan işlenebilir.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

HAIKU_MODEL = "claude-haiku-4-5"
OPUS_MODEL = "claude-opus-5"
CONFIDENCE_ESCALATION_THRESHOLD = 0.6
MAX_INPUT_CHARS = 6000

# Bir dizin kaynağının verdiği "sektör" metni bazen tek bir şirkete değil,
# kaynağın TÜM kategori listesine ait oluyor (gerçek veride görüldü — TÜBİSAD
# üye sayfasında bazı satırlar 40+ kategoriyi art arda taşıyordu). Böyle bir
# metni "bu şirketin sektörü" diye modele vermek yanıltıcı; bu uzunluğun
# üstündeki etiketler ipucu olarak kullanılmaz.
MAX_HINT_LABEL_CHARS = 80


def load_taxonomy(path: str | Path = "taxonomy.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _build_instructions(taxonomy: dict) -> str:
    domain_list = ", ".join(taxonomy["domain"])
    size_list = ", ".join(taxonomy["size_bucket"])
    sector_list = ", ".join(taxonomy["sector"])
    return f"""\
Sana bir şirketin web sitesinden alınmış ana sayfa metni (ve varsa şirketin \
listelendiği dizinden gelen kısa bir sektör ipucu) verilecek. Görevin bu \
şirketi sınıflandırmak.

KURALLAR:
1. `domain` alanı İÇİN YALNIZCA şu listeden seç (birden fazla seçebilirsin, \
şirket birden fazla alanda çalışabilir): {domain_list}
   Listede olmayan bir kategori UYDURMA. Sayfa hangi alana girdiğini net \
göstermiyorsa `domain` alanını boş bırak.
2. `size_bucket` İÇİN YALNIZCA şu listeden seç: {size_list}
   Sayfada çalışan sayısına dair açık bir ipucu (ör. "50 kişilik ekip", \
"500+ çalışan") yoksa bu alanı BOŞ BIRAK — tahmin etme.
3. `sector` İÇİN YALNIZCA şu listeden seç: {sector_list}
4. `stack` alanına sayfada GERÇEKTEN adı geçen teknoloji/araç isimlerini yaz \
(ör. "Python", "Kubernetes", "React"). Sayfada hiç teknoloji ismi yoksa boş liste.
5. `reasoning` alanına bir-iki cümlelik kısa gerekçe yaz — hangi cümle/ifade \
seni bu sınıflandırmaya götürdü.
6. `confidence` alanına 0-1 arası bir güven skoru yaz. Sayfa çok az bilgi \
veriyorsa düşük confidence ver, boş bırakmak zorunda değilsin ama dürüst ol.

Şimdi aşağıdaki şirketi sınıflandır:

"""


class CompanyClassification(BaseModel):
    domain: list[str] = Field(default_factory=list)
    size_bucket: str | None = None
    sector: str | None = None
    stack: list[str] = Field(default_factory=list)
    reasoning: str = ""
    confidence: float = Field(ge=0, le=1, default=0.5)


@dataclass
class ClassificationInput:
    company_id: int
    name: str
    page_url: str
    page_text: str
    directory_hint: str | None = None


def build_prompt(item: ClassificationInput, taxonomy: dict) -> str:
    instructions = _build_instructions(taxonomy)
    hint = f"\nDizin kaynağından sektör ipucu: {item.directory_hint}\n" if item.directory_hint else ""
    return (
        f"{instructions}Şirket adı: {item.name}\n"
        f"Kaynak sayfa: {item.page_url}{hint}\n"
        f"Sayfa içeriği:\n{item.page_text[:MAX_INPUT_CHARS]}"
    )


def clean_hint(raw_hint: str | None) -> str | None:
    """Aşırı uzun/çok kategorili dizin etiketlerini ipucu olarak reddeder.

    Gerçek veride görülen hata: bazı dizin kaynakları tek bir şirkete tüm
    kategori listesini (40+ sektör) etiket olarak yazmış — bunu "bu şirketin
    sektörü" diye modele vermek yanıltıcı olurdu.
    """
    if not raw_hint or len(raw_hint) > MAX_HINT_LABEL_CHARS:
        return None
    return raw_hint


def validate_classification(result: CompanyClassification, taxonomy: dict) -> CompanyClassification:
    """Modelin çıktısını kapalı kümeye karşı doğrular — talimata uymasa bile
    geçersiz bir etiket veritabanına asla yazılmaz."""
    valid_domain = set(taxonomy["domain"])
    valid_size = set(taxonomy["size_bucket"])
    valid_sector = set(taxonomy["sector"])
    return CompanyClassification(
        domain=[d for d in result.domain if d in valid_domain],
        size_bucket=result.size_bucket if result.size_bucket in valid_size else None,
        sector=result.sector if result.sector in valid_sector else None,
        stack=result.stack,
        reasoning=result.reasoning,
        confidence=result.confidence,
    )


def classify_api(item: ClassificationInput, taxonomy: dict, client=None) -> CompanyClassification:
    """ANTHROPIC_API_KEY ile canlı sınıflandırma. Düşük confidence'ta Opus'a eskalasyon."""
    prompt = build_prompt(item, taxonomy)

    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    def run(model: str) -> CompanyClassification:
        response = client.messages.parse(
            model=model, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            output_format=CompanyClassification,
        )
        return validate_classification(response.parsed_output, taxonomy)

    result = run(HAIKU_MODEL)
    if result.confidence < CONFIDENCE_ESCALATION_THRESHOLD:
        result = run(OPUS_MODEL)
    return result


def export_batch(items: list[ClassificationInput], taxonomy: dict, out_path: str | Path) -> int:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out_path.open("w", encoding="utf-8") as f:
        for item in items:
            if not item.page_text.strip():
                continue
            f.write(json.dumps({
                "company_id": item.company_id,
                "name": item.name,
                "page_url": item.page_url,
                "prompt": build_prompt(item, taxonomy),
            }, ensure_ascii=False) + "\n")
            written += 1
    return written


def import_results(in_path: str | Path, taxonomy: dict) -> dict[int, tuple[CompanyClassification, str]]:
    """Sonuç JSONL'ini okur, her satırı taksonomiye karşı doğrular.

    Dönen: company_id -> (doğrulanmış CompanyClassification, page_url).
    """
    results: dict[int, tuple[CompanyClassification, str]] = {}
    with Path(in_path).open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{in_path}:{line_no} geçersiz JSON: {exc}") from exc
            classification = validate_classification(
                CompanyClassification(**payload["classification"]), taxonomy
            )
            results[payload["company_id"]] = (classification, payload.get("page_url", ""))
    return results
