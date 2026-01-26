#!/usr/bin/env python3
import email
import imaplib
import os
import re
from email.header import decode_header
from pathlib import Path


def decode_mime(value):
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for text, enc in parts:
        if isinstance(text, bytes):
            decoded.append(text.decode(enc or "utf-8", errors="ignore"))
        else:
            decoded.append(text)
    return "".join(decoded)


def safe_name(value):
    value = value.strip().replace("\n", " ").replace("\r", " ")
    value = re.sub(r"[^A-Za-z0-9._ -]+", "_", value)
    return value[:180] or "file"


def save_pdf_attachments(msg, out_dir, prefix):
    saved = 0
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = decode_mime(filename)
        if not filename.lower().endswith(".pdf"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        out_name = f"{prefix}_{safe_name(filename)}"
        out_path = out_dir / out_name
        if out_path.exists():
            continue
        out_path.write_bytes(payload)
        saved += 1
    return saved


def main():
    user = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    query = os.environ.get("GMAIL_QUERY", "").strip()
    out_dir = Path(os.environ.get("GMAIL_OUT_DIR", "exports/gmail_pdfs")).resolve()

    if not user or not password or not query:
        print("Missing env vars. Set GMAIL_USER, GMAIL_APP_PASSWORD, GMAIL_QUERY.")
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)

    with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
        imap.login(user, password)
        imap.select('"[Gmail]/Sent Mail"')

        status, data = imap.search(None, query)
        if status != "OK":
            print("Search failed:", status, data)
            return 1

        ids = data[0].split()
        print(f"Found {len(ids)} messages.")

        total = 0
        for msg_id in ids:
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject = decode_mime(msg.get("Subject", "no-subject"))
            msg_prefix = safe_name(subject) or "message"
            total += save_pdf_attachments(msg, out_dir, msg_prefix)

        print(f"Saved {total} PDF attachments to {out_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
