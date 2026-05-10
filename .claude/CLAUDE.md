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

## Linear connection
本项目的 issue 跟踪在 Linear 上
每次get issue时，使用两个步骤：
1. Linear:get_issue(id="LIN-123")          # 拿 issue 主体
2. Linear:list_comments(issueId="LIN-123") # 再拿评论列表
评论列表里面是需求变更记录，和一些讨论，甚至有时会有新的需求冒出来，比description更活跃，所以需要单独拿，并且评估和implement。

如果推送改动的comment，不要说技术细节，用自然语言描述改动的scope、内容和原因，方便非技术人员理解。

先看 Linear Operating Manual，或者在 Linear 里搜索这个标题，依据里面的规范来管理 issue 和 comment。