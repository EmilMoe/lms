#!/usr/bin/env bash
# Installerer eller opdaterer Blackboard-mocken på serveren.
#
#   sudo ./blackboard.sh --domain=blackboard.example.com --path=/mnt/<volume> \
#                        --tool-url=https://nexus.example.com --owner=deploy
#
# Blackboard Learn er lukket kode og kan ikke selvhostes, så stakken er en mock, der svarer som
# Learn's REST-API og som Anthology Developer Portal i LTI 1.3 (blackboard/mock/app.py).
# Lægger stakken i <path>/blackboard (docker-compose.yml, mock/, seed/, .env, data/).
# Opdatering: kør igen med kun --path; øvrige indstillinger læses fra <path>/blackboard/.env.
# Nøgler og id'er bevares, seed/ erstattes af testdata/blackboard.
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

TOOL_URL=""

usage() {
  sed -n '2,11p' "$0"
  echo
  echo "Blackboard-flag:"
  echo "  --tool-url=URL                Nexus' rod-URL, som mocken launcher (fx https://nexus.example.com)"
  echo "  (--email er valgfri her; mocken har ingen e-mail)"
  common_usage
}

for arg in "$@"; do
  parse_common "$arg" && continue
  case $arg in
    --tool-url=*) TOOL_URL=${arg#*=} ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "ukendt flag: $arg" ;;
  esac
done
stack="$DATA_PATH/blackboard"
env_file="$stack/.env"
load_previous "$stack" BLACKBOARD
: "${TOOL_URL:=$(env_value "$env_file" BB_MOCK_TOOL_URL)}"
: "${EMAIL:=admin@${DOMAIN:-blackboard.invalid}}"
TOOL_URL=${TOOL_URL%/}
[[ -z $TOOL_URL || $TOOL_URL =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || \
  die "ugyldig --tool-url: '$TOOL_URL' (kun rod-URL'en, fx https://nexus.example.com)"
validate_common

step "Installerer stakken i $stack"
install_stack "$REPO_DIR/blackboard" "$stack"
rm -rf "${stack:?}/seed"
cp -R "$REPO_DIR/testdata/blackboard" "$stack/seed"
chown -R "$OWNER": "$stack/seed"
mkdir -p "$stack/data"
chown 10001 "$stack/data"   # mockens bruger i containeren

step "Skriver .env"
keep() {
  local key=$1 value=$2 line
  line=$(old_value_line "$env_file" "$key")
  if [[ -n $line ]]; then echo "$line"; else echo "$key=$value"; fi
}
uuid() { cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen | tr '[:upper:]' '[:lower:]'; }
write_env "$env_file" <<ENV
# Skrevet af blackboard.sh. Indstillinger overskrives ved næste kørsel; nøgler og id'er bevares.
BLACKBOARD_DOMAIN=$DOMAIN
BLACKBOARD_ADMIN_EMAIL=$EMAIL
BB_MOCK_TOOL_URL=$TOOL_URL
$(traefik_env)

$(keep BB_MOCK_APPLICATION_ID "$(uuid)")
$(keep BB_MOCK_APPLICATION_KEY "$(uuid)")
$(keep BB_MOCK_SECRET "$(rand 24)")
$(keep BB_MOCK_DEPLOYMENT_ID "_$((RANDOM % 900 + 100))_1")
$(keep BB_MOCK_UI_PASSWORD "$(rand 12)")
$(keep BB_MOCK_BACKFILL_DAYS 30)
$(keep BB_MOCK_DAILY_CALL_LIMIT 0)
ENV

if [[ $START -eq 0 ]]; then
  echo "Filer skrevet (--no-start). Start med: cd $stack && docker compose up -d --build"
  exit 0
fi

cd "$stack"
step "Bygger og starter mocken"
as_owner docker compose build
as_owner docker compose up -d --remove-orphans --force-recreate

cat <<INFO

Blackboard-mock: https://$DOMAIN  (admin / sudo grep BB_MOCK_UI_PASSWORD $env_file)
Siden viser alt, der skal ind i Nexus. Secret: sudo grep BB_MOCK_SECRET $env_file
Logs:            cd $stack && docker compose logs -f mock
INFO
[[ -n $TOOL_URL ]] || echo "Advarsel: --tool-url er ikke sat, så knapperne til at åbne Nexus virker ikke." >&2
