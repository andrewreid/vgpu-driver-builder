# TODO

Outstanding work items. Severity: 🔴 critical / 🟡 major / 🔵 minor / ❓ question.

For repository orientation read [AGENTS.md](AGENTS.md) first.

---

## 1. Open bugs

(none)

---

## 2. Risks / hardening

### 🔵 BuildKit pod runs `privileged: true`

`operator/src/vgpu_driver_operator/job_factory.py` (around line ~210). After
Bug 10, rootless mode was abandoned because rootlesskit cannot unshare mount
namespace under k3s/RKE2 default kernel + cgroup config. This works but it's a
real privilege escalation surface. Consider:

- Document the requirement explicitly in `docs/installation.md`.
- Investigate `moby/buildkit:rootless` with `--oci-worker-no-process-sandbox`
  on a kernel/cgroup configuration that supports user namespaces.
- Or fence to a dedicated build-only node pool via taints/tolerations.

---

## 3. Code TODOs / future work

### Tests

- `parse_precompile_tag` rc-driver regression test added; consider
  fuzzing against a corpus of real GPU-Operator-generated tags.

### Real Flatcar cluster verification

Pending. The Debian k3s test cluster validated everything except true on-node
module load. Run a full end-to-end on the Flatcar RKE2 dev cluster — see
`docs/examples/flux/` for the manifests.

---

## 4. Open design plans

### NVIDIA upstream alignment for `nvidia-driver` + `Dockerfile.prebuilt`

Plan: `~/.claude/plans/considering-specifically-charts-vgpu-dr-nested-rabin.md`

Status: planned, not implemented. Three sequenced PRs:

1. Replace manual `mkprecompiled` + `ld -r` relink with a single
   `nvidia-installer` invocation. Net deletion ~250 lines.
2. Add `KERNEL_MODULE_TYPE` (open / proprietary / auto) — required for
   Hopper+/Blackwell GPUs.
3. Refcnt-aware unload, flock on PID file, modprobe params from
   `/drivers/*.conf`, `NVreg_CoherentGPUMemoryMode=driver` default,
   fast-path config-digest skip.

Read the plan file in full before touching the build assets.

---

## 5. Resolved (kept for context)

- **Bug 1 — Registry HTTP calls have no timeout** — FIXED:
  `registry.py` now applies `timeout=(5, 10)` to registry and bearer-token
  HTTP calls, raises typed `RegistryUnreachable` for timeouts/connection
  failures, and `_do_reconcile` surfaces that as
  `Reconciled=False/RegistryUnreachable` before returning.
- **Bug 2 — Operator emits no INFO-level reconcile logs** — FIXED:
  `_do_reconcile` now logs reconcile start, node-discovery summary, registry
  idempotency hits, job creation, status patching, and final ready/building/
  pending/failed counts. Existing `on_job_event` and poller INFO logs now have
  regression coverage.
- **Bug 3 — `operator/Dockerfile` missing `ENV PYTHONUNBUFFERED=1`** —
  FIXED: `operator/Dockerfile` sets `PYTHONUNBUFFERED=1` so `kubectl logs`
  receives Python stdout promptly.
- **Test-plan label mismatch** — RESOLVED: grep confirmed no live docs use
  `=vgpu-build`; only the Job *name* prefix uses `vgpu-build-` which is
  unrelated. Note kept in this file only; closed.
- **Bug 5 — Helm field-manager conflict on imagePullPolicy** — RESOLVED at
  documentation level: `docs/installation.md` has an Operations /
  Troubleshooting section covering symptom, root cause, recommendation and
  three workarounds (server-side=false, jq strip-managedFields + replace,
  force-conflicts). Chart `templates/NOTES.txt` carries the same warning
  post-install.
- **Anonymous-registry CR path test** — RESOLVED: two tests added in
  `tests/test_job_factory.py` covering `registry_secret_name=None`
  (no docker-config volume/mount) and `="my-secret"` (volume + mount
  present).
- **Document flatcar-developer base image lifecycle** — RESOLVED: subsection
  added to `docs/architecture.md`. Image is published from a separate repo
  owned by the operator's author and auto-rebuilt by GitHub Actions on
  upstream Flatcar releases.
