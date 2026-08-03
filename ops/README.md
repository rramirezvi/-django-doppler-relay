# Hardened deployment procedure

`deployment_hardening.py` implements a fail-closed deployment workflow without
hardcoding the application directory, virtualenv, service user, or Nginx host.
It does not modify Django application behavior.

## TD-02C worker restart gate

`td02c_worker_gate.py` classifies the worker state around the temporarily
approved `django.service` restart used by an import-only canary. Production
currently declares `Requires=django.service` in
`doppler-background-jobs.service`, so a new worker PID is expected when Django
is restarted and is not, by itself, an abort condition.

Before the restart, capture the worker unit's `MainPID`, `NRestarts`,
`ActiveState`, `SubState`, `ActiveEnterTimestamp`, `ExecMainStartTimestamp`,
`ExecStart`/command and `Requires`, plus queued/running job counts, running job
IDs and recent warning-or-higher journal entries. The precondition requires an
active, idle worker with the expected `process_background_jobs` command.

After the restart and application readiness, take at least two worker samples
over a brief bounded stability window. The gate passes when the worker is
active with one valid stable PID, the command and dependency are unchanged,
`NRestarts` has stabilized, job counts did not grow, no queued/running/orphaned
job exists, and no new critical journal message appeared. An unchanged healthy
PID also passes. Missing/different `Requires`, a restart loop, ambiguous PID,
unexpected work or errors fail closed with an explicit diagnostic.

Readiness remains layered and independent: `django.service`, Gunicorn socket,
Nginx/TLS, `GET /admin/login/ == 200`, then the worker stability gate. The gate
never restarts a unit and never changes application or database state.

## TD-02C effective-settings gate

`td02c_settings_gate.py` validates the six effective canary settings after
activation and again after deactivation. It uses the same
`relay.services.bulk_v2_canary.normalize_allowlist` function as the production
policy instead of comparing raw setting strings with Python sets.

The active gate accepts only the canonical raw values
`td02c-canary-import-v1-20260731` and `1`, whose normalized forms must be
exactly `{td02c-canary-import-v1-20260731}` and `{1}`. Whitespace, duplicates,
wildcards, prefixes, extra entries and invalid/non-positive user IDs fail
closed. The inactive gate requires both raw allowlists and both normalized
allowlists to be empty. Both modes also validate the engine/canary flags,
`max_rows=20`, and external template lookup disabled.

## TD-02C HTTP client contract

The canary client receives the unique validated `NginxTarget` produced by the
same active-configuration discovery used by deployment preflight. It rejects
zero or multiple candidates, wildcards, Nginx variables, invalid names, and
any separately asserted hostname that differs from that target. URL, Host,
SNI, Origin, Referer and local `--resolve` are all derived from this single
value. The client then uses normal TLS and local
`--resolve`, create a new authenticated session, and keep its cookie jar and
curl configuration in a private temporary directory (`0700`, files `0600`).
It obtains CSRF through a safe GET and confirms the cookie jar contains both
`sessionid` and `csrftoken` without printing either value.

The multipart POST sends the discovered Host, matching HTTPS `Origin` and
`Referer`, `X-CSRFToken` from the cookie, the authenticated cookie jar, and
`Accept: application/json`. It never uses `-k` or `--location`. Secret headers
belong in a mode-0600 curl config rather than argv. Record only method, path,
status, content type, sanitized Location, redirect count and duration; never
record response bodies, tokens, cookies, credentials or recipient data.

`201 application/json` is valid only for initial creation and `200
application/json` only for the one authorized idempotent retry. Redirects,
HTML and all other statuses abort without retry. The temporary workspace is
deleted on success and failure.

## Phases

### Preflight â€” before fast-forward

Preflight discovers and validates:

