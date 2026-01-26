from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional, Tuple

from app.services.parsing import (
    extract_date,
    extract_date_from_text,
    extract_date_range,
    extract_nights,
    extract_time,
    nights_from_range,
    parse_kids_response,
    parse_people_count,
)


def _blank_reservation_state_fallback() -> dict[str, Optional[str | int]]:
    return {
        "step": None,
        "type": None,
        "date": None,
        "time": None,
        "nights": None,
        "rooms": None,
        "people": None,
        "adults": None,
        "kids": None,
        "kids_ages": None,
        "name": None,
        "phone": None,
        "email": None,
        "location": None,
        "available_locations": None,
        "language": None,
        "dinner_people": None,
        "note": None,
        "availability": None,
    }


def reset_reservation_state(state: dict[str, Optional[str | int]]) -> None:
    state.clear()
    state.update(_blank_reservation_state_fallback())


def get_booking_continuation(step: str, state: dict) -> str:
    """Vrne navodilo za nadaljevanje glede na trenutni korak."""
    continuations = {
        "awaiting_date": "Za kateri **datum** bi rezervirali?",
        "awaiting_nights": "Koliko **nočitev**?",
        "awaiting_people": "Za koliko **oseb**?",
        "awaiting_kids": "Koliko je **otrok** in koliko so stari?",
        "awaiting_kids_info": "Koliko je **otrok** in koliko so stari?",
        "awaiting_kids_ages": "Koliko so stari **otroci**?",
        "awaiting_room_location": "Katero **sobo** želite? (ALJAŽ, JULIJA, ANA)",
        "awaiting_name": "Vaše **ime in priimek**?",
        "awaiting_phone": "Vaša **telefonska številka**?",
        "awaiting_email": "Vaš **e-mail**?",
        "awaiting_dinner": "Želite **večerje**? (Da/Ne)",
        "awaiting_dinner_count": "Za koliko oseb želite **večerje**?",
        "awaiting_note": "Želite še kaj **sporočiti**? (ali 'ne')",
        "awaiting_time": "Ob kateri **uri**?",
        "awaiting_table_date": "Za kateri **datum** bi rezervirali mizo?",
        "awaiting_table_time": "Ob kateri **uri** bi prišli?",
        "awaiting_table_people": "Za koliko **oseb**?",
        "awaiting_table_location": "Katero **jedilnico** želite? (Pri peči / Pri vrtu)",
        "awaiting_table_event_type": "Kakšen je **tip dogodka**?",
        "awaiting_confirmation": "Potrdite rezervacijo? (da/ne)",
    }
    return continuations.get(step or "", "Lahko nadaljujemo z rezervacijo?")


def reservation_prompt_for_state(
    state: dict[str, Optional[str | int]],
    room_intro_text: Any,
    table_intro_text: Any,
) -> str:
    step = state.get("step")
    res_type = state.get("type")
    if res_type == "table":
        if step == "awaiting_table_date":
            return "Prosim za datum (sobota/nedelja) v obliki DD.MM ali DD.MM.YYYY."
        if step == "awaiting_table_time":
            return "Ob kateri uri bi želeli mizo? (12:00–20:00, zadnji prihod na kosilo 15:00)"
        if step == "awaiting_table_people":
            return "Za koliko oseb pripravimo mizo?"
        if step == "awaiting_table_location":
            return "Izberi prostor: Pri peči ali Pri vrtu?"
    else:
        if step == "awaiting_room_date":
            return "Za kateri datum prihoda? (DD.MM ali DD.MM.YYYY)"
        if step == "awaiting_nights":
            return "Koliko nočitev načrtujete? (min. 3 v jun/jul/avg, sicer 2)"
        if step == "awaiting_people":
            return "Za koliko oseb bi bilo bivanje (odrasli + otroci)?"
        if step == "awaiting_room_location":
            return "Katero sobo želite (ALJAŽ, JULIJA, ANA)?"
    return (
        f"Sobe: {room_intro_text()}\n"
        f"Mize: {table_intro_text()}"
    )


def validate_reservation_rules(
    arrival_date_str: str,
    nights: int,
    reservation_service: Any,
) -> Tuple[bool, str, str]:
    cleaned_date = arrival_date_str.strip()
    try:
        datetime.strptime(cleaned_date, "%d.%m.%Y")
    except ValueError:
        return False, "Tega datuma ne razumem. Prosimo uporabite DD.MM ali DD.MM.YYYY (npr. 12.7. ali 12.7.2025).", "date"

    if nights <= 0:
        return False, "Število nočitev mora biti pozitivno. Poskusite znova.", "nights"

    ok, message = reservation_service.validate_room_rules(cleaned_date, nights)
    if not ok:
        # vsako pravilo za sobe zahteva ponovni vnos datuma/nočitev -> vrnemo tip "date" za reset datuma
        return False, message, "date"

    return True, "", ""