- **Update Flux examples + docs to use OCI Helm chart** — RESOLVED:
  `docs/examples/flux/gitrepository.yaml` removed; `ocirepository.yaml`
  added (uses `source.toolkit.fluxcd.io/v1`, with optional GHCR
  `secretRef` documented for private packages). `helmrelease.yaml`,
  `kustomization.yaml`, and `README.md` updated. `docs/installation.md`
  leads with OCI install (`helm install ... oci://ghcr.io/andrewreid/charts/vgpu-driver-operator`)
  and retains local-path install for development.
- **Bug 6/8** — operator unconditionally provided a registry-secret name even
  without `authSecretRef`. FIXED: dropped hardcoded default in `main.py`,
  `reg_secret_name` now flows as `None` when neither `authSecretRef` nor
  `REGISTRY_SECRET_NAME` env is set, so `job_factory.py` skips the secret mount.
- **Bug 9** — URI placeholder syntax mismatch. FIXED: `job_factory.py`
  accepts both `${DRIVER_VERSION}` and `{driverVersion}` (Python str.format
  style). Tests added.
- **status.builds[].tag placeholder for precompile** — FIXED: `on_job_event`
  now resolves the real published tag via `registry.find_matching_tags()`
  when a precompile build hits Ready, rewriting the placeholder.
- **status.conditions[Reconciled] not re-evaluated on build completion** —
  FIXED: `on_job_event` re-evaluates and patches the condition (status,
  reason, message) using the same shape `_do_reconcile` writes
  (`AllBuildsReady` / `BuildsPending`).
- **reconciler regex over-permissive on driver-version boundary** — FIXED:
  `parse_precompile_tag` and `extract_kernel_from_precompile_tag` now anchor
  the driver pattern explicitly and require kernel to start with `\d+\.\d+`.
  Regression test asserts runtime-style rc-driver tags do NOT false-match.
- **`on_job_event` entries missing `mode`** — FIXED: derived from the Job's
  `vgpu.flatcar.io/mode` label when appending a new build entry.
- **No input validation on driverVersions** — FIXED: CRD pattern
  `^[0-9]+\.[0-9]+(\.[0-9]+)?(-[A-Za-z0-9.]+)?$` rejects shell
  metacharacters at admission time.
- **Debounce thread swallowing exceptions** — FIXED: thread body wrapped in
  try/except that logs via the module logger so silent death is visible.
- **`flatcar.versions` schema regex disallows pre-release suffixes** — FIXED:
  pattern loosened to `^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.]+)?$`.
- **Single-replica deployment without leader election** — FIXED at
  documentation level: chart `templates/NOTES.txt` now warns operators
  against horizontal scaling and against `kubectl set image`/`patch`.
- **Bug 7** — chart `secret-s3.yaml` keys (FIXED in chart; renders
  `S3_ENDPOINT_URL` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`).
- **Bug 10** — rootless BuildKit unshare failure (FIXED via
  `privileged: true`; commit `0ef9ff5`).
- **Bug 11** — chart build assets gitignored, ConfigMap rendered empty
  (FIXED; commit `9f91254`).
- **Bug 12** — operator coined fake `flatcarVersion="12"` from Debian
  `VERSION_ID` (FIXED; commit `ef65dfc`).
- **Bug 13** — Flatcar 4593.x feed lacks `FLATCAR_KERNEL_VERSION`, precompile
  silently falls back to runtime (FIXED via two-phase buildctl that discovers
  kernel from base image; latest commit at the time of writing).
- **Plan BUG-1, 3, 4, 5** (ConfigMap env injection, extraArgs, startupProbe,
  `app.kubernetes.io/component=builder` label) — all FIXED pre-merge.
- **Critical: discoverFromNodes returned no versions on real Flatcar
  cluster** — `core_api.list_node().items[*].to_dict()` emits snake_case
  (`os_image`, `node_info`); parser reads camelCase as Kubernetes API JSON.
  All Flatcar nodes silently skipped, CR stuck at `NoFlatcarVersions`.
  Untested live until 2026-05-09 because all prior runs used
  `discoverFromNodes: false`. FIXED: switched both `main.py:343-344` and
  `crd.py:99-101` to `client.ApiClient().sanitize_for_serialization()`.
  Two regression tests added in `test_flatcar.py`: one positive path through
  `sanitize_for_serialization`, one permanent guard asserting `to_dict()`
  fails to parse so any regression is visible immediately.