- systemd `WorkingDirectory`, `ExecStart`, user, group and environment files;
- the Python interpreter declared by any `ExecStart` script shebang;
- `manage.py`, Git branch/commits and runtime-file intersection;
- `manage.py check` on the current commit;
- the effective TLS Nginx vhost linked to the application socket;
- baseline smoke responses using the real host and loopback `--resolve`.

Running without `--execute` stops after Preflight:

```bash
sudo python3 ops/deployment_hardening.py \
  --service-unit django.service \
  --old-sha OLD_COMMIT \
  --target-sha TARGET_COMMIT \
  --remote origin \
  --branch PRODUCTION_BRANCH
```

This strictly local mode performs no fetch. The target commit must already
exist in the repository object database.

When the approved target object is absent, controlled acquisition must be
requested explicitly:

```bash
sudo python3 ops/deployment_hardening.py \
  --service-unit django.service \
  --old-sha OLD_COMMIT \
  --target-sha TARGET_COMMIT \
  --remote origin \
  --branch PRODUCTION_BRANCH \
  --fetch-target
```

`--fetch-target` is still non-deploying, but it is not metadata-read-only. It
writes only fetched objects and the hash-qualified temporary reference
`refs/deployment-preflight/TARGET_COMMIT`. Fetch uses `--no-tags`,
`--no-prune`, `--no-write-fetch-head`, and an empty `--refmap=`; it does not update the local branch,
HEAD, index, working tree, `FETCH_HEAD`, or `refs/remotes/origin/*`.
The procedure snapshots and compares those active Git states, validates the
temporary ref and remote hash, and deletes the ref with a compare-and-swap
`git update-ref -d` in `finally`. It never runs `git gc`.

An interrupted process may leave the isolated ref. A later controlled
acquisition accepts it only if it points exactly to the requested hash;
otherwise it aborts. The ref can be removed safely only by supplying its
observed object ID to the compare-and-swap cleanup command.

## Deployment target pinning

An isolated `--fetch-target` preflight proves that an object can be inspected,
but intentionally does not update `refs/remotes/origin/*`; therefore that mode
must never be treated as proof that `origin/<branch>` is deployable. Before an
authorized execution, use `--refresh-deployment-ref`. It performs an explicit,
no-tags/no-prune/no-FETCH_HEAD fetch from the approved branch to the exact
`refs/remotes/<remote>/<branch>` consumed by deployment, then requires that
reference to resolve to the full approved SHA. HEAD, branch, index and working
tree must remain byte-for-byte unchanged by this metadata update.

The commit sequence can be pinned by repeating `--expected-commit` in
OLD..TARGET order. Preflight rejects missing, reordered or additional commits.
Execution revalidates the remote-tracking ref and then merges the full target
object ID, never an abbreviation, `FETCH_HEAD`, or an unchecked branch name.
Immediately after the fast-forward it requires exact HEAD, a fully materialized
index/working tree for every range path, the original runtime file set and
hashes, and service-user ownership of every existing changed path.

### Post-update â€” after fast-forward

Execution additionally requires an explicit absolute backup root and explicit
authorization to restart the web unit:

```bash
sudo python3 ops/deployment_hardening.py \
  --service-unit django.service \
  --old-sha OLD_COMMIT \
  --target-sha TARGET_COMMIT \
  --remote origin \
  --branch PRODUCTION_BRANCH \
  --backup-root /ABSOLUTE/APPROVED/BACKUP/ROOT \
  --allowed-warning W005 \
  --allowed-warning W021 \
  --restart-web \
  --execute
```

Post-update performs:

1. exact fast-forward and HEAD verification;
2. `manage.py check` and `manage.py check --deploy` with the discovered
   interpreter, service user and working directory;
3. structured warning comparison;
4. migration/static diff gate;
5. only explicitly authorized restarts;
6. objective application-readiness polling;
7. baseline and security smoke tests;
8. runtime SHA256 verification.

## Application readiness

