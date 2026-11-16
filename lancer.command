#!/bin/sh
# Lance le scanner web (double-clic dans le Finder ou ./lancer.command).
# Crée .venv et installe les dépendances au premier lancement (ou si elles manquent).
cd "$(dirname "$0")" || exit 1
[ -x .venv/bin/python ] || python3 -m venv .venv || exit 1
# typing_extensions : dépendance oubliée par fast-flights 3.1.0
.venv/bin/python -c "import fast_flights, curl_cffi" 2>/dev/null ||
  .venv/bin/pip install -q fast-flights curl_cffi typing_extensions || exit 1
(sleep 2 && open http://localhost:8765) &
exec .venv/bin/python app.py
