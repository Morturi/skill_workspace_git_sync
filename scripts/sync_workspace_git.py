#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


PLACEHOLDER_MARKERS = ("CHANGE_ME", "REPLACE_ME", "<", "your-", "example.com")
SUPPORTED_PULL_STRATEGIES = {"rebase", "ff-only"}


@dataclass
class RepoResult:
    repo: str
    branch: str
    commit_status: str
    sync_status: str
    ok: bool
    details: str = ""


class GitCommandError(RuntimeError):
    def __init__(self, repo: Path, args: list[str], result: subprocess.CompletedProcess[str]) -> None:
        self.repo = repo
        self.args = args
        self.result = result
        message = result.stderr.strip() or result.stdout.strip() or "git command failed"
        super().__init__(f"{repo.name}: {' '.join(args)} -> {message}")


def parse_args() -> argparse.Namespace:
    skill_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Commit, pull, and push all Git repositories under a workspace root."
    )
    parser.add_argument(
        "--workspace-root",
        default=str(skill_root.parent),
        help="Workspace root to scan. Defaults to the parent of the skill folder.",
    )
    parser.add_argument(
        "--config",
        default=str(skill_root / "workspace-sync.config.json"),
        help="Path to workspace-sync.config.json.",
    )
    parser.add_argument(
        "--commit-message",
        default=None,
        help="Override the default commit message from config.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview actions without mutating repositories.",
    )
    return parser.parse_args()


def load_config(config_path: Path) -> dict:
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in config file: {config_path}") from exc


