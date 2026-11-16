# 跳出“选列”：基于本周数据的研究方向思考

- 日期：2026-11-05；2026-11-16 更新（§2 方向 A、§3、§4 填入上限实验结果）。配套组会报告 `2026-11-06_jev_schema_linking_report.md`。
- 本文的新数字来自已有记录的离线分析（脚本 `wzy/analysis/probe_ideas.py`，结果 `probe_ideas.json`），以及一次不调用 JEV 的 oracle 上限生成实验（§4 步骤 1，996 次生成调用，遵守 5 并发上限）。
- 所有数字都只在 BIRD mini-dev（496 道 gold 可执行题）、一个生成器（qwen3.8-flash）、单次生成上测得，且是事后分析。它们说明方向，不是结论。§4 写了怎么正式验证。

## 1. 先看清问题：schema linking 这个问题本身可能定义错了

师兄的三个实验，以及大部分 schema linking 文献，都在优化同一个目标：选出的列集合要尽量接近 gold 列集合（P/R/F1、EM）。本周的数据说明这个目标和下游 EX 不一致：

- 阈值越低，F1 越低，EX 越高（报告 §3.4）。
- gold schema 比全 schema 高 8.5 个点，但这 8.5 个点的来源很集中。按 JEV 的分数把题分成两类（见下表）：“干净”的题里，给 gold schema 和给全 schema 几乎一样（+0.9）；有混淆列的题里，gold schema 高 14.5 个点。两组之差的 95% CI 是 [+6.7, +20.5]。

| 题目类型（按 JEV 分数，用到了 gold，仅作诊断） | 题数 | 全 schema EX | gold schema EX | 差 |
|---|---:|---:|---:|---|
| 干净：没有任何非 gold 列的分数高过最弱的 gold 列 | 219 | 77.6 | 78.5 | +0.9 [−2.7, +4.6] |
| 混淆：有非 gold 列的分数高过最弱的 gold 列 | 255 | 52.2 | 66.7 | +14.5 [+8.6, +20.4] |
| 有 gold 列根本没被打分（表没选上） | 22 | 45.5 | 59.1 | +13.6 [+0.0, +27.3] |

- 把全 schema 生成失败、而 gold schema 成功的 62 题拿出来看，有 45 题是生成器用了一个**不该用的列**，只有 2 题是漏用了 gold 列（另有 9 题列都对但逻辑错，6 题执行报错）。这 45 题里一共用了 93 个“干扰列”：40 个与某个 gold 列**同名、只是在另一张表**，29 个名字在库里多张表出现，18 个是 gold 表里的其他列，只有 6 个来自完全无关的表。93 个里有 45 个是主键或外键列。
- 这些同名干扰列，JEV 也分不开：54 对“干扰列 vs 它顶替的 gold 列”里，76% 的干扰列得分不低于 gold 列，52% 两者相差不到 0.3；在列阈值 0.5 下，这些干扰列全部被保留。例如 q95 `client.district_id` 1.65 vs `account.district_id` 1.56；q422 `cards.multiverseid` 2.72 vs `foreign_data.multiverseid` 2.75。
- 这个现象很普遍：40% 的题，其 gold 用到的非键列名在库里不止一张表有。

**我的判断**：在“全 schema 放得进 prompt”的设定下，schema linking 的价值不在“减少噪声”，在“消解混淆”。无关列基本无害，生成器能自己忽略；同名、近义、跨表的“近邻列”才有害，而现有的打分式 linker（包括 JEV 的独立 Score）恰好最分不清它们。这能解释两件事：一是 RSL-SQL、KaSLA 都观察到漏列比多列更伤，二是 Death of Schema Linking 和 arXiv 2606.29733 都发现 schema linking 对强模型没用（见 §5）。因为大部分题是“干净”的，砍掉无关列没收益，而“混淆”的题，按列独立打分又分不开。

这个视角下，问题就从“选哪些列”变成了“在一组近邻列里，问题指的是哪一个”。后者恰好是 Choice 的强项：一组封闭、互斥的选项里选一个，且输出可校准。现在的流程只用了 Noul 和 Score，Choice 一次都没用。

## 2. 三个可以做成论文的方向

### 方向 A（主推）：混淆感知的 schema 构造，从“过滤”改为“消歧”

**核心想法**：不再逐列打独立分、再按阈值过滤，改成三步：

