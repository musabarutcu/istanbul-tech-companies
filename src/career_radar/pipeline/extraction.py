"""İsimli kişi (CEO/kurucu/İK yetkilisi) çıkarımı — LLM yargısı gerektiren adım.

Regex bir e-postanın rol tabanlı mı genel mi olduğunu ayırt edebilir (`contacts.py`)
ama sayfadaki hangi ismin hangi role ait olduğunu anlamak bağlam gerektirir. Bu
yüzden ayrı bir adım: model, sayfa metnini okuyup "bu kişi CEO, bu kişi İK" gibi
bir yargıya varıyor.

İki backend:
  - api:    ANTHROPIC_API_KEY ile Haiku 4.5 üzerinden gerçek zamanlı çalışır.
            Düşük confidence'lı sayfalar Opus 5 ile tekrar denenir.
  - export: Aday sayfaların metnini JSONL'e yazar; API anahtarı olmadan veya
            maliyeti ertelemek için kullanılır — bir Claude Code oturumunda
            işlenip `contacts-import` ile geri okunabilir.

ANTİ-HALÜSİNASYON ÖNLEMİ (prompttan bağımsız, mekanik bir kontrol):
Model her kayıt için sayfadan BİREBİR bir alıntı (`evidence_quote`) vermek
zorunda. Bu alıntı kaynak sayfa metninde bulunamıyorsa kayıt otomatik elenir —
"uydurma yapma" talimatına güvenmek yerine, uydurulmuş her kayıt bu kontrolden
geçemeyeceği için yapısal olarak elenmiş olur.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from selectolax.parser import HTMLParser

from ..textutil import fold

HAIKU_MODEL = "claude-haiku-4-5"
OPUS_MODEL = "claude-opus-5"
CONFIDENCE_ESCALATION_THRESHOLD = 0.7
# 6000'de gerçek bir kayıp yaşandı: Hepsiburada'nın kurumsal hub sayfasında
# yönetim kurulu biyografileri, önce gelen uzun bir şirket tarihçesinin ardından
# duruyordu ve bu sınırla hiç modele ulaşmıyordu. 12000 karakter (~3000 token/
# sayfa), 8 sayfa üst sınırıyla bile Haiku için ucuz kalıyor.
MAX_PAGE_CHARS = 12000
MIN_EVIDENCE_QUOTE_CHARS = 8

EXTRACTION_INSTRUCTIONS = """\
Sana bir şirketin web sitesinden (iletişim/hakkımızda/ekip/kariyer) alınmış sayfa \
metinleri verilecek. Görevin: sayfada BİREBİR adı geçen, CEO, kurucu, genel müdür \
veya İnsan Kaynakları yetkilisi gibi bir role sahip KİŞİLERİ çıkarmak.

KURALLAR (çok önemli, hiçbirini esnetme):
1. Yalnızca sayfada gerçekten yazan isimleri çıkar. Sayfada adı geçmeyen hiçbir \
kişiyi UYDURMA.
2. Bir kişinin rolünden emin değilsen veya sayfa yeterince açık değilse, o kişiyi \
listeye EKLEME — boş liste döndürmek, yanlış bir kayıt eklemekten iyidir.
3. Her kayıt için, o bilgiyi doğrulayan sayfadaki cümleyi/ifadeyi BİREBİR AYNEN \
`evidence_quote` alanına kopyala. Bu alan kelime kelime kaynak metinde \
bulunabilmeli; parafraz etme, özetleme.
4. E-posta adresi sayfada birebir yazmıyorsa `email` alanını boş bırak. ASLA \
isimden e-posta türetme veya tahmin etme (ör. "ahmet.yilmaz@sirket.com" gibi \
uydurma).
5. `confidence` alanına 0 ile 1 arasında, bu çıkarımdan ne kadar emin olduğunu yaz.

Şimdi aşağıdaki sayfaları incele ve uygun kişileri çıkar:

"""


class PersonRecord(BaseModel):
    name: str
    role: str = Field(description="Sayfada yazan unvan, birebir (ör. 'Kurucu Ortak')")
    email: str | None = None
    contact_type: Literal["executive", "hr"]
    evidence_quote: str = Field(description="Bu bilgiyi doğrulayan, sayfadan birebir alıntı")
    confidence: float = Field(ge=0, le=1)


class PageExtraction(BaseModel):
    people: list[PersonRecord] = Field(default_factory=list)


@dataclass
class PageText:
    url: str
    text: str


def html_to_page_text(url: str, html: str, max_chars: int = MAX_PAGE_CHARS) -> PageText:
    """Ham HTML'i modele gönderilecek görünür metne çevirir.

    selectolax'ın `.text()` metodu `<script type="application/json">` gibi veri
    bloklarını GÜVENİLİR biçimde dışarıda BIRAKMIYOR — gerçek bir sitede (Getir)
    bu, Next.js'in ~90KB'lık `__NEXT_DATA__` durum nesnesinin düz metne karışıp
    prompt'u ve kanıt karşılaştırmasını gürültüyle doldurmasına yol açtı. Script/
    style/noscript düğümleri bu yüzden `.text()` çağrılmadan ÖNCE ağaçtan
    kaldırılıyor — parser'ın kendi hariç tutma davranışına güvenilmiyor.

    Burada üretilen metin hem prompt'a giriyor hem de `verify_evidence`'ın
    karşılaştırdığı kaynak — ikisi aynı temsili kullanmazsa kanıt kontrolü
    modelin gördüğü metinle değil, başka bir metinle karşılaştırma yapar.
    """
    try:
        tree = HTMLParser(html)
        for node in tree.css("script, style, noscript"):
            node.decompose()
        text = tree.text(separator=" ")
    except Exception:  # selectolax nadiren bozuk HTML'de patlar
        text = ""
    return PageText(url=url, text=" ".join(text.split())[:max_chars])


def find_source_page(record: PersonRecord, pages: list[PageText]) -> str | None:
    """Kanıt alıntısının gerçekten bulunduğu sayfanın URL'sini döner.

    Mekanik halüsinasyon kontrolü tam olarak burada yaşıyor: alıntı hiçbir
    sayfada birebir geçmiyorsa None döner — bu, prompttaki "uydurma" talimatından
    bağımsız çalışır, LLM kurallara uymasa bile kayıt bu kontrolü geçemez.
    """
    quote = fold(record.evidence_quote.strip())
    if len(quote) < MIN_EVIDENCE_QUOTE_CHARS:
        return None
    for page in pages:
        if quote in fold(page.text):
            return page.url
    return None


def verify_evidence(record: PersonRecord, pages: list[PageText]) -> bool:
    """Alıntı gerçekten kaynak sayfalardan birinde var mı?"""
    return find_source_page(record, pages) is not None


def build_prompt(pages: list[PageText]) -> str:
    body = "\n\n".join(f"=== {p.url} ===\n{p.text}" for p in pages if p.text.strip())
    return EXTRACTION_INSTRUCTIONS + body if body else ""


def to_contact_records(people: list[PersonRecord], pages: list[PageText]) -> list[dict]:
    """`repo.save_contacts`'ın beklediği kayıt biçimine çevirir.

    `source_url`, kanıt alıntısının gerçekten bulunduğu sayfa — genel bir "şirket
    sitesi" referansı değil, panoda "nereden geldi" dendiğinde açılacak tam sayfa.
    Bulunamayan (kanıtı doğrulanamayan) kayıtlar burada da atlanır; çağıranın
    `verify_evidence` ile önceden filtrelemiş olması beklenir ama bu son bir
    güvenlik ağı — kaynağı gösterilemeyen kayıt asla yazılmaz.
    E-postası olmayan isimli kayıtlar tutulur (email=None) — CEO'nun adını bilmek,
    e-postası olmasa bile kullanıcıya "kime yazacağım" sorusunda yardımcı olur.
    """
    records = []
    for person in people:
        source_url = find_source_page(person, pages)
        if source_url is None:
            continue
        records.append({
            "name": person.name,
            "role": person.role,
            "email": person.email,
            "contact_type": person.contact_type,
            "source_url": source_url,
            "extraction_method": "llm",
            "confidence": person.confidence,
        })
    return records


# --------------------------------------------------------------------- api backend


def extract_people_api(pages: list[PageText], client=None) -> list[PersonRecord]:
    """ANTHROPIC_API_KEY ile canlı çıkarım yapar. Düşük confidence'ta Opus'a eskalasyon.

    `client` parametresi test için enjekte edilebilir (gerçek anthropic.Anthropic
    örneği yerine sahte bir istemci verilebilir).
    """
    prompt = build_prompt(pages)
    if not prompt:
        return []

    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    def run(model: str) -> PageExtraction:
        response = client.messages.parse(
            model=model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
            output_format=PageExtraction,
        )
        return response.parsed_output

    result = run(HAIKU_MODEL)
    verified = [r for r in result.people if verify_evidence(r, pages)]

    if any(r.confidence < CONFIDENCE_ESCALATION_THRESHOLD for r in verified) or (
        len(verified) < len(result.people)
    ):
        # Ya düşük güvenli bir kayıt var ya da bazı kayıtlar kanıt kontrolünü
        # geçemedi (muhtemel halüsinasyon) — daha güçlü modelle tekrar dene.
        escalated = run(OPUS_MODEL)
        verified = [r for r in escalated.people if verify_evidence(r, pages)]

    return verified


# ------------------------------------------------------------------ export backend


def export_batch(companies: list[tuple[int, str, list[PageText]]], out_path: str | Path) -> int:
    """Şirket başına sayfaları JSONL'e yazar (`--backend export`).

    Her satır: {"company_id", "name", "pages": [{"url","text"}, ...], "prompt"}.
    Sayfa METNİ de yazılır (yalnızca URL değil) — sonuç dosyası geri okunduğunda
    `verify_evidence` aynı orijinal metinle tekrar çalıştırılabilsin diye. Bunu
    atlamak, import aşamasında anti-halüsinasyon kontrolünü imkansız kılardı.
    Boş içerikli şirketler (hiç aday sayfası bulunamamış) yazılmaz.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out_path.open("w", encoding="utf-8") as f:
        for company_id, name, pages in companies:
            prompt = build_prompt(pages)
            if not prompt:
                continue
            f.write(json.dumps({
                "company_id": company_id,
                "name": name,
                "pages": [{"url": p.url, "text": p.text} for p in pages if p.text.strip()],
                "prompt": prompt,
            }, ensure_ascii=False) + "\n")
            written += 1
    return written


