# bazel-payloads

Immutable, sha256-pinned build payloads (npm bundles, toolchain archives) served at pinned commits for xoxd-ai/bazel-registry module sources. TIN-7477 R581.

Consumers fetch a payload as `https://raw.githubusercontent.com/xoxd-ai/bazel-payloads/<40-hex commit on main>/<path>`, one GET with no redirect and no query, which the GFTB cell admits (policy `a583e632`). `xoxd-ai/bazel-registry` stays the index: its `source.json` names that URL and the archive's `sha256-` integrity from `SHA256SUMS`.

## Layout

| Path | What |
|---|---|
| `locks/gftb-site/` | P0 input: gftb-site's `pnpm-lock.yaml`, `package.json`, `pnpm-workspace.yaml`, `.pnpmfile.cjs` and `patches/`, moved here under R577 (from gftb-site #168 at `7cd2b83d`). `.npmrc` is kept for `lock-update` and is not exported |
| `plans/<name>.json` | One plan per payload. An npm bundle names its direct packages and pins the closure count and the sha256 of the sorted `name@version` list; a lock plan pins the lock sha256; the Node plan pins the release signer and the rules_nodejs sha256 table |
| `tools/` | `payloads.py` (closure walk, fetch, packer, `SHA256SUMS`, tree rules, pin lint), `propose.py`, `reproduce.py`, `hopcheck.py` (S'' and PR-G1 hop predicates), tests |
| `keys/node/` | The Node release key that signs v22.22.0's `SHASUMS256.txt` (`CC68F5A3106FF448322E48ED27F5E38D5B0A215F`), from `nodejs/release-keys` at `481637f8` |
| `flake.nix`, `flake.lock` | Python 3.12 with its zlib and PyYAML, GnuPG, and pnpm 10.13.1 for `lock-update` |
| `payloads/<module>/<version>/` | `<module>-<version>.tar.gz` and a copy of its `MANIFEST.json` for review diffs |
| `node/v<version>/` | Node archives, byte for byte as nodejs.org serves them |
| `SHA256SUMS` | Every file under `payloads/` and `node/`, sorted by path. Append-only |
| `attestations/`, `receipts/` | The provenance bundle and the fetch receipt of each publish run |

## Payloads

| Plan | Module | Carries |
|---|---|---|
| `xoxd_npm_gftb_site_lock-1.0.0` | P0 `xoxd_npm_gftb_site_lock` 1.0.0 | the lock files above, as `exports_files` |
| `xoxd_npm_gftb_site_lint-1.0.0` | P1 `xoxd_npm_gftb_site_lint` 1.0.0 | the 160 `//:lint_suite` packages: 142 full, 18 TypeScript 7 natives as stubs (linux-x64 and darwin-arm64 are full, R582); one `npm_package` target per package; `npm_payload.MODULE.bazel` with the 160 `npm_replace_package` tags; `MANIFEST.json` |
| `node-v22.22.0` | none (a `node_urls` mirror) | `node-v22.22.0-linux-x64.tar.xz`, `node-v22.22.0-darwin-arm64.tar.gz` |

Versions are immutable. A new `name@version` in a bundle bumps its minor version; a removal or a major package bump bumps its major version.

## Flow

1. **Plans and locks change by ordinary PR.** `lock-update.yml` re-resolves the lock and uploads it with a diff; a person commits it in a signed PR together with the plan's new closure count and sha256. `check.yml` fails until they agree.
2. **Publish.** Dispatch `publish.yml` from `main` with the plan names, `publish=true` and the Linear issue. `fetch-pack` (GitHub-hosted `ubuntu-24.04`) is the only read of npm and nodejs.org for that publish: each tarball must equal its lock sha512, and each Node archive its signed `SHASUMS256.txt` line and the plan's rules_nodejs sha256. `attest` writes build provenance for every new file. `propose` pushes `payload/<run>-<attempt>` through the Git Data API with the workflow's own `GITHUB_TOKEN` (`contents: write`, `pull-requests: write`), so the commit is GitHub-signed; it refuses to create the branch unless the API reports the commit verified, and refuses any path that already exists on `main`.
3. **Verify.** That PR triggers no workflow. A reviewer dispatches, from `main`, `check.yml` and `reproduce.yml` with the PR number. `reproduce` runs on a different runner class (`tinyland-nix` by default, or `ubuntu-24.04-arm`), uses only `main`'s tools, reads the PR head as data, does its own fetch, re-packs and requires every new file to be byte-equal.
4. **Review.** `gh attestation verify <file> --repo xoxd-ai/bazel-payloads --signer-workflow xoxd-ai/bazel-payloads/.github/workflows/publish.yml --source-ref refs/heads/main` passes; each bundle MANIFEST's sha512 set equals the lock's entries for it. Merge with a merge commit.
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

- Files under `payloads/`, `node/`, `attestations/` and `receipts/` are add-only. `SHA256SUMS` lines are append-only and must list exactly the files under `payloads/` and `node/`.
- A change that adds payload files changes nothing else. Payload files arrive only on `payload/` branches whose commits are verified `github-actions[bot]` commits.
- Every `uses:` in `.github/workflows/` is pinned by a full 40-hex commit sha.
- Commit statuses `bazel-payloads/check` and `bazel-payloads/reproduce` must be green before a merge. For a PR that adds no payload file, `check` posts `reproduce` as success, since there is nothing to reproduce.
- Self-hosted runners never run on `pull_request` from a fork; `reproduce.yml` never runs on `pull_request` at all.
