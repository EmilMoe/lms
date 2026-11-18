#!/usr/bin/env bash
# Klargør staging-dropletten (Ubuntu, Docker allerede installeret):
#   1. swapfil på systemdisken
#   2. DO-volume monteret permanent via /etc/fstab (UUID, nofail)
#   3. docker.service starter først når volumet er monteret (RequiresMountsFor)
#   4. valgfrit: flyt hele Docker (images, lag) til volumet via data-root
#   5. datamapper til Canvas, Moodle og Caddy med rette ejere
#
# Brug (som root):
#   ./setup-server.sh --volume <navn> [--swap 4G] [--docker-data-root] [--format]
#
#   --volume            DO-volumets navn (enheden er /dev/disk/by-id/scsi-0DO_Volume_<navn>)
#   --swap              størrelse på /swapfile (standard 4G, 0 = spring over)
#   --docker-data-root  mulighed 2: flyt /var/lib/docker til /mnt/<navn>/docker
#   --format            formatér volumet med ext4 HVIS det ikke har et filsystem.
#                       Uden flaget stopper scriptet i stedet. Et volume med data
#                       formateres aldrig.
#
# Scriptet kan køres igen uden at skade noget.
set -euo pipefail

volume="" swap_size="4G" move_docker=0 allow_format=0
while [[ $# -gt 0 ]]; do
  case $1 in
    --volume) volume=$2; shift 2 ;;
    --swap) swap_size=$2; shift 2 ;;
    --docker-data-root) move_docker=1; shift ;;
    --format) allow_format=1; shift ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "Ukendt argument: $1" >&2; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Kør som root (sudo)." >&2; exit 1; }
[[ -n $volume ]] || { echo "Mangler --volume <navn>." >&2; exit 1; }
[[ $volume =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "Ugyldigt volumenavn: $volume" >&2; exit 1; }
command -v docker >/dev/null || { echo "Docker er ikke installeret." >&2; exit 1; }

device="/dev/disk/by-id/scsi-0DO_Volume_${volume}"
mountpoint="/mnt/${volume}"
step() { printf '\n==> %s\n' "$*"; }

step "Plads før"
df -h / || true
docker system df || true

# ---------------------------------------------------------------- 1. swap
if [[ $swap_size != 0 ]]; then
  step "Swapfil ($swap_size)"
  if swapon --show=NAME --noheadings | grep -qx /swapfile; then
    echo "/swapfile er allerede aktiv."
  else
    [[ -e /swapfile ]] || fallocate -l "$swap_size" /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
  fi
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  cat > /etc/sysctl.d/99-swap.conf <<'EOF'
vm.swappiness=10
vm.vfs_cache_pressure=50
EOF
  sysctl -q --system
  swapon --show
fi

# ---------------------------------------------------------------- 2. volume
step "Volume $volume -> $mountpoint"
[[ -b $device ]] || { echo "Fandt ikke $device. Er volumet tilsluttet denne droplet?" >&2; exit 1; }

fstype=$(blkid -o value -s TYPE "$device" || true)
if [[ -z $fstype ]]; then
  if [[ $allow_format -eq 1 ]]; then
    echo "Ingen filsystem på $device: formaterer med ext4."
    mkfs.ext4 -q -L "${volume:0:16}" "$device"
    fstype=ext4
  else
    echo "$device har intet filsystem. Kør igen med --format for at oprette ext4." >&2
    exit 1
  fi
fi
case $fstype in ext4|xfs) ;; *) echo "Uventet filsystem på volumet: $fstype" >&2; exit 1 ;; esac
uuid=$(blkid -o value -s UUID "$device")

mkdir -p "$mountpoint"
# DO's "auto-mount" skriver selv en fstab-linje med by-id-stien; erstat den med UUID-linjen.
sed -i "\|^${device}[[:space:]]|d; \|^UUID=${uuid}[[:space:]]|d" /etc/fstab
echo "UUID=${uuid} ${mountpoint} ${fstype} defaults,nofail,discard,noatime 0 2" >> /etc/fstab
systemctl daemon-reload
mountpoint -q "$mountpoint" || mount "$mountpoint"
findmnt "$mountpoint"

# ---------------------------------------------------------------- 3. Docker venter på volumet
step "docker.service: RequiresMountsFor=$mountpoint"
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/10-require-volume.conf <<EOF
[Unit]
RequiresMountsFor=${mountpoint}
EOF
systemctl daemon-reload

