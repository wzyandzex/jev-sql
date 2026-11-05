# 中期调研：Jev 与 jev-like 开源决策模型及其 finetune 方法

> 调研日期 2026-11-05。用途：评估 Jev（TypeSafe AI，API id `typesafe-ai/jev`）在 BIRD schema linking 上的定位，选定开源对照组，给出可做成论文贡献的 finetune 配方。
> 规则：带数字的结论都附来源标签，标签对应的 URL 见文末「参考来源」表；没能核实的内容标「未核实」；我自己的推断标「推断」。

## TL;DR

1. **ChatGPT 对话里的说法基本属实。** 独立评测论文确实存在，作者是 Deußer 等人（University of Bonn），arXiv 2609.37647，v1 发布于 2026-09-29，v2 发布于 2026-10-29。论文用 jev-1.13.0 跑了 37 个数据集、346,009 个请求，pooled Choice ECE 为 0.028，accuracy 与 ECE 的 Spearman ρ 为 −0.83，并指出 Noul 用固定 0.5 阈值会有偏差 [p-bonn]。Choice confidence 公式与官方文档一致 [d-conf]。只有「1.14.0 是 preview」一条已经过时：jev-1.14.0 在 2026-10-14 发布，`jev-latest` 现在就指向它 [d-models][b-114]。
2. **Bonn 的「多 Noul 高估 yes」在我们的表级阶段没有复现。** Bonn 发现一个请求里问很多个 Noul、且多数答案为「否」时，平均 P(yes) 是 0.209，实际正例率只有 0.041，调出的阈值远高于 0.5 [p-bonn]。本组实测（2026-11-05，BIRD mini-dev，实验 2，498 题，3,613 个 question–table 对）[本组实测]：表级 Noul 平均 P(yes) 0.286，实际正例率 0.284，ECE 0.025（10 个等宽箱）；负例平均 P 0.051，只有 1.4% 的负例 P ≥ 0.5；AUROC 0.992。表阈值从 0.5 降到 0.1 后，gold 表全部保留的题目比例从 85.3% 升到 95.4%。所以不应调高表阈值。列级 Score 是期望等级、不是概率（Score 在 1.0–1.5 的列有 38.6% 是 gold），那里是否高估还没有结论。
3. **版本要钉死。** Vercel 上不带版本号的 `typesafe-ai/jev` 挂了 TypeSafe AI 和 DigitalOcean 两个 provider [v-model]，Gateway 的 decision 响应里 `model` 字段只写 `typesafe-ai/jev` [v-decide]。正式实验应改用 `typesafe-ai/jev-1.14.0` 或 `typesafe-ai/jev-1.13.0`。
4. **不能拿 Jev 的概率当软标签训练开源模型。** TypeSafe 的 Master Customer Agreement 第 2.3(b) 条明确禁止用 Services 或 Output「perform model distillation, train a model to imitate the output」[t-mca]。训练信号只能来自 BIRD/Spider 的 gold 标签或开源 teacher。
5. **开源 jev-like 生态已经很大。** 社区榜单 Jev Decision Index 在 Jev 发布 7 周后已收录约 265 个条目（含少量纯推理技巧）[hf-di]。按该榜单，多个 27–31B 开源微调模型的 balanced skill 高于 Jev 1.14；在与 schema linking 最接近的 ToolRet 相关性排序上，一个 4B 模型也高于 Jev [hf-di]。这个榜单有训练集泄漏风险，只能作参考。
6. **Jev + Text-to-SQL 目前没有论文。** 我用 arXiv API 检索 `abs:Jev AND abs:SQL` 没有结果；唯一相关的是一篇博客，在 3 个问题、56 张表的合成 schema 上做了 table pruning 小实验 [blog-ai2sql]。这块是空白，但审稿人一定会要求开源对照组。
7. **建议先跑的 3 个 baseline：** (a) CodeS/RESDSQL 系的有监督 schema item classifier；(b) Qwen3-Reranker-4B 的零样本 P(yes)；(c) perplexity-ai/pplx-decider-flash-v1.2，它的请求格式与 Jev 相同（state + questions），能直接替换 `call_jev`。三者在 MINIDEV 上的推理都在单卡小时级（推断）。
8. **可做成贡献的 finetune 配方：** 以 Qwen3-Reranker-4B 或 Qwen3.5-4B 为基座，LoRA 微调。数据用 BIRD/Spider train 从 gold SQL 抽出的表/列，加同库 hard negatives。损失用 pointwise BCE 加表内 listwise softmax CE，然后在按库切分的 dev 上拟合 temperature 和阈值。细节见 §4.2。

---

## 1. Jev 本身：核实过的事实

### 1.1 版本、限制、价格

| 项目 | 事实 | 来源 |
|---|---|---|
| 发布 | 2026-09-15 发布，是 TypeSafe 的第一个 System One model；创始人 Diogo Almeida。Vercel AI Gateway 在 2026-09-16 上线 | [b-intro][v-cl1] |
| 版本 | `jev-1.13.0`（2026-09-15）；`jev-1.14.0`（2026-10-14 发布，更好的多语言支持，新增 Box 题型） | [d-models][d-changelog] |
| 别名 | `jev-latest` 和 `jev-preview` 现在都指向 `jev-1.14.0`；官方说明目前没有 preview build | [d-models] |
| 能否钉版本 | 能。官方原话：别名会随新版本移动，「answers can change without any change to your code」，调过阈值的应发送版本化 ID。官方 API 响应的 `model` 字段会返回版本化 ID（如 `jev-1.13.0`） | [d-models][d-score] |
| 弃用节奏 | 官方计划给 1.13.0 下线前 60 天通知，1.14.0 为 30 天；正式弃用政策还没发布 | [b-114] |
| Vercel 上的 ID | `typesafe-ai/jev`（provider：TypeSafe AI 和 DigitalOcean）、`typesafe-ai/jev-1.14.0`、`typesafe-ai/jev-1.13.0`。DigitalOcean 条目的 playground 链接写的是 `typesafe-jev-1.13.0`，所以不带版本号的 slug 可能路由到 1.13（推断，未核实） | [v-model] |
| 上下文 | 默认每个请求 64k、每题 32k（state 加最长的那一题）。加 `TypeSafe-Danger-Zone-Long-Context: true` header 后两个上限都变成 262k，但官方说会更慢、更不准 | [d-models] |
| 题数 | 一个请求里的题数没有单独上限，只受上面两个上下文限制 | [d-models] |
| 选项/等级 | Choice 最多 255 个选项；Score 2–10 个等级 | [d-choice][d-score][v-kb] |
| 请求体 | 最大 32 MiB；state、instructions、criteria 各自最多 64 层嵌套；支持 gzip/zstd 压缩 | [d-errors] |
| 价格 | 输入 $0.042 / 1M tokens，输出免费；Vercel 价格相同 | [d-models][v-kb] |
| 定制 | 不对客户数据做 fine-tune 或 LoRA，所有账户共用同一套权重，只能通过 state、instructions、criteria 定制 | [d-models] |
| 语言 | 英语最好；中日韩等语言可用，但官方说不如英语 | [d-models] |
| 批量 | 官方 cookbook：13 题合并成一次调用，比逐题调用便宜 12.2 倍、快 10.0 倍，答案不变 | [d-parallel] |
| 我们的用量 | exp1 日志：1,497 次调用，平均每次 45.5 题、5,124 input tokens、1.05 s；合计 7.67M tokens，按 $0.042/M 约 $0.32 | 本地 `logs/exp1.log`（截至 2026-11-05 17:40） |

### 1.2 概率是怎么来的

- 官方只说「new architecture, parallel sampler, RLCD（Reinforcement Learning for Calibrated Decisions）」：state 只读一次，所有题并行评估。架构、参数量、训练数据都未公开 [b-intro][d-models][d-primer]。
- Score 文档说模型只看到各等级的描述，看不到等级编号和相邻等级，每个等级单独对照 state 评判 [d-score]。由此推断：每个选项或等级各自打分，再在题内归一化，类似 cross-encoder 逐候选打分（推断，未核实）。
- confidence 公式都已公开 [d-conf]：
  - Noul：不返回 confidence，官方建议用 |2p−1| 代替；阈值建议从 0.7（yes）和 0.3（no）开始，中间段单独处理 [d-conf][d-noul]。
  - Choice：C = (p_max − 1/n)/(1 − 1/n)。只看最大概率，所以 (0.6, 0.3, 0.1) 和 (0.6, 0.2, 0.2) 的 confidence 都是 0.4。官方建议直接对目标选项的概率或 top1/top2 比值设阈值 [d-conf]。
  - Score：用众数等级 m 和概率加权的偏离距离，除以均匀分布的 MAD，下限截到 0 [d-conf]。官方提醒不要用 Score 的期望值在两个等级之间插值求数值 [d-jag]。
- 可复现性：ai2sql 对同一请求连发 5 次，胜出选项的概率漂移中位数 0.02、最大 0.09，但选出的选项 12/12 不变 [blog-ai2sql]。arXiv 2610.00346 测得 Jev 的 test-retest 翻转下限约 1.3/100 [p-gates]。所以阈值 ±0.05 附近的样本可能来回翻。

### 1.3 已知弱点，以及对 schema linking 的含义

