"""bazel-payloads tools: closure walk, fetch, deterministic pack, sums, tree rules.

TIN-7477, rulings R575, R581 and R582. Normative recipe: the R575 design,
section 3 (round 2) with critique-2 fold-ins N2 and N3.

Run it through the flake so that Python, zlib and PyYAML are pinned:

    nix develop -c python3 -I tools/payloads.py <command> ...

Commands:
    closure  PLAN                         print the closure; fail on count or digest drift
    fetch    PLAN... --out DIR [--cache DIR]
                                          download and verify every input once
    pack     PLAN... --fetched DIR --out DIR
                                          build the payload files, byte-stable
    sums     --root DIR [--check]         write or check SHA256SUMS
    tree     --base REV --head REV [--payload-pr]
                                          immutability and path rules between two commits
    pins     --root DIR                   every workflow `uses:` is pinned by full sha

Only `fetch` touches the network: registry.npmjs.org for npm tarballs and
nodejs.org for Node. `pack` is offline and reads only the fetch output.
"""

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zlib

import yaml

EPOCH = 499162500  # npm's own fixed tarball mtime, 1985-10-26T08:15:00Z
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024  # bundle cap: under GitHub's 100 MiB file limit
BOUNDARY_FILES = {"BUILD", "BUILD.bazel", "WORKSPACE", "WORKSPACE.bazel", "MODULE.bazel", "REPO.bazel"}
NPM_HOST = "registry.npmjs.org"
NODE_HOST = "nodejs.org"
PAYLOAD_ROOTS = ("payloads/", "node/")
PR_ONLY_ROOTS = PAYLOAD_ROOTS + ("attestations/", "receipts/")
SUMS = "SHA256SUMS"
LOCKS_ROOT = "locks/"
PLANS_ROOT = "plans/"
# Payload-root paths are plain ASCII (PR-R1's consumer regex is ASCII only).
SAFE_PAYLOAD_PATH = re.compile(r"[A-Za-z0-9._@+-]+(/[A-Za-z0-9._@+-]+)*")


class PayloadError(Exception):
    pass


def fail(msg):
    raise PayloadError(msg)


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def sri512(data):
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def read_json(path):
    with open(path, "rb") as f:
        return json.load(f)


def dump_json(obj):
    return (json.dumps(obj, indent=1, sort_keys=True) + "\n").encode()


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_plan(path):
    plan = read_json(path)
    for key in ("kind", "module", "version"):
        if key not in plan:
            fail(f"{path}: plan lacks {key!r}")
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", plan["version"]):
        fail(f"{path}: version {plan['version']!r} is not plain semver")
    if plan["kind"] != "node" and not re.fullmatch(r"[a-z][a-z0-9_]*", plan["module"]):
        fail(f"{path}: module {plan['module']!r} has characters outside [a-z0-9_]")
    plan["_path"] = path
    return plan


def plan_by_name(name):
    return load_plan(os.path.join(repo_root(), "plans", name + ".json"))


# ---------------------------------------------------------------- pnpm lock


def split_dep(name, value):
    """A lock dependency value is `<version>(<peers>)` or `<alias target>@<version>(<peers>)`."""
    value = str(value)
    if value.startswith(("link:", "file:", "workspace:", "git", "http")):
        fail(f"{name}: non-registry dependency {value!r}")
    if value[:1].isdigit():
        return name, value
    at = value.rfind("@", 1, value.find("(") if "(" in value else len(value))
    if at <= 0:
        fail(f"{name}: cannot parse dependency {value!r}")
    return value[:at], value[at + 1 :]


def bare(version):
    return version.split("(", 1)[0]


def target_name(name, version):
    return name.replace("@", "at_").replace("/", "_") + "__" + version


def npm_url(name, version):
    base = name.rsplit("/", 1)[-1]
    return f"https://{NPM_HOST}/{name}/-/{base}-{version}.tgz"


