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

## Capteur de présence caméra (`plate_sensor/`)

`plate_sensor/reader.py` fait de la caméra du Raspberry Pi le capteur de présence **CP-001** de l’armoire
CTRL-01 (place P1 / EXT / 05). L’IA (fast-alpr) lit les plaques ; la place est **occupée** tant qu’une plaque
est confirmée (vue dans 2 des 5 dernières images analysées), puis encore `--occupied-hold` secondes (5 s).

- **Vers l’agent** : `POST http://127.0.0.1:8090/detectors/CP-001 {"occupied": true|false}` à chaque
  changement et toutes les 10 s (`--detector-url`, `--detector-heartbeat`; jeton éventuel : `DETECTOR_TOKEN`).
- **Vers le site** (page *Appareils de contrôle*, clic sur CP-001), HTTP sur `0.0.0.0:8081` :
  - `GET /stream.mjpg` : vue en direct (~30 images/s) avec les cadres des plaques
    (vert = lue avec assez de confiance, orange = trop incertaine) ;
  - `GET /status.json` : `{"device_code", "plate", "confidence", "occupied", "plate_since", "analysed_fps",
    "stream_fps", "cpu_temp_c", "thermal_state", "rotate", "updated_at"}`.

  Les deux demandent le jeton partagé `SENSOR_TOKEN` (en-tête `X-Sensor-Token` ou `?token=`), sauf depuis
  127.0.0.1. Le serveur (conteneur `api`) lit le même fichier `plate_sensor/sensor.env`.
- **Protection thermique** : au plus `--max-fps` (3) images analysées par seconde ; au-dessus de
  `--hot-temp` (75 °C) 1 image/s, au-dessus de `--pause-temp` (82 °C) analyse en pause (la vidéo continue,
  la place garde son dernier état) jusqu’à `--resume-temp` (70 °C).

Installation (utilisateur normal, pas root) :

```bash
cd ~/intelpark/CarDetector/plate_sensor
python3 -m venv --system-site-packages .venv && .venv/bin/pip install -r requirements.txt   # picamera2 : apt
cp reader.env.example reader.env                                   # options (--rotate, --max-fps, ...)
(umask 077; echo "SENSOR_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" > sensor.env)
cp parking-reader.service ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user enable --now parking-reader
journalctl --user -u parking-reader -f      # une ligne toutes les 10 s : images analysées, CPU, place
```

Test sans caméra sur une photo : `.venv/bin/python reader.py --image voiture.jpg`.
`reader.env` et `sensor.env` restent sur le Pi (ignorés par git). Une seule application peut utiliser la
caméra à la fois.

## Simulateur de capteurs de présence (`sensor_simulator/`)

Une page web pour garer et faire partir des voitures à la main, afin de tester le tableau de bord.
Le simulateur joue les capteurs CP-002 à CP-056 de l'armoire CTRL-01 (toutes les places P1 / Extérieur
sauf la 05, qui est la vraie caméra CP-001) et envoie leurs mesures au serveur par le réseau, comme une
vraie armoire (`POST /api/v1/device/readings`). L'agent réel ne voit pas ces capteurs (adresse `sim:`).

```bash
# sur le serveur : activer les capteurs simulés (et "off" pour les retirer, leurs places redeviennent libres)
docker compose -f docker-compose.yml -f deploy/raspberry-pi/docker-compose.pi.yml exec -T api python -m app.cli simulated-sensors on
# sur le Pi
cp sensor_simulator/simulator.env.example sensor_simulator/simulator.env
python3 sensor_simulator/simulator.py          # puis ouvrir http://<pi>:8082
```

La page propose : clic sur une place (avec la plaque saisie, ou une plaque au hasard), « Tout libérer »,
« Remplir à 50 % », « Tout remplir » et un trafic automatique (une arrivée ou un départ toutes les 3 à 8 s).
Service utilisateur : `sensor_simulator/sensor-simulator.service`. Python 3 seul, sans dépendance.
