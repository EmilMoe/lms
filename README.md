# Test-LMS'er: Canvas og Moodle på staging

Selv-hostet Canvas og Moodle til at udvikle og teste vores LTI 1.3- og API-integration. De kører på en separat DigitalOcean-droplet (2 vCPU, 4 GB RAM) med et volume på 200 GB, aldrig på produktionsserveren.

```
.github/workflows/build-canvas.yml   bygger Canvas-imaget i GitHub Actions -> GHCR
server/setup-server.sh               swap, volume-mount, Docker på volumet
canvas.sh  + canvas/                 installerer Canvas, Postgres og Redis bag Traefik
canvas/dap-mock/                     mock af Canvas Data 2 (kursusadgang), som open source-Canvas ikke har
moodle.sh  + moodle/                 installerer Moodle og MariaDB bag Traefik
lib/common.sh                        fælles kode til de to scripts
install.sh                           kører canvas.sh/moodle.sh direkte fra GitHub (curl)
```

Scriptene kopierer compose-filerne til `<path>/canvas` og `<path>/moodle` på volumet og skriver `.env` med tilfældige kodeord dér (chmod 600). Domæner, e-mail, stier og hemmeligheder gives som flag på serveren og kommer aldrig i repoet eller imaget.

## 1. Byg Canvas-imaget

Kør workflowet under Actions → "Build Canvas LMS image". `canvas_ref` er `stable/2026-05-20` som standard; en nyere `stable/ÅÅÅÅ-MM-DD`-branch kan angives. `prod` kan ikke bygges lige nu, fordi en JavaScript-pakke mangler i dens `package.json`. Buildet tager cirka 15 minutter.

Imaget lægges på `ghcr.io/<ejer>/canvas-lms` med tags `latest`, `<ref>` og `<ref>-<sha>`. Pakken arver repoets offentlige synlighed og kan hentes uden login. Hvis den er privat, så log ind på serveren med en PAT der har `read:packages`:

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

Kør direkte fra GitHub som en bruger med sudo. Der skal ikke klones noget:

```bash
curl -fsSL https://raw.githubusercontent.com/EmilMoe/lms/main/install.sh | sudo bash -s -- canvas --domain=canvas.example.com --path=/mnt/<volume> --email=admin@example.com --owner=deploy
curl -fsSL https://raw.githubusercontent.com/EmilMoe/lms/main/install.sh | sudo bash -s -- moodle --domain=moodle.example.com --path=/mnt/<volume> --email=admin@example.com --owner=deploy
```

`install.sh` henter repoet som tarball til en midlertidig mappe, kører `canvas.sh` eller `moodle.sh` med flagene og rydder op. Lås til et tag eller en commit med `sudo LMS_REF=<tag-eller-sha> bash -s -- ...`. Fra et klonet repo virker `sudo ./canvas.sh ...` og `sudo ./moodle.sh ...` med de samme flag.

- `--owner` er den linux-bruger, der ejer stakken og kører `docker compose` (skal være i docker-gruppen). Brugerens `docker login` bruges til at hente fra ghcr.io.
- `canvas.sh` henter imaget, genererer hemmeligheder og LTI-nøglen, opsætter databasen første gang (ellers `db:migrate`) og starter web og jobs. Imaget er som standard `ghcr.io/<repo-ejer>/canvas-lms:latest`; brug `--image=` til et bestemt tag.
- `moodle.sh` starter Moodle, som installerer sig selv ved første start (nogle minutter).
- Opdatering: kør kommandoen igen med kun `--path`. Domæne, e-mail, image/tag og Traefik-indstillinger læses fra `<path>/<lms>/.env`, og ejeren fra stakmappen. Flag, du angiver, vinder. Kodeord, nøgler og `data/` bevares, mens `docker-compose.yml` og `config/` erstattes af repoets version:

  ```bash
  curl -fsSL https://raw.githubusercontent.com/EmilMoe/lms/main/install.sh | sudo bash -s -- canvas --path=/mnt/<volume>
  ```
- `--help` viser alle flag.

Tag en sikkerhedskopi af `<path>/canvas/.env`. `ENCRYPTION_KEY` må aldrig ændres, og uden den kan databasen ikke læses.

LTI 1.3: Canvas signerer id_tokens med `CANVAS_LTI_JWK`, som indsættes via `config/dynamic_settings.yml` i stedet for Consul. Den offentlige nøgle ligger på `https://<canvas>/api/lti/security/jwks`. Developer keys oprettes som admin under Admin → Developer Keys.

## 5. Testdata

`testdata/` indeholder det samme lille, fiktive datasæt til begge LMS'er: 3 kurser (DAN101, MAT101, ENG101), 2 lærere og 18 elever med samme ID'er. Brugerne har ingen adgangskode, fordi repoet er offentligt og serveren ligger på internettet. Se data som en bruger med "Act as user" (Canvas) eller "Log in as" (Moodle).

### Canvas

**Brugere, kurser og tilmeldinger via SIS-import:**

