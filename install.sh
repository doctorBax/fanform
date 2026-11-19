#!/usr/bin/env bash
# Fanform: one-command bootstrap for Debian / Proxmox VE.
# Usage (once hosted in the GitHub repository):
#   curl -fsSL https://raw.githubusercontent.com/doctorBax/fanform/main/install.sh | sudo bash
set -Eeuo pipefail
umask 077

REPO_URL="${FANFORM_REPO_URL:-https://github.com/doctorBax/fanform.git}"
DEFAULT_PORT=8796

say() { printf '\n\033[1;36m[fanform]\033[0m %s\n' "$*"; }
fail() { printf '\n[fanform] ERROR: %s\n' "$*" >&2; exit 1; }

[[ ${EUID:-$(id -u)} -eq 0 ]] || fail 'Запустите от root: curl ... | sudo bash'
command -v apt-get >/dev/null || fail 'Поддерживаются только Debian/Proxmox с apt-get.'
command -v systemctl >/dev/null || fail 'Для установки нужен systemd.'
[[ -d /run/systemd/system ]] || fail 'systemd не запущен. Выполняйте установку на хосте Proxmox.'

# Do not silently fight existing fan-management daemons.
for unit in fancontrol.service coolercontrold.service; do
  if systemctl is-active --quiet "$unit"; then
    fail "Активен другой контроллер вентиляторов ($unit). Остановите его до установки Fanform."
  fi
done

work="$(mktemp -d -t fanform-install.XXXXXXXX)"
cleanup() { rm -rf -- "$work"; }
trap cleanup EXIT

# Local archive installs are supported for testing. Piped installs fetch the full git repository.
src=''
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  candidate="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || :)"
  if [[ -n "$candidate" && -f "$candidate/fanform/server.py" && -f "$candidate/systemd/fanform.service" ]]; then
    src="$candidate"
  fi
fi

say 'Проверяю зависимости…'
missing=()
for pkg in git python3 ca-certificates; do
  if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -qx 'install ok installed'; then
    missing+=("$pkg")
  fi
done
if (( ${#missing[@]} > 0 )); then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends "${missing[@]}"
fi

if [[ -z "$src" ]]; then
  say 'Скачиваю исходники Fanform из GitHub…'
  if ! GIT_TERMINAL_PROMPT=0 git clone --depth 1 --single-branch "$REPO_URL" "$work/source"; then
    fail "Не удалось скачать $REPO_URL. Убедитесь, что репозиторий опубликован и доступен."
  fi
  src="$work/source"
fi

[[ -f "$src/fanform/server.py" && -f "$src/scripts/fanformctl" ]] || fail 'Неполный пакет Fanform.'
say 'Проверяю программу…'
/usr/bin/python3 -m compileall -q "$src/fanform"
if [[ -d "$src/tests" ]]; then
  (cd "$src" && /usr/bin/python3 -m unittest discover -s tests -q) || fail 'Встроенные тесты не пройдены; установка отменена.'
fi

# For the first installation, automatically pick a free port instead of failing on a busy default.
# Existing installations always keep their saved port.
chosen_port="$DEFAULT_PORT"
if [[ ! -e /etc/fanform/config.json ]]; then
  chosen_port="$(/usr/bin/python3 - <<'PYPORT'
import socket
for port in range(8796, 8897):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(('0.0.0.0', port))
        except OSError:
            continue
        print(port)
        break
else:
    raise SystemExit('Нет свободного порта в диапазоне 8796–8896')
PYPORT
)"
fi

# Preserve a previous config so repeated runs are non-destructive.
mkdir -p /etc/fanform /var/lib/fanform
chmod 700 /etc/fanform /var/lib/fanform
if [[ ! -e /etc/fanform/config.json ]]; then
  say 'Генерирую пароль администратора…'
  FANFORM_ASSIGNED_PORT="$chosen_port" /usr/bin/python3 - <<'PY'
import hashlib, json, os, secrets
password = secrets.token_urlsafe(24)
salt = secrets.token_hex(16)
digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 250000).hex()
path = '/etc/fanform/config.json'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    json.dump({'bind':'0.0.0.0','port':int(os.environ['FANFORM_ASSIGNED_PORT']),'password':{'salt':salt,'hash':digest},
               'calibrations':{},'groups':[]},stream,indent=2)
print('\n' + '='*67)
print('  ПАРОЛЬ FANFORM (сохраните; повторно не показывается):', password)
print('='*67 + '\n')
PY
fi

say 'Устанавливаю systemd-службы и CLI…'
# Install the new revision without copying temporary caches. Do not touch other Proxmox services.
systemctl stop fanform.service 2>/dev/null || true
mkdir -p /opt/fanform
cp -a -- "$src/." /opt/fanform/
install -m 0755 "$src/scripts/fanformctl" /usr/local/bin/fanformctl
install -m 0644 "$src/systemd/fanform.service" /etc/systemd/system/fanform.service
install -m 0644 "$src/systemd/fanform-update.service" /etc/systemd/system/fanform-update.service
install -m 0644 "$src/systemd/fanform-update.timer" /etc/systemd/system/fanform-update.timer
systemctl daemon-reload
systemctl enable --now fanform.service
if ! systemctl is-active --quiet fanform.service; then
  journalctl -u fanform.service -n 40 --no-pager >&2 || :
  fail 'Веб-панель не запустилась; подробности в журнале выше.'
fi

# Explicit opt-in: automatic root-running code updates are a security decision.
if [[ "${FANFORM_AUTO_UPDATE:-0}" == 1 ]]; then
  if [[ -d /opt/fanform/.git ]]; then
    systemctl enable --now fanform-update.timer
    say 'Автообновления включены по запросу.'
  else
    say 'Автообновления недоступны без Git checkout.'
  fi
fi
port="$(/usr/bin/python3 - <<'PY'
import json
print(json.load(open('/etc/fanform/config.json'))['port'])
PY
)"

printf '\n\033[1;32mУстановка завершена.\033[0m\n'
printf 'Веб-панель: http://IP-ВАШЕГО-PROXMOX:%s\n' "$port"
printf 'CLI: sudo fanformctl\n'
printf 'Автообновления по умолчанию выключены. Меню CLI позволяет включить их, сменить порт и удалить Fanform.\n'
printf 'Безопасность: не открывайте HTTP-порт в интернет, используйте только доверенную локальную сеть.\n'