| 弱点 | 证据 | 对我们的含义 |
|---|---|---|
| 字面理解；间接、多跳推理 | 官方 jaggedness 第 1、4 条 [d-jag] | 「经外键间接相关」这类判断要拆成直接问题；把 FK 连接单独交给 Noul 的做法是对的 |
| 数学、计数、日期比较 | 官方第 2、3 条 [d-jag]；但 Bonn 发现 Jev 做 MMLU 计算题反而更准（94% vs 91%）[p-bonn] | 数值、日期过滤条件对应哪一列可以让 Jev 判断，比较本身不要交给它 |
| state 大且多为无关内容（context rot） | 官方第 5 条 [d-jag] | 步骤 2 的 state 里放了全库紧凑 schema，值得做一个消融：只放本表加 FK 邻接表 |
| 独立提问无法比较 | ai2sql：56 张表逐表单独问 Noul，必需表的最差排名中位数 22.5（最差 46）；改成一个 56 选项的 Choice 降到 3；Noul 但在 state 里放全表名单也降到 3 [blog-ai2sql] | 比较性问题必须带比较性上下文。我们的 state 已含 `schema_summary`。Choice 的概率会被多个必需表分摊（各约 0.3），只能取 top-k，不能用 0.5 阈值 [blog-ai2sql] |
| 多标签 Noul 过度预测 yes | Bonn：平均 P(yes) 0.209，实际 0.041，ECE 0.168；per-label AUROC 仍有 0.873（GoEmotions）和 0.994（UNFAIR-ToS）；调出的阈值中位数 0.86 [p-bonn]。本组表级实测没有复现：平均 P(yes) 0.286，实际 0.284，ECE 0.025，AUROC 0.992 [本组实测] | 表级不要调高阈值；降到 0.1 后 gold 表全保留率从 85.3% 升到 95.4% [本组实测]。差异原因可能是我们的正例率高得多（0.284 对 0.041），且 state 里带了全库 schema 作比较上下文（推断）。列级 Score 不是概率，是否高估待测 |
| 单个 Noul 略欠自信 | 平均 P(yes) 0.465，实际 0.518，ECE 0.052；Prompt Injection 精确率为 1 但召回只有 50%，AUROC 0.982 [p-bonn] | 排序强、阈值位置差：先看 AUROC/PR-AUC，再定阈值 |
| Choice 选项顺序偏置 | 官方说有时偏向第一个选项 [d-jag]；ai2sql 在 12 个 SQL 方言片段上没测出偏置 [blog-ai2sql] | 评测时打乱一次顺序做检查 |
| 选项命名与极性 | 把 yes/no 名称与 rubric 对调后，Jev 每 100 题翻 50.5 个，AUROC 从 0.89 跌到 0.31；4 个开源微调 checkpoint 最多翻 6.5 个 [p-gates] | Noul 的 true/false criteria 不能与 instructions 语义相反；Choice 的 key 用中性 ID（a/b/c），语义写在 criteria 里 |
| 对抗内容 | JevAdvBench：黑盒攻击翻转 Jev 46.3% 的正确判定，同请求重跑只翻 0.17%；8 个开源 TDM 被翻 34.0%–51.7% [p-adv] | 列描述和样本值来自数据库，属于不可信输入；论文里不要声称判定不可被干扰 |

### 1.4 逐条核实 ChatGPT 对话里的说法

| 说法 | 结论 | 依据 |
|---|---|---|
| 独立论文《Evaluating and Benchmarking the System One Model Jev》（2026-09-29） | 属实。arXiv 2609.37647，v1 作者 3 人，v2（2026-10-29）作者 4 人，v2 增加了 ALFWorld、TextWorld 等序列决策实验 | [p-bonn][p-bonn-v2] |
| 37 个数据集、346,009 个请求、Jev 1.13.0 | 属实。主评测共 217.9M input tokens，花费 $9.15，整个研究 $9.40 | [p-bonn] |
| 总体 Choice ECE ≈ 0.028 | 属实，但要加限定：这是 22 个 Choice 数据集、279,925 个答案的 pooled 值；Belebele、SIB-200、IMDB 三个大数据集占 63%；逐数据集中位数也是 0.028，不加权平均是 0.061；22 个里有 20 个是过度自信 | [p-bonn] |
| accuracy 与 ECE 的 Spearman ≈ −0.83 | 属实（ρ = −0.83），误校准集中在低准确率数据集，最差的是 Emotion（0.279）和 AfriXNLI（0.165） | [p-bonn] |
| 固定 0.5 的 Noul 阈值有偏差 | 属实。在 1,000 条训练样本上调阈值后，UNFAIR-ToS 的 micro-F1 从 0.499 升到 0.748 | [p-bonn] |
| Choice confidence 公式 C = (p_max − 1/n)/(1 − 1/n) | 属实，官方文档原文 | [d-conf] |
| 存在 1.14.0 preview | 已过时。1.14.0 在 2026-10-14 先以 `jev-preview` 发布，一周后 `jev-latest` 切过去；现在两个别名都指向 1.14.0 | [b-114][d-models] |
| 官方列出的局限是多跳、数学、日期、长而嘈杂的上下文 | 属实但不完整。官方 jaggedness 共 9 条，另外 5 条是字面理解、对抗内容、instructions 与 criteria 矛盾、Choice 选项顺序、生成 | [d-jag] |

Bonn 论文的开源对照组正是「读小 LLM 的 logprob」：Qwen3.8-27B 和 Gemma-4-E4B 读单 token 选项码的 next-token 概率。结果 Jev 在 27/37 个数据集上胜 Qwen（Qwen 领先的 9 个都在 bootstrap 区间内），37/37 胜 Gemma [p-bonn]。详见 §2.8。

### 1.5 对当前实验的直接建议

1. **钉版本。** 配置里的 `jev.model` 从 `typesafe-ai/jev` 改成 `typesafe-ai/jev-1.14.0`（照 AGENTS.md 复制新配置，不改旧的）。论文里写明版本和请求日期。
2. **表阈值往下调，不要往上调。** 本组实测表级 Noul 已经校准良好，0.5 → 0.1 让 gold 表全保留率从 85.3% 升到 95.4% [本组实测]，与 Bonn「调高阈值」的结论相反 [p-bonn]。但 0.1 是在 MINIDEV 上看到的，论文用的阈值要照 Bonn 的流程选：在 BIRD train 按库留出一部分，网格搜索（步长 0.01），再原样用到 MINIDEV [p-bonn]。目标设成「strict recall 固定时保留表/列最少」，不用 F1，因为下游更怕漏。
3. **列级多用排序。** 列级 Score 是期望等级，Score 在 1.0–1.5 的列有 38.6% 是 gold [本组实测]，「≥ 2.0」这类阈值会漏很多列。先画列级 reliability 图，再决定是用「每表 top-k 列 + 最低分下限」还是先把 Score 校准成概率（Platt 或 isotonic，见 §3.6）。
4. **加一组 listwise 对照。** 比较三种方式：逐列 Score；一个 Choice 覆盖整张表的列（≤255 个选项）；逐列 Noul 后在表内归一化。医疗场景的论文发现「逐选项二元打分再归一化」比直接 Choice 的保留准确率更高 [p-med]，ai2sql 的小实验结论方向相反 [blog-ai2sql]，值得在 BIRD 上测一次。
5. **选项命名中性化**，原因见 §1.3 的极性一行 [p-gates]。
6. **写明 Mini-Dev 版本。** BIRD 在 2026-10-28 发布了修正标注后的 Mini-Dev 更新版 [bird-home][hf-minidev]。本地 `data/minidev.zip` 内文件日期是 2024-06-20，是旧版。论文要写清楚用的是哪一版，最好两版都报。
7. **测一次可复现性。** 同一批请求跑两遍，报告阈值附近的翻转率。

---

## 2. 开源「jev-like」决策组件全景

### 2.1 按概率来源分类

| 机制 | 代表 | 概率怎么来 | 适合我们流程的哪一步 |
|---|---|---|---|
| A. 生成式 LLM + 受限 token（verbalizer） | Qwen3-Reranker 读 yes/no [hf-qwen3rr]；monoT5 读 true/false [p-monot5]；Bonn 的 Qwen/Gemma 读选项码 [p-bonn] | 在合法 token 上做 softmax 再重归一化 | 零训练的 Noul/Choice 替代品，需要温度校准 |
| B. 生成式 LLM + 专用决策 head | autotrust JEV-9B 的 24 槽位 head [hf-jev9b]；Cloudflare Clef 的 joint schema head [hf-clef]；EDU-Jev 的 pointer head [p-edujev] | 从 hidden state 直接输出每个选项的 logit | 最接近 Jev 的形态，适合多题型 |
| C. 编码器 cross-encoder 逐候选打分 | RESDSQL/CodeS schema filter [p-resdsql]；bge-reranker-v2-m3 [hf-bge]；Kenning（ModernBERT-NLI）[hf-kenning]；GLiClass [p-gliclass] | 每个 (state, 候选) 对出一个分数，题内 softmax 或逐项 sigmoid | 有监督 schema linking 的主力，便宜 |
| D. 标量 reward model（Bradley–Terry） | Skywork-Reward-V2、ArmoRM [p-skyv2][p-armo] | 一个无界标量，比较时取 sigmoid(r_A − r_B) | 适合给 SQL 候选排序，不适合直接对相关性设阈值 |
| E. 生成式 judge / verifier | Prometheus 2、Selene、GenRM [p-prom2][p-selene][p-genverif] | 先写理由再给分，概率只能读打分 token | 慢，适合离线造标签或给 SQL 候选打分 |

### 2.2 社区 Jev 复刻与 Decision Index

Jev Decision Index 0.4.1 是社区维护的榜单，2026-11-04 快照：110,201 个请求、42 个 benchmark、5 个大类，Jev 一侧跑的是托管 API 上的 jev-1.14.0 [hf-di]。Jev 1.14 的成绩：balanced skill 60.24，ECE 0.0799，ToolRet nDCG@10 0.6505，HTTP 延迟中位数 196 ms [hf-di]。榜单没有同行评审；它的方法说明称会把参赛者训练过的样本记为错，但泄漏风险仍然存在 [hf-di]。

