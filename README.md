# trajet-scanner

Interface web locale pour trouver le voyage le moins cher vers une destination : vol Google Flights le moins cher
**plus** le trajet jusqu'à l'aéroport de départ (train, car Flixbus, voiture en option), aller et retour.

- Départ : n'importe quelle ville. Destination : pays, ville ou codes IATA.
- Période de dates, durée du séjour et pas entre deux départs calculé automatiquement.
- Tri (prix, durée, distance…) et filtre par mode d'accès, côté navigateur.

## Lancer

```sh
./lancer.command
```

Crée `.venv` au premier lancement, installe `fast-flights` et `curl_cffi`, puis ouvre http://localhost:8765.

## Sources

| Donnée | Source |
|---|---|
| Vols | Google Flights, via [fast-flights](https://pypi.org/project/fast-flights/) |
| Car | API publique Flixbus (prix réels) |
| Train | open data SNCF, fourchettes de prix TGV INOUI / OUIGO (estimation) |
| Voiture | OSRM × coût au km + parking |
| Lieux | OpenStreetMap (Nominatim) |
| Aéroports | [OurAirports](https://ourairports.com/data/), téléchargé au premier lancement dans `cache/` |

## Limites

- Google refuse les requêtes au-delà de quelques centaines d'affilée (erreur 13) : le nombre de refus est affiché.
- Les vols avec correspondances depuis les petits aéroports ne sont pas lisibles : un lien Google Flights est fourni.
- Les prix de train sont des estimations, pas des tarifs réservables. Le dernier trajet jusqu'à l'aéroport n'est renseigné
  que pour NTE, RNS, CDG, ORY, BVA et LYS (table `ACCES` dans `app.py`).
- BlaBlaCar n'est pas intégré : pas d'API publique, et le site bloque les robots.
- Scraping non officiel de Google Flights : usage personnel, à vos risques vis-à-vis de leurs conditions d'utilisation.
