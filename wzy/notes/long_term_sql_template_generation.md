# SQL 模板化 / 骨架化生成调研，及结合 JEV 的端到端生成可行性

- 日期：2026-11-05。对应长期任务「调研 SQL 模板化生成方法（经典方法 + LLM），思考结合 JEV 进行端到端生成的可能性」。
- 本地数据：`data/minidev/MINIDEV/mini_dev_sqlite.json`，500 行，其中 qid 137、138 各重复一次，实际 498 道不同题；原始 500 行的难度分布 simple / moderate / challenging = 148 / 250 / 102。
- 统计脚本：`wzy/notes/scripts/skeleton_stats.py`（sqlglot 30.29.0，只打印、不写文件；默认按 question_id 去重，与主线配对 bootstrap 同用 498 题，难度 148 / 248 / 102；498/498 条 gold SQL 解析成功）。下文「本地统计」都出自该脚本，分母均为 498。
- 引用约定：带数字的结论都给出来源 URL；拿不到原文核对的标「未核实」。LB = 官方 leaderboard（BIRD https://bird-bench.github.io/ ，Spider https://yale-lily.github.io/spider ，均于 2026-11-05 抓取）。

## 0. 结论速览

1. **经典路线**：「草图 + 槽位分类 + 确定性拼装」在单表 WikiSQL 上已经饱和（SQLova+EG test EX 89.6，SeaD+EG 93.0，https://github.com/salesforce/WikiSQL ）。到了 Spider 的多表、嵌套 SQL，主流改为语法 AST 解码或 seq2seq + 约束解码（IRNet、RAT-SQL、PICARD、RESDSQL）。DB 社区的 NaLIR / ATHENA / Templar 一直是「离散解释 + 排序 + 确定性生成」，形态与 Jev 最接近。
2. **LLM 时代**：骨架 / IR 主要用作示例检索键（DAIL-SQL、PURPLE）、生成的中间步骤（RESDSQL、LEAF-SQL）或子句级监督粒度（CAPER、EXPO-SQL）。没有工作把「骨架 + 槽位」作为最终输出交给非生成式模型。BIRD 榜首 test EX 83.40（AskData，LB），走的都是「多候选生成 + 选择」路线。
3. **本地统计的核心发现**：mini-dev gold SQL 在子句级骨架（L0）上高度集中，top-10 覆盖 82.9%，96.0% 的 L0 骨架在 BIRD train 中出现过。完整匿名 SQL（L2）却是长尾：346 种里 304 种只出现一次，只有 49.2% 在 train 中出现过（挑战题 23.5%）。所以应做「子句骨架分类 + 组合式槽位填充 + 确定性编译」，不应做整句模板库。
4. **语法分层覆盖**（本地统计）：A 层扁平 SPJA（含 GROUP / ORDER / LIMIT）53.4%；加上 B 层（算术、IIF/CASE、日期/字符串函数）86.3%；再加 C 层（单个子查询）95.6%；剩余 D 层 4.4%。挑战题三档分别只有 26.5% / 75.5% / 92.2%。
5. **BIRD 的难点本身很模板化**（本地统计，§4.6–4.7）：27.1% 的题含算术。168 个算术表达式归一化后只有 51 种，top-10 覆盖 58.9%，最常见的是 `SUM(IIF(c,1,0))*100/COUNT(x)`（13.1%）。含算术的题有 91.1% 在 evidence 里给出了公式。算术应由代码从 evidence 解析，Jev 只做操作数和条件的绑定。
6. **决策数决定上限**（本地统计，§4.5）：每题中位 10 个内容决策（p90 17，挑战题中位 15），另有约 9 个全局子句开关。若各决策独立且准确率都是 0.98，整题全对率约 67%（0.95 时 37%，0.99 时 82%）。因此必须配 beam、执行过滤和置信度门控的 LLM 回退。
7. **推荐**：主线 P1「Jev-Sketch」（组合式槽位 + sqlglot 编译 + 校准后门控回退 LLM）；副线 P2「Jev 作为候选 SQL 选择 / 校验器」，2–3 天可出第一版结果。语法逐产生式解码（设计 B）不推荐。
8. **新颖性**：已有 Zenn 原型（Jev 判槽 + NetworkX + sqlglot，只在 TPC-H 上演示 3 个案例，没有准确率统计，https://zenn.dev/snaga/articles/2026-10-25-deterministic-text-to-sql-jev-ast ）和 SPC（LLM planner + 确定性编译，38 题 97.4% vs 55.3%，https://arxiv.org/abs/2608.16663 ）。截至今天没检索到 Jev 在 BIRD / Spider 上的公开评测。「BIRD 规模 + 槽位级校准 + 选择性预测 / 级联成本」是可发表的空白，但应采用 selective / cascade 叙事，不宜与 SOTA 硬拼 EX。
9. **不能预设「Jev 的离散决策比 LLM 准」**：本组 2026-11-05 在 mini-dev 上的结果是，qwen3.8-flash 一次调用选列再做外键补全，列 F1 87.3，与最好的 JEV 流程（实验3）持平，token 只有约 1/10（内部报告 `wzy/reports/2026-11-06_jev_schema_linking_report.md` §0、§6，非公开）。所以每个槽位都要与「LLM 在同一编译器下填同样的槽」逐一对比，论文卖点应放在校准后的概率、可调工作点、成本和与 LLM 的互补上，而不是单纯的准确率。

## 1. 背景：Jev 的能力边界与「模板化」的三种含义

### 1.1 与设计直接相关的 Jev 事实

| 事实 | 数值 / 原文要点 | 来源 |
|---|---|---|
| 输出类型与上限 | noul / choice / score；单个 choice 最多 255 个选项，第 256 个选项 API 返回 400 | Pydantic AI 集成文档 https://github.com/pydantic/pydantic-ai/blob/main/docs/models/typesafe.md |
| 上下文上限 | `jev-1.13`：state + 最长问题 32k token，state + 全部问题 64k；state 每个请求只计一次 | 同上 |
| 文档列出的弱项 | 按字面理解问题；不是计算器，计数不可靠，日期按文本读；「属性的属性」和多跳会掉点；无关上下文越多准确率越低；对抗文本能左右答案 | 同上 |
| 价格 | 输入 $0.042 / 1M token，输出不计费；实测 372,480 token 计费 $0.0156 | ai2sql 博客 https://builder.ai2sql.io/blog/jev-for-sql-use-cases |
| 延迟 | TypeSafe 称 70–500 ms / 次；ai2sql 实测单次中位 296 ms；我们批量几百个问题约 1–3 s / 次（本组调用）；当前 schema linking 全流程每题 3–5 s，498 题一次实验约 0.2–0.3 美元（内部报告 §0） | 同上；内部报告 |
| 校准（第三方） | 290 条 GitHub issue：0.8–1.0 档 123 条中 97% 为正；0.0–0.2 档也有 65% 为正（输入缺关键信息所致） | 同上 |
| 校准（本组实测） | 表级 Noul AUROC 0.992、ECE 0.025；列级 Score AUROC 0.984，但它不是概率（Score 1.0–1.5 的列有 38.6% 是 gold），需先做 isotonic 等校准 | 内部报告 §5 |
| 非确定性 | 第三方：同一输入重复 5 次，top 选项概率变化中位 0.02、最大 0.09，阈值附近会翻转。本组：同一请求两次运行，表 Noul 平均绝对差 0.0061（最大 0.21），列 Score 平均绝对差 0.021，在 2.0 阈值处翻转 0.46% | ai2sql 博客；内部报告 §3.3 |
| 比较式上下文 | 56 表 schema 选表，「最差必需表排名」中位数：1 个 choice 为 3；56 个独立 noul 为 22.5；独立 noul 在 state 中加入全部表名后为 3 | 同上 |

注：TypeSafe 官方文档本次未直接获取，弱项以 Pydantic AI 集成文档为准。搜索结果中的 jevapi.org、jevtypesafe.org、aijev.org 等域名疑似第三方仿站，未采用。

对当前 schema linking 的启发：「比较式问题需要比较式上下文」。我们的表级 Noul 在 state 里带了 `schema_summary`，相当于上表第三种做法；但列级 Score 和外键 Noul 是否同样受益，值得做一次很便宜的消融。

### 1.2 「模板化生成」的三种粒度

| 粒度 | 代表 | 输出空间 | 与 Jev choice 的契合度 |
|---|---|---|---|
| 整句模板库（template bank） | Finegan-Dollak 模板基线、工业 verified queries | 训练中见过的 SQL 模板 + 槽位 | 模板数可以塞进 choice，但无法泛化到新结构 |
| 草图 / 骨架（sketch / skeleton） | SQLNet、Coarse2Fine、RESDSQL、PURPLE、LEAF-SQL | 子句结构 + 类型化空洞 | 最契合：骨架分类（choice）+ 空洞填充（choice / noul） |
| 语法 / IR（AST、SemQL、NatSQL、QPL） | SyntaxSQLNet、IRNet、RAT-SQL、SmBoP | 产生式序列或简化 IR | 每一步都是选择，但步数多且相互依赖，不利于批量 |

