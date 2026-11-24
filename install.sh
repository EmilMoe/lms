#!/usr/bin/env bash
# Installerer eller opdaterer Canvas, Moodle eller Blackboard-mocken uden at klone repoet:
#
#   curl -fsSL https://raw.githubusercontent.com/EmilMoe/lms/main/install.sh \
#     | sudo bash -s -- canvas --domain=canvas.example.com --path=/mnt/<volume> \
#                              --email=admin@example.com --owner=deploy
#
# Første argument er "canvas", "moodle" eller "blackboard"; resten sendes videre til
# canvas.sh/moodle.sh/blackboard.sh (se --help). Henter repoet som tarball til en midlertidig
# mappe, kører scriptet og rydder op. Stakken selv ligger i <path>/<lms>.
#
# Lås til et tag eller en commit i stedet for main (variablen skal stå efter sudo):
#   ... | sudo LMS_REF=<tag-eller-sha> bash -s -- canvas ...
#
# Alt er pakket ind i main(), så bash har læst hele scriptet fra pipen,
# før noget køres.
set -euo pipefail

main() {
  local repo=${LMS_REPO:-EmilMoe/lms}
  local ref=${LMS_REF:-main}
  local target=${1:-}

  case $target in
    canvas|moodle|blackboard) shift ;;
    *)
      sed -n '2,13p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null || true
      echo "Brug: install.sh canvas|moodle|blackboard [flag...]" >&2
      exit 1 ;;
  esac
  [[ $repo =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "Ugyldigt LMS_REPO: $repo" >&2; exit 1; }
  [[ $ref =~ ^[A-Za-z0-9_./-]+$ ]] || { echo "Ugyldigt LMS_REF: $ref" >&2; exit 1; }
  if ! command -v curl >/dev/null || ! command -v tar >/dev/null; then
    echo "curl og tar skal være installeret." >&2
    exit 1
  fi

  local tmp
  tmp=$(mktemp -d)
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" EXIT

  echo "==> Henter $repo@$ref"
  curl -fsSL "https://codeload.github.com/${repo}/tar.gz/${ref}" | tar -xz -C "$tmp" --strip-components=1
  [[ -f $tmp/$target.sh ]] || { echo "Fandt ikke $target.sh i $repo@$ref" >&2; exit 1; }

  # Uden git-checkout kan canvas.sh ikke læse ejeren fra origin; giv den videre.
  export LMS_GITHUB_OWNER=${repo%%/*}
  # stdin er pipen fra curl; luk den, så ingen kommando læser fra den.
  bash "$tmp/$target.sh" "$@" </dev/null
}

main "$@"