1. **召回**：宽松阈值保召回。2b 记录上，列阈值 0.5 时 92.1% 的题保住了全部 gold 列。
2. **找混淆组**：在保留的列里，用确定性规则找出“近邻组”：同名跨表列、名字/描述相近的列、外键两端的同名键。这一步不需要模型，代价是零。
3. **组内消歧**：对每个混淆组问 JEV 一个 Choice：“这个问题里的『区』指的是哪张表的 district_id？”，选项包括组内各列和“都需要”。然后在 prompt 里只保留赢家，或者保留全部、但给生成器加一行明确的消歧提示。

**为什么它能成立（已有证据）**：

- 有“同名、分数接近”混淆对的题（gold-free 判据，推理时可算）占 193 题，这批题上全 schema 50.3、阈值 0.5 的 linked schema 53.9、gold schema 65.8，还有 12 个点的空间。没有混淆对的 303 题，三者是 71.3 / 69.6 / 75.2，空间很小。也就是说，可改进空间集中在一个推理时就能识别出来的子集上。
- 按非键同名列算，92 题全 schema 51.1 → gold 67.4（+16.3 [+6.5, +26.1]）。
- **上限实验（§4 步骤 1，2026-11-16 完成）**：在 2b 列阈值 0.5 的 linked schema 上，只对同名跨表组（≥2 张表共用一个列名）用 gold 做 oracle 消歧、其余不动，EX 65.9（平均 7.5 列），比它自己的基线 63.5 高 +2.4 [0.0, +5.0]，比全 schema 63.1 高 +2.8 [−0.6, +6.5]。把同一种消歧做在**全 schema** 上：EX **69.0**（平均 57.6 列），比全 schema 高 **+5.8 [+2.8, +9.1]**（gold schema 71.6，比全 schema 高 +8.5 [+5.0, +11.9]）。即：oracle 同名消歧在全 schema 上就收回了 gold schema 增益的约 2/3；做在 linked schema 上时，召回损失抵掉了这份收益（详见 §4 步骤 1 结果）。
- **现实消歧（§4 步骤 2，同日完成）**：JEV 的组级 Choice 本身很准（不丢 gold 列 93.1%，置信度 AUROC 0.86），但把它接进全 schema 后 EX 只 +0.4（oracle 收益回收 7%）。原因是 JEV 在可修题上保留了生成器同样用错的同名列——瓶颈在“该看哪张表”，不在“同名列里挑哪一个”。详见 §4 步骤 2 结果。
- 我检索到的工作里，KaSLA 用背包优化控制“冗余”，RSL-SQL 和 Robust/Extractive linking 追求高召回，AmbiQT 讨论的是“问题本身有两种合理 SQL”的歧义。我没有找到把“schema 内部的近邻列混淆”当作 linking 的核心对象、并用决策模型做组内消歧的工作（检索不完整，需要再做一轮系统检索）。

**它为什么是 JEV 的好场景**：组内选项少（2–5 个）、互斥、需要结合问题语义；Bonn 那篇评测和官方文档都说 Choice 的校准最好；而且每题只需要问几个 Choice，比现在逐列打分的问题数少一个数量级。

**风险**：

- 这些数字来自事后分组，“混淆题”的判定用到了 JEV 分数和 gold，可能有选择偏差。正式实验必须只用 gold-free 判据。
- 同名干扰列里有 24/40 是主外键列，这类往往两边都要（join 两端），“都需要”这个选项要设计好。
- 只在一个生成器上看到。强一点的生成器可能本来就分得清，空间会变小。
- 更强的直接证据：在 linked schema 上，“没有同名近似并列”的 303 题里，linked 比全 schema 还低 1.7 个点（69.6 vs 71.3）。说明一刀切的过滤即使在“干净”的题上也有小代价，方向 A 应该只对检测到混淆的题动手，其余题直接用全 schema 或宽松 schema。

### 方向 B：从“选一个阈值”到“按题分配 schema 预算”（有召回保证）

**核心想法**：全局阈值对每道题一视同仁。改成按题决定保留多少：先把 JEV 的分数校准成“这一列是 gold 的概率”，再按“全 gold 列都被保留的概率 ≥ 1−α”选最小集合，用 conformal risk control 给出有限样本保证。

**已有证据（偏弱）**：

- 2b 记录上，全局阈值从 2.0 降到 0.5，平均列数 5.9 → 8.9，全召回率 71.2% → 92.1%。
- 我在 2b 记录上试的两种按题规则（相对最高分的比例、最大分数断崖），在同样平均列数下都**没有**赢过全局阈值。按题自适应并不是免费午餐。
- 能预测“这道题会漏列”的现成信号很弱，最好的 AUROC 只有 0.61–0.63。把“高风险题”改用全 schema，在阈值 0.5 的设置下 EX 不升反降。

