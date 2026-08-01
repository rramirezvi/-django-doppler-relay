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
evidencia aprobada es: cliente HTTP 8/8, API V2 27/27 en PostgreSQL aislado,
suite ops 87/87 y repeticiones Linux deterministas. El gate aborta si el SHA o
el orden `d7de839...` → `1bc524a...` no coincide.

### B. Permitido durante el despliegue productivo

- `py_compile` y la suite `ops`, que usa repositorios temporales;
- `manage.py check` y `manage.py check --deploy`;
- ORM read-only, readiness, GET autenticados y smoke tests no destructivos;
- flags, allowlists y conteos read-only de jobs, BulkSend, ledger y
  EmailMessage.

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


