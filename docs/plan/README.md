# 项目大脑缓存

> 最后整理: 2026-08-26
> 规则: 此文件是 plan 总览入口。specs/todos/archive 已迁 Linear (LT team)，本文件保留指针；ideas 仍为本地 markdown
> 技术难题沉淀见 [development_log.md](../../development_log.md)

## 近期计划更新

> 关键更新（新到旧）：
> - **2026-08-26 LT-169**：在 [聊天与工具并行分层](parallel-chat-tool-split.md) 中补全异步基础设施的表结构、模块边界、锁/发送队列关系以及开关与回退契约；这是 LT-170 的实现依据。
> - **2026-08-26 Dispatch POC 收口**：串行 Dispatch POC 已被异步并行分层取代；LT-12、LT-13、LT-15、LT-16、LT-17 Canceled，LT-14 作为测试基础设施保留。
> - **2026-08-26 异步设计定案**：静默窗口 30 秒、批次最长等待 60 秒、心跳 2 分钟、批次占用 5 分钟；review 决策与验证层次均已写回同一份设计文档。
> - **2026-05-05 文档迁移**：specs/todos/archive 迁至 Linear（LT team），本文件只保留入口与状态指针；ideas 继续保留本地 Markdown。

## 🎯 本周焦点 (最多 3 个)

当前正在投入精力的,详细介绍改动的过程和最新进展，以追踪最近的更新。

- [聊天与工具并行分层](parallel-chat-tool-split.md) `0/6 backlog` — 设计已定案，代码实现尚未开始；LT-169 的实现规格已在本分支补齐，合并后再做被它阻塞的 LT-170（统一发送队列）。LT-171（场景边界）、LT-172（天气快照）、LT-173（清理 Dispatch 死代码）互不依赖，可随时并行。

## 🟢 进行中 (active)

所有建了 spec 但不是本周焦点的。