**判断**：值得做，但它是一个扎实的“方法 + 理论保证”工作，难点在于把分数校准好，而不是规则本身。它和方向 A 互补：B 管召回，A 管混淆。另外要注意 RTS（arXiv 2501.10858）已经把 conformal 方法用在 schema linking 上（在 LLM 隐层上做分支点预测），要在定位上区分开。贝叶斯子图那篇（arXiv 2609.20294）也已经指出“逐表独立打分会过度自信”，并且发现收益主要来自学到的“保留多少张表”的先验。

### 方向 C：选择性生成，配合级联与拒答

**核心想法**：同一道题既用全 schema、也用 linked schema 生成两条 SQL，让 JEV 当裁判二选一，或在两者都不可信时拒答 / 升级到更强模型。

**已有证据**：全 schema 与 linked schema（阈值 0.5）两条 SQL 中只有一条对的题有 60 道（只有全 schema 对 29 道，只有 linked 对 31 道），“每题挑对的那条”的上限是 69.4，比任一条单独高约 6 个点。这是一个现成、可量化的空间。另外，JEV 与 qwen 选列的分歧列里，各自独有的列分别有 43% 和 51% 是 gold，两者能力相当、错得不同，适合互相校验。

**判断**：上限可观，但 GPT 对话已经指出，候选选择这个方向很拥挤（MCS-SQL、CHASE-SQL 等）。只有把它做成“风险—覆盖率 + 成本的帕累托”，并且 JEV 的裁判比 LLM 自评更可靠，才有新意。建议作为方向 A 的下游组件，不单独成文。

## 3. 一个必须正视的反面事实

在 mini-dev 这种小 schema 上，全 schema 是很强的基线，几篇独立工作都得到相近结论（Death of Schema Linking；arXiv 2606.29733：BIRD dev 上 schema linking 对各开源模型都没有显著提升）。所以：

- 如果论文的故事是“JEV 让 schema linking 更准”，审稿人会问：既然 linking 本身都没用，更准又有什么意义？
- 方向 A 的故事是另一回事：linking 之所以没用，是因为大家在优化错的目标。我们指出真正起作用的是“混淆消解”，并给出一种做法，让 schema 处理第一次稳定超过全 schema。这既回应了“linking 已死”，又给了 JEV 一个不可替代的位置。
- 但方向 A 能否成立，取决于 §4 的第一个实验：消歧之后，EX 能不能**显著超过**全 schema。上限实验已完成：按原计划的“linked schema + 消歧”，**没有**达到事先约定的门槛（+2.8 [−0.6, +6.5]）；但同一消歧做在全 schema 上**显著**超过全 schema（+5.8 [+2.8, +9.1]）。机制是对的，形态要改：不是“linked schema + 消歧”，而是“全 schema（或高召回 schema）+ 消歧”。
- 步骤 2（真实 Choice）随后也完成了：JEV 的组内选择很准（不丢 gold 列 93.1%，置信度 AUROC 0.86/0.905），但下游 EX 只 +0.4 [−2.6, +3.2]，oracle 收益回收约 7%。**方向 A 现在的形态不成立**。诊断指向“该看哪张表”而不是“同名列里挑哪一个”（JEV 与生成器错在同一处），因此下一步（步骤 2′）要把表相关性/join 结构放进 Choice 的条件里；若仍无效，方向 A 只保留诊断价值，主攻 B/C。

## 4. 建议的验证顺序（每一步都有明确的“继续/放弃”标准）

| 步骤 | 内容 | 成本 | 继续的标准 |
|---|---|---|---|
| 1 | 上限实验：在 2b 列阈值 0.5 的 linked schema 上，只对“同名混淆组”用 gold 做 oracle 消歧（只留 gold 列），其余不动，重新生成 SQL | 不用 JEV，996 次生成（498 题 × 2 变体），已完成 | **未过门槛**（linked@oracle 比全 schema +2.8 [−0.6, +6.5]）；但同消歧做在全 schema 上 +5.8 [+2.8, +9.1]，见下方结果 |
| 2 | 真实消歧：组内 Choice 由 JEV 回答（schema 档位按步骤 1 的结论选全 schema 或高召回档）；同时对比 qwen 回答同样的 Choice、以及不做消歧 | 每题几个 Choice，已完成 | **部分达标**：置信度 AUROC 0.860（不丢 gold 列）/ 0.905（精确）✓；但 oracle 收益只拿到 7%（后验 +0.4 vs oracle +5.8）✗，见下方结果 |
| 2′ | 诊断后重做：让 Choice 的条件从“问题需要哪个同名列”改为“哪张表与问题相关、该组是否随 join 一起需要”，或把表级 Noul 概率/join 路径放进 state 后再问组内选择 | 每题几个 Choice，约 1,000 次调用 | JEV 修好的题 ≥ oracle 可修题的 50%，且不破坏超过 30% 的 oracle 破坏题 |
| 3 | 泛化：换生成器（一个更强、一个开源）、去掉 evidence、换 BIRD dev 全量 1,534 题；阈值在 BIRD train 的留出库上选 | 中 | 结论在至少两个生成器上成立 |
| 4 | 方向 B 的召回保证、方向 C 的级联，作为增强部件接上 | 中 | 视前三步而定 |

