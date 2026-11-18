# Fælles hjælpefunktioner til canvas.sh og moodle.sh. Sources, køres ikke direkte.
# shellcheck shell=bash disable=SC2034

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

DOMAIN="" DATA_PATH="" EMAIL="" OWNER="${SUDO_USER:-root}"
TRAEFIK_NETWORK="traefik" TRAEFIK_ENTRYPOINT="websecure" TRAEFIK_CERTRESOLVER="letsencrypt"
START=1

die()  { echo "Fejl: $*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }
rand() { openssl rand -hex "$1"; }

# Fælles flag. Returnerer 1 for flag, scriptet selv skal håndtere.
parse_common() {
  case $1 in
    --domain=*)               DOMAIN=${1#*=} ;;
    --path=*)                 DATA_PATH=${1#*=} ;;
    --email=*)                EMAIL=${1#*=} ;;
    --owner=*)                OWNER=${1#*=} ;;
    --traefik-network=*)      TRAEFIK_NETWORK=${1#*=} ;;
    --traefik-entrypoint=*)   TRAEFIK_ENTRYPOINT=${1#*=} ;;
    --traefik-certresolver=*) TRAEFIK_CERTRESOLVER=${1#*=} ;;
    --no-start)               START=0 ;;
    *) return 1 ;;
  esac
}

validate_common() {
  [[ $EUID -eq 0 ]] || die "kør med sudo (data-mapper skal have bestemte ejere)."
  command -v docker >/dev/null || die "Docker er ikke installeret."
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 mangler."
  [[ $DOMAIN =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && $DOMAIN == *.* ]] || die "ugyldigt --domain: '$DOMAIN'"
  [[ $DATA_PATH == /* && -d $DATA_PATH ]] || die "--path skal være en eksisterende absolut sti (volumet): '$DATA_PATH'"
  [[ $EMAIL == ?*@?*.?* ]] || die "ugyldig --email: '$EMAIL'"
  id "$OWNER" >/dev/null 2>&1 || die "brugeren '$OWNER' findes ikke."
  if [[ $OWNER != root ]] && ! id -nG "$OWNER" | tr ' ' '\n' | grep -qx docker; then
    die "'$OWNER' er ikke i docker-gruppen (usermod -aG docker $OWNER)."
  fi
  findmnt -n -T "$DATA_PATH" -o TARGET | grep -qx / && \
    echo "Advarsel: $DATA_PATH ligger på systemdisken, ikke på et volume." >&2
  docker network inspect "$TRAEFIK_NETWORK" >/dev/null 2>&1 || \
    die "Traefik-netværket '$TRAEFIK_NETWORK' findes ikke. Start Traefik først, eller angiv --traefik-network=."
}

# Kør som stakkens ejer, så dennes docker-login (fx til ghcr.io) bruges.
as_owner() {
  if [[ $OWNER == root ]]; then "$@"; else sudo -u "$OWNER" -H -- "$@"; fi
}

# Kopiér compose og config fra repoet til stakmappen. .env og data/ røres ikke.
install_stack() {
  local src=$1 dest=$2
  mkdir -p "$dest"
  install -m 644 "$src/docker-compose.yml" "$dest/docker-compose.yml"
  if [[ -d $src/config ]]; then
    rm -rf "$dest/config"
    cp -R "$src/config" "$dest/config"
  fi
  chown -R "$OWNER": "$dest/docker-compose.yml" "${dest}/config" 2>/dev/null || true
  chown "$OWNER": "$dest"
}

# Hent en eksisterende linje KEY=... fra en gammel .env (tom hvis den ikke findes).
old_value_line() {
  local file=$1 key=$2
  [[ -f $file ]] && grep -m1 -E "^${key}=" "$file" || true
}

# Skriv .env atomisk, chmod 600, ejet af OWNER. Indhold kommer fra stdin.
write_env() {
  local file=$1 tmp
  tmp=$(mktemp "${file}.XXXXXX")
  cat > "$tmp"
  chmod 600 "$tmp"
  chown "$OWNER": "$tmp"
  mv "$tmp" "$file"
}

traefik_env() {
  cat <<EOF
TRAEFIK_NETWORK=$TRAEFIK_NETWORK
TRAEFIK_ENTRYPOINT=$TRAEFIK_ENTRYPOINT
TRAEFIK_CERTRESOLVER=$TRAEFIK_CERTRESOLVER
EOF
}

common_usage() {
  cat <<'EOF'
Fælles flag:
  --domain=HOST                 domænet brugerne åbner (DNS skal pege på serveren)
  --path=DIR                    volumet, fx /mnt/<volume>
  --email=ADDR                  admin-brugerens e-mail
  --owner=USER                  linux-bruger der ejer og kører stakken (standard: den der kører sudo)
  --traefik-network=NAME        Traefiks eksterne docker-netværk (standard: traefik)
  --traefik-entrypoint=NAME     HTTPS-entrypoint i Traefik (standard: websecure)
  --traefik-certresolver=NAME   certresolver i Traefik (standard: letsencrypt)
  --no-start                    skriv kun filer, start ikke containere
EOF
}
