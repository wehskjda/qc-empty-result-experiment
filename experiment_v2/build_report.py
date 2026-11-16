"""Read saved C1/order replay results and build reports without running policies."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path


SCENARIOS = {
    "F": "一页永久失败", "N": "两次网络失败恢复", "D": "成功返回旧页",
    "P": "持续新增页面", "R": "单页第四次通过", "S": "前三页需OCR",
    "L": "四页均第四次通过",
}
CONFIGS = [("B", ""), ("C", "2"), ("C", "3"), ("C", "4"),
           ("C1", "2"), ("C1", "3"), ("C1", "4")]
REASONS = {"complete": "完成", "budget_exhausted": "预算耗尽",
           "methods_exhausted": "方式耗尽", "stagnation": "停滞暂停"}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def integer(row: dict, key: str, default: int = 0) -> int:
    return int(row[key]) if row.get(key, "") != "" else default


def csv_file(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise ValueError(f"Refuse to create a table without column definitions: {path}")
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def json_file(path: Path, value) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")


def markdown_table(headers, rows):
    def safe(value):
        return str(value).replace("|", "/").replace("\n", " ")
    return "\n".join([
        "| " + " | ".join(map(safe, headers)) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
        *("| " + " | ".join(map(safe, row)) + " |" for row in rows),
    ])


def label(strategy: str, k: str) -> str:
    return strategy + (f" k={k}" if k else "")


def cell(row: dict) -> str:
    return f'{row["completed_pages"]}/{row["calls"]}'


def range_string(values) -> str:
    values = list(values)
    return str(min(values)) if min(values) == max(values) else f"{min(values)}—{max(values)}"


def aggregate(rows: list[dict]) -> dict:
    if not rows:
        raise ValueError("Empty aggregation group")
    return {
        "conditions": len(rows),
        "completed_documents": sum(integer(r, "completed_pages") == 4 for r in rows),
        "stagnation_conditions": sum(r["stop_reason"] == "stagnation" for r in rows),
        "false_stop_conditions": sum(r["false_stop"] == "true" for r in rows),
        "pages_min": min(integer(r, "completed_pages") for r in rows),
        "pages_max": max(integer(r, "completed_pages") for r in rows),
        "pages_mean": sum(integer(r, "completed_pages") for r in rows) / len(rows),
        "calls_min": min(integer(r, "calls") for r in rows),
        "calls_max": max(integer(r, "calls") for r in rows),
        "calls_mean": sum(integer(r, "calls") for r in rows) / len(rows),
        "calls_total": sum(integer(r, "calls") for r in rows),
        "strict_repeats_total": sum(integer(r, "repeated_calls") for r in rows),
        "old_page_returns_min": min(integer(r, "old_page_returns") for r in rows),
        "old_page_returns_max": max(integer(r, "old_page_returns") for r in rows),
        "stagnation_with_unattempted_pages": sum(
            r["stop_reason"] == "stagnation" and integer(r, "unattempted_count") > 0 for r in rows),
        "unattempted_pages_at_stagnation_total": sum(
            integer(r, "unattempted_count") for r in rows if r["stop_reason"] == "stagnation"),
        "shadow_conditions": sum(r["shadow_extra_calls"] != "" for r in rows),
        "shadow_extra_calls_total": sum(integer(r, "shadow_extra_calls") for r in rows),
        "shadow_recovered_pages_total": sum(integer(r, "lost_pages") for r in rows),
        "deferred_stagnation_events_total": sum(integer(r, "deferred_stagnation_events") for r in rows),
    }


def paired_rows(rows: list[dict]) -> list[dict]:
    lookup = {(r["scenario"], r["order_id"], r["budget"], r["strategy"], r["k"]): r for r in rows}
    pairs = []
    for c1 in rows:
        if c1["strategy"] != "C1":
            continue
        c = lookup[(c1["scenario"], c1["order_id"], c1["budget"], "C", c1["k"])]
        pairs.append({
            "scenario": c1["scenario"], "order_id": c1["order_id"],
            "budget": c1["budget"], "k": c1["k"],
            "C_run_id": c["run_id"], "C1_run_id": c1["run_id"],
            "C_pages": c["completed_pages"], "C1_pages": c1["completed_pages"],
            "pages_gained": integer(c1, "completed_pages") - integer(c, "completed_pages"),
            "C_calls": c["calls"], "C1_calls": c1["calls"],
            "extra_main_calls": integer(c1, "calls") - integer(c, "calls"),
            "C_strict_repeats": c["repeated_calls"], "C1_strict_repeats": c1["repeated_calls"],
            "extra_strict_repeats": integer(c1, "repeated_calls") - integer(c, "repeated_calls"),
            "C_unattempted": c["unattempted_count"], "C1_unattempted": c1["unattempted_count"],
            "C_stop_reason": c["stop_reason"], "C1_stop_reason": c1["stop_reason"],
            "C_shadow_calls": c["shadow_extra_calls"], "C1_shadow_calls": c1["shadow_extra_calls"],
            "C_shadow_recovered_pages": c["lost_pages"], "C1_shadow_recovered_pages": c1["lost_pages"],
            "C_false_stop": c["false_stop"], "C1_false_stop": c1["false_stop"],
        })
    return pairs


def read_events(run: Path, row: dict, phase: str = "main") -> list[dict]:
    key = "main_trace_path" if phase == "main" else "shadow_trace_path"
    if not row[key]:
        return []
    path = run / row[key]
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def trace_table(run: Path, row: dict) -> str:
    sequence = [("主", e) for e in read_events(run, row)]
    sequence += [("影子", e) for e in read_events(run, row, "shadow")]
    visited, lines = set(), []
    for phase, e in sequence:
        visited.add(e["page_id"])
        lines.append([
            phase, e["global_call"], e["page_id"], f'{e["method"]}/{e["attempt_n"]}',
            e["raw_response"]["http_status"], f'{e["validation"]["cer"]:.4%}',
            "全正确" if all(e["validation"]["number_checks"].values()) else "缺失或错误",
            "是" if e["new_progress"] else "否", len(visited),
            f'{e["S_before"]}→{e["S_after"]}', f'{e["E_before"]}→{e["E_after"]}',
            "是" if e["repeated_no_progress"] else "否",
        ])
    return markdown_table(["阶段", "总调用", "固定源页", "方式/尝试号", "HTTP", "实际CER",
                           "关键数字", "新增页", "已尝试源页数", "S变化", "E变化", "严格重复"], lines)


def build(run: Path, root: Path) -> Path:
    outcome = read_json(run / "run_outcome.json")
    if outcome["status"] != "passed":
        raise ValueError("A passed-results report requires a passed source run")
    rows = read_csv(run / "summary.csv")
    canonical = [r for r in rows if r["order_id"] == "1234"]
    order_rows = [r for r in rows if r["strategy"] in ("B", "C", "C1")]
    if len(rows) != 2366 or len(canonical) != 112 or len(order_rows) != 2352:
        raise ValueError("Unexpected source grid; report refuses to silently change its denominator")
    if len({r["run_id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate source run ID")
    lookup = {r["run_id"]: r for r in rows}
    comparison = read_json(run / "comparison_summary.json")
    regression = read_json(run / "regression_v1_70.json")
    audit = read_json(run / "audit.json")
    before = read_json(run / "baseline_integrity_before.json")
    after = read_json(run / "baseline_integrity_after.json")
    if not all((audit["passed"], regression["passed"], before["passed"], after["passed"])):
        raise ValueError("Source audit, v1 regression or baseline preservation did not pass")
    out = root / "reports" / run.name
    out.mkdir(parents=True, exist_ok=False)
    link = f"../../runs/{run.name}"
    pairs = paired_rows(order_rows)
    if len(pairs) != 1008:
        raise ValueError("Expected one C1/C pair for each scenario/order/budget/k")
    csv_file(out / "paired_C1_vs_C.csv", pairs)
    canon_pairs = [r for r in pairs if r["order_id"] == "1234"]
    csv_file(out / "canonical_C1_vs_C.csv", canon_pairs)
    groups = defaultdict(list)
    for row in order_rows:
        groups[(row["scenario"], row["budget"], row["strategy"], row["k"])].append(row)
    aggregate_rows = []
    for scenario in SCENARIOS:
        for budget in ("12", "20"):
            for strategy, k in CONFIGS:
                members = groups[(scenario, budget, strategy, k)]
                if len(members) != 24:
                    raise ValueError("Order group does not contain all 24 permutations")
                aggregate_rows.append({"scenario": scenario, "budget": budget,
                                       "strategy": strategy, "k": k, **aggregate(members)})
    csv_file(out / "order_aggregate.csv", aggregate_rows)
    position_rows = []
    for scenario in ("S", "R"):
        for budget in ("12", "20"):
            for strategy, k in CONFIGS:
                for position in range(1, 5):
                    members = [r for r in groups[(scenario, budget, strategy, k)]
                               if r["order_id"].index("4") + 1 == position]
                    if len(members) != 6:
                        raise ValueError("Fixed-p4-position group must contain six orders")
                    states = sorted({f'{r["completed_pages"]}/{r["calls"]}' for r in members})
                    position_rows.append({"scenario": scenario, "budget": budget,
                                          "strategy": strategy, "k": k, "p4_initial_position": position,
                                          "page_call_outcomes": " ; ".join(states), **aggregate(members)})
    csv_file(out / "S_and_R_by_p4_position.csv", position_rows)
    all_shadows = [r for r in rows if r["shadow_extra_calls"] != ""]
    csv_file(out / "all_shadow_results.csv", [{key: r[key] for key in (
        "run_id", "scenario", "order_id", "strategy", "budget", "k", "completed_pages", "calls",
        "shadow_extra_calls", "shadow_final_pages", "shadow_total_calls", "lost_pages", "false_stop",
        "main_trace_path", "shadow_trace_path")} for r in all_shadows])
    summary_by_strategy = {s: aggregate([r for r in order_rows if r["strategy"] == s])
                           for s in ("B", "C", "C1")}
    counts = {
        "unique_main_conditions": len(rows), "canonical_conditions": len(canonical),
        "order_conditions": len(order_rows), "paired_C1_C_conditions": len(pairs),
        "canonical_order_overlap": len([r for r in canonical if r["strategy"] != "A"]),
        "main_calls_executed": sum(integer(r, "calls") for r in rows),
        "shadow_conditions": len(all_shadows),
        "shadow_extra_calls_executed": sum(integer(r, "shadow_extra_calls") for r in all_shadows),
        "C1_extra_main_calls": sum(p["extra_main_calls"] for p in pairs),
        "C1_pages_gained": sum(p["pages_gained"] for p in pairs),
        "C1_extra_strict_repeats": sum(p["extra_strict_repeats"] for p in pairs),
        "pair_conditions_more_pages": sum(p["pages_gained"] > 0 for p in pairs),
        "pair_conditions_more_calls_no_more_pages": sum(
            p["extra_main_calls"] > 0 and p["pages_gained"] == 0 for p in pairs),
        "scope": "exhaustive_constructed_orders_and_parameter_conditions_not_random_documents",
    }
    scenario_pairs = []
    for scenario in SCENARIOS:
        members = [p for p in pairs if p["scenario"] == scenario]
        scenario_pairs.append({"scenario": scenario, "pairs": len(members),
                               "extra_calls": sum(p["extra_main_calls"] for p in members),
                               "pages_gained": sum(p["pages_gained"] for p in members),
                               "extra_repeats": sum(p["extra_strict_repeats"] for p in members),
                               "more_calls_without_more_pages": sum(
                                   p["extra_main_calls"] > 0 and p["pages_gained"] == 0 for p in members)})
    json_file(out / "analysis.json", {"counts": counts, "order_strategy_summary": summary_by_strategy,
                                     "scenario_pair_totals": scenario_pairs,
                                     "comparison": comparison, "v1_regression": regression,
                                     "baseline_before": before, "baseline_after": after})

    c, c1 = summary_by_strategy["C"], summary_by_strategy["C1"]
    lines = [
        "# C1首轮尝试覆盖与页面顺序实验结果",
        "C1把“至少尝试过全部源页”作为停滞暂停的前提，能够挽回原S顺序中尚未尝试就被放弃的页面。"
        "它保留从第一次调用开始的S计数，首轮完成不清零，因此仍可能在四页首试结束后立即暂停；"
        "同页延迟修复也仍会被截断。下面的数值均从本批实际summary与逐次日志读取。",
        "本轮延续确定性受控故障回放，T/O为模拟接口，不是真实PDF、OCR、网络或大模型性能测试。"
        "只改变初始队列与C1的停滞生效门槛，源页正文、关键数字、故障和恢复尝试序号均绑定原始页身份。",
        "## 实验范围与核对",
        f"共{counts['unique_main_conditions']}个唯一主条件：七场景×24顺序×两预算×"
        "（B一组、C三个k、C1三个k），再加原顺序的A回归条件。"
        f"原顺序分析包含{counts['canonical_conditions']}条件，全部顺序分析包含{counts['order_conditions']}条件，"
        f"两者有{counts['canonical_order_overlap']}个重合条件，不能把两个分组相加当作独立样本数。",
        f"原70条件回归通过；全条件与预先冻结的手推表逐字段比较，差异为{comparison['differences']}。"
        f"v1运行前、运行后保护核查均通过。新增规则和预期见[冻结规则](../../inputs/confirmed_extension.md)"
        "与[编码前手推](../../inputs/manual_expectations.md)。",
        f"主运行累计{counts['main_calls_executed']}次，{counts['shadow_conditions']}次影子续跑另执行"
        f"{counts['shadow_extra_calls_executed']}次；主影成果和成本分别记账。"
        "主结果先落盘并封存，再deepcopy做原预算内的B续跑；主状态隔离、同键同返回及同顺序B轨迹审计通过。",
        f"原始文件：[全部主条件]({link}/summary.csv)、[手推核对]({link}/comparison_summary.json)、"
        f"[原70回归]({link}/regression_v1_70.json)、[审计]({link}/audit.json)、"
        f"[原实验保护]({link}/baseline_integrity_after.json)。",
        "## 原顺序中的C与C1",
        "单元格为“完成页数/主调用次数”，四页为完成目标。原顺序始终是p1→p2→p3→p4。"
        "A/B/C/C1的全部112行及影子结果见[原顺序完整表](canonical_details.md)，"
        "逐对差异见[42行原顺序比较](canonical_C1_vs_C.csv)。",
    ]
    for budget in ("12", "20"):
        display = []
        for scenario, name in SCENARIOS.items():
            item = [f"{scenario} {name}"]
            for k in ("2", "3", "4"):
                cr = lookup[f"{scenario}_o1234_L{budget}_C_k{k}"]
                nr = lookup[f"{scenario}_o1234_L{budget}_C1_k{k}"]
                item.append(f"{cell(cr)} → {cell(nr)}")
            display.append(item)
        lines += [f"### 预算{budget}次", markdown_table(["场景", "k=2 C→C1", "k=3 C→C1", "k=4 C→C1"], display)]
    lines += [
        "S的原顺序把固定易页p4放在最后。C1延后停滞判断，允许第4次实际访问p4；"
        "它通过验收后才使S归零，随后前三页各一次OCR完成。这里挽回页面依靠新的验收成果，"
        "没有在首轮结束时额外清零，也没有新增预算或放宽验收。",
        "## 全部顺序中的停滞与未尝试页面",
        markdown_table(["策略", "参数条件", "完整完成", "停滞暂停", "影子证明文档错停", "停滞时仍有未尝试页的条件"], [
            [s, a["conditions"], a["completed_documents"], a["stagnation_conditions"],
             a["false_stop_conditions"], a["stagnation_with_unattempted_pages"]]
            for s, a in summary_by_strategy.items()]),
        f"C的{c['stagnation_with_unattempted_pages']}个停滞参数条件还留有从未实际尝试的源页，"
        f"C1的此类条件为{c1['stagnation_with_unattempted_pages']}。这直接支持新增门槛缓解了"
        "“尚未尝试的页被一起放弃”。它只约束停滞原因；总预算仍可在全页覆盖前耗尽，"
        "因此不能外推为任意小预算下都保证尝试全部页。",
        f"全部顺序中的C1相对C多完成{counts['C1_pages_gained']}个“页面×参数条件”，"
        f"增加{counts['C1_extra_main_calls']}次主调用，增加的严格重复为{counts['C1_extra_strict_repeats']}次。"
        "每个原文档以不同顺序、预算与k反复出现，这些不是同等数量的独立真实文档或页面。",
        markdown_table(["场景", "配对条件", "增加主调用", "多完成页×条件", "新增严格重复", "多调用但无新页的条件"], [
            [r["scenario"], r["pairs"], r["extra_calls"], r["pages_gained"], r["extra_repeats"],
             r["more_calls_without_more_pages"]] for r in scenario_pairs]),
        "追加调用不自动等于新增有效内容，也不自动等于重复浪费：例如首轮尚未尝试的困难页会增加一次诊断调用，"
        "但未必给后续OCR留下机会。严格重复、没有新页和页面损失分别记录，不能把它们混成一个指标。",
        "[1008行逐对比较](paired_C1_vs_C.csv)保留同场景、顺序、预算、k下的C与C1；"
        "[98组顺序汇总](order_aggregate.csv)分别列出每组24排列的完成数、错停数、页数和调用范围。",
        "## S仍然受易页位置影响",
        "p4始终是容易页，p1/p2/p3始终需要OCR；改变的是队列位置，未将困难身份换给别的页。"
        "每个p4位置对应另外三页的6个排列。下表为“完成四页的排列数/6”，按实际结果分组。",
    ]
    for budget in ("12", "20"):
        display = []
        for position in range(1, 5):
            item = [position]
            for strategy, k in CONFIGS:
                matches = [r for r in position_rows if r["scenario"] == "S" and r["budget"] == budget
                           and r["strategy"] == strategy and r["k"] == k
                           and r["p4_initial_position"] == position]
                item.append(f'{matches[0]["completed_documents"]}/{matches[0]["conditions"]}')
            display.append(item)
        lines += [f"### S预算{budget}次", markdown_table(
            ["p4位置", "B", "C k2", "C k3", "C k4", "C1 k2", "C1 k3", "C1 k4"], display)]
    lines += [
        "当p4排在最后，前三次首试无新增，但C1暂缓停滞；第四次p4通过，使S归零，后续OCR成功接续产生新页。"
        "当p4最先通过，之后三页首试连续失败；覆盖完成时S仍保留此前累计值，C1可以立即暂停，"
        "三个困难页虽然都试过，却尚未得到OCR重试。这解释了为什么C1修复了未尝试页问题，却没有消除全部顺序影响。",
        "具体序列见[典型逐次过程](trace_examples.md)。两预算下的S行为可直接比较；"
        "若预算都足够完成7次调用，增加总预算本身不能取消停滞守卫。",
        "## R与L显示改动的作用边界",
        "R只有固定p4需要四次同方式修复。原C发生停滞时已经尝试全部源页，所以覆盖门槛通常不会再延迟它。"
        "如果p4靠前，后续正常页成功可以重置S；如果p4最后，连续修复的计数更早开始，顺序仍可能影响结果。",
        markdown_table(["R预算", "策略", "k", "24顺序完成数", "页数范围", "调用范围", "错停数"], [
            [r["budget"], r["strategy"], r["k"], f'{r["completed_documents"]}/{r["conditions"]}',
             f'{r["pages_min"]}—{r["pages_max"]}', f'{r["calls_min"]}—{r["calls_max"]}', r["false_stop_conditions"]]
            for r in aggregate_rows if r["scenario"] == "R"]),
        "L四个源页都需要第四次尝试。C1首次覆盖时没有任何验收新页，S不会因覆盖完成而归零；"
        "所以它仍可能直接暂停。预算12的B本就无法在原预算内完成，预算20的影子则有足够机会等到第四次尝试。"
        "这一区别由剩余原预算决定，不能用额外补预算把原本不可完成的条件判成错停。",
        markdown_table(["L预算", "策略", "k", "24顺序完成数", "页数范围", "调用范围", "错停数"], [
            [r["budget"], r["strategy"], r["k"], f'{r["completed_documents"]}/{r["conditions"]}',
             f'{r["pages_min"]}—{r["pages_max"]}', f'{r["calls_min"]}—{r["calls_max"]}', r["false_stop_conditions"]]
            for r in aggregate_rows if r["scenario"] == "L"]),
        "## 全七场景的24顺序汇总",
        "每一行覆盖24个初始顺序。均值只是穷举网格描述，不把顺序、预算和阈值当成独立随机样本。"
        "B没有停滞阈值，k栏留空。D的旧页诊断可能随p1是否已经验收而变化，但按目标页正文验收始终拒绝旧内容，"
        "并不表示改变了故障条件。",
    ]
    for budget in ("12", "20"):
        display = []
        for r in aggregate_rows:
            if r["budget"] != budget:
                continue
            display.append([r["scenario"], label(r["strategy"], r["k"]),
                            f'{r["completed_documents"]}/{r["conditions"]}',
                            f'{r["pages_min"]}—{r["pages_max"]}', f'{r["calls_min"]}—{r["calls_max"]}',
                            f'{r["calls_mean"]:.3f}', r["stagnation_conditions"],
                            r["false_stop_conditions"], r["stagnation_with_unattempted_pages"]])
        lines += [f"### 全顺序预算{budget}次", markdown_table(
            ["场景", "策略", "完成顺序", "页数范围", "调用范围", "平均调用", "停滞数", "错停数", "停滞仍有未试页"], display)]
    lines += [
        "## 原始证据与复现",
        "[全部影子结果](all_shadow_results.csv)单列主页数、主调用和影子额外调用及恢复页。"
        "影子最终页数不能计为C/C1主运行成果；主与影子成本也没有混记。"
        "[analysis.json](analysis.json)保存本报告采用的实际汇总数值。",
        "程序入口为[run_experiment.py](../../run_experiment.py)，报告生成器为[build_report.py](../../build_report.py)。"
        "输入固定后才实现和运行；每批保存独立目录，比较差异和失败日志保留，不通过修改原预期来掩盖不一致。"
        "边界测试与历史失败记录位于[audit_logs](../../audit_logs/)。",
        "37项测试通过，包括网络失败算已试、覆盖时非零S冻结、覆盖完成不清零、预算优先与原70条件回归。"
        "首次批次在策略执行前被输入集合校验阻止：测试加载归档代码时生成了额外字节码缓存，冻结材料内容未变。"
        "缓存与原错误输出保留在[失败批次诊断](../../runs/20261115T114812_115089Z_7968123eacac/diagnosis.json)，"
        "关闭归档模块缓存写入后重新核验29项输入，再用独立新目录执行。详见[实现核查记录](../../IMPLEMENTATION_NOTES.md)。",
        "C1将停滞暂停前的源页尝试覆盖作为一个可验证约束；在本轮受控条件下，它减少了未尝试页被一并放弃的情况。"
        "R和L保留的错停，以及S随易页位置变化的表现，表明这个约束并不等于给每页充分的修复机会。",
    ]
    (out / "results.md").write_text("\n\n".join(lines) + "\n", encoding="utf-8")

    detail = ["# 原顺序112条件完整结果", "初始队列固定为p1→p2→p3→p4；页数与调用均为主运行。"
              "原70条件和新增42个C1条件在同一表列出，不覆盖第一版原始记录。"]
    for budget in ("12", "20"):
        members = [r for r in canonical if r["budget"] == budget]
        detail += [f"## 预算{budget}次的56条件", markdown_table(
            ["场景", "策略", "页数", "调用", "无新页", "严格重复", "已试/未试", "覆盖调用/S", "暂缓停滞事件", "停止原因"], [
                [r["scenario"], label(r["strategy"], r["k"]), r["completed_pages"], r["calls"],
                 r["no_progress_calls"], r["repeated_calls"], f'{r["attempted_count"]}/{r["unattempted_count"]}',
                 f'{r["coverage_call"]}/{r["coverage_S"]}', r["deferred_stagnation_events"], REASONS[r["stop_reason"]]]
                for r in members])]
    detail += ["## 原顺序中单独记录的影子续跑", markdown_table(
        ["场景", "策略", "预算", "主页/调用", "额外调用", "影子最终页/累计调用", "恢复页", "文档错停"], [
            [r["scenario"], label(r["strategy"], r["k"]), r["budget"], cell(r),
             r["shadow_extra_calls"], f'{r["shadow_final_pages"]}/{r["shadow_total_calls"]}',
             r["lost_pages"], "是" if r["false_stop"] == "true" else "否"]
            for r in canonical if r["shadow_extra_calls"] != ""])]
    (out / "canonical_details.md").write_text("\n\n".join(detail) + "\n", encoding="utf-8")

    traces = ["# C1与页面顺序的典型逐次过程", "源页身份随p编号固定；主与影子分开显示。"
              "已尝试页数由实际事件中的源页身份累积得到，网络失败也计入。"
              "影子中的S/E保留诊断值，但停滞守卫已经关闭。"]
    for run_id in ("S_o1234_L12_C_k2", "S_o1234_L12_C1_k2",
                   "S_o4123_L12_C_k2", "S_o4123_L12_C1_k2",
                   "R_o1234_L20_C1_k3", "R_o4123_L20_C1_k3",
                   "L_o1234_L12_C1_k2", "L_o1234_L20_C1_k2",
                   "N_o1234_L12_C1_k2"):
        row = lookup[run_id]
        traces += ["## " + run_id.replace("_", " "), trace_table(run, row),
                   f'[主原始记录]({link}/{row["main_trace_path"]})'
                   + (f'；[影子原始记录]({link}/{row["shadow_trace_path"]})' if row["shadow_trace_path"] else "")]
    (out / "trace_examples.md").write_text("\n\n".join(traces) + "\n", encoding="utf-8")
    artifacts = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file()}
    json_file(out / "report_manifest.json", {
        "source_run": run.name,
        "source_summary_sha256": hashlib.sha256((run / "summary.csv").read_bytes()).hexdigest(),
        "build_report_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "artifacts": artifacts,
    })
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    out = build(args.run.resolve(), Path(__file__).resolve().parent)
    print(json.dumps({"report_directory": str(out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