## 2. 经典模板 / 草图 / 语法方法（2014–2023）

| 方法（会议） | 模板 / 草图 / IR 是什么 | 离散分类 vs 生成 | 报告结果 | 局限 |
|---|---|---|---|---|
| NaLIR（PVLDB 2014） | 依存树节点映射为 select / operator / function / quantifier / logic / name / value 等节点，调整成合法的 Query Tree 后确定性翻译成 SQL | 节点映射与结构调整都是候选排序；前 5 类节点用枚举短语识别；交互模块让用户确认解释 | 只有用户研究，没有标准 benchmark 数字 https://dbgroup.eecs.umich.edu/files/PVLDB14LF.pdf | 依赖 parser 和规则，需要用户交互 |
| ATHENA（PVLDB 2016） | 两阶段：NLQ → 本体查询语言 OQL → SQL；用 interpretation tree 表示候选解释并排序 | 证据映射 + 解释排序（离散），SQL 确定性生成 | GEO / MAS precision 100%，FIN 99%；recall 87.2 / 88.3 / 88.9% http://www.vldb.org/pvldb/vol9/p1209-saha.pdf | 需要人工构建领域本体 |
| ATHENA++（PVLDB 2020） | ATHENA 加嵌套查询检测（语言模式 + 本体推理），子 OQL 用连接条件组合 | 嵌套与否、内外查询切分都是离散判定 | FIBEN 88.33%，Spider dev 78.89% https://www.vldb.org/pvldb/vol13/p2747-sen.pdf | 同上 |
| Templar（ICDE 2019） | 从 SQL 日志构建 Query Fragment Graph，增强现有 NLIDB 的关键词映射和 join path 推断 | 映射与路径都是打分排序 | 现有 NLIDB top-1 准确率最多提升 138% https://arxiv.org/abs/1902.00031 | 需要该库的查询日志 |
| 模板基线（Finegan-Dollak, ACL 2018） | 从训练集自动抽 SQL 模板；预测用哪个模板，以及哪些词填哪个槽 | 纯分类 | question split 上不弱（Advising 80、Restaurants 95、GeoQuery 57）；query split 上全部为 0 https://arxiv.org/abs/1806.09029 | 完全无法泛化到新查询结构 |
| Seq2SQL（2017） | SQL 分为聚合 / SELECT 列 / WHERE 三部分 | 前两部分分类，WHERE 用指针生成 + RL | WikiSQL test EX 59.4，LF 48.3 https://arxiv.org/abs/1709.00103 | WHERE 部分的顺序问题 |
| SQLNet（2017） | 草图 `SELECT $AGG $COLUMN WHERE $COLUMN $OP $VALUE (AND ...)*`，带依赖图；$OP ∈ {=,<,>}；$VALUE 是问题子串 | 全部是槽位分类或抽取（seq2set + 列注意力），不用 seq2seq | WikiSQL test EX 68.0（README）https://arxiv.org/abs/1711.04436 | 只能处理 WikiSQL 语法 |
| TypeSQL（NAACL 2018） | 视为 slot filling，并注入 KB / DB 的类型信息 | 槽位分类 | README 中 test EX 73.5；论文称使用 DB 内容时 82.6% https://arxiv.org/abs/1804.09769 | 同上 |
| Coarse2Fine（ACL 2018） | 先生成粗略 sketch，再填入细节 | 两阶段生成 | WikiSQL test EX 78.5 / LF 71.7（README）https://arxiv.org/abs/1805.04793 | 错误会从第一阶段传递下去 |
| SQLova（2019） | BERT 表格上下文化 + 槽位分类；论文指出 BERT + seq2seq 效果差 | 槽位分类 | test EX 86.2，加执行引导（EG）89.6（README）https://arxiv.org/abs/1902.01069 | 单表 |
| SyntaxSQLNet（EMNLP 2018） | SQL 语法树解码；用栈调度 IUEN / KW / COL / OP / AGG / AND-OR / DESC-ASC-LIMIT / HAVING 等模块 | 每个模块都是分类，树逐步展开 | Spider test EM 19.7，加数据增强 27.2（LB）https://arxiv.org/abs/1810.05237 | 早期，准确率低 |
| IRNet（ACL 2019） | 先做 schema linking，再用语法模型生成 SemQL（去掉 JOIN ON / FROM / GROUP BY，合并 HAVING 与 WHERE），最后确定性推出 SQL | 产生式序列（语法解码） | Spider test EM 46.7，加 BERT 54.7（LB）https://arxiv.org/abs/1905.08205 | SemQL 只能还原 86.2% 的 Spider dev gold（NatSQL 论文 Table 3，https://arxiv.org/abs/2109.05153 ）；没有外键的 join 会出错 |
| RAT-SQL（ACL 2020） | relation-aware self-attention 统一编码 schema 与 linking；AST 解码 | 语法解码 | Spider test EM 57.2，加 BERT 65.6；加 GraPPa 69.6（LB）https://arxiv.org/abs/1911.04942 | 只报 EM，不预测值 |
| NatSQL（EMNLP Findings 2021） | 更简的 IR：去掉 GROUP BY / HAVING / FROM / JOIN ON，消除嵌套和集合运算 | 作为解码目标 | gold IR 在 Spider dev 上 EM / EX 93.3 / 95.3（加 FROM 版 96.2 / 96.5），SemQL 86.2；RATSQL+GAP+NatSQL test EX 73.3（LB）https://arxiv.org/abs/2109.05153 | 按 Spider 设计，有少量无法表达的 SQL |
| SmBoP（NAACL 2021） | 自底向上半自回归：第 t 步并行生成高度 ≤ t 的 top-K 子树 | 每步对子树打分排序 | Spider 指称准确率 71.1、EM 69.5；解码提速 2.2 倍 https://arxiv.org/abs/2010.12412 | 需要关系代数式语法 |
| BRIDGE（EMNLP Findings 2020） | 问题 + schema + 匹配到的单元格值组成一条序列；pointer-generator 解码，加 schema 一致性剪枝 | 生成 + 剪枝 | Spider dev 71.1 / test 67.5（集成）；WikiSQL test 91.9 https://arxiv.org/abs/2012.12627 | 值只来自字符串匹配 |
| PICARD（EMNLP 2021） | 增量解析约束解码，每一步拒绝不合法的 token | 生成 + 约束 | T5-3B+PICARD Spider dev EM / EX 75.5 / 79.3，test 71.9 / 75.1 https://arxiv.org/abs/2109.05093 | 只保证合法，不保证语义正确 |
| RESDSQL（AAAI 2023） | cross-encoder 给表 / 列排序后注入编码器；解码器先输出骨架（如 `select _ from _ where _`），再输出 SQL | 骨架由生成式解码器产出 | RESDSQL-3B+NatSQL Spider dev EM / EX 80.5 / 84.1，test 72.0 / 79.9 https://arxiv.org/abs/2302.05965 | 骨架仍是生成的，没有显式置信度 |
| QPL（EMNLP 2023） | 从 SQL Server 执行计划翻译出的模块化查询计划语言，树叶是 Scan，内部节点是一元 / 二元算子 | 作为解码目标 | Spider dev EX 77.4（无 DB 内容）/ 83.8（有 DB 内容）https://arxiv.org/abs/2310.13575 | 需要 SQL → QPL 转换器 |

经典方法给我们的四点经验：

- **「分类够不够」与 SQL 复杂度成反比**。WikiSQL 的单表 sketch 靠分类就能做到 90% 以上（https://github.com/salesforce/WikiSQL ），Spider 的嵌套和集合运算迫使大家改用语法或 IR 解码。BIRD mini-dev 结构上并不复杂（GROUP BY 7.6%、子查询 12.0%，见 §4.1），难点在值、evidence 和脏数据（BIRD 论文 https://arxiv.org/abs/2305.03111 ），这对分类式方案是有利的。
- **IR 覆盖率就是上限**。SemQL 只能还原 86.2% 的 Spider dev gold，NatSQL 93.3–96.2%（https://arxiv.org/abs/2109.05153 Table 3）。我们的槽位语法必须先做 gold → 槽位 → SQL 的 round-trip，测执行一致率，不能只看 §4 的语法层统计。
- **模板法在 query split 上归零**（Finegan-Dollak）。论文评测必须单独报告「骨架在 train 中未出现」的子集。
- **NaLIR / ATHENA / Templar 本质上是「离散解释 + 排序 + 确定性生成」**，瓶颈在手写规则和本体。Jev 可以替换其中的打分和选择环节，保留确定性生成。

## 3. LLM 时代的骨架 / IR / 分解方法（2023–2026）