def lock_closure(lock_path, importer, direct):
    """Walk the pnpm v9 lock from the importer's direct entries.

    Returns sorted (name, version, integrity, url, os, cpu) rows, one per
    name@version. Peer variants of one name@version collapse to one row, as
    `npm_replace_package` keys do (`name@version` matches every variant).
    """
    with open(lock_path, "rb") as f:
        lock = yaml.safe_load(f)
    if str(lock.get("lockfileVersion")) != "9.0":
        fail(f"{lock_path}: lockfileVersion {lock.get('lockfileVersion')!r}, want 9.0")
    imp = lock["importers"][importer]
    roots = {}
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, entry in (imp.get(section) or {}).items():
            roots[name] = entry["version"]
    stack = []
    for name in direct:
        if name not in roots:
            fail(f"direct package {name!r} is not an importer {importer!r} dependency")
        stack.append(split_dep(name, roots[name]))
    seen_snap, rows = set(), {}
    snapshots, packages = lock["snapshots"], lock["packages"]
    while stack:
        name, ver = stack.pop()
        key = f"{name}@{ver}"
        if key in seen_snap:
            continue
        seen_snap.add(key)
        if "patch_hash=" in ver:
            fail(f"{key}: patched package; P1 carries no patches (a P2 decision)")
        snap = snapshots.get(key)
        if snap is None:
            fail(f"{key}: no snapshot entry")
        pkg_key = f"{name}@{bare(ver)}"
        meta = packages.get(pkg_key)
        if meta is None:
            fail(f"{pkg_key}: no packages entry")
        res = meta.get("resolution") or {}
        integrity = res.get("integrity", "")
        if not integrity.startswith("sha512-"):
            fail(f"{pkg_key}: integrity is not sha512")
        url = res.get("tarball") or npm_url(name, bare(ver))
        if urllib.parse.urlsplit(url).netloc != NPM_HOST:
            fail(f"{pkg_key}: tarball host is not {NPM_HOST}")
        if meta.get("libc"):
            fail(f"{pkg_key}: a libc field; plan platforms name no libc yet (a P2 decision)")
        rows[pkg_key] = (name, bare(ver), integrity, url, meta.get("os") or [], meta.get("cpu") or [])
        for section in ("dependencies", "optionalDependencies"):
            for dname, dval in (snap.get(section) or {}).items():
                stack.append(split_dep(dname, dval))
    return [rows[k] for k in sorted(rows)]


def closure_digest(rows):
    listing = "".join(f"{r[0]}@{r[1]}\n" for r in rows)
    return sha256_hex(listing.encode())


def bundle_closure(plan):
    lock_plan = plan_by_name(plan["lock_plan"])
    lock_path = os.path.join(repo_root(), lock_plan["source"], "pnpm-lock.yaml")
    rows = lock_closure(lock_path, plan.get("importer", "."), plan["direct"])
    digest = closure_digest(rows)
    if len(rows) != plan["closure_count"] or digest != plan["closure_sha256"]:
        fail(
            f"{plan['module']}: closure is {len(rows)} entries, sha256 {digest}; "
            f"the plan says {plan['closure_count']}, {plan['closure_sha256']}"
        )
    return rows, lock_plan


def platform_ok(value, allowed):
    """npm's os/cpu rule (npm-install-checks `checkList`).

    An empty list or ["any"] admits every value. A negated entry `!x` refuses
    x. Otherwise the value must equal a positive entry, unless every entry is
    negated. One field alone restricts only that field.
    """
    if isinstance(allowed, str):
        allowed = [allowed]
    if list(allowed) == ["any"]:
        return True
    negated, match = 0, False
    for entry in allowed:
        if entry.startswith("!"):
            negated += 1
            if value == entry[1:]:
                return False
        elif value == entry:
            match = True
    return match or negated == len(allowed)