| 模型（HF id） | 规模 / 基座 | 机制 | 训练标签来源 | DI skill | ToolRet | ECE | 许可 |
|---|---|---|---|---|---|---|---|
| Jev 1.14（托管 API，对照） | 未公开 | 未公开（RLCD） | 未公开 | 60.24 | 0.6505 | 0.080 | 闭源 |
| PelaAI/KnowLine-31B-Gen1 | 31.3B / Gemma-4-31B-it | LoRA | 未公开（未核实） | 71.86 | 0.694 | 0.032 | Apache-2.0 |
| fastino/GLiDE（v1.2） | 27.8B / Qwen3.8-27B | 全参微调 | 未公开 | 71.52 | 0.712 | 0.020 | other |
| perplexity-ai/pplx-decider-v1.2 | 26.1B / Qwen3.8-27B | 全参微调 | 大量来自 tasksource，其余未完全公开 | 67.98 | 未核实 | 0.055 | Apache-2.0 |
| Cloudflare/clef | 27.4B / Qwen3.8-27B | joint schema head | 未公开 | 54.92 | 0.691 | 0.040 | Apache-2.0 |
| Cloudflare/clef-flash | 9.4B / Qwen3.5-9B | joint schema head | 未公开 | 44.43 | 0.662 | 0.025 | Apache-2.0 |
| perplexity-ai/pplx-decider-flash-v1.2 | 4.5B / Qwen3.5-4B | 从 27B 蒸馏完整分布，model soup | 27B teacher 加 tasksource | 54.29 | 未核实 | 0.052 | Apache-2.0 |
| tattrongvu/Jeffan-4B-2010 | 4.2B / Qwen3.5-4B | 全参微调 | 未公开 | 52.21 | 0.737 | 0.039 | Apache-2.0 |
| OmniJev/OneJev1.6-0.8B | 0.85B / Qwen3.5-0.8B | 全参微调 | 未公开 | 29.58 | 0.685 | 0.035 | Apache-2.0 |
| internlm/Intern-Decision-4B | 4.5B / Qwen3.5-4B | 占位符前位置的 masked-next-token 读数，再做温度校准 | 未公开 | 38.69 | 0.528 | 0.028 | Apache-2.0 |
| autotrust/JEV-9B | 9B / Qwen3.5-9B（冻结） | LoRA r=16 + 24 槽位 head | 49.8 万行 Jev 1.13 输出分布（经 OpenRouter）+ Open-Jev 程序化标签 | 未核实 | 未核实 | 卡上自报 0.0007（对 teacher 分布） | Apache-2.0 |
| tasksource/tasksource-decider-nano | 0.15B / ettin-reranker-150m | 编码器，选项 hidden state 均值过线性头 | gold 标签（tasksource 等），训练前对 DI 0.3 做去重防火墙 | 19.85 | 未核实 | 0.070 | Apache-2.0 |
| knowledgator/gliclass-v4-edge | 0.44B / DeBERTa-v3-large | GLiClass，一次前向打所有标签 | 未公开 | 31.21 | 0.550 | 0.089 | Apache-2.0 |

DI skill、ToolRet、ECE 取自 [hf-di] 的 `data/index.json`；规模、基座、许可取自各模型 HF API 和模型卡 [hf-jev9b][hf-pplxflash][hf-pplx27][hf-clef][hf-intern][hf-tsnano][hf-gliclass4]。

规模规律（同一快照 [hf-di]）：
- 4B 级全参微调：第一梯队约 48–54，整体约 38–54；2B 级约 32–40；≤1B 编码器约 12–31。
- 不训练、只读 logits 的推理技巧：Gemma-4-31B（「Decider chat」）是 61.63，ECE 0.047，略高于 Jev；4B 级只有 23–43，ECE 在 0.046–0.371 之间乱跳。所以小模型直接读 logprob 一定要先校准。
- 在 ToolRet（对 32 个候选工具逐个打相关性，和逐列打分最像）上，Jeffan-4B（0.737）、OneJev1.6-4B（0.710）乃至 OneJev1.6-0.8B（0.685）都高于 Jev（0.6505）。

模型卡里值得借用的做法：
- **head 用 verbalizer 行初始化。** JEV-9B 把 head 初始化成基座 `lm_head` 里 false/true、0–5、A–P 对应的行，第 0 步的输出就等于基座的受限 next-token 分布（|Δp| < 1e-5）。没见过任何标签时 Choice 一致率已有 53%、Noul AUROC 0.82，蒸馏后到 90% 和 0.996 [hf-jev9b]。
- **可训练参数很少。** LoRA r=16 加 head 共 40.2M，占基座 0.5%，单张 B200 约 3 小时 [hf-jev9b]。pngwn 的 4B scorer 用 LoRA r=16 加标量头，30.5M 可训练参数，单张 A100 用了 2h58m [hf-pngwn]。
- **损失与增强。** JEV-9B 用 KL(target‖model)，Score 另加 0.5×RPS（ranked probability score）。30% 的样本打乱 Choice 选项，选项打乱后的 top-1 翻转率从 38% 降到 3.9% [hf-jev9b]。
- **判别适配与生成分开。** 把 System 1 LoRA 合并进基座后，HumanEval 从 70.7% 掉到 61.6% [hf-jev9b]。如果同一个模型以后还要生成 SQL，判别用的 adapter 要单独挂。
- **温度校准几乎是必做项。** pngwn：ECE 0.135 → 0.044（T=1.75）[hf-pngwn]；Kenning：0.040 → 0.006 [hf-kenning]；pplx-decider-flash：0.057 → 0.021 [hf-pplxflash]。
- **排序用 logit 差。** pplx 建议用 Noul 的 logit margin（true − false）排序，因为概率会饱和 [hf-pplxflash]。
- **请求粒度要测。** pplx-decider-flash 在多数多题任务上「一题一请求」更准，27B 版则适合合并 [hf-pplxflash]。
- **领域内有监督的小模型能超过通用 API。** EDU-Jev 4B（pointer head，单阶段软 CE）在 12 个教育 benchmark 上平均 67.6%，比 5 个商业 decision API 里最强的高 9.4 个点 [p-edujev]。
- **RLCD 不一定比 CE 好。** SBERT2S1 发现开源 RLCD 配方比交叉熵低 2.5–3.0 个点；做完温度缩放后，没有哪个目标在校准上明显更好 [p-sbert2s1]。

### 2.3 LLM judge / evaluator

| 模型 | 规模 / 许可 | 输出形式 | 备注 |
|---|---|---|---|
| Prometheus 2（prometheus-eval） | 7B、8x7B / Apache-2.0 [hf-prom] | 先写反馈，再给 1–5 分或 A/B | 支持自定义 rubric、direct assessment 与 pairwise 两种模式 [p-prom2] |
| JudgeLM（BAAI） | 7B、13B、33B / HF 卡未标许可 | 打分加理由 | 2023 年工作 [p-judgelm] |
| Auto-J（GAIR） | 13B / HF 卡未标许可 | pairwise 或单回答评估，附 critique | [p-autoj] |
| PandaLM | 7B / Apache-2.0 | pairwise 判断哪个更好 | [p-pandalm] |
| Skywork-Critic-Llama-3.1 | 8B、70B / other | pairwise 判断 | 2024-09 发布 [hf-ids] |
| Atla Selene 1 Mini / Selene 1 | 8B Apache-2.0；70B Llama-3.3 许可 | 打分加 critique | Mini 在 11 个 OOD benchmark 上总体超过其他小型 judge 和 GPT-4o-mini [p-selene][hf-ids] |
| CompassJudger-1 / -2 | 7B、32B / Apache-2.0 | 生成式评判 | -2 用 verifiable rewards 训练 [p-cj1][p-cj2] |
| Flow-Judge-v0.1 | 3.8B（Phi-3.5-mini）/ Apache-2.0 | rubric 打分加推理 | [hf-ids] |
| J1、JudgeLRM、RM-R1 | 见各论文 | 先推理再判断 | 用 RL（GRPO/RLVR）训练 judge [p-j1][p-judgelrm][p-rmr1] |

对我们的意义：这类模型的概率只能从打分 token 的 logits 间接读，而且每次都要先生成理由。MINIDEV 展开后有 38,428 个 question×column 对（本地统计，见 §4.1），逐个跑生成式 judge 不现实（推断）。适合的用途有两个：给训练数据打软标签，以及在少量 SQL 候选之间做 pairwise 评审。

### 2.4 Reward model 与生成式 verifier

| 模型 | 类型 | 规模 / 许可 | 要点 |
|---|---|---|---|
| Skywork-Reward-V2（Qwen3 0.6B/1.7B/4B/8B，Llama-3.1/3.2） | Bradley–Terry 标量 RM | 0.6B–8B；Qwen3 版 Apache-2.0，Llama-3.1-8B 版 llama3.1，Llama-3.2 版许可未核实 [hf-ids] | 8 个模型，用 SynPref-40M 里精选的 2,600 万对训练，作者称在 7 个 RM benchmark 上 SOTA [p-skyv2] |
| Skywork-Reward v0.2 | BT 标量 RM | 8B、27B | [p-skyv1] |
| ArmoRM-Llama3-8B | 多目标绝对评分 + MoE 门控 | 8B / llama3 | 每个维度可解释（如 honesty、verbosity、safety）[p-armo] |
| INF-ORM-Llama3.1-70B | 标量 ORM | 70B / HF 卡未标许可 | RM-R1 拿它作对照 [p-rmr1] |
| GenRM（Generative Verifiers） | 用 next-token prediction 训练 verifier | — | 可加 CoT 和多数投票；GSM8K 上 Best-of-N 从 73% 升到 93.4% [p-genverif] |
| Generative Reward Models（Mahan 等） | 生成式 RM | — | [p-genrm] |
| Qwen2.5-Math-PRM-7B/72B；Math-Shepherd | 过程奖励模型，逐步打分 | 7B、72B / other | Qwen 的经验：MC 估计合成的数据不如 LLM-as-judge 和人工标注 [p-prmlessons][p-mathshep][p-prm800k] |
| RewardBench / RewardBench 2 | 评测 | — | RewardBench 2 上模型平均比 RewardBench 1 低约 20 分 [p-rb1][p-rb2] |

对我们的意义：BT 标量分数没有上界，不同 prompt 之间不可比。用来判断「这一列是否相关」还要另训一个 sigmoid 映射。它更自然的用途是在 SQL 候选之间做选择，专门的 SQL ORM 见 §2.6。PRM 的逐步打分思路可以借来做「逐个 SQL 子句是否正确」的校验，目前我没找到现成的 SQL PRM（未核实）。