### 3.1 骨架 / IR / 分解类方法

EX 均为 BIRD（%）。「LB」表示数字取自 BIRD 官方榜，其余取自论文摘要或正文。

| 方法 | 骨架 / IR / 分解形式 | 离散决策 | BIRD dev / test EX | 来源 |
|---|---|---|---|---|
| DIN-SQL + GPT-4 | 把问题分为 easy / non-nested / nested 三类，按类别选 prompt；用 NatSQL 作中间表示；自纠错 | 三分类 | 50.72 / 55.90；Spider test 85.3 | https://arxiv.org/abs/2304.11015 |
| DAIL-SQL + GPT-4 | 选示例：对问题做 mask 后算 embedding 距离，并要求预生成 SQL 与示例 SQL 的骨架相似度超过阈值 | 检索 | 54.76 / 57.41（LB）；Spider test 86.6 | https://arxiv.org/abs/2308.15363 |
| PURPLE | schema 剪枝 → 预测 SQL 骨架 → 按四级自动机抽象检索示例 → 数据库适配 | 骨架预测 + 检索 | GPT-4o 62.97 / 64.51，+RED 68.12 / 70.21（LB）；Spider dev EM 80.5 / EX 87.8 | https://arxiv.org/abs/2403.20014 |
| ZeroNL2SQL | PLM 生成 sketch（SELECT 属性、FROM 表、所需关键词），LLM 补全细节；谓词校准 + 执行选择 | sketch 由 PLM 生成 | 摘要称比 PLM 方法高 3.2–13%、比 LLM 方法高 10–20%；BIRD 数字未核实 | https://arxiv.org/abs/2306.08891 |
| SQL-PaLM | few-shot 一致性解码 + 列选择 + 执行选择 | 选择 | dev：few-shot 45.05，微调 53.59，加执行选择 61.7；人工抽查 100 条「错例」有 31% 是评测误判 | https://arxiv.org/abs/2306.00739 |
| MAC-SQL + GPT-4 | Selector / Decomposer / Refiner 三个 agent | 无显式离散决策 | 57.56 / 59.59（LB） | https://arxiv.org/abs/2312.11242 |
| TA-SQL + GPT-4 | Task Alignment：schema linking 与逻辑合成两阶段都对齐相似任务 | 无 | 56.19 / 59.14（LB）；mini-dev（SQLite）TA + GPT-4o 63.00 | https://arxiv.org/abs/2405.15307 |
| PTD-SQL / DEA-SQL | 按查询类型分组做针对性练习 / 按问题类别组织 prompt | 题型分类 | PTD-SQL 未核实；DEA-SQL 称 BIRD dev 提升约 2–3 点 | https://arxiv.org/abs/2409.14082 ，https://arxiv.org/abs/2402.10671 |
| MCS-SQL + GPT-4 | 多 prompt schema linking → 多候选 → 按置信度过滤 → LLM 多选题选优 | 多选 | 63.36 / 65.45（LB） | https://arxiv.org/abs/2405.07467 |
| CHESS | IR / SS / CG / UT 四个 agent（值检索、schema 选择、候选生成、单元测试） | 选择 | IR+CG+UT 68.31 / 71.10（LB）；SS 让 token 减少约 5 倍 | https://arxiv.org/abs/2405.16755 |
| CHASE-SQL + Gemini | 分治 CoT、查询计划 CoT、实例级合成示例三种生成器 + 微调的成对选择器 | 成对选择 | 论文 73.01 / 73.0；LB 2025-04 条目 74.90 / 76.02 | https://arxiv.org/abs/2410.01943 |
| XiYan-SQL | M-Schema + ICL / SFT 多生成器集成 + refiner + 微调选择模型 | 选择 | 73.34 / 75.63（LB）；Spider test 89.65 | https://arxiv.org/abs/2411.08599 |
| OmniSQL-32B | 用合成的 SynSQL-2.5M（250 万条、1.6 万多个库）微调 | 无 | 69.23 / 72.05（LB） | https://arxiv.org/abs/2503.02240 |
| Alpha-SQL | MCTS，7 种动作：改写问题、选 schema、识别列值、识别列函数、生成 SQL、修订、终止 | 动作序列搜索 | 69.70 / 70.26（LB），零样本 32B 模型 | https://arxiv.org/abs/2502.17248 |
| MCTS-SQL / SQL-o1 | MCTS 引导多步修正 / 自奖励启发式搜索 | 搜索 | SQL-o1 称在 BIRD 上 EX 提升 10.8 点（对照未核实） | https://arxiv.org/abs/2501.16607 ，https://arxiv.org/abs/2502.11741 |
| ReFoRCE（Spider 2.0） | 按模式分组压缩 schema、自修正、多数投票共识、列探索 | 共识投票 | Spider 2.0-Snow 35.83，Lite 36.56 | https://arxiv.org/abs/2502.00675 |
| LEAF-SQL（2026） | 三级骨架层级 Base / Expanded / Detailed，粗到细树搜索；SkeFor 生成、SkeEval 剪枝 | 骨架搜索 | 73.5 / 71.6（GPT-4o），比同底座 RSL-SQL 高 2.9 点 | https://arxiv.org/abs/2605.09295 |
| HES-SQL（2025） | SFT + GRPO，奖励里有骨架完整度和查询时延 | 无 | 论文称 BIRD 79.14、KaggleDBQA 54.9（划分未核实） | https://arxiv.org/abs/2510.08896 |
| CAPER（2026） | 在 SQL AST 上做反事实干预，自动得到子句级监督，训练 9B 的 Clause-PRM | 子句级打分 | 相对 GPT-5.4 的 EX 最多提升 15.3%（相对值）；失败定位准确率 84.53%、MRR 90.60% | https://arxiv.org/abs/2606.03327 |
| EXPO-SQL / Progress-SQL（2026） | 子句级奖励：靠报错信息和逐子句增量执行定位错误子句 / Oracle 诊断树 | 子句级 | 摘要没有给 BIRD 数字（未核实） | https://arxiv.org/abs/2606.23693 ，https://arxiv.org/abs/2606.06825 |
| DecoSearch（2026） | LLM Judger 二分路由：简单题直接生成，复杂题分解成子问题 DAG | 二分类路由 | BIRD 70.53、Spider 88.31（DeepSeek 底座），token 少一个数量级 | https://arxiv.org/abs/2606.17821 |
| SPC（2026） | 多轮 planner 只在「受治理的、针对该问题的选项」里选；图遍历、角色谓词、粒度下推、SQL 构造都在代码里做 | 受控选择 + 确定性编译 | ACME 保险库 38 题：三次都对的题 97.4% vs 直接生成 55.3%（McNemar p=3.05e-5） | https://arxiv.org/abs/2608.16663 |

其他检索到的：DexterSQL 用依存树 IR 引导出 SQL 骨架，BIRD dev 70.4%（GPT-OSS-120B）https://arxiv.org/abs/2608.11889 ；SGU-SQL 用语法式 prompt 分解生成 https://arxiv.org/abs/2402.13284 ；OpenSearch-SQL 设计了中间语言 SQL-Like，69.30 / 72.28（LB）https://arxiv.org/abs/2502.14913 ；AV-SQL 用 agent 生成的 CTE 视图，Spider 2.0 70.38%、BIRD 72.16% https://arxiv.org/abs/2604.07041 ；RSL-SQL 双向 schema linking，strict recall 94%、输入列减少 83% https://arxiv.org/abs/2411.00073 。

### 3.2 SOTA 参照（非骨架类，只作坐标）

| 系统 | BIRD dev / test EX | 要点 | 来源 |
|---|---|---|---|
| AskData（AT&T，2026-10） | 78.49 / 83.40 | 当前 LB 第一 | LB |
| Agentar-Scale-SQL | 74.90 / 81.67 | 内部推理（RL）、迭代修正、并行合成 + 锦标赛选择三种 test-time scaling | https://arxiv.org/abs/2509.24403 |
| Human（数据工程师 + 学生） | – / 92.96 | BIRD 人类水平 | https://arxiv.org/abs/2305.03111 |
| Reasoning-SQL 14B | 72.29 / 72.78 | GRPO + schema linking、AI 反馈、n-gram、语法等部分奖励 | https://arxiv.org/abs/2503.23157 |
| Arctic-Text2SQL-R1-32B | 72.20 / 73.84（单模型榜） | 只用执行正确性做奖励 | https://arxiv.org/abs/2505.20315 |
| CSC-SQL + XiYanSQL-QwenCoder-32B | 71.33 / 73.67 | RL 修正的 self-consistency | https://arxiv.org/abs/2505.13271 |
| SQL-R1 | 论文称 BIRD 67.1、Spider 88.6（划分未核实） | RL 推理模型 | https://arxiv.org/abs/2504.08600 |
| Distillery + GPT-4o | 67.21 / 71.83 | 「Death of Schema Linking?」：schema 放得进上下文时不做 schema linking | https://arxiv.org/abs/2408.07702 |
| mini-dev（SQLite / MySQL / PostgreSQL EX） | GPT-4 47.80 / 40.80 / 35.80；TA + GPT-4o 63.00（SQLite）；2026-10 条目 schemagate + Claude Opus 5.5 81.00 / 72.40 / 72.60（所用 mini-dev 版本未核实） | 我们的直接参照 | https://github.com/bird-bench/mini_dev ，LB |

