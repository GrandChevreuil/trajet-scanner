"""Interface web du scanner : vol le moins cher + trajet jusqu'à l'aéroport (voiture, train, bus), aller et retour.

    ./lancer.command          (ou double-clic) : installe .venv au premier lancement, ouvre http://localhost:8765

Sources : vols Google Flights (scanner.py), bus Flixbus (prix réels), train = fourchette de prix open data SNCF
(TGV INOUI / OUIGO, estimation), voiture = itinéraire OSRM x coût au km + parking. Lieux : OpenStreetMap (Nominatim).
Aéroports : OurAirports, téléchargé une fois dans cache/.
"""
import csv
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from curl_cffi import requests

import scanner

ICI = Path(__file__).parent
AEROPORTS_CSV = ICI / "cache" / "airports.csv"
AEROPORTS_SOURCE = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv"
SNCF = "https://ressources.data.sncf.com/api/explore/v2.1/catalog/datasets/tarifs-tgv-inoui-ouigo/records"
FLIX = "https://global.api.flixbus.com/search"
UA = {"User-Agent": "scanner-voyage/1.0 (usage personnel)"}  # Nominatim et OSRM refusent les requêtes sans User-Agent
MAX_REQUETES = 400  # au-delà, Google renvoie l'erreur 13 (trop de requêtes)
BUDGET = 150  # nombre de recherches visé : le pas entre deux dates de départ s'en déduit
RAYON_KM, NB_AEROPORTS, EUR_KM, PARKING = 400, 8, 0.18, 8  # valeurs par défaut, réglables depuis la roue crantée

# Accès aux aéroports sans voiture, en aller simple :
#   train = (préfixe de la gare dans l'open data SNCF, lat, lon de la gare, coût du dernier trajet, minutes, détail)
#   bus   = [(ville ou arrêt Flixbus, coût du dernier trajet, minutes, détail), ...] essayés dans l'ordre
# ponytail: table à la main pour les aéroports utiles depuis l'ouest et Paris ; ailleurs, accès inconnu (lien Google Maps).
ACCES = {
    "NTE": {"train": ("NANTES", 47.2174, -1.5422, 9.0, 25, "navette aéroport depuis la gare (9 €)"),
            "bus": [("Nantes", 9.0, 25, "navette aéroport (9 €)")]},
    "RNS": {"train": ("RENNES", 48.1035, -1.6722, 1.8, 25, "bus C6 depuis la gare"),
            "bus": [("Rennes", 1.8, 25, "bus C6")]},
    "CDG": {"train": ("AEROPORT CDG2", 49.0040, 2.5710, 0, 10, "gare TGV dans l'aéroport"),
            "bus": [("Aéroport de Paris-Charles-de-Gaulle", 0, 0, "arrêt à l'aéroport"), ("Paris", 14.0, 50, "RER B (14 €)")]},
    "ORY": {"train": ("PARIS", 48.8412, 2.3200, 14.0, 45, "métro 14 jusqu'à Orly (14 €)"),
            "bus": [("Aéroport de Paris-Orly", 0, 0, "arrêt à l'aéroport"), ("Paris", 14.0, 45, "métro 14 (14 €)")]},
    "BVA": {"train": ("PARIS", 48.8412, 2.3200, 20.45, 110, "métro + navette Aérobus Porte Maillot (17,90 €)"),
            "bus": [("Aéroport de Paris-Beauvais", 0, 0, "arrêt à l'aéroport"),
                    ("Paris", 20.45, 110, "métro + navette Aérobus Porte Maillot (17,90 €)")]},
    "LYS": {"train": ("LYON ST EXUPERY", 45.7208, 5.0757, 0, 10, "gare TGV dans l'aéroport"),
            "bus": [("Aéroport de Lyon Saint-Exupéry", 0, 0, "arrêt à l'aéroport")]},
}


def km(a, b):
    la1, lo1, la2, lo2 = map(radians, (a[0], a[1], b[0], b[1]))
    return 6371 * 2 * asin(sqrt(sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2))


