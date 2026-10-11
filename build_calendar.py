"""Build a subscribable iCalendar feed from Agenda Eletrônica SP.

The script runs in GitHub Actions and fails rather than publishing an empty feed
if the source page changes unexpectedly.
"""
from __future__ import annotations

import hashlib
import html
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag

SOURCE_URL = "https://agendaeletronica.com.br/"
OUTPUT_PATH = Path(__file__).resolve().parent / "agenda-eletronica-sp.ics"
TZ = ZoneInfo("America/Sao_Paulo")
MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11,
    "dezembro": 12,
}
DATE_HEADING = re.compile(
    r"(?:segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo)(?:-feira)?\s*,?\s*"
    r"(?P<day>\d{1,2})\s+de\s+(?P<month>[a-zçãéêô]+)\s+de\s+(?P<year>\d{4})",
    re.IGNORECASE,
)
TIME_SUFFIX = re.compile(r"\bàs\s*(?P<hour>\d{1,2})h(?P<minute>\d{2})?\s*$", re.IGNORECASE)
DATETIME_SUFFIX = re.compile(
    r"\s+\d{1,2}\s+de\s+[a-zçãéêô]+\s+às\s+\d{1,2}h\d{0,2}\s*$",
    re.IGNORECASE,
)
SP_CITIES = [
    "São José dos Campos", "São Sebastião", "Nazaré Paulista", "Pirassununga",
    "Mairiporã", "São Paulo", "Campinas", "Valinhos", "Ubatuba", "Sorocaba",
    "Ilhabela", "Guarujá", "Sumaré", "Cotia", "Itirapina", "Piedade",
]
CITY_NAMES_PATTERN = "|".join(re.escape(city) for city in sorted(SP_CITIES, key=len, reverse=True))
CITY_SUFFIX = re.compile(
    rf"\s*(?:\|\s*)?(?P<city>(?:{CITY_NAMES_PATTERN})-SP)\s*$",
    re.IGNORECASE,
)
KNOWN_VENUES = [
    "Crema Club", "Dr Banana Club", "Home Club", "Le Club", "Ephigenia",
    "Mansão Morumbi", "Sonora Garden", "CAOS", "AME Club", "Cafe de La Musique",
    "Laroc Club Guarujá", "Laroc Club", "Sea Club Ilhabela", "Parador Maresias",
    "High Club", "Arena Épico", "Estúdio Lâmina", "Komplexo Tempo",
    "Parque Villa-Lobos", "Vale Do Anhangabaú", "Santa Helena Eventos e Lazer",
    "Autódromo de Interlagos", "Fazenda Meia Lua", "Clube Esperia",
    "Hotel Unique", "Centro de Eventos - Arena Pinheiros", "Aeródromo Nascimento",
    "Pier18 Lounge Club", "Club A Moema", "Amiiici Club", "Bardo Netão",
    "Parador Maresias", "Sea Club Ilhabela", "Laroc Club Guarujá",
]


@dataclass(frozen=True)
class Event:
    title: str
    event_date: date
    hour: int
    minute: int
    location: str
    ticket_url: str


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def parse_heading_date(text: str) -> date | None:
    match = DATE_HEADING.search(clean_text(text))
    if not match:
        return None
    month = MONTHS.get(match.group("month").lower())
    if not month:
        return None
    try:
        return date(int(match.group("year")), month, int(match.group("day")))
    except ValueError:
        return None


def is_time_or_location_piece(value: str) -> bool:
    value = clean_text(value)
    return (
        not value or value in {"|", "—", "–", "-"}
        or bool(TIME_SUFFIX.search(value))
        or bool(re.search(r"\b\d{1,2}\s+de\s+[a-zçãéêô]+\s+às\s+\d{1,2}h", value, re.IGNORECASE))
        or bool(re.search(r"(?:SP|São Paulo)\s*$", value, re.IGNORECASE))
    )


