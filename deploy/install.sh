#!/usr/bin/env bash
# Install the polybot stack (paper engine + live engine + dashboard) on an
# Ubuntu VPS as three independent systemd services.
#
#   sudo ./deploy/install.sh [--dir /opt/polybot] [--user polybot] [--no-nginx]
#
# Idempotent: re-running upgrades the units, dependencies and file permissions
# without touching your configuration or data.
set -euo pipefail

INSTALL_DIR="/opt/polybot"
SERVICE_USER="polybot"
WITH_NGINX=1
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)      INSTALL_DIR="$2"; shift 2 ;;
    --user)     SERVICE_USER="$2"; shift 2 ;;
    --no-nginx) WITH_NGINX=0; shift ;;
    -h|--help)  sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo $0)"

# ---------------------------------------------------------------- packages ---
log "installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-dev build-essential \
                       sqlite3 curl ca-certificates
if [[ $WITH_NGINX -eq 1 ]]; then
  apt-get install -y -qq nginx
fi

# ------------------------------------------------------------------- user ----
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  log "creating service user: $SERVICE_USER"
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# ------------------------------------------------------------------ files ----
log "syncing application to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
if [[ "$SOURCE_DIR" != "$INSTALL_DIR" ]]; then
  # Never overwrite live configuration or data.
  tar -C "$SOURCE_DIR" \
      --exclude='.venv' --exclude='data' --exclude='__pycache__' --exclude='.git' \
      --exclude='env.txt' --exclude='env.paper.txt' --exclude='env.dashboard.txt' \
      -cf - . | tar -C "$INSTALL_DIR" -xf -
fi

mkdir -p "$INSTALL_DIR"/data/{paper/logs,paper/state,live/logs,live/state,dashboard/backups,dashboard/uploads,dashboard/certs}

# ------------------------------------------------------------------- venv ----
log "building the virtualenv"
if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# ------------------------------------------------------------------ config ---
for template in env.example.txt:env.txt env.paper.example.txt:env.paper.txt \
                env.dashboard.example.txt:env.dashboard.txt; do
  src="${template%%:*}"; dst="${template##*:}"
  if [[ ! -f "$INSTALL_DIR/$dst" && -f "$INSTALL_DIR/$src" ]]; then
    cp "$INSTALL_DIR/$src" "$INSTALL_DIR/$dst"
    warn "created $dst from $src — edit it before starting the service"
  fi
done

if [[ -f "$INSTALL_DIR/env.dashboard.txt" ]] && \
   ! grep -q '^DASHBOARD_SECRET_KEY=.\{32,\}' "$INSTALL_DIR/env.dashboard.txt"; then
  secret="$("$INSTALL_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')"
  sed -i "s|^DASHBOARD_SECRET_KEY=.*|DASHBOARD_SECRET_KEY=$secret|" "$INSTALL_DIR/env.dashboard.txt" \
    || echo "DASHBOARD_SECRET_KEY=$secret" >> "$INSTALL_DIR/env.dashboard.txt"
  log "generated a fresh DASHBOARD_SECRET_KEY"
fi

# ------------------------------------------------------------- permissions ---
log "tightening permissions"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR"
for secret_file in env.txt env.paper.txt env.dashboard.txt; do
  [[ -f "$INSTALL_DIR/$secret_file" ]] && chmod 600 "$INSTALL_DIR/$secret_file"
done
# The paper engine must not be able to read the live wallet credentials.
[[ -f "$INSTALL_DIR/env.txt" ]] && chmod 600 "$INSTALL_DIR/env.txt"

# ----------------------------------------------------------------- systemd ---
log "installing systemd units"
for unit in polybot-paper polybot-live polybot-dashboard; do
  sed -e "s|/opt/polybot|$INSTALL_DIR|g" -e "s|^User=polybot|User=$SERVICE_USER|" \
      -e "s|^Group=polybot|Group=$SERVICE_USER|" \
      "$INSTALL_DIR/deploy/systemd/$unit.service" > "/etc/systemd/system/$unit.service"
done

log "installing the sudoers rule for dashboard service control"
sed "s|^polybot ALL|$SERVICE_USER ALL|" "$INSTALL_DIR/deploy/polybot-dashboard.sudoers" \
  > /etc/sudoers.d/polybot-dashboard
chmod 0440 /etc/sudoers.d/polybot-dashboard
if ! visudo -c -q; then
  rm -f /etc/sudoers.d/polybot-dashboard
  die "sudoers validation failed; the rule was removed. Service control will not work."
fi

systemctl daemon-reload
systemctl enable polybot-dashboard.service >/dev/null
systemctl enable polybot-paper.service >/dev/null
# Live is enabled but NOT started: arming real trading is always a manual act.
systemctl enable polybot-live.service >/dev/null

# ------------------------------------------------------------------- nginx ---
if [[ $WITH_NGINX -eq 1 && ! -f /etc/nginx/sites-available/polybot ]]; then
  log "installing the nginx site (edit server_name, then run certbot)"
  cp "$INSTALL_DIR/deploy/nginx/polybot-dashboard.conf" /etc/nginx/sites-available/polybot
  ln -sf /etc/nginx/sites-available/polybot /etc/nginx/sites-enabled/polybot
  warn "nginx site installed but NOT enabled in DNS/TLS yet:"
  warn "  1. edit /etc/nginx/sites-available/polybot  (server_name)"
  warn "  2. sudo certbot --nginx -d your.domain"
  warn "  3. sudo nginx -t && sudo systemctl reload nginx"
fi

# ------------------------------------------------------------------- start ---
log "starting the dashboard"
systemctl restart polybot-dashboard.service
sleep 2
systemctl --no-pager --lines=0 status polybot-dashboard.service || true

cat <<EOF

$(log 'installation complete')

  Configuration     $INSTALL_DIR/env.txt            (live wallet — keep it 600)
                    $INSTALL_DIR/env.paper.txt      (paper engine)
                    $INSTALL_DIR/env.dashboard.txt  (dashboard)

  Services          systemctl status polybot-dashboard
                    systemctl start  polybot-paper
                    systemctl start  polybot-live     <-- real funds

  Logs              journalctl -u polybot-dashboard -f
                    $INSTALL_DIR/data/live/logs/

  First login       the initial admin password is printed once in the dashboard
                    journal on first start:
                      journalctl -u polybot-dashboard | grep 'INITIAL ADMIN'
                    Change it immediately from the account menu.

  Next steps        1. Put your markets in env.txt / env.paper.txt (MARKETS=...)
                    2. Start the paper engine and confirm it trades
                    3. Only then consider ENABLE_LIVE_TRADING=true

EOF