### 2.5 零样本分类器与 NLI cross-encoder

| 模型 | 规模 / 许可 | 概率形式 | 备注 |
|---|---|---|---|
| MoritzLaurer/deberta-v3-large-zeroshot-v2.0 | 0.44B / MIT | NLI entailment 与 not_entailment，按每个假设出概率 | 名字带 `-c` 的版本只用可商用数据训练 [hf-laurer][p-laurer] |
| MoritzLaurer/deberta-v3-base-zeroshot-v2.0 | 0.18B / MIT | 同上 | [hf-laurer] |
| MoritzLaurer/ModernBERT-large-zeroshot-v2.0；tasksource/ModernBERT-large-nli | 0.40B / Apache-2.0 | 同上 | ModernBERT 支持长上下文 [p-modernbert] |
| knowledgator/gliclass-large-v1.0、gliclass-modern-large-v2.0、gliclass-v4-edge | 0.40–0.44B / Apache-2.0 | 一次前向给所有标签打分；单标签 softmax，多标签 sigmoid；v4-edge 带校准分数 | v4-edge 自报每个决策延迟中位数 11 ms [hf-gliclass4][p-gliclass] |
| GLiNER / GLiNER2（fastino/gliner2-large-v1） | 0.29B / 0.49B，Apache-2.0 | span 抽取与分类 | 可替代目前用生成式 LLM 做的 mention 抽取（推断，未实测）[p-gliner][p-gliner2] |
| SetFit | 依所选句向量模型 | 对比学习微调句向量，再接分类头 | 少样本场景 [p-setfit] |
| 基础方法 | — | 用 entailment 做零样本分类 | [p-yin] |

两个对照点：
- 在 16 选项、人工 gold 标签的 `decision-models-under-pressure` 上，按 JEV-9B 卡转述的结果，DeBERTa-v3-large 零样本达到 Jev 准确率的 90%，GLiClass-large 是 81% [hf-jev9b]（原 benchmark 未核实）。
- 有任务标签时，微调过的 DeBERTa-v3-large 在 arXiv 2610.00346 的带标签 benchmark 上「除一个外全部最高」[p-gates]。BIRD train 有 9,428 条 [bird-home]，有监督 cross-encoder 是必须有的对照组。

### 2.6 Reranker / cross-encoder

| 模型 | 规模 / 许可 | 相关性概率怎么来 | 备注 |
|---|---|---|---|
| BAAI/bge-reranker-v2-m3 | 0.57B / Apache-2.0 | 单个 logit，`normalize=True` 时过 sigmoid 映射到 [0,1] | 多语言，基于 M3 [hf-bge][p-m3] |
| BAAI/bge-reranker-v2-gemma、v2-minicpm-layerwise、v2.5-gemma2-lightweight | 2.5B / 2.7B / 9.2B；v2.5 是 gemma 许可，其余 Apache-2.0 | LLM 式 reranker；layerwise 版可选截断层 | [hf-bge][hf-ids2] |
| Qwen/Qwen3-Reranker-0.6B/4B/8B | 0.6B / 4.0B / 8.2B，Apache-2.0 | system prompt 让模型「Judge whether the Document meets the requirements」，读 "yes"/"no" 两个 token 的 log-softmax，取 P(yes) | 支持自定义 instruction，官方称通常带来 1%–5% 提升 [hf-qwen3rr][p-qwen3emb] |
| mixedbread-ai/mxbai-rerank-base-v2 / large-v2 | 0.49B / 1.54B，Apache-2.0 | 三阶段训练：GRPO、对比学习、偏好学习 | [hf-mxbai] |
| jinaai/jina-reranker-v3（v3.5 于 2026-07 发布） | 0.6B / CC-BY-NC-4.0 | listwise，多个文档在同一上下文里联合编码 | 非商用许可 [p-jina3][hf-ids2] |
| Alibaba-NLP/gte-reranker-modernbert-base；zeroentropy/zerank-2-reranker | 0.15B；4.0B，均 Apache-2.0 | cross-encoder 打分 | [hf-ids2] |
| monoT5 / RankT5 / RankLLaMA | — | monoT5 读目标词 true/false 的 logit；RankT5 用 pairwise/listwise 损失，listwise 训练的 OOD 零样本更好；RankLLaMA 是 pointwise LLM reranker | [p-monot5][p-rankt5][p-rankllama] |

与 Jev 的直接对比：
- GEO-Bench 引用预测（10 个 answer engine）：Jev 和两个未训练的开源 27B decision model 的 Spearman 为 0.575–0.578；用 LoRA 在 teacher 标签上微调的 Qwen3-Reranker-4B 为 0.542–0.549，与 Jev 的差异在 229 个 query 上不显著；让 Jev 也一次只看一个段落后，剩下的差距基本消失 [p-geo]。
- 巴西葡语 IR：Jev 重排 BM25 top-1000，nDCG@10 从 0.333 升到 0.633 [p-ptbr]。官方 CLERC 法律检索 cookbook：top-1 从 5% 升到 18%，top-10 从 38% 升到 62% [d-rerank]。
- ToolRet：Jev 0.6505，Jeffan-4B 0.737 [hf-di]。

结论：「(问题, 列描述) → 相关性概率」本质上就是 reranking，Qwen3-Reranker 的 P(yes) 是现成的开源 Noul 替代品。

### 2.7 Text-to-SQL 专用的决策组件

**Schema linking / schema filter**

| 方法 | 形式 | 训练 / 许可 | 关键数字 |
|---|---|---|---|
| RESDSQL schema item classifier | RoBERTa-Large cross-encoder，BiLSTM 池化加 column-enhanced layer，表和列多任务分类 | 用 focal loss 处理正负例不平衡，换成 CE 会掉点；推理时保留 top-k1 表、每表 top-k2 列；按表 AUC 加列 AUC 选 checkpoint；代码 MIT | [p-resdsql][gh-resdsql] |
| CodeS schema filter（sic） | 沿用 RESDSQL 结构，提供 Spider、BIRD、BIRD+evidence 的训练脚本和 checkpoint | 代码 Apache-2.0；checkpoint `sic_ckpts.zip` 放在 Google Drive | [p-codes][gh-codes] |
| RUCKBReasoning/text2sql-schema-filter | XLM-RoBERTa-XL（3.5B）cross-encoder，中英双语 | 用 Spider、BIRD、CSpider train 训练；推理至少要 15GB 内存；GitHub 没有标许可 | [gh-tsfilter] |
| ExSL（IBM，抽取式 schema linking） | DeepSeek-Coder 6.7B decoder，在 schema 元素 token 的 hidden state 上对每个元素出概率，可选细粒度的列角色（select/join/group 等） | BCE，标签来自解析 gold SQL；BIRD 列级 ROC AUC 99.35、PR AUC 93.77，生成式 linker 是 94.15 和 69.18；比自回归生成式快 20 倍以上；权重是否公开未核实 | [p-exsl][gh-exsl] |
| CHESS Schema Selector | LLM agent 剪枝 | 作者称准确率提升约 2%，LLM token 用量降为 1/5 | [p-chess][gh-chess] |
| XiYan-SQL | 关键词抽取 → column retriever 和 LSH value retriever → LLM few-shot 列选择 | BIRD dev EX 75.63% | [p-xiyan] |
| RSL-SQL | 双向 schema linking、binary selection、多轮自我修正 | strict recall 94%，输入列数减少 83%；BIRD 67.2%（GPT-4o） | [p-rsl] |
| Schema-R1 | 生成式 linker：先 SFT 冷启动，再用基于规则奖励的 GRPO | filter accuracy 比已有方法高 10% | [p-schemar1][gh-schemar1] |
| Calibrated Bayesian schema subgraph（Richardson） | 层次贝叶斯：问题证据加 FK 图，输出「完整表集合」的概率 | 逐表独立打分会过度自信，它的预测集合覆盖率接近承诺值；让模型自己决定保留几张表时，全部收益来自学到的子集大小先验，FK 图没有额外贡献 | [p-bayes] |

其他 2025–2026 年的工作：双向检索，把「全 schema」与「完美 schema」之间的差距缩小 50% [p-bidir]；corpus-adaptive 微调一个 305M embedder，recall@10 从 60.4 升到 75.6 [p-embret]；EviLink 在 Spider2-Snow 上字段级 strict recall 93.04% [p-evilink]；CHS-SQL 用模型内部置信度做启发式搜索，平衡 precision 和 recall [p-chs]。

**SQL 候选选择与 verifier**

| 方法 | 形式 | 关键数字 |
|---|---|---|
| CHASE-SQL selector | 微调的二元候选比较 LLM，pairwise 投票 | [p-chase] |
| XiYan selection model | 微调模型在候选集中挑一个，对训练数据做改写以适配不同 SQL 风格 | [p-xiyan] |
| GradeSQL（ORM） | 自动生成候选，用执行结果打标签，训练 outcome reward model，做 Best-of-N | 比执行式 BoN 和多数投票高，BIRD 最多 +4.33%，Spider +2.10%；代码、数据、模型都已公开 [p-gradesql][p-gradesql-v1] |
| EvoSQL | generator 与 critic 协同进化，加 SDPO 微调 | BIRD-Dev 上比 Maj@16 高 1.37%（Qwen3-4B）到 9.19%（Qwen2.5-Coder-3B）[p-evosql] |
| DPC | 不训练：构造 Minimal Distinguishing Database，做确定性验证 | [p-dpc] |
| RL 生成器 | Reasoning-SQL（部分奖励）、SQL-R1、Arctic-Text2SQL-R1（只用执行正确性奖励）、ReViSQL（BIRD-Platinum 上的 RLVR） | [p-reasonsql][p-sqlr1][p-arctic][p-revisql] |

