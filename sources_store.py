"""
Saved data sources: a small sources.json file kept in the app's own GitHub repo.

Streamlit apps can't edit their own Secrets, so sources added from the dashboard are saved here instead,
through the GitHub API, using a token stored in Secrets (GITHUB_TOKEN). Everyone then sees the same list.
Without a token, the app falls back to the sources.json bundled with the code (read-only).
"""
import base64
import json
import os

import requests

FILE_PATH = "sources.json"
API = "https://api.github.com/repos/{repo}/contents/{path}"


class SourceStore:
    def __init__(self, token: str = "", repo: str = "", branch: str = "main"):
        self.token, self.repo, self.branch = (token or "").strip(), (repo or "").strip().strip("/"), branch or "main"

    @property
    def can_save(self) -> bool:
        return bool(self.token and self.repo)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def _url(self) -> str:
        return API.format(repo=self.repo, path=FILE_PATH)

    def load(self) -> tuple:
        """Returns (sources dict, sha or None)."""
        if self.can_save:
            r = requests.get(self._url(), headers=self._headers(), params={"ref": self.branch}, timeout=20)
            if r.status_code == 404:
                return {}, None
            r.raise_for_status()
            body = r.json()
            data = json.loads(base64.b64decode(body.get("content", "")).decode("utf-8") or "{}")
            return _clean(data), body.get("sha")
        local = os.path.join(os.path.dirname(os.path.abspath(__file__)), FILE_PATH)
        if os.path.exists(local):
            with open(local, encoding="utf-8") as fh:
                return _clean(json.load(fh)), None
        return {}, None

    def save(self, sources: dict, message: str) -> None:
        if not self.can_save:
            raise RuntimeError("Saving needs GITHUB_TOKEN and GITHUB_REPO in the app's Secrets.")
        _, sha = self.load()
        payload = {"message": message, "branch": self.branch,
                   "content": base64.b64encode(json.dumps(_clean(sources), indent=2).encode()).decode()}
        if sha:
            payload["sha"] = sha
        r = requests.put(self._url(), headers=self._headers(), json=payload, timeout=20)
        if r.status_code in (401, 403):
            raise RuntimeError("GitHub refused the token. It needs 'Contents: Read and write' access to this repo.")
        if r.status_code == 404:
            raise RuntimeError(f"GitHub repo '{self.repo}' not found. Check GITHUB_REPO (format: username/repo-name).")
        r.raise_for_status()


def _clean(data) -> dict:
    if not isinstance(data, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in data.items() if str(k).strip() and str(v).strip()}