def import_results(in_path: str | Path) -> dict[int, list[dict]]:
    """Export edilmiş dosyaya "people" alanı eklenmiş sonuç dosyasını okur ve doğrular.

    Beklenen format, `export_batch` satırlarına eklenmiş bir "people" alanı:
    {"company_id": 1, "pages": [{"url","text"}], "people": [{"name":..., ...}]}
    Bu dosya bir Claude Code oturumunda ya da `--backend api` ile üretilir.

    Dönen değer, doğrudan `repo.save_contacts`'a verilebilecek kayıt sözlükleri
    (`to_contact_records` ile üretilir) — kanıt kontrolü BURADA tekrar çalışır:
    dosyayı üreten süreç prompttaki kurallara uymamış olsa bile, kaynak metinde
    bulunamayan bir alıntı taşıyan kayıt elenir ve asla yazılmaz.
    """
    results: dict[int, list[dict]] = {}
    with Path(in_path).open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{in_path}:{line_no} geçersiz JSON: {exc}") from exc

            company_id = payload["company_id"]
            pages = [PageText(url=p["url"], text=p.get("text", "")) for p in payload.get("pages", [])]
            people = [PersonRecord(**p) for p in payload.get("people", [])]
            results[company_id] = to_contact_records(people, pages)
    return results