**必须正面回应的反证。** arXiv 2606.29733 在 BIRD dev（n=1534）上测了 Qwen2.5-Coder 7B/14B/32B、CodeLlama、Llama-3.1-8B 和 Llama-3.3-70B：schema linking 从来没有显著提升；词法 linking 让 Llama-3.3-70B 掉了 4.2 个点；table recall 更高的 embedding linker 与不做 linking 没有可测差异 [p-onprem]。OmniSQL 的成绩也来自单个 LLM，没有 schema linking、SQL revision 或 selection [gh-omnisql]。另一方面，ExSL 和双向检索的论文都报告了以召回为导向的 linking 有帮助 [p-exsl][p-bidir]。论文定位见 §4.4。

### 2.8 读小 LLM 的 logprob：最便宜的 Noul / Choice 替代品

Bonn 的做法 [p-bonn]：每题渲染成一个 chat prompt（state 的 JSON、指令、选项或等级），要求只回答选项码；每个选项分配一个单 token 码（A、B…，Noul 用 Yes/No，Score 用数字）；关闭 thinking，一次前向读 next-token 分布，限制到合法码上再重归一化。Qwen 和 Gemma 落在合法码上的概率质量中位数分别是 0.997 和 1.000，所以重归一化几乎不丢信息。

| 模型 | Choice pooled ECE | 单 Noul ECE（平均 P(yes) / 实际） | 多标签 Noul ECE |
|---|---|---|---|
| Jev 1.13 | 0.028 | 0.052（0.465 / 0.518） | 0.168 |
| Qwen3.8-27B 读 logprob | 0.063 | 0.113（平均 P(yes) 0.405，更欠自信） | 0.119 |
| Gemma-4-E4B 读 logprob | 0.208（平均最大概率 0.936，准确率 72.8%） | 0.123 | 0.161 |

数据来自 [p-bonn]。在 33 个含 Choice 或 Noul 的数据集上平均，Jev 是 0.074，Qwen 是 0.075，两者持平，Gemma 是 0.184 [p-bonn]。

其他证据：
- 4B 级不训练直接读 logits 的效果不稳（见 §2.2），31B 级能接近 Jev [hf-di]。
- 生成式模型「口头报概率」在回复不符合格式时就读不出来；读 key likelihood 能避开这个问题，但可能饱和 [p-gates]。
- 格式合适时，大模型在多选和判断题上校准不错，可以用 P(True) 做自评 [p-kadavath]。few-shot 有标签偏置，可以用 content-free 输入做 contextual calibration 修正 [p-calibrate]。
- Qwen3-Reranker 就是「受限 yes/no token」的产品化版本，同样基于 Qwen3 系列 [p-qwen3emb]。拿 Qwen3-4B 直接读 logprob 和 Qwen3-Reranker-4B 比，是一个干净的消融：基座同族，差别只在有没有做 reranking 训练（具体 checkpoint 对应关系是推断）。

实操建议：
1. 用 true 与 false 的 logit 差排序 [hf-pplxflash]。
2. 在按库切分的 dev 上用 temperature 或 Platt scaling 把它映射成概率 [p-guo][sk-cal]。
3. 确认 yes/no 是单 token、大小写一致，并检查落在合法码上的概率质量 [p-bonn]。
4. 选项码中性化，避免极性偏置 [p-gates]。

---

## 3. 决策模型的 finetune 方法

### 3.1 输出形式：分类头还是 token 概率

| 形式 | 例子 | 优点 | 缺点 |
|---|---|---|---|
| 编码器 + 分类头（pointwise） | RESDSQL、bge-reranker、DeBERTa-NLI | 快、便宜，有监督时很强 | 上下文短；多选项要多次前向 |
| decoder + verbalizer token | monoT5、Qwen3-Reranker、Bonn 的 logprob 读数 | 不改结构，零样本就能用，能用上 LLM 的知识 | 需要温度校准；有 token 偏置 |
| decoder + 专用 head（槽位、pointer、读 hidden state） | JEV-9B、Clef、EDU-Jev、ExSL | 一次前向答多题，输出类型安全 | 要写自定义推理代码 |
| decoder + seq-cls 标量头 | pngwn scorer、Skywork-Reward | transformers 原生支持 | 每个候选一次前向；分数要校准 |

对 schema linking，「对每个表/列候选打分，再在表内或库内归一化」最合适。ExSL 证明这种抽取式做法比生成式更快也更准 [p-exsl]。

### 3.2 参数高效微调

- LoRA：冻结基座，训练低秩增量 [p-lora]。QLoRA：在 4-bit 量化的基座上训练 LoRA，单张 48GB GPU 能微调 65B 模型 [p-qlora]。
- 可参考的超参数：JEV-9B 用 r=16、α=32、dropout 0.05，覆盖所有投影层，head 学习率 2e-4、LoRA 1e-4，cosine，warmup 3%，约 0.93 个 epoch [hf-jev9b]。pngwn 用 r=16，lr 1e-4，max_len 384，2,200 步 [hf-pngwn]。
- 长度：我们 exp1 平均每次调用 5,124 input tokens，但那是一次请求塞了约 45 题 [本地 logs/exp1.log]。pointwise 训练时每个样本只有「问题 + evidence + 紧凑 schema + 一个候选」，max_len 取 1024–2048 应该够（推断）。

### 3.3 损失函数

| 粒度 | 损失 | 什么时候用 |
|---|---|---|
| pointwise | BCE；focal loss | 表/列相关性，正例稀疏。RESDSQL 的消融显示 focal 比 CE 好 [p-resdsql][p-focal]；focal loss 对校准也有帮助 [p-focalcal] |
| pointwise 软标签 | KL(target‖model) 或 soft CE | 有 teacher 分布时（JEV-9B、EDU-Jev）[hf-jev9b][p-edujev] |
| 有序 | RPS（ranked probability score） | Score 类的分级相关性（irrelevant/weak/relevant/strong）[hf-jev9b] |
| pairwise | RankNet / Bradley–Terry：−log σ(s⁺ − s⁻) | 同一个问题里正列与负列比较；也用于 SQL 候选选择 [p-rankt5][p-skyv1] |
| listwise | ListNet / softmax CE；ListMLE | 一张表里所有列一起归一化；RankT5 发现 listwise 训练的 OOD 零样本更好 [p-rankt5] |
| 偏好 / RL | DPO；GRPO | 生成式 judge 或生成式 linker [p-dpo][p-grpo][p-schemar1] |

对 schema linking，我建议用 BCE（或 focal）加 λ·表内 listwise softmax CE。前者给出可以设阈值的绝对概率，后者直接优化「gold 列排在前面」，λ 在 dev 上调（推断）。

### 3.4 蒸馏：能做什么、不能做什么

- **不能做的：** 用 Jev 的输出训练模仿模型。TypeSafe MCA 第 2.3(b) 条原文：「use the Services or any Output … to perform model distillation, train a model to imitate the output of the Services, or develop (or to facilitate the development of) a similar or competing product or service」[t-mca]。通过 Vercel 调用时这份合同是否适用我没能核实；保守起见按适用处理。「开源一个 jev-like 模型」是否算「competing product」，投稿前请导师或学校法务确认。
- **社区的做法不能当先例。** autotrust 用 49.8 万行 Jev 1.13 输出（经 OpenRouter）训练了 JEV-9B/27B [hf-jev9b]。arXiv 2610.09188 报告只用 Jev 标签训练的 reranker 与用 Qwen3-32B 标签训练的一样好，标注只花了 21 分钟 API 调用，Qwen 要 5.1 GPU 小时 [p-distill]。技术上可行，不代表我们能这么做。这些数据和模型最多当评测对照，论文里要写明来源。
- **可以做的：** gold SQL 抽出的表/列标签；开源 LLM 的 logprob 当软标签，再与 gold 混合；多 judge 标签平均。在国际象棋任务上，两个 judge 的标签平均比把整个预算都给一个 judge 高 9.6 Elo（95% 区间 4.3–14.9）[p-distill]。噪声标签可以参考 JEV-9B 对占位标签乘 0.05 降权的做法 [hf-jev9b]。

### 3.5 DPO / GRPO 训练 judge 值不值得

- 对「先推理再判断」的生成式 judge，RL 有明显收益：J1、JudgeLRM、RM-R1、CompassJudger-2 都用了 RL 或 verifiable rewards [p-j1][p-judgelrm][p-rmr1][p-cj2]。Schema-R1 用 GRPO 训练 schema linker，filter accuracy 高 10% [p-schemar1]。
- 对单次前向、不输出文本的 jev-like 模型，RL 的必要性不明确：开源 RLCD 配方比 CE 低 2.5–3.0 个点，原因主要是奖励归一化把有噪声的 score-function 项放大了 3.6–15 倍 [p-sbert2s1]。
- 建议：第一版只做监督训练（BCE/listwise + 温度缩放），RL 留作消融。

### 3.6 校准方法与评测指标

| 方法 | 做法 | 备注 |
|---|---|---|
| Temperature scaling | 在 dev 上用 NLL 拟合一个标量 T，logit 除以 T | 不改变 argmax，最常用 [p-guo]；几乎所有开源复刻都用 [hf-jev9b][hf-pngwn][hf-intern] |
| Platt scaling | 对分数拟合 σ(a·s + b) | 适合 reranker 这类无界分数 [sk-cal] |
| Isotonic regression | 非参数的单调映射 | 需要较多 dev 样本，容易过拟合 [sk-cal] |
| Contextual calibration | 用 content-free 输入估计偏置后扣除 | 针对 prompt 方式的标签偏置 [p-calibrate] |
| 按题型分别设 T | Noul、Choice、Score 各一个温度 | JEV-9B 拟合出的 T 是 1.002 / 0.984 / 1.012 [hf-jev9b] |