`systemctl is-active` is necessary but not sufficient: during the failed
production attempt systemd reported the unit started at `21:38:36.832490`,
Gunicorn did not listen on the Unix socket until `21:38:37.220561`, and its
second worker started at `21:38:37.332599`. Nginx returned 502 to smoke probes
issued in that window.

After an authorized restart, the procedure now polls these signals in order:

1. the discovered web unit is `active`;
2. the discovered absolute Unix socket exists and is a socket;
3. a real TLS request through the effective Nginx vhost to `/admin/login/`
   returns exactly the status captured in the preflight baseline.

Only then do deployment smoke tests begin. This proves the complete path
Nginx → socket → Gunicorn worker → Django can serve a known endpoint; socket
existence alone cannot produce a false ready result from a stale or
not-yet-serving socket. Polling defaults to every 250 ms with a fail-closed
60-second upper bound, configurable through `--readiness-poll-interval` and
`--readiness-timeout`. These values bound failure detection; they are not a
fixed startup sleep.

The hostname is never supplied by an operator or embedded in the runbook.
The procedure parses the active output of `nginx -T`, selects the single TLS
`server_name` whose proxy/upstream points to the socket discovered from
`django.service`, rejects empty, invalid, absent, wildcard, variable, or
ambiguous names, and verifies certificate coverage. Preflight then validates
the layers separately: active systemd unit, Unix socket, `nginx -t`, local TLS
connection through `127.0.0.1` with the discovered Host/SNI, and HTTP 200 from
`/admin/login/`. Its evidence records the discovered hostname, URL, Host
header, status, attempts and elapsed time without response bodies or secrets.
An HTTP 000 is therefore reported only after hostname/TLS/connection/Nginx and
socket context has been established; it is not classified directly as a
Django failure.

Any failure after mutation restores only the paths in the exact approved Git range from `OLD_COMMIT` with `git restore --source ... --staged --worktree`, then atomically restores the previous branch reference with `git update-ref`. Runtime files outside that range, `.env`, and untracked files are never included. A repeated rollback stops successfully when HEAD and the affected paths already match `OLD_COMMIT`; any ambiguous intermediate state aborts.

If execution is interrupted after `git restore` but before `git update-ref`,
HEAD still names `TARGET_COMMIT` while the index and affected working-tree paths
match `OLD_COMMIT`. This remains visible as a dirty tree, so a new deployment
preflight fails on the range/runtime intersection. Recovery is automatic only
when the operator explicitly invokes the rollback path again. The rollback
recognizes that exact split state, moves the branch reference to `OLD_COMMIT`,
verifies the old tree and runtime SHA256 values, and does not touch `.env`.
Any other split or ambiguous state aborts without alternative operations.
All Git restore and reference operations in rollback run as the discovered
systemd service user (normally `app`), including the idempotent recovery path.
Root must not restore working-tree files. If an emergency requires root, the
exception must immediately restore the discovered user/group, verify content
hashes and modes, and record `git status`; this is an operator exception, not
an automatic fallback.

Rollback captures OLD, TARGET, branch, the exact approved commit sequence,
range paths, and runtime metadata. It accepts HEAD at OLD or at any approved
intermediate commit, and it does not assume HEAD describes the index or working
tree. It restores the complete approved range from OLD (which also removes only
files introduced by that range), atomically moves the branch from the observed
approved HEAD back to OLD, and verifies runtime SHA256, mode, owner, group,
size, mtime and final Git state. A HEAD outside the approved range fails closed;
there is no root fallback.

## Worker restarts

Workers are not restarted automatically. Inspect their effective `ExecStart`
and dependency/import relationship with the changed files first. An explicitly
approved unit can be added with:

```bash
--restart-unit approved-worker.service
```

## Safety properties

- no `git reset --hard` or `git clean`;
- default mode is read-only: no fetch, pull, merge, checkout, backup, restart,
  repository write, or rollback occurs without `--execute`;
- no migration, collectstatic or dependency installation;
- no guessed hostname, virtualenv or user;
- no `source .env` and no secret values in reports;
- no authenticated send smoke test;
- ambiguous service, interpreter or Nginx discovery aborts.

