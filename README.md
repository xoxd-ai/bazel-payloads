# bazel-payloads

Immutable, sha256-pinned build payloads (npm bundles, toolchain archives) served at pinned commits for xoxd-ai/bazel-registry module sources. TIN-7477 R581.

Consumers fetch a payload as `https://raw.githubusercontent.com/xoxd-ai/bazel-payloads/<40-hex commit on main>/<path>`, one GET with no redirect and no query, which the GFTB cell admits (policy `a583e632`). `xoxd-ai/bazel-registry` stays the index: its `source.json` names that URL and the archive's `sha256-` integrity from `SHA256SUMS`.

## Layout

| Path | What |
|---|---|
| `locks/gftb-site/` | P0 1.0.0 input: gftb-site's `pnpm-lock.yaml`, `package.json`, `pnpm-workspace.yaml`, `.pnpmfile.cjs` and `patches/`, moved here under R577 (from gftb-site #168 at `7cd2b83d`). `.npmrc` is kept for `lock-update` and is not exported. Immutable once merged: a later P0 version adds `locks/<its lock plan name>/` |
| `plans/<name>.json` | One plan per payload. An npm bundle names its direct packages and pins the closure count and the sha256 of the sorted `name@version` list; a lock plan pins the lock sha256; the Node plan pins the release signer and the rules_nodejs sha256 table |
| `tools/` | `payloads.py` (closure walk, fetch, packer, `SHA256SUMS`, tree rules, pin lint), `propose.py`, `reproduce.py`, `hopcheck.py` (S'' and PR-G1 hop predicates), tests |
| `keys/node/` | The Node release key that signs v22.22.0's `SHASUMS256.txt` (`CC68F5A3106FF448322E48ED27F5E38D5B0A215F`), from `nodejs/release-keys` at `481637f8` |
| `flake.nix`, `flake.lock` | Python 3.12 with its zlib and PyYAML, GnuPG, and pnpm 10.13.1 for `lock-update` |
| `payloads/<module>/<version>/` | `<module>-<version>.tar.gz` and a copy of its `MANIFEST.json` for review diffs |
| `node/v<version>/` | Node archives, byte for byte as nodejs.org serves them |
| `SHA256SUMS` | Every file under `payloads/` and `node/`, sorted by path. Append-only. It does not cover `attestations/` or `receipts/`: consumers read only `payloads/` and `node/`, and the other two are add-only by tree rule |
| `attestations/`, `receipts/` | The provenance bundle and the fetch receipt of each publish run |

## Payloads

| Plan | Module | Carries |
|---|---|---|
| `xoxd_npm_gftb_site_lock-1.0.0` | P0 `xoxd_npm_gftb_site_lock` 1.0.0 | the lock files above, as `exports_files` |
| `xoxd_npm_gftb_site_lint-1.0.0` | P1 `xoxd_npm_gftb_site_lint` 1.0.0 | the 160 `//:lint_suite` packages: 142 full, 18 TypeScript 7 natives as stubs (linux-x64 and darwin-arm64 are full, R582); one `npm_package` target per package; `npm_payload.MODULE.bazel` with the 160 `npm_replace_package` tags; `MANIFEST.json` |
| `node-v22.22.0` | none (a `node_urls` mirror) | `node-v22.22.0-linux-x64.tar.xz`, `node-v22.22.0-darwin-arm64.tar.gz` |

Versions are immutable. A new `name@version` in a bundle bumps its minor version; a removal or a major package bump bumps its major version.

## Flow

