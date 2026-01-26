import json
import os
import re
import threading
import time
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional, Tuple

import imaplib

from app.services.reservation_service import ReservationService

IMAP_HOST = os.getenv("IMAP_HOST", "").strip()
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "").strip()
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "").strip()
IMAP_SSL = os.getenv("IMAP_SSL", "").strip().lower() in {"1", "true", "yes"}
IMAP_POLL_INTERVAL = int(os.getenv("IMAP_POLL_INTERVAL", "300"))
SUBJECT_PREFIX = os.getenv("SUBJECT_PREFIX", "").strip()

RESERVATION_ID_RE = re.compile(r"rezervacija\s*#(\d+)", re.IGNORECASE)


def _decode_header(value: Optional[str]) -> str:
    if not value:
        return ""
    decoded_parts = decode_header(value)
    parts = []
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            try:
                parts.append(part.decode(encoding or "utf-8", errors="ignore"))
            except Exception:
                parts.append(part.decode("utf-8", errors="ignore"))
        else:
            parts.append(part)
    return "".join(parts)


def _extract_text(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = (part.get("Content-Disposition") or "").lower()
            if content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="ignore").strip()
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                html = payload.decode(charset, errors="ignore")
                return re.sub(r"<[^>]+>", " ", html).strip()
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="ignore").strip()


def _strip_reply_prefixes(subject: str) -> str:
    cleaned = subject or ""
    while True:
        updated = re.sub(r"^(re|fw|fwd)\\s*:\\s*", "", cleaned, flags=re.IGNORECASE)
        if updated == cleaned:
            return updated.strip()
        cleaned = updated


def _match_reservation_id(subject: str, body: str = "") -> Optional[int]:
    match = RESERVATION_ID_RE.search(subject or "")
    if not match:
        match = RESERVATION_ID_RE.search(body or "")
        if not match:
            return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _state_path() -> Path:
    project_root = Path(__file__).resolve().parents[2]
    data_dir = project_root / "data"
    data_dir.mkdir(exist_ok=True)
    return data_dir / "imap_state.json"


def _load_last_uid() -> int:
    path = _state_path()
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("last_uid", 0))
    except Exception:
        return 0


def load_state() -> dict:
    path = _state_path()
    if not path.exists():
        return {"last_uid": 0, "last_poll_at": None, "last_error": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "last_uid": int(data.get("last_uid", 0)),
            "last_poll_at": data.get("last_poll_at"),
            "last_error": data.get("last_error"),
        }
    except Exception:
        return {"last_uid": 0, "last_poll_at": None, "last_error": None}