def is_full(plan, row):
    os_list, cpu_list = row[4], row[5]
    return any(platform_ok(p["os"], os_list) and platform_ok(p["cpu"], cpu_list) for p in plan["full_platforms"])


# ---------------------------------------------------------------- fetch


def http_get(url, expect_host):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.netloc != expect_host or parts.query or parts.fragment:
        fail(f"refusing to fetch {url}")
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "bazel-payloads"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                final = urllib.parse.urlsplit(resp.geturl())
                if final.scheme != "https" or final.netloc != expect_host:
                    fail(f"{url} redirected off {expect_host}")
                return resp.read()
        except PayloadError:
            raise
        except Exception as exc:  # network errors only; content is checked by the caller
            last = exc
            time.sleep(2 * (attempt + 1))
    fail(f"GET {url} failed: {last}")


def cache_index(cache_dirs):
    """sha512 SRI -> path for every *.tgz under the given local caches (local P-0 only)."""
    index = {}
    for root in cache_dirs or []:
        for dirpath, _, files in os.walk(root):
            for fn in files:
                if fn.endswith(".tgz"):
                    p = os.path.join(dirpath, fn)
                    with open(p, "rb") as f:
                        index.setdefault(sri512(f.read()), p)
    return index


def gpg_verify(data, sig, keys_dir, signer):
    with tempfile.TemporaryDirectory() as home:
        os.chmod(home, 0o700)
        keys = sorted(os.path.join(keys_dir, k) for k in os.listdir(keys_dir) if k.endswith(".asc"))
        subprocess.run(["gpg", "--homedir", home, "--batch", "--quiet", "--import", *keys], check=True, capture_output=True)
        dpath, spath = os.path.join(home, "data"), os.path.join(home, "data.sig")
        with open(dpath, "wb") as f:
            f.write(data)
        with open(spath, "wb") as f:
            f.write(sig)
        out = subprocess.run(
            ["gpg", "--homedir", home, "--batch", "--status-fd", "1", "--verify", spath, dpath], capture_output=True, text=True
        )
        valid = [ln.split()[2] for ln in out.stdout.splitlines() if ln.startswith("[GNUPG:] VALIDSIG ")]
        if out.returncode != 0 or valid != [signer]:
            fail(f"SHASUMS256.txt signature is not a valid signature by {signer}")


def fetch(plans, out, caches):
    os.makedirs(os.path.join(out, "npm"), exist_ok=True)
    os.makedirs(os.path.join(out, "node"), exist_ok=True)
    index = cache_index(caches)
    receipt = {"npm": [], "node": [], "plans": [], "python": sys.version.split()[0], "zlib": zlib.ZLIB_RUNTIME_VERSION}
    for plan in plans:
        receipt["plans"].append({"plan": os.path.basename(plan["_path"]), "sha256": sha256_hex(read_bytes(plan["_path"]))})
        if plan["kind"] == "npm-bundle":
            rows, _ = bundle_closure(plan)
            for name, ver, integrity, url, _, _ in rows:
                dest = os.path.join(out, "npm", target_name(name, ver) + ".tgz")
                if os.path.exists(dest):
                    data = read_bytes(dest)
                elif integrity in index:
                    data = read_bytes(index[integrity])
                else:
                    data = http_get(url, NPM_HOST)
                if sri512(data) != integrity:
                    fail(f"{name}@{ver}: sha512 does not equal the lock integrity")
                with open(dest, "wb") as f:
                    f.write(data)
                receipt["npm"].append(
                    {"package": name, "version": ver, "url": url, "integrity": integrity, "sha256": sha256_hex(data), "bytes": len(data)}
                )
        elif plan["kind"] == "node":
            base = plan["base_url"]
            sums = http_get(base + "SHASUMS256.txt", NODE_HOST)
            sig = http_get(base + "SHASUMS256.txt.sig", NODE_HOST)
            gpg_verify(sums, sig, os.path.join(repo_root(), plan["keys"]), plan["signer"])
            listed = {}
            for line in sums.decode().splitlines():
                parts = line.split()
                if len(parts) == 2:
                    listed[parts[1]] = parts[0]
            for fn, want in sorted(plan["files"].items()):
                if listed.get(fn) != want:
                    fail(f"{fn}: signed SHASUMS256.txt says {listed.get(fn)}, the plan (rules_nodejs table) says {want}")
                data = http_get(base + fn, NODE_HOST)
                if sha256_hex(data) != want:
                    fail(f"{fn}: downloaded bytes do not match sha256 {want}")
                with open(os.path.join(out, "node", fn), "wb") as f:
                    f.write(data)
                receipt["node"].append({"file": fn, "url": base + fn, "sha256": want, "bytes": len(data), "signer": plan["signer"]})
        elif plan["kind"] != "npm-lock":
            fail(f"unknown plan kind {plan['kind']!r}")
    with open(os.path.join(out, "fetch-receipt.json"), "wb") as f:
        f.write(dump_json(receipt))
    return receipt


