# 项目大脑缓存

> 最后整理: 2026-05-05
> 规则: 此文件是 plan 总览入口。specs/todos/archive 已迁 Linear (LT team)，本文件保留指针；ideas 仍为本地 markdown
> 技术难题沉淀见 [devlog.md](../devlog.md)

## 近期计划更新

> 最近 7 天有改动的 plan 文件：
> - **2026-05-05 archive 也迁 Linear + 进度细化**：7 个 archive → Project (6 Cancelled + 1 Completed)；4 个 active Project 加 21 个 progress issues (LT-2~LT-22) 反映步骤完成情况；planned-event Project → Completed。所有 stub 收在 `5-migrated/`
> - **2026-05-05 specs/todos 迁移至 Linear (LT team)**：5 个 spec → Linear Project，1 个 todo → LT-1 Issue。stub 集中在 `5-migrated/`；`1-ideas/` 保持本地 markdown
> - `1-ideas/inspiration.md` — 2026-05-03 新增想法 #9–#15（美食地图/偏好查询系统、memory 强化用户画像、bot 系统消息自动消失、觉察 channel + 轻量状态打标 agent 新思路、prompt 个人信息移出 cache 权衡）
> - `2-specs/dispatch-poc.md` — 2026-05-01 步骤 0 + 1 完成后**暂停**，产出已合入 main：步骤 0（63 条人工标注，L1 覆盖率 22.2% 落在灰区）+ 步骤 1（`bot/prompts_dispatch.py` 4 份 prompt + parsers，342 行）。剩余步骤 2-7 未开工
> - `2-specs/dispatch-poc.md` — 2026-04-28 新建并增补：第二 bot 进程 + 4 份 prompt + ACTIONS/FACTS 协议 + 离线标注先行；增补 escalate_state 多轮粘滞机制（Option b：跳 DECIDE 不跳 PARAPHRASE，对话风格不切换）+ 离线 replay 验证步骤（cache hit / 模型间通讯全程 log）+ 实施步骤重排（先本地 API 验证再上 Discord）
> - `4-archive/mcp-bot-2026-04-25.md` — 2026-04-25 整体推翻（Phase 1 实施全跑通后因 TOS / 物理 / 单用户价值三层边界 revert；详 devlog 同日条目）
> - `2-specs/planned-event.md` — 2026-04-22 多次修订（分支核查 + plan 最终化），PR #8 已合入 main，功能收尾
> - `2-specs/2026Q2-consolidation.md` — 2026-04-23 新建（Q2 整合重构总纲 + Phase 2 dispatch 成本估算完成）；2026-04-25 抽掉原 Phase 1（已推翻）
> - `4-archive/notes-memory-split-2026-04-25.md` — 2026-04-25 新归档（Q2 原 Phase 1 整体推翻）

## 🎯 本周焦点 (最多 3 个)

当前正在投入精力的,详细介绍改动的过程和最新进展，以追踪最近的更新。

- [2026 Q2 整合重构](https://linear.app/chachas/project/2026-q2-整合重构-97639f6c6030) `0/2 active` — Phase 1 已推翻 (LT-2 Cancelled，详见 archive Project Notes+Memory); Phase 3 删除 PROTOCOLS (LT-3 Backlog, 半天); Phase 4 精力槽方向 A (LT-4 Backlog, 2-3 工作日); Phase 2 dispatch 细化到独立 Project

## 🟢 进行中 (active)

所有建了 spec 但不是本周焦点的。

- [Dispatch POC 双层架构](https://linear.app/chachas/project/dispatch-poc-双层架构-d2edb322e01c) `2/8 done` — **2026-05-01 暂停**。LT-10 (步骤 0 标注) + LT-11 (步骤 1 prompt 草稿) Done; 剩 LT-12 (engine) → LT-13 (离线 replay 验证, 核心) → LT-14 (第二 bot 进程) → LT-15 (路由+config) → LT-16 (实测 2 周) → LT-17 (决策) 全 Backlog。恢复直接接 LT-12
- [Merlin 精力调度引擎](https://linear.app/chachas/project/merlin-精力调度引擎-1a080495a02d) `0/5 backlog` — 长线。LT-18 (LLM 抽取器 benchmark, M1 prereq) → LT-19 (M1 离线管道) → LT-20 (M2 词缀固化) → LT-21 (M3 Apriori 月报) → LT-22 (M4+ 重算法)
- [Obsidian 接入](https://linear.app/chachas/project/obsidian-接入-b7441f2f60eb) — `query_obsidian` 工具 + MCP server 设计稿,尚未实施 (Project 自身即 todo, 无 sub-issue)

## 💡 想法池 (ideas)

没升级成 spec,可能永远不会做。**不要在这里排序,也不要强迫自己处理**。

- [inspiration.md](1-ideas/inspiration.md) — 15 段原始想法流水（#2–#8 是 Q2 整合 plan 的源头；#9–#15 为 2026-05-03 新增）
- [dispatch-cost-estimate.md](1-ideas/dispatch-cost-estimate.md) — Flash+Smart 分发成本调研(Phase 2 决策依据)

## 📋 执行清单 (todos)

短命,做完就删。

- [LT-1 Disaster Recovery Drill](https://linear.app/chachas/issue/LT-1/disaster-recovery-drill-litestreamr2-备份链验收) — Litestream → R2 备份链首次验收（档位 1+2 read-only / 不动 live DB；档位 3 全链路演练可选）

## 🪦 已归档 (archive, 在 Linear)

被推翻或已完成沉淀。本地 stub 在 `5-migrated/`，full description 在 Linear。

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
