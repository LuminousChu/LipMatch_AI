"""色名參照（color-ref-v1，2026-10-26）：問題提到某個中文色名時，由程式附上「這個色名在某品牌官網色號裡的用法」。

問題提到 PR029 裡的色名時，由程式附上色名參照區塊（品牌、色名、官方網頁色塊示意、取得日期），
先在實驗庫驗證；只給參照、不做推薦。

- 資料：Git 外的色名清冊 CSV（例如 PR029＝M·A·C 台灣官網 166 個色號中文名＋網頁色塊 HEX），由 COLOR_REFERENCE_CSV 指定、
  COLOR_REFERENCE_SHA256 核對；沒設定或指紋不符就不啟用（記錄原因），不讀其他資料。
- 比對：問題裡用「最長比對」找分組詞（沿用 scripts/build_color_term_data.py 的 ZH_KEYS，10-21 色名參考資料用的同一份），
  所以「乾燥玫瑰」不會被當成「玫瑰」。單字分組詞（粉、紫、裸、梅、李…）太容易誤中、「奶油」多半在說質地，不拿來觸發。
- 不經過生成模型：文字由程式組出，固定說明「網頁色塊示意，不是上唇實際顏色、不是通用色名標準、不是推薦」與資料是否待審。

color-ref-v2（2026-10-26，W02）：附上有色塊的參照時，模型寫的「資料沒有 HEX／色碼」限制會和下方參照互相矛盾。
由 reconcile_limitations 把這類限制改寫成固定句「文獻沒有提供色碼；下方色名參照是品牌網頁色塊示意，不是標準色碼」，
原句與改寫記在日誌（limitations_rewritten）；沒有對到任何色號（n＝0）時不改。
"""
from __future__ import annotations

import csv
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

COLOR_REF_VERSION = "color-ref-v6"
# color-ref-v6（2026-11-08，第十四輪 W03／W04，更正 color-ref-v4）：
# 「沒有色號時不顯示 M·A·C 色號參照」是指清冊裡對不到色號就不顯示（color-ref-v3 已做到），
# 不是「問題沒問色號就不顯示」。所以拿掉 color-ref-v4 的「要問色號／色值才附」：問題提到的色名對得到色號就附。
# asks_color_value 仍保留，只記在日誌（asks_color_value 欄）方便追溯。
# color-ref-v5（2026-11-06，重審 P08）：
# 證據不足、改附「有來源的一般建議」時（insufficient-assist），問題提到的色名照樣附參照，讓使用者知道那個色名大概是什麼顏色；
# 其他情況仍照 color-ref-v4。
# color-ref-v4（2026-11-05，W04）：
# 只有問題在問色號、色碼、色值這類實際顏色數值時才附參照；只比較色名（例如「豆沙色跟奶茶色差在哪」）不附
# （已被 color-ref-v6 取代，見上方）
_ASK_COLOR_VALUE = re.compile(r"HEX|hex|Hex|色碼|色號|色值|RGB|rgb|L\*?a\*?b\*?|Lab ?值|實測值")


def asks_color_value(question: str) -> bool:
    """問題是否在問色號／色碼／色值等實際顏色數值。"""
    return bool(_ASK_COLOR_VALUE.search(question or ""))
# 沿用 scripts/build_color_term_data.py 的 ZH_KEYS（2026-10-21）；去掉單字與「奶油」
_ALL_KEYS = ["豆沙", "奶茶", "乾燥玫瑰", "玫瑰", "磚紅", "楓葉", "焦糖", "肉桂", "裸", "正紅", "酒紅", "勃艮第",
             "莓", "梅", "李", "珊瑚", "橘紅", "橙紅", "蜜桃", "奶油", "可可", "棕", "褐", "紫", "藍調", "粉", "米"]
TRIGGER_KEYS = tuple(sorted((k for k in _ALL_KEYS if len(k) >= 2 and k != "奶油"), key=len, reverse=True))
_BRAND_BY_HOST = {"www.maccosmetics.com.tw": "M·A·C 台灣官網"}
MAX_LISTED = 3


# 說「來源沒有色碼」的限制：有否定詞，且提到 HEX／色碼／色號／色值／RGB
_CODE_WORD = re.compile(r"HEX|hex|Hex|色碼|色號|色值|RGB|rgb")
_NEGATION = re.compile(r"沒有|未|缺|不含|無法|無")
REWRITTEN_LIMITATION = "文獻沒有提供色碼；下方色名參照是品牌網頁色塊示意，不是標準色碼。"


