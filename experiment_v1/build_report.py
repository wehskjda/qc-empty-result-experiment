"""Summarize saved actual replay outputs; never executes a strategy or changes inputs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


NAMES = {"F": "永久失败", "N": "网络恢复", "D": "成功返回旧页", "P": "持续新页",
         "R": "单页延迟修复", "S": "前三页需OCR", "L": "四页第四次才通过"}
REASONS = {"complete": "完成", "budget_exhausted": "预算耗尽",
           "methods_exhausted": "方式耗尽", "stagnation": "停滞暂停"}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def events(run, run_id, phase="main"):
    path = run / "conditions" / run_id / phase / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_csv(path, fields, rows):
    with path.open("x", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def table(headers, rows):
    return "\n".join(["| " + " | ".join(headers) + " |",
                       "|" + "|".join(["---"] * len(headers)) + "|"]
                      + ["| " + " | ".join(str(v) for v in row) + " |" for row in rows])


def trace_table(sequence):
    return table(["阶段", "总调用", "源页", "方式/该方式尝试", "HTTP/错误码", "实际CER",
                  "关键数字", "新页", "S变化", "E变化", "严格重复"], [
        [phase, e["global_call"], e["page_id"], f'{e["method"]}/{e["attempt_n"]}',
         f'{e["raw_response"]["http_status"]}/{e["raw_response"]["code"]}',
         f'{e["validation"]["cer"]:.4%}',
         "全正确" if all(e["validation"]["number_checks"].values()) else "缺失或错误",
         "是" if e["new_progress"] else "否", f'{e["S_before"]}→{e["S_after"]}',
         f'{e["E_before"]}→{e["E_after"]}', "是" if e["repeated_no_progress"] else "否"]
        for phase, e in sequence])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    args = parser.parse_args()
    run = args.run.resolve()
    root = Path(__file__).resolve().parent
    outcome = read_json(run / "run_outcome.json")
    if outcome["status"] != "passed":
        raise ValueError("Do not publish a passed-results report from a failed run")
    with (run / "summary.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    lookup = {row["run_id"]: row for row in rows}
    c_rows = [r for r in rows if r["strategy"] == "C"]
    shadow_rows = [r for r in c_rows if r["shadow_extra_calls"] != ""]
    comparison = read_json(run / "comparison_summary.json")
    audit = read_json(run / "audit.json")
    out = root / "reports" / run.name
    out.mkdir(parents=True, exist_ok=False)
    run_link = "../../runs/" + run.name
    differences = []
    for r in c_rows:
        b = lookup[f'{r["scenario"]}_L{r["budget"]}_B']
        differences.append({
            "run_id": r["run_id"], "scenario": r["scenario"], "budget": r["budget"], "k": r["k"],
            "B_pages": b["completed_pages"], "C_pages": r["completed_pages"],
            "B_calls": b["calls"], "C_calls": r["calls"],
            "calls_saved": int(b["calls"]) - int(r["calls"]),
            "B_repeated_calls": b["repeated_calls"], "C_repeated_calls": r["repeated_calls"],
            "repeated_calls_saved": int(b["repeated_calls"]) - int(r["repeated_calls"]),
            "B_old_page_returns": b["old_page_returns"], "C_old_page_returns": r["old_page_returns"],
            "shadow_extra_calls": r["shadow_extra_calls"],
            "shadow_final_pages": r["shadow_final_pages"], "lost_pages": r["lost_pages"],
            "false_stop": r["false_stop"],
        })
    write_csv(out / "C_vs_B.csv", list(differences[0]), differences)
    counts = {
        "main_conditions": len(rows), "C_conditions": len(c_rows),
        "stagnation_conditions": len(shadow_rows),
        "false_stop_conditions": sum(r["false_stop"] == "true" for r in shadow_rows),
        "main_calls_executed": sum(int(r["calls"]) for r in rows),
        "shadow_calls_executed": sum(int(r["shadow_extra_calls"]) for r in shadow_rows),
        "field_comparisons": comparison["field_comparisons"],
        "expectation_differences": comparison["differences"],
        "audit_passed": audit["passed"],
        "metrics_scope": "constructed_parameter_conditions_not_population_estimates",
    }
    with (out / "analysis.json").open("x", encoding="utf-8") as f:
        json.dump({"counts": counts, "C_vs_B": differences}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    sections = [
        "# 文档解析停滞规则的第一版受控回放结果",
        "C在永久失败与旧页回放中减少了严格重复调用，且没有损失本预算内可挽回的页面；"
        "在延迟修复和困难页排在前面的情形中，较小阈值提前截断了本来能够完成的任务。"
        "k=4使两个原始反例完成，却仍截断四页都需要四次尝试的场景。因此，本版结果支持把该规则看作成本与完成机会的权衡，不能据此把k=4当成通用安全值。",
        "本批次是确定性受控模拟：没有真实PDF解析、OCR、网络或大模型请求。"
        "实际测量对象是冻结故障返回下的程序行为。静态尾部字符替换表示修复质量逐步改善，不代表真实修复器的学习过程。",
        "## 材料与核对结果",
        f"七场景、两预算，A/B各一组与C的三个k，共{len(rows)}个主条件；"
        f"{len(shadow_rows)}个停滞条件分别做独立影子续跑。"
        f"实际与编码前手推预期的{comparison['field_comparisons']}项字段比较全部一致，差异为0。"
        "正式批次无需修订手推预期。正式运行前的边界测试曾发现两处实现缺陷，已修正并保留失败日志，见下文。",
        f"主运行累计执行{counts['main_calls_executed']}次，影子另执行{counts['shadow_calls_executed']}次，"
        f"合计{audit['counts']['events_checked']}次。影子成本和页面不计入C主结果。"
        "输入14文件哈希全部匹配；42个C动作轨迹均为对应B的前缀，C加影子均还原B完整动作轨迹；"
        "主状态与已落盘文件在续跑后均未改变。",
        f"原始批次：[运行结果]({run_link}/run_outcome.json)、[全部70行CSV]({run_link}/summary.csv)、"
        f"[逐字段对照]({run_link}/comparison.csv)、[零差异记录]({run_link}/differences.json)、"
        f"[公平性与隔离审计]({run_link}/audit.json)。",
        "## 主运行完整结果",
        "下表每个单元格为“完成页数/调用次数”，每份文档目标都是4页。"
        "F为p1永久失败，N为两次网络失败后恢复，D为p2成功状态却返回p1，P为四页首试成功，"
        "R为p4第四次修复才通过，S为前三页需OCR，L为四页均第四次才通过。",
    ]
    configs = [("A", ""), ("B", ""), ("C", "2"), ("C", "3"), ("C", "4")]
    for budget in (12, 20):
        compact = []
        for scenario, name in NAMES.items():
            row = [f"{scenario} {name}"]
            for strategy, k in configs:
                rid = f"{scenario}_L{budget}_{strategy}" + (f"_k{k}" if k else "")
                r = lookup[rid]
                row.append(f'{r["completed_pages"]}/{r["calls"]}')
            compact.append(row)
        sections += [f"### 总预算{budget}次", table(["场景", "A", "B", "C k=2", "C k=3", "C k=4"], compact)]
    sections += [
        "## 严格重复调用与调用总数的区别",
        "严格重复要求同一源页、相同方式和实际源输入，返回相同结构化状态码、错误码和规范化正文，且无新增页。"
        "尝试编号、错误自由文案和工具别名不产生新状态。首次尝试一种新方式不算严格重复；"
        "实际正文逐次改善的R/L也不算严格重复。旧页返回另计，与严格重复可重叠，二者不能相加。",
        "F和D在两种预算下相同：B均用11次，其中6次严格重复。C的k=2/3/4分别用6/7/8次，"
        "严格重复为2/3/3次。因此调用总数少5/4/3次，严格重复少4/3/3次；"
        "C与B均完成3页，影子也不能增加页面。这里可以具体指出减少了重复工作，而不是仅因少产出而省调用。",
        table(["场景及预算", "k", "B→C调用", "少调用", "B→C严格重复", "少重复", "可挽回页"], [
            ["F、D；12或20", k, f"11→{c}", 11-c, f"6→{rep}", 6-rep, 0]
            for k, c, rep in [(2, 6, 2), (3, 7, 3), (4, 8, 3)]]),
        "D中B共8次返回旧页，C为3/4/5次。即使返回HTTP 200、code=OK并声称p2，"
        "也用p2参考验收其正文：第一次错页的实际CER为60/121=49.5868%，提取的深度12、水温23.4、盐度31.8均不匹配p2。"
        "这些调用没有通过验收，也没有产生新页面。",
        "## 省调用却损失完成机会的情形",
        "R、S、L中B与C的严格重复调用均为0。这里不能把C减少的调用写成减少了重复无效调用。",
        table(["场景", "预算", "k", "相对B少调用", "影子额外调用", "影子恢复页", "文档错停"], [
            [d["scenario"], d["budget"], d["k"], d["calls_saved"], d["shadow_extra_calls"],
             d["lost_pages"], "是" if d["false_stop"] == "true" else "否"]
            for d in differences if d["scenario"] in ("R", "S", "L") and d["shadow_extra_calls"] != ""]),
        "R在前三页完成后，p4的CER依次为59.8361%、35.2459%、14.7541%、1.6393%，关键数字一直正确。"
        "k=2时只看到前两次就暂停，主运行停在3页/5次；独立影子再做同方式第3、4次，累计7次完成4页。"
        "k=3只差最后一次；k=4先完成验收并清零，正常完成。这是二值页面信号看不见部分修复改善的后果。",
        "S在前三页第一次T调用后已各自安排下次转OCR，但C的全局停滞计数不因切方式清零。"
        "k=2/3分别在前两/三个失败后暂停，第四页尚未尝试。B在第4次先完成p4，再在第5—7次用OCR完成前三页。"
        "影子从相同队列继续即可恢复全部4页。k=4能等到第4次新增并完成；A始终T只完成p4，"
        "A与B的差异同时包含错误路由和重试上限，不单归因于停滞。",
        "L的前12次是四页各三次，CER从约20%改善到约10%、再到5.7377%—5.8824%，均严格大于5%；"
        "第13—16次才逐页通过。预算12时影子也不能增加页面，故不算本预算内文档错停；"
        "预算20时三个k都可由影子恢复4页。因此，同一暂停轨迹的错停判定会随允许的剩余预算改变。",
        "这些构造条件共出现11个文档错停：R占4个、S占4个、L在预算20下占3个。"
        "分母分别为42个C参数条件或26个暂停条件，即11/42与11/26；它们是本构造网格的描述，"
        "不是独立随机文档的总体错停概率。",
        "## 网络保护与验收边界",
        "网络保护固定允许HTTP 408、502、503、504，其余不因错误文案而保护。"
        "N的两次网络失败分别为503和504，实际都消耗调用及p1/T次数，E从2减至0；"
        "六次调用内四页完成。此用例的失败前S恰为0，因此还用独立边界测试核验非零冻结："
        "普通失败→503→504→普通失败得到S=1→1→1→2，没有清零。第三次网络失败开始增加S，方式上限仍生效。",
        "25项实现测试覆盖CER恰好5%、低CER但关键数字错误、字段错配与重复字段、HTTP成功旧页、"
        "状态码允许表、网络冻结与耗尽、方式第四次、预算同拍停止及影子隔离。"
        "曾发现的两处边界缺陷为合法同文异源页误标旧页、畸形重复字段未被计入唯一性检查；"
        "两者已在正式运行前修正，原始失败输出和修正后通过日志均保留于[审计日志](../../audit_logs/)，"
        "详见[实现核查记录](../../IMPLEMENTATION_NOTES.md)。未发生正式70条件与手推表不一致。",
        "## 全部70条件的计数与停止原因",
        "无新增页调用包含网络失败和必要修复步骤，不能自动称为浪费。下表由实际summary生成；"
        "每条的原始正文、字段提取、CER、队列、尝试次数和S/E均可在对应events.jsonl查阅。",
    ]
    for budget in (12, 20):
        full = []
        for r in rows:
            if int(r["budget"]) != budget:
                continue
            label = r["strategy"] + (f' k={r["k"]}' if r["k"] else "")
            full.append([r["scenario"], label, r["completed_pages"], r["calls"],
                         r["no_progress_calls"], r["repeated_calls"], r["old_page_returns"],
                         f'{r["network_failures"]}/{r["protected_network_failures"]}',
                         REASONS[r["stop_reason"]]])
        sections += [f"### 预算{budget}的35个条件", table(
            ["场景", "策略", "页数", "调用", "无新页", "严格重复", "旧页", "网络/保护", "停止原因"], full)]
    sections += ["## 全部26次影子续跑",
                 "主页数和主调用在影子前已单独落盘并封存；续跑使用deepcopy，下面的最终页数仅属于影子。",
                 table(["场景", "预算", "k", "主页/调用", "影子额外调用", "影子最终页/累计调用", "损失页", "文档错停"], [
                     [r["scenario"], r["budget"], r["k"], f'{r["completed_pages"]}/{r["calls"]}',
                      r["shadow_extra_calls"], f'{r["shadow_final_pages"]}/{r["shadow_total_calls"]}',
                      r["lost_pages"], "是" if r["false_stop"] == "true" else "否"] for r in shadow_rows]),
                 "## 可复现文件",
                 f"[逐条C与B比较](C_vs_B.csv)包含全部42个C条件；[典型逐次过程](trace_examples.md)展示主影边界。"
                 "运行入口为[run_experiment.py](../../run_experiment.py)，验收与状态机在"
                 "[replay_engine.py](../../src/replay_engine.py)。标准库即可运行，无需外部API。"
                 "重跑会创建新的独占目录，不覆盖本批证据；输入哈希不匹配时停止。",
                 "这份结果说明用于下一轮讨论，不是最终课程实验报告。第一版没有加入首轮覆盖保护，也没有根据结果改动k。"
                 "完整聊天Markdown、Public GitHub仓库链接和最终创新点拆解仍属于后续整理报告阶段。"]
    (out / "results.md").write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    traces = ["# 典型条件的逐次运行记录", "以下内容直接摘自已保存events.jsonl；主与影子分开显示。"
              "影子日志中的S/E仅作诊断，guard_enabled已关闭，不再用于暂停。"]
    for run_id in ("F_L12_C_k2", "D_L12_C_k4", "N_L12_C_k2", "R_L20_C_k2",
                   "S_L20_C_k2", "L_L12_C_k4", "L_L20_C_k4"):
        sequence = [("主", e) for e in events(run, run_id)]
        shadow_path = run / "conditions" / run_id / "shadow" / "events.jsonl"
        if shadow_path.exists():
            sequence += [("影子", e) for e in events(run, run_id, "shadow")]
        traces += ["## " + run_id.replace("_", " "), trace_table(sequence),
                   f'[主原始日志]({run_link}/conditions/{run_id}/main/events.jsonl)'
                   + (f'；[影子原始日志]({run_link}/conditions/{run_id}/shadow/events.jsonl)' if shadow_path.exists() else "")]
    (out / "trace_examples.md").write_text("\n\n".join(traces) + "\n", encoding="utf-8")
    artifacts = {}
    for path in out.iterdir():
        if path.is_file():
            artifacts[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (out / "report_manifest.json").write_text(json.dumps({
        "source_run": run.name,
        "source_summary_sha256": hashlib.sha256((run / "summary.csv").read_bytes()).hexdigest(),
        "build_report_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "artifacts": artifacts,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report_directory": str(out), **counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
