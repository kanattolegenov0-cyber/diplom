"""Deterministic Kazakh/Russian commands for Ayan, without side effects."""

from __future__ import annotations

import ipaddress
import re
from difflib import SequenceMatcher
from urllib.parse import urlsplit, urlunsplit


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


def normalize_public_url(value: str) -> str:
    """Normalize a public HTTP(S) URL or raise ValueError.

    This checks syntax and literal IP addresses only. The browser backend must
    also resolve DNS and reject non-public addresses before each navigation.
    """
    value = _unquote(value)
    if not value or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        raise ValueError("URL бос болмауы және бос орын қамтымауы керек.")
    if value.startswith("//"):
        raise ValueError("Толық HTTP(S) URL немесе домен көрсетіңіз.")
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("Сайттың мекенжайы дұрыс емес.") from exc
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("Тек HTTP және HTTPS сайттарын ашуға болады.")
    if not host or parts.username is not None or parts.password is not None:
        raise ValueError("URL ішінде логин не құпиясөз болмауы керек.")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Порт нөмірі дұрыс емес.")
    host = host.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("Домен дұрыс емес.") from exc
        labels = host.split(".")
        if (
            len(host) > 253
            or len(labels) < 2
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
            or not re.fullmatch(r"(?:[a-z]{2,63}|xn--[a-z0-9-]+)", labels[-1])
            or labels[-1] in {"localhost", "local", "internal", "invalid", "test", "onion"}
            or host == "home.arpa"
            or host.endswith(".home.arpa")
        ):
            raise ValueError("Жалпыға қолжетімді сайттың доменін көрсетіңіз.")
        normalized_host = host
    else:
        if not address.is_global or address.is_multicast:
            raise ValueError("Жергілікті немесе жеке желі мекенжайларын ашуға болмайды.")
        normalized_host = f"[{address.compressed}]" if address.version == 6 else str(address)
    netloc = normalized_host + (f":{port}" if port is not None else "")
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, parts.fragment))


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
    """Return {intent, message, payload}; no IO, browser, or audio actions occur."""
    if not isinstance(text, str) or not text.strip():
        return _unknown("Команда жазыңыз немесе микрофон арқылы айтыңыз.")
    command = re.sub(r"\s+", " ", text).strip()
    command = re.sub(r"^(?:аян|ayan)[\s,:]+", "", command, flags=_FLAGS).strip()
    try:
        volume = max(0, min(100, int(volume)))
    except (TypeError, ValueError, OverflowError):
        volume = 60

    if re.fullmatch(r"(?:көмек|помощь|help|не істей аласың\??|что ты умеешь\??)", command, _FLAGS):
        return _result("help", "Музыка қосамын, сайт ашамын, күнделік пен еске салулар жүргіземін.")
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

    match = re.fullmatch(r"(?:сайт\s+аш|(?:открой|открыть)\s+(?:сайт\s+)?|open\s+)(.+)", command, _FLAGS)
    if not match:
        match = re.fullmatch(r"(.+?)\s+сайтын\s+аш", command, _FLAGS)
    if not match:
        match = re.fullmatch(r"((?:https?://|www\.)\S+|[\w.-]+\.[\w.-]+)\s+аш", command, _FLAGS)
    if match:
        try:
            url = normalize_public_url(match.group(1))
        except ValueError as exc:
            return _unknown(str(exc))
        return _result("browser_open", "Сайт ашылады.", url=url)

    match = re.fullmatch(r"введи\s+(.+?)\s+в\s+поле\s+(.+)", command, _FLAGS)
    if match:
        return _result("browser_action", "Өріс толтырылады.", action="fill", target=_unquote(match.group(2)), value=_unquote(match.group(1)))
    match = re.fullmatch(r"(.+?)\s+өрісіне\s+(.+?)\s+жаз", command, _FLAGS)
    if match:
        return _result("browser_action", "Өріс толтырылады.", action="fill", target=_unquote(match.group(1)), value=_unquote(match.group(2)))
    match = re.fullmatch(r"(?:нажми|бас)\s+(?:(?:кнопку|на)\s+)?(.+)", command, _FLAGS)
    if not match:
        match = re.fullmatch(r"(.+?)\s+түймесін\s+бас", command, _FLAGS)
    if match:
        return _result("browser_action", "Батырма басылады.", action="click", target=_unquote(match.group(1)))
    if re.fullmatch(r"(?:төмен\s+(?:жылжыт|түс)|прокрути\s+вниз|scroll\s+down)", command, _FLAGS):
        return _result("browser_action", "Бет төмен жылжиды.", action="scroll", direction="down")
    if re.fullmatch(r"(?:жоғары\s+(?:жылжыт|көтер)|прокрути\s+вверх|scroll\s+up)", command, _FLAGS):
        return _result("browser_action", "Бет жоғары жылжиды.", action="scroll", direction="up")
    if re.fullmatch(r"(?:(?:браузерде\s+)?артқа|(?:вернись\s+)?назад|go\s+back)", command, _FLAGS):
        return _result("browser_action", "Алдыңғы бет ашылады.", action="back")

    season_label = r"(?:сезон(?:ы|ын|ның)?|маусым(?:ы|ын|ның)?|season)"
    episode_label = r"(?:серия(?:сын|ны|сы)?|серию|серии|эпизод(?:ын|ы)?|episode|бөлім(?:ін|і)?)"
    anime_cue = re.search(rf"\b(?:аниме|anime|{season_label}|{episode_label})\b", command, _FLAGS)
    known_anime = any(SequenceMatcher(None, command.casefold().strip(' «»"'), name).ratio() >= .72
                      for name in ("гачиакута", "gachiakuta"))
    if anime_cue or known_anime:
        season_match = re.search(rf"(?:\b(\d{{1,2}})\s*{season_label}\b|\b{season_label}\s*(\d{{1,2}})\b)", command, _FLAGS)
        episode_match = re.search(rf"(?:\b(\d{{1,3}})\s*{episode_label}\b|\b{episode_label}\s*(\d{{1,3}})\b)", command, _FLAGS)
        season = int(next((value for value in season_match.groups() if value), 1)) if season_match else None
        episode = int(next((value for value in episode_match.groups() if value), 1)) if episode_match else None
        query = command
        for match in (season_match, episode_match):
            if match:
                query = query.replace(match.group(0), " ")
        query = re.sub(r"\b(?:аниме|anime|қара|көр|көрсет|қос|аш|смотри|посмотри|включи|открой|play|watch)\b", " ", query, flags=_FLAGS)
        query = _unquote(re.sub(r"\s+", " ", query).strip(" :,-"))
        if not query:
            return _unknown("Қай анимені көргіңіз келетінін айтыңыз.")
        if season == 0 or episode == 0:
            return _unknown("Маусым мен серия 1-ден басталады.")
        return _result("anime_search", "Аниме ізделеді.", query=query, season=season, episode=episode)

    match = re.fullmatch(r"(?:(?:громкость|звук|дыбыс(?:ты)?|дауыс(?:ты)?)(?:\s+(?:на|қой))?\s+(-?\d+)\s*%?(?:\s*[-–]?ға)?(?:\s+(?:қой|орнат))?|(?:установи|поставь)\s+громкость\s+(?:на\s+)?(-?\d+)\s*%?)", command, _FLAGS)
    if match:
        level = max(0, min(100, int(next(value for value in match.groups() if value is not None))))
        return _result("music_control", f"Дыбыс деңгейі: {level}%.", action="volume", volume=level)
    match = re.fullmatch(r"(?:громче|(?:дыбыс(?:ты)?|дауыс(?:ты)?)\s+көтер)(?:\s+(?:на\s+)?(\d+)\s*%?)?", command, _FLAGS)
    if match:
        level = min(100, volume + int(match.group(1) or 10))
        return _result("music_control", f"Дыбыс деңгейі: {level}%.", action="volume", volume=level)
    match = re.fullmatch(r"(?:тише|(?:дыбыс(?:ты)?|дауыс(?:ты)?)\s+азайт)(?:\s+(?:на\s+)?(\d+)\s*%?)?", command, _FLAGS)
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
