#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
umask 077

command -v docker >/dev/null 2>&1 || { echo 'Docker is required.' >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo 'OpenSSL is required to initialize local passwords.' >&2; exit 1; }
if [ ! -f .env ]; then
    : > .env
fi

ensure_secret() {
    key=$1
    # Fill missing/empty template entries; retain every configured value.
    if ! grep -q "^${key}=." .env; then
        value=$(openssl rand -hex 24)
        printf '\n%s=%s\n' "$key" "$value" >> .env
    fi
}
ensure_secret MYSQL_PASSWORD
ensure_secret MYSQL_ROOT_PASSWORD
ensure_secret JWT_SECRET_KEY
ensure_secret GRAFANA_ADMIN_PASSWORD
ensure_secret ELASTIC_PASSWORD
ensure_secret KIBANA_SYSTEM_PASSWORD
ensure_secret LOGSTASH_WRITER_PASSWORD
ensure_secret KIBANA_SECURITY_ENCRYPTION_KEY
ensure_secret KIBANA_SAVED_OBJECTS_ENCRYPTION_KEY
ensure_secret KIBANA_REPORTING_ENCRYPTION_KEY
chmod 600 .env

echo 'Starting the application stack (ELK is included when COMPOSE_PROFILES=elk)...'
echo 'Local credentials are stored in .env; existing backend/.env supplies model settings.'
docker compose -f docker-compose.yml up -d --build --wait --wait-timeout 360 "$@"
case ",${COMPOSE_PROFILES:-}," in
    *,elk,*) docker compose -f docker-compose.yml run --rm --no-deps elk-views ;;
esac
