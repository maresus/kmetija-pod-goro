#!/usr/bin/env bash
set -euo pipefail

BASE_URL=${1:-http://127.0.0.1:8000}
SESSION_ID=${2:-health-smoke}

ask() {
  local msg="$1"
  echo "\n> $msg"
  curl -s "$BASE_URL/chat" \
    -H 'Content-Type: application/json' \
    -d "{\"session_id\": \"$SESSION_ID\", \"message\": \"$msg\"}" | sed 's/^/  /'
}

ask "Kako se naročim na dermatološki pregled?"
ask "Koliko stane dermatološki pregled?"
ask "Ali delate lasersko odstranjevanje bradavic?"
ask "Kje imate ordinacijo in kako do vas?"
ask "Imate okulistični pregled?"
ask "Kdaj lahko pridem na ortopedski pregled?"
ask "Kaj obsega dermatološki pregled?"
ask "Ali izvajate estetske posege (botox/filerji)?"
ask "Kakšen je cenik kozmetičnih storitev?"
ask "Imate naročanje preko telefona ali e-pošte?"
