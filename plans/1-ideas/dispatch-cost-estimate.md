# Dispatch 三策略成本估算报告

生成时间：2026-04-23T23:30:04.288432
样本范围：过去 7 天 user 消息 445 条
id 区间：1235 – 2219

## 需要工具的消息占比（三种估算）

- regex 关键词命中率：**5.6%**（precision ~0.80，粗筛）
- Flash Lite 判断：**未跑**（加 `--flash <gemini-preset-name>` 开启）
- 人工标注（n=30）：**23.3%** ← ground truth

## 月成本矩阵（按当前日均外推 30 天）

日均 63.6 条 → 月约 **1907** 条，采用 need_tool_rate = 23.3%（来源：manual）

| Smart 模型 | conditional_flash | always_smart | rule_based |
|---|---|---|---|
| opus       | $  7.95 | $ 20.61 | $ 12.28 |
| sonnet     | $  4.97 | $ 12.57 | $  7.50 |
| gemini-pro | $  2.00 | $  4.55 | $  2.73 |

## 建议的判断逻辑

- **regex vs Flash vs 人工标注差异大** → 说明 Flash 判断力差 / regex 规则不全。Flash 比人工显著低 → `conditional_flash` 漏记严重，慎选。
- **always_smart + Sonnet** 与 **conditional_flash + Opus** 成本接近 → 选前者（更稳定）。
- **rule_based + Gemini Pro** 通常最省，但要接受 ~15% miss rate（走 Flash 兜底）。
- 如果月成本差距 < $10 → 不用在这个维度抠，选稳定性优先。

## 参数

定价（per million tokens，PRICING dict）：

| 模型 | input | output | cache_read |
|---|---|---|---|
| opus | $5.00 | $25.00 | $0.50 |
| sonnet | $3.00 | $15.00 | $0.30 |
| haiku | $1.00 | $5.00 | $0.10 |
| gemini-pro | $1.25 | $5.00 | $0.00 |
| flash-lite | $0.10 | $0.40 | $0.00 |

Token 预算（单条消息粗估，TOKEN_BUDGET dict）：

- `flash_chat`: in=2000, out=150
- `smart_tools`: in=5000, out=300
- `smart_decision`: in=3000, out=200

cache hit rate 假设：Smart=70%，Flash=0%

关键词正则（regex 模拟 rule_based）：`记[一下]?(一?下|着|的|录)?|帮我记|给我记|提醒我|别忘[了记]|再叫我|到点喊我|deadline|截止|期限|ddl|设(一?个)?(闹钟|提醒)|加(一?条|到)?(todo|待办|任务)|(明天|后天|下周|下月|今晚|周[一二三四五六日天])\s*(要|有|得|去|做|考|交|见|开会|上课)|\d{1,2}\s*[点:：]\s*(要|有|得|去|做|考|交|见|开会|上课|开始|结束|睡|吃|提醒)|安排一?下|日程(怎么样|有什么)`