Distillery 那一行对我们的叙事是风险：mini-dev 最大的库只有 199 列，整库放得进 LLM 上下文，内部报告 §6 也因此看不出 JEV 按表拆分的优势。

### 3.3 约束解码与置信度校准

| 工作 | 要点 | 来源 |
|---|---|---|
| Synchromesh | 目标相似度调优（TST）选示例 + 约束语义解码（CSD），覆盖 SQL、Vega-Lite、SMCalFlow | https://arxiv.org/abs/2201.11227 |
| GCD | 输入相关的文法，把语法约束解码统一到结构化 NLP 任务 | https://arxiv.org/abs/2305.13971 |
| XGrammar | CFG 约束解码最多提速 100 倍 | https://arxiv.org/abs/2411.15100 |
| Grammar-Aligned Decoding | 约束解码会扭曲 LM 的分布；ASAp 算法让采样匹配「在文法约束下的条件分布」 | https://arxiv.org/abs/2405.21047 |
| Text-to-SQL 校准：Just Rescale | 用整句概率当置信度，优于追问式自检和口述置信度 | https://arxiv.org/abs/2411.16742 |
| 子句频率 SCF + 多元 Platt | 利用 SQL 结构给出子句级正确性信号，再合成为整体置信度；与「槽位级概率」直接对应 | https://arxiv.org/abs/2505.23804 |
| 选择性预测研究（2026） | 黑盒信号（自一致性、可执行性等）AUROC 约 0.61–0.68，白盒 log-prob 0.67；LLM judge 0.72–0.78；两家 judge 集成 0.82，ECE 0.03 | https://arxiv.org/abs/2607.06799 |
| Conformal abstention（2026） | 校准标签换成蒸馏测试集后，证书的实际风险比自报高 2.73–10.23 点 | https://arxiv.org/abs/2609.25938 |
| 置信度估计 | T5 比 GPT-4、Llama 3 校准得好；一致性方法在黑盒设定下最好 | https://arxiv.org/abs/2501.09527 ，https://arxiv.org/abs/2508.14056 |

### 3.4 工业界的模板库与语义层

- Snowflake Cortex Analyst：Verified Query Repository 是「问题 + 已验证 SQL」对，用来提高准确率，也能自动推荐新的 verified query（最多 10 条）https://docs.snowflake.com/en/user-guide/views-semantic/verified-query-repository ，https://docs.snowflake.com/en/user-guide/views-semantic/verified-query-suggestions 。
- Databricks Genie：trusted assets 是编辑审核过的参数化示例 SQL 和 SQL 函数，Genie 优先使用，命中时把答案标为 Trusted https://docs.databricks.com/aws/en/genie/add-instructions 。
- dbt Semantic Layer / MetricFlow：指标在 YAML 里定义一次，「指标 + 维度」请求由 MetricFlow 编译成 SQL https://upsolve.ai/blog/dbt-semantic-layer ，代码 https://github.com/transform-data/metricflow 。
- 知识图谱 benchmark：GPT-4 零样本直接对企业 SQL 库只有 16%，改在知识图谱表示上提问为 54% https://arxiv.org/abs/2311.07509 。
- 共同点：都是「受控选项 + 确定性编译」，正好是 Jev 擅长的选择题形态；但都依赖人工维护的语义资产，BIRD 没有这些资产。

### 3.5 与 Jev 直接相关的已有工作

- **Zenn 原型（snaga，2026-10-25）**：把分析型 Text-to-SQL 当作选择题。每个槽位（起始表、指标、期间、维度、过滤条件）从元数据白名单中选，所有问题放在一个 Jev 请求里；join 路径用 NetworkX 最短路在代码里算；sqlglot 组装 AST；按置信度阈值分为自动执行、人工确认和中止 https://zenn.dev/snaga/articles/2026-10-25-deterministic-text-to-sql-jev-ast ，代码 https://github.com/snaga/Deterministic-Text-to-SQL （2026-10-18 创建，0 star）。只在 TPC-H 8 表上演示了 3 个手工案例，没有准确率统计。文中「0% 语法错误、0% 列幻觉、<200 ms」等说法没有可复核的评测，未核实。
- **ai2sql 博客**：Jev 只用于选表、方言识别、执行前把关，明确说没有测 Spider / BIRD 上的整体准确率 https://builder.ai2sql.io/blog/jev-for-sql-use-cases 。
- 2026-11-05 用 web search 检索「Jev + BIRD / Spider / slot filling」，没有找到公开评测。

**小结**：LLM 时代把骨架用作检索键、中间生成步骤或监督粒度，没有工作把「骨架 + 槽位」作为最终输出交给非生成式模型，并在 BIRD 上评测。最强的系统都靠多候选选择，而选择器本身就是离散决策问题，适合交给 Jev。

## 4. 本地统计：BIRD mini-dev gold SQL 能被多少骨架覆盖

### 4.0 数据与口径

- 本地文件去重后 498 题，难度 148 / 248 / 102。原始 500 行的分布 148 / 250 / 102 与 README 的 30 / 50 / 20% 一致 https://github.com/bird-bench/mini_dev 。
- **版本警告**：mini-dev README 记录 2026-10-28 发布了更新版（修正标注、替换部分实例）https://github.com/bird-bench/mini_dev 。通过 HF datasets-server 读到的当前 `birdsql/bird_mini_dev` 的 `mini_dev_sqlite` split（500 行），与本地文件按 (db_id, 问题文本) 匹配只有 13 题相同，难度分布是 8 / 32 / 460 https://huggingface.co/datasets/birdsql/bird_mini_dev 。本地文件是旧版，与 2026-10 之后的 LB mini-dev 条目可能不可比，论文里必须写明版本。
- 统计只看语法（sqlglot 解析，不执行）。「覆盖」指语法落在该层，不代表编译器能复现 gold 结果。BIRD 的 EX 判定是 `set(predicted_res) == set(ground_truth_res)`（mini_dev 仓库 `evaluation/evaluation_ex.py`）：行序和重复行不影响结果，列的个数和顺序影响。

### 4.1 结构出现频率（% of 题）

| 结构 | 全部 | simple | moderate | challenging |
|---|---:|---:|---:|---:|
| WHERE | 87.3 | 89.9 | 88.3 | 81.4 |
| JOIN（≥1） | 81.3 | 60.1 | 88.3 | 95.1 |
| ≥3 张表 | 21.1 | 8.1 | 22.2 | 37.3 |
| GROUP BY / HAVING | 7.6 / 1.8 | 3.4 / 1.4 | 9.3 / 2.0 | 9.8 / 2.0 |
| ORDER BY / ORDER BY + LIMIT 1 | 17.7 / 14.1 | 12.2 / 11.5 | 23.0 / 17.3 | 12.7 / 9.8 |
| SELECT DISTINCT / COUNT(DISTINCT) | 12.0 / 4.4 | 12.8 / 2.0 | 9.7 / 3.6 | 16.7 / 9.8 |
| 任意聚合 | 49.4 | 39.2 | 47.6 | 68.6 |
| 算术 + - * / | 27.1 | 10.1 | 24.6 | 57.8 |
| 比值 × 100（百分比） | 12.7 | 4.1 | 11.3 | 28.4 |
| IIF / CASE | 17.9 | 7.4 | 14.5 | 41.2 |
| 条件聚合（如 `SUM(IIF(...))`） | 15.1 | 7.4 | 11.7 | 34.3 |
| CAST | 18.5 | 6.8 | 16.9 | 39.2 |
| 日期函数 / 字符串函数 | 10.4 / 5.6 | 6.1 / 2.0 | 10.9 / 4.8 | 15.7 / 12.7 |
| 谓词含 OR / 含 NOT | 3.8 / 5.4 | 0.7 / 3.4 | 4.0 / 5.6 | 7.8 / 7.8 |
| 子查询 | 12.0 | 8.1 | 10.1 | 22.5 |
| 集合运算 / CTE / 窗口函数 | 0.4 / 1.2 / 1.0 | 0 / 0 / 2.0 | 0.8 / 0.4 / 0.8 | 0 / 4.9 / 0 |

