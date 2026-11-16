# C1与页面顺序的受控回放扩展

本目录单独保存C1与24种队列顺序的新实验。原v1全部保留，保护清单为baseline_preservation_manifest.json。

inputs中的29个文件已在实现前冻结，包含扩展规则、源页材料与故障快照、24排列清单及2366条手推预期。新实验延续确定性受控模拟，只改变停滞暂停的全页尝试门槛和初始队列顺序。

## 结果与原始记录

- [完整结果说明](reports/20261115T115007_959097Z_c0cd65f16ca5/results.md)
- [原顺序112条件完整表与影子结果](reports/20261115T115007_959097Z_c0cd65f16ca5/canonical_details.md)
- [全部2366主条件CSV](runs/20261115T115007_959097Z_c0cd65f16ca5/summary.csv)
- [24顺序的98组汇总](reports/20261115T115007_959097Z_c0cd65f16ca5/order_aggregate.csv)
- [1008对C1与C的配对差异](reports/20261115T115007_959097Z_c0cd65f16ca5/paired_C1_vs_C.csv)
- [典型逐次过程](reports/20261115T115007_959097Z_c0cd65f16ca5/trace_examples.md)
- [全部1092次影子续跑汇总](reports/20261115T115007_959097Z_c0cd65f16ca5/all_shadow_results.csv)

2366条件与编码前手推的33124项字段比较全部一致，原70条件的1260项回归比较一致；v1原582文件在新运行前后均未改变。37项测试通过。第一次启动曾因测试产生的额外字节码缓存而被输入校验阻止，策略调用为0；原记录与缓存保留，修正后另开新批次，见[实现核查记录](IMPLEMENTATION_NOTES.md)。

同场景、顺序、预算、k的1008对比较中，C1比C多324次主调用、多完成144个页面×参数条件，没有增加严格重复调用。收益全部来自S；L多144次调用而没有新增主页面。C1消除了本实验中“因停滞暂停却留有未尝试源页”的情况，S与R仍有顺序依赖，R/L中的多次修复误停仍存在。上述数字描述构造网格，不能视为随机真实文档的总体统计。

## 代码与复现

需要Python 3.10或更新版本，只使用标准库。在本目录运行：

```powershell
python -m unittest discover -s tests -v
python run_experiment.py
python build_report.py --run runs/新批次目录
```

运行入口为[run_experiment.py](run_experiment.py)，状态机与原样验收逻辑在[src/replay_engine.py](src/replay_engine.py)。每次运行创建独占目录，逐条落盘，禁止覆盖既存批次；报告生成也单独创建目录。校验失败或预期差异保留原始记录，不修改冻结预期。

C1中实际尝试含网络失败；计数S从第一次开始，覆盖完成不清零。每个主运行先封存结果，再deepcopy按B和原剩余预算做影子；主状态及文件哈希、固定源页故障键、同顺序B轨迹前缀与全续跑等审计均保存于批次的audit.json。原顺序112与全顺序2352有98重合条件，因此全批是2366而非两个数相加。

原第一版结果仍在[experiment_v1](../experiment_v1/README.md)。最终课程报告、完整聊天Markdown和Public GitHub仓库属于后续整理提交阶段。
