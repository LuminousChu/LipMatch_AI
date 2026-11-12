"""建立唇彩中文色名參考資料（M·A·C 台灣官網色號；只做資料，不做商品資料庫、不做推薦）。

整支程式在做什麼（依執行順序）：
    1. 讀兩份既有快照，**不重抓官網**：
         - 10-17 分支版整理表（`data/mac-shade-terms` 的 scripts/build_mac_shade_terms.py 產生，194 筆，含來源指紋與頁面定位）；
         - 10-16 電腦 2 版（同一頁 194 筆，多了由 HEX 換算的 L*a*b*）。
       兩份依 sku_id 對齊；SKU 集合、HEX、英文描述必須一致，否則停止。
    2. 由官網 HEX 換算 CIELAB（sRGB → XYZ，D65、2° 觀察者；四捨五入兩位），並與電腦 2 版逐筆核對（容許 ±0.02）。
       換算值是「螢幕色塊」的示意，**不是唇上實測**。
    3. 輸出三張表到 data/review/color_terms/（data/ 不進 Git；第三方資料授權未確認）：
         a. MAC_TW_shades_<日期>.csv：194 筆，分支版全部欄位＋L*、a*、b*、C*、h°、換算說明、與 10-16 快照的一致性、已知不符標記。
         b. MAC_TW_color_term_index_<日期>.csv：依「專案選定的分組關鍵字」把中文色名分組，列出 SKU 數、英文描述、Lab 平均與範圍；
            關鍵字一筆都沒對到的也列出（例如乾燥玫瑰），不硬湊。分組關鍵字只是整理用，**不是色名定義**。
         c. MAC_TW_en_zh_term_pairs_<日期>.csv：同一 SKU 上英文描述關鍵詞與中文色名的對照，列出出現次數；只是候選，**不是等價詞**。
    4. 寫 MANIFEST（每個輸入與輸出的大小與 SHA-256）。全部 review_status 維持「待審」，沒有任何核准。
執行：
    python -I scripts/build_color_term_data.py
"""
from __future__ import annotations

import csv
import hashlib
import math
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/web"
OUT = ROOT / "data/review/color_terms"
DATE = "20261021"
BRANCH_CSV = RAW / "MAC_TW_lipstick_shade_finder_20261017_branch.csv"
PC2_CSV = RAW / "MAC_TW_lipstick_shade_finder_20261016_pc2.csv"
EXTRACT_JSON = RAW / "MAC_TW_shade_finder_extract_20261017.json"
LAB_METHOD = "由官網 HEX 換算：sRGB→XYZ→CIELAB，D65、2°；螢幕色塊示意，非唇上實測"

# 已知「官方描述與色塊明顯不符」：出自 10-16 快照說明與 10-17 整理紀錄，原樣保留、只加標記
KNOWN_MISMATCH = {"Fall In Love": "描述「明亮奶油色」但色塊為亮粉紅", "Go For Gold": "描述「閃耀焦糖棕」但色塊很淺",
                  "Velvet Teddy": "描述「深褐色」但色塊偏淺"}

# 專案選定的中文分組關鍵字（整理用，不是定義）；含 G1 缺口要查的豆沙、奶茶、乾燥玫瑰
ZH_KEYS = ["豆沙", "奶茶", "乾燥玫瑰", "玫瑰", "磚紅", "楓葉", "焦糖", "肉桂", "裸", "正紅", "酒紅", "勃艮第",
           "莓", "梅", "李", "珊瑚", "橘紅", "橙紅", "蜜桃", "奶油", "可可", "棕", "褐", "紫", "藍調", "粉", "米"]
# 英文描述關鍵詞（同樣只是整理用）
EN_KEYS = ["dusty rose", "rose", "mauve", "nude", "brick", "berry", "plum", "coral", "peach", "brown", "burgundy",
           "wine", "red", "pink", "orange", "beige", "cool", "warm", "blue", "yellow"]


def sha256(p: Path) -> str:
    """檔案 SHA-256。"""
    return hashlib.sha256(p.read_bytes()).hexdigest()


def hex_to_lab(hx: str) -> tuple[float, float, float]:
    """#rrggbb → CIELAB（sRGB 伽瑪展開 → XYZ（D65）→ Lab，2° 觀察者）。"""
    rgb = [int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    r, g, b = lin
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = (0.2126729 * r + 0.7151522 * g + 0.0721750 * b) / 1.00000
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883
    f = [t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29 for t in (x, y, z)]
    return 116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])


def lch(a: float, b: float) -> tuple[float, float]:
    """由 a*、b* 算彩度 C* 與色相角 h°（0～360）。"""
    return math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360


def read_csv(p: Path) -> list[dict]:
    """讀 UTF-8（可含 BOM）CSV。"""
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(p: Path, rows: list[dict], cols: list[str]) -> None:
    """寫 UTF-8 BOM CSV（Excel 可直接開）。"""
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def shade_rows(branch: list[dict], pc2: dict[str, dict]) -> list[dict]:
    """合併兩版、加 Lab 與一致性欄位；兩版不一致就停止。"""
    assert {r["sku_id"] for r in branch} == set(pc2), "兩版 SKU 集合不同"
    out = []
    for r in branch:
        p = pc2[r["sku_id"]]
        assert r["hex_official"].lower() == p["hex_official"].lower(), r["sku_id"]
        assert r["shade_description_raw"].strip() == p["shade_description"].strip(), r["sku_id"]
        L, a, b = hex_to_lab(r["hex_official"])
        C, h = lch(a, b)
        lab = {"L_star": round(L, 2), "a_star": round(a, 2), "b_star": round(b, 2), "C_star": round(C, 2), "h_deg": round(h, 1)}
        dev = max(abs(lab[k] - float(p[k])) for k in ("L_star", "a_star", "b_star", "C_star"))
        assert dev <= 0.02, (r["sku_id"], dev)
        name = re.sub(r"^\d{3}\s*", "", r["shade_name_en"]).strip().lower()      # 去掉色名前的三位數貨號（例如 307）
        flag = next((v for k, v in KNOWN_MISMATCH.items() if name == k.lower()), "")
        out.append({**r, **lab, "lab_method": LAB_METHOD, "snapshot_20261016_match": "是",
                    "known_mismatch": flag, "manual_check_needed": "是"})
    return out