1. **Plans and locks change by ordinary PR.** A merged lock directory and a published plan never change, so every published payload stays reproducible from `main`. `lock-update.yml` re-resolves a lock directory and uploads the result with a diff; a person commits it in a signed PR as a new directory `locks/<new lock plan name>/`, with a new lock plan version and new bundle plan versions that name it. `check.yml` fails until they agree.
2. **Publish.** Dispatch `publish.yml` from `main` with the plan names, `publish=true` and the Linear issue. `fetch-pack` (GitHub-hosted `ubuntu-24.04`) is the only read of npm and nodejs.org for that publish: each tarball must equal its lock sha512, and each Node archive its signed `SHASUMS256.txt` line and the plan's rules_nodejs sha256. `attest` writes build provenance for every new file. `propose` pushes `payload/<run>-<attempt>` through the Git Data API with the workflow's own `GITHUB_TOKEN` (`contents: write`, `pull-requests: write`), so the commit is GitHub-signed; it refuses to create the branch unless the API reports the commit verified, and refuses any path that already exists on `main`.
3. **Verify.** That PR triggers no workflow. A reviewer dispatches, from `main`, `check.yml` and `reproduce.yml` with the PR number. `check` runs the tree rules and the pin lint with `main`'s tools against the head as data, then the head's own tool tests. `reproduce` runs only on `tinyland-nix`, the second infrastructure, never on a GitHub-hosted class; if that class cannot run it, the fallback is decision D6 for the operator. It uses only `main`'s tools, reads the PR head as data, does its own fetch, re-packs and requires every new file to be byte-equal.
4. **Review.** `gh attestation verify <file> --repo xoxd-ai/bazel-payloads --signer-workflow xoxd-ai/bazel-payloads/.github/workflows/publish.yml --source-ref refs/heads/main --deny-self-hosted-runners` passes; each bundle MANIFEST's sha512 set equals the lock's entries for it. Merge with a merge commit.
5. **Register.** A `xoxd-ai/bazel-registry` PR names `raw.githubusercontent.com/xoxd-ai/bazel-payloads/<merge commit>/payloads/<m>/<v>/<m>-<v>.tar.gz` with `strip_prefix` `<m>-<v>`.

`reproduce.yml` also runs weekly on `main`. It reads npm and nodejs.org again on purpose, to catch an unpublish or a serving change. The cell never reads npm.

## The pack recipe (normative)

- Strip each npm tarball's first path component and lay files out as `pkgs/<target>/...` under `<module>-<version>/`, where `<target>` is the name with `@` as `at_` and `/` as `_`, then `__<version>`.
- PAX tar, sorted by path; `mtime` 499162500; uid and gid 0 with empty names; files 0755 if any execute bit was set, else 0644; directories 0755, only those that hold files.
- One compressor: Python 3.12 `gzip.GzipFile(filename="", mtime=0, compresslevel=9)` over its zlib, both from `flake.lock`. GNU gzip is never used.
- Fail closed on any member that is not a regular file or a directory (symlinks, hardlinks, devices, FIFOs), on a nested `node_modules/`, on a Bazel boundary file (`BUILD`, `BUILD.bazel`, `WORKSPACE`, `WORKSPACE.bazel`, `MODULE.bazel`, `REPO.bazel`), on `..` or empty path segments, on a patched package, and on an archive over 64 MiB.
- A package with `os` or `cpu` fields is full only for a plan platform (linux x64, darwin arm64); otherwise it keeps `package.json` and top-level README and LICENSE files.
- `MANIFEST.json` has sorted keys and, per package, the lock sha512, the tarball sha256 and size, the payload class and bytes, and a `bin` map: npm's string form becomes `{<name without scope>: <path>}`. It also records `p0_lock_sha256`, the sha256 of the lock the bundle was cut from.
- Node archives are copied byte for byte.

Local run (for gate P-0 or a review), from a checkout:

```sh
nix develop -c python3 -I tools/test_payloads.py
nix develop -c python3 -I tools/payloads.py fetch plans/<p>.json... --out "$TMPDIR/f" [--cache <dir of verified .tgz>]
nix develop -c python3 -I tools/payloads.py pack  plans/<p>.json... --fetched "$TMPDIR/f" --out "$TMPDIR/o"
```

`--cache` is a local convenience only: a cached tarball is used only when its sha512 equals the lock's. The workflows never pass it.

## Rules