- 子查询位置（60 题中共 75 个嵌套 SELECT，不含 CTE）：WHERE 标量比较 28，FROM 派生表 26，IN 11，SELECT 标量 7，EXISTS 3。
- 谓词运算符（981 个，不含 JOIN ON）：`=` 728（74.2%），`>` 61，LIKE 32，IS NOT NULL 30，BETWEEN 27，`>=` 26，`<` 26，IN 20。
- SELECT 项：普通列 415 个，算术表达式 106 个，COUNT(col) 67 个，AVG 15 个，COUNT(DISTINCT) 15 个，SUM 10 个。自连接 2 题，LEFT JOIN 1 题。

### 4.2 语法分层覆盖

| 层 | 定义 | 本层题数 | 累计 | simple | moderate | challenging |
|---|---|---:|---:|---:|---:|---:|
| A | 单层 SELECT：JOIN、WHERE（=、比较、LIKE、BETWEEN、IN、IS NULL、AND / OR / NOT）、聚合、DISTINCT、GROUP BY、HAVING、ORDER BY、LIMIT；单独出现的 CAST 不升层 | 266 | 53.4% | 73.6% | 52.4% | 26.5% |
| B | A + 算术、IIF / CASE、日期 / 字符串 / 数值函数（含 JULIANDAY 等） | 164 | 86.3% | 91.2% | 87.9% | 75.5% |
| B′ | A + B 中算术只用 top-10 模板（§4.6）的题（累计数） | 389 | 78.1% | 88.5% | 79.8% | 58.8% |
| C | B + 恰好一个深度为 1 的子查询 | 46 | 95.6% | 96.6% | 96.4% | 92.2% |
| D | 集合运算、CTE、窗口函数、深度 ≥ 2 或多个子查询 | 22（4.4%） | 100% | 100% | 100% | 100% |

### 4.3 查询族（每题一个标签，优先级 F8 > F7 > … > F1）

| 族 | 全部 | simple | moderate | challenging |
|---|---:|---:|---:|---:|
| F1 查值 / 投影 | 156（31.3%） | 68 | 69 | 19 |
| F2 标量聚合 | 83（16.7%） | 34 | 42 | 7 |
| F3 无 GROUP 的 argmax / top-k | 51（10.2%） | 14 | 34 | 3 |
| F4a GROUP + argmax / top-k | 15（3.0%） | 3 | 9 | 3 |
| F4b GROUP 聚合 | 9（1.8%） | 1 | 6 | 2 |
| F5 行级算术 | 25（5.0%） | 2 | 16 | 7 |
| F6 聚合级算术（比值 / 百分比 / 差） | 91（18.3%） | 13 | 42 | 36 |
| F7 子查询 | 55（11.0%） | 10 | 25 | 20 |
| F8 集合运算 / CTE / 窗口 | 13（2.6%） | 3 | 5 | 5 |

挑战题有 56 / 102（54.9%）落在 F6 和 F7。F8 优先级更高，所以 F7（55）比 §4.1 的子查询题数（60）少。

### 4.4 骨架集中度与 train 重合

train 用 HF `birdsql/bird23-train-filtered`（BIRD train 过滤后 6,601 / 9,428 条，https://github.com/bird-bench/mini_dev ），6601 条全部解析成功。

| 层级 | 定义 | 种类 | 只出现一次 | top-5 | top-10 | top-50 | 在 train 出现过（全部 / 挑战） | train 中种类数；train top-K 类的覆盖 |
|---|---|---:|---:|---:|---:|---:|---|---|
| L0 | 子句级：有哪些子句、投影是列还是聚合、子查询位置 | 59 | 31 | 75.3% | 82.9% | 98.2% | 96.0% / 92.2% | 116；K=10 80.7%，K=30 91.2% |
| L1 | L0 + 表达式标记（算术、百分比、条件聚合、日期 / 字符串函数） | 121 | 73 | 50.0% | 59.2% | 85.7% | 90.6% / 81.4% | 260；K=50 78.3%，K=255 90.6% |
| L2c | 匿名 SQL，列表和同形 AND 链合并 | 304 | 267 | 29.3% | 34.9% | 49.0% | 58.2% / 31.4% | 1312；K=255 50.6% |
| L2 | 匿名 SQL，严格（近似 RESDSQL 骨架） | 346 | 304 | 18.1% | 23.7% | 40.6% | 49.2% / 23.5% | 1748；K=255 40.8% |

最常见的 L0：`SELECT AGG WHERE` 29.3%，`SELECT COL WHERE` 23.3%，`SELECT DISTINCT COL WHERE` 9.8%，`SELECT COL WHERE ORDER LIMIT1` 7.4%，`SELECT AGG` 5.4%（AGG 表示外层 SELECT 中有聚合，也包括 `SUM(...)/COUNT(...)` 这类算术）。

含义：L0 在 train 中只有 116 类，一个 choice 就能装下。L1 有 260 类，略超 255 的上限，可以取 top-255（覆盖 90.6%），或拆成「L0 choice + 若干标记 noul」。L2 不能用作模板库：挑战题只有 23.5% 的骨架在 train 中出现过，与 Finegan-Dollak 的 query split 结论一致。

### 4.5 决策数与误差累积

内容决策数 = 2×SELECT 项 + 3×谓词原子 + 连接词 + GROUP 键 + 2×ORDER 键 + LIMIT + 算术根 + IIF/CASE + 子查询位置。另有约 9 个全局开关不计入（族、DISTINCT / WHERE / GROUP / HAVING / ORDER / LIMIT 有无、子查询、集合运算）。

| 子集 | 内容决策 中位 / p90 | SELECT 项 中位 / p90 | 谓词原子 中位 / p90 | AST 节点 中位 / p90 | AST 深度 中位 / p90 | 表 / 列 中位 |
|---|---|---|---|---|---|---|
| 全部 | 10 / 17（均值 11.1） | 1 / 2 | 2 / 3 | 22 / 34 | 5.5 / 9 | 2 / 5 |
| simple | 8 / 13 | 1 / 2 | 1 / 3 | 16 / 23 | 5 / 8 | 2 / 4 |
| moderate | 10 / 16 | 1 / 2 | 2 / 3 | 23 / 29 | 6 / 9 | 2 / 5 |
| challenging | 15 / 25 | 1 / 3 | 2 / 5 | 31 / 50 | 8 / 11 | 2 / 6 |

假设每个决策（内容 + 9 个开关）独立，且准确率都为 p，整题全对的期望：p = 0.95 时 37.0%，0.98 时 67.0%，0.99 时 81.8%。这个估计偏悲观的一面是很多决策近乎确定（`=` 占 74.2%）；偏乐观的一面是没有计入上游 schema linking 的错误。

### 4.6 算术模板

135 题（27.1%）含算术，共 168 个算术根表达式。归一化规则：列 → c，常数 → k（保留 100），`SUM(IIF(cond,1,0))` → CNT_IF，`COUNT(CASE ...)` → COUNT(IND)，其他 IIF/CASE → IFVAL，标量函数 → FN，去掉 CAST 和括号。归一化后只有 51 种，27 种只出现一次；top-10 覆盖 58.9%，top-20 覆盖 79.2%。

| 次数 | 归一化形式 | 典型含义 |
|---:|---|---|
| 22（13.1%） | `(CNT_IF * 100) / COUNT(c)` | 满足条件的百分比 |
| 14（8.3%） | `FN - FN` | 年份相减，如 `STRFTIME('%Y', CURRENT_TIMESTAMP) - STRFTIME('%Y', Birthday)` 求年龄 |
| 14（8.3%） | `(COUNT(IND) * 100) / COUNT(c)` | 同上，CASE 写法 |
| 10（6.0%） | `FN + k` | 字符串位置偏移，如 `INSTR(q3, ':') + 1`，用于解析圈速 |
| 9 / 9 | `c / c`；`SUM(c) / COUNT(c)` | 行级比值（如 Price / Amount）；手写的均值 |
| 6 | `((SUM(IFVAL) - SUM(IFVAL)) * 100) / SUM(IFVAL)` | 增长率 |
| 5 / 5 / 5 / 5 | `CNT_IF / CNT_IF`；`CNT_IF - CNT_IF`；`(FN - FN) - k`；`FN - k` | 两组计数之比 / 之差；日期差 |

按类别：聚合级百分比 35.1%，聚合级比值 / 均值 15.5%，行级差（在 SELECT 中）9.5%，聚合级差 6.5%，行级差（在 WHERE 中）4.8%。

### 4.7 evidence 公式与 gold 算术

- evidence 非空 99.6%。含公式样文本（DIVIDE / SUM / COUNT(...) 调用、词间的 + - * /、percent / ratio / rate 等词）43.6%（simple 25.7%，moderate 45.6%，challenging 64.7%）；其中 DIVIDE / SUM 式调用 38.6%。含值映射（「refers to」「=」等）83.3%。
- P(evidence 有公式 | gold 有算术) = 123 / 135 = 91.1%；P(gold 有算术 | evidence 有公式) = 123 / 217 = 56.7%（公式词有时只对应单个聚合，如 MAX）。
- 含义：算术公式绝大多数直接写在 evidence 里。应由代码把 evidence 解析成「算术模板候选」，Jev 只负责选模板、把操作数绑定到列、判断条件谓词。值映射也能用同样的方式解析成谓词候选。