def is_work_tree_repo(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        text=True,
        capture_output=True,
        encoding="utf-8",
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def resolve_excluded_paths(workspace_root: Path, skill_root: Path, config: dict) -> list[Path]:
    excluded_paths = [skill_root.resolve()]
    for raw_path in config.get("exclude_paths", []):
        candidate = Path(str(raw_path))
        if not candidate.is_absolute():
            candidate = (workspace_root / candidate).resolve()
        else:
            candidate = candidate.resolve()
        excluded_paths.append(candidate)
    return excluded_paths


def discover_repos(workspace_root: Path, excluded_paths: list[Path]) -> list[Path]:
    repos: list[Path] = []
    seen: set[Path] = set()
    excluded_set = {path.resolve() for path in excluded_paths}

    for current_root, dirnames, _filenames in os.walk(workspace_root):
        current_path = Path(current_root).resolve()

        if any(current_path == excluded_path or excluded_path in current_path.parents for excluded_path in excluded_set):
            dirnames[:] = []
            continue

        if (current_path / ".git").exists() and current_path not in seen and is_work_tree_repo(current_path):
            repos.append(current_path)
            seen.add(current_path)

        dirnames[:] = [name for name in dirnames if name != ".git"]

    repos.sort()
    return repos


def is_placeholder(value: str | None) -> bool:
    if value is None:
        return True
    normalized = value.strip()
    if not normalized:
        return True
    lowered = normalized.lower()
    return any(marker.lower() in lowered for marker in PLACEHOLDER_MARKERS)


def git(
    repo: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        encoding="utf-8",
        env=env,
    )
    if check and result.returncode != 0:
        raise GitCommandError(repo, args, result)
    return result


def repo_remote_url(repo: Path, remote_name: str) -> str:
    result = git(repo, ["remote", "get-url", remote_name], check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def needs_github_https_auth(remote_url: str) -> bool:
    if not remote_url:
        return False
    parsed = urlparse(remote_url)
    return parsed.scheme in {"http", "https"} and (parsed.hostname or "") in {"github.com", "gist.github.com"}


def get_token(config: dict) -> str:
    token_env_name = str(config.get("github_pat_env", "")).strip()
    if token_env_name:
        env_value = os.environ.get(token_env_name, "").strip()
        if env_value:
            return env_value
    return str(config.get("github_pat", "")).strip()


def build_authenticated_env(username: str, token: str) -> tuple[dict[str, str], tempfile.TemporaryDirectory[str]]:
    temp_dir = tempfile.TemporaryDirectory(prefix="workspace-git-sync-")
    askpass_path = Path(temp_dir.name) / "git_askpass.py"
    askpass_path.write_text(
        "import os\n"
        "import sys\n"
        "prompt = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "if 'username' in prompt.lower():\n"
        "    print(os.environ['WORKSPACE_GIT_SYNC_USERNAME'])\n"
        "else:\n"
        "    print(os.environ['WORKSPACE_GIT_SYNC_TOKEN'])\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = str(askpass_path)
    env["WORKSPACE_GIT_SYNC_USERNAME"] = username
    env["WORKSPACE_GIT_SYNC_TOKEN"] = token
    return env, temp_dir


def validate_config_for_real_run(config: dict, repos: list[Path], remote_name: str) -> tuple[str, str | None]:
    git_user_name = str(config.get("git_user_name", "")).strip()
    git_user_email = str(config.get("git_user_email", "")).strip()
    github_username = str(config.get("github_username", "")).strip()
    token = get_token(config)

    if is_placeholder(git_user_name):
        raise RuntimeError("Config field git_user_name is missing or still set to a placeholder.")
    if is_placeholder(git_user_email):
        raise RuntimeError("Config field git_user_email is missing or still set to a placeholder.")

    requires_auth = any(needs_github_https_auth(repo_remote_url(repo, remote_name)) for repo in repos)
    if requires_auth:
        if is_placeholder(github_username):
            raise RuntimeError("Config field github_username is missing or still set to a placeholder.")
        if is_placeholder(token):
            token_env_name = str(config.get("github_pat_env", "")).strip() or "github_pat"
            raise RuntimeError(
                f"GitHub or Gist HTTPS remotes were found, but no usable token is available. Fill github_pat or set {token_env_name}."
            )
        return token, github_username

    return "", None


def pull_strategy_from_config(config: dict) -> str:
    strategy = str(config.get("pull_strategy", "rebase")).strip().lower() or "rebase"
    if strategy not in SUPPORTED_PULL_STRATEGIES:
        raise RuntimeError(
            f"Unsupported pull_strategy '{strategy}'. Use one of: {', '.join(sorted(SUPPORTED_PULL_STRATEGIES))}."
        )
    return strategy


def sync_repo(
    repo: Path,
    *,
    remote_name: str,
    commit_message: str,
    pull_strategy: str,
    git_user_name: str,
    git_user_email: str,
    authenticated_env: dict[str, str] | None,
    dry_run: bool,
) -> RepoResult:
    branch = git(repo, ["branch", "--show-current"]).stdout.strip()
    if not branch:
        return RepoResult(repo.name, "(detached)", "skipped", "skipped", False, "Detached HEAD is not supported.")

    dirty = bool(git(repo, ["status", "--porcelain"]).stdout.strip())
    remote_url = repo_remote_url(repo, remote_name)
    sync_env = authenticated_env if needs_github_https_auth(remote_url) else None

    if dry_run:
        commit_status = f"would commit '{commit_message}'" if dirty else "clean"
        sync_status = f"would pull/push via {remote_name}" if remote_url else "no remote"
        return RepoResult(repo.name, branch, commit_status, sync_status, True)

    git(repo, ["config", "user.name", git_user_name])
    git(repo, ["config", "user.email", git_user_email])

    if dirty:
        git(repo, ["add", "-A"])
        if git(repo, ["diff", "--cached", "--name-only"]).stdout.strip():
            git(repo, ["commit", "-m", commit_message])
            commit_status = "committed"
        else:
            commit_status = "nothing staged"
    else:
        commit_status = "clean"

    if not remote_url:
        return RepoResult(repo.name, branch, commit_status, "no remote", True)

    git(repo, ["fetch", remote_name], env=sync_env)
    remote_ref = git(repo, ["show-ref", "--verify", f"refs/remotes/{remote_name}/{branch}"], check=False)

    if remote_ref.returncode == 0:
        if pull_strategy == "rebase":
            git(repo, ["pull", "--rebase", "--autostash", remote_name, branch], env=sync_env)
        else:
            git(repo, ["pull", "--ff-only", remote_name, branch], env=sync_env)
        pulled = "pulled"
    else:
        pulled = "remote branch missing"

    upstream = git(repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], check=False)
    if upstream.returncode == 0:
        git(repo, ["push"], env=sync_env)
    else:
        git(repo, ["push", "-u", remote_name, branch], env=sync_env)

    sync_status = "pushed" if pulled == "pulled" else f"pushed ({pulled})"
    return RepoResult(repo.name, branch, commit_status, sync_status, True)


def print_results(results: list[RepoResult]) -> None:
    repo_width = max(len("repository"), *(len(item.repo) for item in results))
    branch_width = max(len("branch"), *(len(item.branch) for item in results))
    commit_width = max(len("commit"), *(len(item.commit_status) for item in results))
    header = (
        f"{'repository'.ljust(repo_width)}  "
        f"{'branch'.ljust(branch_width)}  "
        f"{'commit'.ljust(commit_width)}  sync"
    )
    print(header)
    print("-" * len(header))
    for item in results:
        print(
            f"{item.repo.ljust(repo_width)}  "
            f"{item.branch.ljust(branch_width)}  "
            f"{item.commit_status.ljust(commit_width)}  "
            f"{item.sync_status}"
        )
        if item.details:
            print(f"  note: {item.details}")


def main() -> int:
    args = parse_args()
    workspace_root = Path(args.workspace_root).resolve()
    config_path = Path(args.config).resolve()
    skill_root = Path(__file__).resolve().parents[1]

    if not workspace_root.exists():
        raise RuntimeError(f"Workspace root does not exist: {workspace_root}")

    config = load_config(config_path)
    remote_name = str(config.get("remote_name", "origin")).strip() or "origin"
    pull_strategy = pull_strategy_from_config(config)
    commit_message = args.commit_message or str(config.get("default_commit_message", "")).strip()
    if not commit_message:
        raise RuntimeError("No commit message available. Set default_commit_message or pass --commit-message.")

    excluded_paths = resolve_excluded_paths(workspace_root, skill_root, config)
    repos = discover_repos(workspace_root, excluded_paths=excluded_paths)
    if not repos:
        raise RuntimeError(f"No Git repositories found under {workspace_root}")

    print(f"Workspace root: {workspace_root}")
    print(f"Repositories found: {len(repos)}")
    for repo in repos:
        print(f"- {repo}")

    authenticated_env = None
    temp_dir = None

    try:
        if not args.dry_run:
            token, github_username = validate_config_for_real_run(config, repos, remote_name)
            if github_username is not None:
                authenticated_env, temp_dir = build_authenticated_env(github_username, token)

        results: list[RepoResult] = []
        overall_ok = True

        for repo in repos:
            try:
                result = sync_repo(
                    repo,
                    remote_name=remote_name,
                    commit_message=commit_message,
                    pull_strategy=pull_strategy,
                    git_user_name=str(config.get("git_user_name", "")).strip(),
                    git_user_email=str(config.get("git_user_email", "")).strip(),
                    authenticated_env=authenticated_env,
                    dry_run=args.dry_run,
                )
            except Exception as exc:  # noqa: BLE001
                overall_ok = False
                result = RepoResult(repo.name, "?", "failed", "failed", False, str(exc))
            results.append(result)
            overall_ok = overall_ok and result.ok

        print()
        print_results(results)
        return 0 if overall_ok else 1
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
