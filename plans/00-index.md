# 项目大脑缓存

> 最后整理: 2026-05-05
> 规则: 此文件是 plan 总览入口。specs/todos 已迁 Linear (LT team)，本文件保留指针；ideas/archive 仍为本地 markdown
> 技术难题沉淀见 [devlog.md](../devlog.md)

## 近期计划更新

> 最近 7 天有改动的 plan 文件：
> - **2026-05-05 specs/todos 迁移至 Linear (LT team)**：5 个 spec → Linear Project，1 个 todo → LT-1 Issue。stub 集中在 `5-migrated/`；`1-ideas/` 和 `4-archive/` 保持不动
> - `1-ideas/inspiration.md` — 2026-05-03 新增想法 #9–#15（美食地图/偏好查询系统、memory 强化用户画像、bot 系统消息自动消失、觉察 channel + 轻量状态打标 agent 新思路、prompt 个人信息移出 cache 权衡）
> - `2-specs/dispatch-poc.md` — 2026-05-01 步骤 0 + 1 完成后**暂停**，产出已合入 main：步骤 0（63 条人工标注，L1 覆盖率 22.2% 落在灰区）+ 步骤 1（`bot/prompts_dispatch.py` 4 份 prompt + parsers，342 行）。剩余步骤 2-7 未开工
> - `2-specs/dispatch-poc.md` — 2026-04-28 新建并增补：第二 bot 进程 + 4 份 prompt + ACTIONS/FACTS 协议 + 离线标注先行；增补 escalate_state 多轮粘滞机制（Option b：跳 DECIDE 不跳 PARAPHRASE，对话风格不切换）+ 离线 replay 验证步骤（cache hit / 模型间通讯全程 log）+ 实施步骤重排（先本地 API 验证再上 Discord）
> - `4-archive/mcp-bot-2026-04-25.md` — 2026-04-25 整体推翻（Phase 1 实施全跑通后因 TOS / 物理 / 单用户价值三层边界 revert；详 devlog 同日条目）
> - `2-specs/planned-event.md` — 2026-04-22 多次修订（分支核查 + plan 最终化），PR #8 已合入 main，功能收尾
> - `2-specs/2026Q2-consolidation.md` — 2026-04-23 新建（Q2 整合重构总纲 + Phase 2 dispatch 成本估算完成）；2026-04-25 抽掉原 Phase 1（已推翻）
> - `4-archive/notes-memory-split-2026-04-25.md` — 2026-04-25 新归档（Q2 原 Phase 1 整体推翻）

## 🎯 本周焦点 (最多 3 个)

当前正在投入精力的,详细介绍改动的过程和最新进展，以追踪最近的更新。

- [2026 Q2 整合重构](https://linear.app/chachas/project/2026-q2-整合重构-97639f6c6030) — 原四阶段总纲;Phase 1 (Notes+Memory) 已推翻归档;Phase 2 (Flash+Smart 分发) 细化设计落地到 dispatch-poc (步骤 0+1 完成后暂停);剩余 Phase 3 (主动询问重写) / Phase 4 (精力槽)
- [planned-event 支持](https://linear.app/chachas/project/planned-event-支持-aec55f2886f9) — 后端 + 三态视觉已合入 main (PR #8),前端删除按钮刚补,收尾中

## 🟢 进行中 (active)

所有建了 spec 但不是本周焦点的。

- [Dispatch POC 双层架构](https://linear.app/chachas/project/dispatch-poc-双层架构-d2edb322e01c) — **2026-05-01 暂停**,产出已合入 main (step 1 commit `b06e283`)。步骤 0 完成 (63 条人工标,L1 覆盖率 22.2%) + 步骤 1 完成 (`bot/prompts_dispatch.py` 4 份 prompt + parsers)。剩余 7 步未开工：步骤 2 dispatch engine → 步骤 3 离线 replay 验证 (核心) → 步骤 4 第二 bot 进程 → 步骤 5 路由 + config → 步骤 6 实测 2 周 → 步骤 7 决策。恢复时直接接步骤 2
- [Merlin 精力调度引擎](https://linear.app/chachas/project/merlin-精力调度引擎-1a080495a02d) — v3 架构 + M1–M4+ 路线图,长线
- [Obsidian 接入](https://linear.app/chachas/project/obsidian-接入-b7441f2f60eb) — `query_obsidian` 工具 + MCP server 设计稿,尚未实施

## 💡 想法池 (ideas)

没升级成 spec,可能永远不会做。**不要在这里排序,也不要强迫自己处理**。

- [inspiration.md](1-ideas/inspiration.md) — 15 段原始想法流水（#2–#8 是 Q2 整合 plan 的源头；#9–#15 为 2026-05-03 新增）
- [dispatch-cost-estimate.md](1-ideas/dispatch-cost-estimate.md) — Flash+Smart 分发成本调研(Phase 2 决策依据)

## 📋 执行清单 (todos)

短命,做完就删。

- [LT-1 Disaster Recovery Drill](https://linear.app/chachas/issue/LT-1/disaster-recovery-drill-litestreamr2-备份链验收) — Litestream → R2 备份链首次验收（档位 1+2 read-only / 不动 live DB；档位 3 全链路演练可选）

## 🪦 已归档 (archive)

被推翻或已完成沉淀,**不要在这里排序**。文件名带日期方便排序。

- [prompt-sections-2026-04-18](4-archive/prompt-sections-2026-04-18.md) — 6 段正交 section + chat/poll unify,2026-04-18 已落地,文件首行自标 ✅ 归档
- [energy-2026-04-18](4-archive/energy-2026-04-18.md) — 精力调度 `energy_type` chill/drain 子标签整体撤销,后续情绪/洞察转 Merlin 路线
- [event-notes-split-2026-04-23](4-archive/event-notes-split-2026-04-23.md) — **废止**:原方案把 `events.notes` 拆成 `event_notes` 表挂在 event 上;后续替代方案(daily_notes 独立表)也已推翻,最终保留 `events.notes` 现状
- [notes-memory-split-2026-04-25](4-archive/notes-memory-split-2026-04-25.md) — **废止**:Q2 原 Phase 1。notes 不需 first-class 化(`events.notes` 够用),memory 不该收紧到"纯偏好"(继续容纳备忘录式内容才是它的实际价值)
- [role-split-2026-04-23](4-archive/role-split-2026-04-23.md) — **部分废止**:Role A/B 拆分被 Q2 Phase 2 的 Flash+Smart 分发取代;Role C 夜间清理原计划并入 Phase 1 daily summary cron,Phase 1 推翻后整体搁置;阶段 3(主动询问重写)保留并入 Phase 3
- [ai-token-classifier-2026-04-23](4-archive/ai-token-classifier-2026-04-23.md) — **部分吸收**:Classifier 前置多处理器架构改为"Flash 主聊 + 工具意图 escalate 到 Smart",落地在 Q2 Phase 2
- [mcp-bot-2026-04-25](4-archive/mcp-bot-2026-04-25.md) — **整体推翻**：Phase 1 实施全跑通后，因 Anthropic TOS 禁第三方 app 走订阅 + MCP 单用户无架构价值，整体 revert（commit `0631972`）

## 📦 已完成 (done,最近 10 条)

只保留最近的,老的直接删,**有 git log 作证就够了**。

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