### 4.8 值槽位（谓词中的字面值，不含 JOIN ON）

| 字面值 | 问题中原样出现 | 问题中出现但大小写不同 | 只在 evidence 中出现 | 都不出现 |
|---|---:|---:|---:|---:|
| 字符串（693 个） | 57.0% | 9.2% | 28.3% | 5.5% |
| 数字（236 个） | 59.7% | – | 34.3% | 5.9% |

92.6% 的题有谓词字面值，其中 52.3% 的题所有字面值都能在问题中（不区分大小写）找到。值槽位主要是「span → 库中值」的匹配问题。大小写不同（9.2%）和找不到（5.5%）的情况需要库值索引（LSH / trigram），由 Jev 在检索出的候选值里选，不让它生成值。SQLite 的 `=` 默认区分大小写，大小写错了 EX 就会错。

### 4.9 JOIN 与外键

- 81.3% 的题有 JOIN，ON 中共 528 个列等式：88.8% 是 `dev_tables.json` 声明的外键；另有 31 个等式两侧列名相同但没有声明外键；6 个别名无法解析。
- 按题算：所有 ON 等式都是声明外键的 86.7%；把同名列也算作外键边后 93.1%。自连接 2 题，没有缺 ON 的 join。
- 内部报告 §4.3 用另一种口径交叉印证：412 道多表 gold 中，43 道（10.4%）的 gold 表在外键图里不连通，原因是兄弟表直接 join 和元数据漏了外键，两者都能用确定性规则补。
- 含义：join 可以由外键图最短路 / Steiner 树确定性补全，不需要 Jev 决策；剩下约 7% 交给规则或回退。

### 4.10 对设计的直接含义

1. 骨架用「L0 / 族分类 + 表达式标记」，不用整句模板。
2. join、CAST、别名、括号全部交给编译器。Jev 的决策只集中在列角色、谓词、算术模板绑定、值、子查询位置。
3. 最难的三类槽位是算术（27.1%）、问题和 evidence 中都没有的值、子查询（12.0%）。v1 可以先不做子查询。
4. EX 按集合比较，所以顶层 DISTINCT 和没有 LIMIT 的 ORDER BY 不影响 EX，可以从决策集里删掉或用固定规则处理；COUNT(DISTINCT) 和 SELECT 列的个数与顺序则会影响。

## 5. 用 Jev 驱动端到端生成：三种设计

### 5.0 共同原则（来自 Jev 文档列出的弱项和本组实测）

1. 算术、计数、日期由代码完成，Jev 只选模板、操作数和粒度（文档：Jev 不是计算器，计数不可靠，日期按文本读）。
2. 互斥的决策用一个 choice 让候选互相比较（ai2sql：56 表时，choice 的最差必需表排名中位 3，独立 noul 是 22.5，https://builder.ai2sql.io/blog/jev-for-sql-use-cases ）；不互斥的决策（每条候选谓词要不要）用 noul。
3. state 只放链接后的子 schema、相关 evidence 和候选，不放整库：无关上下文会降低准确率，而且有 32k 上限。
4. 一个问题只问一件事。有依赖的子问题也无条件一起问，由编译器丢掉不一致的答案；state 每个请求只计一次费，多问几十个问题几乎不增加成本。
5. 阈值附近不要硬切，交给 beam 和执行过滤。本组实测的波动很小（列 Score 在阈值处翻转 0.46%），但第三方实测最大漂移 0.09（§1.1）。
6. 每个槽位都要和「LLM 在同一编译器下填同样的槽」比较（§0 第 9 条），不预设 Jev 更准。

### 5.1 设计 A：Jev-Sketch（组合式槽位 + 确定性编译），推荐主线

单题流程：

- **R0（已有）**：mention 抽取 → 表 Noul → 列 Score → 阈值 + 强制外键列，得到候选列集合 C；值索引给出候选值 V。新增两个确定性组件：evidence 解析器（值映射 → 候选谓词；公式 → 算术模板候选），以及日期模板（年 / 月 / 区间 → `STRFTIME` / `SUBSTR` / `BETWEEN`）。
- **R1（一次 Jev 请求，所有问题并行）**：
  - 族 / 骨架：一个 choice，选项是 train 中最常见的 L0 类（或 §4.3 的 F1–F8），每个选项附描述和一条示例问题。
  - 子句开关：约 6 个 noul（COUNT 是否去重、ORDER + LIMIT、GROUP、HAVING、子查询、两组比较）。
  - SELECT：choice over C 选第 1 个输出列；choice over {无, COUNT, COUNT DISTINCT, SUM, AVG, MAX, MIN} 选聚合；需要多列时再问「第 k 列」。
  - 算术：choice over evidence 解析出的模板候选（外加「无算术」）；操作数用 choice 绑定到 C 中的列和候选谓词。
  - WHERE：每条候选谓词一个 noul（是否需要这个条件）；运算符有歧义时用 choice（= / LIKE / > / BETWEEN …）；值用 choice over V。
  - GROUP / ORDER：choice over C 选键；choice {ASC, DESC} 选方向；LIMIT 的 k 由正则从问题中抽取，Jev 只做确认（noul）。
- **R2（本地编译 + beam）**：按联合 log-prob 枚举 top-B 组相互一致的槽位组合（例如 GROUP 键必须出现在 SELECT 或 ORDER 中）；用 sqlglot 构建 AST，按外键图补 join，除法自动加 `CAST(... AS REAL)`；执行，丢掉报错和空结果，按结果去重。
- **R3（可选，一次 Jev 请求）**：给 top-B 候选的 SQL 和结果预览出一个 choice，再对每个候选做少量 noul 校验（「输出列是不是问题要的」「evidence 中的条件都用上了吗」）。
- **置信度**：把联合 log-prob 和 R3 的概率用 Platt 或多元 Platt 校准（与 SCF 的思路相同，https://arxiv.org/abs/2505.23804 ）。低于阈值就回退到 LLM（设计 C3）。

**例：qid 1472**「In 2012, who had the least consumption in LAM?」，gold 为 `SELECT T1.CustomerID FROM customers AS T1 INNER JOIN yearmonth AS T2 ON T1.CustomerID = T2.CustomerID WHERE T1.Segment = 'LAM' AND SUBSTR(T2.Date, 1, 4) = '2012' GROUP BY T1.CustomerID ORDER BY SUM(T2.Consumption) ASC LIMIT 1`。对应的槽位：族 = F4a（GROUP + argmax）；SELECT = customers.CustomerID，无聚合；谓词 `Segment = 'LAM'` 为真（mention 与值索引匹配）；年份谓词由 evidence「The first 4 strings of the Date values ... can represent year」经日期模板得到 `SUBSTR(Date, 1, 4) = '2012'`；GROUP = CustomerID；ORDER = SUM(Consumption) ASC；LIMIT 1；join 由外键补全。共约 10 个决策，一次 R1 请求就够。

```json
{
  "state": {
    "task": "sql_slot_filling",
    "question": "In 2012, who had the least consumption in LAM?",
    "evidence": "Year 2012 can be presented as Between 201201 And 201212; The first 4 strings of the Date values in the yearmonth table can represent year.",
    "linked_schema": {"customers": ["CustomerID", "Segment", "Currency"], "yearmonth": ["CustomerID", "Date", "Consumption"]},
    "candidate_predicates": {"p1": "customers.Segment = 'LAM'", "p2": "SUBSTR(yearmonth.Date, 1, 4) = '2012'", "p3": "yearmonth.Date BETWEEN 201201 AND 201212"}
  },
  "questions": {
    "family": {"type": "choice", "instructions": "Which query shape answers the question?",
               "criteria": {"lookup": "return column values of matching rows", "scalar_agg": "one aggregate over matching rows",
                            "argmax": "the row with the highest or lowest value", "group_argmax": "the group with the highest or lowest aggregate",
                            "ratio": "a ratio or percentage computed from aggregates"}},
    "select_col": {"type": "choice", "instructions": "Which column holds the answer the question asks for?",
                   "criteria": {"customers.CustomerID": "customer id", "customers.Segment": "customer segment", "yearmonth.Consumption": "monthly consumption"}},
    "pred_p1": {"type": "noul", "instructions": "Does answering the question require the condition customers.Segment = 'LAM'?"},
    "pred_p2": {"type": "noul", "instructions": "Does answering the question require the condition SUBSTR(yearmonth.Date, 1, 4) = '2012'?"},
    "order_dir": {"type": "choice", "instructions": "Does the question ask for the lowest or the highest value?",
                  "criteria": {"ASC": "lowest / least / minimum", "DESC": "highest / most / maximum"}}
  }
}
```

