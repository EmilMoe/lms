#!/usr/bin/env bash
# Installerer eller opdaterer Moodle-stakken på serveren.
#
#   sudo ./moodle.sh --domain=moodle.example.com --path=/mnt/<volume> \
#                    --email=admin@example.com --owner=deploy
#
# Lægger stakken i <path>/moodle (docker-compose.yml, .env, data/).
# Opdatering: kør igen med kun --path; øvrige indstillinger læses fra <path>/moodle/.env.
# Compose og indstillinger opdateres, kodeord bevares. Moodle installerer/opgraderer
# sig selv ved start.
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

TAG="" DEFAULT_TAG="5.3"

usage() {
  sed -n '2,10p' "$0"
  echo
  echo "Moodle-flag:"
  echo "  --tag=TAG                     oksigenia/moodle-tag (standard: værdien i .env, ellers $DEFAULT_TAG, LTS)"
  common_usage
}

for arg in "$@"; do
  parse_common "$arg" && continue
  case $arg in
    --tag=*) TAG=${arg#*=} ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "ukendt flag: $arg" ;;
  esac
done
stack="$DATA_PATH/moodle"
env_file="$stack/.env"
load_previous "$stack" MOODLE
: "${TAG:=$(env_value "$env_file" MOODLE_TAG)}"
: "${TAG:=$DEFAULT_TAG}"
validate_common

step "Installerer stakken i $stack"
install_stack "$REPO_DIR/moodle" "$stack"
mkdir -p "$stack"/data/{db,moodle,moodledata}   # MariaDB og Moodle retter selv ejerskab

step "Skriver .env"
secret() {
  local key=$1 value=$2 line
  line=$(old_value_line "$env_file" "$key")
  if [[ -n $line ]]; then echo "$line"; else echo "$key=$value"; fi
}
write_env "$env_file" <<EOF
# Skrevet af moodle.sh. Indstillinger overskrives ved næste kørsel; kodeord bevares.
MOODLE_DOMAIN=$DOMAIN
MOODLE_TAG=$TAG
MOODLE_ADMIN_EMAIL=$EMAIL
$(traefik_env)

$(secret MOODLE_DB_PASSWORD "$(rand 24)")
$(secret MARIADB_ROOT_PASSWORD "$(rand 24)")
$(secret MOODLE_ADMIN_PASSWORD "$(rand 12)")
EOF

if [[ $START -eq 0 ]]; then
  echo "Filer skrevet (--no-start). Start med: cd $stack && docker compose up -d"
  exit 0
fi

cd "$stack"
step "Starter Moodle"
as_owner docker compose pull
as_owner docker compose up -d --remove-orphans

cat <<EOF

Moodle: https://$DOMAIN  (første start installerer Moodle og tager nogle minutter)
Admin:  admin / $EMAIL
Kode:   sudo grep MOODLE_ADMIN_PASSWORD $env_file   (gælder kun den første installation)
Logs:   cd $stack && docker compose logs -f moodle
EOF
