#!/usr/bin/env bash
# Installerer eller opdaterer Canvas-stakken på serveren.
#
#   sudo ./canvas.sh --domain=canvas.example.com --path=/mnt/<volume> \
#                    --email=admin@example.com --owner=deploy
#
# Lægger stakken i <path>/canvas (docker-compose.yml, config/, .env, data/).
# Kan køres igen efter "git pull": compose, config og indstillinger opdateres,
# mens hemmeligheder (ENCRYPTION_KEY m.fl.) bevares. Databasen sættes kun op første gang.
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

# Standard: ghcr.io/<ejer af dette repos origin>/canvas-lms:latest
IMAGE=""
origin=$(git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)
gh_owner=${LMS_GITHUB_OWNER:-}   # sat af install.sh, når der ikke er et git-checkout
if [[ -z $gh_owner && $origin =~ github\.com[:/]([^/]+)/ ]]; then
  gh_owner=${BASH_REMATCH[1]}
fi
if [[ -n $gh_owner ]]; then
  IMAGE="ghcr.io/$(tr '[:upper:]' '[:lower:]' <<<"$gh_owner")/canvas-lms:latest"
fi

usage() {
  sed -n '2,9p' "$0"
  echo
  echo "Canvas-flag:"
  echo "  --image=REF                   Canvas-image (standard: ${IMAGE:-ingen, angiv --image})"
  common_usage
}

for arg in "$@"; do
  parse_common "$arg" && continue
  case $arg in
    --image=*) IMAGE=${arg#*=} ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "ukendt flag: $arg" ;;
  esac
done
validate_common
[[ -n $IMAGE ]] || die "angiv --image=ghcr.io/<ejer>/canvas-lms:latest"

stack="$DATA_PATH/canvas"
env_file="$stack/.env"

step "Henter $IMAGE (flere GB første gang; senere kun ændrede lag)"
as_owner docker pull "$IMAGE" || die "kunne ikke hente $IMAGE.
  Gør pakken offentlig på GitHub, eller log ind som $OWNER:
  echo \$PAT | docker login ghcr.io -u <github-bruger> --password-stdin   (PAT med read:packages)"

step "Installerer stakken i $stack"
install_stack "$REPO_DIR/canvas" "$stack"
mkdir -p "$stack"/data/{postgres,files,log}
chown 9999:9999 "$stack/data/files" "$stack/data/log"   # "docker"-brugeren i Canvas-imaget

step "Skriver .env"
secret() {   # bevar eksisterende hemmelighed, ellers generér
  local key=$1 value=$2 line
  line=$(old_value_line "$env_file" "$key")
  if [[ -n $line ]]; then echo "$line"; else echo "$key=$value"; fi
}
jwk_line=$(old_value_line "$env_file" CANVAS_LTI_JWK)
if [[ -z $jwk_line ]]; then
  echo "Genererer LTI 1.3-signeringsnøgle..."
  jwk=$(as_owner docker run --rm "$IMAGE" bundle exec ruby -e \
    'require "json/jwt"; k = OpenSSL::PKey::RSA.generate(2048); puts k.to_jwk(kid: Time.now.utc.strftime("%Y-%m-%dT%H:%M:%SZ")).to_json')
  [[ $jwk == \{*\"kty\":\"RSA\"* ]] || die "uventet JWK-output: $jwk"
  jwk_line="CANVAS_LTI_JWK='$jwk'"
fi
[[ -f $env_file ]] && cp -p "$env_file" "$env_file.bak"
write_env "$env_file" <<EOF
# Skrevet af canvas.sh. Indstillinger overskrives ved næste kørsel; hemmeligheder bevares.
# Tag en sikkerhedskopi: uden ENCRYPTION_KEY kan databasen ikke læses.
CANVAS_IMAGE=$IMAGE
CANVAS_DOMAIN=$DOMAIN
CANVAS_SSL=true
CANVAS_LTI_ISS=https://canvas.instructure.com
PASSENGER_MAX_POOL_SIZE=1
CANVAS_ADMIN_EMAIL=$EMAIL
CANVAS_ACCOUNT_NAME=Canvas test
$(traefik_env)

$(secret POSTGRES_PASSWORD "$(rand 24)")
$(secret ENCRYPTION_KEY "$(rand 32)")
$(secret CANVAS_JWT_KEY "$(rand 32)")
$(secret CANVAS_ADMIN_PASSWORD "$(rand 12)")
$jwk_line
EOF
rm -f "$env_file.bak"

if [[ $START -eq 0 ]]; then
  echo "Filer skrevet (--no-start). Start med: cd $stack && docker compose up -d"
  exit 0
fi

cd "$stack"
step "Starter Postgres og Redis"
as_owner docker compose up -d --wait postgres redis

migrated=$(as_owner docker compose exec -T postgres \
  psql -U canvas -d canvas_production -tAc "select to_regclass('public.accounts') is not null")
if [[ $migrated != t ]]; then
  step "Første opsætning: skema, konto og admin (tager nogle minutter)"
  as_owner docker compose --profile setup run --rm -T setup
else
  step "Databasen findes allerede: kører migreringer"
  as_owner docker compose run --rm -T --no-deps jobs bin/rails db:migrate
fi

step "Starter Canvas"
as_owner docker compose up -d --remove-orphans --force-recreate web jobs   # config/ er nye filer; uden recreate ser containerne de gamle

cat <<EOF

Canvas: https://$DOMAIN  (første side kan tage et minut, mens Passenger starter)
Admin:  $EMAIL
Kode:   sudo grep CANVAS_ADMIN_PASSWORD $env_file
Logs:   cd $stack && docker compose logs -f web jobs
EOF
