"""scripts/build_color_term_data.py 的換算與分組測試（不需要 data/ 裡的第三方資料）。"""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "build_color_term_data", Path(__file__).resolve().parents[1] / "scripts/build_color_term_data.py")
B = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(B)


def test_hex_to_lab_white_black():
    """白色 L*=100、黑色 L*=0，a*、b* 都接近 0（D65 白點）。"""
    L, a, b = B.hex_to_lab("#ffffff")
    assert abs(L - 100) < 0.01 and abs(a) < 0.01 and abs(b) < 0.01
    L, a, b = B.hex_to_lab("#000000")
    assert abs(L) < 0.01 and abs(a) < 0.01 and abs(b) < 0.01


def test_hex_to_lab_matches_known_value():
    """#911517 與 10-16 快照的換算值一致（30.93、49.27、33.49）。"""
    L, a, b = B.hex_to_lab("#911517")
    assert (round(L, 2), round(a, 2), round(b, 2)) == (30.93, 49.27, 33.49)


def test_lch_hue_range():
    """色相角落在 0～360；純 a* 正向為 0°、b* 正向為 90°。"""
    assert B.lch(10, 0) == (10, 0)
    C, h = B.lch(0, 5)
    assert abs(C - 5) < 1e-9 and abs(h - 90) < 1e-9
    assert 0 <= B.lch(-3, -4)[1] < 360


def test_term_index_lists_missing_terms():
    """沒有任何色號用到的分組關鍵字也要列出（n_skus=0），不硬湊。"""
    rows = [{"shade_term_zh": "奶油豆沙色", "shade_name_raw": "X", "shade_description_raw": "", "shade_name_en": "X",
             "sku_id": "S1", "L_star": 50.0, "a_star": 30.0, "b_star": 10.0, "C_star": 31.6, "known_mismatch": ""}]
    idx = {r["term_key_zh"]: r for r in B.term_index(rows)}
    assert idx["豆沙"]["n_skus"] == 1 and idx["乾燥玫瑰"]["n_skus"] == 0
    assert idx["豆沙"]["is_definition"].startswith("否")
