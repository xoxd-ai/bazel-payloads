"""Offline tests for tools/payloads.py and tools/hopcheck.py.

    nix develop -c python3 -I tools/test_payloads.py
"""

import gzip
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import hopcheck  # noqa: E402
import payloads  # noqa: E402
import reproduce  # noqa: E402

ROOT = os.path.dirname(HERE)


def npm_tgz(members):
    """members: list of (name, kind, data_or_target, mode)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for name, kind, data, mode in members:
            ti = tarfile.TarInfo(name)
            ti.mode = mode
            if kind == "file":
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
            elif kind == "dir":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            elif kind == "symlink":
                ti.type = tarfile.SYMTYPE
                ti.linkname = data
                tf.addfile(ti)
            elif kind == "hardlink":
                ti.type = tarfile.LNKTYPE
                ti.linkname = data
                tf.addfile(ti)
            elif kind == "fifo":
                ti.type = tarfile.FIFOTYPE
                tf.addfile(ti)
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb") as g:
        g.write(raw.getvalue())
    return out.getvalue()


PKG = ("package/package.json", "file", b'{"name": "a", "version": "1.0.0"}', 0o644)


class FailClosed(unittest.TestCase):
    def refused(self, members, needle):
        with self.assertRaises(payloads.PayloadError) as cm:
            payloads.read_npm_tarball(npm_tgz(members), "a@1.0.0")
        self.assertIn(needle, str(cm.exception))

    def test_regular_package_is_read(self):
        files = payloads.read_npm_tarball(
            npm_tgz([("package", "dir", None, 0o755), PKG, ("package/bin/x.js", "file", b"x", 0o755)]), "a"
        )
        self.assertEqual(sorted(files), ["bin/x.js", "package.json"])
        self.assertEqual(files["bin/x.js"][1], 0o755)
        self.assertEqual(files["package.json"][1], 0o644)

    def test_symlink(self):
        self.refused([PKG, ("package/l", "symlink", "package.json", 0o777)], "not a regular file")

    def test_hardlink(self):
        self.refused([PKG, ("package/h", "hardlink", "package/package.json", 0o644)], "not a regular file")

    def test_fifo(self):
        self.refused([PKG, ("package/f", "fifo", None, 0o644)], "not a regular file")

    def test_nested_node_modules_file(self):
        self.refused([PKG, ("package/node_modules/b/index.js", "file", b"", 0o644)], "nested node_modules")

    def test_nested_node_modules_dir(self):
        self.refused([PKG, ("package/lib/node_modules", "dir", None, 0o755)], "nested node_modules")

    def test_boundary_files(self):
        for name in sorted(payloads.BOUNDARY_FILES):
            with self.subTest(name=name):
                self.refused([PKG, (f"package/sub/{name}", "file", b"", 0o644)], "boundary")

    def test_traversal(self):
        self.refused([PKG, ("package/../evil", "file", b"", 0o644)], "unsafe")
        self.refused([PKG, ("package//x", "file", b"", 0o644)], "unsafe")

    def test_member_outside_package(self):
        self.refused([PKG, ("toplevel", "file", b"", 0o644)], "outside")

    def test_missing_package_json(self):
        self.refused([("package/index.js", "file", b"", 0o644)], "no package.json")


class Archive(unittest.TestCase):
    def test_byte_stable_and_order_free(self):
        a = {"m-1.0.0/b/x": (b"x", 0o644), "m-1.0.0/a": (b"a", 0o755)}
        b = dict(reversed(list(a.items())))
        self.assertEqual(payloads.write_archive(a), payloads.write_archive(b))

    def test_member_metadata(self):
        blob = payloads.write_archive({"m-1.0.0/d/f": (b"f", 0o755)})
        self.assertEqual(blob[3], 0, "gzip FLG: no name, no extra")
        self.assertEqual(blob[4:8], b"\0\0\0\0", "gzip MTIME 0")
        with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
            ms = tf.getmembers()
        self.assertEqual([m.name for m in ms], ["m-1.0.0", "m-1.0.0/d", "m-1.0.0/d/f"])
        for m in ms:
            self.assertEqual((m.mtime, m.uid, m.gid, m.uname, m.gname), (payloads.EPOCH, 0, 0, "", ""))
        self.assertEqual([m.mode for m in ms], [0o755, 0o755, 0o755])

    def test_cap(self):
        old = payloads.MAX_ARCHIVE_BYTES
        payloads.MAX_ARCHIVE_BYTES = 10
        try:
            with self.assertRaises(payloads.PayloadError):
                payloads.write_archive({"m/x": (os.urandom(64), 0o644)})
        finally:
            payloads.MAX_ARCHIVE_BYTES = old


class Lock(unittest.TestCase):
    def test_split_dep(self):
        self.assertEqual(payloads.split_dep("eslint", "10.10.0(jiti@2.7.0)"), ("eslint", "10.10.0(jiti@2.7.0)"))
        self.assertEqual(
            payloads.split_dep("typescript", "@typescript/typescript6@6.0.2"), ("@typescript/typescript6", "6.0.2")
        )
        self.assertEqual(payloads.split_dep("@typescript/old", "typescript@6.0.3"), ("typescript", "6.0.3"))
        for bad in ("link:../x", "file:x.tgz"):
            with self.assertRaises(payloads.PayloadError):
                payloads.split_dep("x", bad)

    def test_target_and_url(self):
        self.assertEqual(payloads.target_name("@eslint/js", "10.0.1"), "at_eslint_js__10.0.1")
        self.assertEqual(payloads.npm_url("@eslint/js", "10.0.1"), "https://registry.npmjs.org/@eslint/js/-/js-10.0.1.tgz")

    def test_committed_plans_match_the_lock(self):
        """Each npm-bundle plan's closure count and digest hold against the committed lock."""
        for fn in sorted(os.listdir(os.path.join(ROOT, "plans"))):
            plan = payloads.load_plan(os.path.join(ROOT, "plans", fn))
            if plan["kind"] == "npm-bundle":
                with self.subTest(plan=fn):
                    rows, _ = payloads.bundle_closure(plan)
                    self.assertEqual(len(rows), plan["closure_count"])
                    full = [r for r in rows if payloads.is_full(plan, r)]
                    self.assertTrue(all(r[2].startswith("sha512-") for r in rows))
                    self.assertGreater(len(full), 0)

    def test_lint_plan_natives(self):
        plan = payloads.plan_by_name("xoxd_npm_gftb_site_lint-1.0.0")
        rows, _ = payloads.bundle_closure(plan)
        natives = {r[0]: payloads.is_full(plan, r) for r in rows if r[4] or r[5]}
        self.assertEqual(len(natives), 20)
        self.assertEqual(sorted(n for n, f in natives.items() if f), ["@typescript/typescript-darwin-arm64", "@typescript/typescript-linux-x64"])

    def test_p0_pack_is_stable(self):
        plan = payloads.plan_by_name("xoxd_npm_gftb_site_lock-1.0.0")
        a, ma = payloads.pack_lock(plan)
        b, mb = payloads.pack_lock(plan)
        self.assertEqual((a, ma), (b, mb))
        self.assertEqual(json.loads(ma)["files"]["pnpm-lock.yaml"], plan["lock_sha256"])