# ---------------------------------------------------------------- 4. data-root (valgfri)
# Docker 29+ bruger som standard containerd's image store på nye installationer.
# Så ligger images i /var/lib/containerd, ikke i Dockers data-root, og begge skal flyttes.
if [[ $move_docker -eq 1 ]]; then
  target="${mountpoint}/docker"
  ctr_target="${mountpoint}/containerd"
  current=$(docker info --format '{{.DockerRootDir}}')
  uses_containerd_store=0
  docker info --format '{{json .DriverStatus}}' | grep -q 'io.containerd.snapshotter' && uses_containerd_store=1
  ctr_current=$(containerd config dump 2>/dev/null | sed -n 's/^root = "\(.*\)"$/\1/p' | head -n1)
  ctr_current=${ctr_current:-/var/lib/containerd}

  step "Docker data-root: $current -> $target (containerd image store: $uses_containerd_store)"
  if [[ $current == "$target" && ( $uses_containerd_store -eq 0 || $ctr_current == "$ctr_target" ) ]]; then
    echo "Allerede flyttet."
  else
    systemctl stop docker.socket docker.service
    if [[ $current != "$target" ]]; then
      mkdir -p "$target"
      rsync -aHAX --numeric-ids --info=progress2 "${current}/" "${target}/"
      # Flet data-root (og log-grænser) ind i en evt. eksisterende daemon.json.
      python3 - "$target" <<'PY2'
import json, os, sys
path = "/etc/docker/daemon.json"
cfg = {}
if os.path.exists(path):
    with open(path) as f:
        cfg = json.load(f)
cfg["data-root"] = sys.argv[1]
cfg.setdefault("log-driver", "json-file")
cfg.setdefault("log-opts", {"max-size": "10m", "max-file": "3"})
os.makedirs("/etc/docker", exist_ok=True)
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY2
    fi
    if [[ $uses_containerd_store -eq 1 && $ctr_current != "$ctr_target" ]]; then
      systemctl stop containerd.service
      mkdir -p "$ctr_target"
      rsync -aHAX --numeric-ids --info=progress2 "${ctr_current}/" "${ctr_target}/"
      cfg=/etc/containerd/config.toml
      [[ -f $cfg ]] || containerd config default > "$cfg"
      if grep -q '^root *=' "$cfg"; then
        sed -i "s|^root *=.*|root = \"${ctr_target}\"|" "$cfg"
      else
        sed -i "1i root = \"${ctr_target}\"" "$cfg"
      fi
      mkdir -p /etc/systemd/system/containerd.service.d
      printf '[Unit]\nRequiresMountsFor=%s\n' "$mountpoint" \
        > /etc/systemd/system/containerd.service.d/10-require-volume.conf
      systemctl daemon-reload
      systemctl start containerd.service
    fi
    systemctl start docker.service
    [[ $(docker info --format '{{.DockerRootDir}}') == "$target" ]] || { echo "Docker bruger ikke $target!" >&2; exit 1; }
    for old in "$current" "$ctr_current"; do
      if [[ $old != "$mountpoint"/* && -d $old ]]; then
        if [[ $old == "$ctr_current" && $uses_containerd_store -eq 0 ]]; then continue; fi
        mv "$old" "${old}.old"
        echo "Den gamle mappe ligger i ${old}.old. Slet den, når alt kører: rm -rf ${old}.old"
      fi
    done
  fi
fi
# Uden flytning træder RequiresMountsFor i kraft ved næste genstart af Docker.

# ---------------------------------------------------------------- 5. datamapper
step "Datamapper i $mountpoint"
mkdir -p "$mountpoint"/canvas/{postgres,files,log} \
         "$mountpoint"/moodle/{db,moodle,moodledata} \
         "$mountpoint"/caddy/{data,config}
chown 9999:9999 "$mountpoint"/canvas/files "$mountpoint"/canvas/log   # "docker"-brugeren i Canvas-imaget
# Postgres, MariaDB og Moodle (www-data, uid 33) retter selv ejerskab ved første start.

step "Plads efter"
df -h / "$mountpoint"
docker system df
echo
echo "Færdig. Tjek efter genstart: findmnt $mountpoint && docker info --format '{{.DockerRootDir}}'"