步骤 1 已完成（2026-11-16）。它不调用 JEV，直接决定方向 A 的形态。生成调用遵守 5 并发上限。

### 步骤 1 结果（2026-11-16）

496 道 gold 可执行题，qwen3.8-flash 单次生成；两个变体共 996 次生成调用，峰值并发 5，0 失败。EX 与配对 bootstrap（2000 轮，95% CI）：

| 生成器看到的 schema | 平均列数 | EX | vs 全 schema | vs 自己的基线 |
|---|---:|---:|---|---|
| 全 schema | 76.9 | 63.1 | — | — |
| 全 schema + oracle 同名消歧 | 57.6 | **69.0** | **+5.8 [+2.8, +9.1]** | — |
| 2b 列阈值 0.5（基线） | 8.9 | 63.5 | +0.4 [−2.6, +3.4] | — |
| 2b 列阈值 0.5 + oracle 同名消歧 | 7.5 | 65.9 | +2.8 [−0.6, +6.5] | +2.4 [0.0, +5.0] |
| gold schema（参考上限） | 5.0 | 71.6 | +8.5 [+5.0, +11.9] | — |

要点：

- 事先约定的门槛（linked@oracle 比全 schema 高 ≥ 3 个点，且 CI 不含 0）**没有达到**：差 0.2 个点，CI 含 0。按原计划（linked schema + 消歧）推进应当止损。
- 但同一消歧做在**全 schema** 上显著有效（+5.8 [+2.8, +9.1]），拿回了 gold schema 增益（+8.5）的约 2/3。说明“同名跨表混淆”确实是全 schema 相对 gold schema 的主要亏损来源，机制成立。
- linked schema 的天花板被**召回**限制：全召回率 92.1%；漏列的那 7.9% 题 EX 只有 41.0，全召回题 68.1。消歧收益集中在这批题之外，抵不过漏列的损失。
- 因此方向 A 的形态改为：**在高召回 schema（或直接全 schema）上做消歧**；步骤 2 的 Choice 实验按这个设定跑。

实现细节（便于复现）：oracle 判定按“列名在 ≥2 张表中出现”分组；组内有 gold 成员时只留 gold 成员，整组没有 gold 成员时整组删除。这样不会删掉任何 gold 列（已验证：消歧导致的 gold 列新丢失数为 0）。代码在 `wzy/analysis/sql_ex.py`（`oracle_disambiguate`，`--variants ...@oracle`），结果在 `outputs/runs/sql_ex/`（`summary_oracle_all.json`、`tables_oracle_all.md`）。

### 步骤 2 结果（2026-11-16）

现实消歧：每题对每个同名组问 JEV 一个 Choice（选项为组内各列，外加 all / none），决策直接作用在全 schema 上；对照组为 qwen 回答同一批 Choice 问题。498 题、3,495 个组决策；JEV 调用 498 次（0 失败，约 $0.06），生成 1,494 次（3 个变体 × 498 题，5 并发，0 失败）。

决策质量（对照 gold，仅作评估，不参与推理）：

| 方法 | 组级精确 | 不丢 gold 列 | 置信度 AUROC（精确 / 不丢） | keep 全召回 | keep 平均列数 |
|---|---:|---:|---|---:|---:|
| JEV Choice | 82.6% | 93.1% | 0.905 / 0.860 | 62.0% | 58.6 |
| qwen（同题） | 83.1% | 90.1% | —（无置信度） | 55.8% | 58.1 |
| oracle（参考） | 100% | 100% | — | 100% | 57.7 |

JEV 说 “none” 的 2,446 个组里 97.1% 确实没有 gold 成员；两模型的选项一致率 84.8%；qwen 有 31 题输出解析失败（回退为整组保留）。直接取 argmax 时每题 gold 全召回只有 62.0%——平均列数（58.6）几乎等于 oracle（57.7），但删掉的列和 oracle 删的不是同一批。加置信度闸门（σ < τ 时不删、保留整组）后，τ=0.8 时全召回 93.0%、平均 63.6 列（τ=0.5 时 78.3% / 60.8）。

