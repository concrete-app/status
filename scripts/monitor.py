#!/usr/bin/env python3
"""Check every component in components.yml and publish status.json.

History stays inside that one JSON file (90 daily buckets and ~48 hours of
raw checks). Git history does not grow: the file is force-pushed as the only
commit on the status-data branch.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from statuslib import dump_status, load_config, run_checks, utcnow

ROOT = Path(__file__).resolve().parents[1]
DATA_BRANCH = "status-data"


def redact(text: str) -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        text = text.replace(token, "REDACTED")
    text = re.sub(r"x-access-token:[^@\s]+", "x-access-token:REDACTED", text)
    text = re.sub(r"gh[pousr]_[A-Za-z0-9]+", "REDACTED", text)
    text = re.sub(r"github_pat_[A-Za-z0-9_]+", "REDACTED", text)
    text = re.sub(r"(?i)authorization: basic \S+", "AUTHORIZATION: basic REDACTED", text)
    return text


def remote_with_token(remote: str, token: str) -> str:
    """Add the Actions token when origin is a plain https GitHub URL.

    actions/checkout keeps the token out of `git remote get-url`, so a fresh
    repository that only copies that URL cannot push.
    """
    token = token.strip()
    if not token or not remote.startswith("https://github.com/"):
        return remote
    rest = remote.split("://", 1)[1]
    host = rest.split("/", 1)[0]
    if "@" in host:
        return remote
    path = rest.split("/", 1)[1].strip("/") if "/" in rest else ""
    if path.endswith(".git"):
        path = path[:-4]
    return f"https://x-access-token:{token}@github.com/{path}.git"


def git(args: list[str], cwd: str | Path, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        env=env,
    )
    if check and proc.returncode != 0:
        message = redact((proc.stderr or proc.stdout or "git failed").strip())
        raise RuntimeError(message)
    return proc


def load_previous(repo: Path, branch: str) -> dict | None:
    """Return the current data-branch document, or None when the branch is new.

    Any other fetch or read failure aborts the run so a publish cannot replace
    a real history with an empty one.
    """
    fetched = git(
        ["fetch", "--depth", "1", "origin", f"{branch}:refs/remotes/origin/{branch}"],
        repo,
        check=False,
    )
    if fetched.returncode != 0:
        message = (fetched.stderr or fetched.stdout or "").lower()
        if "couldn't find remote ref" in message or "not found" in message:
            return None
        raise RuntimeError(redact((fetched.stderr or fetched.stdout or "fetch failed").strip()))
    shown = git(["show", f"refs/remotes/origin/{branch}:status.json"], repo, check=False)
    if shown.returncode != 0:
        raise RuntimeError(redact((shown.stderr or "status.json missing on data branch").strip()))
    return json.loads(shown.stdout)


def push_args(branch: str, lease: str) -> list[str]:
    """Build the data-branch push.

    The expected SHA has to sit in the same argument as ``--force-with-lease``.
    A following argument is parsed as the remote URL, so ``origin`` becomes the
    refspec and the push never updates ``status-data``.
    """
    if lease:
        return [
            "push",
            f"--force-with-lease=refs/heads/{branch}:{lease}",
            "origin",
            f"HEAD:refs/heads/{branch}",
        ]
    return ["push", "origin", f"HEAD:refs/heads/{branch}"]


def copy_checkout_auth(src: Path, dst: str) -> None:
    """Reuse the Authorization header actions/checkout stored on this repo."""
    header = git(
        ["config", "--local", "--get", "http.https://github.com/.extraheader"],
        src,
        check=False,
    )
    value = header.stdout.strip() if header.returncode == 0 else ""
    if value:
        git(["config", "http.https://github.com/.extraheader", value], dst)


def publish(document_text: str, repo: Path, branch: str) -> None:
    remote = git(["remote", "get-url", "origin"], repo).stdout.strip()
    if not remote:
        raise RuntimeError("origin is not configured")
    remote = remote_with_token(remote, os.environ.get("GITHUB_TOKEN", ""))
    with tempfile.TemporaryDirectory(prefix="status-data-") as tmp:
        git(["init", "-q"], tmp)
        git(["checkout", "--orphan", branch], tmp)
        git(["config", "commit.gpgsign", "false"], tmp)
        if os.environ.get("GITHUB_ACTIONS"):
            name = "github-actions[bot]"
            email = "41898282+github-actions[bot]@users.noreply.github.com"
        else:
            name = git(["config", "user.name"], repo, check=False).stdout.strip() or "Concrete Status"
            email = git(["config", "user.email"], repo, check=False).stdout.strip() or "status@concrete.ch"
        git(["config", "user.name", name], tmp)
        git(["config", "user.email", email], tmp)
        path = Path(tmp) / "status.json"
        path.write_text(document_text, encoding="utf-8")
        git(["add", "status.json"], tmp)
        git(["commit", "-q", "-m", "Update status data"], tmp)
        git(["remote", "add", "origin", remote], tmp)
        copy_checkout_auth(repo, tmp)
        listed = git(["ls-remote", "origin", f"refs/heads/{branch}"], tmp, check=False)
        lease = ""
        if listed.returncode == 0 and listed.stdout.strip():
            lease = listed.stdout.split()[0]
        git(push_args(branch, lease), tmp)


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Concrete status components")
    parser.add_argument("--config", default=str(ROOT / "components.yml"))
    parser.add_argument("--repo", default=str(ROOT))
    parser.add_argument("--branch", default=DATA_BRANCH)
    parser.add_argument("--previous", default="", help="Path to a previous status.json")
    parser.add_argument("--out", default="", help="Also write the document to this path")
    parser.add_argument("--push", action="store_true", help="Force-push the data branch")
    args = parser.parse_args()

    repo = Path(args.repo)
    config = load_config(args.config)
    if args.previous:
        previous = json.loads(Path(args.previous).read_text(encoding="utf-8"))
    elif args.push:
        previous = load_previous(repo, args.branch)
    else:
        previous = None
        remote = git(["remote", "get-url", "origin"], repo, check=False)
        if remote.returncode == 0:
            previous = load_previous(repo, args.branch)

    document = run_checks(config, previous, utcnow())
    text = dump_status(document)
    if args.out:
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        print(f"wrote {destination}")
    if args.push:
        publish(text, repo, args.branch)
        print(f"published {args.branch}")
    for section in document["sections"]:
        for component in section["components"]:
            latency = component.get("latencyMs")
            extra = f" {latency} ms" if latency is not None else ""
            detail = f" — {component['detail']}" if component.get("detail") else ""
            print(f"{component['id']}: {component['status']}{extra}{detail}")
    print(
        f"overall: {document['overall']['state']} "
        f"uptime90d={document['overall']['uptime90d']} "
        f"incidents={len(document['incidents'])}"
    )


if __name__ == "__main__":
    main()