def reconcile_limitations(limitations: list[str], log: dict | None) -> tuple[list[str], list[dict]]:
    """附了有色塊的色名參照時，把「沒有 HEX／色碼」這類限制改寫成固定句（只留一句）；回傳 (新限制清單, 改寫紀錄)。
    log 是 block() 的日誌；沒有參照、或參照裡沒有任何色號時原樣回傳。"""
    if not log or not any(m.get("n") for m in log.get("matches") or []):
        return list(limitations), []
    out, changed = [], []
    for lim in limitations:
        if _CODE_WORD.search(lim) and _NEGATION.search(lim):
            changed.append({"before": lim, "after": REWRITTEN_LIMITATION})
            lim = REWRITTEN_LIMITATION
        if lim not in out:
            out.append(lim)
    return out, changed


@dataclass(frozen=True)
class Shade:
    term_id: str
    name_zh: str
    hex: str
    source_doc_id: str
    source_url: str
    retrieved_at: str
    review_status: str


class ColorReference:
    def __init__(self, rows: list[Shade], *, csv_sha256: str):
        self.rows, self.csv_sha256 = rows, csv_sha256

    @classmethod
    def from_env(cls, env=None) -> tuple["ColorReference | None", dict]:
        env = env if env is not None else os.environ
        path, want = env.get("COLOR_REFERENCE_CSV"), (env.get("COLOR_REFERENCE_SHA256") or "").lower()
        if not path:
            return None, {"status": "disabled", "reason": "COLOR_REFERENCE_CSV 未設定"}
        data = Path(path).read_bytes()
        got = hashlib.sha256(data).hexdigest()
        if not want or got != want:
            return None, {"status": "disabled", "reason": "sha256_mismatch_or_missing", "sha256": got}
        reader = csv.DictReader(data.decode("utf-8-sig").splitlines())
        rows = [Shade(r["term_id"], r["color_term_zh"], r["official_web_swatch_hex"], r["source_doc_id"],
                      r["source_url"], r["source_retrieved_at"], r["review_status"]) for r in reader]
        return cls(rows, csv_sha256=got), {"status": "enabled", "rows": len(rows), "sha256": got}

    def lookup(self, question: str) -> list[dict]:
        """問題裡依序找分組詞（最長比對、不重疊）；每個詞回傳對到的色號。"""
        found, i, text = [], 0, question or ""
        while i < len(text):
            key = next((k for k in TRIGGER_KEYS if text.startswith(k, i)), None)
            if key is None:
                i += 1
                continue
            if key not in {f["key"] for f in found}:
                found.append({"key": key, "shades": [s for s in self.rows if key in s.name_zh]})
            i += len(key)
        return found

    def render(self, matches: list[dict]) -> str | None:
        # color-ref-v3（2026-11-02，W04）：沒有色號的色名不列「沒有名稱含…」；全部都沒有色號就整段不顯示
        matches = [m for m in matches if m["shades"]]
        if not matches:
            return None
        src = self.rows[0] if self.rows else None
        host = urlsplit(src.source_url).netloc if src else ""
        brand = _BRAND_BY_HOST.get(host, host or "來源網站")
        date = (src.retrieved_at or "")[:10] if src else ""
        lines = [f"色名參照（{brand}，{date} 取得）："]
        for m in matches:
            sh = m["shades"]
            if len(sh) <= MAX_LISTED:
                lines.append(f"・「{m['key']}」：" + "、".join(f"{s.name_zh}（網頁色塊 {s.hex}）" for s in sh) + "。")
            else:
                ex = "、".join(f"{s.name_zh}（{s.hex}）" for s in sh[:MAX_LISTED])
                lines.append(f"・「{m['key']}」：名稱含「{m['key']}」的色號有 {len(sh)} 個，色塊差異大，例如 {ex}。")
        pending = any(s.review_status != "approved" for m in matches for s in m["shades"])
        lines.append("說明：色塊是官方網頁上的示意，不是擦在嘴上的實際顏色，也不是通用色名標準；這裡只當參照，不是推薦"
                     + ("；這份色號資料尚未審核。" if pending else "。"))
        return "\n".join(lines)

    def block(self, question: str, *, general_advice: bool = False) -> tuple[str | None, dict | None]:
        """回傳 (附加文字, 日誌)；沒有對到任何色名回 (None, None)。
        color-ref-v6：對到的色名只要清冊裡有色號就附（render 會略過沒有色號的色名，全部沒有就回 None）；
        不再看問題有沒有問色號。general_advice＝True（證據不足改附一般建議）只多記一個原因，附不附的規則相同。"""
        matches = self.lookup(question)
        if not matches:
            return None, None
        log = {"version": COLOR_REF_VERSION, "csv_sha256": self.csv_sha256,
               "matches": [{"key": m["key"], "n": len(m["shades"]),
                            "shown": [s.term_id for s in m["shades"][:MAX_LISTED]]} for m in matches],
               "asks_color_value": asks_color_value(question)}
        if general_advice:
            log["attached_reason"] = "insufficient_general_advice"
        text = self.render(matches)
        if text is None:   # color-ref-v3：問到的色名清冊裡全部沒有色號，整段不顯示（日誌照記，規範 8）
            log["skipped"] = "no_shade_in_registry"
        return text, log
