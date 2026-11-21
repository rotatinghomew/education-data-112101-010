# 风险复核资源配置与影响面覆盖

内容治理复核资源的可重复分析工具：从线索资料到建议队列、执行反馈再到下一轮
分析的完整闭环。输出为**资源分配建议**，不替代人工处置；已启动的核查保留
负责人与理由，未启动的安排在新证据、主体关系更正或结论变化时自动重新评估。

`data/` 为合成数据，标识符不对应真实个人、机构或账号。空字段表示资料未
提供，不代表数值为零或业务状态已否定。

## 运行要求

Python 3.11+，无第三方依赖。

```bash
python3 -m unittest discover -s tests   # 检查资料与全部行为测试
```

## 快速上手

```bash
# 1. 生成下一周期建议队列（容量沿用最近模板期），输出 Markdown + JSON
python3 -m review_alloc analyze --period 2026-W13 --capacity-from 2026-W12 \
    --out out --record

# 2. 团队按建议核查，录入执行反馈
python3 -m review_alloc record-review --review R-40 --signal S-4 \
    --state in_progress --assignee T-3 --reason "影响面核查"
python3 -m review_alloc record-review --review R-40 --state closed \
    --outcome confirmed --reason "确认违规"

# 3. 主体关系更正（例如同名核实不成立）
python3 -m review_alloc record-relation --left M-1 --right M-3 \
    --relation same_name --state rejected

# 4. 下一轮分析自动吸收反馈：已确认线索不再占位，进行中核查保留负责人
python3 -m review_alloc analyze --period 2026-W14 --capacity-from 2026-W12 --out out

# 5. 比较不同容量下的覆盖与遗漏风险
python3 -m review_alloc scenarios --period 2026-W13 --capacity-from 2026-W12 \
    --scales 0.5,1,1.5,2 --out out
```

`analyze --record` 把建议队列写入 `decisions.csv`；同一周期重复执行会整体
替换本工具生成的记录（编号 `A-{周期}-{线索}`），保证可重复运行。

## 数据文件

- `data/signals.csv`：线索（主体、场次、类别、可信度、影响面分档、来源）。
- `data/relationships.csv`：主体关系；`rejected` 的关系不参与经营链归并。
- `data/reviews.csv`：核查记录（待补证 / 进行中 / 已关闭，含结论与理由）。
- `data/decisions.csv`：历史安排决策（安排 / 暂缓 / 排除，含理由）。
- `data/capacity.csv`：各团队每周名额与专长；同队同周期多行按多批授予累加。

## 方法

### 线索状态判定

每条线索按最新核查记录归为：已确认 / 已排除（了结，不再占位）、进行中
（锁定）、待补证、新线索（后两类进入候选）。**结论字段优先**：核查给出
confirmed / excluded 结论即视为了结，状态字段滞后不影响判定。

### 经营链归集

`state != rejected` 的主体关系把主体归并为经营链（并查集，链标识取最小
主体编号，结果可重复）。同名核实不成立等更正会拆开经营链，下一轮自动按
更正后的关系重新归集。

### 可解释评分（0–100）

| 分量 | 默认权重 | 说明 |
| --- | --- | --- |
| 可信度 | 0.40 | 缺失按中性 0.5 计并标注，不当作 0 |
| 影响面 | 0.25 | wide 1.0 / medium 0.6 / unknown 0.3 |
| 资料完整性 | 0.15 | 关键字段齐备比例，缺项写入说明 |
| 经营链风险 | 0.15 | 链内已确认 0.6+0.1/条（封顶 1.0）；进行中 0.4；待补证 0.2 |
| 新颖度 | 0.05 | 主体首次进入视野（该主体无任何核查或安排记录） |
| 证据待补 | −10 分 | 待补证线索减分，但仍可入队 |

每条建议都带分数构成与文字理由；权重可用 `--config` 的 JSON 覆盖
（`{"weights": {...}, "allocation": {...}}`）。

### 分配约束

- **主体上限**（默认每场每主体 1 条）：同一主体的重复场次不占满队列；
- **经营链上限**（默认每链 4 条）：防止单一经营链挤占全部名额；
- **类别覆盖保留**：每个活跃类别的最高分候选优先占位，低频但影响面广的
  新问题不会被高分集中风险挤掉；
- **团队匹配**：类别→专长映射（sales_pressure/renewal_claim→sales，
  credential_question→credentials），无匹配时通用团队承接并注明。

未入队候选逐条记录原因（主体上限 / 链上限 / 名额不足），出现在报告的
“未入队候选”与 JSON 中，不会在汇总里消失，下一轮自动重新评估。

### 锁定与反馈

- 进行中的核查锁定：保留负责人与启动理由，不重新评分、不重新指派；
  `record-review` 改派已启动核查的负责人需 `--force`。
- 已确认 / 已排除的线索不再占位，但保留在“已了结”汇总中；链上其余
  未解决线索通过经营链风险继续可见。
- 已安排但未启动的线索（历史决策有、核查未启动）下一轮自动重新评估。

### 覆盖与遗漏指标

报告与 JSON 给出：候选 / 入队 / 暂缓 / 锁定 / 了结数量，类别与经营链
覆盖比例，影响面 wide 覆盖比例，以及遗漏提示（wide 未入队、链内已有
确认但新线索未入队、类别零覆盖、新主体被暂缓）。`scenarios` 在不同容量
系数下重复完整分析，便于负责人比较覆盖与遗漏风险的此消彼长。

## 项目结构

```
review_alloc/
  loader.py     读取与校验合成资料，容量按周期汇总
  analysis.py   经营链归集、状态判定、可解释评分
  allocator.py  容量分配、覆盖与遗漏指标
  feedback.py   核查反馈 / 关系更正 / 决策回写
  scenarios.py  容量情景对比
  report.py     Markdown 与 JSON 报告
  cli.py        命令行入口
tests/          45 个单元与端到端测试
```

## 假设与边界

- 容量按“新安排名额”理解；进行中的核查单列，不占用本期新名额。
- `capacity.csv` 同队同周期多行视为多批授予，名额累加。
- 目标周期无容量记录时，用 `--capacity-from` 指定模板期并在报告中注明。
- 建议不构成处置结论；人工可随时偏离建议，工具只要求反馈如实录入，
  供下一轮分析吸收。
