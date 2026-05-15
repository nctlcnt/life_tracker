# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository. 

This repository is a personal project for building a life tracking system using Discord and AI. The codebase is developed and maintained by an individual, and is not intended for public use or contribution. 

When working with this codebase, please keep the following in mind:

## ui style
Clean, gentle aesthetic with Morandi-inspired muted tones — soft grays, dusty pinks, sage greens, and warm beiges. Use generous whitespace to let content breathe. Avoid harsh contrasts or saturated colors. Reference `frontend/src/styles/theme.css` for defined colors and fonts.

## Commit & Pull Request Guidelines

### Commits
- Imperative subject, ≤ 50 chars
- Blank line, then body explaining *why*
- Reference Linear only when needed:
  - `Fixes HED-123` on the final/merge commit (auto-closes the issue)
  - `Refs HED-456` when a commit touches an unrelated issue

### Branches
- Named `<type>/<issue-id>-<slug>`, e.g. `feat/HED-123-gmail-digest`
- Type: `feat` / `fix` / `chore` / `refactor`
- Issue ID in branch handles the linking — no need to repeat in every commit
- Delete after merge

## Deploy 端口与环境约定
本机同时跑 prod 和 dev，**端口归属是固定的**：
- `8080` = **prod**（`docker-compose.prod.yml`，bot 用 `.env.prod` + 仓库根目录的 `config.json` 里的 prod token / server.port）
- `8081` = **staging / dev**（`docker-compose.staging.yml`，用 `config.dev.json`）

部署时绝不能把 dev 的 compose 推到 8080，否则会把 prod 的 bot 顶掉、并且让 dev 用了 prod 的 token。规则：
- `make deploy` / `make deploy-local` 只能跑 prod compose（已带 `--env-file .env.prod`）
- 想本机起 dev，**显式覆盖端口**（`API_PORT=8081 docker compose up` 或走 staging compose），不要用 8080 默认值
- bot 使用哪份配置由 compose mount 决定：prod mount `./config.json`，staging mount `./config.dev.json`——别交叉

## Linear connection
本项目的 issue 跟踪在 Linear 上
每次get issue时，使用两个步骤：
1. Linear:get_issue(id="LIN-123")          # 拿 issue 主体
2. Linear:list_comments(issueId="LIN-123") # 再拿评论列表
评论列表里面是需求变更记录，和一些讨论，甚至有时会有新的需求冒出来，比description更活跃，所以需要单独拿，并且评估和implement。

如果推送改动的comment，不要说技术细节，用自然语言描述改动的scope、内容和原因，方便非技术人员理解。