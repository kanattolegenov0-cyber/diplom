"""Language, intent routing, and safety boundaries for the command parser."""

import unittest

from app.commands import MAX_REMINDER_DELAY, parse_command


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
        for text, initial, expected in (("громче", 95, 100), ("тише", 5, 0), ("дыбысты көтер", 60, 70), ("дыбысты азайт", 60, 50), ("дауысын көтер", 60, 70), ("Аян, музыканың дауысын азайт", 60, 50), ("дауысын 20 пайызға төмендет", 60, 40), ("дыбысты көбейтші", 30, 40), ("дауысын қаттырақ қыл", 30, 40), ("дауысын ақырын қыл", 30, 20), ("громче на 25", 30, 55), ("дыбысты 50%-ға қой", 60, 50), ("громкость 110%", 60, 100), ("громкость -10", 60, 0)):
            with self.subTest(text=text):
                self.assert_command(text, "music_control", {"action": "volume", "volume": expected}, volume=initial)

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


if __name__ == "__main__":
    unittest.main()
