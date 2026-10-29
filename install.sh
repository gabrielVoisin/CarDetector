#!/usr/bin/env bash
# Install the agent on a Raspberry Pi OS (Bookworm) cabinet. Run as root from this folder.
set -euo pipefail

apt-get update
apt-get install -y python3-venv python3-lgpio i2c-tools \
  bluez python3-dbus python3-gi python3-cryptography      # Bluetooth identification of the phones
raspi-config nonint do_i2c 0 || true
systemctl enable --now bluetooth
rfkill unblock bluetooth || true

id parking-agent >/dev/null 2>&1 || useradd --system --home /var/lib/parking-agent --shell /usr/sbin/nologin parking-agent
usermod -aG gpio,i2c,bluetooth parking-agent              # bluetooth: allowed to talk to BlueZ (org.bluez)

install -d -o parking-agent -g parking-agent -m 750 /var/lib/parking-agent
install -d -m 755 /opt/parking-agent /etc/parking-agent
cp -r agent requirements.txt /opt/parking-agent/
python3 -m venv --system-site-packages /opt/parking-agent/venv
/opt/parking-agent/venv/bin/pip install -r /opt/parking-agent/requirements.txt

if [ ! -f /etc/parking-agent/config.yaml ]; then
  install -m 600 -o parking-agent -g parking-agent config.example.yaml /etc/parking-agent/config.yaml
  echo ">>> Edit /etc/parking-agent/config.yaml (server_url, controller_key, wiring), then:"
fi
install -m 644 parking-agent.service /etc/systemd/system/parking-agent.service
systemctl daemon-reload
systemctl enable parking-agent
echo ">>> systemctl start parking-agent && journalctl -u parking-agent -f"