# ---------------------------------------------------------------- pack


def normalize_bin(name, pkg_json):
    """MANIFEST `bin`: always a {command: path} map (N3b).

    npm's string form names the command after the package, without its scope.
    """
    b = pkg_json.get("bin")
    if b is None:
        if (pkg_json.get("directories") or {}).get("bin"):
            fail(f"{name}: `directories.bin` without `bin` needs a decision")
        return {}
    if isinstance(b, str):
        return {name.rsplit("/", 1)[-1]: b}
    if isinstance(b, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in b.items()):
        return dict(sorted(b.items()))
    fail(f"{name}: unsupported `bin` field")


def safe_rel(path, where):
    if path.startswith("/") or "\\" in path:
        fail(f"{where}: unsafe member path {path!r}")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        fail(f"{where}: unsafe member path {path!r}")
    return parts


def read_npm_tarball(data, where):
    """Yield (rel, body, mode) for the package's regular files; fail closed on anything else (C5)."""
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf.getmembers():
            name = m.name.rstrip("/") if m.isdir() else m.name
            parts = name.split("/", 1)
            if m.isdir():
                if len(parts) == 2:
                    rel_parts = safe_rel(parts[1], where)
                    if "node_modules" in rel_parts:
                        fail(f"{where}: nested node_modules/ at {parts[1]!r}")
                continue
            if not m.isfile():
                fail(f"{where}: member {m.name!r} is not a regular file or directory (type {m.type!r})")
            if len(parts) != 2:
                fail(f"{where}: member {m.name!r} is outside the package directory")
            rel = parts[1]
            rel_parts = safe_rel(rel, where)
            if "node_modules" in rel_parts:
                fail(f"{where}: nested node_modules/ at {rel!r}")
            if rel_parts[-1] in BOUNDARY_FILES:
                fail(f"{where}: Bazel boundary file {rel!r}")
            if rel in files:
                fail(f"{where}: duplicate member {rel!r}")
            files[rel] = (tf.extractfile(m).read(), 0o755 if m.mode & 0o111 else 0o644)
    if "package.json" not in files:
        fail(f"{where}: no package.json")
    return files


def write_archive(entries):
    """entries: {path: (bytes, mode)} -> deterministic .tar.gz bytes (PAX, fixed mtime, root owner)."""
    dirs = set()
    for p in entries:
        d = p.rsplit("/", 1)[0] if "/" in p else ""
        while d and d not in dirs:
            dirs.add(d)
            d = d.rsplit("/", 1)[0] if "/" in d else ""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for p in sorted(set(entries) | dirs):
            ti = tarfile.TarInfo(p)
            ti.mtime = EPOCH
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            if p in dirs and p not in entries:
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                tf.addfile(ti)
            else:
                body, mode = entries[p]
                ti.size = len(body)
                ti.mode = mode
                tf.addfile(ti, io.BytesIO(body))
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, compresslevel=9, mtime=0) as g:
        g.write(raw.getvalue())
    blob = out.getvalue()
    if len(blob) > MAX_ARCHIVE_BYTES:
        fail(f"archive is {len(blob)} B, over the {MAX_ARCHIVE_BYTES} B bundle cap")
    return blob


