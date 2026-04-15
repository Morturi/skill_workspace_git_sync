# Workspace Git Sync

`workspace-git-sync` 是一个可随文件夹一起迁移的本地 skill，用来统一提交、拉取并推送某个工作区下的全部 Git 仓库。它不依赖 PowerShell 脚本，核心逻辑在 [scripts/sync_workspace_git.py](scripts/sync_workspace_git.py)。

把整个 `workspace-git-sync` 文件夹复制到另一个工作区根目录后，脚本默认会把“skill 文件夹的上一级目录”当作新的工作区根目录，因此可以开箱即用。

## 目录结构

```text
workspace-git-sync/
├─ agents/
│  └─ openai.yaml
├─ scripts/
│  └─ sync_workspace_git.py
├─ README.md
├─ SKILL.md
└─ workspace-sync.config.json
```

## 快速使用

1. 编辑 [workspace-sync.config.json](workspace-sync.config.json)。
2. 填写 `git_user_name`、`git_user_email`、`github_username`。
3. 二选一提供令牌：
   `github_pat`
   `github_pat_env` 指向的环境变量，例如 `GITHUB_TOKEN`
4. 先执行预演：

```bash
python scripts/sync_workspace_git.py --dry-run
```

5. 确认无误后正式执行：

```bash
python scripts/sync_workspace_git.py
```

## 配置说明

[workspace-sync.config.json](workspace-sync.config.json) 当前字段含义如下：

- `git_user_name`: 写入每个仓库本地 Git 配置的提交用户名。
- `git_user_email`: 写入每个仓库本地 Git 配置的提交邮箱。
- `github_username`: 访问 GitHub 或 Gist HTTPS 远程仓库时使用的用户名。
- `github_pat`: 可直接填写的 GitHub Personal Access Token。
- `github_pat_env`: 优先读取的环境变量名；如果该环境变量存在，则优先于 `github_pat`。
- `remote_name`: 默认同步的远程名，通常为 `origin`。
- `default_commit_message`: 用户未显式传入 `--commit-message` 时使用的提交信息。
- `pull_strategy`: 支持 `rebase` 或 `ff-only`。
- `exclude_paths`: 可选排除目录列表。相对路径按工作区根目录解析，也支持绝对路径。

## 命令示例

默认扫描 skill 上一级目录：

```bash
python scripts/sync_workspace_git.py
```

只做预演，不修改任何仓库：

```bash
python scripts/sync_workspace_git.py --dry-run
```

指定提交信息：

```bash
python scripts/sync_workspace_git.py --commit-message "chore: sync workspace changes"
```

如果 skill 不放在工作区根目录下，可以显式指定：

```bash
python scripts/sync_workspace_git.py --workspace-root "C:/path/to/workspace"
```

## 执行逻辑

脚本主要分成 4 个阶段：

1. 解析参数并加载配置。
2. 解析排除目录，扫描真实 Git 工作树仓库。
3. 非 `--dry-run` 时校验身份配置，并为 GitHub/Gist HTTPS 远程仓库构造临时 `GIT_ASKPASS` 环境。
4. 逐仓库执行提交、拉取、推送，并汇总结果。

## 代码流程图

```mermaid
flowchart TD
    A[启动脚本 main] --> B[parse_args]
    B --> C[load_config]
    C --> D{工作区路径存在?}
    D -- 否 --> Z1[抛出错误并退出]
    D -- 是 --> E[pull_strategy_from_config]
    E --> F[resolve_excluded_paths]
    F --> G[discover_repos]
    G --> H{找到 Git 仓库?}
    H -- 否 --> Z2[抛出错误并退出]
    H -- 是 --> I[打印工作区和仓库列表]
    I --> J{dry-run?}
    J -- 否 --> K[validate_config_for_real_run]
    K --> L{需要 GitHub/Gist HTTPS 认证?}
    L -- 是 --> M[build_authenticated_env]
    L -- 否 --> N[直接进入逐仓库处理]
    M --> N
    J -- 是 --> N
    N --> O[遍历每个 repo 调用 sync_repo]
    O --> P{repo 出错?}
    P -- 是 --> Q[记录 failed 结果]
    P -- 否 --> R[记录成功结果]
    Q --> S{还有下一个 repo?}
    R --> S
    S -- 是 --> O
    S -- 否 --> T[print_results]
    T --> U[清理临时 askpass 目录]
    U --> V[返回整体退出码]
```

## 单仓库处理流程

`sync_repo()` 的主要决策如下：

```mermaid
flowchart TD
    A[进入 sync_repo] --> B[读取当前分支]
    B --> C{分支为空?}
    C -- 是 --> D[返回 detached HEAD 跳过结果]
    C -- 否 --> E[检查工作区是否 dirty]
    E --> F[读取 remote URL]
    F --> G{dry-run?}
    G -- 是 --> H[返回 would commit / would pull-push]
    G -- 否 --> I[设置本地 user.name 和 user.email]
    I --> J{dirty?}
    J -- 是 --> K[git add -A]
    K --> L{暂存区有变更?}
    L -- 是 --> M[git commit]
    L -- 否 --> N[记为 nothing staged]
    J -- 否 --> O[记为 clean]
    M --> P{存在 remote?}
    N --> P
    O --> P
    P -- 否 --> Q[返回 no remote]
    P -- 是 --> R[git fetch]
    R --> S{远程分支存在?}
    S -- 是 --> T{pull_strategy}
    T -- rebase --> U[git pull --rebase --autostash]
    T -- ff-only --> V[git pull --ff-only]
    S -- 否 --> W[标记 remote branch missing]
    U --> X{已配置 upstream?}
    V --> X
    W --> X
    X -- 是 --> Y[git push]
    X -- 否 --> AA[git push -u]
    Y --> AB[返回 RepoResult]
    AA --> AB
```

## 关键实现点

- 只把真正能通过 `git rev-parse --is-inside-work-tree` 的目录视为仓库，避免把异常目录误识别为仓库。
- 自动跳过 skill 自身目录，避免把 `workspace-git-sync` 当作被同步对象。
- 只有在远程 URL 是 `https://github.com/...` 或 `https://gist.github.com/...` 时，才会启用临时认证环境。
- 临时认证通过 `GIT_ASKPASS` 注入，不会把令牌直接写进仓库配置。
- 即使某个仓库失败，脚本也会继续处理后续仓库，最后统一汇总结果。

## 建议使用方式

- 第一次接入新工作区时先跑一次 `--dry-run`。
- 优先使用环境变量保存令牌，而不是明文写入 `github_pat`。
- 如果工作区里有缓存目录、镜像目录或其他不应参与同步的路径，就放进 `exclude_paths`。
- 批量同步前尽量确认每个仓库当前分支都在预期状态，避免把不该提交的改动一起提交。