p2 和 p3 是 evidence 给出的两种写法，beam 同时保留，执行结果相同的候选在去重时合并。

**决策数、成本、延迟（估算，未实测）**：每题 R1 约 15–40 个问题（族 1、开关约 6、SELECT 2–4、算术 0–3、每条候选谓词 1–2、GROUP / ORDER 2–3）。R1、R3 的 state 约 1.5–4k token，按 $0.042 / 1M 计，两次请求合计不到 $0.001 / 题，498 题不到 $0.5。延迟在现有 schema linking（每题 3–5 s）之上再加 2 次 Jev 调用（约 2–6 s），外加执行 B 个候选的时间。

**覆盖**：v1 范围（A + B′）在语法上覆盖 78.1%（挑战题 58.8%）。v2 加入单个子查询（C 层 46 题；全部子查询的位置分布见 §4.1：WHERE 标量 28、FROM 派生 26、IN 11）后上限 95.6%；D 层 4.4% 直接回退。round-trip EX 一定不高于语法覆盖，要在 P0 实测。

**主要失败模式**：

- 误差累积（§4.5）。
- 选错算术模板或操作数绑反；evidence 公式与 gold 不一致。BIRD 本身有标注噪声：SQL-PaLM 人工抽查 100 条错例，31% 是评测误判 https://arxiv.org/abs/2306.00739 。
- 值：大小写、单位、日期格式，以及问题和 evidence 里都没有的值（§4.8）。
- argmax 有两种写法（`ORDER BY ... LIMIT 1` 与 `= (SELECT MAX ...)`），并列时结果不同，需要固定一种并贴近 gold 风格。
- Jev 按字面理解问题，问题措辞会影响结果，要在 train 上调问题模板。
- 编译器不支持的写法：窗口函数、CTE、自连接、LEFT JOIN 语义。

**与已有工作的区别**：

| 已有工作 | 相同点 | 不同点 |
|---|---|---|
| SQLNet / TypeSQL / SQLova | sketch + 槽位分类 | 它们是单表 WikiSQL、为任务专门训练的网络；这里用通用的零样本决策模型，面对多表、evidence 和脏值 |
| RESDSQL / PURPLE / LEAF-SQL | 骨架作为中间结构 | 它们的骨架交给生成器继续生成；这里骨架 + 槽位就是最终决策，确定性编译成 SQL |
| SPC | 受控选择 + 确定性编译 | SPC 的 planner 是 LLM，依赖人工维护的语义资产，评测是 38 题对比集；这里决策者是 Jev，没有语义资产，在 BIRD 上评测 |
| Zenn 原型 | Jev 判槽 + sqlglot | 原型只有 TPC-H 上 3 个案例；这里要处理 BIRD 的算术、evidence、脏值和子查询，并做完整评测 |

**可发表的空白**：(1) 首个在 BIRD 规模上评测纯决策式 SQL 构造（不做生成式解码）的工作；(2) 槽位级校准 + 选择性预测的 risk-coverage，与 self-consistency / LLM judge 的 AUROC（0.61–0.82，https://arxiv.org/abs/2607.06799 ）对照；(3) 成本 / 延迟前沿：多大比例的题能以零生成 token 答对；(4) 决策分类学：哪些 SQL 决策适合 System One，哪些必须交给代码或 LLM，对应文档列出的弱项逐项验证。**风险**：如果整体 EX 明显低于 LLM，主结论要改为「同等准确率下的成本」或「高置信子集上的精度」。

### 5.2 设计 B：按语法逐产生式解码 + beam（不推荐作主线）

- 每个 AST 非终结符的展开是一个 choice，决策数约等于 AST 节点数（中位 22，p90 34），是设计 A 的两倍以上，误差累积更严重。
- 产生式依赖已经生成的部分树，只能逐层批量：轮数约等于 AST 深度（中位 5.5，p90 9），每轮 1–3 s，每题 6–27 s；beam 宽度再乘上请求数。
- 「下一个产生式是什么」是抽象、间接的问题，正好是文档列出的弱项（按字面理解、多跳）。
- 这条路已被 SyntaxSQLNet、IRNet、RAT-SQL、SmBoP（语法解码）以及 PICARD、XGrammar（约束解码）占满；GAD 还指出约束解码会扭曲分布 https://arxiv.org/abs/2405.21047 。可借鉴的只是局部：在设计 A 里把「运算符用 = 还是 LIKE 还是 IN」这类小决策写成语法约束下的 choice。

### 5.3 设计 C：混合（LLM 提候选，Jev 选择 / 填槽 / 校验）

- **C1 选择器**：LLM 用不同 prompt 或温度生成 N 个候选，按执行结果聚类；Jev 用一个 choice 在各簇代表中选，再用 noul 做子句级校验（输出列、evidence 条件、聚合粒度）。对应 CHASE-SQL 的微调成对选择器、MCS-SQL 的 LLM 多选题、XiYan-SQL 的选择模型；区别是不用训练、成本近乎为零、还能给出校准后的置信度。
- **C2 填空**：LLM 写带类型空洞的 SQL 骨架，Jev 从 linking 结果里选列和值填入，消除列幻觉。这与 ZeroNL2SQL 的方向相反（ZeroNL2SQL 由 PLM 出 sketch、LLM 填细节）。
- **C3 路由**：Jev 预测族和置信度，高置信走设计 A，其余交给 LLM。对应 DIN-SQL 的三分类和 DecoSearch 的二分路由，区别是路由信号来自校准后的概率。

### 5.4 对比

| 设计 | 每题 Jev 决策数 | 生成 token | 语法覆盖 | 主要风险 | 新颖性 |
|---|---|---|---|---|---|
| A Jev-Sketch | 15–40（1–2 次请求） | 0（回退时才用 LLM） | v1 78.1%，v2 上限 95.6% | 误差累积、算术、值 | 高（BIRD 上无人做过） |
| B 逐产生式 | 约等于 AST 节点数（中位 22），6–9 轮 | 0 | 理论 100% | 延迟、间接问题、误差累积 | 低（语法解码已有大量工作） |
| C1 选择器 | 约 N + 3N 个（1 次请求） | N 个候选 | 100%（取决于生成器） | 候选中没有正确答案 | 中（选择器已有，零训练 + 校准是新的） |
| C2 填空 | 每个空洞 1–2 个 | 骨架 | 取决于骨架 | 骨架错了无法挽回 | 中 |

## 6. 推荐方向、最小实验计划与风险

### 6.1 推荐

- **P1 主线：设计 A（Jev-Sketch）+ C3 置信度门控回退**。直接接在现有 schema linking 上，研究问题清楚：生成式解码有多少能被校准后的离散决策替代，替代后能省多少成本。
- **P2 副线：设计 C1（Jev 选择器 / 校验器）**。2–3 天就能出第一版结果，也是 P1 回退分支要用的组件。内部报告 §6 发现 JEV 与 LLM 的错误不重合（两者取并集后，列全召回率从 62.2% 升到 71.3%），这为「Jev 加 LLM 互补」提供了初步支持。
- 不做：设计 B；整句模板库（L2 在挑战题上只有 23.5% 的骨架在 train 中出现过）。

### 6.2 最小实验计划

| 阶段 | 内容 | 输出 |
|---|---|---|
| P0 数据与上界（不调 API） | 固定旧版 mini-dev 的 498 题；槽位语法 v1 只按 train 的统计设计；写 gold SQL → 槽位 → SQL 的反编译 / 编译器，做 round-trip 执行一致率 | 语法覆盖上界（预期不高于 §4.2 的 78.1%）和失败分类 |
| P1 oracle schema 下的槽位准确率 | 给 gold 列集合，只测槽位决策：Jev vs 同一 slot schema 下 qwen3.8-flash 一次调用填槽 | 每类槽位的准确率、ECE / Brier、重复 3 次的翻转率 |
| P2 端到端 | 用实验3 的 schema linking；beam B ∈ {1, 4, 16} | EX（总体、覆盖子集、按难度和族），误差归因（linking / 结构 / 值 / 算术 / 编译） |
| P2′ 选择器 | LLM 生成 N = 8 个候选；Jev 选 vs 多数投票 vs LLM 选 | EX、正确性 AUROC |
| P3 级联 | 置信度门控：A 高置信直接用，其余回退 LLM | risk-coverage 曲线、成本 / 延迟的 Pareto 前沿 |

所有 EX 用同一个评测脚本，在同样的 498 题上做配对 bootstrap，与主线一致。`wzy/analysis/sql_ex.py` 已验证并跑完 11 种 schema 输入（内部报告 §3.4，2026-11-05）：qwen3.8-flash 单次生成，全 schema EX 63.1、gold schema 71.6、实验3 选出的列 58.7；同一设置两次生成的 EX 差 1.6 个点。P2 的 B0/B1 可以直接复用这些数字，但每个设置要多次生成取平均。

