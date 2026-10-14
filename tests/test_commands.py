"""Language, intent routing, and safety boundaries for the command parser."""

import unittest

from app.commands import MAX_REMINDER_DELAY, normalize_public_url, parse_command


class CommandTests(unittest.TestCase):
    def assert_command(self, text, intent, payload, **kwargs):
        result = parse_command(text, **kwargs)
        self.assertEqual(set(result), {"intent", "message", "payload"})
        self.assertIsInstance(result["message"], str)
        self.assertEqual(result["intent"], intent)
        self.assertEqual(result["payload"], payload)

    def test_music_search_keeps_the_requested_title(self):
        for text in ("музыка қос Димаш SOS", "включи песню Димаш SOS", "Димаш SOS әнін қос", "Аян, музыка қос Димаш SOS", "Музыка қос: Димаш SOS"):
            with self.subTest(text=text):
                self.assert_command(text, "music_search", {"query": "Димаш SOS"})

    def test_music_pause_and_resume(self):
        for text in ("пауза", "музыканы тоқтат", "выключи музыку", "pause"):
            self.assert_command(text, "music_control", {"action": "pause"})
        for text in ("жалғастыр", "продолжи музыку", "resume", "музыка қос"):
            self.assert_command(text, "music_control", {"action": "play"})

    def test_volume_is_relative_and_clamped(self):
        for text, initial, expected in (("громче", 95, 100), ("тише", 5, 0), ("дыбысты көтер", 60, 70), ("дыбысты азайт", 60, 50), ("громче на 25", 30, 55), ("дыбысты 50%-ға қой", 60, 50), ("громкость 110%", 60, 100), ("громкость -10", 60, 0)):
            with self.subTest(text=text):
                self.assert_command(text, "music_control", {"action": "volume", "volume": expected}, volume=initial)

    def test_url_open(self):
        for text in ("сайт аш example.com", "открой сайт example.com", "example.com сайтын аш"):
            self.assert_command(text, "browser_open", {"url": "https://example.com"})
        self.assert_command("открой https://www.youtube.com/results?search_query=Dimash", "browser_open", {"url": "https://www.youtube.com/results?search_query=Dimash"})
        self.assert_command("https://wikipedia.org аш", "browser_open", {"url": "https://wikipedia.org"})

    def test_browser_actions(self):
        for text in ("нажми кнопку Войти", "бас Войти", "Войти түймесін бас"):
            self.assert_command(text, "browser_action", {"action": "click", "target": "Войти"})
        self.assert_command("введи Абай в поле Іздеу", "browser_action", {"action": "fill", "target": "Іздеу", "value": "Абай"})
        self.assert_command("Іздеу өрісіне Абай жаз", "browser_action", {"action": "fill", "target": "Іздеу", "value": "Абай"})
        self.assert_command("прокрути вниз", "browser_action", {"action": "scroll", "direction": "down"})
        self.assert_command("жоғары жылжыт", "browser_action", {"action": "scroll", "direction": "up"})
        self.assert_command("артқа", "browser_action", {"action": "back"})

    def test_diary_content_is_not_misinterpreted_as_another_command(self):
        for text in ("күнделікке жаз Музыка қос деген ой келді", "запиши в дневник Музыка қос деген ой келді", "Күнделікке жаз: Музыка қос деген ой келді"):
            self.assert_command(text, "diary_add", {"content": "Музыка қос деген ой келді"})
        self.assert_command("күнделікті көрсет", "diary_list", {})
        self.assert_command("покажи дневник", "diary_list", {})

    def test_reminders_in_both_languages(self):
        for text in ("напомни через 5 минут Су іш", "5 минуттан кейін Су іш еске сал", "еске сал 5 минуттан кейін Су іш"):
            with self.subTest(text=text):
                self.assert_command(text, "reminder_add", {"title": "Су іш", "delay_seconds": 300})
        self.assert_command("2 сағаттан кейін Қоңырау шал еске сал", "reminder_add", {"title": "Қоңырау шал", "delay_seconds": 7200})
        self.assert_command("напомни через 30 дней Дайындал", "reminder_add", {"title": "Дайындал", "delay_seconds": MAX_REMINDER_DELAY})
        self.assert_command("еске салуларды көрсет", "reminders_list", {})

    def test_bad_reminder_delays_are_not_scheduled(self):
        for text in ("напомни через 0 минут Су іш", "напомни через -5 минут Су іш", "напомни через 31 дней Су іш", "5 минуттан кейін еске сал"):
            with self.subTest(text=text):
                self.assertEqual(parse_command(text)["intent"], "unknown")

    def test_empty_unknown_help_time(self):
        for text in ("", "   ", "запусти произвольный код", "күнделікке жаз"):
            self.assert_command(text, "unknown", {})
        self.assert_command("көмек", "help", {})
        self.assert_command("сағат қанша?", "time", {})


class PublicUrlTests(unittest.TestCase):
    def test_public_urls_are_normalized(self):
        self.assertEqual(normalize_public_url("HTTPS://Example.COM:443/a?b=c#d"), "https://example.com:443/a?b=c#d")
        self.assertEqual(normalize_public_url("8.8.8.8"), "https://8.8.8.8")
        self.assertEqual(normalize_public_url("https://[2606:4700:4700::1111]/"), "https://[2606:4700:4700::1111]/")
        self.assertEqual(normalize_public_url("пример.рф"), "https://xn--e1afmkfd.xn--p1ai")

    def test_local_private_and_ambiguous_addresses_are_rejected(self):
        unsafe = (
            "localhost", "localhost:3000", "foo.localhost", "example.local", "router.internal", "home.arpa", "foo.home.arpa", "127.0.0.1", "127.1", "2130706433", "0x7f000001", "0177.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.1.1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "https://[::1]", "https://[fc00::1]", "https://[fe80::1]", "http://user:pass@example.com", "file:///etc/passwd", "javascript:alert(1)", "ftp://example.com", "https://example.com:99999", "https://example.com:0", "https://example.com@127.0.0.1", "https://example.com\\@127.0.0.1", "https://exa mple.com", "//example.com", "https://example.com%2f.local", "example.test",
        )
        for value in unsafe:
            with self.subTest(url=value):
                with self.assertRaises(ValueError):
                    normalize_public_url(value)
                self.assertEqual(parse_command("сайт аш " + value)["intent"], "unknown")


if __name__ == "__main__":
    unittest.main()