## Local validation

```bash
python -m unittest ops.tests.test_deployment_hardening -v
python -m py_compile ops/deployment_hardening.py
git diff --check
```

## Production versus isolated test profiles

Las suites Django que crean una test database no se ejecutan contra
producción. Se validan previamente en PostgreSQL aislado y durante el
despliegue se verifica el mismo commit exacto junto con checks no destructivos.

### A. Obligatorio antes del despliegue, en aislamiento

- `manage.py test` y pruebas API que crean/destruyen la Django test database;
- PostgreSQL, concurrencia, rollback transaccional y escrituras de fixtures;
- evidencia ligada al SHA objetivo y a la secuencia exacta de commits.

Para TD-02C sobre `1bc524a487811c3a524025ceb323f82e4821e7bc`, la
evidencia aprobada fue: cliente HTTP 8/8, API V2 27/27 en PostgreSQL aislado,
suite ops 87/87 y repeticiones Linux deterministas. El gate aborta si el SHA o
el orden `d7de839...` → `1bc524a...` no coincide.

`validate_predeployment_evidence` (ops/deployment_test_profile.py) exige pisos
mínimos, no conteos exactos: `api_v2_passed`, `http_client_passed` y
`ops_passed` deben ser mayores o iguales a `MINIMUM_API_V2_PASSED`,
`MINIMUM_HTTP_CLIENT_PASSED` y `MINIMUM_OPS_PASSED`. Estas suites crecen con el
tiempo por cobertura legítima; cada despliegue posterior debe aportar
evidencia real igual o superior al conteo real vigente, y el piso solo se
actualiza cuando el conteo real de la suite correspondiente crece de forma
estable.

### B. Permitido durante el despliegue productivo

- `py_compile` exclusivamente mediante `ops/isolated_py_compile.py`. El helper
  recibe el repositorio, el Python y el usuario descubiertos desde systemd.
  Crea directamente como ese usuario un workspace unico
  `td02c-pycache-XXXXXXXXXX`, modo `0700`, bajo un directorio temporal absoluto,
  real, no symlink y externo al checkout. No reutiliza un cache root fijo. Antes
  de compilar prueba efectivamente crear, escribir, leer y borrar dentro del
  workspace. `PYTHONPYCACHEPREFIX` apunta solo a ese workspace. El helper
  verifica owner, grupo, modo, `git status` y que no aparezcan `.pyc` en el
  checkout, y elimina de forma fail-closed exactamente el workspace creado en
  exito, error, excepcion o señal terminable. No cambia ni elimina ningun
  `__pycache__` preexistente;
- la suite `ops`, que usa repositorios temporales;
- `manage.py check` y `manage.py check --deploy`;
- ORM read-only, readiness, GET autenticados y smoke tests no destructivos;
- flags, allowlists y conteos read-only de jobs, BulkSend, ledger y
  EmailMessage.

#### Diagnóstico del GET autenticado TD-02C

El GET autenticado permitido debe ejecutarse mediante
`run_authenticated_get_gate` de `ops.td02c_http_client`. El coordinador recibe
operaciones secret-bearing inyectadas y emite una línea JSON sanitizada por
subetapa: creación del workspace `0700`, cookie jar `0600`, descubrimiento de
vhost, preparación TLS/resolución local, GET de login, autenticación, presencia
de `sessionid` y `csrftoken`, GET autenticado, clasificación de respuesta y
limpieza final.

Cada registro contiene únicamente subetapa, PASS/FAIL, exit code, duración y
clasificación segura. Los registros HTTP se limitan a método, ruta, status,
Content-Type, Location sin query/fragment/credenciales, redirects,
`ssl_verify_result` y tiempo total. Nunca se registran cuerpos, cookies,
tokens, credenciales, formularios ni headers sensibles. La representación del
comando se construye antes de ejecutar con marcadores `<redacted>` y no se
reconstruye desde argv después de un fallo.