def advance_after_room_people(reservation_state: dict[str, Optional[str | int]], reservation_service: Any) -> str:
    """Premakne flow po tem, ko poznamo število oseb."""
    people_val = int(reservation_state.get("people") or 0)
    reservation_state["rooms"] = max(1, (people_val + 3) // 4)
    available, alternative = reservation_service.check_room_availability(
        reservation_state["date"] or "",
        reservation_state["nights"] or 0,
        people_val,
        reservation_state["rooms"],
    )
    if not available:
        reservation_state["step"] = "awaiting_room_date"
        free_now = reservation_service.available_rooms(
            reservation_state["date"] or "",
            reservation_state["nights"] or 0,
        )
        free_text = ""
        if free_now:
            free_text = f" Trenutno so na ta termin proste: {', '.join(free_now)} (vsaka 2+2)."
        suggestion = (
            f"Najbližji prost termin je {alternative}. Sporočite, ali vam ustreza, ali podajte drug datum."
            if alternative
            else "Prosim izberite drug datum ali manjšo skupino."
        )
        return f"V izbranem terminu nimamo dovolj prostih sob.{free_text} {suggestion}"
    # ponudi izbiro sobe, če je več prostih
    free_rooms = reservation_service.available_rooms(
        reservation_state["date"] or "",
        reservation_state["nights"] or 0,
    )
    needed = reservation_state["rooms"] or 1
    if free_rooms and len(free_rooms) > needed:
        reservation_state["available_locations"] = free_rooms
        reservation_state["step"] = "awaiting_room_location"
        names = ", ".join(free_rooms)
        return f"Proste imamo: {names}. Katero bi želeli (lahko tudi več, npr. 'ALJAZ in ANA')?"
    # auto-assign
    if free_rooms:
        chosen = free_rooms[:needed]
        reservation_state["location"] = ", ".join(chosen)
    else:
        reservation_state["location"] = "Sobe (dodelimo ob potrditvi)"
    reservation_state["step"] = "awaiting_name"
    return "Odlično. Kako se glasi ime in priimek nosilca rezervacije?"


def proceed_after_table_people(reservation_state: dict[str, Optional[str | int]], reservation_service: Any) -> str:
    people = int(reservation_state.get("people") or 0)
    available, location, suggestions = reservation_service.check_table_availability(
        reservation_state["date"] or "",
        reservation_state["time"] or "",
        people,
    )
    if not available:
        reservation_state["step"] = "awaiting_table_time"
        alt = (
            "Predlagani prosti termini: " + "; ".join(suggestions)
            if suggestions
            else "Prosim izberite drugo uro ali enega od naslednjih vikendov."
        )
        return f"Izbran termin je zaseden. {alt}"
    # če imamo lokacijo že izbranega prostora
    if location:
        reservation_state["location"] = location
        reservation_state["step"] = "awaiting_name"
        return f"Lokacija: {location}. Odlično. Prosim še ime in priimek nosilca rezervacije."

    # če ni vnaprej dodelil, ponudimo izbiro med razpoložljivimi
    possible = []
    occupancy = reservation_service._table_room_occupancy()
    norm_time = reservation_service._parse_time(reservation_state["time"] or "")
    for room in ["Jedilnica Pri peči", "Jedilnica Pri vrtu"]:
        used = occupancy.get((reservation_state["date"], norm_time, room), 0)
        cap = 15 if "peč" in room.lower() else 35
        if used + people <= cap:
            possible.append(room)
    if len(possible) <= 1:
        reservation_state["location"] = possible[0] if possible else "Jedilnica (dodelimo ob prihodu)"
        reservation_state["step"] = "awaiting_name"
        return "Odlično. Prosim še ime in priimek nosilca rezervacije."
    reservation_state["available_locations"] = possible
    reservation_state["step"] = "awaiting_table_location"
    return "Imamo prosto v: " + " ali ".join(possible) + ". Kje bi želeli sedeti?"


def _handle_room_reservation_impl(
    message: str,
    state: dict[str, Optional[str | int]],
    reservation_service: Any,
    is_affirmative: Any,
    validate_reservation_rules_fn: Any,
    advance_after_room_people_fn: Any,
    reset_reservation_state: Any,
    send_reservation_emails_async: Any,
    reservation_pending_message: str,
) -> str:
    reservation_state = state
    step = reservation_state["step"]

    if step == "awaiting_room_date":
        range_data = extract_date_range(message)
        if range_data:
            reservation_state["date"] = range_data[0]
            nights_candidate = nights_from_range(range_data[0], range_data[1])
            if nights_candidate:
                ok, error_message, _ = validate_reservation_rules_fn(
                    reservation_state["date"] or "", nights_candidate
                )
                if not ok:
                    reservation_state["step"] = "awaiting_room_date"
                    reservation_state["date"] = None
                    reservation_state["nights"] = None
                    return (
                        error_message
                        + " Prosim pošlji nov datum in št. nočitev skupaj (npr. 15.7. ali 15.7.2025 za 3 nočitve)."
                    )
                reservation_state["nights"] = nights_candidate
                reservation_state["step"] = "awaiting_people"
                return (
                    f"Odlično, zabeležila sem {reservation_state['date']} za {reservation_state['nights']} nočitev. "
                    "Za koliko oseb bi bilo bivanje (odrasli + otroci)?"
                )
        date_candidate = extract_date(message)
        nights_candidate = extract_nights(message)
        if not date_candidate:
            reservation_state["date"] = None
            return "Z veseljem uredim sobo. 😊 Sporočite datum prihoda (DD.MM ali DD.MM.YYYY) in približno število nočitev?"
        if not nights_candidate:
            reservation_state["date"] = date_candidate
            reservation_state["nights"] = None
            reservation_state["step"] = "awaiting_nights"
            return "Hvala! Koliko nočitev načrtujete?"
        ok, error_message, error_type = validate_reservation_rules_fn(date_candidate, nights_candidate)
        if not ok:
            if error_type == "date":
                reservation_state["date"] = None
                reservation_state["nights"] = None
                return error_message + " Prosim pošljite nov datum prihoda (DD.MM ali DD.MM.YYYY)."
            reservation_state["nights"] = None
            return error_message + " Prosim pošljite število nočitev."
        reservation_state["date"] = date_candidate
        reservation_state["nights"] = nights_candidate
        reservation_state["step"] = "awaiting_people"
        return (
            f"Super, zabeležila sem {reservation_state['date']} za {reservation_state['nights']} nočitev. "
            "Za koliko oseb bi bilo bivanje?"
        )

    if step == "awaiting_nights":
        new_nights = extract_nights(message)
        if not new_nights:
            return "Prosimo navedite število nočitev (npr. '2 nočitvi')."
        ok, error_message, _ = validate_reservation_rules_fn(reservation_state.get("date") or "", new_nights)
        if not ok:
            reservation_state["nights"] = None
            return error_message + " Poskusite z drugim številom nočitev."
        reservation_state["nights"] = new_nights
        if reservation_state.get("people"):
            return advance_after_room_people_fn(reservation_state, reservation_service)
        reservation_state["step"] = "awaiting_people"
        return "Za koliko oseb bi bilo bivanje (odrasli + otroci)?"

    if step == "awaiting_people":
        parsed = parse_people_count(message)
        total = parsed["total"]
        if total is None or total <= 0:
            return "Koliko vas bo? (npr. '2 odrasla in 1 otrok' ali '3 osebe')"
        if total > 12:
            return "Na voljo so tri sobe (vsaka 2+2). Za več kot 12 oseb nas prosim kontaktirajte na email."
        reservation_state["people"] = total
        reservation_state["adults"] = parsed["adults"]
        reservation_state["kids"] = parsed["kids"]
        reservation_state["kids_ages"] = parsed["ages"]
        if parsed["kids"] is None and parsed["adults"] is None:
            reservation_state["step"] = "awaiting_kids_info"
            return "Imate otroke? Koliko in koliko so stari?"
        if parsed["kids"] and not parsed["ages"]:
            reservation_state["step"] = "awaiting_kids_ages"
            return "Koliko so stari otroci?"
        return advance_after_room_people_fn(reservation_state, reservation_service)

    if step == "awaiting_kids_info":
        text = message.lower().strip()
        if any(word in text for word in ["ne", "brez", "ni", "nimam"]):
            reservation_state["kids"] = 0
            reservation_state["kids_ages"] = ""
            return advance_after_room_people_fn(reservation_state, reservation_service)
        if is_affirmative(text):
            return "Koliko otrok?"
        kids_parsed = parse_kids_response(message)
        if kids_parsed["kids"] is not None:
            reservation_state["kids"] = kids_parsed["kids"]
        if kids_parsed["ages"]:
            reservation_state["kids_ages"] = kids_parsed["ages"]
        if reservation_state.get("kids") and not reservation_state.get("kids_ages"):
            reservation_state["step"] = "awaiting_kids_ages"
            return "Koliko so stari otroci?"
        return advance_after_room_people_fn(reservation_state, reservation_service)

    if step == "awaiting_kids_ages":
        reservation_state["kids_ages"] = message.strip()
        return advance_after_room_people_fn(reservation_state, reservation_service)

    if step == "awaiting_room_location":
        options = reservation_state.get("available_locations") or []
        if not options:
            reservation_state["step"] = "awaiting_name"
            return "Nadaljujmo. Prosim še ime in priimek nosilca rezervacije."

        def normalize(text: str) -> str:
            return (
                text.lower()
                .replace("š", "s")
                .replace("ž", "z")
                .replace("č", "c")
                .replace("ć", "c")
            )

        input_norm = normalize(message)
        selected = []
        any_keywords = {"vseeno", "vseen", "vseeni", "katerakoli", "katerakol", "karkoli", "any"}
        for opt in options:
            opt_norm = normalize(opt)
            if opt_norm in input_norm or input_norm == opt_norm:
                selected.append(opt)
        if input_norm.strip() in any_keywords and not selected:
            selected = options[:]
        if not selected:
            return "Prosim izberite med: " + ", ".join(options)
        needed = reservation_state.get("rooms") or 1
        if len(selected) < needed:
            for opt in options:
                if opt not in selected and len(selected) < needed:
                    selected.append(opt)
        reservation_state["location"] = ", ".join(selected[:needed])
        reservation_state["step"] = "awaiting_name"
        return f"Zabeleženo: {reservation_state['location']}. Prosim še ime in priimek nosilca rezervacije."

    if step == "awaiting_name":
        full_name = message.strip()
        if len(full_name.split()) < 2:
            return "Prosim napišite ime in priimek (npr. 'Nika Kmetija Pod Goro')."
        reservation_state["name"] = full_name
        reservation_state["step"] = "awaiting_phone"
        return "Hvala! Zdaj prosim še telefonsko številko."

    if step == "awaiting_phone":
        phone = message.strip()
        digits = re.sub(r"\D+", "", phone)
        if len(digits) < 7:
            return "Zaznal sem premalo številk. Prosimo vpišite veljavno telefonsko številko."
        reservation_state["phone"] = phone
        reservation_state["step"] = "awaiting_email"
        return "Kam naj pošljem povzetek ponudbe? (e-poštni naslov)"

    if step == "awaiting_email":
        email = message.strip()
        if "@" not in email or "." not in email:
            return "Prosim vpišite veljaven e-poštni naslov (npr. info@primer.si)."
        reservation_state["email"] = email
        reservation_state["step"] = "awaiting_dinner"
        return (
            "Želite ob bivanju tudi večerje? (25€/oseba, vključuje juho, glavno jed in sladico)\n"
            "Odgovorite Da ali Ne."
        )

    if step == "awaiting_dinner":
        answer = message.strip().lower()
        positive = {
            "da",
            "ja",
            "seveda",
            "zelim",
            "želim",
            "hocem",
            "hočem",
            "polpenzion",
            "pol penzion",
            "pol-penzion",
        }
        negative = {"ne", "no", "nocem", "nočem", "brez"}

        def dinner_warning() -> Optional[str]:
            arrival = reservation_service._parse_date(reservation_state.get("date") or "")
            nights = int(reservation_state.get("nights") or 1)
            if not arrival:
                return None
            for offset in range(max(1, nights)):
                day = (arrival + timedelta(days=offset)).weekday()
                if day in {0, 1}:
                    return "Opozorilo: večerje ob ponedeljkih in torkih ne strežemo."
            return None

        warn = dinner_warning()
        if any(word in answer for word in positive):
            reservation_state["step"] = "awaiting_dinner_count"
            follow = "Za koliko oseb želite večerje?"
            if warn:
                follow = warn + " " + follow
            return follow
        if any(word in answer for word in negative):
            reservation_state["dinner_people"] = 0
            reservation_state["step"] = "awaiting_note"
            return "Želite še kaj sporočiti? (posebne želje, alergije, praznovanje...)"
        return "Prosim odgovorite z Da ali Ne glede na večerje."

    if step == "awaiting_dinner_count":
        digits = re.findall(r"\d+", message)
        if not digits:
            return "Prosim povejte za koliko oseb želite večerje (število)."
        count = int(digits[0])
        reservation_state["dinner_people"] = count
        reservation_state["step"] = "awaiting_note"
        return "Želite še kaj sporočiti? (posebne želje, alergije, praznovanje...)"

    if step == "awaiting_note":
        skip_words = {"ne", "nic", "nič", "nimam", "brez"}
        note_text = "" if any(word in message.lower() for word in skip_words) else message.strip()
        reservation_state["note"] = note_text
        reservation_state["step"] = "awaiting_confirmation"
        chosen_location = reservation_state.get("location") or "Sobe (dodelimo ob potrditvi)"
        dinner_note = (
            f"Večerje: {reservation_state.get('dinner_people')} oseb (25€/oseba)"
            if reservation_state.get("dinner_people")
            else "Večerje: ne"
        )
        lines = [
            "Prosimo, preverite podatke:",
            f"📅 Datum: {reservation_state.get('date')}, {reservation_state.get('nights')} noči",
            f"👥 Osebe: {reservation_state.get('people')}",
            f"🛏️ Soba: {chosen_location}",
            f"👤 Ime: {reservation_state.get('name')}",
            f"📞 Telefon: {reservation_state.get('phone')}",
            f"📧 Email: {reservation_state.get('email')}",
            f"🍽️ {dinner_note}",
        ]
        if note_text:
            lines.append(f"📝 Opombe: {note_text}")
        lines.append("Potrdite rezervacijo? (da/ne)")
        return "\n".join(lines)

    if step == "awaiting_confirmation":
        if message.strip().lower() in {"ne", "no"}:
            reset_reservation_state(state)
            return "V redu, rezervacijo sem preklical. Kako vam lahko pomagam?"
        if is_affirmative(message):
            summary_state = reservation_state.copy()
            dinner_note = ""
            if reservation_state.get("dinner_people"):
                dinner_note = f"Večerje: {reservation_state.get('dinner_people')} oseb (25€/oseba)"
            chosen_location = reservation_state.get("location") or "Sobe (dodelimo ob potrditvi)"
            res_id = reservation_service.create_reservation(
                date=reservation_state["date"] or "",
                people=int(reservation_state["people"] or 0),
                reservation_type="room",
                source="chat",
                nights=int(reservation_state["nights"] or 0),
                rooms=int(reservation_state["rooms"] or 0),
                name=str(reservation_state["name"]),
                phone=str(reservation_state["phone"]),
                email=reservation_state["email"],
                location=chosen_location,
                note=(reservation_state.get("note") or "") or dinner_note,
                kids=str(reservation_state.get("kids") or ""),
                kids_small=str(reservation_state.get("kids_ages") or ""),
            )
            email_data = {
                "id": res_id,
                "name": reservation_state.get("name", ""),
                "email": reservation_state.get("email", ""),
                "phone": reservation_state.get("phone", ""),
                "date": reservation_state.get("date", ""),
                "nights": reservation_state.get("nights", 0),
                "rooms": reservation_state.get("rooms", 0),
                "people": reservation_state.get("people", 0),
                "reservation_type": "room",
                "location": chosen_location,
                "note": (reservation_state.get("note") or "") or dinner_note,
                "kids": reservation_state.get("kids", ""),
                "kids_ages": reservation_state.get("kids_ages", ""),
            }
            session_id = reservation_state.get("session_id")
            if session_id:
                reservation_service.log_conversation(
                    session_id=session_id,
                    user_message="(auto) reservation completed",
                    bot_response="(auto) reservation completed",
                    intent="reservation_completed",
                    needs_followup=False,
                )
            send_reservation_emails_async(email_data)
            reset_reservation_state(state)
            lines = [
                "Odlično! 😊 Vaša rezervacija je zabeležena:",
                f"📅 Datum: {summary_state.get('date')}, {summary_state.get('nights')} noči",
                f"👥 Osebe: {summary_state.get('people')}",
                f"🛏️ Soba: {chosen_location}",
            ]
            if dinner_note:
                lines.append(f"🍽️ {dinner_note}")
            if summary_state.get("note"):
                lines.append(f"📝 Opombe: {summary_state.get('note')}")
            lines.append(reservation_pending_message.strip())
            return "\n".join([line for line in lines if line])
        return "Prosim potrdite z 'da' ali 'ne'."

    return "Nadaljujmo z rezervacijo sobe. Za kateri datum jo želite?"


def handle_room_reservation(
    message: str,
    state: dict[str, Optional[str | int]],
    translate_response: Any,
    reservation_service: Any,
    is_affirmative: Any,
    validate_reservation_rules_fn: Any,
    advance_after_room_people_fn: Any,
    reset_reservation_state: Any,
    send_reservation_emails_async: Any,
    reservation_pending_message: str,
) -> str:
    response = _handle_room_reservation_impl(
        message,
        state,
        reservation_service,
        is_affirmative,
        validate_reservation_rules_fn,
        advance_after_room_people_fn,
        reset_reservation_state,
        send_reservation_emails_async,
        reservation_pending_message,
    )
    lang = state.get("language", "si")
    return translate_response(response, lang)


def _handle_table_reservation_impl(
    message: str,
    state: dict[str, Optional[str | int]],
    reservation_service: Any,
    reset_reservation_state: Any,
    is_affirmative: Any,
    send_reservation_emails_async: Any,
    reservation_pending_message: str,
) -> str:
    reservation_state = state
    step = reservation_state["step"]

    if step == "awaiting_table_date":
        proposed = extract_date(message) or ""
        if not proposed:
            return "Za kateri datum (sobota/nedelja)? (DD.MM ali DD.MM.YYYY)"
        ok, error_message = reservation_service.validate_table_rules(proposed, "12:00")
        if not ok:
            reservation_state["date"] = None
            return error_message + " Bi poslali datum sobote ali nedelje v obliki DD.MM ali DD.MM.YYYY?"
        reservation_state["date"] = proposed
        reservation_state["step"] = "awaiting_table_time"
        return "Ob kateri uri bi želeli mizo? (12:00–20:00, zadnji prihod na kosilo 15:00)"

    if step == "awaiting_table_time":
        desired_time = extract_time(message) or message.strip()
        ok, error_message = reservation_service.validate_table_rules(
            reservation_state["date"] or "", desired_time
        )
        if not ok:
            reservation_state["step"] = "awaiting_table_date"
            reservation_state["date"] = None
            reservation_state["time"] = None
            return error_message + " Poskusiva z novim datumom (sobota/nedelja, DD.MM ali DD.MM.YYYY)."
        reservation_state["time"] = reservation_service._parse_time(desired_time)
        if not reservation_state.get("people"):
            parsed = parse_people_count(message)
            people = parsed["total"]
            if people:
                reservation_state["people"] = people
                reservation_state["adults"] = parsed["adults"]
                reservation_state["kids"] = parsed["kids"]
                reservation_state["kids_ages"] = parsed["ages"]
                if parsed["kids"] is None and parsed["adults"] is None:
                    reservation_state["step"] = "awaiting_kids_info"
                    return "Imate otroke? Koliko in koliko so stari?"
                if parsed["kids"] and not parsed["ages"]:
                    reservation_state["step"] = "awaiting_kids_ages"
                    return "Koliko so stari otroci?"
                return proceed_after_table_people(reservation_state, reservation_service)
        reservation_state["step"] = "awaiting_table_people"
        return "Za koliko oseb pripravimo mizo?"

    if step == "awaiting_kids_info":
        text = message.lower().strip()
        if any(word in text for word in ["ne", "brez", "ni", "nimam"]):
            reservation_state["kids"] = 0
            reservation_state["kids_ages"] = ""
            return proceed_after_table_people(reservation_state, reservation_service)
        if is_affirmative(text):
            return "Koliko otrok?"
        kids_parsed = parse_kids_response(message)
        if kids_parsed["kids"] is not None:
            reservation_state["kids"] = kids_parsed["kids"]
        if kids_parsed["ages"]:
            reservation_state["kids_ages"] = kids_parsed["ages"]
        if reservation_state.get("kids") and not reservation_state.get("kids_ages"):
            reservation_state["step"] = "awaiting_kids_ages"
            return "Koliko so stari otroci?"
        return proceed_after_table_people(reservation_state, reservation_service)

    if step == "awaiting_kids_ages":
        reservation_state["kids_ages"] = message.strip()
        return proceed_after_table_people(reservation_state, reservation_service)

    if step == "awaiting_note":
        skip_words = {"ne", "nic", "nič", "nimam", "brez"}
        note_text = "" if any(word in message.lower() for word in skip_words) else message.strip()
        reservation_state["note"] = note_text
        reservation_state["step"] = "awaiting_confirmation"
        lines = [
            "Prosimo, preverite podatke:",
            f"📅 Datum: {reservation_state.get('date')} ob {reservation_state.get('time')}",
            f"👥 Osebe: {reservation_state.get('people')}",
            f"🍽️ Jedilnica: {reservation_state.get('location')}",
            f"👤 Ime: {reservation_state.get('name')}",
            f"📞 Telefon: {reservation_state.get('phone')}",
            f"📧 Email: {reservation_state.get('email')}",
        ]
        if note_text:
            lines.append(f"📝 Opombe: {note_text}")
        lines.append("Potrdite rezervacijo? (da/ne)")
        return "\n".join(lines)

    if step == "awaiting_confirmation":
        if message.strip().lower() in {"ne", "no"}:
            reset_reservation_state(state)
            return "V redu, rezervacijo sem preklical. Kako vam lahko pomagam?"
        if is_affirmative(message):
            summary_state = reservation_state.copy()
            res_id = reservation_service.create_reservation(
                date=reservation_state["date"] or "",
                people=int(reservation_state["people"] or 0),
                reservation_type="table",
                source="chat",
                time=reservation_state["time"],
                location=reservation_state["location"],
                name=str(reservation_state["name"]),
                phone=str(reservation_state["phone"]),
                email=reservation_state["email"],
                note=reservation_state.get("note") or "",
                kids=str(reservation_state.get("kids") or ""),
                kids_small=str(reservation_state.get("kids_ages") or ""),
                event_type=reservation_state.get("event_type"),
            )
            email_data = {
                "id": res_id,
                "name": reservation_state.get("name", ""),
                "email": reservation_state.get("email", ""),
                "phone": reservation_state.get("phone", ""),
                "date": reservation_state.get("date", ""),
                "time": reservation_state.get("time", ""),
                "people": reservation_state.get("people", 0),
                "reservation_type": "table",
                "location": reservation_state.get("location", ""),
                "note": reservation_state.get("note") or "",
                "kids": reservation_state.get("people_kids", ""),
                "kids_ages": reservation_state.get("kids_ages", ""),
            }
            session_id = reservation_state.get("session_id")
            if session_id:
                reservation_service.log_conversation(
                    session_id=session_id,
                    user_message="(auto) reservation completed",
                    bot_response="(auto) reservation completed",
                    intent="reservation_completed",
                    needs_followup=False,
                )
            send_reservation_emails_async(email_data)
            reset_reservation_state(state)
            final_response = (
                "Super! 😊 Vaša rezervacija mize je zabeležena:\n"
                f"📅 Datum: {summary_state.get('date')} ob {summary_state.get('time')}\n"
                f"👥 Osebe: {summary_state.get('people')}\n"
                f"🍽️ Jedilnica: {summary_state.get('location')}\n"
                f"{'📝 Opombe: ' + (summary_state.get('note') or '') if summary_state.get('note') else ''}\n\n"
                f"{reservation_pending_message.strip()}"
            )
            return final_response
        return "Prosim potrdite z 'da' ali 'ne'."

    if step == "awaiting_table_people":
        parsed = parse_people_count(message)
        people = parsed["total"]
        if people is None or people <= 0:
            return "Prosim sporočite število oseb (npr. '6 oseb')."
        if people > 35:
            return "Za večje skupine nad 35 oseb nas prosim kontaktirajte za dogovor o razporeditvi."
        reservation_state["people"] = people
        reservation_state["adults"] = parsed["adults"]
        reservation_state["kids"] = parsed["kids"]
        reservation_state["kids_ages"] = parsed["ages"]
        if parsed["kids"] is None and parsed["adults"] is None:
            reservation_state["step"] = "awaiting_kids_info"
            return "Imate otroke? Koliko in koliko so stari?"
        if parsed["kids"] and not parsed["ages"]:
            reservation_state["step"] = "awaiting_kids_ages"
            return "Koliko so stari otroci?"
        return proceed_after_table_people(reservation_state, reservation_service)

    if step == "awaiting_table_location":
        choice = message.strip().lower()
        options = reservation_state.get("available_locations") or []
        selected = None
        for opt in options:
            if opt.lower() in choice or opt.lower().split()[-1] in choice:
                selected = opt
                break
        if not selected:
            return "Prosim izberite med: " + " ali ".join(options)
        reservation_state["location"] = selected
        reservation_state["step"] = "awaiting_name"
        return f"Zabeleženo: {selected}. Prosim še ime in priimek nosilca rezervacije."

    if step == "awaiting_name":
        full_name = message.strip()
        if len(full_name.split()) < 2:
            return "Prosim napišite ime in priimek (npr. 'Nika Kmetija Pod Goro')."
        reservation_state["name"] = full_name
        reservation_state["step"] = "awaiting_phone"
        return "Hvala! Zdaj prosim še telefonsko številko."

    if step == "awaiting_phone":
        phone = message.strip()
        digits = re.sub(r"\D+", "", phone)
        if len(digits) < 7:
            return "Zaznal sem premalo številk. Prosimo vpišite veljavno telefonsko številko."
        reservation_state["phone"] = phone
        reservation_state["step"] = "awaiting_email"
        return "Kam naj pošljem povzetek ponudbe? (e-poštni naslov)"

    if step == "awaiting_email":
        email = message.strip()
        if "@" not in email or "." not in email:
            return "Prosim vpišite veljaven e-poštni naslov (npr. info@primer.si)."
        reservation_state["email"] = email
        reservation_state["step"] = "awaiting_note"
        return "Želite še kaj sporočiti? (posebne želje, alergije, praznovanje...)"

    return "Nadaljujmo z rezervacijo mize. Kateri datum vas zanima?"


def handle_table_reservation(
    message: str,
    state: dict[str, Optional[str | int]],
    translate_response: Any,
    reservation_service: Any,
    reset_reservation_state: Any,
    is_affirmative: Any,
    send_reservation_emails_async: Any,
    reservation_pending_message: str,
) -> str:
    response = _handle_table_reservation_impl(
        message,
        state,
        reservation_service,
        reset_reservation_state,
        is_affirmative,
        send_reservation_emails_async,
        reservation_pending_message,
    )
    lang = state.get("language", "si")
    return translate_response(response, lang)


def handle_reservation_flow(
    message: str,
    state: dict[str, Optional[str | int]],
    detect_language: Any,
    translate_response: Any,
    parse_reservation_type: Any,
    room_intro_text: Any,
    table_intro_text: Any,
    reset_reservation_state: Any,
    is_affirmative: Any,
    reservation_service: Any,
    validate_reservation_rules_fn: Any,
    advance_after_room_people_fn: Any,
    handle_room_reservation_fn: Any,
    handle_table_reservation_fn: Any,
    exit_keywords: set[str],
    detect_reset_request: Any,
    send_reservation_emails_async: Any,
    reservation_pending_message: str,
) -> str:
    reservation_state = state
    if reservation_state.get("language") is None:
        reservation_state["language"] = detect_language(message)

    def _tr(text: str) -> str:
        return translate_response(text, reservation_state.get("language", "si"))

    if any(word in message.lower() for word in exit_keywords):
        reset_reservation_state(state)
        return _tr("V redu, rezervacijo sem preklical. Kako vam lahko pomagam?")

    if detect_reset_request(message):
        reset_reservation_state(state)
        return _tr("Ni problema, začniva znova. Želite rezervirati sobo ali mizo za kosilo?")

    lowered = message.lower()
    if reservation_state.get("step") and reservation_state.get("type") == "room" and "miza" in lowered:
        reset_reservation_state(state)
        reservation_state["type"] = "table"
        reservation_state["step"] = "awaiting_table_date"
        return _tr(
            f"Preklopim na rezervacijo mize. Za kateri datum (sobota/nedelja)? (DD.MM ali DD.MM.YYYY)\n{table_intro_text()}"
        )
    if reservation_state.get("step") and reservation_state.get("type") == "table" and "soba" in lowered:
        reset_reservation_state(state)
        reservation_state["type"] = "room"
        reservation_state["step"] = "awaiting_room_date"
        return _tr(
            f"Preklopim na rezervacijo sobe. Za kateri datum prihoda? (DD.MM ali DD.MM.YYYY)\n{room_intro_text()}"
        )

    if reservation_state.get("step") is None:
        detected = reservation_state.get("type") or parse_reservation_type(message)
        if detected == "room":
            reservation_state["type"] = "room"
            prefilled_date = extract_date_from_text(message)
            range_data = extract_date_range(message)
            if range_data:
                prefilled_date = range_data[0]
            prefilled_nights = None
            if any(token in message.lower() for token in ["nočit", "nocit", "noči", "noci"]):
                prefilled_nights = extract_nights(message)
            if range_data and not prefilled_nights:
                prefilled_nights = nights_from_range(range_data[0], range_data[1])
            prefilled_people = parse_people_count(message)
            if prefilled_people.get("total"):
                reservation_state["people"] = prefilled_people["total"]
                reservation_state["adults"] = prefilled_people["adults"]
                reservation_state["kids"] = prefilled_people["kids"]
                reservation_state["kids_ages"] = prefilled_people["ages"]
            if prefilled_date:
                reservation_state["date"] = prefilled_date
            reply_prefix = "Super, z veseljem uredim rezervacijo sobe. 😊"
            if prefilled_nights:
                ok, error_message, _ = validate_reservation_rules_fn(
                    reservation_state["date"] or "", prefilled_nights
                )
                if not ok:
                    reservation_state["step"] = "awaiting_room_date"
                    reservation_state["date"] = None
                    reservation_state["nights"] = None
                    return _tr(
                        f"{error_message} Na voljo imamo najmanj 2 nočitvi (oz. 3 v poletnih mesecih). "
                        "Mi pošljete nov datum prihoda (DD.MM ali DD.MM.YYYY) in število nočitev?"
                    )
                reservation_state["nights"] = prefilled_nights
            if not reservation_state.get("date"):
                reservation_state["step"] = "awaiting_room_date"
                return _tr(
                    f"{reply_prefix} Za kateri datum prihoda? (DD.MM ali DD.MM.YYYY)\n{room_intro_text()}"
                )
            if not reservation_state.get("nights"):
                reservation_state["step"] = "awaiting_nights"
                return _tr(
                    f"{reply_prefix} Koliko nočitev načrtujete? (min. 3 v jun/jul/avg, sicer 2)"
                )
            if reservation_state.get("people"):
                if reservation_state.get("kids") is None and reservation_state.get("adults") is None:
                    reservation_state["step"] = "awaiting_kids_info"
                    return _tr("Imate otroke? Koliko in koliko so stari?")
                if reservation_state.get("kids") and not reservation_state.get("kids_ages"):
                    reservation_state["step"] = "awaiting_kids_ages"
                    return _tr("Koliko so stari otroci?")
                reply = advance_after_room_people_fn(reservation_state, reservation_service)
                return _tr(reply)
            reservation_state["step"] = "awaiting_people"
            return _tr(
                f"{reply_prefix} Zabeleženo imam {reservation_state['date']} za "
                f"{reservation_state['nights']} nočitev. Za koliko oseb bi to bilo?"
            )
        if detected == "table":
            reservation_state["type"] = "table"
            prefilled_date = extract_date_from_text(message)
            prefilled_time = extract_time(message)
            prefilled_people = parse_people_count(message)
            if prefilled_date:
                reservation_state["date"] = prefilled_date
            if prefilled_time:
                reservation_state["time"] = reservation_service._parse_time(prefilled_time)
            if prefilled_people.get("total"):
                reservation_state["people"] = prefilled_people["total"]
                reservation_state["adults"] = prefilled_people["adults"]
                reservation_state["kids"] = prefilled_people["kids"]
                reservation_state["kids_ages"] = prefilled_people["ages"]

            if not reservation_state.get("date"):
                reservation_state["step"] = "awaiting_table_date"
                return _tr(
                    f"Odlično, mizo rezerviramo z veseljem. Za kateri datum (sobota/nedelja)? (DD.MM ali DD.MM.YYYY)\n{table_intro_text()}"
                )

            time_for_rules = reservation_state.get("time") or "12:00"
            ok, error_message = reservation_service.validate_table_rules(
                reservation_state["date"] or "", time_for_rules
            )
            if not ok:
                reservation_state["date"] = None
                reservation_state["time"] = None
                reservation_state["step"] = "awaiting_table_date"
                return _tr(error_message + " Prosim, pošljite nov datum (DD.MM ali DD.MM.YYYY).")

            if not reservation_state.get("time"):
                reservation_state["step"] = "awaiting_table_time"
                return _tr("Ob kateri uri bi želeli mizo? (12:00–20:00, zadnji prihod na kosilo 15:00)")

            if not reservation_state.get("people"):
                reservation_state["step"] = "awaiting_table_people"
                return _tr("Za koliko oseb pripravimo mizo?")

            if reservation_state.get("kids") is None and reservation_state.get("adults") is None:
                reservation_state["step"] = "awaiting_kids_info"
                return _tr("Imate otroke? Koliko in koliko so stari?")
            if reservation_state.get("kids") and not reservation_state.get("kids_ages"):
                reservation_state["step"] = "awaiting_kids_ages"
                return _tr("Koliko so stari otroci?")

            reply = proceed_after_table_people(reservation_state, reservation_service)
            return _tr(reply)
        reservation_state["step"] = "awaiting_type"
        return _tr("Kako vam lahko pomagam – rezervacija sobe ali mize za kosilo?")

    if reservation_state.get("step") == "awaiting_type":
        choice = parse_reservation_type(message)
        if not choice:
            return _tr(
                "Mi zaupate, ali rezervirate sobo ali mizo za kosilo? "
                f"{room_intro_text()} / {table_intro_text()}"
            )
        reservation_state["type"] = choice
        if choice == "room":
            reservation_state["step"] = "awaiting_room_date"
            return _tr(
            f"Odlično, sobo uredimo. Za kateri datum prihoda razmišljate? (DD.MM ali DD.MM.YYYY)\n{room_intro_text()}"
            )
        reservation_state["step"] = "awaiting_table_date"
        return _tr(
            f"Super, uredim mizo. Za kateri datum (sobota/nedelja)? (DD.MM ali DD.MM.YYYY)\n{table_intro_text()}"
        )

    if reservation_state.get("type") == "room":
        return handle_room_reservation_fn(
            message,
            state,
            translate_response,
            reservation_service,
            is_affirmative,
            validate_reservation_rules_fn,
            advance_after_room_people_fn,
            reset_reservation_state,
            send_reservation_emails_async,
            reservation_pending_message,
        )
    return handle_table_reservation_fn(
        message,
        state,
        translate_response,
        reservation_service,
        reset_reservation_state,
        is_affirmative,
        send_reservation_emails_async=send_reservation_emails_async,
        reservation_pending_message=reservation_pending_message,
    )