def term_text(r: dict) -> str:
    """分組比對用的中文文字：中文色名詞＋色名裡的中文（兩者都保留原文來源）。"""
    return (r["shade_term_zh"] or "") + " " + "".join(ch for ch in r["shade_name_raw"] if "\u4e00" <= ch <= "\u9fff")


def term_index(rows: list[dict]) -> list[dict]:
    """依中文分組關鍵字列出 SKU、英文描述與 Lab 統計；沒對到的也列出。"""
    out = []
    for k in ZH_KEYS:
        hit = [r for r in rows if k in term_text(r)]
        base = {"term_key_zh": k, "n_skus": len(hit), "brand_scope": "僅 M·A·C 台灣官網（單一品牌）",
                "value_type": LAB_METHOD, "is_definition": "否（專案整理用分組，不是色名定義）"}
        if not hit:
            out.append({**base, "zh_terms": "", "en_descriptions": "", "skus": "",
                        "note": "這批資料沒有任何色號使用這個詞"})
            continue
        st = {c: [r[c] for r in hit] for c in ("L_star", "a_star", "b_star", "C_star")}
        out.append({**base,
                    "zh_terms": "／".join(sorted({r["shade_term_zh"] or r["shade_name_raw"] for r in hit})),
                    "en_descriptions": "／".join(sorted({r["shade_description_raw"] for r in hit if r["shade_description_raw"]})),
                    "skus": "／".join(f'{r["shade_name_en"]}({r["sku_id"]})' for r in hit),
                    **{f"{c}_mean": round(statistics.mean(v), 2) for c, v in st.items()},
                    **{f"{c}_min": min(v) for c, v in st.items()}, **{f"{c}_max": max(v) for c, v in st.items()},
                    "known_mismatch_in_group": sum(bool(r["known_mismatch"]) for r in hit),
                    "note": "樣本少，平均只供示意" if len(hit) < 5 else ""})
    return out


def term_pairs(rows: list[dict]) -> list[dict]:
    """同一 SKU 的英文描述關鍵詞 × 中文色名詞，計數（候選，不是等價）。"""
    pairs: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        en, zh = r["shade_description_raw"].lower(), r["shade_term_zh"]
        if not en or not zh:
            continue
        keys = [k for k in EN_KEYS if k in en]
        if "dusty rose" in keys:
            keys = [k for k in keys if k != "rose"]
        for k in keys:
            pairs.setdefault((k, zh), []).append(f'{r["shade_name_en"]}({r["sku_id"]})')
    return [{"en_key": k, "zh_term": z, "n": len(v), "skus": "／".join(v),
             "relation": "同一色號上同時出現（候選對照，不是等價詞）"} for (k, z), v in sorted(pairs.items())]


def main() -> int:
    """讀兩版快照、建三張表、寫 MANIFEST。"""
    branch, pc2 = read_csv(BRANCH_CSV), {r["sku_id"]: r for r in read_csv(PC2_CSV)}
    rows = shade_rows(branch, pc2)
    OUT.mkdir(parents=True, exist_ok=True)
    outs = {
        f"MAC_TW_shades_{DATE}.csv": (rows, list(branch[0]) + ["L_star", "a_star", "b_star", "C_star", "h_deg", "lab_method",
                                                                "snapshot_20261016_match", "known_mismatch", "manual_check_needed"]),
    }
    idx = term_index(rows)
    idx_cols = ["term_key_zh", "n_skus", "zh_terms", "en_descriptions", "skus"] + \
               [f"{c}_{s}" for c in ("L_star", "a_star", "b_star", "C_star") for s in ("mean", "min", "max")] + \
               ["known_mismatch_in_group", "brand_scope", "value_type", "is_definition", "note"]
    outs[f"MAC_TW_color_term_index_{DATE}.csv"] = (idx, idx_cols)
    outs[f"MAC_TW_en_zh_term_pairs_{DATE}.csv"] = (term_pairs(rows), ["en_key", "zh_term", "n", "skus", "relation"])
    for name, (data, cols) in outs.items():
        write_csv(OUT / name, data, cols)
    with open(OUT / f"MANIFEST_{DATE}.tsv", "w", encoding="utf-8", newline="") as f:
        f.write("role\tpath\tsize_bytes\tsha256\n")
        for role, p in [("input", BRANCH_CSV), ("input", PC2_CSV), ("input", EXTRACT_JSON)] + [("output", OUT / n) for n in outs]:
            f.write(f"{role}\t{p.relative_to(ROOT).as_posix()}\t{p.stat().st_size}\t{sha256(p)}\n")
    print({"shades": len(rows), "terms": len(idx), "terms_without_sku": [r["term_key_zh"] for r in idx if not r["n_skus"]],
           "pairs": len(outs[f"MAC_TW_en_zh_term_pairs_{DATE}.csv"][0]), "known_mismatch": sum(bool(r["known_mismatch"]) for r in rows)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
