# Test-LMS'er: Canvas, Moodle og Blackboard på staging

Selv-hostet Canvas og Moodle og en Blackboard-mock til at udvikle og teste vores LTI 1.3- og API-integration. De kører på en separat DigitalOcean-droplet (2 vCPU, 4 GB RAM) med et volume på 200 GB, aldrig på produktionsserveren.

```
.github/workflows/build-canvas.yml   bygger Canvas-imaget i GitHub Actions -> GHCR
server/setup-server.sh               swap, volume-mount, Docker på volumet
canvas.sh  + canvas/                 installerer Canvas, Postgres og Redis bag Traefik
canvas/dap-mock/                     mock af Canvas Data 2 (kursusadgang), som open source-Canvas ikke har
moodle.sh  + moodle/                 installerer Moodle og MariaDB bag Traefik
blackboard.sh + blackboard/          installerer Blackboard-mocken (Learn REST-API + LTI 1.3) bag Traefik
lib/common.sh                        fælles kode til scriptene
install.sh                           kører canvas.sh/moodle.sh/blackboard.sh direkte fra GitHub (curl)
```

Scriptene kopierer compose-filerne til `<path>/canvas`, `<path>/moodle` og `<path>/blackboard` på volumet og skriver `.env` med tilfældige kodeord dér (chmod 600). Domæner, e-mail, stier og hemmeligheder gives som flag på serveren og kommer aldrig i repoet eller imaget.

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

Alle stakke forventer en kørende Traefik med et eksternt docker-netværk. Der publiceres ingen porte på værten; routere sættes op via labels. Standardnavnene kan ændres med flag:

| Flag | Standard |
|---|---|
| `--traefik-network` | `traefik` |
| `--traefik-entrypoint` | `websecure` |
| `--traefik-certresolver` | `letsencrypt` |

DNS for alle domæner skal pege på dropletten.

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

`testdata/` indeholder det samme lille, fiktive datasæt til alle tre LMS'er: 3 kurser (DAN101, MAT101, ENG101), 2 lærere og 18 elever med samme ID'er. Brugerne har ingen adgangskode, fordi repoet er offentligt og serveren ligger på internettet. Se data som en bruger med "Act as user" (Canvas), "Log in as" (Moodle) eller "Åbn i Nexus" på Blackboard-mockens testside.

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

### Moodle

**Indbygget generator** til kurser med aktiviteter, brugere og forum-indlæg (størrelse XS-XXL). Generatoren kører kun, når debugging står på "Developer"; ellers svarer den `Not available on this server because debugging is not set to DEVELOPER`. Kør kommandoerne fra `<path>/moodle`.

Slå Developer-debugging til. `debugdisplay=0` holder fejlbeskederne ude af websiderne imens:

```bash
sudo docker compose exec -u www-data moodle php admin/cli/cfg.php --name=debug --set=32767
```

```bash
sudo docker compose exec -u www-data moodle php admin/cli/cfg.php --name=debugdisplay --set=0
```

Opret kurset:

```bash
sudo docker compose exec -u www-data moodle php public/admin/tool/generator/cli/maketestcourse.php --shortname=TEST1 --size=S
```

Hver kørsel opretter ét kursus; `--size` styrer kun indholdet. Flere kurser får du ved at køre den igen med et nyt, unikt kortnavn, fx i en løkke (`-T` fordi der ikke er nogen terminal i løkken):

```bash
for i in 2 3 4 5; do sudo docker compose exec -T -u www-data moodle php public/admin/tool/generator/cli/maketestcourse.php --shortname=TEST$i --size=S; done
```

Testbrugerne genbruges på tværs af kurserne. `public/admin/tool/generator/cli/maketestsite.php --size=...` laver en hel testside med mange kurser på én gang, men er tung allerede fra S.

Sæt debugging tilbage bagefter:

```bash
sudo docker compose exec -u www-data moodle php admin/cli/cfg.php --name=debug --set=0
```

