"""Minimal GitHub REST client for the workflows (stdlib only).

The token comes from the environment variable named by TOKEN_ENV and is never
printed, logged or written to disk. Error messages carry the method, the path
and the HTTP status only.
"""

import json
import os
import urllib.error
import urllib.request

API = "https://api.github.com"
TOKEN_ENV = "GITHUB_TOKEN"


class ApiError(Exception):
    def __init__(self, method, path, status, message):
        super().__init__(f"{method} {path}: HTTP {status}: {message}")
        self.status = status


def call(method, path, body=None, raw_body=None, ok=(200, 201)):
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        raise ApiError(method, path, 0, f"{TOKEN_ENV} is not set")
    data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "bazel-payloads")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            status, text = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        status, text = err.code, err.read()
    if status not in ok:
        try:
            message = json.loads(text).get("message", "")
        except (ValueError, AttributeError):
            message = ""
        raise ApiError(method, path, status, message[:200])
    return json.loads(text) if text else None


def repo():
    name = os.environ.get("GITHUB_REPOSITORY", "")
    if name.count("/") != 1:
        raise ApiError("-", "-", 0, "GITHUB_REPOSITORY is not owner/name")
    return name


def set_status(sha, context, state, description, target_url):
    if state not in ("pending", "success", "failure", "error"):
        raise ValueError(state)
    call(
        "POST",
        f"/repos/{repo()}/statuses/{sha}",
        {"state": state, "context": context, "description": description[:140], "target_url": target_url},
    )
