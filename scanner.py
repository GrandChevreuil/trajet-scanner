"""Scanner d'allers-retours pas chers sur Google Flights (via la lib open source fast-flights).

Pour chaque origine x destination x date de départ, relève l'A/R de N jours le moins cher.

    pip install fast-flights curl_cffi
    python3 scanner.py --de PAR NTE --vers TBS KUT BUS --debut 2027-01-01 --fin 2027-03-31 --jours 10 --csv georgie.csv

Codes : aéroport (CDG, BVA...) ou ville (PAR = CDG + ORY + BVA).

Limites : Google refuse les requêtes après quelques centaines d'affilée (erreur 13, signalée en fin de scan).
Les trajets à correspondances lentes à calculer (ex. depuis Nantes) ne sont pas dans la page renvoyée :
« aucun vol trouvé » veut alors dire « à vérifier sur Google Flights », pas « aucun vol ».
"""
import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from curl_cffi import requests
from fast_flights import FlightQuery, Passengers, create_query
from fast_flights.exceptions import FlightsNotFound
from fast_flights.parser import parse_js
from selectolax.lexbor import LexborHTMLParser

URL = "https://www.google.com/travel/flights/search"
# Cookie de consentement Google = choix « Tout refuser » ; sans lui, Google renvoie la page RGPD.
COOKIES = {"SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"}
REFUS = []  # (origine, destination) des recherches que Google a refusées (erreur 13)


def params(o, d, aller, retour=None):
    """Recherche aller-retour, ou aller simple si retour est None."""
    vols = [FlightQuery(date=aller.isoformat(), from_airport=o, to_airport=d)]
    if retour:
        vols.append(FlightQuery(date=retour.isoformat(), from_airport=d, to_airport=o))
    q = create_query(flights=vols, trip="round-trip" if retour else "one-way", seat="economy",
                     passengers=Passengers(adults=1), language="fr", currency="EUR")
    return {**q.params(), "gl": "FR"}


def fetch(o, d, aller, retour=None):
    for essai in range(4):
        try:
            r = requests.get(URL, params=params(o, d, aller, retour), cookies=COOKIES, impersonate="chrome", timeout=60)
            if r.status_code == 200 and "/sorry/" not in r.url:
                return r.text
            erreur = r.status_code
        except requests.exceptions.RequestException as e:  # coupure réseau, reset TLS...
            erreur = e
        time.sleep(15 * (essai + 1))
    sys.exit(f"Google bloque les requêtes ({erreur}) — relancer plus tard ou avec moins de --parallele")


def lire(html):
    js = LexborHTMLParser(html).css_first(r"script.ds\:1").text()
    data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        raise FlightsNotFound("erreur Google")
    p = json.loads(data)
    # fast-flights ne lit que p[3] (« autres vols ») ; les meilleurs vols, souvent les low cost, sont dans p[2]
    tous = ((p[2] or [None])[0] or []) + ((p[3] or [None])[0] or [])
    p[3] = [tous, *(p[3] or [None])[1:]]
    return parse_js("data:" + json.dumps(p) + ",0")


def moins_cher(o, d, aller, retour=None):
    for essai in range(2):
        try:
            vols = [v for v in lire(fetch(o, d, aller, retour)) if v.price]
            break
        except (IndexError, TypeError, ValueError, json.JSONDecodeError, AttributeError):
            return None  # pas de vol ce jour-là (page sans résultats)
        except FlightsNotFound:
            time.sleep(30)  # erreur 13 de Google : souvent trop de requêtes, on ralentit puis on retente
    else:
        REFUS.append((o, d))
        return None
    if not vols:
        return None
    v = min(vols, key=lambda v: v.price)
    heure = lambda t: datetime(*t.date, *t.time)
    # durée de l'aller = temps en vol + attentes aux escales (heures locales, mais l'attente est dans le même fuseau)
    duree = sum(f.duration for f in v.flights) + sum(
        int((heure(b.departure) - heure(a.arrival)).total_seconds() // 60) for a, b in zip(v.flights, v.flights[1:]))
    segments = [{"de": f.from_airport.code, "de_nom": f.from_airport.name, "vers": f.to_airport.code,
                 "vers_nom": f.to_airport.name, "depart": heure(f.departure).isoformat(timespec="minutes"),
                 "arrivee": heure(f.arrival).isoformat(timespec="minutes"), "minutes": f.duration, "avion": f.plane_type}
                for f in v.flights]
    return {"origine": o, "destination": d, "aller": aller.isoformat(), "retour": retour.isoformat() if retour else None,
            "segments": segments, "prix": v.price, "duree_vol": duree, "escales": len(v.flights) - 1, "compagnies": " / ".join(v.airlines),
            "trajet_aller": " > ".join([v.flights[0].from_airport.code] + [s.to_airport.code for s in v.flights]),
            "heure": "%02d:%02d" % v.flights[0].departure.time,
            "lien": f"{URL}?{urlencode(params(o, d, aller, retour))}"}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--de", nargs="+", required=True)
    p.add_argument("--vers", nargs="+", required=True)
    p.add_argument("--debut", type=date.fromisoformat, required=True, help="premier départ possible")
    p.add_argument("--fin", type=date.fromisoformat, required=True, help="dernier retour possible")
    p.add_argument("--jours", type=int, default=10)
    p.add_argument("--pas", type=int, default=1, help="un départ tous les N jours")
    p.add_argument("--parallele", type=int, default=4)
    p.add_argument("--csv")
    p.add_argument("--top", type=int, default=5)
    a = p.parse_args()

    departs = [a.debut + timedelta(n) for n in range(0, (a.fin - a.debut).days - a.jours + 1, a.pas)]
    taches = [(o, d, x, x + timedelta(a.jours)) for o in a.de for d in a.vers for x in departs]
    print(f"{len(taches)} recherches...", file=sys.stderr)
    # ponytail: un thread pool simple, pas de file de reprise ; relancer en cas de blocage Google
    with ThreadPoolExecutor(a.parallele) as ex:
        res = [r for r in ex.map(lambda t: moins_cher(*t), taches) if r]

    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[k for k in res[0] if k != "segments"] if res else ["prix"])
            w.writeheader()
            w.writerows({k: v for k, v in r.items() if k != "segments"} for r in res)
    res.sort(key=lambda r: r["prix"])
    for o in a.de:
        for d in a.vers:
            top = [r for r in res if r["origine"] == o and r["destination"] == d][:a.top]
            refus = REFUS.count((o, d))
            print(f"\n{o} -> {d}" + ("" if top else " : aucun vol trouvé") + (f"  [{refus} recherches refusées par Google]" if refus else ""))
            for r in top:
                print(f"  {r['prix']:>5} €  {r['aller']} -> {r['retour']}  {r['compagnies']}  ({r['trajet_aller']})")
    if REFUS:
        print(f"\nAttention : {len(REFUS)}/{len(taches)} recherches refusées par Google (erreur 13), résultats incomplets."
              " Relancer plus tard ou avec --parallele 1.", file=sys.stderr)


if __name__ == "__main__":
    main()
