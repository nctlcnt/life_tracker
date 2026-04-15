# 个人精力与状态调度引擎（Merlin System）架构与落地计划 v3

本计划将传统的"时间记录仪"升级为 **RPG 风格的个人精力管理与状态调度系统**。

- **日和 (Hiyori)**：前端记录器 / 雷达。负责自然语言交互、无感记录、贪婪采集非结构化特征（天气、饮食、情绪等），丢入 JSON 桶。
- **梅林 (Merlin)**：算法中枢 / 数值策划。跑统计算法、预测宕机倒计时、结算 Buff/Debuff，每月输出"版本更新报告"。

## 0. 架构决策（已拍板）

| 决策点 | 结论 |
|---|---|
| **Merlin 落位** | 作为 `bot/merlin/` 子包 + 定时任务；与日和平级，同属 bot 系统。不引入独立进程或独立 FastAPI 服务，同进程直接函数调用 |
| **数据来源** | 不仅限 `events` timeline，**更主要是用户的所有原始消息/对话**（messages 表）。数据积累比 timeline 快得多 |
| **日和的负担** | **日和 prompt 不做任何特征抽取**，保持对话专注。所有 `inferred_factors/states` 抽取由 Merlin 在后台离线批处理完成 |
| **特征抽取方式** | 离线 pipeline：定时批量拉未分析消息 → 便宜 LLM（Haiku / `gpt-4o-mini`）批量抽取 → 写入 JSON 桶。与日和主链路完全解耦 |
| **冷启动策略** | 即便样本不足也先跑起来——数据不够时模型自动退化为简单统计分析，数据够了再看效果 |
| **Merlin 产出消费方** | **仅注入日和的 prompt**（MVP 阶段不做前端面板）。用户主动询问 Merlin 的入口通过斜杠命令 / 私聊指令 |
| **隔离策略** | 所有 schema 改动在新分支实现，新 schema 稳定后再写 migration 脚本迁移旧数据；migration 必须带备份和回滚，不影响现稳定版本 |
| **是否上云** | 暂不上云，隐私/加密议题延后 |

---

## 1. 系统双轨运行机制 (Dual-Track)

两条轨道并行，但**两条都在 Merlin 后台跑**，日和全程不参与分析：

- **主干执行轨 (Production Track)**：基于已"转正"的固定特征 X 和固定词缀库 Y，做概率预测。日和通过函数调用读取主干结算结果，据此调度和干预用户。
- **影子侦察轨 (Discovery Track)**：Merlin 后台批处理抽取隐性变量和新奇情绪，存入 `inferred_factors` / `inferred_states` 的 JSON 桶，供聚类和月报使用。

数据库遵循 **"绝不删除"** 原则：采用软删除（Soft Drop）/ 冷宫（Icebox）机制，废弃参数只屏蔽不删除，随时可复活。

---

## 2. 实施路线图（精简版）

按数据成熟度而非时间硬推。每个里程碑完成后再开下一个。

### M1（~2 周）：离线采集管道 + 基础设施
- **原始消息已在 `messages` 表沉淀**（现有机制），M1 确保未分析消息有标记位可拉取
- 新建 `inferred_tags` 表（或 `messages` 表加 JSON 列）存放离线抽取结果：`inferred_factors` / `inferred_states` / `extracted_at` / `extractor_version`
- 新建 `bot/merlin/` 子包骨架：
  ```
  bot/merlin/
  ├── __init__.py
  ├── extractor.py            # 批量 LLM 抽取器
  ├── scheduler_hook.py       # 挂到 bot/scheduler.py 的定时循环
  └── evals/                  # 抽取器评测模块（见 §2.6）
      ├── gold_set.jsonl
      ├── run_benchmark.py
      ├── metrics.py
      ├── judges.py
      └── results/
  ```
  - `extractor.py`：定时从 `messages` 拉未分析的一批，调便宜模型（Haiku / `gpt-4o-mini`）产出 JSON 标签
  - `scheduler_hook.py`：挂到 `bot/scheduler.py` 的定时循环（如每 30 分钟批跑一次，或按空闲时段触发）
- 建立双层 Schema：主干特征列 + 探索 JSON 桶
- 冷宫机制：加 `status` 字段（`active` / `iced`），绝不 `DROP COLUMN`
- 新分支开发，不动现有稳定 schema
- **日和 prompt 完全不改动**
- **在 M1 编码前先完成 §2.6 的 LLM 选型 benchmark**，确定 `extractor.py` 用哪个模型