def module_bazel(module, version, deps):
    lines = [f'module(name = "{module}", version = "{version}", compatibility_level = 1)', ""]
    lines += [f'bazel_dep(name = "{n}", version = "{v}")' for n, v in deps]
    return ("\n".join(lines) + "\n").encode()


def pack_lock(plan):
    src = os.path.join(repo_root(), plan["source"])
    top = f"{plan['module']}-{plan['version']}"
    entries, files = {}, {}
    for rel in sorted(plan["files"]):
        parts = safe_rel(rel, plan["module"])
        if parts[-1] in BOUNDARY_FILES:
            fail(f"{plan['module']}: boundary file {rel!r}")
        path = os.path.join(src, rel)
        if os.path.islink(path) or not os.path.isfile(path):
            fail(f"{plan['module']}: {rel!r} is not a regular file")
        body = read_bytes(path)
        entries[f"{top}/{rel}"] = (body, 0o644)
        files[rel] = sha256_hex(body)
    present = set()
    for dirpath, _, fns in os.walk(src):
        for fn in fns:
            present.add(os.path.relpath(os.path.join(dirpath, fn), src).replace(os.sep, "/"))
    unlisted = sorted(present - set(plan["files"]) - set(plan.get("not_exported", [])))
    if unlisted:
        fail(f"{plan['module']}: files in {plan['source']} are neither exported nor listed as not_exported: {unlisted}")
    lock_sha = files.get("pnpm-lock.yaml")
    if lock_sha != plan["lock_sha256"]:
        fail(f"{plan['module']}: pnpm-lock.yaml sha256 {lock_sha}, the plan says {plan['lock_sha256']}")
    build = ["# Generated by xoxd-ai/bazel-payloads tools/payloads.py. Do not edit.", "", "exports_files("]
    build += ["    ["] + [f'        "{rel}",' for rel in sorted(files)] + ["    ],", '    visibility = ["//visibility:public"],', ")", ""]
    entries[f"{top}/BUILD.bazel"] = ("\n".join(build).encode(), 0o644)
    entries[f"{top}/MODULE.bazel"] = (module_bazel(plan["module"], plan["version"], []), 0o644)
    manifest = dump_json({"module": plan["module"], "version": plan["version"], "source": plan["source"], "files": files})
    entries[f"{top}/MANIFEST.json"] = (manifest, 0o644)
    return write_archive(entries), manifest