| 指标 | 含义 | 用法 |
|---|---|---|
| ECE | 按置信度分箱，加权平均每箱的「置信度与准确率之差」的绝对值 | Bonn 用 15 个等宽箱，Choice 用最大选项概率，Noul 用 P(yes) [p-bonn] |
| Brier score | 概率与 0/1 结果之差的均方 | 同时反映区分度和校准 [p-bonn] |
| AUROC / PR-AUC | 排序质量，与阈值无关 | 正例少时 PR-AUC 更有信息量；RESDSQL 用 AUC 选 checkpoint，ExSL 报 PR-AUC [p-resdsql][p-exsl] |
| Risk–coverage / AURC | 按置信度排序后，覆盖率与错误率的曲线 | 对应「高置信自动处理、低置信交给人或大模型」[p-selective][p-bonn] |
| schema linking 专用 | strict recall（gold 表/列全部召回的题目比例）、平均保留列数、gold 最差排名、表/列 P/R/F1、EM | strict recall 是 RSL-SQL 的口径 [p-rsl]；最差排名是 ai2sql 的口径 [blog-ai2sql] |

### 3.7 用 BIRD/Spider train 构造 schema linking 训练数据

| 步骤 | 做法 | 依据 |
|---|---|---|
| 数据源 | BIRD train 9,428 条；官方过滤版 BIRD23-train-filtered 保留 6,601 条（约 70%）；BIRD-Platinum 是 2.5k 条经专家核验的样本，其中 61% 被改过；Spider train 7,000 条、140 个 schema | [bird-home][hf-birdfilt][p-revisql][p-exsl] |
| gold 抽取 | 解析 gold SQL 得到用到的表和列，直接复用评测代码里探查 gold SQL 的 SQLite authorizer 逻辑，保证训练标签与评测口径一致 | 本仓库 README；ExSL 也从 gold SQL 解析标签 [p-exsl] |
| 正例 | 被引用的表；SELECT/WHERE/JOIN/GROUP/ORDER 里出现的列，包括 JOIN 键；可选细粒度角色标签 | [p-exsl] |
| hard negatives | ① gold 表内没用到的列（最有价值）；② 同库的非 gold 表；③ 跨表同名列（`id`、`name`）；④ 与问题词面重叠高的列（BM25/embedding top-k）；⑤ FK 邻接但没用到的表 | 推断；思路与 [p-embret] 的 granularity-aware hard negatives 一致 |
| 输入格式 | 与 Jev 实验一致：state = 问题 + evidence + 紧凑 schema；候选 = 列名、所属表、类型、样本值、日期格式、列描述。这样对比的变量只有决策模型 | 本仓库 docs |
| 划分 | 按 `db_id` 把 BIRD train 切成 train/dev，库不重叠；dev 只用于早停、温度和阈值 | 推断 |
| 增强 | 打乱列顺序；轻度改写列描述；Choice 选项置换（JEV-9B：30% 置换让翻转率从 38% 降到 3.9%） | [hf-jev9b] |

泄漏提醒：MINIDEV 的 500 题来自 BIRD dev 的 11 个库（本地 `dev_tables.json` 统计），训练和调参都不能碰 BIRD dev。text2sql-schema-filter 的训练集含 Spider train，在 BIRD dev 上评测没问题，做 Spider dev 的跨数据集对照时要换掉它 [gh-tsfilter]。

---

## 4. 推荐

### 4.1 先跑哪 3 个开源 baseline

MINIDEV 规模（本地 `dev_tables.json` 与 `mini_dev_sqlite.json` 统计）：500 题、11 个库，每库平均 6.8 张表、72.5 列，最大的 european_football_2 有 199 列；展开后共 3,629 个 question×table 对、38,428 个 question×column 对。实验 2 实际跑了 498 题、3,613 个 question–table 对 [本组实测]。

| 优先级 | baseline | 回答审稿人的哪个问题 | 怎么跑 | 工作量（推断） | 推理成本（推断） |
|---|---|---|---|---|---|
| 1 | CodeS 的 schema item classifier（BIRD + evidence 的 checkpoint），即 RESDSQL 系 cross-encoder [gh-codes][p-resdsql] | 为什么不用专用的有监督 schema filter | 下载 `sic_ckpts.zip`，用 CodeS 的推理脚本给每题的表/列出概率，再接我们的 `evaluation.py` | 1–2 天，主要是数据格式转换 | RoBERTa-Large 级，单卡分钟级 |
| 2 | Qwen3-Reranker-4B 的 P(yes)；Qwen3-4B 直接读 yes/no logprob 作消融 [hf-qwen3rr] | 为什么不用通用 reranker 或开源 LLM | query = 问题 + evidence，document = 表或列的描述，instruction 写成 schema linking 任务；约 4.2 万对 | 半天到 1 天 | 4B，单卡几十分钟 |
| 3 | perplexity-ai/pplx-decider-flash-v1.2 [hf-pplxflash] | 为什么不用开源的 Jev 替代品 | 请求格式与 Jev 相同（state + questions，noul/choice/score），只换 `call_jev` 的传输层，pipeline 其余不动 | 半天 | 4.5B，约 9 GiB BF16 权重，单卡一小时内 |

三者分别代表「任务专用有监督」「通用 reranker」「通用 jev-like 决策模型」。成本对照：Jev 跑一遍 MINIDEV 约 $0.32（exp1 日志，§1.1）；开源 baseline 的成本主要是工程和 GPU 时间，不是钱。

备注：
- 备选：tasksource-decider-nano 训练标签全是 gold，来源透明，但 DI skill 只有 19.85，只能当下界 [hf-tsnano][hf-di]。Cloudflare clef-flash（9.4B）和 Intern-Decision-4B 可以补充第三类 [hf-clef][hf-intern]。生成式 judge 不适合逐列打分（§2.3）。
- 不要拿 autotrust JEV-* 当主 baseline：它是 Jev 1.13 的蒸馏，等于拿 Jev 和 Jev 比，还有 §3.4 的合同问题 [hf-jev9b]。
- pplx-decider-flash 的模型卡写着需要有权限的 HF token，但 HF API 返回 `gated=False`、`private=False`，下载前再确认。它的训练数据没有完全公开（大量来自 tasksource，其余未说明），论文里要写明 [hf-pplxflash]。

### 4.2 Finetune 配方：一个可信的开源 jev-like schema linker

目标：一个 4B 级开源模型，在 BIRD schema linking 上与零样本 Jev 和 §4.1 的 3 个 baseline 在同一协议下比较，并输出校准过的概率。训练不使用任何 Jev 输出（§3.4，[t-mca]）。

| 环节 | 配置 |
|---|---|
| 基座 | 主选 Qwen3-Reranker-4B：零样本就能用，又与 baseline 2 是同一个模型，增益可以直接归因于微调。备选 Qwen3.5-4B 加选项槽位 head（用 `lm_head` 的 verbalizer 行初始化 [hf-jev9b]），能同时支持 Noul/Choice/Score，更接近 Jev |
| 训练数据 | BIRD23-train-filtered（6,601 条）加 Spider train（7,000 条）[hf-birdfilt][p-exsl]；按 §3.7 抽 gold 表/列、配 hard negatives，正负比约 1:10 到 1:20（推断）；按 `db_id` 留出 10%–15% 的库作 dev |
| 样本组织 | pointwise：(state, 一个候选) → 0/1。listwise：同一张表的全部列（或同一个库的全部表）作为一组 |
| 输入 | 与 Jev 实验同一模板：问题 + evidence + 紧凑 schema + 候选详情（类型、样本值、日期格式、描述） |
| 损失 | L = BCE（或 focal，γ=2）+ λ·表内 listwise softmax CE；λ 在 {0.1, 0.5, 1} 里用 dev 选（§3.3） |
| PEFT | LoRA r=16、α=32、dropout 0.05，覆盖所有线性层；lr 1e-4（若有新 head 则 2e-4），cosine，warmup 3%，1–2 个 epoch；max_len 1024–2048 [hf-jev9b][hf-pngwn] |
| 增强 | 打乱列顺序；轻度改写列描述；若用 Choice 形式，30% 的样本做选项置换 [hf-jev9b] |
| 校准 | 在 dev 上拟合 temperature（每个题型一个）；再在 dev 上选「strict recall 达到目标时保留最少」的阈值或 top-k（§3.6） |
| 算力 | 几十万对、4B、LoRA，单张 A100/H100 一天内（推断）。参照：JEV-9B 约 3 个 B200 小时 [hf-jev9b]；pngwn 的 4B scorer 在 A100 上 2h58m [hf-pngwn] |

建议报的消融：零样本 vs 微调；BCE vs BCE + listwise；逐类去掉 hard negatives；有无温度缩放（报 ECE/Brier）；state 里有无全库 schema；只用 BIRD vs BIRD + Spider；有无开源 teacher 软标签。

### 4.3 评测协议

- **组件指标：** 表/列 P/R/F1、EM、strict recall、平均保留表数和列数、gold 最差排名、PR-AUC、ECE/Brier、risk–coverage（§3.6）。列级 Score 不是概率，另画 Score 区间 → gold 比例的 reliability 图。
- **阈值纪律：** 所有阈值和 top-k 只在 BIRD train 留出的库上选，MINIDEV 只跑一次。Jev 也走同样的选阈值流程，否则不公平。
- **评测集顺序：** MINIDEV（写明 2024 版还是 2026-10-28 修订版，以及实验 2 为什么是 498 题而不是 500 题）→ BIRD dev 全量（1,534 题）[p-xiyan] → Spider dev（跨数据集）→ 有时间再做大 schema 的 LiveSQLBench-Large（约 54 张表、约 1K 列）[bird-home]。
- **下游：** 固定一个开源生成器，如 OmniSQL-7B（Apache-2.0）[hf-ids2][gh-omnisql]，比较「全 schema / 各 linker / oracle schema」三类输入的 EX 和 prompt token 数。本组已用 qwen3.8-flash 做过一轮（2026-11-05，内部报告 §3.4）[本组实测]：全 schema 63.1、gold schema 71.6；Jev 三个实验在当前阈值下都低于全 schema（−4.4 到 −7.5 个点），列阈值放宽到 0.5 后与全 schema 打平。所以 linker 的阈值要按下游 EX 或 strict recall 选，不能按 F1 选，开源 baseline 也照此办理。
- **统计：** 500 题规模下报 95% bootstrap 置信区间，配对比较用 McNemar 检验，多重比较做 Holm 校正，做法同 [p-bonn][p-onprem]。
- **复现信息：** 版本化的模型 ID、请求日期、重复运行的翻转率（§1.5 第 7 条）。