### M2（~2 周）：词缀固化 + 派生特征
- **人工/半自动聚类**：每周导出 `inferred_*` 让用户肉眼扫一遍 + 人工命名词缀，加入 `modifiers_dict` 表
- 派生特征：`sleep_debt_3d`、`continuous_focus_hours`、`days_since_last_chill` 等，直接基于 timeline event 的时间和 notes 计算（暂定：视图/每次计算 vs 物化表 —— 后续迭代再定）
- 主干 X 特征表固化：`sleep_hours`（从 timeline 推得）、`is_period`（日和聊天识别）、`weather_condition`（已有 `bot/weather.py`）、`heavy_meal`（日和聊天识别）等

### M3（~1 个月）：上 Apriori + 月报原型
- **模块 D：关联规则挖掘 (mlxtend / Apriori)**：最容易跑、数据需求最低，是最早能产生价值的环节
  - 设置 min_support 阈值 + 人工过滤伪相关
- **月度"版本更新报告"原型**：对比主干轨 vs 影子轨数据，输出游戏感研报
  - 发现新兴词缀（聚类新星系）
  - 揪出隐性杀手（高频出现的未转正特征）
  - 废弃失效规则（权重降至阈值以下的 Buff/Debuff）

### M4+（视数据量触发）：更重的算法模块
按数据需求从低到高逐步开启：

- **模块 A：L1 正则化特征筛选 (Lasso)**：自动把无用参数权重压成 0。**bootstrap 多次取中位数**缓解单用户场景下权重抖动
- **模块 B：清晨结算面板（多标签分类 Random Forest）**：每日风险评估。用 `class_weight='balanced'` 或 SMOTE 处理稀有 Debuff
- **模块 C：生存倒计时**：**先用 Kaplan-Meier 看曲线**；累计 20-30 次"宕机"事件后再考虑 Cox 模型（Cox 的比例风险假设单用户通常违反，保留为后续迭代项）

### 非 MVP（可选 / 后续迭代）
- **贝叶斯网络归因（pgmpy）**：安装链重、DAG 结构学习小样本不稳。**建议砍掉或无限延后**
- **Shadow Deploy 影子模式**：单用户 overkill，直接 A/B 一周即可
- **前端 Merlin 面板**：等算法产出稳定后再评估

---

## 2.6 LLM 抽取器选型 Benchmark（M1 开工前必做）

抽取器的 LLM 选型走 **"LLM-as-extractor benchmark"** 标准流程，代码放在 `bot/merlin/evals/`。

### 为什么放 `bot/merlin/evals/`
- 和生产代码 `bot/merlin/extractor.py` **同包**，可直接 `from bot.merlin.extractor import extract` 复用——避免评测/生产调用路径不一致
- 子目录隔离，不会被 scheduler 意外调用
- 和 `scripts/`（运维/操作性脚本）职责清晰：`evals/` 是评估代码

### 一、建 gold set（50-100 条）
- 从 `messages` 表随机抽 50-100 条，覆盖不同场景（早/晚、专注/吐槽、短消息/长消息）
- 用户人工标注 `inferred_factors` / `inferred_states`
- 存为 `gold_set.jsonl`：`{"msg_id": 123, "content": "...", "gold_factors": [...], "gold_states": [...]}`

### 二、量化指标

| 维度 | 指标 | 实现 |
|---|---|---|
| 抽取质量 | Precision / Recall / F1（按标签集合交集） | `metrics.py`：set 交集脚本 |
| 语义匹配（同义不同表述） | BERTScore / embedding cosine 阈值匹配 | `sentence-transformers` |
| JSON 格式遵从率 | 成功解析比例 | try/except 统计 |
| 幻觉率 | 非 gold 标签占比 + LLM-as-judge 打分 | `judges.py`：调 Opus/GPT-4o 当裁判 |
| 延迟 | p50 / p95 per call | timer 包装 |
| 成本 | $/1k messages（按 token 实测） | 按官方计价 |
| 稳定性 | 同一条跑 3 次的 Jaccard 相似度（temperature=0 期望 ≥ 0.8） | 循环 diff |

### 三、候选与加权评分
- **候选**：`claude-haiku-4-5` / `gpt-4o-mini` / `gemini-flash`
- **综合分**：`0.5 * F1 + 0.3 * (1 - normalized_cost) + 0.2 * (1 - normalized_latency)`

