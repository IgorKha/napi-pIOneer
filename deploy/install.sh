#!/bin/sh
# Идемпотентная установка napi-pioneer на плате (запускать от root на плате).
# Код и данные — в /mount/rfs/rw/.pioneer (переживает soft_reset и OTA, spec 11.1).
# В системе ставятся только: systemd-юнит и nginx-сниппет — их soft_reset стирает,
# повторный запуск этого скрипта всё восстанавливает (данные целы).
set -eu

ROOT=/mount/rfs/rw/.pioneer
APP=$ROOT/app
DATA=$ROOT/data
UNIT=/etc/systemd/system/pioneer.service
SNIPPET=/etc/nginx/snippets/pioneer.conf

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mОШИБКА: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "нужен root"
[ -d "$APP/backend/pioneer" ] || die "нет кода в $APP (сначала make deploy)"

say "проверка зависимостей Python на плате"
python3 -c "import fastapi, uvicorn, yaml, gpiod, smbus2, serial, spidev" \
    || die "не хватает системных пакетов Python (ожидается NapiLinux 0.3.x)"

# Данные живут в dot-каталоге, потому что /init при soft_reset делает `rm -rf $ROOT_RWMOUNT/*`.
if ! grep -q 'rm -rf \$ROOT_RWMOUNT/\*' /init 2>/dev/null; then
    echo "ВНИМАНИЕ: /init изменился — проверьте, переживает ли $ROOT сброс к заводским" >&2
fi

say "данные: $DATA"
mkdir -p "$DATA" "$ROOT/bin"
if [ ! -f "$DATA/config.toml" ]; then
    cat > "$DATA/config.toml" <<EOF
[core]
hal = "linux"
data_dir = "$DATA"
port = 8100
EOF
fi

# Команда для обслуживания: pioneer set-password и т.п.
cat > "$ROOT/bin/pioneer" <<EOF
#!/bin/sh
export PYTHONPATH=$APP/backend PIONEER_CONFIG=$DATA/config.toml
exec python3 -m pioneer "\$@"
EOF
chmod 755 "$ROOT/bin/pioneer"

say "systemd: $UNIT"
install -m 644 "$APP/deploy/pioneer.service" "$UNIT"
systemctl daemon-reload
systemctl enable pioneer.service >/dev/null

say "nginx: $SNIPPET"
had_snippet=0
[ -f "$SNIPPET" ] && cp "$SNIPPET" "$SNIPPET.bak" && had_snippet=1
install -m 644 "$APP/deploy/nginx-pioneer.conf" "$SNIPPET"
if ! nginx -t 2>/tmp/pioneer-nginx-test.log; then
    # не ломаем штатную админку: откатываем сниппет
    if [ $had_snippet = 1 ]; then mv "$SNIPPET.bak" "$SNIPPET"; else rm -f "$SNIPPET"; fi
    cat /tmp/pioneer-nginx-test.log >&2
    die "nginx -t не прошёл — сниппет откачен, nginx не перезагружался"
fi
rm -f "$SNIPPET.bak"
systemctl reload nginx

say "запуск сервиса (старт ≈ 15 с)"
if ! systemctl is-active -q pioneer.service && curl -sf http://127.0.0.1:8100/pioneer/api/health >/dev/null; then
    die "порт 8100 занят чужим процессом (пробный запуск из /tmp?) — остановите его"
fi
systemctl restart pioneer.service
i=0
until curl -sf http://127.0.0.1:8100/pioneer/api/health >/dev/null; do
    i=$((i + 1))
    if [ $i -ge 60 ] || ! systemctl is-active -q pioneer.service; then
        journalctl -u pioneer.service -n 30 --no-pager >&2
        die "сервис не поднялся"
    fi
    sleep 1
done
ip=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
say "работает: https://${ip:-$(hostname)}/pioneer/"

if [ ! -f "$DATA/auth.json" ]; then
    echo
    echo "Пароль входа ещё не задан. Задайте его:"
    echo "    $ROOT/bin/pioneer set-password"
fi