### 4.4 论文定位与风险

| 风险 | 应对 |
|---|---|
| schema linking 对强生成器可能没用 [p-onprem][gh-omnisql] | 本组已经观察到（内部报告 §3.4）：mini-dev 上全 schema 是强基线，Jev 三个实验在当前阈值下 EX 都更低 [本组实测]。卖点不只放在 EX 上：同时报 token 节省、成本、延迟；在大 schema 上验证 [bird-home] |
| Jev 版本漂移 [d-models] | 用版本化 slug；写明测试日期；1.13 下线前保存全部原始响应 |
| 闭源零样本 vs 开源有监督，比较不公平 | 分两组报：零样本组（Jev、Qwen3-Reranker、pplx-decider）和有监督组（CodeS sic、我们的 LoRA） |
| 合同限制 [t-mca] | 训练只用 gold 和开源 teacher；论文里写明 Jev 只用于评测 |
| 许可 | jina-reranker-v3 是 CC-BY-NC-4.0 [hf-ids2]；text2sql-schema-filter 没有标许可 [gh-tsfilter]；学术用途前分别确认 |
| BIRD 标注噪声：BIRD-Platinum 改了 61% 的样本 [p-revisql] | 用过滤版训练；评测报两版 Mini-Dev |
| 语言 | Jev 英语最好 [d-models]；结论只限英文 schema 和问题 |

可以主张的贡献，从稳到险：
1. 第一个用 System One 决策模型做 schema linking 的系统评测：阈值、版本、校准、题型（Noul / Score / Choice）的影响。我们的表级 Noul 校准良好、与 Bonn 的多标签结论相反 [本组实测][p-bonn]，这本身就是一个可报告的发现。
2. 一个开源、校准过的 4B schema linker，在同一协议下与 Jev 和三类开源 baseline 比较。
3. 「逐项独立打分是否过度自信」在 schema linking 上的检验：Bonn 的多标签结果 [p-bonn]、Bayesian 子图论文 [p-bayes]、医疗论文的归一化结果 [p-med] 方向不一，可以在 BIRD 上统一测一次。

### 4.5 下一步清单

1. 主线程的 EX 跑完、API 并发空出来之后，用 `typesafe-ai/jev-1.14.0` 新建配置，在 MINIDEV 上重跑当前最佳配置，并测一次翻转率。
2. 写脚本从 BIRD/Spider train 抽 gold 表/列（复用 `evaluation.py` 的 SQLite authorizer 逻辑），按 `db_id` 切出 dev，先用它给 Jev 选阈值。
3. 按改动从小到大接 baseline：Qwen3-Reranker-4B → CodeS sic → pplx-decider-flash。
4. LoRA 训练：先 pointwise BCE，再加 listwise，跑 §4.2 的消融。
5. 下游 EX 已有第一版结果（内部报告 §3.4）。之后每个 linker 都用同一生成器、同一组 schema 输入跑 EX，并多次生成取平均。

---

## 参考来源

TypeSafe 文档页都从 `https://docs.typesafe.ai/llms-full.txt` 读了全文；arXiv 论文都经 arXiv API 读过摘要，标「全文」的还读了 HTML 或 PDF 正文；HF 模型卡和元数据经 HF API 读取。「状态」一栏为空表示已打开并核对过。

| 标签 | 来源 | URL | 状态 |
|---|---|---|---|
| [d-models] | TypeSafe 文档：Models | https://docs.typesafe.ai/models | |
| [d-changelog] | TypeSafe 文档：Model changelog | https://docs.typesafe.ai/changelog | |
| [d-errors] | TypeSafe 文档：Request rules and errors | https://docs.typesafe.ai/api/errors | |
| [d-conf] | TypeSafe 文档：Confidence | https://docs.typesafe.ai/confidence | |
| [d-score] | TypeSafe 文档：Score | https://docs.typesafe.ai/primitives/score | |
| [d-choice] | TypeSafe 文档：Choice | https://docs.typesafe.ai/primitives/choice | |
| [d-noul] | TypeSafe 文档：Noul | https://docs.typesafe.ai/primitives/noul | |
| [d-jag] | TypeSafe 文档：Model jaggedness | https://docs.typesafe.ai/model-jaggedness | |
| [d-primer] | TypeSafe 文档：AI primer（RLCD） | https://docs.typesafe.ai/introduction/machine-learning-primer | |
| [d-parallel] | TypeSafe cookbook：Parallel questions | https://docs.typesafe.ai/cookbooks/parallel_questions | 数字取自官方 `llms.txt` 的摘要，正文未细读 |
| [d-rerank] | TypeSafe cookbook：Re-ranking（CLERC） | https://docs.typesafe.ai/cookbooks/rerank_typesafe | 同上 |
| [b-intro] | TypeSafe 博客：Introducing System One Models & Jev（2026-09-15） | https://typesafe.ai/blog/introducing-system-one-models-and-jev | |
| [b-114] | TypeSafe 博客：Jev 1.14（2026-10-14） | https://typesafe.ai/blog/jev-1-14 | |
| [t-mca] | TypeSafe Master Customer Agreement（2026-09-23 版）第 2.3(b) 条 | https://typesafe.ai/legal/mca | |
| [v-model] | Vercel AI Gateway 模型页：Jev | https://vercel.com/ai-gateway/models/jev | |
| [v-decide] | Vercel 文档：Decision（原名 evaluation） | https://vercel.com/docs/ai-gateway/capabilities/evaluation | |
| [v-kb] | Vercel KB：Jev 与 AI SDK | https://vercel.com/kb/guide/typesafe-jev-and-ai-sdk | |
| [v-cl1] | Vercel changelog：Jev 上线 AI Gateway（2026-09-16） | https://vercel.com/changelog/typesafe-ai-jev-now-available-on-ai-gateway | |
| [blog-ai2sql] | ai2sql 博客：Jev for SQL（2026-09-21） | https://builder.ai2sql.io/blog/jev-for-sql-use-cases | |
| [bird-home] | BIRD 官网（BIRD23-train-filtered、Mini-Dev 2026 更新、LiveSQLBench-Large） | https://bird-bench.github.io/ | |
| [hf-minidev] | HF 数据集 birdsql/bird_mini_dev（最后修改 2026-10-27） | https://huggingface.co/datasets/birdsql/bird_mini_dev | 只读了元数据 |
| [hf-birdfilt] | HF 数据集 birdsql/bird23-train-filtered | https://huggingface.co/datasets/birdsql/bird23-train-filtered | 只读了元数据；6,601 条出自 [bird-home] |
| [sk-cal] | scikit-learn：Probability calibration | https://scikit-learn.org/stable/modules/calibration.html | |
| [本组实测] | 协调者转达的主线程测量（2026-11-05，BIRD mini-dev，实验 2） | 无公开 URL | 我没有独立复算 |
| 本地文件 | `logs/exp1.log`；`data/minidev/MINIDEV/*.json`；`jev-sql/README.md` 与 `docs/` | `E:\2026\ppstudy\text2sql\jev\` | 只读 |
| [hf-di] | Jev Decision Index（Space，0.4.1 版，`data/index.json` 2026-11-04 快照） | https://huggingface.co/spaces/multimodalart/jev-decision-index | |
| [hf-jev9b] | autotrust/JEV-9B 模型卡 | https://huggingface.co/autotrust/JEV-9B | |
| [hf-pplxflash] | perplexity-ai/pplx-decider-flash-v1.2 模型卡 | https://huggingface.co/perplexity-ai/pplx-decider-flash-v1.2 | |
| [hf-pplx27] | perplexity-ai/pplx-decider-v1.2 模型卡 | https://huggingface.co/perplexity-ai/pplx-decider-v1.2 | |
| [hf-clef] | Cloudflare/clef-flash 与 Cloudflare/clef 模型卡 | https://huggingface.co/Cloudflare/clef-flash | |
| [hf-intern] | internlm/Intern-Decision-4B 模型卡 | https://huggingface.co/internlm/Intern-Decision-4B | |
| [hf-tsnano] | tasksource/tasksource-decider-nano 模型卡 | https://huggingface.co/tasksource/tasksource-decider-nano | |
| [hf-gliclass4] | knowledgator/gliclass-v4-edge 模型卡 | https://huggingface.co/knowledgator/gliclass-v4-edge | |
| [hf-kenning] | systemonedev/kenning-large-v0.6 模型卡 | https://huggingface.co/systemonedev/kenning-large-v0.6 | |
| [hf-pngwn] | pngwn/system-one-qwen3.5-4b-scorer 模型卡 | https://huggingface.co/pngwn/system-one-qwen3.5-4b-scorer | |
| [hf-prom] | prometheus-eval/prometheus-7b-v2.0 | https://huggingface.co/prometheus-eval/prometheus-7b-v2.0 | 只读了元数据 |
| [hf-laurer] | MoritzLaurer/deberta-v3-large-zeroshot-v2.0 模型卡 | https://huggingface.co/MoritzLaurer/deberta-v3-large-zeroshot-v2.0 | |
| [hf-bge] | BAAI/bge-reranker-v2-m3 模型卡 | https://huggingface.co/BAAI/bge-reranker-v2-m3 | |
| [hf-qwen3rr] | Qwen/Qwen3-Reranker-0.6B 模型卡（4B、8B 同族） | https://huggingface.co/Qwen/Qwen3-Reranker-0.6B | |
| [hf-mxbai] | mixedbread-ai/mxbai-rerank-large-v2 模型卡 | https://huggingface.co/mixedbread-ai/mxbai-rerank-large-v2 | |
| [hf-ids] | HF API 元数据批量查询：judge 与 RM（Prometheus 2、JudgeLM、Auto-J、PandaLM、Skywork-Critic/Reward、Selene、CompassJudger、Flow-Judge、ArmoRM、INF-ORM、Qwen2.5-Math-PRM 等）。§2.3、§2.4 的参数量和许可出自这里 | `https://huggingface.co/api/models/<repo_id>` | |
| [hf-ids2] | HF API 元数据批量查询：零样本分类器、reranker、Text-to-SQL 模型、Qwen3/Qwen3.5/Gemma-4 基座。§2.5、§2.6 的参数量和许可出自这里 | 同上 | |

