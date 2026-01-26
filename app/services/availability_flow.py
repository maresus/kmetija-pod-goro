from __future__ import annotations

import re
from typing import Any, Optional

from app.services.parsing import (
    extract_date,
    extract_date_range,
    extract_nights,
    extract_time,
    nights_from_range,
    parse_people_count,
)


def _blank_availability_state() -> dict[str, Optional[str | int | bool]]:
    return {
        "active": False,
        "type": None,
        "date": None,
        "time": None,
        "people": None,
        "nights": None,
        "location": None,
        "awaiting": None,
        "can_reserve": False,
    }


def get_availability_state(state: dict[str, Any]) -> dict[str, Any]:
    if not state.get("availability"):
        state["availability"] = _blank_availability_state()
    return state["availability"]


def reset_availability_state(state: dict[str, Any]) -> None:
    state["availability"] = _blank_availability_state()


def _contains_any(text: str, tokens: list[str]) -> bool:
    return any(tok in text for tok in tokens)


def detect_availability_type(message: str) -> Optional[str]:
    lowered = message.lower()
    room_tokens = ["soba", "sobo", "sobe", "nocitev", "preno", "room", "zimmer"]
    table_tokens = ["miza", "mizo", "mize", "kosilo", "vecerja", "table", "tisch"]
    has_room = _contains_any(lowered, room_tokens)
    has_table = _contains_any(lowered, table_tokens)
    if has_room and has_table:
        return None
    if has_room:
        return "room"
    if has_table:
        return "table"
    return None


def is_availability_query(message: str) -> bool:
    lowered = message.lower()
    trigger_tokens = [
        "prosto",
        "prosta",
        "proste",
        "razpoloz",
        "razpolo",
        "na voljo",
        "zaseden",
        "zasedeno",
        "termin",
        "datum",
    ]
    if not _contains_any(lowered, trigger_tokens):
        return False
    return True


def _availability_prompt_missing_type() -> str:
    return "Zelite preveriti prosto sobo ali mizo?"


def _availability_prompt_missing_date() -> str:
    return "Za kateri datum zelite preveriti razpolozljivost? (DD.MM.YYYY)"


def _availability_prompt_missing_people(res_type: str) -> str:
    if res_type == "table":
        return "Za koliko oseb zelite preveriti mizo?"
    return "Za koliko oseb zelite preveriti prosto sobo?"


def _availability_prompt_missing_nights() -> str:
    return "Za koliko nocitev zelite preveriti?"


def _availability_prompt_missing_time() -> str:
    return "Ob kateri uri zelite mizo? (12:00-20:00, zadnji prihod 15:00)"


def start_reservation_from_availability(
    state: dict[str, Any],
    reservation_service: Any,
    reset_reservation_state: Any,
    handle_reservation_flow: Any,
    reset_availability_state_fn: Any,
) -> Optional[str]:
    availability_state = get_availability_state(state)
    if not availability_state.get("can_reserve"):
        return None
    res_type = availability_state.get("type")
    snapshot = {
        "type": availability_state.get("type"),
        "date": availability_state.get("date"),
        "time": availability_state.get("time"),
        "people": availability_state.get("people"),
        "nights": availability_state.get("nights"),
        "location": availability_state.get("location"),
        "language": state.get("language"),
        "session_id": state.get("session_id"),
    }
    reset_reservation_state(state)
    if snapshot.get("language"):
        state["language"] = snapshot.get("language")
    if snapshot.get("session_id"):
        state["session_id"] = snapshot.get("session_id")
    if res_type == "room":
        state["type"] = "room"
        state["date"] = snapshot.get("date")
        state["nights"] = snapshot.get("nights")
        state["people"] = snapshot.get("people")
        reset_availability_state_fn(state)
        return handle_reservation_flow("rezervacija sobe", state)
    if res_type == "table":
        date = snapshot.get("date") or ""
        time_val = snapshot.get("time") or ""
        people = snapshot.get("people") or 0
        ok, error_message = reservation_service.validate_table_rules(date, time_val)
        if not ok:
            reset_availability_state_fn(state)
            return error_message
        available, location, suggestions = reservation_service.check_table_availability(
            date, time_val, int(people)
        )
        if not available:
            reset_availability_state_fn(state)
            if suggestions:
                return "Izbran termin je zaseden. Predlagani prosti termini: " + "; ".join(suggestions) + "."
            return "Izbran termin je zaseden. Zelite drug datum ali uro?"
        state["type"] = "table"
        state["date"] = date
        state["time"] = time_val
        state["people"] = int(people)
        state["adults"] = None
        state["kids"] = None
        state["kids_ages"] = None
        state["location"] = location or "Jedilnica (dodelimo ob prihodu)"
        state["step"] = "awaiting_kids_info"
        reset_availability_state_fn(state)
        return (
            f"Odlicno, mizo lahko rezerviram za {date} ob {time_val}. "
            "Imate otroke? Koliko in koliko so stari?"
        )
    return None


