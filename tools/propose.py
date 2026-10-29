"""Open a payload PR with the workflow's own GITHUB_TOKEN (R581 act eff34be5).

Every file goes up through the Git Data API (blobs, a tree on top of main, a
commit with no author override). GitHub signs that commit as
github-actions[bot]; this script refuses to create the branch unless the API
reports the commit as verified, so main's required_signatures rule holds.

    python3 -I tools/propose.py --base SHA --staging DIR --branch NAME --body FILE --title TITLE

DIR holds the new files at their repository paths (payloads/, node/,
attestations/, receipts/ and the regenerated SHA256SUMS). Only those roots are
accepted, and no existing payload path may be overwritten.
"""

import argparse
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import github_api  # noqa: E402
import payloads  # noqa: E402

BOT_LOGIN = "github-actions[bot]"


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
    tree = []
    for rel in staged_files(a.staging):
        if rel != payloads.SUMS and rel in existing:
            payloads.fail(f"{rel} already exists on main; payload versions are immutable")
        data = payloads.read_bytes(os.path.join(a.staging, rel))
        blob = github_api.call(
            "POST",
            f"/repos/{repo}/git/blobs",
            raw_body=json.dumps({"encoding": "base64", "content": base64.b64encode(data).decode()}).encode(),
        )
        tree.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    new_tree = github_api.call("POST", f"/repos/{repo}/git/trees", {"base_tree": base_commit["tree"]["sha"], "tree": tree})
    title = a.title.strip().splitlines()[0]
    commit = github_api.call(
        "POST", f"/repos/{repo}/git/commits", {"message": title, "tree": new_tree["sha"], "parents": [a.base]}
    )
    if not (commit.get("verification") or {}).get("verified"):
        payloads.fail(f"commit {commit['sha']} is not GitHub-verified; refusing to create {a.branch}")
    github_api.call("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{a.branch}", "sha": commit["sha"]})
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