def pack_bundle(plan, fetched):
    rows, lock_plan = bundle_closure(plan)
    module, version = plan["module"], plan["version"]
    top = f"{module}-{version}"
    entries, manifest_rows = {}, []
    build = [
        'load("@aspect_rules_js//npm:defs.bzl", "npm_package")',
        "",
        "# Generated by xoxd-ai/bazel-payloads tools/payloads.py. Do not edit.",
        "",
    ]
    replace = [
        f"# Generated by xoxd-ai/bazel-payloads tools/payloads.py from {module} {version}. Do not edit.",
        "# Include it verbatim: gftb-site's S00 test compares it byte for byte.",
        'npm = use_extension("@aspect_rules_js//npm:extensions.bzl", "npm")',
        "",
    ]
    by_target = []
    for row in rows:
        name, ver, integrity = row[0], row[1], row[2]
        t = target_name(name, ver)
        data = read_bytes(os.path.join(fetched, "npm", t + ".tgz"))
        if sri512(data) != integrity:
            fail(f"{name}@{ver}: fetched bytes do not match the lock integrity")
        files = read_npm_tarball(data, f"{name}@{ver}")
        pkg_json = json.loads(files["package.json"][0])
        if pkg_json.get("name") != name or pkg_json.get("version") != ver:
            fail(f"{name}@{ver}: package.json says {pkg_json.get('name')}@{pkg_json.get('version')}")
        full = is_full(plan, row)
        kept = 0
        for rel, (body, mode) in sorted(files.items()):
            if not full and rel != "package.json" and not rel.lower().startswith(("readme", "license")):
                continue
            entries[f"{top}/pkgs/{t}/{rel}"] = (body, mode)
            kept += len(body)
        manifest_rows.append(
            {
                "bin": normalize_bin(name, pkg_json),
                "lock_integrity": integrity,
                "npm_tgz_bytes": len(data),
                "npm_tgz_sha256": sha256_hex(data),
                "package": name,
                "payload": "full" if full else "stub",
                "payload_bytes": kept,
                "target": t,
                "version": ver,
            }
        )
        by_target.append((t, name, ver))
    for t, name, ver in sorted(by_target):
        build += [
            "npm_package(",
            f'    name = "{t}",',
            f'    srcs = glob(["pkgs/{t}/**"]),',
            '    include_external_repositories = ["*"],',
            f'    root_paths = ["pkgs/{t}"],',
            f'    package = "{name}",',
            f'    version = "{ver}",',
            '    visibility = ["//visibility:public"],',
            ")",
            "",
        ]
        replace.append(f'npm.npm_replace_package(package = "{name}@{ver}", replacement = "@{module}//:{t}")')
    lock_sha = plan_lock_sha(lock_plan)
    manifest = dump_json(
        {
            "closure_sha256": plan["closure_sha256"],
            "full_platforms": plan["full_platforms"],
            "module": module,
            "p0_lock_module": f"{lock_plan['module']}@{lock_plan['version']}",
            "p0_lock_sha256": lock_sha,
            "packages": manifest_rows,
            "version": version,
        }
    )
    entries[f"{top}/BUILD.bazel"] = ("\n".join(build).encode(), 0o644)
    entries[f"{top}/MODULE.bazel"] = (module_bazel(module, version, [("aspect_rules_js", plan["rules_js"])]), 0o644)
    entries[f"{top}/MANIFEST.json"] = (manifest, 0o644)
    entries[f"{top}/npm_payload.MODULE.bazel"] = (("\n".join(replace) + "\n").encode(), 0o644)
    return write_archive(entries), manifest


def plan_lock_sha(lock_plan):
    path = os.path.join(repo_root(), lock_plan["source"], "pnpm-lock.yaml")
    got = sha256_hex(read_bytes(path))
    if got != lock_plan["lock_sha256"]:
        fail(f"{lock_plan['module']}: pnpm-lock.yaml sha256 {got}, the plan says {lock_plan['lock_sha256']}")
    return got


def payload_paths(plan):
    """Published paths for a plan, relative to the repository root."""
    if plan["kind"] == "node":
        return {fn: f"node/v{plan['version']}/{fn}" for fn in plan["files"]}
    d = f"payloads/{plan['module']}/{plan['version']}"
    return {"archive": f"{d}/{plan['module']}-{plan['version']}.tar.gz", "manifest": f"{d}/MANIFEST.json"}


def pack(plans, fetched, out):
    written = {}
    for plan in plans:
        paths = payload_paths(plan)
        if plan["kind"] == "node":
            blobs = {}
            for fn, rel in paths.items():
                data = read_bytes(os.path.join(fetched, "node", fn))
                if sha256_hex(data) != plan["files"][fn]:
                    fail(f"{fn}: fetched bytes do not match the plan sha256")
                blobs[rel] = data
        else:
            archive, manifest = pack_lock(plan) if plan["kind"] == "npm-lock" else pack_bundle(plan, fetched)
            blobs = {paths["archive"]: archive, paths["manifest"]: manifest}
        for rel, data in blobs.items():
            dest = os.path.join(out, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            written[rel] = {"bytes": len(data), "sha256": sha256_hex(data)}
    return written


# ---------------------------------------------------------------- SHA256SUMS


def sums_lines(root):
    lines = []
    for top in PAYLOAD_ROOTS:
        base = os.path.join(root, top)
        for dirpath, _, fns in os.walk(base):
            for fn in fns:
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, root).replace(os.sep, "/")
                lines.append(f"{sha256_hex(read_bytes(p))}  {rel}")
    return sorted(lines, key=lambda ln: ln.split("  ", 1)[1])