class Platforms(unittest.TestCase):
    """npm's os/cpu rule (PR-P1 review S9)."""

    PLAN = {"full_platforms": [{"os": "linux", "cpu": "x64"}, {"os": "darwin", "cpu": "arm64"}]}

    def full(self, os_list, cpu_list):
        return payloads.is_full(self.PLAN, ("a", "1.0.0", "sha512-x", "u", os_list, cpu_list))

    def test_rule(self):
        self.assertTrue(payloads.platform_ok("linux", []))
        self.assertTrue(payloads.platform_ok("linux", ["any"]))
        self.assertTrue(payloads.platform_ok("linux", "linux"))
        self.assertTrue(payloads.platform_ok("linux", ["!win32"]))
        self.assertFalse(payloads.platform_ok("win32", ["!win32"]))
        self.assertFalse(payloads.platform_ok("linux", ["darwin"]))
        self.assertFalse(payloads.platform_ok("linux", ["linux", "!linux"]))
        self.assertTrue(payloads.platform_ok("linux", ["darwin", "linux"]))

    def test_is_full(self):
        self.assertTrue(self.full([], []))
        self.assertTrue(self.full(["linux"], ["x64"]))
        self.assertTrue(self.full(["darwin"], []), "os alone restricts only os")
        self.assertTrue(self.full([], ["arm64"]), "cpu alone restricts only cpu")
        self.assertFalse(self.full([], ["ia32"]))
        self.assertTrue(self.full(["!win32"], []), "fsevents-style negation")
        self.assertFalse(self.full(["!linux", "!darwin"], []))
        self.assertFalse(self.full(["linux"], ["arm64"]))
        self.assertFalse(self.full(["win32"], ["x64"]))


