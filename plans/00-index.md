# 项目大脑缓存

> 最后整理: 2026-04-23
> 规则: 此文件是唯一入口,任何 plan 变动都要同步这里
> 技术难题沉淀见 [devlog.md](devlog.md)

## 🎯 本周焦点 (最多 3 个)

当前正在投入精力的,超过 3 个说明注意力分散了。

- [2026 Q2 整合重构](plan-2026Q2-consolidation.md) — 四阶段总纲(Notes+Memory / Flash+Smart 分发 / 主动询问重写 / 精力槽),起 Phase 1
- [planned-event 支持](plan-appointment-into-planned-event.md) — 后端 + 三态视觉已合入 main (PR #8),前端删除按钮刚补,收尾中
- [Prompt 重构](plan-prompt-new.md) — 6 段正交 section + chat/poll unify 已落地,仍在观察 cache 命中与前缀识别稳定性

## 🟢 进行中 (active)

所有建了 spec 但不是本周焦点的。

- [Merlin 精力调度引擎](plan-Merlin.md) — v3 架构 + M1–M4+ 路线图,长线
- [Obsidian 接入](Plan-Obsidian-Claude-Code.md) — `query_obsidian` 工具 + MCP server 设计稿,尚未实施

## 💡 想法池 (ideas)

没升级成 spec,可能永远不会做。**不要在这里排序,也不要强迫自己处理**。

- [plan-inspiration.md](plan-inspiration.md) — 8 段原始想法流水,Q2 整合 plan 的源头
- [dispatch-cost-estimate.md](dispatch-cost-estimate.md) — Flash+Smart 分发成本调研(Phase 2 决策依据)

## ⏸️ 暂停中 / 被取代 (paused / superseded)

做了一半停下的或方案被新 plan 吃掉,写清楚**为什么**,不然复工时会茫然。

- [plan-event-notes-split.md](plan-event-notes-split.md) — **废止** (2026-04-23):原方案把 `events.notes` 拆成 `event_notes` 表挂在 event 上;Q2 Phase 1 改走 daily_notes 独立表,event 不再有 notes 概念
- [plan-role-split.md](plan-role-split.md) — **部分废止**:Role A/B 拆分被 Q2 Phase 2 的 Flash+Smart 分发取代;Role C 夜间清理并入 Phase 1 的 daily summary cron;阶段 3(主动询问重写)思路保留并入 Phase 3
- [ai-ai-token-eventual-kahn.md](ai-ai-token-eventual-kahn.md) — **部分吸收**:Classifier 前置多处理器架构改为"Flash 主聊 + 工具意图 escalate 到 Smart",落地在 Q2 Phase 2

## 📦 已完成 (done,最近 10 条)

只保留最近的,老的直接删,**有 git log 作证就够了**。

- 2026-04-23 ✅ planned-event 前端删除按钮 (`d2eccc9`)
- 2026-04-22 ✅ planned-event 后端 + 前端三态视觉 (PR #8 合入 main)
- 2026-04-22 ✅ RhythmView 新视图 + 五 Tab 导航 (`f388040`)
- 2026-04-22 ✅ PROTOCOLS section 临时下线 (`265a431`)
- 2026-04-21 ✅ 调度器轮询策略重构:45–55 分钟随机 + 基准重置 (`03a10e8`)
- 2026-04-21 ✅ AI Preset 管理 `/model` `/fallback` + autocomplete (`f15ffb4`)
- 2026-04-21 ✅ 工具描述大幅瘦身 (`0d3863f`)
- 2026-04-21 ✅ SILENT 消息处理 + text chunk pipeline 优化 (`6e925e3`)
- 2026-04-18 ✅ Prompt 结构性重构:6 段正交 section + chat/poll unify
- 2026-04-18 ✅ `energy_type` chill/drain 子标签整体撤销

## 🪦 已归档 (archive 链接,不展开)

`archive/` 文件夹暂未建立。已正式归档的 plan:

- [Plan-energy.md](Plan-energy.md) — 2026-04-18:精力调度 `energy_type` 字段整体撤销,后续情绪/洞察转 Merlin 路线
