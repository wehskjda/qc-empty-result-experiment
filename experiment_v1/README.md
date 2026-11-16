# 文档解析停滞：第一版受控回放

这是《数据挖掘技术》课程课题的第一版确定性受控模拟实验。研究停止决策和页面验收信号，不测真实PDF/OCR工具能力，也不使用真实网络或大模型调用。

- `inputs/`：编码前固定的已确认规则、四页参考正文、关键数字、原样故障返回正文、手推预期。
- `input_freeze_manifest.json`：编码前输入文件的SHA-256清单，后续运行必须核对。
- `src/replay_engine.py`：实际正文CER、按字段提取数字、队列轮询和A/B/C状态机。
- `run_experiment.py`：70主条件、独立影子续跑、逐次证据保存、手推对照与公平性审计。
- `tests/test_replay.py`：25项边界及全网格不变量测试；原始通过/失败日志在 `audit_logs/`。
- `build_report.py`：只读取已经保存的实际结果生成完整表与典型过程，不执行策略。
- 原讨论和课程核对位于上一级 `课题讨论/` 与 `课程核对素材/`。

## 第一批实际结果

[完整结果说明及70条件表](reports/20261115T111947_619782Z_e2719d593f82/results.md)包含调用节省、严格重复减少、页面损失和26次影子续跑。另有[70行原始汇总CSV](runs/20261115T111947_619782Z_e2719d593f82/summary.csv)、[42行C与B比较](reports/20261115T111947_619782Z_e2719d593f82/C_vs_B.csv)、[典型逐次过程](reports/20261115T111947_619782Z_e2719d593f82/trace_examples.md)。

70主条件与手推预期逐项一致，910字段对照无差异。26次影子续跑中11个参数条件能在原预算内完成，属于文档错停。主调用共500次，影子另138次。各条件有相同源页但不同参数，不能当作独立随机文档样本外推总体错停率。

## 复现

需要Python 3.10或更新版本，仅用标准库。在本目录运行：

```powershell
python -m unittest discover -s tests -v
python run_experiment.py
python build_report.py --run runs/实际新生成的批次目录
```

当前环境实际使用捆绑Python 3.12。运行器验证冻结输入哈希，每次创建带UTC时间及随机后缀的新目录，不覆盖旧批次。每个主运行先保存初态、逐次JSONL、终态、汇总及摘要，再deepcopy续跑；主影调用分别记录。

报告生成器也拒绝覆盖同名报告目录。输入或结果发生差异时检查该批次的 `error.json`、`differences.json`、`comparison.csv`、`audit.json`；原记录始终保留。正式运行前修正的两处实现边界问题见 [IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md)。

固定网络保护表为HTTP 408、502、503、504，最多两次冻结S，不清零、不补充、不减免调用或方式次数；HTTP 200和成功提示也必须实际验收正文。CER用整数交叉相乘判定不超过1/20，关键数值由标签后提取并用Decimal精确比较，不读取预存通过布尔值。

完整原始聊天记录仍需在最终提交阶段实际导出为Markdown，并上传用户的Public GitHub仓库；此README和实验备忘录都不冒充完整聊天导出。
