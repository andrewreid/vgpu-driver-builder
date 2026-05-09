# TODO

Outstanding work items. Severity: 🔴 critical / 🟡 major / 🔵 minor / ❓ question.

For repository orientation read [AGENTS.md](AGENTS.md) first.

---

## 1. Open bugs

### 🟡 Bug 6/8 — operator unconditionally provides a registry-secret name even without authSecretRef

`operator/src/vgpu_driver_operator/main.py:428-430`. The expression

```python
reg_secret_name: str | None = auth_secret_name or os.environ.get(
    "REGISTRY_SECRET_NAME", "private-registry-secret"
) or None
```

always evaluates to the string `"private-registry-secret"` when both
`spec.registry.authSecretRef` and the `REGISTRY_SECRET_NAME` env var are absent.
The trailing `or None` is dead code (a non-empty string is truthy). The
build-job mount is conditional on `registry_secret_name is None`
(`job_factory.py:182, 199`), but it never *is* None at the call site, so the
stub Secret workaround documented in `docs/examples/flux/` remains required.

**Fix**: distinguish "explicit default override" (env var) from "fall through
to no auth". Drop the hardcoded default in `main.py`; let `auth_secret_name or
os.environ.get("REGISTRY_SECRET_NAME") or None` evaluate naturally.

### 🟡 Bug 9 — URI placeholder syntax mismatch

`operator/src/vgpu_driver_operator/job_factory.py:130`. The operator only
honors `${DRIVER_VERSION}`. The CRD docs and migration examples historically
used `{driverVersion}` (Python `str.format` style). README and flux examples
have been updated to match the operator, but the operator could accept both
syntaxes for ergonomics.

**Fix**: add a second `replace("{driverVersion}", key.driver)` call. Decide
which is canonical and update documentation.

### 🟡 status.builds[].tag shows placeholder for precompile

`operator/src/vgpu_driver_operator/main.py` (status entry construction during
reconcile, ~line 460-466). For precompile builds, the entry stores
`<driver>-<kernel>-flatcar<flatcar>` literally — the operator has no kernel
string to interpolate at dispatch time. Once the build completes, no code path
reads the published tag back from the registry to rewrite the placeholder.

**Fix**: in `on_job_event` when a Job hits `Ready`, list registry tags matching
the precompile pattern, parse the kernel via
`reconciler.extract_kernel_from_precompile_tag`, and rewrite the status entry.

### 🟡 status.conditions[Reconciled] not re-evaluated on build completion

`operator/src/vgpu_driver_operator/main.py:233-238`. `on_job_event` patches
`status.builds[]` immediately, but never updates the `Reconciled` condition.
The condition is only refreshed by the 10-minute periodic timer, so a CR can
sit at `BuildsPending`/`status: False` for up to 10 minutes after the last
build completes.

**Fix**: in `on_job_event`, after patching `builds`, re-evaluate the condition
state (`AllImagesPresent` if every entry is `phase: Ready`, else
`BuildsPending`).

### 🟡 reconciler regex over-permissive on driver version boundary

`operator/src/vgpu_driver_operator/reconciler.py:125`. `parse_precompile_tag`
splits on `re.search(r"(\d)-(\d)", remainder)` — the FIRST digit-dash-digit
boundary in the string. A tag like `1-2.3.4-5.6.7-flatcar4593.2.0` would split
at the wrong boundary if the kernel string starts with a digit and the driver
version contains an internal `-`. Today's drivers don't trip it, but custom
driver-version strings (e.g. `535.104-rc1`) could.

**Fix**: anchor the regex to the END of the driver version pattern, or store
the boundary explicitly when computing the tag and parse symmetrically.

### 🟡 on_job_event entries missing `mode` field

`operator/src/vgpu_driver_operator/main.py:223-230`. When `on_job_event`
appends a brand-new build entry (race between Job-event and reconcile-loop),
it omits `mode`. The reconcile path always sets it. Status consumers that key
on `mode` get heterogeneous data.

**Fix**: derive `mode` from the Job's `vgpu.flatcar.io/mode` label and include
it in the appended entry.

### 🔵 Test-plan label mismatch in command examples

Old test-plan said `app.kubernetes.io/component=vgpu-build`. Actual label is
`app.kubernetes.io/component=builder` (`job_factory.py:161`). AGENTS.md is
correct; ensure any new docs also use `=builder`.

### 🔵 Bug 5 — helm field-manager conflict on imagePullPolicy

Operational rather than code: when an operator was previously patched with
`kubectl set image` or `kubectl patch`, subsequent `helm upgrade` (server-side
apply) errors with a field-manager conflict on `imagePullPolicy`. Workaround:
`helm upgrade --server-side=false` or strip stale `managedFields` manually.

**Long-term fix**: chart docs should warn against `kubectl set image`. Already
implicit in AGENTS.md §10.

---

## 2. Risks / hardening

### 🟡 No input validation on driverVersions

`charts/vgpu-driver-operator/crds/vgpudriverimages.vgpu.flatcar.io.yaml:45-46`.
The schema accepts any string, including shell metacharacters. Currently this
flows into `s3 cp` URIs and BuildKit build-args via shell expansion in
`job_factory.py`. A malicious or fat-fingered CR (`"535.04; rm -rf /"`) could
inject into the build job.

**Fix**: add CRD `pattern: '^[0-9]+\\.[0-9]+(\\.[0-9]+)?(-[A-Za-z0-9.]+)?$'`
or similar. Bonus: the operator should reject driver versions containing shell
metacharacters defensively.

### 🟡 Debounce thread swallows exceptions

`operator/src/vgpu_driver_operator/main.py:100-109` (the daemon thread spawned
by `on_node_event` to coalesce node updates). If
`_touch_all_vgpu_driver_images` raises, the thread dies silently — node-driven
reconciliation stops and there is no log line.

**Fix**: wrap the thread body in `try/except Exception as exc` and log via the
kopf logger (capture into a closure or use a module-level logger).

### 🔵 `flatcar.versions` schema regex doesn't allow pre-release suffixes

`crds/vgpudriverimages.vgpu.flatcar.io.yaml`: `pattern:
'^[0-9]+\\.[0-9]+\\.[0-9]+$'`. Real Flatcar releases occasionally ship with
suffixes (e.g. `-rc1` on alpha channel). Tighten or loosen depending on
intent; today an alpha tracker would silently drop versions that fail
validation.

### 🔵 BuildKit pod runs `privileged: true`

`operator/src/vgpu_driver_operator/job_factory.py` (around line ~210). After
Bug 10, rootless mode was abandoned because rootlesskit cannot unshare mount
namespace under k3s/RKE2 default kernel + cgroup config. This works but it's a
real privilege escalation surface. Consider:

- Document the requirement explicitly in `docs/installation.md`.
- Investigate `moby/buildkit:rootless` with `--oci-worker-no-process-sandbox`
  on a kernel/cgroup configuration that supports user namespaces.
- Or fence to a dedicated build-only node pool via taints/tolerations.

### 🔵 Single-replica deployment without leader election

`operator/src/vgpu_driver_operator/main.py:1` (and chart). Comment says
multi-replica is out of scope, but the chart does not enforce
`spec.replicas: 1` or surface a warning. If a user scales the deployment,
dual reconciliation loops will race on Job creation and CR status patching.

**Fix**: add a chart `NOTES.txt` warning, or hardcode `replicas: 1` in the
Deployment template (override discouraged).

---

## 3. Code TODOs / future work

### Tests

- Anonymous-registry CR path (no `authSecretRef`) — requires Bug 6/8 fix first.
- Both `${DRIVER_VERSION}` and `{driverVersion}` URI placeholder syntaxes
  (after Bug 9 fix).
- Non-flatcar node rejection (partially covered by `test_flatcar.py` cases
  added in run 9 — extend to integration scope).
- `parse_precompile_tag` edge cases once the regex is tightened.

### Real Flatcar cluster verification

Pending. The Debian k3s test cluster validated everything except true on-node
module load. Run a full end-to-end on the Flatcar RKE2 dev cluster — see
`docs/examples/flux/` for the manifests.

### Document `flatcar-developer:<version>-sources` base image lifecycle

`charts/vgpu-driver-operator/files/build/Dockerfile.prebuilt:16` references
`ghcr.io/andrewreid/flatcar-developer:<flatcar_version>-sources`. We do not
document who maintains this image, how to publish a new variant when Flatcar
ships a new release, or whether it lives in this repo or a separate one.
Add to `docs/architecture.md` or `docs/installation.md`.

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
