# Hardened deployment procedure

`deployment_hardening.py` implements a fail-closed deployment workflow without
hardcoding the application directory, virtualenv, service user, or Nginx host.
It does not modify Django application behavior.

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


