#!/bin/sh
# Идемпотентная установка на плате. Восстанавливает юнит и nginx-сниппет после soft_reset/OTA.
# ЧЕРНОВИК: подключение сниппета в штатный server{} ещё не проверено на плате.
set -eu
ROOT=/mount/rfs/rw/.pioneer
APP=$ROOT/app

mkdir -p "$ROOT/data"

# Данные живут в dot-каталоге, потому что /init при soft_reset делает `rm -rf $ROOT_RWMOUNT/*`.
# Если OTA поменяет /init — предупредить (spec 11.1).
if ! grep -q 'rm -rf \$ROOT_RWMOUNT/\*' /init 2>/dev/null; then
    echo "WARNING: /init изменился — проверьте, переживает ли $ROOT сброс к заводским" >&2
fi

install -m 644 "$APP/deploy/pioneer.service" /etc/systemd/system/pioneer.service
install -m 644 "$APP/deploy/nginx-pioneer.conf" /etc/nginx/snippets/pioneer.conf
# TODO: include snippets/pioneer.conf в штатный server{} — посмотреть, как устроен конфиг napi

systemctl daemon-reload
systemctl enable pioneer.service
systemctl restart pioneer.service
nginx -t && nginx -s reload