class Tree(unittest.TestCase):
    """tree_rules from main's tools against a scratch repository (PR-P1 review A1, S3, S6)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.old)
        self.git("init", "-q", "-b", "main")
        self.write("README.md", b"x\n")
        self.write("locks/l/pnpm-lock.yaml", b"lock\n")
        self.write("plans/p-1.0.0.json", json.dumps({"kind": "npm-lock", "module": "p", "version": "1.0.0"}).encode())
        self.write("plans/q-1.0.0.json", json.dumps({"kind": "npm-lock", "module": "q", "version": "1.0.0"}).encode())
        self.publish("payloads/p/1.0.0/p-1.0.0.tar.gz", b"tgz")
        self.base = self.commit()

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
            check=True,
            capture_output=True,
        ).stdout

    def write(self, rel, data):
        os.makedirs(os.path.dirname(rel) or ".", exist_ok=True)
        with open(rel, "wb") as f:
            f.write(data)

    def publish(self, rel, data):
        self.write(rel, data)
        payloads.sums(".", check=False)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "c")
        return self.git("rev-parse", "HEAD").decode().strip()

    def refused(self, needle, payload_pr=False):
        head = self.commit()
        with self.assertRaises(payloads.PayloadError) as cm:
            payloads.tree_rules(self.base, head, payload_pr)
        self.assertIn(needle, str(cm.exception))

    def test_tools_change_passes(self):
        self.write("README.md", b"y\n")
        payloads.tree_rules(self.base, self.commit(), False)

    def test_payload_add_passes(self):
        self.publish("payloads/q/1.0.0/q-1.0.0.tar.gz", b"q")
        payloads.tree_rules(self.base, self.commit(), True)

    def test_published_file_is_immutable(self):
        self.publish("payloads/p/1.0.0/p-1.0.0.tar.gz", b"other")
        self.refused("immutable")

    def test_non_ascii_payload_path_is_seen_and_refused(self):
        self.publish("payloads/q/1.0.0/\u00e9.tar.gz", b"q")
        self.refused("plain ASCII", payload_pr=True)

    def test_merged_lock_directory_is_immutable(self):
        self.write("locks/l/pnpm-lock.yaml", b"bumped\n")
        self.refused("lock directory is immutable")

    def test_no_new_file_in_a_merged_lock_directory(self):
        self.write("locks/l/patches/x.patch", b"x\n")
        self.refused("lock directory is immutable")

    def test_new_lock_directory_passes(self):
        self.write("locks/l2/pnpm-lock.yaml", b"lock2\n")
        payloads.tree_rules(self.base, self.commit(), False)

    def test_published_plan_is_immutable(self):
        self.write("plans/p-1.0.0.json", b"{}")
        self.refused("published plan is immutable")

    def test_unpublished_plan_may_change(self):
        self.write("plans/q-1.0.0.json", json.dumps({"kind": "npm-lock", "module": "q", "version": "1.0.0", "x": 1}).encode())
        payloads.tree_rules(self.base, self.commit(), False)

    def test_replace_ref_does_not_hide_a_payload(self):
        self.publish("payloads/q/1.0.0/q-1.0.0.tar.gz", b"q")
        self.write("README.md", b"mixed\n")
        head = self.commit()
        self.git("replace", head, self.base)
        with self.assertRaises(payloads.PayloadError) as cm:
            payloads.tree_rules(self.base, head, False)
        self.assertIn("must not also change", str(cm.exception))


class BotCommit(unittest.TestCase):
    """reproduce.py's commit identity check (PR-P1 review S2)."""

    GOOD = {
        "sha": "0" * 40,
        "author": {"login": "github-actions[bot]"},
        "committer": {"login": "web-flow"},
        "commit": {"committer": {"email": "noreply@github.com"}, "verification": {"verified": True, "reason": "valid"}},
    }

    def variant(self, **patch):
        c = json.loads(json.dumps(self.GOOD))
        for path, value in patch.items():
            node = c
            keys = path.split("__")
            for k in keys[:-1]:
                node = node[k]
            node[keys[-1]] = value
        return c

    def test_good(self):
        self.assertIsNone(reproduce.bot_commit_problem(self.GOOD))

    def test_spoofed_author_signed_by_a_person(self):
        c = self.variant(committer__login="Jesssullivan", commit__committer__email="bot@users.noreply.github.com")
        self.assertIn("committer", reproduce.bot_commit_problem(c))

    def test_other_refusals(self):
        self.assertIn("author", reproduce.bot_commit_problem(self.variant(author__login="someone")))
        self.assertIn("committer", reproduce.bot_commit_problem(self.variant(commit__committer__email="x@example.invalid")))
        self.assertIn("signature", reproduce.bot_commit_problem(self.variant(commit__verification__verified=False)))
        self.assertIn("signature", reproduce.bot_commit_problem(self.variant(commit__verification__reason="unsigned")))
        self.assertIsNotNone(reproduce.bot_commit_problem({}))