Moodle 5.1+ har webkoden (fx `admin/tool/generator`) under `public/`, mens `admin/cli/` ligger i roden; ældre versioner har alt i roden. Den samme funktion findes under Site administration → Development → Make test course, når debugging er sat til "Developer".

**CSV-upload** af datasættet i `testdata/moodle/`, i denne rækkefølge:

1. Site administration → Courses → Upload courses → `courses.csv`. Kurserne lægges i kategori 1.
2. Site administration → Users → Upload users → `users.csv`. Brugerne får `auth=nologin` og tilmeldes kurserne via `course1`/`role1`.

## 6. Canvas Data 2-mock (kursusadgang)

Canvas Data 2 (DAP), som Nexus henter Course Access og Course Access Log fra, er en hostet Instructure-tjeneste. Den findes ikke i open source-Canvas. `canvas.sh` starter derfor en lille mock (`canvas/dap-mock/`), der svarer som Canvas Data 2 på `https://<canvas-domæne>/cd2`:

- `canvas.enrollment_terms`, `courses`, `enrollments` og `roles` læses direkte (skrivebeskyttet) fra Canvas' database, med de samme id'er som REST-API'et.
- `canvas_logs.web_logs` (besøg) har open source-Canvas ikke. Mocken fører sin egen besøgslog i `<path>/canvas/data/dap-mock/web_logs.sqlite`, som kun vokser.

**Sådan opstår besøgene:**

- Ved hver forespørgsel skriver mocken besøg for tiden siden sidst og frem til nu. Det, der er skrevet, ændres aldrig, og der kommer aldrig besøg efter nu. Når Nexus synkroniserer dagligt, kommer der altså en dags nye besøg til hver gang.
- Besøgene hører til de faktiske kurser og personer i Canvas. Der kommer kun besøg, når
  - tilmeldingen er aktiv eller inviteret (elev, lærer eller TA), og kurset er publiceret. Inviteret tæller med, fordi testbrugere uden adgangskode ikke kan acceptere invitationer, og fordi Nexus selv regner dem som tilmeldte,
  - og tidspunktet ligger inden for kursets datoer (eller termens, hvis kurset ikke har nogen) og tilmeldingens egne datoer, når de er sat.
- Første gang mocken ser en tilmelding, får den 30 dages historik bagud. Det gælder også et kursus, du opretter i dag, så der straks er noget at se. Historikken skrives med tidspunktet "nu" og kommer derfor med i næste sync. Derefter skrives kun tiden siden sidst.
- En slettet tilmelding får ikke flere besøg, men beholder de gamle.
- Hver person har en fast profil pr. kursus: hvor aktiv, hvornår på døgnet, hvor meget på mobilappen og i weekenden. Lærere er mest inde i arbejdstiden, elever også om aftenen. Trafikken er en blanding af browser, Canvas-app og lidt API, med enkelte POST-kald og fejlsvar, som Nexus sorterer fra.
- Canvas' egne `page_views` kommer med, hvis Canvas gemmer dem i databasen.

**Som den rigtige Canvas Data 2:** Et snapshot giver besøgene fra de seneste 30 dage. Et incremental-kald (`since`) giver det, der er skrevet siden, og et `since` over 30 dage tilbage afvises, så Nexus må tage et nyt snapshot. Nexus beholder selv alt, den har hentet.

**Start forfra:** Stop mocken, slet `data/dap-mock/web_logs.sqlite` og start den igen. Så fyldes 30 dage bagud på ny.

**Sådan bruges den fra Nexus:**

1. Sæt `CANVAS_DATA_URL=https://<canvas-domæne>/cd2` i Nexus' `.env`. Uden den bruger Nexus Instructures rigtige `{region}.api-gateway.instructure.com`.
2. Hent client ID og secret på serveren:

   ```bash
   sudo grep DAP_MOCK_CLIENT <path>/canvas/.env
   ```

