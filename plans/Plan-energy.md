### 核心愿景：构建精力与状态调度引擎

放弃完美的层级归类，承认并发与混沌。核心目标是把你的"分类决策成本"降到零，并精准识别"真放松"与"假低能"，让系统帮你动态调度多巴胺。

---

### 实施进度

| 阶段 | 状态 | 分支 |
|---|---|---|
| 第一阶段：数据层 + 前端布局骨架 | ✅ 已完成 (2026-04-13) | `feature/phase1-tricat-schema` |
| 第二阶段：Prompt 调优 | ✅ 已完成 (2026-04-13) | `feature/phase1-tricat-schema` |
| 第三阶段：前端视图 + energy_type 参数化 | ✅ 已完成 (2026-04-14) | `feature/phase3-frontend-energy` |
| 第四阶段：情绪评分机制 | ⬜ 未开始 | — |
| 第五阶段：数据洞察扩展（Streak / 趋势 / 雷达图） | ⬜ 未开始 | — |

---

### 第一阶段：数据层与后端洗牌 (DB & API) ✅

**1. 极简三分法与 Project 挂载** ✅

重构数据库 Schema 和 AI 的 Tool 参数，将 Category 严格精简为三类：
* **Focus（正事/投入）**：*必须* 挂载 `project_name` 字段。计算精力消耗。
* **Routine（日常维护）**：维持生命体征的活动（吃饭、通勤等），无需挂载 Project。
* **Chill（蓄水放松）**：看剧、打游戏等，无需挂载 Project。

完成内容：
- `events` 表新增 `project_name TEXT` 列（热迁移兼容，旧数据不受影响）
- `log_timeline_event` / `update_timeline_event` 工具 schema 更新为 category enum + project_name 字段
- `TOOL_GUIDELINES_CHAT` 中分类规则更新为三分法
- `ai_engine_base.py` 工具分发透传 project_name

**2. 分类算力完全外包** ✅
* `log_timeline_event` 工具描述已更新：Focus 时强制填 project_name，允许直接凭直觉新建项目（如 `Project-大模型探索`）。
* 学习成本归因规则已写入工具描述。

**3. 前端布局骨架** ✅（占位符阶段，为真实数据积累做准备）
* 日视图新布局已上线：左 1/4 多泳道占位符 + 右 3/4 上半蓄水/漏水占位符 + 右 3/4 下半 2×2 四方块（完全可用）
* 三 Tab 导航：日 | 周 | Project Overview（后者带热力图占位符）
* 旧分类（休息/工作/娱乐等）保留兜底颜色，存量数据正常显示

---

### 第二阶段：Prompt 调优与"日和"的脑部手术 (AI Logic) ✅

**1. 精力与情绪的深度挂钩 (Drain vs Chill)** ✅
* `TIME_PERCEPTION_CHAT` / `TIME_PERCEPTION_POLL` 中增加蓄水 vs 漏水的核心判断规则
* 有意识的娱乐是"蓄水（Chill）"，无聊逃避导致的漫无目的滑手机是"漏水（Drain）"
* 漏水时干预策略：递一个高刺激、低阻力的有趣台阶，而不是"累了就去休息"
* `TOOL_GUIDELINES_CHAT` 中增加 `[漏水]` 标记规则，便于事后在 notes 中识别
* `_format_ongoing` 更新：进行中事件现在显示 `project_name`，AI 可见当前在做哪个项目

**2. 智能调度策略升级** ✅
* `PROACTIVE_PROMPT` 更新：当检测到漏水状态，干预策略改为"递一个高刺激、低阻力的有趣台阶"，不再是"累了就去睡吧"

---

### 第三阶段：前端视图大换血 + energy_type 参数化 ✅

抛弃传统的甘特图和严格互斥的饼图，拥抱并发与宏观视角。

**日视图新布局**

```
┌────────────┬─────────────────────────────────────────┐
│            │  蓄水/漏水比例图                         │
│  泳道图    │  （含 [蓄水] / [漏水] 筛选标识）        │
│  （竖向）  ├─────────────────────────────────────────┤
│  左 1/4    │  ┌──────────────┬──────────────┐       │
│            │  │   记忆        │   提醒        │       │
│            │  ├──────────────┼──────────────┤       │
│            │  │   待办        │  Deadlines   │       │
│            │  └──────────────┴──────────────┘       │
└────────────┴─────────────────────────────────────────┘
```