### 四、决策阈值
- F1 < 0.6 → 模型太弱，换或加 few-shot
- 幻觉率 > 15% → 加 few-shot / schema 约束（function calling）
- 稳定性 Jaccard < 0.8 → 降 temperature 或换模型
- 成本差异 > 3× 但 F1 差异 < 5% → 选便宜的

### 五、MVP 偷懒版（若懒得全量人工标注）
用 **LLM distillation evaluation**：
1. 抽 20 条消息
2. 先用 Opus / GPT-4o 跑一遍当"伪 gold"
3. 用候选便宜模型对比伪 gold 的 F1
4. 只看**成本 + F1** 两维决策

### 六、产出
- `results/YYYY-MM-DD_<models>.md` 存每次对比报告
- 每次更换模型或升级 prompt 重跑，保留历史基线

---

## 2.5 可选算法栈（Merlin 离线 pipeline 技术选型参考）

本系统本质是一条 **"offline analytics pipeline" / "behavioral log mining"** 流水线。与日和主交互完全解耦。下表是每个环节的候选算法，具体选型在对应里程碑前的"待澄清项"中敲定。

| 环节 | 候选算法 / 库 | 选型状态 |
|---|---|---|
| **① 离线特征抽取** | 批量 LLM extraction（Haiku / `gpt-4o-mini`）按空闲时段批处理 | ✅ 已定方向（便宜 LLM 批抽）；具体模型待 M1 benchmark |
| **② 文本向量化** | `sentence-transformers` 本地 / OpenAI `text-embedding-3-small` / Gemini embedding | ⏳ 待决（M2 前） |
| **③ 主题发现 / 状态星系** | **BERTopic**（首选，现代化 embedding + HDBSCAN + c-TF-IDF）/ LDA（老派）/ Top2Vec | ⏳ 待决（M2 前） |
| **④ 情绪/状态时间序列** | VADER（英文轻量）/ 本地多语情感分类器 / LLM 直接打分 | ⏳ 待决（M2-M3） |
| **⑤ 变点检测 (Change Point Detection)** | `ruptures`（PELT / Binseg / Window）| ⏳ 待决（M3+）——自动识别"状态切换时间点" |
| **⑥ 时序异常检测** | `prophet` 残差 / Isolation Forest on rolling window / `adtk` | ⏳ 待决（M4+）——发现宕机前兆 |
| **⑦ 消息行为聚类** | HDBSCAN on embedding / K-Means | ⏳ 待决（M2）；初期可人工扫列表替代 |
| **⑧ 关联规则** | `mlxtend` Apriori / FP-Growth | ✅ M3 采用 Apriori |
| **⑨ 序列模式挖掘** | **PrefixSpan**（推荐）/ GSP / SPADE | ⏳ 待决（M3+）——发现"下雨 → 数小时后脑雾"这类时序连招 |
| **⑩ 特征筛选** | Lasso (L1) + bootstrap / Elastic Net | ✅ M4+ 采用 Lasso |
| **⑪ 多标签分类** | Random Forest + `class_weight='balanced'` / SMOTE 处理不平衡 | ✅ M4+ 采用 RF |
| **⑫ 生存分析** | Kaplan-Meier（先上）→ Cox PH（数据够后） | ✅ 先 KM 再 Cox，分阶段 |
| **⑬ 归因分析（非 MVP）** | 贝叶斯网络 `pgmpy` / 因果推断 `dowhy` | ⏸ 砍掉或无限延后 |

**两个关键组合拳**：
- **BERTopic (③) + HDBSCAN (⑦)**：从用户自由吐槽里自动发现"状态星系"，直接对应 Phase 0 的聚类目标
- **变点检测 (⑤) + 序列挖掘 (⑨)**：把"什么时候切换状态"和"切换前有什么前兆"连起来，是生存预警的基础

---

## 3. 闭环调度与人工校准

### 3.1 系统数据流转
1. 日和准备主动找用户聊天前，先**同进程**调用 Merlin 的结算函数
2. Merlin 根据当前时间戳和 X 参数，结算身上挂着的 Buff/Debuff
3. 日和读取结算结果注入 prompt：若带 `[Debuff: 脑雾]`，放弃催促进度，改递极低阻力的小台阶