下游 EX（496 题可计分，配对 bootstrap 95% CI）：

| 生成器看到的 schema | 平均列数 | EX | vs 全 schema | vs oracle |
|---|---:|---:|---|---|
| 全 schema | 76.9 | 63.1 | — | — |
| 全 schema + oracle 消歧 | 57.6 | 69.0 | +5.8 [+2.8, +9.1] | — |
| 全 schema + JEV Choice（argmax） | 58.5 | 60.3 | −2.8 [−5.8, +0.2] | −8.7 [−11.9, −5.6] |
| 全 schema + JEV Choice（τ=0.8 闸门） | 63.6 | 63.5 | +0.4 [−2.6, +3.2] | −5.4 [−8.5, −2.4] |
| 全 schema + qwen（argmax） | 58.1 | 59.3 | −3.8 [−7.3, −0.6] | — |

**判定**：置信度 AUROC 过线（≥0.7 ✓），但 oracle 收益回收率只有 0.4 / 5.8 ≈ 7%，远低于约定的 50% ✗。方向 A 按“问 JEV 哪个同名列”的形态**不成立**；qwen 更差，说明问题不在 JEV，而在问法本身。

**诊断**：62 道 gold 可修题（全 schema 错、gold 对）里，oracle 消歧修好 37 道，JEV 闸门版修好 19 道、argmax 修好 16 道；同时闸门版弄坏 6/20、argmax 7/20 的 gold 破坏题。逐题看：39/62 道可修题的全 schema SQL 确实用了某个非 gold 的同名列；这些题上 JEV 与 oracle 决策不一致的组共 74 处，其中 **42 处 JEV 保留了生成器用错的那个同名列**。也就是说，JEV 与生成器犯了同一个错：都把问题措辞映射到“名字最像”的那张表的列，而 gold 列往往由“问题涉及哪张表 / 哪条 join”决定。置信度能区分答案对错（AUROC 0.86），但高置信决策恰好不是能改变 EX 的那批（可修题上 JEV keep 与 oracle keep 完全相同只有 7/62）。argmax 还会因删列引入执行报错（7.1% vs 闸门版 2.2%）。

**下一步（步骤 2′）**：瓶颈是“该看哪张表”。把表的判断放进 Choice 的条件里——选项描述附带该表与问题的相关性（表级 Noul 概率）、该组是否是一条 join 的两端；或先问一个“本题涉及哪些表”的 Choice，再只在相关表之间消歧。若 2′ 仍拿不到收益，方向 A 降级为诊断性发现（“同名混淆是主因，但逐组语义选择消解不了”），主攻方向 B/C。

实现细节：代码 `wzy/analysis/choice_disambig.py`（组检测、Choice 问法、qwen 对照、keep-set 策略），`wzy/analysis/sql_ex.py` 的 `full@choice-jev`、`full@choice-jev-t0.8`、`full@choice-qwen` 变体；结果在 `outputs/runs/choice_disambig/`（`jev.jsonl`、`qwen.jsonl`、`report.md`、`keep_*.json`）和 `outputs/runs/sql_ex/summary_choice_all.json`。

## 5. 参考

- The Death of Schema Linking? Text-to-SQL in the Age of Well-Reasoned Language Models：https://arxiv.org/abs/2408.07702
- How far do on-premises open LLMs get on Text-to-SQL?（schema linking 在 BIRD dev 上不显著）：https://arxiv.org/abs/2606.29733
- KaSLA：Knapsack Optimization-based Schema Linking：https://arxiv.org/abs/2502.12911
- RSL-SQL：Robust Schema Linking in Text-to-SQL Generation：https://arxiv.org/abs/2411.00073
- Extractive Schema Linking for Text-to-SQL：https://arxiv.org/abs/2501.17174
- RTS：Reliable Text-to-SQL with Adaptive Abstention（conformal 用于 schema linking）：https://arxiv.org/abs/2501.10858
- Calibrated Bayesian Selection of Schema Subgraphs：https://arxiv.org/abs/2609.20294
- AmbiQT：Benchmarking and Improving Text-to-SQL Generation under Ambiguity：https://arxiv.org/abs/2310.13659
- EviLink：Multi-Path Schema Linking with Uncertainty-Guided Evidence Acquisition：https://arxiv.org/abs/2605.29670
- Rethinking Schema Linking: A Context-Aware Bidirectional Retrieval Approach：https://arxiv.org/abs/2510.14296
- Evaluating and Benchmarking the System One Model Jev（Choice 校准）：https://arxiv.org/abs/2609.37647
