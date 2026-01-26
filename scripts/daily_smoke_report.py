#!/usr/bin/env python3
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import requests

from app.services.email_service import send_custom_message

DEFAULT_BASE_URL = os.getenv("SMOKE_BASE_URL", "http://127.0.0.1:8000")
QUESTIONS_PATH = os.getenv("SMOKE_QUESTIONS_PATH", "data/smoke_questions.txt")
EMAIL_TO = os.getenv("SMOKE_EMAIL_TO") or os.getenv("ADMIN_EMAIL", "")
EMAIL_MODE = os.getenv("SMOKE_EMAIL_MODE", "errors").strip().lower()
REQUEST_TIMEOUT = float(os.getenv("SMOKE_TIMEOUT_SEC", "12"))


def load_questions(path: str) -> List[str]:
    p = Path(path)
    if not p.exists():
        return []
    questions = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        questions.append(line)
    return questions


def is_error_reply(reply: str) -> bool:
    lowered = reply.lower()
    if "oprostite, strežnik ni dosegljiv" in lowered:
        return True
    if "napaka pri klicu" in lowered:
        return True
    return False


def run_smoke(base_url: str, questions: List[str]) -> Dict[str, Any]:
    session_prefix = datetime.now().strftime("smoke-%Y%m%d-%H%M")
    results = []
    errors = 0
    total_ms = 0

    for idx, question in enumerate(questions, start=1):
        session_id = f"{session_prefix}-{idx}"
        payload = {"session_id": session_id, "message": question}
        start = time.time()
        try:
            resp = requests.post(
                f"{base_url}/chat",
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
            latency_ms = int((time.time() - start) * 1000)
            total_ms += latency_ms
            ok = resp.status_code == 200
            reply = ""
            if ok:
                data = resp.json()
                reply = data.get("reply", "") if isinstance(data, dict) else ""
                if not reply or is_error_reply(reply):
                    ok = False
            if not ok:
                errors += 1
            results.append(
                {
                    "question": question,
                    "status": resp.status_code,
                    "ok": ok,
                    "latency_ms": latency_ms,
                    "reply_preview": reply[:180],
                }
            )
        except Exception as exc:
            latency_ms = int((time.time() - start) * 1000)
            total_ms += latency_ms
            errors += 1
            results.append(
                {
                    "question": question,
                    "status": "error",
                    "ok": False,
                    "latency_ms": latency_ms,
                    "reply_preview": f"EXCEPTION: {exc}",
                }
            )

    avg_ms = int(total_ms / max(1, len(questions)))
    return {
        "base_url": base_url,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "total": len(questions),
        "errors": errors,
        "avg_latency_ms": avg_ms,
        "results": results,
    }


def save_report(report: Dict[str, Any]) -> Path:
    out_dir = Path("data/smoke_reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    out_path = out_dir / f"smoke-{stamp}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def send_report(report: Dict[str, Any], report_path: Path) -> None:
    if not EMAIL_TO:
        return
    if EMAIL_MODE == "errors" and report["errors"] == 0:
        return
    subject = f"[Kmetija Pod Goro AI] Smoke test {report['timestamp']} (errors: {report['errors']})"
    lines = [
        f"Base URL: {report['base_url']}",
        f"Total: {report['total']}",
        f"Errors: {report['errors']}",
        f"Avg latency: {report['avg_latency_ms']} ms",
        "",
    ]
    for item in report["results"]:
        if item["ok"]:
            continue
        lines.append(f"- {item['question']} | status={item['status']} | {item['reply_preview']}")
    lines.append("")
    lines.append(f"Report file: {report_path}")
    body = "\n".join(lines)
    send_custom_message(EMAIL_TO, subject, body)


def main() -> None:
    questions = load_questions(QUESTIONS_PATH)
    if not questions:
        print("No questions found. Check SMOKE_QUESTIONS_PATH.")
        return
    report = run_smoke(DEFAULT_BASE_URL, questions)
    report_path = save_report(report)
    send_report(report, report_path)
    print(json.dumps({k: report[k] for k in ["timestamp", "total", "errors", "avg_latency_ms"]}))


if __name__ == "__main__":
    main()
