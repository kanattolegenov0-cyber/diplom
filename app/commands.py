"""Deterministic Kazakh/Russian commands for Ayan, without side effects."""

from __future__ import annotations

import re


MAX_REMINDER_DELAY = 30 * 24 * 60 * 60
_FLAGS = re.IGNORECASE | re.DOTALL

_PLAYLIST_NAME = (
    r"(?:(?:менің\s+)?(?:плейлист(?:ім(?:ді)?|ті)?|"
    r"жиі\s+тыңда(?:йтын|ған|лған)\s+(?:әндер|өлеңдер)(?:ім(?:ді)?|ді)?)|"
    r"(?:мой\s+)?плейлист|(?:мои\s+)?часто\s+(?:слушаемые|прослушиваемые)\s+(?:песни|треки))"
)


def mentions_playlist(text: str) -> bool:
    """Keep playlist requests out of the bare-song fallback."""
    return bool(re.search(
        r"\b(?:плейлист[а-яәғқңөұүһі]*|playlists?|"
        r"жиі\s+тыңда(?:йтын|ған|лған)|часто\s+(?:слушаемые|прослушиваемые))\b",
        text, _FLAGS,
    ))


def _playlist_command(command: str) -> dict | None:
    if re.fullmatch(
        rf"(?:{_PLAYLIST_NAME}\s+(?:қос|қой|ойнат|қосып\s+бер)|"
        rf"(?:включи|запусти|проиграй)\s+{_PLAYLIST_NAME})[.!]?", command, _FLAGS
    ):
        return _result("music_playlist", "Жиі тыңдалған әндер плейлистін қосамын.", action="play")
    if re.fullmatch(
        rf"(?:{_PLAYLIST_NAME}(?:\s+(?:көрсет|аш))?|"
        rf"(?:покажи|открой)\s+{_PLAYLIST_NAME})[.!]?", command, _FLAGS
    ):
        return _result("music_playlist", "Жиі тыңдалған әндер плейлисті ашылады.", action="show")
    return None


def _result(intent: str, message: str, **payload: object) -> dict:
    return {"intent": intent, "message": message, "payload": payload}


def _unknown(message: str = "Бұл команданы түсінбедім. «Көмек» деп айтыңыз.") -> dict:
    return _result("unknown", message)


def _unquote(value: str) -> str:
    value = value.strip()
    pairs = {"\"": "\"", "'": "'", "«": "»", "“": "”"}
    if len(value) >= 2 and pairs.get(value[0]) == value[-1]:
        return value[1:-1].strip()
    return value


def _reminder(amount: str, unit: str, title: str) -> dict:
    unit = unit.casefold().rstrip(".")
    if unit.startswith(("сек", "second")):
        multiplier = 1
    elif unit.startswith(("мин", "minute")):
        multiplier = 60
    elif unit.startswith(("сағат", "час", "hour")):
        multiplier = 3600
    else:
        multiplier = 86400
    delay = int(amount) * multiplier
    title = _unquote(title.lstrip(" :,-"))
    if not 0 < delay <= MAX_REMINDER_DELAY:
        return _unknown("Еске салу мерзімі 1 секундтан 30 күнге дейін болуы керек.")
    if not title or title.casefold() == "еске сал":
        return _unknown("Нені еске салу керегін айтыңыз.")
    return _result("reminder_add", "Еске салу қосылды.", title=title, delay_seconds=delay)