El gate no sigue redirects. El único redirect aceptado es el `302` explícito y
no seguido de `POST /admin/login/` hacia `/app/`; cualquier otro 302 se clasifica
como `authentication_failed`; TLS, conexión, cookies ausentes, status inesperado,
Content-Type inesperado y fallos de limpieza tienen clasificaciones distintas.
Todo estado ambiguo termina en `unknown_failure`. La limpieza ocurre en
`finally` y un fallo al eliminar temporales también aborta. El workspace se
mantiene fuera del checkout y no se conserva como evidencia.

##### Diagnóstico granular del descubrimiento de Nginx (`nginx_target_discovered`)

`CurlOperations.discover_target()` ya no delega en un único paso opaco: divide
el descubrimiento en diez subetapas observables, cada una con su propia línea
JSON sanitizada, antes de que el `stage()` externo registre el resultado final
de `nginx_target_discovered`. La decisión de vhost sigue siendo exactamente la
de `discover_nginx_target()` (sin cambios); las subetapas solo añaden
diagnóstico read-only para diferenciar la causa:

1. `service_metadata_loaded` — valida metadata del servicio ya descubierta y
   reconfirma con `systemctl is-active` (`service_metadata_invalid`,
   `systemctl_failed`).
2. `service_working_directory_validated` — `WorkingDirectory` absoluto y
   existente (`working_directory_invalid`).
3. `nginx_config_tested` — `nginx -t` (`nginx_test_failed`,
   `command_permission_denied`, `command_not_found`, `subprocess_failed`).
4. `nginx_config_dumped` — `nginx -T` (`nginx_dump_failed`,
   `nginx_output_empty`, mismas clasificaciones de ejecución).
5. `vhost_candidates_parsed` — cuenta candidatos de `server_name` con las
   mismas reglas de coincidencia que `discover_nginx_target` (`no_vhost_found`,
   `wildcard_vhost_rejected`, `variable_vhost_rejected`,
   `nginx_output_unparseable`).
6. `unique_vhost_selected` — delega la decisión final en
   `discover_nginx_target()` sin modificarla (`multiple_vhosts_found`,
   `no_vhost_found`).
7. `certificate_paths_discovered` — existencia del archivo de certificado
   público (`certificate_not_found`).
8. `certificate_hostname_validated` — `openssl x509 -checkhost`
   (`certificate_hostname_mismatch`).
9. `local_resolution_prepared` — verificación defensiva de que el hostname y
   el socket seleccionados son seguros para `--resolve` (`local_resolution_invalid`).
10. `nginx_target_validated` — confirmación final PASS.

Cualquier excepción no clasificada en una subetapa se registra como
`unexpected_discovery_error` en la subetapa que se estaba ejecutando; el fallo
sigue siendo fail-closed: no se ejecuta HTTP, TLS, ni creación de sesión, y el
cleanup de workspace/cookies/credencial ocurre igual que antes. Cada línea
admite un campo opcional `detail` con un resumen corto y saneado (comando,
cantidad de candidatos, hostname seleccionado, ruta pública del certificado, o
la primera línea de stdout/stderr pasada por el mismo `redact_output` que usa
el orquestador de despliegue). Nunca se registra el volcado completo de
`nginx -T`, claves privadas, credenciales, cookies ni cuerpos HTTP.

La ejecución productiva se realiza exclusivamente como módulo del paquete
versionado `ops.td02c_authenticated_get_runner`; un wrapper temporal no puede
reconstruir cookies, buscar sesiones ORM existentes ni ejecutar `curl` por su
cuenta. Su única invocación permitida es equivalente a:

```bash
cd /opt/app/django-doppler-relay
.venv/bin/python -m ops.td02c_authenticated_get_runner \
  --service-unit django.service \
  --credential-file /ruta/temporal/externa/credential
```