def handle_availability_query(
    message: str,
    state: dict[str, Any],
    reservation_service: Any,
    force: bool = False,
) -> Optional[str]:
    if not force and not is_availability_query(message):
        return None

    availability_state = get_availability_state(state)
    availability_state["active"] = True

    res_type = detect_availability_type(message) or availability_state.get("type")
    if res_type:
        availability_state["type"] = res_type
    else:
        availability_state["awaiting"] = "type"
        availability_state["can_reserve"] = False
        return _availability_prompt_missing_type()

    date = extract_date(message) or availability_state.get("date")
    date_range = extract_date_range(message)
    nights = extract_nights(message) or availability_state.get("nights")
    if res_type == "room" and date_range:
        date = date_range[0]
        nights = nights_from_range(*date_range)

    if date:
        availability_state["date"] = date
    else:
        availability_state["awaiting"] = "date"
        availability_state["can_reserve"] = False
        return _availability_prompt_missing_date()

    awaiting = availability_state.get("awaiting")
    if awaiting in {"date", "time"} and availability_state.get("people"):
        people = availability_state.get("people")
    else:
        people = parse_people_count(message).get("total") or availability_state.get("people")
    if people:
        availability_state["people"] = int(people)
    else:
        availability_state["awaiting"] = "people"
        availability_state["can_reserve"] = False
        return _availability_prompt_missing_people(res_type)

    if res_type == "room":
        if not nights:
            availability_state["awaiting"] = "nights"
            availability_state["can_reserve"] = False
            return _availability_prompt_missing_nights()
        availability_state["nights"] = int(nights)
        available, alternative = reservation_service.check_room_availability(
            availability_state["date"], availability_state["nights"], availability_state["people"]
        )
        availability_state["awaiting"] = None
        availability_state["can_reserve"] = available
        availability_state["location"] = None
        if not available:
            if alternative:
                return (
                    "Zal je termin zaseden. Najblizji prost termin je "
                    f"{alternative}. Zelite, da preverim ta datum ali pripravim rezervacijo?"
                )
            return "Zal je termin zaseden. Zelite drug datum ali manjse stevilo oseb?"
        return (
            f"Da, soba je prosta {availability_state['date']} za {availability_state['people']} oseb "
            f"(za {availability_state['nights']} nocitev). Zelite, da pripravim rezervacijo?"
        )

    if res_type == "table":
        cleaned = re.sub(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b", " ", message)
        time_val = extract_time(cleaned) or availability_state.get("time")
        if not time_val:
            availability_state["awaiting"] = "time"
            availability_state["can_reserve"] = False
            return _availability_prompt_missing_time()
        availability_state["time"] = time_val
        ok, error_message = reservation_service.validate_table_rules(date, time_val)
        if not ok:
            availability_state["can_reserve"] = False
            lower_error = error_message.lower()
            if any(token in lower_error for token in ["uro", "ura", "zadnji prihod", "kuhinja"]):
                availability_state["awaiting"] = "time"
            else:
                availability_state["awaiting"] = "date"
            if availability_state["awaiting"] == "date" and "dd.mm" not in lower_error:
                return error_message + " Prosimo poslji datum sobote ali nedelje (DD.MM.YYYY)."
            return error_message
        available, location, suggestions = reservation_service.check_table_availability(
            date, time_val, availability_state["people"]
        )
        availability_state["awaiting"] = None
        availability_state["can_reserve"] = available
        availability_state["location"] = location
        if not available:
            if suggestions:
                return (
                    "Izbran termin je zaseden. Predlagani prosti termini: "
                    + "; ".join(suggestions)
                    + ". Zelite, da rezerviram enega od njih?"
                )
            return "Izbran termin je zaseden. Zelite drug datum ali uro?"
        location_text = f" ({location})" if location else ""
        return (
            f"Da, miza je prosta {date} ob {time_val} za {availability_state['people']} oseb{location_text}. "
            "Zelite, da pripravim rezervacijo?"
        )

    return None


def handle_availability_followup(
    message: str,
    state: dict[str, Any],
    reservation_service: Any,
    is_affirmative: Any,
    is_negative: Any,
    exit_keywords: list[str],
) -> Optional[str]:
    availability_state = get_availability_state(state)
    if not availability_state.get("active"):
        return None
    if any(word in message.lower() for word in exit_keywords):
        reset_availability_state(state)
        return "V redu, prekinjam preverjanje. Kako vam lahko pomagam?"
    if availability_state.get("awaiting"):
        if is_negative(message):
            reset_availability_state(state)
            return "V redu. Kako vam lahko se pomagam?"
        return handle_availability_query(message, state, reservation_service, force=True)
    if not is_affirmative(message) and not is_negative(message):
        has_update = (
            extract_date(message)
            or extract_date_range(message)
            or extract_time(message)
            or parse_people_count(message).get("total")
            or detect_availability_type(message)
            or is_availability_query(message)
        )
        if has_update:
            return handle_availability_query(message, state, reservation_service, force=True)
    return None
