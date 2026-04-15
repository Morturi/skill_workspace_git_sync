---
name: workspace-git-sync
description: Commit, pull, and push all Git repositories under a workspace by using a skill-local config file for Git author identity and GitHub or Gist HTTPS authentication. Use when Codex needs to directly submit or synchronize every repository in the current workspace, especially after batch edits across multiple repos, or when the user asks to commit, pull, push, or sync all workspace repositories at once.
---

# Workspace Git Sync

Use this skill when the user wants one direct pass over every Git repository in a workspace.

## Quick Start

1. Read `workspace-sync.config.json` in the skill root.
2. Ensure `git_user_name`, `git_user_email`, `github_username`, and either `github_pat` or the environment variable named by `github_pat_env` contain real values.
3. If some folders under the workspace should never be scanned, list them in `exclude_paths`.
4. Run `python scripts/sync_workspace_git.py`.

By default, the script treats the parent directory of this skill folder as the workspace root. Copy the whole `workspace-git-sync` folder into another workspace root to reuse it without changing paths.

## Workflow

1. Open `workspace-sync.config.json`.
2. If required fields still contain placeholders, stop and ask the user to fill them before a real sync.
3. If some helper or test folders should be ignored, add them to `exclude_paths` as relative paths from the workspace root.
4. If the user gave a commit message, pass it with `--commit-message`.
5. If the user wants a preview only, run with `--dry-run`.
6. Otherwise run the script normally so it:
   - discovers all Git repositories under the workspace root,
   - sets local `user.name` and `user.email` in each repo,
   - commits dirty repos with one shared message,
   - pulls from the configured remote,
   - pushes back to the configured remote.
7. Summarize results per repository, including skips and failures.

## Command Patterns

Use the default workspace-root behavior when this skill folder sits directly under the workspace root:

```bash
python scripts/sync_workspace_git.py
```

Pass an explicit commit message when the user provides one:

```bash
python scripts/sync_workspace_git.py --commit-message "chore: update workspace"
```

Use dry-run when validating behavior or previewing planned actions:

```bash
python scripts/sync_workspace_git.py --dry-run
```

If the skill folder is stored somewhere else, pass the target workspace explicitly:

```bash
python scripts/sync_workspace_git.py --workspace-root "C:/path/to/workspace"
```

## Notes

- Prefer the token from the environment variable named by `github_pat_env` when it exists. This avoids keeping secrets in plain text.
- Support both `github.com` and `gist.github.com` HTTPS remotes.
- Let the config's `default_commit_message` drive the commit text when the user does not provide one.
- Treat `exclude_paths` as optional relative or absolute paths.
- Fail fast on missing config values before mutating repositories in a real run.
