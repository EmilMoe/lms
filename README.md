# Test-LMS'er: Canvas og Moodle på staging

Selv-hostet Canvas og Moodle til at udvikle og teste vores LTI 1.3- og API-integration. De kører på en separat DigitalOcean-droplet (2 vCPU, 4 GB RAM) med et volume på 200 GB, aldrig på produktionsserveren.

```
.github/workflows/build-canvas.yml   bygger Canvas-imaget i GitHub Actions -> GHCR
canvas/                              compose + ERB-config (læser hemmeligheder fra .env)
moodle/                              compose (oksigenia/moodle + MariaDB)
proxy/                               Caddy med automatisk HTTPS foran begge
server/setup-server.sh               swap, volume-mount, Docker på volumet
```

Hemmeligheder ligger kun i `*/.env` på serveren (chmod 600, ignoreret af git). Repoet og imaget indeholder ingen.

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

Firewall: kun 22, 80 og 443 skal være åbne. Appernes porte er bundet til `127.0.0.1`, fordi Docker-porte ellers går uden om ufw.

## 3. Canvas

```bash
cd canvas
./gen-secrets.sh ghcr.io/<ejer>/canvas-lms:latest canvas.example.com /mnt/<volumenavn> admin@example.com
docker compose up -d postgres redis
docker compose --profile setup run --rm setup     # skema, konto og admin (kun én gang)
docker compose up -d web jobs
```

Tag en sikkerhedskopi af `canvas/.env` et sikkert sted. `ENCRYPTION_KEY` må aldrig ændres, og uden den kan databasen ikke læses.

LTI 1.3: Canvas signerer id_tokens med `CANVAS_LTI_JWK`, som indsættes via `config/dynamic_settings.yml` i stedet for Consul. Den offentlige nøgle kan hentes på `https://<canvas>/api/lti/security/jwks`. Developer keys oprettes som admin under Admin → Developer Keys.

## 4. Moodle

```bash
cd moodle && cp .env.example .env && chmod 600 .env   # udfyld kodeord og MOODLE_URL
docker compose up -d && docker compose logs -f moodle
```

## 5. HTTPS

```bash
cd proxy && cp .env.example .env    # udfyld domæner; DNS A-records skal pege på dropletten
docker compose up -d
```

## RAM

Kør ét LMS ad gangen: `docker compose stop` i den ene mappe før `up -d` i den anden. Canvas bruger cirka 2,5-3 GB (2 Passenger-processer, jobs-worker, Postgres og Redis).

## AGPLv3

Workflowet bygger upstream-koden uændret, og imagets labels peger på den præcise commit (`org.opencontainers.image.revision`). Config-filerne her er driftskonfiguration uden hemmeligheder. Egne tilretninger af Canvas-koden må ikke komme i dette offentlige repo eller image. Hvis vi en dag kører en ændret Canvas for andre brugere, skal kildekoden til ændringerne tilbydes dem.