### 3.2 `@Merlin` 人工校准（替代原"RLHF"提法）
- 用户通过斜杠命令 / 私聊指令覆写 Merlin 的判定："算错了，我今天彻底清零了"
- **不做单样本在线 refit**（会震荡）：override 只存为 ground-truth 标签，**每月批训练一次**
- 同一入口用于：人工校准、主动查询 Merlin 状态、反馈评估（见 §4）

---

## 4. 评估与成本

### 4.1 评估指标
- **用户反馈驱动**：通过斜杠命令 / 私聊入口收集用户对预测的事后确认
- 观察指标（示例，后续细化）：预测与用户事后确认的一致率、Buff/Debuff 预警的提前时间、用户主动查询 Merlin 的频次

### 4.2 Token 成本
- 日和主链路 token **零增加**（抽取全部离线化）
- 新增成本：Merlin 离线批抽取器调便宜模型（Haiku / `gpt-4o-mini`）。批处理 + 低价模型组合下成本可控
- **策略**：M1 先用最简批处理实现，观察一周实际月度成本，再决定是否做提取降频 / 样本采样 / 去重等优化

### 4.3 测试策略
- 新增 fixture 数据集覆盖典型场景：Buff/Debuff 触发、宕机预测、关联规则挖掘
- 随算法迭代持续扩充测试集
- `bot/test_mode.py` 目前只捕 prompt payload，需扩展以支持数值结算的单测

### 4.4 Migration
- 新 schema 稳定前只在新分支内部使用
- 稳定后写 migration 脚本：**必须带数据备份 + 回滚机制**
- 不触碰现稳定版本的数据文件

---

## 5. 循环迭代机制（v2 延续）

整个系统不是一次性交付，而是随季节、生理周期、心态变化**自我迭代打补丁**。循环由每月的"版本更新报告"驱动：

1. **发现新兴词缀**：影子轨数据聚类出新的状态星系 → 用户命名 → 转正进主干
2. **揪出隐性杀手**：宕机事件中频繁出现的未转正特征 → 转正为底层 X
3. **废弃失效规则**：L1 权重降至阈值以下的特征 → 打入冷宫（不删，可复活）
4. **替换更优特征**：如"睡眠深度"替代"睡眠时长"

每月报告后，用户决定本月演化方向，下一轮 M1-M4 的增量工作据此调整。

---

## 6. 待澄清 / 后续 plan 细化项（不阻塞 M1）

以下项目不阻塞落地，但需要在对应里程碑前细化：

**M1 内**
- [ ] **离线抽取器的 LLM 选型**：按 §2.6 流程跑 benchmark。Haiku 4.5 / `gpt-4o-mini` / Gemini Flash 三选一
- [ ] **批抽取触发策略**：固定周期（如每 30 分钟）vs 空闲时段（无用户消息时跑）vs 消息数量阈值触发
- [ ] **抽取 prompt 模板**：一条消息 vs 一个对话窗口（如前后 ±5 条）作为抽取单位；如何引导稳定输出 JSON
- [ ] **`heavy_meal`、`is_period` 等离线识别规则**：是否作为抽取器的固定抽取字段之一

**M2 前**
- [ ] **Embedding 模型选型**：OpenAI `text-embedding-3-small`（外网）vs 本地 `sentence-transformers`（装依赖）vs Gemini embedding
- [ ] **主题发现算法**：BERTopic（首选） vs LDA vs Top2Vec；先跑 silhouette / coherence 对比
- [ ] **消息行为聚类**：HDBSCAN vs K-Means；初期可能仍以人工扫列表为主
- [ ] **派生特征存储形式**：SQL 视图 vs 每次计算 vs 物化表
- [ ] **连续专注的定义**：基于 timeline event 时间 + notes 的具体切分规则

**M3 前**
- [ ] **Merlin 斜杠命令集**：`/merlin status`、`/merlin override`、`/merlin why` 等具体命令设计
- [ ] **情绪时间序列方案**：VADER / 本地分类器 / LLM 打分
- [ ] **序列模式挖掘 (PrefixSpan) 是否在 M3 一起上**，还是等 Apriori 月报稳定后再加

**M4+ 评估**
- [ ] **变点检测 (`ruptures`) 与时序异常 (`prophet` / Isolation Forest)** 的引入时机
- [ ] **前端 Merlin 面板是否做、何时做**

---

*下一步：在 M1 开工前，先补完"待澄清项 - M1 内"的四条（LLM 选型 benchmark、批抽取触发策略、抽取 prompt 模板、schema 具体字段清单），然后在新分支开始实现。*
