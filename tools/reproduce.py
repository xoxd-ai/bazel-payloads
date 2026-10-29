"""Independent reproduction of a payload PR, or of main (TIN-7477, design section 3 step 6).

It runs from main's tools only (critique 2, N2 hardening). The PR head is
fetched as data: its blobs are read and compared, never executed. It does its
own fetch from the lock URLs and Node's signed SHASUMS, never another run's
artifact, re-packs with the same recipe and requires every published file to
be byte-equal. It posts the commit status `bazel-payloads/reproduce`.

    python3 -I tools/reproduce.py --pr N --runner-class LABEL --work DIR
    python3 -I tools/reproduce.py --main --runner-class LABEL --work DIR
"""

import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import github_api  # noqa: E402
import payloads  # noqa: E402

CONTEXT = "bazel-payloads/reproduce"
BOT_LOGIN = "github-actions[bot]"


def git_bytes(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def plan_for(path):
    m = re.fullmatch(r"payloads/([a-z][a-z0-9_]*)/([0-9]+\.[0-9]+\.[0-9]+)/[^/]+", path)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.fullmatch(r"node/v([0-9]+\.[0-9]+\.[0-9]+)/[^/]+", path)
    if m:
        return f"node-v{m.group(1)}"
    payloads.fail(f"{path}: no plan maps to this payload path")


def resolve_pr(number):
    repo = github_api.repo()
    pr = github_api.call("GET", f"/repos/{repo}/pulls/{number}")
    if pr["state"] != "open" or pr["base"]["ref"] != "main":
        payloads.fail(f"PR #{number} is not an open PR into main")
    if (pr["head"]["repo"] or {}).get("full_name") != repo:
        payloads.fail(f"PR #{number} is not from a branch of {repo}; fork heads are never reproduced here")
    head = pr["head"]["sha"]
    cmp = github_api.call("GET", f"/repos/{repo}/compare/main...{head}")
    subprocess.run(["git", "fetch", "--no-tags", "--quiet", "origin", f"+refs/pull/{number}/head:refs/remotes/pr/{number}"], check=True)
    if git_bytes("rev-parse", f"refs/remotes/pr/{number}").decode().strip() != head:
        payloads.fail(f"PR #{number} head moved while resolving")
    return pr, head, cmp["merge_base_commit"]["sha"], cmp["commits"]


def added_payload_files(base, head):
    out = []
    for line in git_bytes("diff", "--no-renames", "--name-status", f"{base}...{head}").decode().splitlines():
        status, path = line.split("\t", 1)
        if status == "A" and path.startswith(payloads.PAYLOAD_ROOTS):
            out.append(path)
    return sorted(out)


def reproduce(files, head, work):
    names = sorted({plan_for(p) for p in files})
    plans = [payloads.plan_by_name(n) for n in names]
    expected = set()
    for plan in plans:
        expected |= set(payloads.payload_paths(plan).values())
    if expected != set(files):
        payloads.fail(f"payload files {sorted(set(files) ^ expected)} do not match their plans' outputs")
    fetched, out = os.path.join(work, "fetched"), os.path.join(work, "out")
    payloads.fetch(plans, fetched, None)
    written = payloads.pack(plans, fetched, out)
    for rel in sorted(files):
        published = git_bytes("cat-file", "blob", f"{head}:{rel}")
        mine = payloads.read_bytes(os.path.join(out, rel))
        if published != mine:
            payloads.fail(f"{rel}: re-pack is not byte-equal (published {payloads.sha256_hex(published)}, re-pack {written[rel]['sha256']})")
    return names


def main(argv=None):
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pr", type=int)
    g.add_argument("--main", action="store_true")
    ap.add_argument("--runner-class", required=True)
    ap.add_argument("--work", required=True)
    a = ap.parse_args(argv)
    run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{github_api.repo()}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '0')}"
    head = None
    try:
        if a.main:
            head = git_bytes("rev-parse", "HEAD").decode().strip()
            github_api.set_status(head, CONTEXT, "pending", f"reproducing main on {a.runner_class}", run_url)
            files = sorted(
                p for p in git_bytes("ls-tree", "-r", "--name-only", head).decode().splitlines() if p.startswith(payloads.PAYLOAD_ROOTS)
            )
            payloads.sums(".", check=True)
            if not files:
                github_api.set_status(head, CONTEXT, "success", "main carries no payload yet", run_url)
                return 0
        else:
            pr, head, base, commits = resolve_pr(a.pr)
            github_api.set_status(head, CONTEXT, "pending", f"reproducing on {a.runner_class}", run_url)
            files = added_payload_files(base, head)
            payloads.tree_rules(base, head, payload_pr=bool(files))
            if not files:
                github_api.set_status(head, CONTEXT, "success", "no payload file in this PR; nothing to reproduce", run_url)
                return 0
            if not pr["head"]["ref"].startswith("payload/"):
                payloads.fail("payload files arrive only on payload/ branches opened by publish.yml")
            for c in commits:
                login = (c.get("author") or {}).get("login")
                if login != BOT_LOGIN or not c["commit"]["verification"]["verified"]:
                    payloads.fail(f"commit {c['sha'][:12]} is not a verified {BOT_LOGIN} commit")
        names = reproduce(files, head, a.work)
        msg = f"{len(files)} files byte-equal ({', '.join(names)}) on {a.runner_class}"
        github_api.set_status(head, CONTEXT, "success", msg, run_url)
        print(json.dumps({"head": head, "plans": names, "files": files, "runner_class": a.runner_class}))
        return 0
    except (payloads.PayloadError, subprocess.CalledProcessError, github_api.ApiError) as exc:
        print(f"reproduce.py: {exc}", file=sys.stderr)
        if head:
            github_api.set_status(head, CONTEXT, "failure", str(exc)[:140], run_url)
        return 1


if __name__ == "__main__":
    sys.exit(main())