def sums(root, check):
    want = "".join(ln + "\n" for ln in sums_lines(root))
    path = os.path.join(root, SUMS)
    if check:
        have = read_text(path) if os.path.exists(path) else ""
        if have != want:
            fail(f"{SUMS} does not match the files under {', '.join(PAYLOAD_ROOTS)}")
        return
    if want:
        with open(path, "w") as f:
            f.write(want)


# ---------------------------------------------------------------- tree rules


def git_env():
    # Replace refs never change what a check reads (PR-P1 review A1).
    return {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}


def git_raw(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, env=git_env()).stdout


def git(*args):
    return git_raw(*args).decode()


def nul_fields(raw):
    """Split `-z` output. Paths are exact bytes: no quoting (PR-P1 review S6)."""
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    return [p.decode("utf-8", "surrogateescape") for p in parts]


def diff_changes(base, head):
    """(status, path) for every change between the merge base and head, from `git diff -z`."""
    fields = nul_fields(git_raw("diff", "--no-renames", "--name-status", "-z", f"{base}...{head}"))
    if len(fields) % 2:
        fail("git diff --name-status -z: unexpected output")
    return [(fields[i], fields[i + 1]) for i in range(0, len(fields), 2)]


def tree_files(rev):
    return nul_fields(git_raw("ls-tree", "-r", "-z", "--name-only", rev))


def published_plan(base, path):
    """True when the plan file at `base` names payload paths that exist at `base`."""
    try:
        plan = json.loads(git_raw("show", f"{base}:{path}"))
    except (subprocess.CalledProcessError, ValueError):
        return False
    if not isinstance(plan, dict) or not all(k in plan for k in ("kind", "module", "version")):
        return False
    if plan["kind"] == "node" and not isinstance(plan.get("files"), dict):
        return False
    present = set(tree_files(base))
    return any(rel in present for rel in payload_paths(plan).values())


def tree_rules(base, head, payload_pr):
    """Payload files are add-only, SHA256SUMS is append-only, and payload PRs touch nothing else.

    Lock directories are add-only and a published plan never changes, so every
    published payload stays reproducible from main (PR-P1 review S3).
    """
    changes = diff_changes(base, head)
    lock_dirs = {p.split("/")[1] for p in tree_files(base) if p.startswith(LOCKS_ROOT) and p.count("/") >= 2}
    for status, path in changes:
        if path.startswith(PR_ONLY_ROOTS) and status != "A":
            fail(f"{path}: published payload files are immutable ({status})")
        if path.startswith(PR_ONLY_ROOTS) and not SAFE_PAYLOAD_PATH.fullmatch(path):
            fail(f"{path!r}: payload paths are plain ASCII")
        if path.startswith(LOCKS_ROOT) and (status != "A" or path.split("/")[1] in lock_dirs):
            fail(f"{path}: a merged lock directory is immutable ({status}); a lock bump adds a new locks/<lock plan name>/")
        if path.startswith(PLANS_ROOT) and status != "A" and published_plan(base, path):
            fail(f"{path}: a published plan is immutable ({status}); add a new plan version")
    touched_payload = any(p.startswith(PR_ONLY_ROOTS) or p == SUMS for _, p in changes)
    other = [p for _, p in changes if not (p.startswith(PR_ONLY_ROOTS) or p == SUMS)]
    if touched_payload and other:
        fail(f"a payload change must not also change {other}")
    if payload_pr and not touched_payload:
        fail("a payload PR adds no payload file")

    def sums_at(rev):
        if SUMS not in tree_files(rev):
            return []
        return git("cat-file", "blob", f"{rev}:{SUMS}").splitlines()

    old, new = sums_at(base), sums_at(head)
    if not set(old) <= set(new):
        fail(f"{SUMS} lines may only be added")
    listed = {}
    for ln in new:
        digest, _, rel = ln.partition("  ")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or rel in listed:
            fail(f"{SUMS}: malformed or duplicate line {ln!r}")
        listed[rel] = digest
    if new != sorted(new, key=lambda ln: ln.split("  ", 1)[1]):
        fail(f"{SUMS} is not sorted by path")
    files = [p for p in tree_files(head) if p.startswith(PAYLOAD_ROOTS)]
    if sorted(files) != sorted(listed):
        fail(f"{SUMS} at head does not list exactly the files under {', '.join(PAYLOAD_ROOTS)}")
    for rel in files:
        blob = git_raw("cat-file", "blob", f"{head}:{rel}")
        if sha256_hex(blob) != listed[rel]:
            fail(f"{rel}: sha256 does not match {SUMS}")


