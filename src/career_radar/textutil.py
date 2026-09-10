"""Türkçe'ye duyarlı metin normalleştirme — dedup'ın doğruluğu buna bağlı.

Python'ın `.lower()` metodu Türkçe'de yanlış çalışır: "İ".lower() birleşik noktalı bir
karakter üretir, "I".lower() ise "i" verir (Türkçe'de "ı" olmalı). Şirket isimlerini
eşleştirirken bu, "İSTANBUL" ile "istanbul"un farklı görünmesine yol açar.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

_TR_LOWER = str.maketrans({"I": "ı", "İ": "i", "Ş": "ş", "Ğ": "ğ", "Ü": "ü", "Ö": "ö", "Ç": "ç"})
_ASCII_FOLD = str.maketrans({"ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c", "â": "a", "î": "i", "û": "u"})

# Yalnızca hukuki şirket türü ekleri temizlenir. "teknoloji", "yazılım" gibi
# tanımlayıcı kelimeler KORUNUR — onları atmak farklı şirketleri birleştirir.
#
# Karşılaştırma noktasız ve ASCII'ye katlanmış halde yapılır: "A.Ş." -> "as".
# Listeyi Türkçe karakterle yazıp katlanmış metinle karşılaştırmak sessizce
# başarısız olur (bu hata testlerle yakalandı).
_LEGAL_TOKENS = {
    "as", "anonim", "sirketi", "ltd", "limited", "sti", "san", "sanayi",
    "tic", "ticaret", "ve", "inc", "llc", "gmbh", "bv", "co", "corp",
    "holding", "grup", "group",
}


def tr_lower(text: str) -> str:
    """Türkçe kurallarına uyan küçük harfe çevirme."""
    return text.translate(_TR_LOWER).lower()


def fold(text: str) -> str:
    """Türkçe karakterleri ASCII karşılıklarına indirger (arama/eşleştirme için)."""
    return tr_lower(text).translate(_ASCII_FOLD)


def normalize_company_name(name: str) -> str:
    """Dedup için kanonik isim. 'ACME Bilişim A.Ş.' -> 'acme bilisim'."""
    text = re.sub(r"[^\w\s.]", " ", fold(name))
    tokens = [t.replace(".", "") for t in text.split()]
    kept = [t for t in tokens if t and t not in _LEGAL_TOKENS]
    # Ad tamamen hukuki eklerden ibaretse (ör. "Grup A.Ş.") hiçbir şey bırakmamak
    # yerine ham hali korunur; boş kanonik isim her şeyi birbirine bağlardı.
    if not kept:
        kept = [t for t in tokens if t]
    return " ".join(kept).strip()


def domain_from_url(url: str | None) -> str | None:
    """URL'den kanonik alan adı. Alt alan adları korunur, 'www.' atılır."""
    if not url:
        return None
    candidate = url.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        host = urlparse(candidate).netloc.lower()
    except ValueError:
        return None
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or host.endswith("."):
        return None
    return host or None


def registrable_domain(domain: str | None) -> str | None:
    """Kaba eTLD+1. 'kariyer.acme.com.tr' -> 'acme.com.tr'.

    Public Suffix List kullanmıyoruz (ek bağımlılık); yaygın çok parçalı TR uzantıları
    elle tanınıyor. Amaç mükemmel doğruluk değil, aynı şirketin iki alt alan adını
    birleştirebilmek.
    """
    if not domain:
        return None
    parts = domain.split(".")
    if len(parts) <= 2:
        return domain
    two_level = {"com.tr", "net.tr", "org.tr", "edu.tr", "gov.tr", "bel.tr", "web.tr",
                 "co.uk", "com.au"}
    if ".".join(parts[-2:]) in two_level and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Türkiye'de spontane staj başvurusunun doğru muhatabı olan rol tabanlı kutular.
HR_LOCAL_PARTS = {
    "ik", "insankaynaklari", "insan.kaynaklari", "insan_kaynaklari", "ikbasvuru",
    "kariyer", "kariyer.basvuru", "staj", "stajyer", "hr", "jobs", "job", "career",
    "careers", "recruitment", "recruiting", "cv", "basvuru", "isealim",
}
GENERIC_LOCAL_PARTS = {
    "info", "iletisim", "bilgi", "contact", "hello", "merhaba", "destek", "support",
    "admin", "office", "mail", "sales", "satis", "pazarlama", "marketing",
}


_JS_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def find_emails(text: str) -> list[str]:
    """Metindeki e-posta adreslerini bulur. Sıra korunur, tekrar atılır.

    Aramadan önce `\\u003e` gibi JS/JSON unicode kaçışları çözülür — bunlar
    genelde gömülü JSON-LD içinde gerçek bir adrese bitişik duruyor
    (`...\\u003elegal@acme.com\\u003c...`), çözülmezse kaçış dizisi adresin
    başına yapışıp `u003elegal@acme.com` gibi bozuk bir sonuç üretiyor.
    """
    text = _JS_UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text or "")
    seen: dict[str, None] = {}
    for match in _EMAIL_RE.findall(text):
        email = match.lower().strip(".")
        if email.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            continue  # dosya adları e-posta gibi görünebiliyor
        seen.setdefault(email, None)
    return list(seen)


def classify_email(email: str) -> str:
    """E-postayı 'hr' | 'generic' | 'personal' olarak sınıflar."""
    local = email.split("@", 1)[0].lower()
    if local in HR_LOCAL_PARTS:
        return "hr"
    if any(token in local for token in ("kariyer", "insankaynak", "staj", "recruit")):
        return "hr"
    if local in GENERIC_LOCAL_PARTS:
        return "generic"
    return "personal"


_INTERNSHIP_RE = re.compile(
    r"\b(staj(yer|\b)|bursiyer|intern\b|internship|co-?op\b|working\s+student|"
    r"öğrenci\s+program|ogrenci\s+program)",
    re.I,
)


def looks_like_internship(title: str) -> bool:
    return bool(_INTERNSHIP_RE.search(fold(title or "") + " " + (title or "")))
