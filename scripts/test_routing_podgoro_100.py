from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Tuple, Union

os.environ.setdefault("USE_UNIFIED_ROUTER", "true")
os.environ.setdefault("STRICT_POLICY", "true")
os.environ.setdefault("DISABLE_INQUIRY", "true")
os.environ.setdefault("SHOP_BASE_URL", "https://kmetijapodgoro.si")
os.environ.setdefault("INFO_EMAIL", "info@kmetijapodgoro.si")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
import app.services.chat_router as chat_router  # noqa: E402
sys.path.append(str(ROOT.parent))  # to import shared scenario builder
from scripts.routing_scenarios import build_scenarios, slice_scenarios  # noqa: E402

chat_router.USE_FULL_KB_LLM = False

client = TestClient(app)

Turn = Tuple[str, Union[str, List[str]]]
Scenario = Tuple[str, List[Turn]]


def _expect_match(reply: str, expected: Union[str, List[str]]) -> bool:
    reply_l = reply.lower()
    if isinstance(expected, list):
        return any(exp.lower() in reply_l for exp in expected)
    return expected.lower() in reply_l


def run_scenario(name: str, turns: List[Turn], idx: int) -> List[str]:
    failures: List[str] = []
    session_id = f"podgoro_100_{idx}_{name}"
    for msg, expected in turns:
        resp = client.post("/chat", json={"message": msg, "session_id": session_id})
        if resp.status_code != 200:
            failures.append(f"{name}: HTTP {resp.status_code} on '{msg}'")
            continue
        reply = resp.json().get("reply", "")
        if expected and not _expect_match(reply, expected):
            failures.append(f"{name}: expected '{expected}' in reply to '{msg}'\nReply: {reply}")
    return failures


def main() -> None:
    scenarios = build_scenarios(
        base_url="https://kmetijapodgoro.si",
        location_token="Zeleno",
        email_token="info@kmetijapodgoro.si",
        include_booking=True,
    )
    scenarios = slice_scenarios(scenarios, 100)

    all_failures: List[str] = []
    for idx, (name, turns) in enumerate(scenarios, start=1):
        all_failures.extend(run_scenario(name, turns, idx))

    if all_failures:
        print("\nFAILURES:")
        for fail in all_failures:
            print("-", fail)
        raise SystemExit(1)
    print("All scenarios passed.")


if __name__ == "__main__":
    main()
