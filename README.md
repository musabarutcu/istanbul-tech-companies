# Career Radar

İstanbul odaklı, Türkiye'deki teknoloji şirketlerini **keşfetmek** için yapılmış kişisel bir
araç. Bir bilgisayar mühendisliği öğrencisinin yaz stajı / iş arayışı için üretildi.

## Neden var

Klasik iş arama siteleri (LinkedIn, kariyer.net) yalnızca zaten tanınan, ilan yayınlamış
büyük şirketleri gösteriyor. Oysa Türkiye'de teknoloji üreten şirketlerin büyük kısmı —
teknokent kiracıları, VC portföy şirketleri, sanayi dernekleri üyeleri — bu aramalarda hiç
görünmüyor. Bu projenin amacı **"hangi şirket var, ne iş yapıyor, kime yazacağım"** sorusuna
cevap vermek; aktif ilan beklemeden doğrudan şirkete mail atmak isteyen bir kullanım şekli
için tasarlandı.

Veri **yalnızca şirketin kendi alan adından** toplanır (üçüncü taraf toplayıcı, LinkedIn
kazıma, e-posta tahmin servisi yok). Her kayıt kaynağıyla birlikte tutulur — panoda her
satırın yanında "nereden geldi" bilgisi görünür.

**Şu an itibarıyla proje bir "aktif şirket listesi / statik keşif snapshot'ı" olarak
kullanılıyor.** Sürekli güncellenen canlı bir tarama sistemi değil.

---

## Nasıl çalıştırılır

```bash
# Windows'ta çift tıkla:
baslat.bat

# ya da elle:
.venv\Scripts\python.exe -m career_radar.cli serve
```
Tarayıcı otomatik `http://127.0.0.1:8000` adresini açar. Açılan panelde şirketleri şehir,
kaynak, kategori (AI/Web/Fintech/...) ve takip durumuna göre filtreleyebilir, arama
yapabilir, bir şirkete tıklayıp iletişim bilgilerini görebilir, kendi takip notunu
(listemde / yazdım / cevap geldi / ilgilenmiyorum) girebilir ve listeyi CSV olarak
indirebilirsin.

---

## Mevcut durum

