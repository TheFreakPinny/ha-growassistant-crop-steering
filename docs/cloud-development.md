# Cloud development baseline

Use the repository's `scripts/setup-cloud.sh` in a fresh cloud workspace with
Python 3 / venv bootstrap support. It installs managed Python **3.13.16**, installs
`.github/requirements-ci.txt` including **Home Assistant 2025.12.5**, checks
ordinary dependencies, then installs **pytest-homeassistant-custom-component
0.13.301** with `--no-deps`. It accepts only the documented HA 2025.12.4 versus
2025.12.5 metadata exception after installation and verifies imports. See
[fixture compatibility](../tests/lifecycle/README.md#fixture-dependency-compatibility).

```sh
bash scripts/setup-cloud.sh
source work/cloud/venv/bin/activate
LITELLM_LOCAL_MODEL_COST_MAP=True python -m pytest -q
python -m ruff check custom_components tests
python -m ruff format --check custom_components tests
python -m compileall -q custom_components tests
git diff --check
```

`GROWASSISTANT_SETUP_ROOT` can select another absolute setup directory. Use a
fresh directory when rebuilding. Installer caches and managed Python stay under
that directory; the HA dependency on uv does not replace the bootstrap installer.
The `work/` directory is ignored by Git, and pytest's default collection is
restricted to the repository's `tests/` directory so downloaded package tests
cannot contaminate the baseline.

## Repository and network readiness

Keep inherited proxy settings, TLS verification and configured CA trust. Inspect
`/etc/codex/network-policy.json` and the cloud environment status before network
work. The proxy sidecar must be reachable under the command's execution network
permissions; a DNS result for `proxy` alone does not prove readiness.

In the repaired session, commands without network permission could not connect
to `proxy:8080` (socket error `Operation not permitted`). Granting network access
through the supported execution permission mechanism allowed `git fetch origin
main` through that same proxy. No proxy bypass, TLS disabling or route changes
were necessary; environment recreation was unnecessary. The runtime status
reported policy state `unknown`, so it did not independently establish enforced
policy readiness; actual fetch/install results established destination access.

```sh
git status --short
git fetch origin main
git log -3 --oneline origin/main
```

For this repair, fetched main is
`893e4925363a56dfeb39cdc4d695c1bedd1e9f7c`, the merge of PR #68. The old checkout
was `a70caba4141f2dd1b6d5790089231e721908f9b9` (PR #67).

If supported execution network permission still cannot reach the proxy, repair
or recreate the managed environment's proxy/sidecar before proceeding. Do not
use a stale checkout for the repair. Permit GitHub and Python package/download
destinations through the supported configuration workflow. CLI GitHub API calls
also require `api.github.com` and appropriate repository credentials; this
session's policy did not list that host and `gh run view` returned `Forbidden`.
The authorized GitHub connector can provide repository API access independently.

## Late activation failure

`PumpController._task()` uses HA background tasks. HA 2025.12.5 documents that
`async_block_till_done()` excludes those tasks unless
`wait_background_tasks=True`. After a timed-out activation, `_stop_locked()`
sends off but retains the fault/recovery target while an earlier service is
unresolved. When that service eventually completes, `_service_finished()`
schedules `_reconcile_late_service()`, which takes the controller lock and sends
another off before durably clearing recovery.

The test released the delayed on handler and drained only foreground tasks.
It could observe on while the background reconciliation was still pending.
The correction explicitly drains background tasks before asserting off and now
also asserts IDLE and cleared recovery. A parameterized delayed reconciliation
case exercises the scheduling boundary. Replacing the drain with the old
foreground-only semantics makes that case fail with `on != off`; using the
background-aware drain passes. No pump implementation or safety assertion was
weakened. This identifies a test synchronization defect; repeated passing tests
do not prove the absence of every possible controller race.

All tests use local HA fixtures and simulated entities. Phase 2, production Home
Assistant and physical pumps remain outside this repair.