def parse_command(text: str, volume: int = 60) -> dict:
    """Return {intent, message, payload}; no IO or audio actions occur."""
    if not isinstance(text, str) or not text.strip():
        return _unknown("Команда жазыңыз немесе микрофон арқылы айтыңыз.")
    command = re.sub(r"\s+", " ", text).strip()
    command = re.sub(r"^(?:аян|ayan)[\s,:]+", "", command, flags=_FLAGS).strip()
    try:
        volume = max(0, min(100, int(volume)))
    except (TypeError, ValueError, OverflowError):
        volume = 60

    if re.fullmatch(r"(?:көмек|помощь|help|не істей аласың\??|что ты умеешь\??)", command, _FLAGS):
        return _result("help", "Музыка қосамын, күнделік, тапсырмалар мен еске салулар жүргіземін.")
    if re.fullmatch(r"(?:уақыт|сағат қанша\??|қазір сағат қанша\??|который час\??|время|time)", command, _FLAGS):
        return _result("time", "Ағымдағы уақыт.")

    match = re.fullmatch(r"(?:күнделікке\s+(?:жаз|қос)|(?:запиши|добавь)\s+в\s+дневник)(?:\s*:\s*|\s+)(.+)", command, _FLAGS)
    if match:
        content = _unquote(match.group(1))
        return _result("diary_add", "Күнделікке жазылды.", content=content) if content else _unknown("Күнделікке не жазу керегін айтыңыз.")
    if re.fullmatch(r"(?:күнделік|күнделікті\s+(?:көрсет|аш)|күнделік\s+(?:көрсет|аш)|дневник|(?:покажи|открой)\s+дневник|записи\s+дневника)", command, _FLAGS):
        return _result("diary_list", "Күнделік жазбалары.")

    units = r"(секунд(?:а|ы|у)?|сек\.?|минут(?:а|ы|у|тан)?|мин\.?|сағат(?:тан)?|час(?:а|ов)?|күн(?:нен)?|день|дня|дней|seconds?|minutes?|hours?|days?)"
    match = re.fullmatch(r"напомни\s+через\s+(-?\d+)\s*" + units + r"\s+(.+)", command, _FLAGS)
    if match:
        return _reminder(*match.groups())
    match = re.fullmatch(r"(?:еске\s+сал\s+)?(-?\d+)\s*" + units + r"\s+кейін\s+(.+?)(?:\s+еске\s+сал)?", command, _FLAGS)
    if match:
        return _reminder(*match.groups())
    if re.fullmatch(r"(?:еске\s+салулар(?:ды\s+көрсет)?|(?:покажи\s+)?напоминания|список\s+напоминаний)", command, _FLAGS):
        return _result("reminders_list", "Еске салулар тізімі.")

    playlist = _playlist_command(command)
    if playlist:
        return playlist

    volume_command = command.rstrip(".!?").strip()
    volume_noun = r"(?:(?:музыканың|музыка)\s+)?(?:дауыс(?:ты|ын)?|дыбыс(?:ты|ын)?|громкость|звук)"
    amount = r"(?:\s+(?:на\s+)?(\d+)\s*(?:%|пайыз)?(?:\s*[-–]?(?:ға|қа))?)?"
    match = re.fullmatch(
        rf"(?:{volume_noun}(?:\s+(?:на|қой))?\s+(-?\d+)\s*%?(?:\s*[-–]?(?:ға|қа))?(?:\s+(?:қой|орнат))?|"
        r"(?:установи|поставь)\s+громкость\s+(?:на\s+)?(-?\d+)\s*%?)",
        volume_command, _FLAGS)
    if match:
        level = max(0, min(100, int(next(value for value in match.groups() if value is not None))))
        return _result("music_control", f"Дыбыс деңгейі: {level}%.", action="volume", volume=level)
    match = re.fullmatch(
        rf"(?:{volume_noun}\s+(?:көтер(?:ші)?|көбейт(?:ші)?|арттыр|жоғарылат|қаттырақ(?:\s+қыл)?)|"
        rf"(?:көтер|көбейт|арттыр)\s+{volume_noun}|громче|сделай\s+громче|прибавь\s+(?:звук|громкость))"
        + amount, volume_command, _FLAGS)
    if not match:
        match = re.fullmatch(rf"{volume_noun}\s+(\d+)\s*(?:%|пайыз)?\s*[-–]?(?:ға|қа)\s+(?:көтер|көбейт|арттыр)", volume_command, _FLAGS)
    if match:
        level = min(100, volume + int(match.group(1) or 10))
        return _result("music_control", f"Дыбыс деңгейі: {level}%.", action="volume", volume=level)
    match = re.fullmatch(
        rf"(?:{volume_noun}\s+(?:азайт(?:шы)?|төмендет|бәсеңдет|ақырындат|ақырын(?:ырақ)?(?:\s+қыл)?)|"
        rf"(?:азайт|төмендет|бәсеңдет)\s+{volume_noun}|тише|сделай\s+тише|убавь\s+(?:звук|громкость))"
        + amount, volume_command, _FLAGS)
    if not match:
        match = re.fullmatch(rf"{volume_noun}\s+(\d+)\s*(?:%|пайыз)?\s*[-–]?(?:ға|қа)\s+(?:азайт|төмендет|бәсеңдет)", volume_command, _FLAGS)
    if match:
        level = max(0, volume - int(match.group(1) or 10))
        return _result("music_control", f"Дыбыс деңгейі: {level}%.", action="volume", volume=level)
    if re.fullmatch(r"(?:(?:музыканы|музыку)\s+)?(?:пауза|тоқтат|pause)|(?:останови|выключи)\s+музыку", command, _FLAGS):
        return _result("music_control", "Музыка тоқтатылды.", action="pause")
    if re.fullmatch(r"(?:жалғастыр|продолжи|продолжить|resume|play|(?:музыканы\s+)?жалғастыр|продолжи\s+музыку|включи\s+музыку|музыка\s+қос)", command, _FLAGS):
        return _result("music_control", "Музыка жалғасады.", action="play")
    match = re.fullmatch(r"(?:музыка\s+қос|ән\s+қос|включи(?:\s+(?:песню|музыку|трек))?|play)(?:\s*:\s*|\s+)(.+)", command, _FLAGS)
    if not match:
        match = re.fullmatch(r"(.+?)\s+(?:әнін|музыкасын)\s+қос", command, _FLAGS)
    if match:
        query = _unquote(match.group(1))
        if mentions_playlist(query):
            return _unknown("Жиі тыңдалған әндерді қосу үшін «плейлистімді қос» деп айтыңыз.")
        return _result("music_search", f"YouTube-тен іздеу: {query}.", query=query) if query else _unknown("Әннің атын айтыңыз.")
    return _unknown()