def get(url, **params):
    return requests.get(url, params=params, headers=UA, timeout=60).json()


def norm(s):
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().upper().replace("-", " ")
    return re.sub(r"\bSAINT\b", "ST", s)


@lru_cache
def aeroports():
    if not AEROPORTS_CSV.exists():
        AEROPORTS_CSV.parent.mkdir(exist_ok=True)
        AEROPORTS_CSV.write_bytes(requests.get(AEROPORTS_SOURCE, headers=UA, timeout=120).content)
    with AEROPORTS_CSV.open(encoding="utf-8") as f:
        return [{"iata": r["iata_code"], "nom": r["name"], "ville": r["municipality"], "pays": r["iso_country"],
                 "grand": r["type"] == "large_airport", "site": r["home_link"], "lat": float(r["latitude_deg"]), "lon": float(r["longitude_deg"])}
                for r in csv.DictReader(f)
                if r["iata_code"] and r["scheduled_service"] == "yes" and r["type"] in ("large_airport", "medium_airport")]


@lru_cache
def lieu(texte):
    r = get("https://nominatim.openstreetmap.org/search", q=texte, format="jsonv2", limit=1, addressdetails=1,
            **{"accept-language": "fr"})
    if not r:
        raise ValueError(f"Lieu introuvable : {texte}")
    r = r[0]
    return {"nom": r["name"], "lat": float(r["lat"]), "lon": float(r["lon"]),
            "pays": r["address"].get("country_code", "").upper(), "est_pays": r.get("addresstype") == "country"}


def aeroports_depart(o, rayon, nb):
    proches = sorted(({**a, "km": round(km((o["lat"], o["lon"]), (a["lat"], a["lon"])))} for a in aeroports()),
                     key=lambda a: a["km"])
    proches = [a for a in proches if a["km"] <= rayon]
    # les grands aéroports (souvent les moins chers) + les 3 petits les plus proches
    return sorted([a for a in proches if a["grand"]] + [a for a in proches if not a["grand"]][:3],
                  key=lambda a: a["km"])[:nb]


def aeroports_arrivee(texte):
    codes = texte.upper().split()
    if codes and all(re.fullmatch(r"[A-Z]{3}", c) for c in codes):
        return [a for a in aeroports() if a["iata"] in codes]
    l = lieu(texte)
    if l["est_pays"]:
        return [a for a in aeroports() if a["pays"] == l["pays"]]
    return sorted((a for a in aeroports() if km((l["lat"], l["lon"]), (a["lat"], a["lon"])) < 150),
                  key=lambda a: km((l["lat"], l["lon"]), (a["lat"], a["lon"])))[:4]


@lru_cache
def route(lat1, lon1, lat2, lon2):
    r = get(f"https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}", overview="false")
    if r.get("code") != "Ok":
        return None
    return r["routes"][0]["distance"] / 1000, r["routes"][0]["duration"] / 60


@lru_cache
def prix_train(ville, gare):
    ville = norm(ville).replace('"', "")
    r = get(SNCF, limit=100, where=f'search(gare_origine,"{ville}") and search(gare_destination,"{gare}") '
                                   'and classe="2" and profil_tarifaire="Tarif Normal"')
    r = [x for x in r.get("results", []) if x["gare_origine"].startswith(ville) and x["gare_destination"].startswith(gare)]
    return (min(x["prix_minimum"] for x in r), min(x["prix_maximum"] for x in r)) if r else None


@lru_cache
def flix_ville(nom):
    r = get(f"{FLIX}/autocomplete/cities", q=nom, lang="fr", flixbus_cities_only="false")
    return r[0]["id"] if r else None


@lru_cache
def flix_trajets(de, vers, jour):
    r = get(f"{FLIX}/service/v4/search", from_city_id=de, to_city_id=vers, departure_date=jour.strftime("%d.%m.%Y"),
            products='{"adult":1}', currency="EUR", locale="fr", search_by="cities")
    trajets = [{"depart": datetime.fromisoformat(v["departure"]["date"]).replace(tzinfo=None),
                "arrivee": datetime.fromisoformat(v["arrival"]["date"]).replace(tzinfo=None), "prix": v["price"]["total"]}
               for v in r["trips"][0]["results"].values() if v["status"] == "available"]
    return [t for t in trajets if t["depart"].hour >= 6]  # pas de car de nuit


