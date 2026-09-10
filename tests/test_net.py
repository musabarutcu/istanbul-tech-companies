"""Fetcher/önbellek testleri — ağsız.

TestCacheWriteResilience: gerçek bir koşuda çıkan hatanın regresyon testi.
Proje OneDrive gibi senkronize bir klasördeyken, senkron süreci önbellek
dosyasını yazma anında kilitleyebiliyor; `os.replace()` bu yüzden WinError 32
ile patlıyor ve düzeltilmeden önce bu, TÜM taramayı (584 şirket) çökertiyordu.
Önbellek bir hız optimizasyonu, doğruluk şartı değil — yazma asla `get()`'i
düşürmemeli.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from career_radar.net import FetchResult, Fetcher


@pytest.fixture
def fetcher(tmp_path):
    f = Fetcher(cache_dir=tmp_path, min_interval=0.0)
    yield f
    f.close()


class TestCacheWriteResilience:
    def test_write_succeeds_normally(self, fetcher, tmp_path):
        result = FetchResult(url="https://acme.com/", status=200, text="merhaba")
        fetcher._write_cache(result)
        cached = fetcher._read_cache(result.url)
        assert cached is not None
        assert cached.text == "merhaba"

    def test_transient_replace_failure_is_retried_and_recovers(self, fetcher, monkeypatch):
        """İlk iki deneme WinError 32 taklidiyle patlar, üçüncüsü başarılı olmalı."""
        calls = {"n": 0}
        real_replace = Path.replace

        def flaky_replace(self, target):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError(32, "Dosya başka bir işlem tarafından kullanılıyor")
            return real_replace(self, target)

        monkeypatch.setattr(Path, "replace", flaky_replace)
        result = FetchResult(url="https://acme.com/x", status=200, text="veri")
        fetcher._write_cache(result)  # exception fırlatmamalı

        assert calls["n"] == 3
        assert fetcher._read_cache(result.url) is not None

    def test_permanent_replace_failure_does_not_raise(self, fetcher, monkeypatch):
        """Hiç başarılı olmasa bile _write_cache sessizce vazgeçer, get()'i düşürmez."""

        def always_fails(self, target):
            raise OSError(32, "Dosya başka bir işlem tarafından kullanılıyor")

        monkeypatch.setattr(Path, "replace", always_fails)
        result = FetchResult(url="https://acme.com/y", status=200, text="veri")

        fetcher._write_cache(result)  # exception fırlatmamalı

        assert fetcher._read_cache(result.url) is None  # yazılamadı ama çökmedi

    def test_permanent_failure_cleans_up_tmp_file(self, fetcher, monkeypatch, tmp_path):
        def always_fails(self, target):
            raise OSError(32, "kilitli")

        monkeypatch.setattr(Path, "replace", always_fails)
        result = FetchResult(url="https://acme.com/z", status=200, text="veri")
        fetcher._write_cache(result)

        leftover_tmp_files = list(tmp_path.rglob("*.tmp"))
        assert leftover_tmp_files == []

    def test_tmp_filename_is_pid_scoped(self, fetcher):
        """Geçici dosya adı süreç kimliğini taşır — ayrı süreçler aynı .tmp'ye yazmaz.

        (Bir koşu yanlışlıkla iki kez başlatılırsa iki süreç aynı sabit '.tmp'
        adını paylaşır ve birbirinin dosyasını silebilirdi; bu da gerçek koşuda
        görülen kilitlenmenin olası bir başka kaynağıydı.)
        """
        import os

        result = FetchResult(url="https://acme.com/x", status=200, text="veri")
        tmp_path = fetcher._cache_path(result.url).with_suffix(f".{os.getpid()}.tmp")
        assert str(os.getpid()) in tmp_path.name