| 标签 | 论文（arXiv；标「全文」的读了正文，其余读了摘要） | URL |
|---|---|---|
| [p-bonn] | Deußer 等：Evaluating and Benchmarking the System One Model Jev，v1（全文） | https://arxiv.org/abs/2609.37647v1 |
| [p-bonn-v2] | 同上，v2（2026-10-29，增加序列决策实验） | https://arxiv.org/abs/2609.37647v2 |
| [p-gates] | Rafe & Das：Benchmarking System One decision models against trained classifiers…（全文，读了选项命名一节） | https://arxiv.org/abs/2610.00346 |
| [p-adv] | JevAdvBench：黑盒攻击 RLCD 模型 | https://arxiv.org/abs/2609.31142 |
| [p-med] | How Reliable Are System-One Decisions in Medicine? | https://arxiv.org/abs/2610.18529 |
| [p-geo] | Predicting What Answer Engines Cite（GEO-Bench） | https://arxiv.org/abs/2610.38090 |
| [p-ptbr] | Jev-as-a-Judge for Brazilian Portuguese IR | https://arxiv.org/abs/2610.23346 |
| [p-distill] | From Probabilities to Decisions: Search and Multi-Teacher Distillation with Jev | https://arxiv.org/abs/2610.09188 |
| [p-edujev] | EDU-Jev：教育领域的 System-One 决策模型 | https://arxiv.org/abs/2610.41079 |
| [p-sbert2s1] | From Retrieval to Typed Decisions（SBERT2S1） | https://arxiv.org/abs/2610.02486 |
| [p-prom2] | Prometheus 2 | https://arxiv.org/abs/2405.01535 |
| [p-judgelm] | JudgeLM | https://arxiv.org/abs/2310.17631 |
| [p-autoj] | Generative Judge for Evaluating Alignment（Auto-J） | https://arxiv.org/abs/2310.05470 |
| [p-pandalm] | PandaLM | https://arxiv.org/abs/2306.05087 |
| [p-selene] | Atla Selene Mini | https://arxiv.org/abs/2501.17195 |
| [p-cj1] | CompassJudger-1 | https://arxiv.org/abs/2410.16256 |
| [p-cj2] | CompassJudger-2 | https://arxiv.org/abs/2507.09104 |
| [p-j1] | J1: Incentivizing Thinking in LLM-as-a-Judge via RL | https://arxiv.org/abs/2505.10320 |
| [p-judgelrm] | JudgeLRM: Large Reasoning Models as a Judge | https://arxiv.org/abs/2504.00050 |
| [p-rmr1] | RM-R1: Reward Modeling as Reasoning | https://arxiv.org/abs/2505.02387 |
| [p-skyv1] | Skywork-Reward: Bag of Tricks | https://arxiv.org/abs/2410.18451 |
| [p-skyv2] | Skywork-Reward-V2 | https://arxiv.org/abs/2507.01352 |
| [p-armo] | ArmoRM：多目标 RM + MoE | https://arxiv.org/abs/2406.12845 |
| [p-genverif] | Generative Verifiers: Reward Modeling as Next-Token Prediction | https://arxiv.org/abs/2408.15240 |
| [p-genrm] | Generative Reward Models（Mahan 等） | https://arxiv.org/abs/2410.12832 |
| [p-prmlessons] | The Lessons of Developing PRMs in Mathematical Reasoning | https://arxiv.org/abs/2501.07301 |
| [p-mathshep] | Math-Shepherd | https://arxiv.org/abs/2312.08935 |
| [p-prm800k] | Let's Verify Step by Step | https://arxiv.org/abs/2305.20050 |
| [p-rb1] | RewardBench | https://arxiv.org/abs/2403.13787 |
| [p-rb2] | RewardBench 2 | https://arxiv.org/abs/2506.01937 |
| [p-yin] | Yin 等：Benchmarking Zero-shot Text Classification（entailment 方法） | https://arxiv.org/abs/1909.00161 |
| [p-laurer] | Laurer 等：Building Efficient Universal Classifiers with NLI | https://arxiv.org/abs/2312.17543 |
| [p-gliclass] | GLiClass | https://arxiv.org/abs/2508.07662 |
| [p-gliner] | GLiNER | https://arxiv.org/abs/2311.08526 |
| [p-gliner2] | GLiNER2 | https://arxiv.org/abs/2507.18546 |
| [p-setfit] | SetFit: Efficient Few-Shot Learning Without Prompts | https://arxiv.org/abs/2209.11055 |
| [p-modernbert] | ModernBERT | https://arxiv.org/abs/2412.13663 |
| [p-m3] | M3-Embedding（BGE-M3） | https://arxiv.org/abs/2402.03216 |
| [p-qwen3emb] | Qwen3 Embedding（含 Qwen3-Reranker） | https://arxiv.org/abs/2506.05176 |
| [p-jina3] | jina-reranker-v3 | https://arxiv.org/abs/2509.25085 |
| [p-monot5] | Document Ranking with a Pretrained Seq2Seq Model（monoT5） | https://arxiv.org/abs/2003.06713 |
| [p-rankt5] | RankT5 | https://arxiv.org/abs/2210.10634 |
| [p-rankllama] | Fine-Tuning LLaMA for Multi-Stage Text Retrieval（RankLLaMA） | https://arxiv.org/abs/2310.08319 |

| [p-resdsql] | RESDSQL（全文，读了 PDF 前 6 页） | https://arxiv.org/abs/2302.05965 |
| [p-codes] | CodeS | https://arxiv.org/abs/2402.16347 |
| [p-exsl] | Extractive Schema Linking for Text-to-SQL（全文） | https://arxiv.org/abs/2501.17174 |
| [p-chess] | CHESS | https://arxiv.org/abs/2405.16755 |
| [p-xiyan] | A Preview of XiYan-SQL（全文） | https://arxiv.org/abs/2411.08599 |
| [p-rsl] | RSL-SQL | https://arxiv.org/abs/2411.00073 |
| [p-schemar1] | Schema-R1 | https://arxiv.org/abs/2506.11986 |
| [p-bayes] | Calibrated Bayesian Selection of Schema Subgraphs | https://arxiv.org/abs/2609.20294 |
| [p-bidir] | Rethinking Schema Linking: Context-Aware Bidirectional Retrieval | https://arxiv.org/abs/2510.14296 |
| [p-embret] | Finding the Right Tables and Columns（schema retrieval benchmark） | https://arxiv.org/abs/2607.13311 |
| [p-evilink] | EviLink | https://arxiv.org/abs/2605.29670 |
| [p-chs] | CHS-SQL | https://arxiv.org/abs/2607.22624 |
| [p-onprem] | How far do on-premises open LLMs get on Text-to-SQL? | https://arxiv.org/abs/2606.29733 |
| [p-chase] | CHASE-SQL | https://arxiv.org/abs/2410.01943 |
| [p-gradesql] | Test-Time Verification for Text-to-SQL via ORMs（GradeSQL） | https://arxiv.org/abs/2606.30851 |
| [p-gradesql-v1] | GradeSQL（同作者的早期版本） | https://arxiv.org/abs/2509.01308 |
| [p-evosql] | EvoSQL | https://arxiv.org/abs/2607.20489 |
| [p-dpc] | DPC: Training-Free Candidate Selection | https://arxiv.org/abs/2604.15163 |
| [p-reasonsql] | Reasoning-SQL | https://arxiv.org/abs/2503.23157 |
| [p-sqlr1] | SQL-R1 | https://arxiv.org/abs/2504.08600 |
| [p-arctic] | Arctic-Text2SQL-R1 | https://arxiv.org/abs/2505.20315 |
| [p-revisql] | Human-Level Text-to-SQL via RL on Verified Data（ReViSQL，BIRD-Platinum） | https://arxiv.org/abs/2603.20004 |
| [p-guo] | On Calibration of Modern Neural Networks | https://arxiv.org/abs/1706.04599 |
| [p-kadavath] | Language Models (Mostly) Know What They Know | https://arxiv.org/abs/2207.05221 |
| [p-calibrate] | Calibrate Before Use | https://arxiv.org/abs/2102.09690 |
| [p-selective] | Selective Classification for Deep Neural Networks | https://arxiv.org/abs/1705.08500 |
| [p-focal] | Focal Loss for Dense Object Detection | https://arxiv.org/abs/1708.02002 |
| [p-focalcal] | Calibrating Deep Neural Networks using Focal Loss | https://arxiv.org/abs/2002.09437 |
| [p-lora] | LoRA | https://arxiv.org/abs/2106.09685 |
| [p-qlora] | QLoRA | https://arxiv.org/abs/2305.14314 |
| [p-dpo] | DPO | https://arxiv.org/abs/2305.18290 |
| [p-grpo] | DeepSeekMath（提出 GRPO） | https://arxiv.org/abs/2402.03300 |
| [gh-resdsql] | RUCKBReasoning/RESDSQL（MIT） | https://github.com/RUCKBReasoning/RESDSQL |
| [gh-codes] | RUCKBReasoning/codes | https://github.com/RUCKBReasoning/codes |
| [gh-tsfilter] | RUCKBReasoning/text2sql-schema-filter | https://github.com/RUCKBReasoning/text2sql-schema-filter |
| [gh-exsl] | IBM/extractive-schema-linking（README 很简短） | https://github.com/IBM/extractive-schema-linking |
| [gh-chess] | ShayanTalaei/CHESS | https://github.com/ShayanTalaei/CHESS |
| [gh-omnisql] | RUCKBReasoning/OmniSQL | https://github.com/RUCKBReasoning/OmniSQL |
| [gh-schemar1] | hongWin/Schema-R1 | https://github.com/hongWin/Schema-R1 |