def split_repeated_venue(core: str) -> tuple[str, str]:
    """Split duplicated venue labels or a recognized venue at the end of a listing."""
    words = core.split()
    # Listings often repeat the venue after the event title. Prefer that exact pattern.
    for width in range(min(len(words) // 2, 6), 0, -1):
        suffix = " ".join(words[-width:])
        prefix = " ".join(words[:-width]).rstrip(" -–—|:")
        if prefix and prefix.casefold().endswith(suffix.casefold()):
            title = prefix.rstrip(" -–—|:")
            if title:
                return title, suffix
    # Some entries append the venue only once, without a separator.
    for venue in sorted(KNOWN_VENUES, key=len, reverse=True):
        if core.casefold().endswith((" " + venue).casefold()):
            title = core[:-len(venue)].rstrip(" -–—|:")
            if title:
                return title, venue
    return core.strip(), ""


def parse_anchor(anchor: Tag, event_date: date) -> Event | None:
    href = anchor.get("href", "").strip()
    if not href:
        return None
    absolute_url = urljoin(SOURCE_URL, href)
    host = (urlparse(absolute_url).hostname or "").lower()
    if not host or host.endswith(("agendaeletronica.com.br", "facebook.com")):
        return None

    visible = clean_text(anchor.get_text(" ", strip=True))
    if not visible:
        return None

    time_match = TIME_SUFFIX.search(visible)
    if not time_match:
        alt_text = clean_text(anchor.get("aria-label", "") or anchor.get("title", ""))
        time_match = TIME_SUFFIX.search(alt_text)
        if time_match:
            visible = alt_text
    if not time_match:
        return None
    hour = int(time_match.group("hour"))
    minute = int(time_match.group("minute") or 0)
    if hour > 23 or minute > 59:
        return None

    core_with_city = DATETIME_SUFFIX.sub("", visible)
    core_with_city = TIME_SUFFIX.sub("", core_with_city).strip()
    city_match = CITY_SUFFIX.search(core_with_city)
    city = clean_text(city_match.group("city")) if city_match else ""
    core = CITY_SUFFIX.sub("", core_with_city).strip() if city_match else core_with_city

    semantic_tags = ["span", "strong", "b", "em"]
    semantic_nodes = [
        node for node in anchor.find_all(semantic_tags)
        if not node.find(semantic_tags)
    ]
    pieces = [clean_text(node.get_text(" ", strip=True)) for node in semantic_nodes]
    pieces = [piece for piece in pieces if piece and piece not in {"|", "—", "–"}]
    pieces = [piece for piece in pieces if not is_time_or_location_piece(piece)]
    title = ""
    venue = ""
    if len(pieces) >= 2:
        first = CITY_SUFFIX.sub("", DATETIME_SUFFIX.sub("", pieces[0])).strip()
        second = CITY_SUFFIX.sub("", DATETIME_SUFFIX.sub("", pieces[1])).strip()
        if first and second and first.casefold() != second.casefold():
            title, venue = first, second

    if not title:
        title, venue = split_repeated_venue(core)

    title = clean_text(title or core)
    venue = clean_text(venue)
    if not title:
        return None

    location = venue
    if city:
        location = f"{venue}, {city}" if venue else city
    if not location:
        location = "São Paulo-SP"

    return Event(title, event_date, hour, minute, location, absolute_url)


def unescape_ics_text(value: str) -> str:
    return (value.replace(r"\n", "\n").replace(r"\,", ",")
            .replace(r"\;", ";").replace(r"\\", "\\"))


def read_existing_feed_context() -> dict[str, tuple[str, str]]:
    """Reuse prior title/location for known ticket URLs when HTML loses boundaries."""
    if not OUTPUT_PATH.exists():
        return {}
    raw = OUTPUT_PATH.read_text(encoding="utf-8", errors="replace")
    raw = re.sub(r"\r?\n[ \t]", "", raw)
    context: dict[str, tuple[str, str]] = {}
    for block in raw.split("BEGIN:VEVENT")[1:]:
        url_match = re.search(r"^URL:(.+)$", block, re.MULTILINE)
        title_match = re.search(r"^SUMMARY:(.+)$", block, re.MULTILINE)
        location_match = re.search(r"^LOCATION:(.+)$", block, re.MULTILINE)
        if url_match and title_match and location_match:
            url = url_match.group(1).strip().rstrip("/")
            title = unescape_ics_text(title_match.group(1).strip())
            location = unescape_ics_text(location_match.group(1).strip())
            context[url] = (title, location)
    return context


def scrape_events(now: datetime | date | None = None) -> list[Event]:
    # Accept an explicit datetime for exact filtering; date-only inputs mean local midnight.
    if now is None:
        reference_time = datetime.now(TZ)
    elif isinstance(now, datetime):
        reference_time = now.astimezone(TZ) if now.tzinfo else now.replace(tzinfo=TZ)
    else:
        reference_time = datetime.combine(now, datetime.min.time(), tzinfo=TZ)
    existing_context = read_existing_feed_context()
    response = requests.get(
        SOURCE_URL,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; AgendaEletronicaCalendar/1.0; +https://github.com/)"
        },
    )
    response.raise_for_status()
    if len(response.text) < 1000:
        raise RuntimeError("A página de origem retornou conteúdo inesperadamente curto.")

    soup = BeautifulSoup(response.text, "html.parser")
    events: list[Event] = []
    active_date: date | None = None
    headings_and_links = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "a"])
    for node in headings_and_links:
        if node.name and node.name.startswith("h"):
            heading_text = clean_text(node.get_text(" ", strip=True))
            parsed_date = parse_heading_date(heading_text)
            if parsed_date:
                active_date = parsed_date
            elif re.search(r"festas eletrônicas por estado|festas eletr[oô]nicas por estado", heading_text, re.IGNORECASE):
                active_date = None
            continue
        if active_date is None or not isinstance(node, Tag):
            continue
        event = parse_anchor(node, active_date)
        if event and datetime(
            event.event_date.year, event.event_date.month, event.event_date.day,
            event.hour, event.minute, tzinfo=TZ,
        ) >= reference_time:
            cached = existing_context.get(event.ticket_url.rstrip("/"))
            if cached:
                # Do not resurrect a stale title if it incorrectly ends with a city suffix.
                cached_title_has_city = CITY_SUFFIX.search(cached[0]) is not None
                city_only = "," not in event.location and not cached_title_has_city
                partial_title = (
                    not cached_title_has_city
                    and event.location == cached[1]
                    and event.title.casefold() != cached[0].casefold()
                    and event.title.casefold() in cached[0].casefold()
                )
                if city_only or partial_title:
                    event = Event(cached[0], event.event_date, event.hour, event.minute, cached[1], event.ticket_url)
            events.append(event)

    unique: dict[tuple[str, date], Event] = {}
    for event in events:
        unique[(event.ticket_url.rstrip("/"), event.event_date)] = event
    result = sorted(unique.values(), key=lambda e: (e.event_date, e.hour, e.minute, e.title.casefold()))
    if not result:
        raise RuntimeError(
            "Nenhum evento futuro foi extraído. O HTML pode ter mudado ou a origem bloqueou a consulta; "
            "o feed existente foi preservado porque a atualização falhou."
        )
    return result


