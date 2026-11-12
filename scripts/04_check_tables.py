"""04_check_tables.py — 用數學關係交叉檢查人工／讀圖轉錄的 TH013 表格。

用法：
    python scripts/04_check_tables.py                  # 預設讀 data/review/tables/
    python scripts/04_check_tables.py path/to/tables/

輸出：
    data/review/tables/TH013_table_check.md

原則：
- 只檢查、不修改 CSV。
- 所有比較都用「四捨五入區間」：原文寫 2 位小數就代表 ±0.005，
  只有區間完全不重疊才算不一致，避免把捨入誤差當成錯誤。
- 不一致可能是「轉錄錯誤」也可能是「原文本身的錯誤」，一律列出讓人工對照原圖判斷。
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "data" / "review" / "tables"
PREFIX = "TH013"


# ---------------------------------------------------------------------------
# 區間運算
# ---------------------------------------------------------------------------

class Num:
    """原文數字與其捨入區間；'<.001' 這類不精確值為 None。"""

    def __init__(self, text: str):
        self.text = text.strip()
        self.value: float | None = None
        self.half = 0.0
        if re.fullmatch(r"-?\d*\.?\d+", self.text):
            self.value = float(self.text)
            decimals = len(self.text.split(".")[1]) if "." in self.text else 0
            self.half = 0.5 * 10 ** -decimals

    @property
    def lo(self) -> float:
        return self.value - self.half

    @property
    def hi(self) -> float:
        return self.value + self.half


def ratio_range(a: Num, b: Num) -> tuple[float, float]:
    vals = [x / y for x in (a.lo, a.hi) for y in (b.lo, b.hi)]
    return min(vals), max(vals)


def overlaps(lo: float, hi: float, n: Num, slack: float = 0.0) -> bool:
    return lo - slack <= n.hi and n.lo <= hi + slack


def fmt(lo: float, hi: float) -> str:
    return f"{(lo + hi) / 2:.3f}（容許 {lo:.3f}～{hi:.3f}）"


# ---------------------------------------------------------------------------
# 讀檔
# ---------------------------------------------------------------------------

def load(dir_: Path, name: str) -> list[dict[str, str]]:
    with open(dir_ / f"{PREFIX}_{name}.csv", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def code_of(label: str) -> str:
    """'light orange (LO)' → 'LO'；'face average' → 'face ave'。"""
    m = re.search(r"\(([A-Z]+)\)", label)
    return m.group(1) if m else "face ave"


# ---------------------------------------------------------------------------
# 檢查
# ---------------------------------------------------------------------------

def check_a1_chroma_hue(a1, out):
    std_rows, swapped_rows = [], []
    for r in a1:
        a, b, h, c = (Num(r[k]) for k in ("a*", "b*", "h*", "C*"))
        where = f"A1 {r['image_id']} {r['data_name']}"
        cs = [math.hypot(x, y) for x in (a.lo, a.hi) for y in (b.lo, b.hi)]
        if not overlaps(min(cs), max(cs), c):
            out["fail"].append(f"{where}：C*={c.text}，但 √(a*²+b*²) = {fmt(min(cs), max(cs))}")
        std = [math.degrees(math.atan2(y, x)) for x in (a.lo, a.hi) for y in (b.lo, b.hi)]
        swp = [90 - v for v in std]
        if overlaps(min(std), max(std), h):
            std_rows.append(where)
        elif overlaps(min(swp), max(swp), h):
            swapped_rows.append(where)
        else:
            out["fail"].append(f"{where}：h*={h.text} 與 atan2(b*,a*) 或其互補角都不符")
    out["info"].append(
        f"A1 h* 公式：{len(swapped_rows)} 列符合 atan(a*/b*)（與 CIE 定義相反），"
        f"{len(std_rows)} 列符合標準 atan2(b*,a*)。"
    )
    if std_rows:
        out["info"].append("　符合標準公式的列：" + "、".join(std_rows))


def check_t41_vs_a1(t41, a1, out):
    groups = defaultdict(list)
    for r in a1:
        groups[(r["group"], r["data_name"])].append(r)
    for r in t41:
        key = (r["group"], code_of(r["measured_point"]))
        src = groups.get(key)
        if not src:
            out["fail"].append(f"T4.1 {key}：A1 找不到對應列")
            continue
        for col in ("L*", "a*", "b*", "h*", "C*"):
            vals = [Num(s[col]) for s in src]
            lo = sum(v.lo for v in vals) / len(vals)
            hi = sum(v.hi for v in vals) / len(vals)
            n = Num(r[col])
            if not overlaps(lo, hi, n):
                out["fail"].append(
                    f"T4.1 {r['group']} {r['measured_point']} {col}={n.text}，"
                    f"但 A1 五張影像平均 = {fmt(lo, hi)}"
                )


def check_t42_vs_t41(t42, t41, out):
    base = {(r["group"], code_of(r["measured_point"])): r for r in t41}
    for r in t42:
        g, code = r["group"], code_of(r["lipstick_id"])
        lip, face = base[(g, code)], base[(g, "face ave")]
        for col in ("L*", "a*", "b*"):
            lp, fc, d = Num(lip[col]), Num(face[col]), Num(r[f"delta {col}"])
            if not overlaps(lp.lo - fc.hi, lp.hi - fc.lo, d):
                out["fail"].append(
                    f"T4.2 {g} {code} delta {col}={d.text}，但 T4.1 唇色−膚色 = "
                    f"{fmt(lp.lo - fc.hi, lp.hi - fc.lo)}"
                )
        de = math.sqrt(sum(float(r[f"delta {c}"]) ** 2 for c in ("L*", "a*", "b*")))
        out["info_de"].append(f"{g} {code}: 表列 ΔE={r['delta E']}，由三個 delta 算出 {de:.2f}")


def check_a21(a21, out):
    for r in a21:
        n, sd, se = Num(r["N"]), Num(r["sd"]), Num(r["se"])
        lo, hi = sd.lo / math.sqrt(n.value), sd.hi / math.sqrt(n.value)
        if not overlaps(lo, hi, se):
            out["fail"].append(
                f"A2.1 {r['group']} {r['lip_id']} {r['response']}：se={se.text}，但 sd/√N = {fmt(lo, hi)}"
            )


def check_a22(a22, out):
    for r in a22:
        where = f"A2.2 {r['group']} {r['pair']}"
        mean, sd, se, lo_ci, hi_ci, t, df = (
            Num(r[k]) for k in ("mean", "sd", "se", "ci_lower", "ci_upper", "t", "df")
        )
        lo, hi = ratio_range(mean, se)
        if not overlaps(lo, hi, t):
            out["fail"].append(f"{where}：t={t.text}，但 mean/se = {fmt(lo, hi)}")
        n = df.value + 1
        lo, hi = sd.lo / math.sqrt(n), sd.hi / math.sqrt(n)
        if not overlaps(lo, hi, se):
            out["fail"].append(f"{where}：se={se.text}，但 sd/√(df+1) = {fmt(lo, hi)}")
        mid_lo, mid_hi = (lo_ci.lo + hi_ci.lo) / 2, (lo_ci.hi + hi_ci.hi) / 2
        if not overlaps(mid_lo, mid_hi, mean):
            out["fail"].append(f"{where}：mean={mean.text}，但信賴區間中點 = {fmt(mid_lo, mid_hi)}")
        p1, p2 = Num(r["p_one_sided"]), Num(r["p_two_sided"])
        if p1.value is not None and p2.value is not None:
            if not overlaps(2 * p1.lo, 2 * p1.hi, p2):
                out["fail"].append(f"{where}：雙尾 p={p2.text}，但 2×單尾 p = {fmt(2 * p1.lo, 2 * p1.hi)}")
        elif (p1.value is None) != (p2.value is None) and not (p1.text == "<.001" and p2.value is not None and p2.value <= 0.002):
            out["fail"].append(f"{where}：單尾 p={p1.text}、雙尾 p={p2.text} 不一致")


def check_effect_sizes(es, a22, out):
    src = {(r["group"], r["pair"]): r for r in a22}
    for r in es:
        pair = r["pair"].replace("BL - ", "NL - ")
        for grp in ("CN", "CA"):
            s = src.get((f"{grp}_{r['attribute'].lower()}", pair))
            d = Num(r[grp])
            if not s:
                out["fail"].append(f"{r['table_id']} {grp} {r['pair']}：A2.2 找不到對應列")
                continue
            lo, hi = ratio_range(Num(s["mean"]), Num(s["sd"]))
            if not overlaps(lo, hi, d):
                out["fail"].append(
                    f"{r['table_id']} {r['attribute']} {grp} {r['pair']}：d={d.text}，"
                    f"但 A2.2 mean/sd = {fmt(lo, hi)}"
                )


def check_df(a22, a21, out):
    dfs = defaultdict(set)
    for r in a22:
        dfs[r["group"]].add(r["df"])
    n = {r["N"] for r in a21}
    out["info"].append(
        "A2.2 自由度（df）：" + "、".join(f"{g}={'/'.join(sorted(v))}" for g, v in dfs.items())
        + f"；A2.1 寫 N={'/'.join(sorted(n))}（成對 t 檢定應為 df=N−1）。"
    )


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", nargs="?", type=Path, default=DEFAULT_DIR)
    args = ap.parse_args(argv)

    t41, t42 = load(args.dir, "T4.1"), load(args.dir, "T4.2")
    es, a1 = load(args.dir, "T4.3-4.6"), load(args.dir, "A1")
    a21, a22 = load(args.dir, "A2.1"), load(args.dir, "A2.2")

    out: dict[str, list[str]] = {"fail": [], "info": [], "info_de": []}
    check_a1_chroma_hue(a1, out)
    check_t41_vs_a1(t41, a1, out)
    check_t42_vs_t41(t42, t41, out)
    check_a21(a21, out)
    check_a22(a22, out)
    check_effect_sizes(es, a22, out)
    check_df(a22, a21, out)

    lines = [
        f"# {PREFIX} 表格交叉檢查",
        "",
        "比較皆以原文小數位數的捨入區間為準；不一致可能是轉錄錯誤，也可能是原文本身的錯誤，請對照原圖判斷。",
        "",
        f"## 不一致（{len(out['fail'])}）",
        "",
        *(f"- {x}" for x in out["fail"]),
        "",
        "## 觀察",
        "",
        *(f"- {x}" for x in out["info"]),
        "",
        "## T4.2 ΔE 對照（僅供參考：若 ΔE 是逐張影像計算後再平均，本來就不會等於平均 delta 算出的值）",
        "",
        *(f"- {x}" for x in out["info_de"]),
        "",
    ]
    report = args.dir / f"{PREFIX}_table_check.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"[check] 不一致 {len(out['fail'])} 項 → {report}")
    for x in out["fail"]:
        print("  -", x)
    for x in out["info"]:
        print("  *", x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