class Bin(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(payloads.normalize_bin("prettier", {"bin": "./bin/prettier.cjs"}), {"prettier": "./bin/prettier.cjs"})
        self.assertEqual(payloads.normalize_bin("@s/tool", {"bin": "cli.js"}), {"tool": "cli.js"})
        self.assertEqual(payloads.normalize_bin("t", {"bin": {"b": "2", "a": "1"}}), {"a": "1", "b": "2"})
        self.assertEqual(payloads.normalize_bin("t", {}), {})
        with self.assertRaises(payloads.PayloadError):
            payloads.normalize_bin("t", {"directories": {"bin": "bin"}})
        with self.assertRaises(payloads.PayloadError):
            payloads.normalize_bin("t", {"bin": ["x"]})


class Pins(unittest.TestCase):
    def test_repository_workflows_are_pinned(self):
        payloads.pins(ROOT)

    def test_unpinned_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".github", "workflows"))
            with open(os.path.join(d, ".github", "workflows", "x.yml"), "w") as f:
                f.write("jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n")
            with self.assertRaises(payloads.PayloadError):
                payloads.pins(d)


class Hops(unittest.TestCase):
    REL = "https://github.com/bazel-contrib/bazel-lib/releases/download/v2.22.5/bazel-lib-v2.22.5.tar.gz"
    SIGNED = "https://release-assets.githubusercontent.com/github-production-release-asset/425866965/fb2d?sp=r&sv=x&sig=x"
    RAW = "https://raw.githubusercontent.com/xoxd-ai/bazel-payloads/0123456789abcdef0123456789abcdef01234567/node/v22.22.0/node-v22.22.0-linux-x64.tar.xz"

    def test_strict(self):
        self.assertTrue(hopcheck.strict_ok(self.RAW))
        self.assertTrue(hopcheck.strict_ok(self.REL))
        for bad in (
            self.SIGNED,
            self.RAW + "?",
            self.RAW + "#x",
            "http://raw.githubusercontent.com/x",
            "https://raw.githubusercontent.com:443/x",
            "https://u@raw.githubusercontent.com/x",
            "https://RAW.githubusercontent.com/x",
            "https://registry.npmjs.org/x/-/x-1.0.0.tgz",
            "https://nodejs.org/dist/v22.22.0/x",
        ):
            with self.subTest(url=bad):
                self.assertFalse(hopcheck.strict_ok(bad))

    def test_g1(self):
        self.assertTrue(hopcheck.g1_ok(self.REL, self.SIGNED))
        self.assertFalse(hopcheck.g1_ok(None, self.SIGNED))
        self.assertFalse(hopcheck.g1_ok("https://github.com/o/r/archive/v1.tar.gz", self.SIGNED))
        self.assertFalse(hopcheck.g1_ok(self.REL, self.SIGNED.replace("release-assets", "objects")))
        self.assertFalse(hopcheck.g1_ok(self.REL, self.SIGNED + "#f"))
        self.assertFalse(hopcheck.g1_ok(self.REL, self.SIGNED.split("?")[0] + "?"))
        self.assertFalse(hopcheck.g1_ok(self.REL, self.SIGNED + "&x=%zz"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
