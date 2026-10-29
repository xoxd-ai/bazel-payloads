"""Dual-predicate redirect hop check for payload and module URLs (TIN-7477, R575 design 6.4).

For each URL it follows redirects by hand (ranged GET, no automatic redirect)
and judges every hop twice:

  s2    the GFTB S'' 6ae4cd74 per-hop predicate: `generationHTTPSURL`
        (source_generation.go:149) plus the acquisition origin list.
  g1    the PR-G1 (R580) predicate: s2, or a signed query on a
        release-assets.githubusercontent.com hop that directly follows a
        github.com/<o>/<r>/releases/download/... hop.

This is a diagnostic for review receipts. The cell's Go code stays the
authority; this file mirrors it so a payload URL can be checked before a
registry PR names it.

    python3 -I tools/hopcheck.py URL...
"""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ORIGINS = {
    "bcr.bazel.build",
    "codeload.github.com",
    "files.pythonhosted.org",
    "github.com",
    "mirror.bazel.build",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
RELEASE_ASSET_HOST = "release-assets.githubusercontent.com"
RELEASE_DOWNLOAD = re.compile(r"^/[^/]+/[^/]+/releases/download/(?:[^/]+/)+[^/]+$")
MAX_HOPS = 4


def strict_ok(raw):
    """The S'' predicate: https, admitted origin, no userinfo, port, query, fragment; canonical form."""
    try:
        u = urllib.parse.urlsplit(raw)
        port = u.port
    except ValueError:
        return False
    return (
        u.scheme == "https"
        and "@" not in u.netloc
        and port is None
        and ":" not in u.netloc
        and u.hostname is not None
        and u.netloc == u.netloc.lower()
        and u.netloc in ORIGINS
        and "?" not in raw
        and "#" not in raw
        and urllib.parse.urlunsplit(u) == raw
    )


def release_download(raw):
    u = urllib.parse.urlsplit(raw)
    return strict_ok(raw) and u.netloc == "github.com" and bool(RELEASE_DOWNLOAD.match(u.path))


def g1_ok(previous, raw):
    """PR-G1: strict, or a valid signed query on the release-asset host right after a release download."""
    if strict_ok(raw):
        return True
    if previous is None or not release_download(previous):
        return False
    base, sep, query = raw.partition("?")
    if not sep or not query or "#" in raw:
        return False
    if urllib.parse.urlsplit(base).netloc != RELEASE_ASSET_HOST or not strict_ok(base):
        return False
    if re.search(r"%(?![0-9A-Fa-f]{2})", query):
        return False  # Go's url.ParseQuery refuses a bad escape; Python's unquote does not
    try:
        urllib.parse.parse_qs(query, keep_blank_values=True, strict_parsing=True, errors="strict")
    except (ValueError, UnicodeDecodeError):
        return False
    return all(ord(c) > 32 and ord(c) < 127 for c in query)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def walk(url):
    opener = urllib.request.build_opener(NoRedirect)
    hops, previous, current = [], None, url
    for _ in range(MAX_HOPS + 1):
        hop = {"url": current.split("?", 1)[0] + ("?<query>" if "?" in current else ""), "s2": strict_ok(current)}
        hop["g1"] = g1_ok(previous, current) if previous is not None else strict_ok(current)
        hops.append(hop)
        if not hop["g1"]:
            break
        req = urllib.request.Request(current, headers={"Range": "bytes=0-0", "User-Agent": "bazel-payloads-hopcheck"})
        try:
            with opener.open(req, timeout=30) as resp:
                hop["status"] = resp.status
                break
        except urllib.error.HTTPError as err:
            hop["status"] = err.code
            location = err.headers.get("Location")
            if err.code not in (301, 302, 303, 307, 308) or not location:
                break
            previous, current = current, urllib.parse.urljoin(current, location)
    return {"url": url, "hops": hops, "s2": all(h["s2"] for h in hops), "g1": all(h["g1"] for h in hops)}


def main(argv):
    results = [walk(u) for u in argv]
    print(json.dumps(results, indent=1))
    return 0 if all(r["g1"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
