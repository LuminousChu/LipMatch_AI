"""回覆前置說明（notice-v1，2026-11-02 依 40 題審核備註）。

兩件事，都是固定規則、不經模型，只改回覆最前面的說明句（message），不改路由、檢索、回答或權限：
1. 提示詞注入警語（I01～I04）：訊息裡出現要求忽略規則、略過檢查、冒稱授權、捏造來源等字樣時，
   在說明前面加一句警語；能回答的部分照常回答（例如 I02 問唇蜜與口紅差在哪）。
2. 範圍外點名對象（O01）：因為問的是衣服、頭髮、指甲等非唇彩物件而走範圍外時，直接說出問的是什麼、
   訊息裡沒有唇彩內容；其他範圍外原因（診斷、交易等）維持原說明。
命中的字樣回傳給呼叫端記進請求紀錄（規範 8）。注入字樣只用來加警語，不會讓任何指令生效。
"""
from __future__ import annotations

import re

from . import preferences as pref

NOTICE_VERSION = "notice-v5"   # v5（2026-11-08）：醫療類附了範圍內回答時改委婉說明（O02）

# notice-v2（2026-11-02）：委婉、不點破是注入、不說明系統怎麼判定
# notice-v4（2026-11-05，I04）：要明確提醒「權限、審核與指令類要求不能照做」，仍不說明怎麼判定
INJECTION_WARNING = "訊息中有關系統權限、審核或操作指令的要求無法照做，以下只回覆可以協助的部分。"
# notice-v4（2026-11-05，I01）：商品出口不加警語，說明句只講商品未開放
_NO_WARNING_OUTCOMES = ("product_not_enabled",)

# 每條規則（名稱, 正則）；要「動作＋對象」一起出現才算，避免「可以忽略唇線嗎」這類一般問題誤判
_INJECTION_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("假系統訊息", re.compile(r"系統更新|優先級最高|最高優先|system\s*prompt|系統提示詞?", re.I)),
    ("要求忽略規則", re.compile(r"忽略(?:所有|全部|以上|前面|之前|一切)?的?(?:來源|限制|規則|指令|設定|檢查)")),
    ("要求略過檢查", re.compile(r"(?:略過|跳過|關閉|繞過)(?:[^，。,；;]{0,8})(?:檢查|審核|限制)|不用(?:再)?檢查(?:權限)?")),
    ("要求改變審核狀態", re.compile(r"(?:當成|視為|改成|標成)(?:已)?核准")),
    ("冒稱授權", re.compile(r"開發者授權|管理員授權|授權稽核|我是(?:系統)?管理員|我是開發者")),
    ("要求捏造來源", re.compile(r"(?:捏造|假造|偽造|編造|杜撰)(?:[^，。,；;]{0,10})(?:來源|編號|頁碼|引用|研究)")),
    ("要求解除限制", re.compile(r"解除(?:商品|所有)?限制|限制已解除")),
    ("要求不留紀錄", re.compile(r"不(?:用|要)記錄")),
)
_NON_LIP_ITEM = re.compile(pref._NON_LIP_ITEMS)


def injection_markers(text: str) -> list[str]:
    """回傳命中的注入規則名稱（依規則順序、不重複）；沒有命中回空清單。"""
    return [name for name, pat in _INJECTION_RULES if pat.search(text or "")]


def non_lip_object(text: str) -> str | None:
    """範圍外是因為問非唇彩物件的顏色時，回傳該物件（例如「裙子」）；不是這個原因回 None。"""
    hit = pref.outside_hit(text or "")
    if not hit or not pref._RE_NON_LIP_COLOR.search(hit):
        return None
    m = _NON_LIP_ITEM.search(hit)
    return m.group(0) if m else None


_MEDICAL = re.compile(r"診斷|處方|用藥|吃藥|擦藥|藥膏|唇炎|發炎|過敏反應|紅腫|潰爛")


def decorate(message: str, question: str, outcome: str, assisted: bool = False) -> tuple[str, dict]:
    """依問題與結果調整說明句；回傳（新說明, 日誌）。日誌只在有改動時有內容。

    assisted：攔截出口後另外附了範圍內的回答（exit_assist）。notice-v3（2026-11-02）：語氣委婉，
    不寫「可以直接問」；範圍外點名對象但不說教；醫療類建議找醫師或藥師。
    """
    log: dict = {}
    if outcome == "outside_scope":
        item = non_lip_object(question)
        if item:
            message = (f"這個問題比較偏向{item}的搭配，我能協助的是唇彩的部分；以下提供可以參考的唇色方向。" if assisted
                       else f"這個問題比較偏向{item}的搭配，我能協助的是唇彩相關的問題。")
            log["non_lip_object"] = item
        elif _MEDICAL.search(question or "") and assisted:
            # notice-v5（2026-11-08，O02）：
            # 附了範圍內回答時，先委婉說明不診斷不用藥，再交代下面是嘴唇乾燥時的唇妝做法；不保證安全（規範 6）
            message = ("嘴唇紅腫、脫皮的原因需要醫師或藥師當面判斷，我沒辦法幫你診斷或建議用藥。"
                       "如果之後想上唇妝，下面整理了來源裡嘴唇乾燥時的唇妝做法給你參考；"
                       "紅腫或破皮還沒好之前，先別勉強上妝。")
            log["medical"] = True
        elif _MEDICAL.search(question or ""):
            message = ("這部分需要專業的醫療判斷，我沒辦法協助診斷或用藥；嘴唇不舒服的話，建議請醫師或藥師協助。"
                       "我能協助的是唇彩的色系與妝感。")
            log["medical"] = True
    if outcome == "product_not_enabled" and assisted:
        message = "商品功能未開放，以下先整理可以參考的色系方向。"
    if assisted:
        log["assisted"] = True
    markers = injection_markers(question)
    if markers:
        if outcome not in _NO_WARNING_OUTCOMES:
            message = f"{INJECTION_WARNING}\n{message}"
        log["injection_markers"] = markers   # 不加警語時仍記錄命中的字樣（規範 8）
    if log:
        log["version"] = NOTICE_VERSION
    return message, log