- **基线**：B0 LLM 直接生成；B1 LLM + 我们的 schema linking；B2 已公开的 mini-dev 数字（GPT-4 47.80、TA + GPT-4o 63.00，https://github.com/bird-bench/mini_dev ，只作参考，版本可能不同）；B3 qwen 一次调用填同样的槽、走同一编译器（用来分离「槽位语法」和「决策者」的贡献）；B4 self-consistency 多数投票、LLM 选择器。
- **指标**：EX（官方脚本）与 Soft-F1；覆盖率；覆盖子集上的 EX；每类槽位的决策准确率（对照从 gold 反编译出的槽位，执行等价的不同槽位算对）；ECE / Brier / AUROC（参照 0.61–0.82 的区间，https://arxiv.org/abs/2607.06799 ）；risk-coverage 与 AURC；每题 Jev token 数、LLM token 数、p50 / p90 延迟；重复调用的槽位翻转率。
- **消融**：SELECT / ORDER 用一个 choice 还是逐列 noul；state 用全 schema 还是子 schema；有无 evidence 公式解析器；beam 宽度；有无执行过滤；置信度用原始概率、Platt 还是多元 Platt；无 evidence（内部报告 §4.4：gold 列中 45.9% 的列名原样出现在问题或 evidence 里）。

### 6.3 主要风险与缓解

| 风险 | 依据 | 缓解 |
|---|---|---|
| 误差累积 | §4.5：p = 0.98 时整题全对约 67% | beam + 执行过滤；删去对 EX 无影响的决策（§4.10）；联合校准 |
| Jev 在离散决策上不一定比 LLM 准 | 内部报告：列 F1 87.3 vs 87.3 | 每个槽位都与 B3 对比；卖点放在校准、成本、与 LLM 互补 |
| 算术 / 计数 / 日期是弱项 | 文档；27.1% 的题含算术 | evidence 公式解析器 + 日期模板，计算交给代码 |
| 值匹配 | 字符串值 9.2% 大小写不同、5.5% 不在文本中 | 库值索引 + choice 选值 |
| 覆盖不足 | v1 78.1%，挑战题 58.8% | v2 加单个子查询；其余回退 LLM |
| 标注噪声与版本 | SQL-PaLM 抽查 31% 误判；HF 当前版只有 13 题重合（§4.0） | 固定版本；对分歧题做人工复核 |
| 非确定性 | 第三方最大漂移 0.09；本组翻转 0.46% | 重复 3 次，报告方差 |
| 上下文与选项上限 | 32k token、单个 choice 最多 255 个选项 | 先做 schema linking 再按槽位裁剪 state；超过 255 时分层选 |
| 过拟合 mini-dev | 槽位语法是看着 dev 统计设计的 | 只用 train 设计；单独报告 L1 骨架在 train 中未出现的 47 题 |
| 新颖性被质疑 | SQLNet、RESDSQL、SPC、Zenn | 强调 BIRD 规模、校准、级联成本和决策分类学（§5.1） |

### 6.4 两周 TODO

1. 写 gold SQL ↔ 槽位的反编译 / 编译器（sqlglot），在 498 题上测 round-trip EX。优先级最高，不调 API。
2. 写 evidence 解析器（值映射 → 候选谓词；公式 → 算术模板）。
3. 在 train 上抽样设计问题模板，跑 P1（oracle schema，Jev 与 qwen 逐槽位对比）。
4. 并行做 P2′：选择器 vs 多数投票。
5. 组会汇报：round-trip 覆盖、逐槽位准确率与校准、P2′ 的 EX。

## 参考来源

- 榜单与数据：BIRD LB https://bird-bench.github.io/ ；Spider LB https://yale-lily.github.io/spider ；WikiSQL https://github.com/salesforce/WikiSQL ；BIRD 论文 https://arxiv.org/abs/2305.03111 ；mini-dev https://github.com/bird-bench/mini_dev ，https://huggingface.co/datasets/birdsql/bird_mini_dev ；Spider 2.0 https://arxiv.org/abs/2411.07763 。
- 经典方法：NaLIR https://dbgroup.eecs.umich.edu/files/PVLDB14LF.pdf ；ATHENA http://www.vldb.org/pvldb/vol9/p1209-saha.pdf ；ATHENA++ https://www.vldb.org/pvldb/vol13/p2747-sen.pdf ；Templar https://arxiv.org/abs/1902.00031 ；Finegan-Dollak https://arxiv.org/abs/1806.09029 ；Seq2SQL https://arxiv.org/abs/1709.00103 ；SQLNet https://arxiv.org/abs/1711.04436 ；TypeSQL https://arxiv.org/abs/1804.09769 ；Coarse2Fine https://arxiv.org/abs/1805.04793 ；SQLova https://arxiv.org/abs/1902.01069 ；SyntaxSQLNet https://arxiv.org/abs/1810.05237 ；IRNet https://arxiv.org/abs/1905.08205 ；RAT-SQL https://arxiv.org/abs/1911.04942 ；NatSQL https://arxiv.org/abs/2109.05153 ；SmBoP https://arxiv.org/abs/2010.12412 ；BRIDGE https://arxiv.org/abs/2012.12627 ；PICARD https://arxiv.org/abs/2109.05093 ；RESDSQL https://arxiv.org/abs/2302.05965 ；QPL https://arxiv.org/abs/2310.13575 。
- LLM 时代、约束解码与校准：见 §3.1–3.3 表内链接。另检索到但未展开：CodeS https://arxiv.org/abs/2402.16347 ，DTS-SQL https://arxiv.org/abs/2402.01117 ，E-SQL https://arxiv.org/abs/2409.16751 ，综述 https://arxiv.org/abs/2408.05109 ，https://arxiv.org/abs/2406.01265 。
- Jev：Pydantic AI 集成文档 https://github.com/pydantic/pydantic-ai/blob/main/docs/models/typesafe.md ；ai2sql 实测 https://builder.ai2sql.io/blog/jev-for-sql-use-cases ；Zenn 原型 https://zenn.dev/snaga/articles/2026-10-25-deterministic-text-to-sql-jev-ast ；本组内部报告 `wzy/reports/2026-11-06_jev_schema_linking_report.md`（非公开）。

## 附录 A：统计口径与复现

- 运行：`.venv\Scripts\python.exe notes\scripts\skeleton_stats.py [--train-json <bird23-train-filtered 的 JSON>] [--examples] [--keep-duplicates]`。只打印，不写文件，不调用任何 API。train JSON 通过 HF datasets-server 的 `/rows` 接口分页拉取，放在临时目录，没有放进项目。
- 默认按 question_id 去重（498 题）；`--keep-duplicates` 用原始 500 行。我抽查的主要比例在两种口径下相差不超过约 0.2 个百分点。
- L0：集合运算、WITH、SELECT [DISTINCT] AGG / COL、WHERE、GROUP、HAVING、ORDER、LIMIT1 / LIMITk、SUB:<位置>、WINDOW。子查询位置按嵌套 SELECT 在父 SELECT 中的参数位置判定（where / having → where_scalar，from / joins → from_derived，expressions → select_scalar，另有 IN、EXISTS）。
- L1：L0 + ARITH / ARITH%（含 ×100）、CONDAGG / COND、DATEFN、STRFN、NUMFN。
- L2：去掉别名、CAST 和普通表的 join，列、表、字面值替换为 `_`；L2c 再合并 `_, _` 列表和同形的 AND / OR 谓词链。
- 层：D = 集合运算 / CTE / 窗口 / 深度 ≥ 2 / 多个子查询；C = 恰好一个子查询；B = 有算术、IIF / CASE、日期 / 字符串 / 数值函数或未知函数；其余为 A。
- 字面值：只统计谓词（比较、LIKE、BETWEEN、IN、IS）的直接操作数，不含 JOIN ON 和 LIMIT；LIKE 去掉 `%`；按词边界匹配，分别做区分和不区分大小写两种。
- 外键：在每个 SELECT 作用域内用别名解析出表名，与 `dev_tables.json` 的 foreign_keys 比对；两侧列名相同但没有声明外键的单独计数。
- evidence 公式和值映射用正则识别，属于启发式，只适合看量级。

## 附录 B：未核实条目

- ZeroNL2SQL、PTD-SQL、EXPO-SQL、Progress-SQL 的 BIRD EX；SQL-o1「+10.8 点」的对照对象。
- HES-SQL 79.14%、SQL-R1 67.1% 对应的数据划分。
- 2026-10 之后 mini-dev LB 条目所用的数据版本。
- TypeSafe 官方文档（本次只读到 Pydantic AI 集成文档和第三方实测）。
- Zenn 原型的「0% 语法错误 / 0% 列幻觉 / <200 ms」。
- §5 中每题的问题数、token 数、成本和延迟都是估算，没有实测。