| Aşama | Durum | Sayı |
|---|---|---|
| Şirket keşfi | ✅ Tamamlandı | 1246 şirket, 18 kaynak |
| Domain çözümleme | ✅ Tamamlandı | 1159/1246 (%93) |
| Sınıflandırma (AI/Web/Savunma/... kategorileri) | ✅ Tamamlandı | 1159/1159 (domain'i olan hepsi) |
| İletişim taraması (e-posta/telefon) | Kısmi | 881/1246 (%71) tarandı → 494 şirkette en az 1 e-posta |
| Kariyer sayfası / aktif ilan tespiti | Neredeyse hiç yapılmadı | 38/1246 |
| Web arayüzü (filtre, tablo, detay, açık/koyu tema) | ✅ Çalışıyor | — |

Şu an için ek tarama (iletişim/ilan) yapılmıyor; proje mevcut haliyle bırakıldı. Aşağıdaki
"Gelecekte eklenebilecek özellikler" bölümü bunun nasıl genişletilebileceğini anlatıyor.

---

## Veri nasıl toplandı

Üç adımlı bir süreç:

1. **Keşif** — aşağıdaki 18 kaynaktan (teknokent dizinleri, VC portföy sayfaları, sanayi
   dernekleri üye listeleri, birkaç elle doğrulanmış tohum şirket) şirket adı ve web sitesi
   adresi kazındı.
2. **Temizleme** — şirket unvanlarındaki ekler (A.Ş., Ltd. Şti. vb.) normalize edildi, web
   sitesi adresleri (domain) çözümlendi; aynı şirket birden fazla kaynakta çıkabildiği için
   `rapidfuzz` ile isim benzerliği ve domain eşleşmesi kullanılarak tekilleştirildi (dedup).
3. **Sınıflandırma** — domain'i bulunan her şirketin ana sayfası, hakkımızda ve kariyer
   sayfası metni okunarak 14 kategoriden (AI/ML, fintech, savunma-havacılık, oyun, bulut,
   siber güvenlik...) uygun olanlarla etiketlendi. Bu adım ücretli bir API anahtarı
   gerektirmeden yapıldı: sayfa metinleri dışa aktarıldı, bir LLM aracılığıyla her şirketin
   gerçek sayfa içeriği okunarak elle etiketlendi, sonuç geri içe aktarıldı — yaklaşık 53
   grup halinde, grup başına 20-25 şirket.

İletişim bilgileri (e-posta, telefon) de aynı ilkeyle, **yalnızca şirketin kendi sitesindeki**
iletişim/hakkımızda/kariyer sayfalarından — üçüncü taraf toplayıcı veya e-posta tahmin
servisi kullanılmadan — çekildi; her kayıt hangi sayfadan geldiğiyle birlikte tutuluyor.

| Kaynak | Şirket sayısı | Tür |
|---|---:|---|
| İTÜ ARI Teknokent | 410 | Teknokent dizini |
| SaSaD üyeleri (savunma) | 258 | Dernek üye listesi |
| TÜBİSAD kurumsal üyeleri | 242 | Dernek üye listesi |
| TÜBİSAD üye listesi | 158 | Dernek üye listesi |
| Teknopol İstanbul | 104 | Teknokent dizini |
| Bilişim Vadisi (İstanbul kampüsü) | 78 | Teknokent dizini |
| Dijitalpark Teknokent | 40 | Teknokent dizini |
| Elle girilen tohum listesi | 38 | Manuel |
| Yıldız Teknopark | 36 | Teknokent dizini |
| ScaleX Ventures | 36 | VC portföyü |
| Revo Capital | 25 | VC portföyü |
| Bilkent CYBERPARK | 11 | Teknokent dizini |
| Maslak / Ataşehir (elle doğrulanmış) | 9 | Manuel |
| Endeavor Türkiye | 9 | VC portföyü |

*(Bir şirket birden fazla kaynakta geçebildiği için yukarıdaki sayıların toplamı, toplam 1246
tekil şirket sayısından fazladır.)*

---

## Gelecekte eklenebilecek özellikler

Şu an çalıştırılmayan ama altyapısı hazır olan işler:

- **İletişim taramasının tamamlanması** — kalan ~278 şirket hiç taranmadı:
  `career-radar contacts --limit 0`
- **KAP (Kamuyu Aydınlatma Platformu) entegrasyonu** — BIST'te işlem gören şirketler için
  resmî yönetim kurulu/yatırımcı ilişkileri iletişimini çekebilir; henüz yazılmadı.
- **Aktif ilan / kariyer sayfası tespiti** — 7 ATS sağlayıcısı (Greenhouse, Lever, Ashby,
  Workable, Recruitee, SmartRecruiters, Personio) için bağlayıcı kod zaten yazılı, sadece
  çalıştırılması gerekiyor: `career-radar detect --limit 0` ardından `career-radar jobs`.
- **Panelden tek tuşla güncelleme** — şu an tüm tarama CLI'dan elle tetikleniyor; arayüze
  bir "Listeyi güncelle" butonu + canlı ilerleme çubuğu eklenebilir.

---

## Teknoloji yığını

| Katman | Teknoloji |
|---|---|
| Dil | Python 3.11 |
| Web çatısı | FastAPI + uvicorn |
| CLI | Typer (`career-radar serve / classify / detect / contacts / jobs / stats`) |
| Veritabanı | SQLite (WAL modu) |
| HTTP / HTML ayrıştırma | httpx + selectolax |
| Deduplikasyon | rapidfuzz (isim benzerliği) |
| Veri doğrulama | Pydantic v2 |
| Frontend | Düz HTML + vanilla JavaScript + CSS — derleme adımı yok |
| Test | pytest + FastAPI `TestClient` (217 test) |

---

## Bilinen sınırlar

- İletişim kapsaması kısmi (%71 tarandı, %40'ında en az 1 e-posta bulundu) — sitesinde
  e-posta yayımlamayan/formla iletişim kuran şirketler için "kaynak bulunamadı" olarak kalır.
- Aktif ilan rozeti neredeyse hiç dolu değil — panoda "ilan yok" görmek normal, kullanım
  şekli zaten doğrudan yazmak üzerine kurulu.
- 87 şirketin hiç web sitesi bulunamadı; bunlar sınıflandırma dışı kaldı (kanıt yoksa iddia
  yok politikası).