- [Merlin 精力调度引擎](https://linear.app/chachas/project/merlin-精力调度引擎-5003f2ea4641) — 长线、尚未启动；等当前 Hiyori 主线收尾后，再从 LLM 抽取器 benchmark 评估是否进入 M1。
- [Obsidian 接入](https://linear.app/chachas/project/obsidian-接入-b7441f2f60eb) — `query_obsidian` 工具 + MCP server 设计稿,尚未实施 (Project 自身即 todo, 无 sub-issue)

## 💡 想法池 (ideas)

没升级成 spec,可能永远不会做。**不要在这里排序,也不要强迫自己处理**。

- [inspiration.md](inspiration.md) — 15 段原始想法流水（#2–#8 是 Q2 整合 plan 的源头；#9–#15 为 2026-05-03 新增）
- `dispatch-cost-estimate.md` — Flash+Smart 分发成本调研（历史文件，当前仓库未保留）

## 📋 执行清单 (todos)

短命,做完就删。

- [LT-1 Disaster Recovery Drill](https://linear.app/chachas/issue/LT-1/disaster-recovery-drill-litestreamr2-备份链验收) — Litestream → R2 备份链首次验收（档位 1+2 read-only / 不动 live DB；档位 3 全链路演练可选）

## 🪦 已归档 (archive, 在 Linear)

被推翻或已完成沉淀。本地 stub 在 `5-migrated/`，full description 在 Linear。

- **Dispatch POC 串行架构** — **Cancelled / Superseded（2026-08-26）**：LT-10、LT-11 的历史产出保留；LT-12、LT-13、LT-15、LT-16、LT-17 已取消，后续以 [聊天与工具并行分层](parallel-chat-tool-split.md) 和 LT-169～LT-173 为准；LT-14 仅保留为测试基础设施。

- [Prompt 6 段正交 section](https://linear.app/chachas/project/prompt-6-段正交-section-已落地-d4d7a0f330db) — **Completed**: 6 段正交 + chat/poll unify, 2026-04-18 落地 (refactor/prompt-sections 分支)
- [energy_type chill/drain 子标签](https://linear.app/chachas/project/energy-type-chilldrain-子标签-撤销-94c7423cc072) — **Cancelled**: 蓄水/漏水二级标签整体撤销, 后续转 Merlin 离线管道
- [event-notes 拆表](https://linear.app/chachas/project/event-notes-拆表-废止-4044ed41a8c5) — **Cancelled**: `events.notes` 现状已够用, notes 不需 first-class 化
- [Notes + Memory 重构 (Q2 原 P1)](https://linear.app/chachas/project/notes-memory-重构-q2-原-p1废止-37d5c2e3a592) — **Cancelled**: events.notes + memory 现状够用, memory 不该收紧到纯偏好
- [Role A/B/C 拆分](https://linear.app/chachas/project/role-abc-拆分-部分废止-bade0dcc0193) — **Cancelled (部分废止)**: A/B 被 Q2 Phase 2 的 Flash+Smart dispatch 取代; C 并入 Phase 1 后整体搁置; 阶段 3 (主动询问重写) 保留并入 Phase 3
- [AI Token Classifier 架构](https://linear.app/chachas/project/ai-token-classifier-架构-部分吸收-831b9f5191cb) — **Cancelled (部分吸收)**: Classifier 前置多处理器架构改为 "Flash 主聊 + 工具意图 escalate 到 Smart", 落地在 Q2 Phase 2 / Dispatch POC
- [MCP Bot B](https://linear.app/chachas/project/mcp-bot-b-整体推翻-12614a547eed) — **Cancelled (整体推翻)**: Phase 1 跑通后因 Anthropic TOS 禁第三方 app 走订阅 + MCP 单用户无架构价值，整体 revert (commit `0631972`)

## 📦 已完成 (done,最近 10 条)

只保留最近的,老的直接删,**有 git log 作证就够了**。

- 2026-05-05 ✅ planned-event 支持 Project 收口 → Completed (5 sub-issues LT-5~LT-9 全 Done): Schema / Tools / Prompts / API / Frontend 三态视觉已全部合入 main
- 2026-05-03 ✅ deploy 修复：ghcr 镜像引用统一到 `nctlcnt/life_tracker`（release.yml / Makefile / docker-compose.prod.yml 全部对齐，VPS 拉取恢复正常）(`d8e9462`)
- 2026-05-01 ✅ dispatch-poc 步骤 1：`bot/prompts_dispatch.py` 4 份 prompt + parsers，342 行，已合入 main (`b06e283`)
- 2026-04-29 ✅ dispatch-poc 步骤 0：63 条样本人工标注完成 + `scripts/parse_dispatch_labels.py`，L1 覆盖率 22.2% (`f293cf5`)
- 2026-04-27 ✅ OpenAI 引擎 `max_tokens` 改名 `max_completion_tokens`，兼容新版 API (`d839534`)
- 2026-04-25 ✅ pending_reminders 注入 Block 4 + 主动 follow-up 策略默认开启 (`a44fb46`)
- 2026-04-25 ✅ 前端移除 Memory Tab，导航收为 4 个（日/周/Project/Rhythm）(`a44fb46`)
- 2026-04-24 ✅ OpenAI 原生引擎 `ai_engine_openai.py` 新增 (`f36f996`)
- 2026-04-24 ✅ weather 模块改接 tomorrow.io API，支持小时预报 (`4c16475`)
- 2026-04-23 ✅ dispatch 成本离线估算脚本 + 人工标注工具 (`c8b7126`)
- 2026-04-23 ✅ proactive prompt 按 provider 分离 + `get_proactive_prompt()` (`18b7da6`)
- 2026-04-23 ✅ planned-event 前端删除按钮 (`d2eccc9`)
- 2026-04-22 ✅ planned-event 后端 + 前端三态视觉 (PR #8 合入 main)
- 2026-04-22 ✅ RhythmView 新视图 + 五 Tab 导航 (`f388040`)
- 2026-04-22 ✅ PROTOCOLS section 临时下线 (`265a431`)
- 2026-04-21 ✅ 调度器轮询策略重构:45–55 分钟随机 + 基准重置 (`03a10e8`)
- 2026-04-21 ✅ AI Preset 管理 `/model` `/fallback` + autocomplete (`f15ffb4`)
- 2026-04-21 ✅ 工具描述大幅瘦身 (`0d3863f`)
- 2026-04-21 ✅ SILENT 消息处理 + text chunk pipeline 优化 (`6e925e3`)
- 2026-04-18 ✅ Prompt 结构性重构:6 段正交 section + chat/poll unify (归档见 `archive/prompt-sections-2026-04-18.md`)
- 2026-04-18 ✅ `energy_type` chill/drain 子标签整体撤销