3. Indtast dem på Canvas-forbindelsens Canvas Data 2-fane i Nexus og test forbindelsen. Regionen bliver `eu-west-1`, men det er ligegyldigt for mocken.
4. Kør sync som normalt. Første sync giver de seneste 30 dage, og hver sync derefter lægger de nye besøg til.

Indstillinger i `<path>/canvas/.env` (bevares ved opdatering, genstart med `docker compose up -d dap-mock`):

| Variabel | Standard | |
|---|---|---|
| `DAP_MOCK_COURSE_ACCESS` | `true` | `false` svarer til en rigtig nøgle uden web logs, så Nexus' "without course access"-vej kan testes |
| `DAP_MOCK_SYNTHETIC_WEB_LOGS` | `true` | `false` giver kun Canvas' egne `page_views` |
| `DAP_MOCK_BACKFILL_DAYS` | `30` | dage der fyldes bagud, første gang mocken ser en tilmelding |
| `DAP_MOCK_RETENTION_DAYS` | `30` | hvor langt et snapshot og `since` går tilbage (30 som Canvas Data 2) |
| `DAP_MOCK_REFRESH_HOURS` | `0` | fx `4`: data kun op til seneste opdatering hver 4. time, som Canvas Data 2. `0` = altid til nu |

Mocken laver kun den normale vej: ingen throttling, ventende jobs eller andre fejl end "ingen nye data" og "for langt tilbage". Logs: `docker compose logs -f dap-mock`.

## 7. Blackboard-mock

Blackboard Learn er lukket kode og kan ikke selvhostes som Canvas og Moodle. Anthology har kun et udvikler-image til AWS, som kræver login, udløber og har brug for langt mere RAM end dropletten. Derfor er der en mock (`blackboard/mock/app.py`), som svarer som Blackboard på præcis de kald, Nexus laver:

- **REST** (`/learn/api/public/...`): token med application key og secret, `terms`, `v3/courses` (også organisationer), medlemskaber med `expand=user` og `lastAccessed`-filter, `users` og `courseRoles`. Id'erne kan slås op med `_12_1`, `uuid:`, `courseId:`/`userName:` og `externalId:`. Svarene har `fields=` og `paging.nextPage` som i Learn.
- **LTI 1.3**: Hos en rigtig Blackboard går LTI-login og nøgler gennem Anthology Developer Portal (`developer.blackboard.com`). Mocken spiller den rolle: `/api/v1/gateway/oidcauth` og `/api/v1/management/applications/<id>/jwks.json`. `context.id` og `sub` er kursets og brugerens uuid, som i Learn.
- **Testside** på `https://<blackboard-domæne>/` (admin og `BB_MOCK_UI_PASSWORD`). Den viser de værdier, Nexus skal have, og lister kurser og medlemmer. Knappen **Åbn i Nexus** launcher Nexus som den bruger, og administratoren kan åbnes uden kursus (Nexus' admin-sider).

**Installér** (Nexus' rod-URL i `--tool-url`, uden sti):

```bash
curl -fsSL https://raw.githubusercontent.com/EmilMoe/lms/main/install.sh | sudo bash -s -- blackboard --domain=blackboard.example.com --path=/mnt/<volume> --tool-url=https://nexus.example.com --owner=deploy
```

Opdatering: kør igen med kun `--path`. Application ID, key, secret, deployment ID, kodeordet og LTI-nøglen (`data/lti-private.pem`) bevares. Mocken bruger 30-40 MB RAM.

**Testdata** ligger i `testdata/blackboard/` og er det samme datasæt som til de andre LMS'er, med lidt ekstra, så Nexus' filtre kan testes:

- ENG101 har ingen term, så Nexus' "No term" bruges.
- t02 er TeachingAssistant i ENG101.
- s18's medlemskab i ENG101 er slået fra (`available=No`).
- `LAERER` er en organisation, som Nexus springer over.
- `bbadmin` er systemadministrator og bruges til admin-launch.

CSV-filerne kopieres til `<path>/blackboard/seed/` og læses igen, når de ændres. Hver række får et fast `_N_1`-id og uuid, gemt i `data/blackboard.sqlite`. En række, der fjernes, forsvinder fra API'et, men får de samme id'er igen, hvis den kommer tilbage. En kørsel af `blackboard.sh` erstatter `seed/` med repoets version.

**lastAccessed** (kursusadgang): Hvert medlemskab har en fast profil. Lærere er inde de fleste hverdage i arbejdstiden, elever sjældnere og også om aftenen. `lastAccessed` er seneste besøg til og med nu. Fortiden ændrer sig aldrig, så hver daglige sync i Nexus finder nye besøgsdage. Et nyt medlemskab får `created` og historik `BB_MOCK_BACKFILL_DAYS` dage bagud.

**Sådan bruges den fra Nexus:**

1. Opret en Blackboard-forbindelse med testsidens **Base URL**, **Application ID** og **Application key**, og hent secret på serveren:

   ```bash
   sudo grep BB_MOCK_SECRET <path>/blackboard/.env
   ```

   Test forbindelsen. Den kontrollerer de samme rettigheder som mod en rigtig Learn, og mocken giver alle.
2. Tilføj LTI-registreringen manuelt på tenantens LTI-fane. Client ID er application ID'et. Åbn **Platform** og ret **Issuer**, **OIDC auth endpoint** og **Platform keyset URL** til testsidens værdier (standardværdierne peger på `developer.blackboard.com`), og tilføj **Deployment ID**.
3. Kør sync som normalt, og åbn Nexus fra testsiden. Sæt evt. "Target link" øverst (standard `lti/launch/course-insights`; `lti/launch` er Nexus' egen launch).

Indstillinger i `<path>/blackboard/.env` (genstart med `docker compose up -d mock`):

| Variabel | Standard | |
|---|---|---|
| `BB_MOCK_TOOL_URL` | (fra `--tool-url`) | Nexus' rod-URL: login på `/lti/login`, id_token til `/lti/launch...` |
| `BB_MOCK_BACKFILL_DAYS` | `30` | dages historik for nye medlemskaber |
| `BB_MOCK_DAILY_CALL_LIMIT` | `0` | fx `50`: svar 429 med `Retry-After` til midnat UTC efter så mange kald pr. døgn, som Anthologys grænse. `0` = ingen grænse |

**Det kan mocken ikke:** Den kan ikke skrive (kun GET efter token). Den tjekker ikke entitlements, så "mangler rettighed" kan ikke testes. Den har ingen Ultra-brugerflade, ingen deep linking og ingen LTI Advantage-tjenester (NRPS, AGS). Uoverensstemmelser med den rigtige Learn fanges derfor ikke her. Logs: `docker compose logs -f mock`.

## RAM

Alle tre kan køre samtidig, når de bare leverer demodata gennem API'et. Omtrentligt forbrug i tomgang:

| | RAM |
|---|---|
| Canvas (1 Passenger-proces, jobs-worker, Postgres, Redis) | 2-2,5 GB |
| Moodle (Apache/PHP, MariaDB) | 0,4-0,6 GB |
| Blackboard-mock | under 0,05 GB |
| Traefik og OS | 0,3-0,4 GB |

Det er tæt på 4 GB, så swapfilen tager spidserne. Spidserne kommer især, når Canvas starter (asset-load) og ved tunge baggrundsjob. Hold øje med `free -h` og `docker stats --no-stream`. Hvis serveren swapper hele tiden, så stop den ene stak (`docker compose stop`) eller opgradér dropletten til 8 GB.

## AGPLv3

Workflowet bygger upstream-koden uændret, og imagets labels peger på den præcise commit (`org.opencontainers.image.revision`). Config-filerne her er driftskonfiguration uden hemmeligheder. Egne tilretninger af Canvas-koden må ikke komme i dette offentlige repo eller image. Hvis vi en dag kører en ændret Canvas for andre brugere, skal kildekoden til ændringerne tilbydes dem.
