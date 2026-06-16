#!/usr/bin/env sh
set -eu

cd /app
python -m app.bootstrap
python -m app.write_crontab
exec /usr/bin/supervisord -c /etc/supervisord.conf
