"""CLI yardımcı fonksiyon testleri.

test_source_display_health: gerçek bir hatanın regresyon testi. Bilkent CYBERPARK
İstanbul kapsamına geçilirken kapatıldığında (enabled=0) panel hâlâ eski koşudan
kalan health='ok' değerini gösteriyordu — kapatılmış bir kaynak çalışıyor gibi
görünüyordu.
"""

from career_radar.cli import source_display_health


class TestSourceDisplayHealth:
    def test_disabled_source_shows_kapali_even_with_stale_ok_health(self):
        assert source_display_health(enabled=0, health="ok") == "kapalı"

    def test_disabled_source_shows_kapali_with_no_health(self):
        assert source_display_health(enabled=0, health=None) == "kapalı"

    def test_enabled_source_shows_its_health(self):
        assert source_display_health(enabled=1, health="ok") == "ok"
        assert source_display_health(enabled=1, health="error") == "error"

    def test_enabled_source_never_crawled_shows_dash(self):
        assert source_display_health(enabled=1, health=None) == "-"
