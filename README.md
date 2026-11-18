# Test-LMS'er: Canvas og Moodle på staging

Selv-hostet Canvas og Moodle til at udvikle og teste vores LTI 1.3- og API-integration. De kører på en separat DigitalOcean-droplet (2 vCPU, 4 GB RAM) med et volume på 200 GB, aldrig på produktionsserveren.

```
.github/workflows/build-canvas.yml   bygger Canvas-imaget i GitHub Actions -> GHCR
server/setup-server.sh               swap, volume-mount, Docker på volumet
canvas.sh  + canvas/                 installerer Canvas, Postgres og Redis bag Traefik
moodle.sh  + moodle/                 installerer Moodle og MariaDB bag Traefik
lib/common.sh                        fælles kode til de to scripts
```

Scriptene kopierer compose-filerne til `<path>/canvas` og `<path>/moodle` på volumet og skriver `.env` med tilfældige kodeord dér (chmod 600). Domæner, e-mail, stier og hemmeligheder gives som flag på serveren og kommer aldrig i repoet eller imaget.

## 1. Byg Canvas-imaget

Kør workflowet under Actions → "Build Canvas LMS image". `canvas_ref` er `prod` som standard; brug alternativt en `stable/ÅÅÅÅ-MM-DD`-branch. Buildet tager 1-2 timer.

Første gang bliver pakken `ghcr.io/<ejer>/canvas-lms` privat. Gør den enten offentlig (Package settings), eller log ind på serveren med en PAT der har `read:packages`:

```bash
echo "$PAT" | docker login ghcr.io -u <bruger> --password-stdin
```

## 2. Klargør dropletten

Tilslut volumet i DO-panelet, kopiér repoet til serveren og kør:

```bash
sudo ./server/setup-server.sh --volume <volumenavn> --swap 4G --docker-data-root
```

- Uden `--docker-data-root` (mulighed 1) ligger kun data på volumet, og images bliver på systemdisken (25 GB). Canvas-imaget fylder flere GB, så mulighed 2 anbefales.
- Scriptet håndterer også Docker 29+, hvor images ligger i containerd (`/var/lib/containerd`) og ikke i `/var/lib/docker`.
- `RequiresMountsFor` sættes på `docker.service` (og `containerd.service`), så Docker aldrig starter på en tom mappe på systemdisken.
- Et nyt, uformateret volume kræver `--format`. Et volume med data formateres aldrig.

Firewall: kun 22, 80 og 443 skal være åbne (Traefik). Stakkene publicerer ingen porte selv.

## 3. Traefik

Begge stakke forventer en kørende Traefik med et eksternt docker-netværk. Der publiceres ingen porte på værten; routere sættes op via labels. Standardnavnene kan ændres med flag:

| Flag | Standard |
|---|---|
| `--traefik-network` | `traefik` |
| `--traefik-entrypoint` | `websecure` |
| `--traefik-certresolver` | `letsencrypt` |

DNS for begge domæner skal pege på dropletten.

## 4. Canvas og Moodle

Klon repoet på serveren og kør som en bruger med sudo:

```bash
sudo ./canvas.sh --domain=canvas.example.com --path=/mnt/<volume> --email=admin@example.com --owner=deploy
sudo ./moodle.sh --domain=moodle.example.com --path=/mnt/<volume> --email=admin@example.com --owner=deploy
```

- `--owner` er den linux-bruger, der ejer stakken og kører `docker compose` (skal være i docker-gruppen). Brugerens `docker login` bruges til at hente fra ghcr.io.
- `canvas.sh` henter imaget, genererer hemmeligheder og LTI-nøglen, opsætter databasen første gang (ellers `db:migrate`) og starter web og jobs. Imaget er som standard `ghcr.io/<repo-ejer>/canvas-lms:latest`; brug `--image=` til et bestemt tag.
- `moodle.sh` starter Moodle, som installerer sig selv ved første start (nogle minutter).
- Opdatering: `git pull` og kør scriptet igen med samme flag. Kodeord og nøgler bevares.
- `--help` viser alle flag.

Tag en sikkerhedskopi af `<path>/canvas/.env`. `ENCRYPTION_KEY` må aldrig ændres, og uden den kan databasen ikke læses.

LTI 1.3: Canvas signerer id_tokens med `CANVAS_LTI_JWK`, som indsættes via `config/dynamic_settings.yml` i stedet for Consul. Den offentlige nøgle ligger på `https://<canvas>/api/lti/security/jwks`. Developer keys oprettes som admin under Admin → Developer Keys.

## RAM

Begge LMS'er kan køre samtidig, når de bare leverer demodata gennem API'et. Omtrentligt forbrug i tomgang:

| | RAM |
|---|---|
| Canvas (1 Passenger-proces, jobs-worker, Postgres, Redis) | 2-2,5 GB |
| Moodle (Apache/PHP, MariaDB) | 0,4-0,6 GB |
| Traefik og OS | 0,3-0,4 GB |

Det er tæt på 4 GB, så swapfilen tager spidserne. Spidserne kommer især, når Canvas starter (asset-load) og ved tunge baggrundsjob. Hold øje med `free -h` og `docker stats --no-stream`. Hvis serveren swapper hele tiden, så stop den ene stak (`docker compose stop`) eller opgradér dropletten til 8 GB.

## AGPLv3

Workflowet bygger upstream-koden uændret, og imagets labels peger på den præcise commit (`org.opencontainers.image.revision`). Config-filerne her er driftskonfiguration uden hemmeligheder. Egne tilretninger af Canvas-koden må ikke komme i dette offentlige repo eller image. Hvis vi en dag kører en ændret Canvas for andre brugere, skal kildekoden til ændringerne tilbydes dem.
