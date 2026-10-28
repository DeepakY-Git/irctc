#!/usr/bin/env python3
"""Automated Indian Railways current availability alert pipeline.

This script polls a configurable tracking URL, parses HTML with BeautifulSoup,
tracks state locally to avoid duplicate notifications, and sends a high-priority
Pushover alert when current availability is detected.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional dependency
    sync_playwright = None


DEFAULT_POLL_INTERVAL_SECONDS = 90
DEFAULT_MAX_RETRIES = 3
BACKOFF_SECONDS = (30, 60, 120)
WINDOW_ALPHA_HOURS = 9
WINDOW_BETA_HOURS = 8
WINDOW_OMEGA_MINUTES = 30


@dataclasses.dataclass(frozen=True)
class Config:
    """Runtime configuration derived from CLI arguments and environment."""

    train_number: str
    source_station: str
    destination_station: str
    travel_date: str
    travel_class: str
    quota: str
    tracking_url: str
    pushover_user_key: str
    pushover_app_token: str
    pushover_device: str
    state_file: Path
    timeout_seconds: int
    poll_interval_seconds: int
    use_browser_rendering: bool
    browser_wait_seconds: int
    cutoff_at: Optional[datetime] = None


@dataclasses.dataclass(frozen=True)
class AvailabilityResult:
    """Normalized availability payload."""

    status: str
    vacant_count: Optional[int]
    raw_payload: Dict[str, Any]


@dataclasses.dataclass(frozen=True)
class AvailabilityEntry:
    """One parsed availability row from the HTML response."""

    train_number: str
    train_name: str
    train_class: str
    raw_text: str
    status: str = "UNKNOWN"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll current availability and alert on seat opening via Pushover."
    )
    parser.add_argument("--train-number", default=os.getenv("TRAIN_NUMBER", ""))
    parser.add_argument("--source", default=os.getenv("SOURCE_STATION", ""))
    parser.add_argument("--destination", default=os.getenv("DESTINATION_STATION", ""))
    parser.add_argument("--travel-date", default=os.getenv("TRAVEL_DATE", ""))
    parser.add_argument("--travel-class", default=os.getenv("TRAVEL_CLASS", "SL"))
    parser.add_argument("--quota", default=os.getenv("QUOTA", "GN"))
    parser.add_argument(
        "--tracking-url",
        default=os.getenv("TRACKING_URL", "https://example.invalid/irctc/search"),
        help="HTML page or URL template to fetch and parse with BeautifulSoup.",
    )
    parser.add_argument("--pushover-user-key", default=os.getenv("PUSHOVER_USER_KEY", ""))
    parser.add_argument("--pushover-app-token", default=os.getenv("PUSHOVER_APP_TOKEN", ""))
    parser.add_argument("--pushover-device", default=os.getenv("PUSHOVER_DEVICE", ""))
    parser.add_argument(
        "--state-file",
        default=os.getenv("ALERT_STATE_FILE", "alert_fired.txt"),
        help="Local file used to suppress duplicate alerts.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=int(os.getenv("CHECK_INTERVAL_SECONDS", "90")),
    )
    parser.add_argument(
        "--use-browser-rendering",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("USE_BROWSER_RENDERING", "1").strip().lower() not in {"0", "false", "no"},
        help="Render the page in a headless browser before parsing it with BeautifulSoup.",
    )
    parser.add_argument(
        "--browser-wait-seconds",
        type=int,
        default=int(os.getenv("BROWSER_WAIT_SECONDS", "5")),
        help="Extra time to wait after the page loads so client-side data can appear.",
    )
    parser.add_argument(
        "--cutoff",
        default=os.getenv("MONITOR_CUTOFF", ""),
        help="Stop monitoring at an ISO datetime, for example 2026-06-18T23:00:00.",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> Config:
    if not args.train_number:
        raise ValueError("train number is required")
    if not args.source:
        raise ValueError("source station is required")
    if not args.destination:
        raise ValueError("destination station is required")
    if not args.travel_date:
        raise ValueError("travel date is required")
    state_file = Path(args.state_file).expanduser().resolve()
    cutoff_at = datetime.fromisoformat(args.cutoff) if args.cutoff else None

    return Config(
        train_number=args.train_number,
        source_station=args.source,
        destination_station=args.destination,
        travel_date=args.travel_date,
        travel_class=args.travel_class,
        quota=args.quota or "GN",
        tracking_url=args.tracking_url,
        pushover_user_key=args.pushover_user_key,
        pushover_app_token=args.pushover_app_token,
        pushover_device=args.pushover_device,
        state_file=state_file,
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        use_browser_rendering=args.use_browser_rendering,
        browser_wait_seconds=args.browser_wait_seconds,
        cutoff_at=cutoff_at,
    )


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def resolve_tracking_url(config: Config) -> str:
    tracking_url = config.tracking_url.format(
        train_number=config.train_number,
        source=config.source_station,
        destination=config.destination_station,
        travel_date=config.travel_date,
        travel_class=config.travel_class,
        quota=config.quota,
    )

    if is_confirmtkt_home_url(tracking_url):
        return build_confirmtkt_route_url(config)

    return tracking_url


def is_confirmtkt_home_url(url: str) -> bool:
    normalized = url.rstrip("/")
    return normalized == "https://www.confirmtkt.com" or normalized == "https://confirmtkt.com"


def build_confirmtkt_route_url(config: Config) -> str:
    travel_date = normalize_confirmtkt_date(config.travel_date)
    return (
        f"https://www.confirmtkt.com/rbooking-d/trains/from/"
        f"{url_encode_station(config.source_station)}/to/{url_encode_station(config.destination_station)}/{travel_date}"
    )


def normalize_confirmtkt_date(value: str) -> str:
    if re.fullmatch(r"\d{2}-\d{2}-\d{4}", value):
        return value

    try:
        parsed = datetime.fromisoformat(value)
        return parsed.strftime("%d-%m-%Y")
    except ValueError:
        return value


def url_encode_station(value: str) -> str:
    return requests.utils.quote(value, safe="")


def parse_train_identity(text: str) -> tuple[str, str]:
    number_match = re.search(r"\b(?P<number>\d{5,6})\b", text)
    if not number_match:
        return "", ""

    train_number = number_match.group("number")
    tail = text[number_match.end() :]
    name_match = re.search(r"\s+(?P<name>.+?)\s+\d(?:\.\d+)?\s+\d{2}:\d{2}", tail)
    if name_match:
        train_name = normalize_text(name_match.group("name"))
    else:
        train_name = normalize_text(tail.split("Schedule", 1)[0])
        train_name = re.sub(r"\s+\d(?:\.\d+)?$", "", train_name).strip()

    return train_number, train_name


def normalize_availability_status(value: str) -> str:
    normalized = normalize_text(value).upper().replace(" ", "_")
    normalized = normalized.replace("CURR_AVL", "CURR_AVBL")
    if normalized == "CURRENT_AVAILABLE" or normalized.startswith("CURRENT_AVAILABLE_"):
        return "AVAILABLE"
    if normalized.startswith(("WL", "RLWL", "GNWL", "TQWL", "PQWL")):
        return "WL"
    if normalized.startswith("CURR_AVBL"):
        return "CURR_AVBL"
    if normalized.startswith("AVAILABLE") or normalized == "AVL":
        return "AVAILABLE"
    return normalized


def parse_availability_entries(soup: BeautifulSoup) -> list[AvailabilityEntry]:
    """Extract all class/status rows from a page.

    This is intentionally heuristic so it can work against many train-tracking
    pages that expose availability in static HTML.
    """

    confirmtkt_entries = parse_confirmtkt_entries(soup)
    if confirmtkt_entries:
        return dedupe_entries(confirmtkt_entries)

    entries: list[AvailabilityEntry] = []
    availability_pattern = re.compile(
        r"\b(?P<class>1A|2A|3A|SL|CC|EC|2S|FC|AC)\b\s+₹?\d+(?:\.\d+)?\s+(?P<status>CURR_AVL|CURR_AVBL|CURRENT AVAILABLE|(?<!NOT )AVAILABLE|AVL|WL\w*)\b",
        re.I,
    )

    for row in soup.find_all(["tr", "li", "div"]):
        text = normalize_text(row.get_text(" ", strip=True))
        if not text:
            continue
        if not re.search(r"₹|\b\d{5,6}\b|\bSchedule\b", text):
            continue

        train_number, train_name = parse_train_identity(text)
        if not train_number:
            continue

        for match in availability_pattern.finditer(text):
            entries.append(
                AvailabilityEntry(
                    train_number=train_number,
                    train_name=train_name,
                    train_class=match.group("class").upper(),
                    status=normalize_availability_status(match.group("status")),
                    raw_text=text,
                )
            )

    return dedupe_entries(entries)


def parse_confirmtkt_entries(soup: BeautifulSoup) -> list[AvailabilityEntry]:
    entries: list[AvailabilityEntry] = []

    for train_card in soup.find_all(id=re.compile(r"^train-\d+$")):
        title_block = train_card.select_one("div.text-neutral-800")
        if not title_block:
            continue

        title_number = title_block.select_one("span.mr-5")
        if not title_number:
            continue

        train_number = normalize_text(title_number.get_text(" ", strip=True))
        train_name = normalize_text(title_block.get_text(" ", strip=True)).replace(train_number, "", 1).strip()

        if not train_number or not train_name:
            continue

        for availability_container in train_card.select("div[class*='_avail-card']"):
            status_node = availability_container.find(
                string=lambda value: isinstance(value, str)
                and re.search(r"CURR_AVL|AVAILABLE|WL", value, re.I)
            )
            if not status_node:
                continue

            status = normalize_availability_status(status_node)

            class_label = None
            for span in availability_container.find_all("span"):
                candidate = normalize_text(span.get_text(" ", strip=True)).upper()
                if re.fullmatch(r"1A|2A|3A|SL|CC|EC|2S|FC|AC|GN|PQ|TQ|PT|RLWL|TQWL|GNWL", candidate):
                    class_label = candidate
                    break

            if not class_label:
                continue

            entries.append(
                AvailabilityEntry(
                    train_number=train_number,
                    train_name=train_name,
                    train_class=class_label,
                    status=status,
                    raw_text=normalize_text(availability_container.get_text(" ", strip=True)),
                )
            )

    return entries


def dedupe_entries(entries: list[AvailabilityEntry]) -> list[AvailabilityEntry]:
    seen: set[tuple[str, str, str, str]] = set()
    unique_entries: list[AvailabilityEntry] = []
    for entry in entries:
        marker = (entry.train_number, entry.train_name, entry.train_class, entry.status)
        if marker in seen:
            continue
        seen.add(marker)
        unique_entries.append(entry)
    return unique_entries


def discover_trains(session: requests.Session, config: Config) -> list[AvailabilityEntry]:
    url = resolve_tracking_url(config)
    last_error: Optional[Exception] = None

    for attempt in range(1, DEFAULT_MAX_RETRIES + 1):
        try:
            html = load_html(session, config, url)
            soup = BeautifulSoup(html, "html.parser")
            entries = parse_availability_entries(soup)
            return entries
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < DEFAULT_MAX_RETRIES:
                time.sleep(BACKOFF_SECONDS[attempt - 1])
                continue
            raise RuntimeError("failed to fetch availability after retries") from last_error

    raise RuntimeError("failed to fetch availability")


def filter_candidates(entries: list[AvailabilityEntry], config: Config) -> list[AvailabilityEntry]:
    train_number = config.train_number.strip().upper()
    travel_class = config.travel_class.strip().upper()
    return [
        entry
        for entry in entries
        if (train_number in {"", "NA", "ALL"} or entry.train_number == train_number)
        and (not travel_class or entry.train_class == travel_class)
    ]


def is_cutoff_reached(config: Config, now: Optional[datetime] = None) -> bool:
    return bool(config.cutoff_at and (now or datetime.now()) >= config.cutoff_at)


def fetch_availability(session: requests.Session, config: Config) -> list[AvailabilityEntry]:
    return filter_candidates(discover_trains(session, config), config)


def load_html(session: requests.Session, config: Config, url: str) -> str:
    if config.use_browser_rendering and sync_playwright is not None:
        try:
            return load_html_with_browser(url, config)
        except Exception as exc:
            log_line(f"Browser render failed, falling back to requests: {exc}")

    response = session.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=config.timeout_seconds,
    )
    if response.status_code in (429, 503):
        response.raise_for_status()
    response.raise_for_status()
    return response.text


def load_html_with_browser(url: str, config: Config) -> str:
    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": 1440, "height": 2200},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=config.timeout_seconds * 1000)
            page.wait_for_timeout(config.browser_wait_seconds * 1000)
            page.wait_for_load_state("networkidle", timeout=config.timeout_seconds * 1000)
        except Exception:
            page.wait_for_timeout(config.browser_wait_seconds * 1000)
        html = page.content()
        context.close()
        browser.close()
        return html


def state_signature(config: Config, entries: list[AvailabilityEntry]) -> str:
    actionable_entries = [
        entry for entry in entries if entry.status in {"AVAILABLE", "CURR_AVBL"}
    ]
    payload = {
        "train_number": config.train_number,
        "source_station": config.source_station,
        "destination_station": config.destination_station,
        "travel_date": config.travel_date,
        "travel_class": config.travel_class,
        "quota": config.quota,
        "entries": [dataclasses.asdict(entry) for entry in actionable_entries],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return digest


def read_last_alert(state_file: Path) -> Optional[str]:
    if not state_file.exists():
        return None
    content = state_file.read_text(encoding="utf-8").strip()
    return content or None


def write_last_alert(state_file: Path, signature: str) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(signature + "\n", encoding="utf-8")


def clear_last_alert(state_file: Path) -> None:
    if state_file.exists():
        state_file.unlink()


def alert_condition_met(entries: list[AvailabilityEntry]) -> bool:
    return any(entry.status in {"AVAILABLE", "CURR_AVBL"} for entry in entries)


def summarize_entries(entries: list[AvailabilityEntry]) -> str:
    parts = []
    for entry in entries:
        train_label = f"{entry.train_name} {entry.train_number}".strip()
        parts.append(f"{train_label} - {entry.train_class} ({entry.status})")
    return "; ".join(parts)


def send_pushover_alert(session: requests.Session, config: Config, entries: list[AvailabilityEntry]) -> None:
    if not config.pushover_user_key or not config.pushover_app_token:
        raise RuntimeError(
            "Pushover credentials are missing; set PUSHOVER_USER_KEY and PUSHOVER_APP_TOKEN"
        )

    message = (
        f"Train {config.train_number} {config.source_station}->{config.destination_station} "
        f"{config.travel_date} {config.travel_class}/{config.quota}: {summarize_entries(entries)}"
    )

    payload = {
        "token": config.pushover_app_token,
        "user": config.pushover_user_key,
        "message": message,
        "title": "IRCTC Availability Alert",
        "priority": "1",
        "retry": "30",
        "expire": "3600",
    }
    if config.pushover_device:
        payload["device"] = config.pushover_device

    response = session.post("https://api.pushover.net/1/messages.json", data=payload, timeout=config.timeout_seconds)
    response.raise_for_status()


def log_line(message: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def run(config: Config) -> int:
    session = requests.Session()

    while True:
        if is_cutoff_reached(config):
            log_line("Cutoff reached; stopping monitor.")
            return 0
        entries = fetch_availability(session, config)
        if alert_condition_met(entries):
            log_line(f"Parsed availability: {summarize_entries(entries)}")
            signature = state_signature(config, entries)
            last_alert = read_last_alert(config.state_file)
            if last_alert == signature:
                log_line("Duplicate alert suppressed by local state file.")
            else:
                send_pushover_alert(session, config, entries)
                write_last_alert(config.state_file, signature)
                log_line("Alert sent and state updated.")
        else:
            log_line("No current availability rows found.")
            clear_last_alert(config.state_file)

        time.sleep(config.poll_interval_seconds)


def main() -> int:
    try:
        args = parse_args()
        config = build_config(args)
        return run(config)
    except KeyboardInterrupt:
        log_line("Interrupted by user; exiting.")
        return 130
    except Exception as exc:
        log_line(f"Fatal error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())