- Files under `payloads/`, `node/`, `attestations/` and `receipts/` are add-only, with plain ASCII paths. `SHA256SUMS` lines are append-only and must list exactly the files under `payloads/` and `node/`.
- A merged directory under `locks/` is immutable: no file in it changes, and none is added. A plan whose payload files exist on `main` is immutable.
- A change that adds payload files changes nothing else. Payload files arrive only on `payload/` branches whose commits are `github-actions[bot]` commits that GitHub committed and signed (committer `web-flow`, `noreply@github.com`, verification reason `valid`). That identity check is defense in depth: byte equality in `reproduce` is the guard.
- Every `uses:` in `.github/workflows/` is pinned by a full 40-hex commit sha. The `pins` check is a lint; the enforcement is the repository setting `sha_pinning_required` below.
- Commit statuses `bazel-payloads/check` and `bazel-payloads/reproduce` must be green before a merge. Only `check.yml` posts `check` and only `reproduce.yml` posts `reproduce`, also for a PR that adds no payload file (success, nothing to reproduce, after `main`'s tree rules pass). Dispatch both for every PR.
- No legitimate workflow runs a PR-supplied definition on the self-hosted class: `reproduce.yml` and the `p-e-egress` gate run only on `schedule` or `workflow_dispatch` from `main`. Those guards live in workflow files, and a fork PR's `pull_request` run uses the fork's copy of every workflow, so the guard against forks is the repository settings below. Approving a fork PR's workflow run approves that fork's workflow code, which can name any runner label this repository can reach.

## Operator preconditions (ordered)

Repository and organization settings. None is applied by a PR.

1. **Fork-PR approval first.** Set the fork-PR workflow approval policy to `all_external_contributors` (`PUT /repos/xoxd-ai/bazel-payloads/actions/permissions/fork-pr-contributor-approval`). Read on 2026-10-29: `first_time_contributors`. Design A-1 requires approval for every fork-PR run.
2. **Only then, a runner group.** Admit this repository to an ARC runner group that serves `tinyland-nix`. Prefer a group used by this repository alone, restricted to the selected workflows `xoxd-ai/bazel-payloads/.github/workflows/reproduce.yml@refs/heads/main` and `xoxd-ai/bazel-payloads/.github/workflows/gates.yml@refs/heads/main` (whether the organization's plan offers workflow restriction is UNVERIFIED). The shared `tinyland-infra` group (id 5) also serves the GloriousFlywheel repositories, so a fork job there could reach whatever an ARC pod reaches. Before admitting the repository, cancel any queued `p-e-egress` job from a `pull_request` run.
3. **The `publish` environment.** Set its deployment branch policy to `main` only, so `attest` and `propose` cannot run from another branch's copy of `publish.yml`.
4. **Workflow permissions.** Set `can_approve_pull_request_reviews` to `false` (`PUT /repos/xoxd-ai/bazel-payloads/actions/permissions/workflow`). Read on 2026-10-29: `true`, which lets any workflow token approve a PR, not only open one. GitHub's one setting governs both creating and approving PRs with `GITHUB_TOKEN`, so with `false` the `propose` job's `POST /pulls` is refused: `propose` keeps the pushed `payload/` branch, fails the run and names the branch, and a person opens the PR from it. With the single code owner, that person cannot approve a PR they opened. Operator decision: set `false` and add a second code owner (or have someone other than the code owner open payload PRs), or keep `true` and rely on `require_code_owner_review`, since a workflow's approval is never a code-owner approval.
5. **Actions policy.** Set `sha_pinning_required: true` and `allowed_actions: selected`, allowing `actions/*` and `cachix/install-nix-action`. Read on 2026-10-29: `false` and `all`. Enable secret scanning and push protection.
6. **Ruleset 26159473.** Add the rules below.

Proposed additions to ruleset 26159473 (today: deletion, `non_fast_forward`, `required_signatures`, and `pull_request` with merge only):

```json
{
  "type": "required_status_checks",
  "parameters": {
    "strict_required_status_checks_policy": false,
    "required_status_checks": [
      { "context": "bazel-payloads/check", "integration_id": 15368 },
      { "context": "bazel-payloads/reproduce", "integration_id": 15368 }
    ]
  }
}
```

and, in the existing `pull_request` rule: `required_approving_review_count: 1`, `require_code_owner_review: true`, `require_last_push_approval: true`, `dismiss_stale_reviews_on_push: true`.

- `integration_id` 15368 is the GitHub Actions app: a status posted with a personal token, including an agent lane's `gh api statuses`, does not satisfy the rule.
- A same-repository `pull_request` run can still post either context with the head's copy of a workflow, so the code-owner review with last-push approval is the guard against a writer. Payload PRs are authored by `github-actions[bot]`, so the code owner can approve them. A PR authored from the code owner's own account cannot be approved by that account; that applies to tools PRs, and the operator decides whether to add a second code owner.
- A ruleset `workflows` rule pinning `reproduce.yml` at `main` would also help. Its availability for this repository is UNVERIFIED.
