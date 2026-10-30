"""Open a payload PR with the workflow's own GITHUB_TOKEN (R581 act eff34be5).

The file bytes go up with `git push` to a scratch ref; the tree, the commit
and the payload/ branch are made through the Git Data API. GitHub signs that
commit as github-actions[bot]; this script refuses to create the branch unless
the API reports the commit as verified, so main's required_signatures rule
holds.

The REST blob endpoint takes a file as one base64 JSON body and refuses large
ones (HTTP 422 for the 31.7 MB and 49.9 MB Node archives in publish run
40988958431), so no file goes through it. The scratch ref
refs/payload-scratch/<branch suffix> holds one unsigned commit whose tree
carries the blobs. It is never a branch, never a parent and never in a PR; the
payload commit's only parent is --base. The ref is deleted when this script
ends, whether it succeeded or not.

    python3 -I tools/propose.py --base SHA --staging DIR --branch NAME --body FILE --title TITLE

DIR holds the new files at their repository paths (payloads/, node/,
attestations/, receipts/ and the regenerated SHA256SUMS). Only those roots are
accepted, and no existing payload path may be overwritten.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import github_api  # noqa: E402
import payloads  # noqa: E402

BOT_LOGIN = "github-actions[bot]"
SCRATCH_ROOT = "refs/payload-scratch/"


def staged_files(staging):
    out = []
    for dirpath, _, files in os.walk(staging):
        for fn in files:
            rel = os.path.relpath(os.path.join(dirpath, fn), staging).replace(os.sep, "/")
            if not (rel.startswith(payloads.PR_ONLY_ROOTS) or rel == payloads.SUMS):
                payloads.fail(f"{rel}: not a payload path")
            out.append(rel)
    if not any(r.startswith(payloads.PAYLOAD_ROOTS) for r in out):
        payloads.fail("nothing to propose")
    return sorted(out)


def remote_url():
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    return f"{server}/{github_api.repo()}.git"


def git_env(tmp, remote):
    """No system or global git config, no prompt, no replace refs.

    The token rides in an http.extraHeader from the environment, so it is never
    in an argument list, a config file or a log line.
    """
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_INDEX_FILE": os.path.join(tmp, "index"),
        "GIT_AUTHOR_NAME": "bazel-payloads propose scratch",
        "GIT_AUTHOR_EMAIL": "scratch@bazel-payloads.invalid",
        "GIT_COMMITTER_NAME": "bazel-payloads propose scratch",
        "GIT_COMMITTER_EMAIL": "scratch@bazel-payloads.invalid",
        "GIT_CONFIG_COUNT": "0",
    }
    token = os.environ.get(github_api.TOKEN_ENV, "")
    if remote.startswith("https://") and token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader", GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")
    return env


def git(env, gitdir, *args, stdin=None):
    try:
        return subprocess.run(
            ["git", f"--git-dir={gitdir}", *args], check=True, capture_output=True, env=env, input=stdin
        ).stdout
    except subprocess.CalledProcessError as exc:
        err = exc.stderr.decode("utf-8", "replace").strip()[-500:]
        payloads.fail(f"git {args[0]} failed (exit {exc.returncode}): {err}")


def write_blobs(env, gitdir, staging, rels, scratch_ref):
    """Write each staged file as a blob in a local scratch commit; return ({path: blob sha}, commit)."""
    git(env, gitdir, "init", "--quiet", "--bare")
    shas = {}
    index_info = b""
    for rel in rels:
        sha = git(env, gitdir, "hash-object", "-w", "--no-filters", "--", os.path.join(staging, rel)).decode().strip()
        shas[rel] = sha
        index_info += f"100644 {sha}\t{rel}".encode() + b"\0"
    git(env, gitdir, "update-index", "-z", "--add", "--index-info", stdin=index_info)
    tree = git(env, gitdir, "write-tree").decode().strip()
    commit = git(env, gitdir, "commit-tree", "--no-gpg-sign", "-m", f"scratch blobs for {scratch_ref}; not a payload commit", tree)
    return shas, commit.decode().strip()


def drop_scratch(env, gitdir, remote, scratch_ref):
    try:
        git(env, gitdir, "push", "--quiet", remote, f":{scratch_ref}")
    except payloads.PayloadError as exc:
        print(f"propose.py: warning: could not delete {scratch_ref} ({exc}); delete it by hand", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="the main commit the payload is packed against")
    ap.add_argument("--staging", required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", required=True)
    a = ap.parse_args(argv)
    if not a.branch.startswith("payload/") or not all(c.isalnum() or c in "/-_." for c in a.branch):
        payloads.fail(f"branch {a.branch!r} must be payload/<safe name>")
    repo = github_api.repo()
    main_ref = github_api.call("GET", f"/repos/{repo}/git/ref/heads/main")
    if main_ref["object"]["sha"] != a.base:
        payloads.fail("main moved since the payload was packed; dispatch again")
    base_commit = github_api.call("GET", f"/repos/{repo}/git/commits/{a.base}")
    listing = github_api.call("GET", f"/repos/{repo}/git/trees/{base_commit['tree']['sha']}?recursive=1")
    if listing.get("truncated"):
        payloads.fail("main's tree listing is truncated; cannot prove the new paths are new")
    existing = {e["path"] for e in listing["tree"]}
    rels = staged_files(a.staging)
    for rel in rels:
        if rel != payloads.SUMS and rel in existing:
            payloads.fail(f"{rel} already exists on main; payload versions are immutable")
    remote = remote_url()
    scratch_ref = SCRATCH_ROOT + a.branch[len("payload/"):]
    with tempfile.TemporaryDirectory(prefix="propose-") as tmp:
        env = git_env(tmp, remote)
        gitdir = os.path.join(tmp, "scratch.git")
        shas, scratch_commit = write_blobs(env, gitdir, a.staging, rels, scratch_ref)
        git(env, gitdir, "push", "--quiet", remote, f"{scratch_commit}:{scratch_ref}")
        try:
            tree = [{"path": rel, "mode": "100644", "type": "blob", "sha": shas[rel]} for rel in rels]
            new_tree = github_api.call(
                "POST", f"/repos/{repo}/git/trees", {"base_tree": base_commit["tree"]["sha"], "tree": tree}
            )
            title = a.title.strip().splitlines()[0]
            commit = github_api.call(
                "POST", f"/repos/{repo}/git/commits", {"message": title, "tree": new_tree["sha"], "parents": [a.base]}
            )
            if not (commit.get("verification") or {}).get("verified"):
                payloads.fail(f"commit {commit['sha']} is not GitHub-verified; refusing to create {a.branch}")
            github_api.call("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{a.branch}", "sha": commit["sha"]})
        finally:
            drop_scratch(env, gitdir, remote, scratch_ref)
    body = payloads.read_text(a.body)
    try:
        pr = github_api.call(
            "POST",
            f"/repos/{repo}/pulls",
            {"title": title, "head": a.branch, "base": "main", "body": body, "maintainer_can_modify": False},
        )
    except github_api.ApiError as exc:
        # With can_approve_pull_request_reviews false, GitHub refuses PR
        # creation by GITHUB_TOKEN. The branch stays; a person opens the PR.
        payloads.fail(f"{a.branch} is pushed at {commit['sha']}, but opening its PR failed ({exc}); open it by hand from that branch")
    print(json.dumps({"pr": pr["html_url"], "number": pr["number"], "head": commit["sha"], "files": len(tree)}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (payloads.PayloadError, github_api.ApiError) as exc:
        print(f"propose.py: {exc}", file=sys.stderr)
        sys.exit(1)