# ---------------------------------------------------------------- action pins

USES = re.compile(r"^\s*(?:-\s+)?uses:\s*([^\s#]+)", re.M)


def pins(root):
    bad = []
    wf = os.path.join(root, ".github", "workflows")
    for fn in sorted(os.listdir(wf)):
        text = read_text(os.path.join(wf, fn))
        for ref in USES.findall(text):
            ref = ref.strip("'\"")
            if ref.startswith("./"):
                continue
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}", ref):
                bad.append(f"{fn}: {ref}")
    if bad:
        fail("actions not pinned by full commit sha: " + "; ".join(bad))


# ---------------------------------------------------------------- CLI


def main(argv=None):
    ap = argparse.ArgumentParser(prog="payloads.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("closure")
    c.add_argument("plan")
    f = sub.add_parser("fetch")
    f.add_argument("plans", nargs="+")
    f.add_argument("--out", required=True)
    f.add_argument("--cache", action="append", help="local only: verified *.tgz lookup by sha512")
    p = sub.add_parser("pack")
    p.add_argument("plans", nargs="+")
    p.add_argument("--fetched", required=True)
    p.add_argument("--out", required=True)
    s = sub.add_parser("sums")
    s.add_argument("--root", default=".")
    s.add_argument("--check", action="store_true")
    t = sub.add_parser("tree")
    t.add_argument("--base", required=True)
    t.add_argument("--head", required=True)
    t.add_argument("--payload-pr", action="store_true")
    q = sub.add_parser("pins")
    q.add_argument("--root", default=".")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "closure":
            rows, _ = bundle_closure(load_plan(a.plan))
            for r in rows:
                print(f"{r[0]}@{r[1]}\t{'full' if is_full(load_plan(a.plan), r) else 'stub'}")
            print(f"# {len(rows)} entries, sha256 {closure_digest(rows)}", file=sys.stderr)
        elif a.cmd == "fetch":
            r = fetch([load_plan(x) for x in a.plans], a.out, a.cache)
            print(json.dumps({"npm": len(r["npm"]), "node": len(r["node"])}))
        elif a.cmd == "pack":
            print(json.dumps(pack([load_plan(x) for x in a.plans], a.fetched, a.out), indent=1, sort_keys=True))
        elif a.cmd == "sums":
            sums(a.root, a.check)
        elif a.cmd == "tree":
            tree_rules(a.base, a.head, a.payload_pr)
        elif a.cmd == "pins":
            pins(a.root)
    except (PayloadError, subprocess.CalledProcessError) as exc:
        print(f"payloads.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
