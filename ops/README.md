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
6. baseline and security smoke tests;
7. runtime SHA256 verification.

Any failure after mutation restores only the paths in the exact approved Git range from `OLD_COMMIT` with `git restore --source ... --staged --worktree`, then atomically restores the previous branch reference with `git update-ref`. Runtime files outside that range, `.env`, and untracked files are never included. A repeated rollback stops successfully when HEAD and the affected paths already match `OLD_COMMIT`; any ambiguous intermediate state aborts.

If execution is interrupted after `git restore` but before `git update-ref`,
HEAD still names `TARGET_COMMIT` while the index and affected working-tree paths
match `OLD_COMMIT`. This remains visible as a dirty tree, so a new deployment
preflight fails on the range/runtime intersection. Recovery is automatic only
when the operator explicitly invokes the rollback path again. The rollback
recognizes that exact split state, moves the branch reference to `OLD_COMMIT`,
verifies the old tree and runtime SHA256 values, and does not touch `.env`.
Any other split or ambiguous state aborts without alternative operations.

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