完成内容：
- `energy_type TEXT` 字段加入 events 表，迁移现有 `[漏水]` notes 标记
- `log_timeline_event` / `update_timeline_event` 工具新增 `energy_type` 参数（chill/drain/null）
- prompts.py 中改用 `energy_type` 字段描述，移除 `[漏水]` notes 标记约定
- `ai_engine_base.py` 透传 `energy_type`；`merge_events()` 保留 `project_name` 和 `energy_type`
- 新增 `MultiLaneTimeline` 组件：竖向三泳道（Focus/Routine/Chill），事件块含 tooltip
- 新增 `ChillDrainChart` 组件：精力分布条形图，蓄水/漏水/Focus/Routine 分行，含筛选 chip
- 新增 `ProjectOverview` 组件：GitHub 式热力图，按项目×日期展示 Focus 投入分钟
- 新增 `/api/projects/heatmap` 端点，聚合近 N 天各项目每日 Focus 时长
- `App.tsx` 替换所有占位符为实际组件，恢复日视图的 `/api/timeline` 数据获取

---

### 第四阶段：情绪评分机制（待开发）

**目标**：为每条 Chill 类型的时间段打情绪分，以情绪分辅助判断蓄水/漏水，并加成计算比例。

**核心设计思路**：
- 在 events 表新增 `mood_score INTEGER`（可选，1-5 分或自定义范围）
- 情绪分由用户在对话中主动打分，或由 AI 根据对话情绪推断
- 蓄水/漏水最终判断逻辑：`energy_type` 参数 + 情绪分综合加权
- 蓄水比例计算：`(时长 × 情绪分因子)` 的加权和，高情绪分的 Chill 时间权重更高
- 前端 `ChillDrainChart` 可根据情绪分渐变色块深浅

**实施步骤**：
1. DB 迁移：`events` 表加 `mood_score INTEGER` 列（热迁移，NULL 表示未评分）
2. 工具更新：`log_timeline_event` / `update_timeline_event` 加 `mood_score` 参数，Chill 类型时 AI 可选填
3. Prompt 更新：Chill 事件结束时，日和主动轻问一句情绪感受并记录分数
4. API 更新：`/api/timeline` 返回 `mood_score`；`/api/energy/summary` 加权计算蓄水质量
5. 前端更新：`ChillDrainChart` 中 Chill 色块深浅与 `mood_score` 挂钩

---

### 第五阶段：数据洞察扩展（待开发）

本阶段在 `ProjectOverview` Tab 内扩展三个数据洞察模块，让复盘从"我今天做了什么"升级为"我的精力模式是什么"。

---

#### 5.1 Streak 统计

**目标**：量化项目持续投入的连贯性，提供正向激励。

**数据定义**：
- 连续天数（Streak）：某 Project 在连续自然日内均有 Focus 类型事件记录（按北京时间日期）
- 当前 Streak（`current_streak`）：从今天往前推，连续有记录的天数
- 历史最长 Streak（`best_streak`）：该 Project 有记录以来的最长连续天数
- 当日是否已打卡（`checked_today`）：今天是否有该 Project 的 Focus 事件

**后端**：
- 新增 `/api/projects/streaks` 端点，返回所有 Project 的 streak 数据
- 查询逻辑：从 events 表按 project_name + date 聚合，计算连续天数（Go 语言式：倒序遍历日期序列）
- 响应格式：
  ```json
  [
    {
      "project_name": "Project-大模型探索",
      "current_streak": 5,
      "best_streak": 12,
      "checked_today": true,
      "last_active_date": "2026-04-14"
    }
  ]
  ```

**前端**：
- `ProjectOverview` 热力图每行 Project 名称右侧展示 Streak 徽章
- 当前 Streak > 0：显示火焰图标 + 数字（如 🔥 5）
- 当前 Streak = 0 但 best_streak > 0：显示历史最长（灰色，如 ⚡ 12）
- `checked_today = true` 时徽章高亮；否则低饱和度提示"今日未打卡"
- 可选：在 ProjectOverview 顶部加一行"活跃 Streak 排行"横向卡片，按 current_streak 降序

---

#### 5.2 项目时长趋势

**目标**：可视化各 Project 的 Focus 投入随时间的变化，识别项目冷热切换规律。