def _save_state(uid: int, last_poll_at: Optional[str], last_error: Optional[str]) -> None:
    path = _state_path()
    payload = {
        "last_uid": uid,
        "last_poll_at": last_poll_at,
        "last_error": last_error,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _imap_connect() -> imaplib.IMAP4:
    if IMAP_SSL:
        return imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    return imaplib.IMAP4(IMAP_HOST, IMAP_PORT)


def _list_folders(mail: imaplib.IMAP4) -> list[str]:
    """Vrne seznam map v mailboxu."""
    folders: list[str] = []
    status, data = mail.list()
    if status != "OK" or not data:
        return ["INBOX"]
    for raw in data:
        if not raw:
            continue
        line = raw.decode(errors="ignore")
        match = re.search(r'"([^"]+)"\\s*$', line)
        if match:
            folders.append(match.group(1))
        else:
            parts = line.split()
            if parts:
                folders.append(parts[-1].strip('"'))
    # vedno zagotovi INBOX na začetku
    uniq = []
    for f in folders:
        if f not in uniq:
            uniq.append(f)
    if "INBOX" not in uniq:
        uniq.insert(0, "INBOX")
    return uniq


def _process_message(
    service: ReservationService,
    uid: int,
    msg_bytes: bytes,
) -> Tuple[bool, Optional[int]]:
    msg = message_from_bytes(msg_bytes)
    subject = _decode_header(msg.get("Subject", ""))
    if SUBJECT_PREFIX:
        normalized = _strip_reply_prefixes(subject)
        if not normalized.startswith(SUBJECT_PREFIX):
            return False, None
    message_id = _decode_header(msg.get("Message-ID", "")) or f"imap-uid-{uid}"
    from_email = _decode_header(msg.get("From", ""))
    to_email = _decode_header(msg.get("To", ""))
    body = _extract_text(msg)

    reservation_id = _match_reservation_id(subject, body)
    if not reservation_id:
        return False, None

    if service.message_exists(message_id):
        return False, reservation_id

    ok = service.add_reservation_message(
        reservation_id=reservation_id,
        direction="inbound",
        subject=subject,
        body=body,
        from_email=from_email,
        to_email=to_email,
        message_id=message_id,
    )
    if not ok:
        print(f"[IMAP] Neuspešen zapis sporočila (reservation_id={reservation_id})")
        return False, reservation_id
    print(f"[IMAP] Zabeležen odgovor za rezervacijo #{reservation_id}")
    return True, reservation_id


def _poll_loop() -> None:
    if not (IMAP_HOST and IMAP_USER and IMAP_PASSWORD):
        print("[IMAP] Manjkajo IMAP nastavitve. Polling ne bo zagnan.")
        return

    service = ReservationService()
    last_uid = _load_last_uid()
    last_error: Optional[str] = None

    while True:
        try:
            mail = _imap_connect()
            mail.login(IMAP_USER, IMAP_PASSWORD)
            mail.select("INBOX")
            uids: set[int] = set()
            if last_uid:
                search_query = f"(UID {last_uid + 1}:*)"
                status, data = mail.uid("search", None, search_query)
                if status == "OK" and data and data[0]:
                    uids.update(int(u) for u in data[0].split())

            status, data = mail.uid("search", None, "UNSEEN")
            if status == "OK" and data and data[0]:
                uids.update(int(u) for u in data[0].split())

            for uid in sorted(uids):
                status, msg_data = mail.uid("fetch", str(uid), "(RFC822)")
                if status != "OK" or not msg_data:
                    continue
                msg_bytes = msg_data[0][1]
                processed, _ = _process_message(service, uid, msg_bytes)
                if processed:
                    mail.uid("store", str(uid), "+FLAGS", "(\\Seen)")
                last_uid = max(last_uid, uid)

            last_error = None
            _save_state(last_uid, datetime.now().isoformat(timespec="seconds"), last_error)
            mail.logout()
        except Exception as exc:
            last_error = str(exc)
            _save_state(last_uid, datetime.now().isoformat(timespec="seconds"), last_error)
            print(f"[IMAP] Napaka pri polling-u: {exc}")
        time.sleep(IMAP_POLL_INTERVAL)


def start_imap_poller() -> None:
    """Zažene IMAP polling v ozadju."""
    thread = threading.Thread(target=_poll_loop, daemon=True)
    thread.start()


def resync_last_messages(limit: int = 50) -> dict:
    """Ročno prebere zadnjih N sporočil in jih poskusi ujemati na rezervacije."""
    if not (IMAP_HOST and IMAP_USER and IMAP_PASSWORD):
        return {"ok": False, "error": "IMAP nastavitve manjkajo."}
    limit = max(1, min(limit, 200))
    service = ReservationService()
    processed = 0
    matched = 0
    scanned = 0
    sample_subjects: list[str] = []
    try:
        mail = _imap_connect()
        mail.login(IMAP_USER, IMAP_PASSWORD)
        folders = _list_folders(mail)
        for folder in folders:
            status, _ = mail.select(folder)
            if status != "OK":
                continue
            status, data = mail.uid("search", None, "ALL")
            if status != "OK" or not data or not data[0]:
                continue
            uids = [int(u) for u in data[0].split()]
            for uid in uids[-limit:]:
                status, msg_data = mail.uid("fetch", str(uid), "(RFC822)")
                if status != "OK" or not msg_data:
                    continue
                scanned += 1
                msg_bytes = msg_data[0][1]
                msg = message_from_bytes(msg_bytes)
                subject = _decode_header(msg.get("Subject", ""))
                if subject and len(sample_subjects) < 5:
                    sample_subjects.append(f"[{folder}] {subject}")
                if _match_reservation_id(subject):
                    matched += 1
                processed_now, _ = _process_message(service, uid, msg_bytes)
                if processed_now:
                    processed += 1
        mail.logout()
        return {
            "ok": True,
            "processed": processed,
            "matched": matched,
            "scanned": scanned,
            "sample_subjects": sample_subjects,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def preview_last_messages(limit: int = 10) -> dict:
    """Vrne osnovne podatke zadnjih N sporočil (subject/from/date)."""
    if not (IMAP_HOST and IMAP_USER and IMAP_PASSWORD):
        return {"ok": False, "error": "IMAP nastavitve manjkajo."}
    limit = max(1, min(limit, 50))
    try:
        mail = _imap_connect()
        mail.login(IMAP_USER, IMAP_PASSWORD)
        preview = []
        folders = _list_folders(mail)
        for folder in folders:
            status, _ = mail.select(folder)
            if status != "OK":
                continue
            status, data = mail.uid("search", None, "ALL")
            if status != "OK" or not data or not data[0]:
                continue
            uids = [int(u) for u in data[0].split()]
            for uid in uids[-limit:]:
                status, msg_data = mail.uid("fetch", str(uid), "(RFC822)")
                if status != "OK" or not msg_data:
                    continue
                msg_bytes = msg_data[0][1]
                msg = message_from_bytes(msg_bytes)
                preview.append(
                    {
                        "uid": uid,
                        "folder": folder,
                        "subject": _decode_header(msg.get("Subject", "")),
                        "from": _decode_header(msg.get("From", "")),
                        "date": _decode_header(msg.get("Date", "")),
                    }
                )
        mail.logout()
        return {"ok": True, "messages": preview}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