1. Kør denne kommando på din egen maskine. Den henter de fire CSV-filer fra `testdata/canvas/` ned i den mappe, du står i, og pakker dem i `canvas-sis.zip`:

   ```bash
   for f in users courses sections enrollments; do curl -fsSLO https://raw.githubusercontent.com/EmilMoe/lms/main/testdata/canvas/$f.csv; done && zip canvas-sis.zip users.csv courses.csv sections.csv enrollments.csv
   ```

2. Log ind i Canvas som admin. Klik på **Admin** i menuen yderst til venstre, og vælg rodkontoen (den øverste konto på listen).
3. Klik på **SIS Import** i kontoens menu.
4. Vælg `canvas-sis.zip` under "Choose a file". Lad "This is a full batch update" være slået fra, ellers sletter Canvas alt det, der ikke står i filerne. Klik på **Process Data**.
5. Når importen er færdig, viser siden, hvad der er oprettet: 3 kurser, 5 sektioner (hold), 20 brugere og 36 tilmeldinger. Eventuelle fejl og advarsler står på samme side.

Kurserne ligger derefter under Admin → rodkontoen → **Courses**. Du ser kurset som en lærer eller elev ved at åbne **People** i kurset, klikke på brugeren og vælge **Act as User**.

Importen kan køres igen. Canvas opdaterer rækkerne ud fra deres ID (`user_id`, `course_id` osv.), så der kommer ikke dubletter. Filformatet er beskrevet i Canvas' dokumentation under "SIS Import Format".

**Kursusindhold** (moduler, sider, opgaver) ligger ikke i `testdata/`. Har du en Common Cartridge-fil (`.imscc`) eller en eksport fra et andet Canvas-kursus, importerer du den i kurset under Settings → **Import Course Content**.

**REST-API** til alt andet (opgaver, afleveringer, karakterer): opret en token under Account → Settings → **+ New Access Token** og brug fx `POST /api/v1/courses/:id/assignments`.

### Canvas Data 2-mock (kursusadgang)

Canvas Data 2 (DAP), som Nexus henter Course Access og Course Access Log fra, er en hostet Instructure-tjeneste. Den findes ikke i open source-Canvas. `canvas.sh` starter derfor en lille mock (`canvas/dap-mock/`), der svarer som Canvas Data 2 på `https://<canvas-domæne>/cd2`:

- `canvas.enrollment_terms`, `courses`, `enrollments` og `roles` læses direkte (skrivebeskyttet) fra Canvas' database, med de samme id'er som REST-API'et.
- `canvas_logs.web_logs` bygges af Canvas' `page_views` (hvis de gemmes i databasen) plus genererede besøg for aktive tilmeldinger i publicerede kurser de seneste 30 dage: browser, Canvas-app og lidt API-trafik, flest på hverdage i dagtimerne. Besøgene er faste pr. bruger, kursus og dag, så et nyt snapshot giver de samme data, og incremental-kald giver kun det nye.

**Sådan bruges den fra Nexus:**

1. Sæt `CANVAS_DATA_URL=https://<canvas-domæne>/cd2` i Nexus' `.env`. Uden den bruger Nexus Instructures rigtige `{region}.api-gateway.instructure.com`.
2. Hent client ID og secret på serveren:

   ```bash
   sudo grep DAP_MOCK_CLIENT <path>/canvas/.env
   ```

3. Indtast dem på Canvas-forbindelsens Canvas Data 2-fane i Nexus og test forbindelsen. Regionen bliver `eu-west-1`, men det er ligegyldigt for mocken.
4. Kør sync som normalt. Course Access og Course Access Log fyldes med de seneste 30 dage.

Indstillinger i `<path>/canvas/.env` (bevares ved opdatering, genstart med `docker compose up -d dap-mock`):

| Variabel | Standard | |
|---|---|---|
| `DAP_MOCK_COURSE_ACCESS` | `true` | `false` svarer til en rigtig nøgle uden web logs, så Nexus' "without course access"-vej kan testes |
| `DAP_MOCK_SYNTHETIC_WEB_LOGS` | `true` | `false` giver kun Canvas' egne `page_views` |

Mocken laver kun den normale vej: ingen throttling, ventende jobs eller andre fejl end "ingen nye data" og "for langt tilbage" (over 30 dage). Logs: `docker compose logs -f dap-mock`.

### Moodle

**Indbygget generator** til kurser med aktiviteter, brugere og forum-indlæg (størrelse XS-XXL):

```bash
cd <path>/moodle && sudo docker compose exec -u www-data moodle php public/admin/tool/generator/cli/maketestcourse.php --shortname=TEST1 --size=S
```

Moodle 5.1+ har koden under `public/`; ældre versioner uden. Den samme funktion findes under Site administration → Development → Make test course, når debugging er sat til "Developer".

**CSV-upload** af datasættet i `testdata/moodle/`, i denne rækkefølge:

1. Site administration → Courses → Upload courses → `courses.csv`. Kurserne lægges i kategori 1.
2. Site administration → Users → Upload users → `users.csv`. Brugerne får `auth=nologin` og tilmeldes kurserne via `course1`/`role1`.

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
