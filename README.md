# CarDetector

Agent de l’armoire de contrôle Intel Park, sur Raspberry Pi : il lit et traite les signaux des capteurs de
présence (GPIO ou détecteurs Ethernet), surveille les caméras et les barrières, et sert l’identification
Bluetooth des téléphones. Il envoie tout au serveur central
([IntelparcAdminWebPage](https://github.com/gabrielVoisin/IntelparcAdminWebPage)).

```bash
pip install -r requirements.txt
python -m agent -c config.simulator.yaml -v        # sans matériel
curl -X POST localhost:8090/simulate/bay/EXT/05 -H 'Content-Type: application/json' -d '{"occupied": true}'
python -m unittest discover -s tests
```

Installation sur une armoire (en root) : `./install.sh`, puis éditer `/etc/parking-agent/config.yaml`.
Prototype tout sur un seul Pi : voir `deploy/raspberry-pi/install-all-in-one.sh` dans IntelparcAdminWebPage,
qui attend ce dépôt cloné à côté du sien.

Seuls les capteurs configurés sur le serveur (avec une adresse de câblage) sont envoyés à l’agent.
