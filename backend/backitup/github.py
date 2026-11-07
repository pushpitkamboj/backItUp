import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import jwt

from backitup.engine import Budget, Git, write_json
from backitup.errors import BackItUpError

CORE_METADATA = ("issues", "pull_requests", "comments", "reviews", "labels", "milestones", "releases", "settings")
OPTIONAL_CATEGORIES = ("lfs", "release_assets", "wiki")
ALL_CATEGORIES = ("git", *CORE_METADATA, *OPTIONAL_CATEGORIES)
ARCHIVED_ONLY = [*CORE_METADATA, "release_assets", "wiki"]


def valid_owner(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", value):
        raise BackItUpError("invalid_owner", "Invalid GitHub account name")
    return value


def valid_repo(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) or value in (".", ".."):
        raise BackItUpError("invalid_repository", "Use a GitHub repository name with letters, digits, dots, hyphens, or underscores")
    return value


class GitHub:
    def __init__(self, token: str, api_version: str = "2026-03-10", *, client: httpx.Client | None = None, budget: Budget | None = None):
        self.token = token
        self.budget = budget
        self.client = client or httpx.Client(timeout=httpx.Timeout(60, connect=10), follow_redirects=False)
        self.headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": api_version, "User-Agent": "BackItUp/0.1"}

    def close(self):
        self.client.close()

    def request(self, method: str, path: str, *, json_body=None, params=None, allow_not_found=False):
        if self.budget:
            self.budget.check()
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise BackItUpError("unsafe_url", "Untrusted GitHub API path")
        try:
            response = self.client.request(method, "https://api.github.com" + path, headers=self.headers, json=json_body, params=params)
        except httpx.HTTPError as exc:
            raise BackItUpError("github_network", "GitHub could not be reached", transient=True) from exc
        if allow_not_found and response.status_code == 404:
            return None
        if response.status_code >= 400:
            if response.status_code == 401:
                raise BackItUpError("github_revoked", "GitHub authorization expired or was revoked; reconnect this account", status=409)
            if response.status_code == 429 or (response.status_code == 403 and (response.headers.get("x-ratelimit-remaining") == "0" or response.headers.get("retry-after"))):
                error = BackItUpError("github_rate_limit", "GitHub rate limit reached; work will retry", transient=True)
                retry = response.headers.get("retry-after")
                reset = response.headers.get("x-ratelimit-reset")
                error.retry_after = max(60, min(86400, int(retry or 0),) if retry and retry.isdigit() else max(60, int(reset or time.time() + 60) - int(time.time())))
                raise error
            if response.status_code == 403:
                raise BackItUpError("github_permissions", "GitHub denied this operation. Check the App installation, repository access, and required permissions.", status=409)
            if response.status_code == 404:
                raise BackItUpError("github_not_found", "GitHub resource is missing or inaccessible", status=404)
            if response.status_code == 422:
                raise BackItUpError("github_conflict", "GitHub rejected the repository name or configuration; choose a different name and check account policies", status=409)
            raise BackItUpError("github_error", "GitHub could not complete this operation", transient=response.status_code >= 500)
        if response.status_code == 204:
            return None
        return response

    def get(self, path: str, **kwargs):
        response = self.request("GET", path, **kwargs)
        return response.json() if response is not None else None

    def pages(self, path: str, *, params=None, key: str | None = None):
        params = {**(params or {}), "per_page": 100}
        visited = set()
        while path:
            marker = path + json.dumps(params, sort_keys=True)
            if marker in visited or len(visited) >= 100_000:
                raise BackItUpError("pagination_loop", "GitHub pagination did not terminate safely")
            visited.add(marker)
            response = self.request("GET", path, params=params)
            payload = response.json()
            items = payload.get(key) if key else payload
            if not isinstance(items, list):
                raise BackItUpError("github_response", "GitHub returned an unexpected collection response")
            yield from items
            following = response.links.get("next", {}).get("url")
            if not following:
                return
            parsed = urlparse(following)
            if parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.username or parsed.fragment:
                raise BackItUpError("unsafe_url", "GitHub returned an unsafe pagination URL")
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
            params = {}

    def require_repository(self, owner: str, name: str, provider_id: int) -> dict:
        repo = self.get(f"/repos/{valid_owner(owner)}/{valid_repo(name)}")
        if repo["id"] != provider_id:
            raise BackItUpError("repository_replaced", "This repository name now refers to a different repository")
        if not repo.get("permissions", {}).get("pull", True):
            raise BackItUpError("github_permissions", "You are no longer authorized to read this repository")
        return repo

    def download_asset(self, path: str, target: Path, budget: Budget) -> dict:
        # Only API asset IDs are accepted; metadata-provided download URLs are never fetched.
        if not re.fullmatch(r"/repos/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/releases/assets/\d+", path):
            raise BackItUpError("unsafe_url", "Invalid release asset endpoint")
        headers = {**self.headers, "Accept": "application/octet-stream"}
        url = "https://api.github.com" + path
        for _ in range(5):
            with self.client.stream("GET", url, headers=headers) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = response.headers.get("location", "")
                    parsed = urlparse(url)
                    if parsed.scheme != "https" or not parsed.hostname or not (parsed.hostname.endswith(".githubusercontent.com") or parsed.hostname == "github.com") or parsed.username or parsed.port not in (None, 443):
                        raise BackItUpError("unsafe_url", "Release asset redirect was not a trusted GitHub host")
                    headers = {"User-Agent": "BackItUp/0.1"}  # Never forward API tokens to download hosts.
                    continue
                if response.status_code != 200:
                    raise BackItUpError("asset_access", "A selected release asset could not be downloaded", transient=response.status_code >= 500)
                import hashlib
                digest = hashlib.sha256()
                size = 0
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        budget.check()
                        size += len(chunk)
                        if size > budget.max_bytes:
                            raise BackItUpError("disk_limit", "Release assets exceed the job disk allowance")
                        output.write(chunk)
                        digest.update(chunk)
                return {"size": size, "sha256": digest.hexdigest(), "path": str(target.name)}
        raise BackItUpError("asset_redirects", "Too many release asset redirects")


def app_jwt(app_id: str, key_file: Path) -> str:
    if not app_id or not key_file.is_file():
        raise BackItUpError("github_setup", "The operator must configure this GitHub App before connecting accounts", status=503)
    return jwt.encode({"iat": int(time.time()) - 30, "exp": int(time.time()) + 540, "iss": app_id}, key_file.read_bytes(), algorithm="RS256")


def installation_token(app_id: str, key_file: Path, installation_id: int, repository_id: int, api_version: str) -> dict:
    app = GitHub(app_jwt(app_id, key_file), api_version)
    try:
        return app.request("POST", f"/app/installations/{installation_id}/access_tokens", json_body={"repository_ids": [repository_id]}).json()
    finally:
        app.close()


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        response = client.post("https://github.com/login/oauth/access_token", json={"client_id": client_id, "client_secret": client_secret, "code": code, "redirect_uri": redirect_uri}, headers={"Accept": "application/json"})
    data = response.json()
    if response.status_code != 200 or not data.get("access_token"):
        raise BackItUpError("github_authorization", "GitHub authorization could not be completed. Please try connecting again.")
    if data.get("expires_in"):
        data["expires_at"] = time.time() + data["expires_in"]
    return {k: v for k, v in data.items() if k in ("access_token", "refresh_token", "expires_at")}


def refresh_user_token(client_id: str, client_secret: str, saved: dict) -> dict:
    if not saved.get("expires_at") or saved["expires_at"] > time.time() + 120:
        return saved
    if not saved.get("refresh_token"):
        raise BackItUpError("github_revoked", "Reconnect GitHub to renew authorization")
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        response = client.post("https://github.com/login/oauth/access_token", json={"client_id": client_id, "client_secret": client_secret, "grant_type": "refresh_token", "refresh_token": saved["refresh_token"]}, headers={"Accept": "application/json"})
    data = response.json()
    if not data.get("access_token"):
        raise BackItUpError("github_revoked", "GitHub authorization was revoked; reconnect the account")
    data["expires_at"] = time.time() + data.get("expires_in", 28800)
    return {k: data[k] for k in ("access_token", "refresh_token", "expires_at") if k in data}


def collect_metadata(gh: GitHub, owner: str, name: str, selected: list[str], output: Path, budget: Budget, progress=None) -> tuple[dict, list]:
    base = f"/repos/{valid_owner(owner)}/{valid_repo(name)}"
    metadata = output / "metadata"
    metadata.mkdir(parents=True, exist_ok=True)
    coverage = {}
    assets = []
    pulls = None
    # Related objects depend on PR enumeration even when PR JSON is not selected.
    if set(selected) & {"pull_requests", "comments", "reviews"}:
        pulls = list(gh.pages(base + "/pulls", params={"state": "all", "sort": "created", "direction": "asc"}))
    for category in CORE_METADATA:
        budget.check()
        if category not in selected:
            coverage[category] = {"status": "skipped", "reason": "Not selected"}
            continue
        if progress:
            progress("metadata", category)
        try:
            if category == "pull_requests":
                records = pulls or []
            elif category == "issues":
                records = [v for v in gh.pages(base + "/issues", params={"state": "all", "sort": "created", "direction": "asc"}) if "pull_request" not in v]
            elif category == "comments":
                records = {"issue_comments": list(gh.pages(base + "/issues/comments")), "review_comments": list(gh.pages(base + "/pulls/comments"))}
            elif category == "reviews":
                records = [{"pull_number": pull["number"], "reviews": list(gh.pages(base + f"/pulls/{pull['number']}/reviews"))} for pull in pulls or []]
            elif category == "settings":
                records = gh.get(base)
            else:
                records = list(gh.pages(base + "/" + category, params={"state": "all"} if category == "milestones" else None))
            write_json(metadata / f"{category}.json", records)
            coverage[category] = {"status": "complete", "mode": "archive_only", "count": len(records) if isinstance(records, list) else None}
        except BackItUpError as exc:
            coverage[category] = {"status": "failed", "code": exc.code, "reason": exc.message}
            # Persist partial coverage for the final failure report; never publish a complete marker.
            write_json(metadata / "coverage.json", coverage)
            exc.coverage = coverage
            raise
    if "release_assets" in selected:
        releases = list(gh.pages(base + "/releases"))
        for release in releases:
            for asset in gh.pages(base + f"/releases/{release['id']}/assets"):
                path = output / "assets" / str(release["id"]) / str(asset["id"])
                info = gh.download_asset(base + f"/releases/assets/{asset['id']}", path, budget)
                if info["size"] != asset["size"]:
                    raise BackItUpError("asset_size", "A release asset changed during capture")
                assets.append({**info, "id": asset["id"], "name": asset["name"], "release_id": release["id"], "path": path.relative_to(output).as_posix()})
        coverage["release_assets"] = {"status": "complete", "mode": "archive_only", "count": len(assets)}
    else:
        coverage["release_assets"] = {"status": "skipped", "reason": "Not selected"}
    return coverage, assets


def capture_wiki(git: Git, owner: str, name: str, output: Path) -> dict:
    try:
        repo = output / "wiki.git"
        captured = git.capture(f"https://github.com/{valid_owner(owner)}/{valid_repo(name)}.wiki.git", repo)
        git.remove_remote(repo)
        return {"status": "complete", "mode": "archive_only", "refs": captured["refs"]}
    except BackItUpError as exc:
        raise BackItUpError("wiki_access", "Selected wiki could not be captured with this GitHub authorization. Deselect wiki or grant supported access; the backup is incomplete.") from exc