La ejecución directa `python ops/td02c_authenticated_get_runner.py` no está
soportada y no admite fallback. Antes de solicitar la credencial, el runbook
comprueba sin ejecutar el runner:

```bash
cd "$DISCOVERED_WORKING_DIRECTORY"
"$DISCOVERED_PYTHON" -c "import ops.td02c_authenticated_get_runner"
```

Al iniciar con `-m`, el runner vuelve a comprobar fail-closed que el
`WorkingDirectory` descubierto desde `django.service` coincide exactamente con
el directorio actual, que el archivo del módulo existe, que la raíz del
repositorio está en `sys.path` y que el usuario efectivo coincide con el usuario
del servicio. Las comprobaciones solicitadas bajo ese usuario se ejecutan
directamente cuando el proceso ya corre como él; `runuser` se utiliza solamente
cuando el invocador es `root`. Cualquier otro desajuste de usuario aborta sin
leer la credencial, crear workspaces, iniciar HTTP ni buscar sesiones. No
modifica `PYTHONPATH`, no introduce hacks de `sys.path` y no
permite ejecución como otro usuario.

El archivo contiene solo la contraseña técnica, debe ser absoluto, externo al
checkout, no symlink, propiedad del usuario operativo y modo `0600`. El runner
lo elimina verificando device/inode. El usuario está fijado por contrato a
`user_id=1`, `username=ricardo`. Tras validar actividad, staff y permisos, el
runner toma baselines de conteos, ejecuta `GET /admin/login/`, el único POST
permitido `POST /admin/login/`, y `GET /app/`. Identifica la nueva sesión por la
cookie obtenida y la diferencia frente al baseline, elimina solamente esa clave
y verifica que sesiones preexistentes y datos funcionales no cambien. Si la
credencial falta o la sesión es ambigua, aborta sin buscar sesiones existentes.

El gate invoca el helper con los valores descubiertos desde `django.service`,
no con rutas o usuarios codificados:

```bash
python3 ops/isolated_py_compile.py \
  --repository "$DISCOVERED_WORKING_DIRECTORY" \
  --python "$DISCOVERED_PYTHON" \
  --service-user "$DISCOVERED_SERVICE_USER" \
  --cache-root /tmp \
  ops/td02c_http_client.py \
  ops/deployment_hardening.py \
  ops/td02c_worker_gate.py
```

`--cache-root` designa solo el padre temporal seguro; el workspace hijo se crea
con `mktemp` como el usuario operativo y nunca usa `/tmp/td02c-pycache-root` ni
otro nombre fijo. La compilacion, la prueba efectiva de escritura, los snapshots
Git y la limpieza se ejecutan como el usuario operativo descubierto. Un error
de sintaxis conserva el exit code no-cero. Solo se registran creación y metadata
no sensible, resultado del probe/compilación y cleanup; nunca el contenido del
cache.

### C. Prohibido en producción

- `manage.py test` cuando intenta crear `test_doppler_prod`;
- pruebas que escriben o hacen `flush` sobre `doppler_prod`;
- `TransactionTestCase` contra la base real;
- conceder `CREATEDB`, usar `root`/`postgres`, o crear una base improvisada.

`ops.deployment_test_profile` aplica esta clasificación de forma fail-closed:
el perfil `production` rechaza suites Django con test database, creación de
bases y elevación de privilegios; el perfil `isolated` permite esas suites en
el entorno preparado para ello. Ningún gate de Git, runtime o rollback se
relaja.


## Orquestador único de despliegue TD-02C

`ops.td02c_deployment_runner` es el único propietario del flujo operativo de
preflight, despliegue y rollback TD-02C. El contrato del wrapper externo es una
sola invocación desde el `WorkingDirectory` descubierto:

```bash
.venv/bin/python -m ops.td02c_deployment_runner \
  --mode preflight-only \
  --old-sha SHA_INICIAL_COMPLETO \
  --target-sha SHA_OBJETIVO_COMPLETO \
  --branch operator-ui-production-test \
  --expected-commit SHA_OBJETIVO_COMPLETO \
  --validation-evidence /ruta/externa/evidencia-aislada.json \
  --evidence-output /ruta/externa/preflight.json
```

Los modos admitidos son `preflight-only`, `deploy-only` y `rollback`. El
orquestador llama directamente a `deployment_hardening`,
`deployment_test_profile`, `isolated_py_compile`, `td02c_settings_gate` y
`td02c_worker_gate`. La evidencia se escribe en JSON con modo `0600`; sus
temporales atómicos se eliminan mediante `finally`.

`preflight-only` es estrictamente de lectura para Git: no hace `fetch`, no
actualiza refs, no escribe `FETCH_HEAD` ni incorpora objetos. Comprueba el
remoto con `ls-remote` y exige que el objeto aprobado ya esté disponible para
el análisis local. El refresh explícito de la ref consumida pertenece
exclusivamente a `deploy-only`. Los tres modos comparten un `flock` exclusivo
por repositorio, externo al checkout y modo `0600`; una segunda ejecución
aborta y nunca elimina el lock de un proceso activo. `SIGINT` y `SIGTERM`
liberan temporales/lock antes del merge y disparan rollback dirigido si el
merge ya comenzó.

La evidencia debe usar una ruta absoluta externa al checkout, con directorio
`0700` y archivo `0600`, sin symlinks. Se publica mediante tempfile más rename
atómico y se vuelve a leer como JSON antes de declarar PASS. Un fallo de
evidencia posterior al merge obliga a rollback. `check --deploy` acepta
exclusivamente `W005` y `W021`; cualquier otro warning, error o salida de
warnings malformada aborta.

Está prohibido reconstruir en Bash el refresh Git, merge, rollback, readiness,
settings, cambio de usuario o manejo de pycache. También están prohibidos
`/tmp/td02c-pycache-root`, cualquier cache root fijo, `curl` ad hoc,
autenticación ad hoc, `runuser` anidado y la ejecución directa de archivos
`ops/*.py`. `isolated_py_compile_ephemeral` crea como el usuario operativo un
padre y workspace únicos, prueba escritura/lectura/eliminación, compila y
elimina ambos tanto en éxito como en error. La misma función se utiliza antes y
después del merge.

El primer despliegue que incorpora el propio orquestador es un bootstrap
especial: el módulo aún no existe en el HEAD productivo anterior. Debe revisarse
y autorizarse expresamente usando únicamente el
`python -m ops.deployment_hardening` que ya está versionado en el HEAD
productivo. Ese bootstrap no puede reconstruirse en Bash ni ejecutar el nuevo
orquestador desde una copia ad hoc. Su única interfaz es:

```bash
.venv/bin/python -m ops.deployment_hardening \
  --service-unit django.service --old-sha OLD --target-sha TARGET \
  --remote origin --branch operator-ui-production-test \
  --expected-commit TARGET \
  --bootstrap-module ops.td02c_deployment_runner
```

El bootstrap ejecuta el preflight inicial una sola vez con `HEAD=OLD`, valida
remotamente sin mutar, refresca de forma controlada y hace fast-forward al SHA
completo. Después usa un gate post-merge separado que exige `HEAD=TARGET` y
comprueba rama, índice, paths unmerged, materialización, runtime con metadata,
ownership, archivos añadidos/eliminados, settings desactivados e importación del
módulo; nunca vuelve a ejecutar el preflight de `HEAD=OLD`. Luego se detiene.
Rechaza usarlo si el módulo ya existía y no realiza
backup de datos, migraciones, reinicios, readiness posterior, smoke tests,
dependencias, `collectstatic`, cambios de `.env` ni canary. Después de
incorporarlo, todo preflight,
deploy y rollback TD-02C se realiza exclusivamente con el nuevo módulo.