**数据聚合粒度**：
- 日视图：过去 30 天，每天每 Project 的 Focus 分钟数（适合精细复盘）
- 周视图：过去 12 周，每周每 Project 的 Focus 小时数（适合宏观趋势）

**后端**：
- 新增 `/api/projects/trends` 端点，支持 `?granularity=daily|weekly&days=30|90` 参数
- 响应格式（daily 示例）：
  ```json
  {
    "granularity": "daily",
    "projects": ["Project-A", "Project-B"],
    "series": [
      { "date": "2026-04-01", "Project-A": 90, "Project-B": 45 },
      { "date": "2026-04-02", "Project-A": 0,  "Project-B": 120 }
    ]
  }
  ```

**前端组件 `ProjectTrendChart`**：
- 堆叠面积图（Stacked Area Chart）或多折线图，Y 轴为分钟/小时，X 轴为日期
- 每个 Project 一种颜色，颜色与热力图保持一致（从主题 Morandi 色盘取色）
- 支持点击图例切换 Project 显示/隐藏
- 切换日/周粒度的 Toggle（与热力图联动，切换后热力图也对应缩放）
- 放置于热力图下方，默认折叠，点击"查看趋势"展开

---

#### 5.3 精力雷达图

**目标**：将一段时间内的精力使用模式压缩为一张多维度雷达图，直觉式感知自己的"生活形状"。

**维度设计（5 维）**：

| 维度 | 计算方式 | 满分标准（可配置） |
|---|---|---|
| 专注深度 | 近 7 天 Focus 总时长（分钟） | 420 分钟/周（1h/天） |
| 蓄水质量 | 近 7 天 Chill 事件中 energy_type=chill 的占比（% × 时长加权） | 90% |
| 生活规律 | 近 7 天 Routine 事件的时间方差（越低越规律，倒数归一化） | 方差 < 15 分钟 |
| 项目专注度 | 近 7 天投入项目数的集中度（Herfindahl 指数，单项目 = 1.0） | 1.0 |
| 精力密度 | 近 7 天 Focus 时长 / 清醒总时长（估算 16h/天） | 30%（约 5h/天） |

（维度与满分标准均可在 config 中自定义，初期硬编码作为 MVP）

**后端**：
- 新增 `/api/energy/radar` 端点，支持 `?days=7|30` 参数
- 返回各维度的原始值与归一化得分（0-1）
- 响应格式：
  ```json
  {
    "period_days": 7,
    "dimensions": [
      { "name": "专注深度",  "raw": 360, "unit": "分钟", "score": 0.86 },
      { "name": "蓄水质量",  "raw": 0.75, "unit": "%",  "score": 0.75 },
      { "name": "生活规律",  "raw": 12.3, "unit": "min_var", "score": 0.82 },
      { "name": "项目专注度","raw": 0.68, "unit": "HHI",     "score": 0.68 },
      { "name": "精力密度",  "raw": 0.22, "unit": "%",       "score": 0.73 }
    ]
  }
  ```

**前端组件 `EnergyRadarChart`**：
- SVG 原生绘制（或 recharts Radar 组件）五边形雷达图，Morandi 配色
- 背景五层同心网格（0.2 / 0.4 / 0.6 / 0.8 / 1.0）
- 当前周期填充色（半透明 sage green）+ 上一周期轮廓线（虚线，对比变化）
- 各顶点标注维度名 + 原始值（hover 显示详细说明）
- 放置于 ProjectOverview Tab 右侧（与热力图左右分栏），7 天 / 30 天切换 Toggle
- 若数据不足（少于 3 天），显示"数据积累中"占位符

---

### 实施路线建议 (MVP 跑通法)

1. **先动后端与 Prompt**：把 DB 字段加好，更新日和的 Function/Tool 定义和规则。✅ 已完成
2. **裸跑积攒数据**：完全不管前端怎么显示，强迫自己用微信聊天的状态跟日和对话，用新的三分法和 AI 自动分 Project 跑上 3-5 天。✅ 已完成
3. **前端收网**：多泳道时间轴、蓄水/漏水比例图、Project 热力图均已实现。✅ 已完成
4. **情绪评分**：为 Chill 时间段打情绪分，以情绪分加成计算蓄水比例。← **第四阶段**
5. **数据洞察扩展**：Streak 统计 → 项目时长趋势 → 精力雷达图，按需逐步上线。← **第五阶段**
