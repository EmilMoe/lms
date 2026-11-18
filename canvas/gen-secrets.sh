#!/usr/bin/env bash
# Laver canvas/.env med tilfældige hemmeligheder. Køres på serveren, én gang.
# Brug: ./gen-secrets.sh <image> <domæne> <data-dir> <admin-email>
#   fx  ./gen-secrets.sh ghcr.io/minbruger/canvas-lms:latest canvas.example.com /mnt/lms admin@example.com
set -euo pipefail
cd "$(dirname "$0")"

if [[ -e .env ]]; then
  echo ".env findes allerede; overskriver ikke (ENCRYPTION_KEY må aldrig skiftes)." >&2
  exit 1
fi
if [[ $# -ne 4 ]]; then
  sed -n '3,4p' "$0" >&2
  exit 1
fi
image=$1 domain=$2 data_dir=$3 admin_email=$4

rand() { openssl rand -hex "$1"; }

echo "Henter $image og genererer LTI-nøgle (kan tage et øjeblik)..."
docker pull -q "$image" >/dev/null
jwk=$(docker run --rm "$image" bundle exec ruby -e \
  'require "json/jwt"; k = OpenSSL::PKey::RSA.generate(2048); puts k.to_jwk(kid: Time.now.utc.strftime("%Y-%m-%dT%H:%M:%SZ")).to_json')
[[ $jwk == \{*\"kty\":\"RSA\"* ]] || { echo "Uventet JWK-output: $jwk" >&2; exit 1; }

admin_password=$(rand 12)
umask 077
cat > .env <<ENV
CANVAS_IMAGE=$image
DATA_DIR=$data_dir
CANVAS_DOMAIN=$domain
CANVAS_SSL=true
CANVAS_PORT=8000
CANVAS_LTI_ISS=https://canvas.instructure.com
PASSENGER_MAX_POOL_SIZE=1
POSTGRES_PASSWORD=$(rand 24)
ENCRYPTION_KEY=$(rand 32)
CANVAS_JWT_KEY=$(rand 32)
CANVAS_LTI_JWK='$jwk'
CANVAS_ADMIN_EMAIL=$admin_email
CANVAS_ADMIN_PASSWORD=$admin_password
CANVAS_ACCOUNT_NAME=Canvas test
ENV
echo "Skrev $(pwd)/.env (chmod 600). Admin-adgangskoden står i filen."
echo "Tag en sikkerhedskopi af .env: uden ENCRYPTION_KEY kan databasen ikke læses."
