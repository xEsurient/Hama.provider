#!/bin/sh
# Install the HAMA provider as a systemd service (run as root from anywhere).
#   sudo sh install/linux/install.sh            installs, runs as user 'plex' if it exists
#   sudo sh install/linux/install.sh uninstall
set -e
DIR=$(cd "$(dirname "$0")/../.." && pwd)
UNIT=/etc/systemd/system/hama-provider.service

if [ "$1" = "uninstall" ]; then
  systemctl disable --now hama-provider 2>/dev/null || true
  rm -f "$UNIT"; systemctl daemon-reload; echo "Removed hama-provider service (files in $DIR kept)."; exit 0
fi

command -v python3 >/dev/null || { echo "python3 (3.8+) is required"; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo "python3 3.8+ is required"; exit 1; }
RUN_AS=${RUN_AS:-$(id -u plex >/dev/null 2>&1 && echo plex || echo root)}
mkdir -p "$DIR/data"; chown -R "$RUN_AS" "$DIR/data"
sed -e "s#@DIR@#$DIR#g" -e "s#@USER@#$RUN_AS#g" "$DIR/install/linux/hama-provider.service" > "$UNIT"
systemctl daemon-reload
systemctl enable --now hama-provider
echo "hama-provider running as $RUN_AS. Check: systemctl status hama-provider / journalctl -u hama-provider -f"
echo "Plex: Settings > Metadata Agents > Add Provider > http://127.0.0.1:32550/tv (and /movies)"