def voiture(o, a, jours, eur_km, parking):
    r = route(o["lat"], o["lon"], a["lat"], a["lon"])
    if not r:
        return None
    dist, minutes = r
    return {"mode": "voiture", "cout": round(2 * dist * eur_km + parking * jours, 2), "minutes": round(minutes),
            "detail": f"{dist:.0f} km x2 à {eur_km} €/km + parking {parking} €/j x {jours} j", "estimation": True,
            "lien": f"https://www.google.com/maps/dir/{o['lat']},{o['lon']}/{a['lat']},{a['lon']}"}


def train(o, a):
    if a["iata"] not in ACCES:
        return None
    gare, lat, lon, cout, mins, detail = ACCES[a["iata"]]["train"]
    p = prix_train(o["nom"], gare)
    if not p:
        return None
    trajet = km((o["lat"], o["lon"]), (lat, lon)) / 150 * 60 + 20  # ponytail: durée estimée à 150 km/h, pas d'horaires
    return {"mode": "train", "cout": round(2 * (p[0] + cout), 2), "minutes": round(trajet + mins),
            "detail": f"{o['nom']} → {gare.title()} : {p[0]:.0f} à {p[1]:.0f} € l'aller, + {detail}",
            "estimation": True, "lien": "https://www.sncf-connect.com/"}


def bus(o, a, aller, retour, heure):
    origine = flix_ville(o["nom"])
    for nom, cout, mins, detail in ACCES.get(a["iata"], {}).get("bus", []):
        arret = flix_ville(nom)
        if not origine or not arret or arret == origine:
            continue
        limite = datetime.combine(aller, datetime.strptime(heure, "%H:%M").time()) - timedelta(minutes=120 + mins)
        alle = [t for t in flix_trajets(origine, arret, aller) if t["arrivee"] <= limite]
        ret = flix_trajets(arret, origine, retour)
        if not alle or not ret:
            continue
        g, r = min(alle, key=lambda t: t["prix"]), min(ret, key=lambda t: t["prix"])
        return {"mode": "bus", "arret": nom, "arret_id": arret, "origine_id": origine,
                "alle": {"depart": g["depart"].isoformat(timespec="minutes"),
                         "arrivee": g["arrivee"].isoformat(timespec="minutes"), "prix": g["prix"]},
                "dernier": {"cout": cout, "minutes": mins, "detail": detail},
                "cout": round(g["prix"] + r["prix"] + 2 * cout, 2),
                "minutes": round((g["arrivee"] - g["depart"]).total_seconds() / 60 + mins),
                "detail": f"Flixbus {g['depart']:%H:%M}→{g['arrivee']:%H:%M} ({g['prix']} €) vers {nom}, + {detail} ; "
                          f"retour {r['depart']:%H:%M} ({r['prix']} €), à caler sur l'heure d'arrivée du vol",
                "estimation": False,
                "lien": "https://shop.flixbus.fr/search?" + urlencode({"departureCity": origine, "arrivalCity": arret,
                                                                       "rideDate": aller.strftime("%d.%m.%Y"), "adult": 1})}
    return None


