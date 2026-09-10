"""Nazik HTTP katmanı: disk önbelleği, origin başına hız sınırı, robots.txt kapısı, geri çekilme.

Buradaki her özellik bir zorunluluktan doğdu:
- Disk önbelleği: 1-3 saatlik koşuyu parser değiştiği için baştan yapmamak.
- Origin başına hız sınırı: aynı teknokent dizininin 200 sayfasını art arda çekerken
  siteyi dövmemek.
- robots.txt kapısı: uyarı değil, geçilemeyen kapı. İzin yoksa istek hiç atılmaz.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

DEFAULT_TIMEOUT = 20.0
MAX_RETRIES = 3
RETRY_STATUSES = {429, 500, 502, 503, 504}


def _ua() -> str:
    """Tarayıcı uyumlu ama kendini tanıtan User-Agent.

    Salt bot dizesi kullanınca birçok Türk sitesi (CDN kuralları yüzünden) 403 dönüyordu.
    Bu yüzden standart tarayıcı jetonları korunuyor, kimliğimiz sona ekleniyor: site
    sahibi loglarda kim olduğumuzu ve kime ulaşacağını görebiliyor. robots.txt kuralları
    yine de eksiksiz uygulanıyor — bu satır bir kılık değiştirme değil, uyumluluk.
    """
    contact = os.getenv("CRAWLER_CONTACT_EMAIL", "").strip()
    suffix = f"; +{contact}" if contact else ""
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 "
        f"CareerRadar/0.1 (kisisel staj arastirmasi{suffix})"
    )


@dataclass
class FetchResult:
    url: str
    status: int | None
    text: str
    from_cache: bool = False
    blocked_by_robots: bool = False
    insecure_tls: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300 and bool(self.text)


class RateLimiter:
    """Origin başına minimum bekleme. Thread'ler arası paylaşılır."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, origin: str) -> None:
        with self._lock:
            now = time.monotonic()
            last = self._last.get(origin)
            if last is not None:
                gap = self.min_interval - (now - last)
                if gap > 0:
                    time.sleep(gap)
                    now = time.monotonic()
            self._last[origin] = now


class Fetcher:
    def __init__(
        self,
        cache_dir: str | Path = "cache",
        min_interval: float | None = None,
        respect_robots: bool = True,
        cache_ttl_seconds: int = 30 * 24 * 3600,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if min_interval is None:
            min_interval = float(os.getenv("REQUEST_DELAY_SECONDS", "1.0"))
        self.limiter = RateLimiter(min_interval)
        self.respect_robots = respect_robots
        self.cache_ttl = cache_ttl_seconds
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()
        self._client = httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={
                "User-Agent": _ua(),
                "Accept-Language": "tr,en;q=0.8",
            },
        )
        self.stats = {"network": 0, "cache": 0, "blocked": 0, "errors": 0}
        self._insecure: httpx.Client | None = None
        self._timeout = timeout

    def _insecure_client(self) -> httpx.Client:
        """Yalnızca sertifika zinciri eksik sitelerde kullanılan yedek istemci."""
        if self._insecure is None:
            self._insecure = httpx.Client(
                follow_redirects=True,
                timeout=self._timeout,
                verify=False,
                headers=dict(self._client.headers),
            )
        return self._insecure

    # ---------------------------------------------------------------- cache

    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.json"

    def _read_cache(self, url: str) -> FetchResult | None:
        path = self._cache_path(url)
        if not path.exists():
            return None
        if self.cache_ttl > 0 and (time.time() - path.stat().st_mtime) > self.cache_ttl:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return FetchResult(
            url=payload["url"],
            status=payload["status"],
            text=payload["text"],
            from_cache=True,
        )

    def _write_cache(self, result: FetchResult) -> None:
        """Yanıtı önbelleğe yazar. Yazma başarısızlığı asla koşuyu düşürmez.

        Proje dizini OneDrive gibi senkronize edilen bir klasördeyse, senkron
        sürecinin dosyayı geçici olarak kilitlemesi `os.replace()`'i WinError 32
        ile patlatabiliyor (gerçek koşuda görüldü — tüm tarama bu yüzden çökmüştü).
        Önbellek bir hız optimizasyonu, doğruluk şartı değil: birkaç kısa denemeden
        sonra hâlâ başarısızsa sessizce vazgeçilir, çağırana normal sonuç döner.
        """
        path = self._cache_path(result.url)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"url": result.url, "status": result.status, "text": result.text}
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            for attempt in range(3):
                try:
                    tmp.replace(path)
                    return
                except OSError:
                    if attempt == 2:
                        raise
                    time.sleep(0.2 * (attempt + 1))
        except OSError:
            tmp.unlink(missing_ok=True)

    # --------------------------------------------------------------- robots

    def _robots_for(self, url: str) -> RobotFileParser | None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        with self._robots_lock:
            if origin in self._robots:
                return self._robots[origin]

        parser: RobotFileParser | None = None
        try:
            self.limiter.wait(origin)
            resp = self._client.get(urljoin(origin, "/robots.txt"))
            if resp.status_code == 200 and resp.text.strip():
                parser = RobotFileParser()
                parser.parse(resp.text.splitlines())
        except httpx.HTTPError:
            parser = None  # robots.txt okunamadıysa kısıt yok varsayılır

        with self._robots_lock:
            self._robots[origin] = parser
        return parser

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(_ua(), url)

    # ---------------------------------------------------------------- fetch

    def get(self, url: str, use_cache: bool = True) -> FetchResult:
        if use_cache:
            cached = self._read_cache(url)
            if cached is not None:
                self.stats["cache"] += 1
                return cached

        if not self.allowed(url):
            self.stats["blocked"] += 1
            return FetchResult(url=url, status=None, text="", blocked_by_robots=True)

        origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
        last_error: str | None = None
        insecure = False

        for attempt in range(MAX_RETRIES):
            self.limiter.wait(origin)
            try:
                client = self._insecure_client() if insecure else self._client
                resp = client.get(url)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                # Bazı kurumsal Türk siteleri ara sertifikayı sunmuyor; zincir eksik
                # olduğu için doğrulama patlıyor. Sadece bu durumda, sadece bir kez,
                # doğrulamayı kapatıp tekrar deniyoruz ve sonucu işaretliyoruz —
                # kayıt `insecure_tls` ile gelir, sessizce geçiştirilmez.
                if not insecure and "CERTIFICATE_VERIFY_FAILED" in str(exc):
                    insecure = True
                    continue
                time.sleep(2**attempt)
                continue

            if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if (retry_after or "").isdigit() else 2**attempt
                time.sleep(min(delay, 30))
                continue

            self.stats["network"] += 1
            if insecure:
                self.stats["insecure_tls"] = self.stats.get("insecure_tls", 0) + 1
            result = FetchResult(
                url=str(resp.url), status=resp.status_code, text=resp.text,
                insecure_tls=insecure,
            )
            if result.ok:
                # Önbelleğe istenen URL ile yazılır; yönlendirme sonrası adres değil.
                self._write_cache(FetchResult(url=url, status=resp.status_code, text=resp.text))
            return result

        self.stats["errors"] += 1
        return FetchResult(url=url, status=None, text="", error=last_error)

    def get_json(self, url: str, use_cache: bool = True):
        result = self.get(url, use_cache=use_cache)
        if not result.ok:
            return None
        try:
            return json.loads(result.text)
        except json.JSONDecodeError:
            return None

    def close(self) -> None:
        self._client.close()
        if self._insecure is not None:
            self._insecure.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