def ics_escape(value: str) -> str:
    raw_lines = html.unescape(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    escaped_lines = []
    for raw_line in raw_lines:
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        line = line.replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,")
        escaped_lines.append(line)
    return r"\n".join(escaped_lines)


def fold_ics_line(line: str, limit: int = 75) -> str:
    """Fold content lines at 75 UTF-8 octets as required by RFC 5545."""
    chunks: list[str] = []
    current = ""
    current_bytes = 0
    for char in line:
        char_bytes = len(char.encode("utf-8"))
        if current and current_bytes + char_bytes > limit:
            chunks.append(current)
            current = " " + char
            current_bytes = 1 + char_bytes
        else:
            current += char
            current_bytes += char_bytes
    chunks.append(current)
    return "\r\n".join(chunks)


def build_ics(events: list[Event], generated_at: datetime | None = None) -> str:
    stamp = (generated_at or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Agenda Eletronica//Calendario SP//PT-BR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Agenda Eletrônica SP — festas e ingressos",
        "X-WR-TIMEZONE:America/Sao_Paulo",
    ]
    for event in events:
        local_start = datetime(
            event.event_date.year, event.event_date.month, event.event_date.day,
            event.hour, event.minute, tzinfo=TZ,
        )
        utc_start = local_start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        digest = hashlib.sha256((event.ticket_url.rstrip("/") + event.event_date.isoformat()).encode("utf-8")).hexdigest()[:24]
        description = f"Compra de ingressos: {event.ticket_url}\nAgenda de origem: {SOURCE_URL}"
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{digest}@agendaeletronica-sp",
            f"DTSTAMP:{stamp}",
            f"LAST-MODIFIED:{stamp}",
            f"DTSTART:{utc_start}",
            f"SUMMARY:{ics_escape(event.title)}",
            f"LOCATION:{ics_escape(event.location)}",
            f"DESCRIPTION:{ics_escape(description)}",
            f"URL:{event.ticket_url}",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ics_line(line) for line in lines) + "\r\n"


def write_feed_atomically(content: str, output_path: Path | None = None) -> None:
    """Replace the public feed atomically so readers never see a partial file."""
    target = output_path or OUTPUT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, target)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def normalize_feed_timestamps(content: str) -> str:
    """Compare calendar data while ignoring generation-only timestamps and newline style."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(
        r"^(DTSTAMP|LAST-MODIFIED):\d{8}T\d{6}Z$",
        r"\1:TIMESTAMP",
        normalized,
        flags=re.MULTILINE,
    )

def main() -> int:
    try:
        events = scrape_events()
        content = build_ics(events)
        if OUTPUT_PATH.exists():
            existing = OUTPUT_PATH.read_text(encoding="utf-8", errors="replace")
            if normalize_feed_timestamps(existing) == normalize_feed_timestamps(content):
                print("Os eventos não mudaram; preservando o feed atual sem alterar os timestamps.")
            else:
                write_feed_atomically(content)
                print(f"Feed atualizado: {len(events)} eventos futuros -> {OUTPUT_PATH.name}")
        else:
            write_feed_atomically(content)
            print(f"Feed criado: {len(events)} eventos futuros -> {OUTPUT_PATH.name}")
        for event in events:
            print(f"- {event.event_date.isoformat()} {event.hour:02d}:{event.minute:02d} | {event.title} | {event.location}")
        return 0
    except (requests.RequestException, OSError, ValueError, RuntimeError, TypeError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