def rechercher(p):
    o = lieu(p["depart"])
    deps = aeroports_depart(o, float(p.get("rayon", RAYON_KM)), int(p.get("nb", NB_AEROPORTS)))
    arrs = aeroports_arrivee(p["arrivee"])
    if not deps or not arrs:
        raise ValueError("Aucun aéroport trouvé au départ ou à l'arrivée")
    # période : le séjour entier (départ + durée) doit tenir entre « du » et « au »
    du = date.fromisoformat(p["du"])
    jours = int(p.get("jours", 10))
    au = date.fromisoformat(p.get("au") or (du + timedelta(jours)).isoformat())
    if au < du + timedelta(jours):
        raise ValueError(f"La période est plus courte que le séjour ({jours} jours)")
    n_dates = (au - du).days - jours + 1
    pas = max(1, -(-n_dates // max(1, BUDGET // (len(deps) * len(arrs)))))  # arrondi au-dessus
    departs = [du + timedelta(n) for n in range(0, n_dates, pas)]
    taches = [(d["iata"], a["iata"], x, x + timedelta(jours)) for d in deps for a in arrs for x in departs]
    if len(taches) > MAX_REQUETES:
        raise ValueError(f"{len(taches)} recherches de vols : trop pour Google (max {MAX_REQUETES}). "
                         "Réduis la période ou précise la destination (une ville plutôt qu'un pays).")

    scanner.REFUS.clear()
    try:
        with ThreadPoolExecutor(3) as ex:
            resultats = list(ex.map(lambda t: scanner.moins_cher(*t), taches))
    except SystemExit as e:  # scanner.fetch abandonne quand Google bloque
        raise ValueError(str(e))
    vols = [v for v in resultats if v]
    # trajets avec correspondances : Google les charge à part, la page lue ne les contient pas -> à vérifier à la main
    vides = {(t[0], t[1]): t for t, v in zip(taches, resultats) if not v}
    vides = [{"trajet": f"{o}→{d}", "lien": f"{scanner.URL}?{urlencode(scanner.params(*t))}"} for (o, d), t in vides.items()]

    par_iata = {a["iata"]: a for a in deps + arrs}
    avec_voiture = p.get("voiture") == "1"  # par défaut on part sans voiture : train et car seulement
    fixes = {d["iata"]: [x for x in (voiture(o, d, jours, float(p.get("eur_km", EUR_KM)), float(p.get("parking", PARKING)))
                                     if avec_voiture else None, train(o, d)) if x] for d in deps}

    def complete(v):
        aller, retour = date.fromisoformat(v["aller"]), date.fromisoformat(v["retour"])
        b = bus(o, par_iata[v["origine"]], aller, retour, v["heure"])
        return {**v, "origine_nom": par_iata[v["origine"]]["nom"], "destination_nom": par_iata[v["destination"]]["nom"],
                "km": par_iata[v["origine"]]["km"], "acces": fixes[v["origine"]] + ([b] if b else []),
                "lien_transport": "https://www.google.com/maps/dir/?api=1&travelmode=transit"
                                  f"&origin={o['lat']},{o['lon']}&destination={par_iata[v['origine']]['lat']},{par_iata[v['origine']]['lon']}"}

    with ThreadPoolExecutor(4) as ex:
        vols = list(ex.map(complete, vols))
    return {"origine": o, "aeroports_depart": [f"{d['iata']} ({d['km']} km)" for d in deps],
            "aeroports_arrivee": [a["iata"] for a in arrs], "requetes": len(taches), "pas": pas, "refus": len(scanner.REFUS),
            "vols": vols, "vides": vides}


# Compagnies : (domaine officiel, résumé bagages ou None). Un résumé n'est écrit que si plusieurs sites concordent.
# ponytail: résumés relevés sur des sites tiers (pas de page officielle lisible), à confirmer ; lien officiel dans tous les cas.
COMPAGNIES = {
    "Wizz Air": ("wizzair.com", "Petit sac sous le siège gratuit (40 x 30 x 20 cm, 10 kg). Bagage cabine 55 x 40 x 23 cm "
                                "(option Priority) et bagage en soute : payants."),
    "Ryanair": ("ryanair.com", "Petit sac sous le siège gratuit (40 x 30 x 20 cm). Bagage cabine 10 kg (55 x 40 x 20 cm) "
                               "avec l'option Priority ; soute payante."),
    "Pegasus": ("flypgs.com", "Cabine : 1 bagage jusqu'à 8 kg. Soute : aucune en tarif Basic, 20 kg en Essentials à "
                              "l'international."),
    "easyJet": ("easyjet.com", None), "Transavia": ("transavia.com", None), "Vueling": ("vueling.com", None),
    "Air France": ("airfrance.fr", None), "Lufthansa": ("lufthansa.com", None), "Aegean": ("aegeanair.com", None),
    "SunExpress": ("sunexpress.com", None), "AJet": ("ajet.com", None), "Air Serbia": ("airserbia.com", None),
    "Tap Air Portugal": ("flytap.com", None), "Royal Air Maroc": ("royalairmaroc.com", None),
    "Turkish Airlines": ("turkishairlines.com", None), "Air Cairo": ("aircairo.com", None),
    "Royal Jordanian": ("rj.com", None),
}


def recherche_google(q):
    return "https://www.google.com/search?q=" + quote(q)


def bagages(compagnies):
    sortie = []
    for nom in dict.fromkeys(c.strip() for c in compagnies.split("/") if c.strip()):
        cle = max((k for k in COMPAGNIES if k.lower() in nom.lower()), key=len, default=None)
        domaine, resume = COMPAGNIES[cle] if cle else (None, None)
        sortie.append({"nom": nom, "resume": resume,
                       "liens": [{"texte": "Page bagages officielle", "url": recherche_google(f"site:{domaine} bagages")}
                                 if domaine else {"texte": "Chercher la politique bagages", "url": recherche_google(f"{nom} bagages cabine soute")},
                                 *([{"texte": "Site de la compagnie", "url": f"https://{domaine}"}] if domaine else [])]})
    return sortie


def fiche_aeroport(iata, nom_vol):
    a = next((x for x in aeroports() if x["iata"] == iata), None)
    nom = a["nom"] if a else nom_vol
    return {"iata": iata, "nom": nom, "ville": a["ville"] if a else "",
            "liens": [{"texte": "Site de l'aéroport", "url": a["site"] if a and a["site"] else recherche_google(f"{nom} aéroport site officiel")},
                      {"texte": "Infos pratiques (accès, bagages, horaires)", "url": recherche_google(f"{nom} aéroport accès enregistrement bagages")}]}


def iso(dt):
    return dt.isoformat(timespec="minutes") if dt else None


def etapes_vol(segments, compagnies):
    etapes = []
    for i, s in enumerate(segments):
        if i:
            prec = segments[i - 1]
            etapes.append({"mode": "attente", "de": prec["vers_nom"], "minutes": round(
                (datetime.fromisoformat(s["depart"]) - datetime.fromisoformat(prec["arrivee"])).total_seconds() / 60),
                "texte": f"Escale à {prec['vers_nom']} ({prec['vers']})"})
        etapes.append({"mode": "vol", "de": f"{s['de_nom']} ({s['de']})", "vers": f"{s['vers_nom']} ({s['vers']})",
                       "depart": s["depart"], "arrivee": s["arrivee"], "minutes": s["minutes"],
                       "texte": f"{compagnies} · {s['avion']} · heures locales"})
    return etapes


def detail(p):
    """Itinéraire complet d'une carte : accès aller, vols, accès retour, liens à vérifier, bagages, aéroports."""
    o, v, mode = lieu(p["depart"]), p["vol"], p["mode"]
    apt = {x["iata"]: x for x in aeroports()}
    a, d = apt[v["origine"]], apt[v["destination"]]
    aller, retour = date.fromisoformat(v["aller"]), date.fromisoformat(v["retour"])
    decollage = datetime.fromisoformat(v["segments"][0]["depart"])
    marge = 120  # présence conseillée à l'aéroport avant le décollage, en minutes
    # ponytail: le retour n'est pas lu dans la page A/R ; on prend le vol le moins cher du jour en aller simple (indicatif)
    vr = scanner.moins_cher(d["iata"], a["iata"], retour)
    atterrissage = datetime.fromisoformat(vr["segments"][-1]["arrivee"]) if vr else None
    google = f"https://www.google.com/maps/dir/?api=1&travelmode=transit&origin={o['lat']},{o['lon']}&destination={a['lat']},{a['lon']}"
    liens = [{"texte": "Vol aller-retour sur Google Flights", "url": v["lien"]},
             {"texte": f"Vol retour seul ({d['iata']} → {a['iata']}) sur Google Flights",
              "url": f"{scanner.URL}?{urlencode(scanner.params(d['iata'], a['iata'], retour))}"}]
    hypotheses = [f"Présence conseillée à l'aéroport {marge // 60} h avant le décollage."]
    avant, apres_vol = [], []  # étapes avant le vol aller, après le vol retour

    if mode == "train":
        gare, lat, lon, cout, mins, texte = ACCES[a["iata"]]["train"]
        prix = prix_train(o["nom"], gare)
        duree = round(km((o["lat"], o["lon"]), (lat, lon)) / 150 * 60 + 20)
        arrivee_gare = decollage - timedelta(minutes=marge + mins)
        trajet = {"mode": "train", "de": o["nom"], "vers": gare.title(), "minutes": duree, "estimation": True,
                  "prix": f"{prix[0]:.0f} à {prix[1]:.0f} € l'aller (fourchette open data, 2de classe, tarif normal)",
                  "texte": f"Horaires non disponibles gratuitement : choisis un train arrivant avant {arrivee_gare:%H:%M}"
                           f" ({arrivee_gare:%d/%m}). Durée estimée."}
        avant = [trajet, {"mode": "navette", "de": gare.title(), "vers": a["nom"], "minutes": mins, "prix": f"{cout:g} €", "texte": texte}]
        liens += [{"texte": "Horaires et prix du train (SNCF Connect)", "url": "https://www.sncf-connect.com/"},
                  {"texte": "Itinéraire en transports en commun (Google Maps)", "url": google}]
        premier = f"après {(atterrissage + timedelta(minutes=45 + mins)):%H:%M}" if atterrissage else "à caler sur l'heure d'atterrissage"
        apres_vol = [{"mode": "navette", "de": a["nom"], "vers": gare.title(), "minutes": mins, "prix": f"{cout:g} €", "texte": texte},
              {"mode": "train", "de": gare.title(), "vers": o["nom"], "minutes": duree, "estimation": True,
               "prix": trajet["prix"], "texte": f"Horaires non disponibles gratuitement : prends un train au départ de la gare {premier}."}]
        hypotheses.append("Durée du train estimée à 150 km/h + 20 min, prix = fourchette open data (pas un tarif réservable).")
    elif mode == "bus":
        b = bus(o, a, aller, retour, v["heure"])
        if not b:
            raise ValueError("Plus de car disponible pour ces dates")
        dn = b["dernier"]
        avant = [{"mode": "bus", "de": o["nom"], "vers": b["arret"], "depart": b["alle"]["depart"], "arrivee": b["alle"]["arrivee"],
               "prix": f"{b['alle']['prix']} €", "texte": "Flixbus (prix réel)"}]
        if dn["minutes"] or dn["cout"]:
            avant.append({"mode": "navette", "de": b["arret"], "vers": a["nom"], "minutes": dn["minutes"],
                       "prix": f"{dn['cout']:g} €", "texte": dn["detail"]})
        trajets = flix_trajets(b["arret_id"], b["origine_id"], retour)
        if atterrissage:
            apres = atterrissage + timedelta(minutes=45 + dn["minutes"])
            trajets = [t for t in trajets if t["depart"] >= apres]
        if trajets:
            t = min(trajets, key=lambda t: (t["prix"], t["depart"]))
            apres_vol = ([{"mode": "navette", "de": a["nom"], "vers": b["arret"], "minutes": dn["minutes"], "prix": f"{dn['cout']:g} €",
                    "texte": dn["detail"]}] if dn["minutes"] or dn["cout"] else [])
            apres_vol.append({"mode": "bus", "de": b["arret"], "vers": o["nom"], "depart": iso(t["depart"]), "arrivee": iso(t["arrivee"]),
                       "prix": f"{t['prix']} €", "texte": "Flixbus (prix réel), le moins cher après l'atterrissage"})
        else:
            apres_vol = [{"mode": "attente", "de": a["nom"], "texte": "Aucun car Flixbus ce jour-là après l'atterrissage : "
                   "prévoir un train ou une nuit sur place."}]
        liens.append({"texte": "Horaires et prix Flixbus (aller)", "url": b["lien"]})
    else:
        r = route(o["lat"], o["lon"], a["lat"], a["lon"])
        if not r:
            raise ValueError("Itinéraire en voiture introuvable")
        eur_km, parking = float(p.get("eur_km", EUR_KM)), float(p.get("parking", PARKING))
        jours = (retour - aller).days
        base = {"mode": "voiture", "minutes": round(r[1]), "estimation": True}
        avant = [{**base, "de": o["nom"], "vers": a["nom"], "prix": f"{r[0] * eur_km:.0f} € de carburant/péage",
               "texte": f"{r[0]:.0f} km. Parking à l'aéroport : {parking:g} €/jour x {jours} j = {parking * jours:.0f} €."}]
        apres_vol = [{**base, "de": a["nom"], "vers": o["nom"], "prix": f"{r[0] * eur_km:.0f} € de carburant/péage", "texte": f"{r[0]:.0f} km."}]
        liens.append({"texte": "Itinéraire en voiture (Google Maps)",
                      "url": f"https://www.google.com/maps/dir/{o['lat']},{o['lon']}/{a['lat']},{a['lon']}"})

    if vr:
        hypotheses.append("Vol retour = le moins cher du jour en aller simple (indicatif) : le prix aller-retour de la carte "
                          "peut correspondre à un autre vol du même jour.")
        compagnies = f"{v['compagnies']} / {vr['compagnies']}"
        retour_vol = etapes_vol(vr["segments"], vr["compagnies"])
    else:
        compagnies, retour_vol = v["compagnies"], [{"mode": "vol", "de": f"{d['nom']} ({d['iata']})", "vers": f"{a['nom']} ({a['iata']})",
            "texte": "Horaires du vol retour non lus : voir le lien « Vol retour seul »."}]
    codes = dict.fromkeys([s["de"] for s in v["segments"]] + [s["vers"] for s in v["segments"]] + [x for s in (vr["segments"] if vr else []) for x in (s["de"], s["vers"])])
    noms = {s["de"]: s["de_nom"] for s in v["segments"]} | {s["vers"]: s["vers_nom"] for s in v["segments"]}
    return {"aller": [*avant, {"mode": "attente", "de": a["nom"], "arrivee": iso(decollage - timedelta(minutes=marge)),
                            "texte": f"Être à l'aéroport au plus tard à {decollage - timedelta(minutes=marge):%H:%M} (enregistrement, bagages, sécurité)."},
                      *etapes_vol(v["segments"], v["compagnies"])],
            "retour": [*retour_vol, *apres_vol], "liens": liens, "hypotheses": hypotheses,
            "bagages": bagages(compagnies), "aeroports": [fiche_aeroport(c, noms.get(c, c)) for c in codes]}


class Handler(BaseHTTPRequestHandler):
    def envoyer(self, code, corps, type_="application/json; charset=utf-8"):
        corps = corps.encode() if isinstance(corps, str) else corps
        self.send_response(code)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self.envoyer(200, (ICI / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path != "/api/recherche":
            return self.envoyer(404, '{"erreur": "introuvable"}')
        try:
            self.envoyer(200, json.dumps(rechercher(dict(parse_qsl(u.query))), ensure_ascii=False))
        except (ValueError, KeyError) as e:
            self.envoyer(400, json.dumps({"erreur": str(e)}, ensure_ascii=False))

    def do_POST(self):
        if urlparse(self.path).path != "/api/detail":
            return self.envoyer(404, '{"erreur": "introuvable"}')
        try:
            corps = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self.envoyer(200, json.dumps(detail(corps), ensure_ascii=False))
        except (ValueError, KeyError) as e:
            self.envoyer(400, json.dumps({"erreur": str(e)}, ensure_ascii=False))


if __name__ == "__main__":
    assert round(km((48.8566, 2.3522), (45.7640, 4.8357))) == 391  # Paris -> Lyon à vol d'oiseau
    assert norm("Saint-Malo") == "ST MALO"
    print("http://localhost:8765")
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
