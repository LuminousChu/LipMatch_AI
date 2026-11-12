"""哪些區塊可以進 RAG：全專案唯一的篩選規則。

判斷分兩層：
1. 解析品質：已移除、沒有文字、待人工確認的區塊不能用。
2. 內容適用性：前言（標題頁、聲明、致謝）、目錄、參考文獻、縮寫表、關鍵字清單、
   出版資訊（授權聲明、版權、通訊作者、收稿日期）、教材題組（練習題、活動、學習目標、答案）不放進 RAG，
   否則問「黃皮膚適合什麼唇色」時會搜到書目或目錄，而不是研究結論。
   標題、編號標籤不單獨成段（章節路徑已記在每個區塊的 section）。

輸出是排除原因（None = 可用），方便統計與稽核，而不是只回傳 True/False。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from schemas import Document, TextBlock

CONTENT_TYPES = {"paragraph", "list_item", "figure_caption", "table_caption", "table"}

# 以最上層章節名稱判斷；Appendix 保留（含實驗數據）
EXCLUDED_SECTION_RE = re.compile(
    r"^\s*(Acknowledg(e)?ments?|Table of Contents|Contents|List of (Abbreviations|Figures|Tables)"
    r"|Abbreviations|Reference List|References|Bibliography|Declaration|Copyright"
    # 2026-10-18：基金聲明、利益衝突、作者簡介是出版行政資訊，不是研究知識（AR003 p.8–10）
    r"|Funding|Conflicts? of Interests?|Author Biograph(y|ies))\b"
    r"|^\s*(誌\s*謝|致\s*謝|目\s*錄|圖目錄|表目錄|符號說明|參考文獻)",   # 中文論文
    re.I,
)
# 正文起點：出現這些章節之前的內容一律視為前言（標題頁、聲明、致謝…）
BODY_START_RE = re.compile(
    r"^\s*(Abstract|Summary|Chapter\s*1\b|1\.?\s+Introduction|Introduction)\b"
    r"|^\s*(中文摘要|摘\s*要|第\s*[一1]\s*章|緒\s*論)",   # 中文論文
    re.I,
)


# 出版／行政資訊，不是研究知識：授權聲明、版權、通訊作者、收稿日期、引用格式提示
# 只對短段落（<= BOILERPLATE_MAX_CHARS）判斷，避免誤刪剛好提到 "license" 的研究內容
BOILERPLATE_RE = re.compile(
    r"open access article|Creative Commons|This is an open access|How to cite this article"
    r"|©\s*\d{4}|\(c\)\s*\d{4}|All rights reserved|^\s*Correspondence\b|^\s*Received\s*:|Accepted\s*:\s*\d"
    r"|[\w.+-]+@[\w-]+\.[\w.-]+|^\s*(Funding information|ORCID|Conflict of interest)\b"
    r"|版權所有|著作權",
    re.I,
)
BOILERPLATE_MAX_CHARS = 800
# 關鍵字清單：標籤（"K E Y W O R D S"、"Keywords"、"關鍵字"）後面那一段，或以「Keywords:」開頭的段落
KEYWORDS_LABEL_RE = re.compile(r"^\s*(K\s*E\s*Y\s*W\s*O\s*R\s*D\s*S|Key\s*words?|Mots[- ]cl[ée]s|關鍵字|關鍵詞)\s*[:：]?\s*$", re.I)
KEYWORDS_INLINE_RE = re.compile(r"^\s*(Key\s*words?|關鍵字|關鍵詞)\s*[:：]", re.I)


def keyword_block_ids(doc: Document) -> set[str]:
    """關鍵字清單所在的區塊：緊接在關鍵字標籤之後的第一個內容區塊（略過已移除的頁首頁尾）。"""
    ids: set[str] = set()
    pending = False
    for b in sorted(doc.blocks, key=lambda x: x.order):
        if b.is_removed:
            continue
        text = (b.cleaned_text or b.raw_text or "").strip()
        if b.block_type in ("label", "heading") and KEYWORDS_LABEL_RE.match(text):
            pending = True
            continue
        if KEYWORDS_INLINE_RE.match(text):
            ids.add(b.block_id)
        elif pending and b.block_type in CONTENT_TYPES:
            ids.add(b.block_id)
        if b.block_type in CONTENT_TYPES:
            pending = False
    return ids


# --- 教材題組：章末練習題、活動、學習目標、答案（TX001 NCERT 職訓教材） --------------------
# CLAUDE.md 第 7 條：題組不進 RAG。這些標記在教材裡單獨成一行（常被擷取成 label 或併進 figure_text）。
# 練習題、活動：標記之後到下一個章節標題（或學習目標、詞彙表標記）之前都是題組。
EXERCISE_MARKER_RE = re.compile(
    r"^\s*(?:Check Your Progress|Practical Exercises?|Activity\s+\d+|Answer Key"
    r"|[A-H]\.\s*(?:Multiple[- ]Choice Questions|Fill in the Blanks?"
    r"|State whether the following statements are true or false|Subjective Questions"
    r"|Short Answer Questions|Long Answer Questions|Match the (?:Columns?|following)))\s*[:：]?\s*$",
    re.I | re.M,
)
# 學習目標（每節結尾的 'What have you learned?' 方框）：後面常直接接下一節正文、中間沒有標題
# （TX001 p.49），所以只涵蓋緊接在後面的單一項目符號條列，見 `objectives_block_ids`
OBJECTIVES_MARKER_RE = re.compile(
    r"^\s*(?:What have you learned\??|After completing this (?:session|unit|chapter),? you will be able to)"
    r"\s*[:：]?\s*$",
    re.I | re.M,
)
# 詞彙表是知識內容（術語定義），不是題組；它也是題組區的結束點
GLOSSARY_MARKER_RE = re.compile(r"^\s*(?:Glossary|詞彙表|名詞解釋)\s*$", re.I | re.M)
# 答案通常集中在書末：一旦出現就延續到文件結尾，不因其中的 'Unit I'、'Session 2' 小標而中斷
STICKY_EXERCISE_MARKER_RE = re.compile(r"^\s*Answer Key\s*$", re.I | re.M)
# 填空題的空格線（'__________ is used to …'）：不論在不在題組區都視為題目
FILL_IN_BLANK_RE = re.compile(r"_{3,}")
# 學習目標條列用單一項目符號（'• Explain …'）；TX001 正文條列是雙項目符號（'• • It is better …'）
SINGLE_BULLET_RE = re.compile(r"^\s*[•▪●◦]\s*(?![•▪●◦])\S")
# 標題文字在 >= 這麼多頁重複出現 → 視為頁邊欄標籤（TX001 每頁左欄的 'Notes'），不當成章節邊界
MARGIN_HEADING_MIN_PAGES = 5


def margin_heading_texts(blocks: Iterable[TextBlock]) -> set[str]:
    """找出在很多頁重複出現的標題文字（頁邊欄標籤），它們不是真正的章節邊界。

    參數：
        blocks：已清洗文件的全部區塊。

    回傳：
        小寫、壓空白後的標題文字集合；出現在 >= MARGIN_HEADING_MIN_PAGES 個不同頁面的 heading 才算。

    注意：
        TX001 每頁左欄的 'Notes' 被擷取成 heading level 1；若把它當章節邊界，
        跨頁的練習題區會在每一頁中斷。真正的章節標題（Session 2、Introduction）不會重複這麼多頁。
    """
    pages: dict[str, set] = {}
    for b in blocks:
        if b.block_type == "heading":
            key = " ".join((b.cleaned_text or "").lower().split())
            pages.setdefault(key, set()).add(b.page_number)
    return {k for k, v in pages.items() if len(v) >= MARGIN_HEADING_MIN_PAGES}


# 表格序列化後以 ' | ' 分隔儲存格；標記可能單獨佔一格（TX001 p.75 '|  | Practical Exercises'）
_CELL_SPLIT_RE = re.compile(r"\n|\s\|\s|^\|\s|\s\|$")


def _marker_segments(text: str) -> list[str]:
    """把區塊文字切成「行」與「表格儲存格」，供標記比對。

    參數：
        text：區塊文字（表格區塊是 serialize_table 的結果，儲存格以 ' | ' 分隔）。

    回傳：
        去掉頭尾空白後的非空片段清單，保持原順序。

    注意：
        標記必須「整個片段」符合才算，所以正文句子裡提到 'Activity 1' 不會被當成標記。
    """
    return [s.strip() for s in _CELL_SPLIT_RE.split(text) if s.strip()]


def _block_text(b: TextBlock) -> str:
    """取區塊的比對用文字。

    參數：
        b：區塊。

    回傳：
        優先 cleaned_text，沒有時用 raw_text，去掉頭尾空白。

    注意：
        02 清洗時呼叫（例如詞彙表區判斷）cleaned_text 已寫入；只讀 extracted 時才會退回 raw_text。
    """
    return (b.cleaned_text or b.raw_text or "").strip()


def marker_region_ids(blocks: list[TextBlock], start_re: re.Pattern,
                      sticky_re: Optional[re.Pattern] = None, stop_re: Optional[re.Pattern] = None) -> set[str]:
    """依「標記行 → 下一個章節標題」切出區域，回傳區域內的區塊 id。

    參數：
        blocks：已清洗文件的全部區塊（含已移除、待確認的；標記常出現在待確認的 figure_text 裡）。
        start_re：開始標記的 regex，對每一行／每個表格儲存格整段比對（見 `_marker_segments`）。
        sticky_re：符合的開始標記一旦出現，區域延續到文件結尾（例如書末的 Answer Key）。
        stop_re：結束標記（例如練習題區遇到 'What have you learned?'、'Glossary'）；
                 含結束標記的區塊本身不算在區域內。sticky 之後不再結束。

    回傳：
        區域內的 block_id 集合。開始標記所在的區塊：第一個片段就是標記才算在區域內
        （'…the sun (Figure 1.37).⏎Practical Exercises' 前面的正文不算）。

    區域結束：結束標記，或遇到 heading 區塊（不是開始標記、也不是頁邊欄標籤，見 `margin_heading_texts`）。

    注意：
        只用區塊順序與文字判斷，不看 section（TX001 的 section 被頁邊欄 'Notes' 污染，不可靠）。
        同一個區塊同時含開始與結束標記時，以結束為準（該區塊多半是待確認的 figure_text）。
    """
    margin = margin_heading_texts(blocks)
    ids: set[str] = set()
    inside = sticky = False
    for b in sorted(blocks, key=lambda x: x.order):
        text = _block_text(b)
        segments = _marker_segments(text)
        hits = [s for s in segments if start_re.match(s)]
        if not sticky and stop_re is not None and any(stop_re.match(s) for s in segments):
            inside = False
            continue
        if (b.block_type == "heading" and not hits and not sticky
                and " ".join(text.lower().split()) not in margin):
            inside = False
        if inside or (hits and segments[0] is hits[0]):
            ids.add(b.block_id)
        if hits:
            inside = True
            if sticky_re is not None and any(sticky_re.match(s) for s in hits):
                sticky = True
    return ids


def objectives_block_ids(blocks: list[TextBlock]) -> set[str]:
    """每節結尾「學習目標」方框所在的區塊 id（'After completing this session, you will be able to: • …'）。

    參數：
        blocks：已清洗文件的全部區塊。

    回傳：
        block_id 集合：標記區塊（第一個片段就是標記時），加上緊接在後面、以單一項目符號開頭的
        paragraph／list_item／figure_text（例：TX001 p.40 '• Evaluate the best bindi styles …'）。

    結束：遇到 heading，或第一個不是單一項目符號開頭的 paragraph／list_item
    （例：TX001 p.49 學習目標後直接接下一節正文 '• • It is better to use non-drying …'）。
    其他區塊（圖片、不以項目符號開頭的 figure_text、已被清洗移除的頁首頁尾）略過，不中斷也不納入。

    注意：
        學習目標是「學完能做什麼」的條列，不是知識內容；但後面的正文不能被一起排除，所以範圍刻意收窄。
    """
    ids: set[str] = set()
    inside = False
    for b in sorted(blocks, key=lambda x: x.order):
        text = _block_text(b)
        segments = _marker_segments(text)
        hits = [s for s in segments if OBJECTIVES_MARKER_RE.match(s)]
        if hits:
            inside = True
            if segments[0] is hits[0]:
                ids.add(b.block_id)
            continue
        if not inside:
            continue
        if b.is_removed:
            # 已被清洗移除的頁首頁尾（TX001 p.16 頁尾「Beauty and Wellness Industry… 5」）不是內容，不中斷區域
            continue
        if b.block_type == "heading":
            inside = False
        elif SINGLE_BULLET_RE.match(text) and b.block_type in ("paragraph", "list_item", "figure_text"):
            ids.add(b.block_id)
        elif b.block_type in ("paragraph", "list_item"):
            inside = False
    return ids


def exercise_block_ids(doc: Document) -> set[str]:
    """教材題組（練習題、活動、學習目標、答案）所在的區塊 id。

    參數：
        doc：已清洗的文件。

    回傳：
        block_id 集合，包含：
          1. EXERCISE_MARKER_RE 標記之後、下一個章節標題（或學習目標、詞彙表標記）之前的區塊
             （見 `marker_region_ids`；Answer Key 之後延續到文件結尾）；
          2. 學習目標條列（見 `objectives_block_ids`）；
          3. 任何含填空線（'_____'）的區塊。

    注意：
        這些區塊的文字是完整的，只是用途是考題／活動說明，不是知識內容；
        exclusion_reason 以 'exercise' 排除，cleaned 與 raw 都不改。
    """
    stop = re.compile(f"(?:{OBJECTIVES_MARKER_RE.pattern})|(?:{GLOSSARY_MARKER_RE.pattern})", re.I | re.M)
    ids = marker_region_ids(doc.blocks, EXERCISE_MARKER_RE, STICKY_EXERCISE_MARKER_RE, stop)
    # 圖說走既有的 figure_caption 流程：版面上常排在題組標記之後（TX001 p.38 Figure 1.37），不算題組
    ids -= {b.block_id for b in doc.blocks if b.block_type in ("figure_caption", "table_caption")}
    ids |= objectives_block_ids(doc.blocks)
    ids |= {b.block_id for b in doc.blocks if FILL_IN_BLANK_RE.search(b.cleaned_text or "")}
    return ids


# --- 教材實作活動（Practical Exercises 的 Activity N）與測驗的區分 ------------------------------
# 2026-10-22：練習區原本整區排除。測驗（Check Your Progress、選擇／填空／是非／問答、Answer Key）仍排除；
# 實作活動（Activity N → 任務與 Materials Required → Procedure → 條列步驟）是操作示範，
# 結構完整、且步驟與同一文件可匯出的正文方法重複時，附在那筆正文紀錄上當第二個原文定位，不另成一段
# （避免同一方法灌兩次，也不孤立匯出「Cover the eyelids and lips.」這種沒有主詞的短句）。
# 對不到正文的活動（多為課堂任務：討論、寫下用途、靜脈測試練習…）沒有對應的正文審核依據，另列原因不匯出。
QUIZ_MARKER_RE = re.compile(
    r"^\s*(?:Check Your Progress|Answer Key"
    r"|[A-H]\.\s*(?:Multiple[- ]Choice Questions|Fill in the Blanks?"
    r"|State whether the following statements are true or false|Subjective Questions"
    r"|Short Answer Questions|Long Answer Questions|Match the (?:Columns?|following)))\s*[:：]?\s*$",
    re.I | re.M,
)
ACTIVITY_MARKER_RE = re.compile(r"^\s*Activity\s+\d+\s*[:：]?\s*$", re.I)
PROCEDURE_LABEL_RE = re.compile(r"^\s*Procedure\s*[:：]?\s*$", re.I)
MATERIALS_RE = re.compile(r"\bMaterials Required\s*:", re.I)
STEP_BULLET_RE = re.compile(r"^\s*[•▪●◦]\s*")
# 步驟比對（見 `match_steps`）：逐句對正文單一片段，實詞 >= 70% 且否定一致才算對上；>= 80% 的步驟對上、
# 順序不倒置、且沒有任何一步明確相反，活動才算重複。
# TX001 實測：真正重複的 3 個活動（p.28–29 粉底、p.29–30 眼影、p.56 蒸臉）為 9/9、6/7、4/4；
# 門檻 0.6 時 p.35 色彩理論課堂任務會誤對到 p.12 單元簡介，所以取 0.7
ACTIVITY_STEP_MATCH = 0.7
ACTIVITY_MIN_MATCHED_SHARE = 0.8
ACTIVITY_MIN_STEPS = 3
_STOPWORDS = frozenset("the and you your with for onto into any are use using this that will from then have all "
                       "has can may its it is be to of on in or if a an by as at so".split())
# 否定詞一定保留成比對詞：「Never apply…」不能被當成和「Apply…」同一步
_NEGATIONS = frozenset({"no", "not", "never", "don", "avoid", "without"})
MATCH_TARGET_TYPES = ("table", "paragraph", "list_item")
# 練習區成員的原因：`exclusion_reason` 給 'exercise'，`classify_blocks` 再改記成活動原因；兩種輸入都要認得
PRACTICE_REGION_REASONS = frozenset({"exercise", "practical_activity_repeat", "practical_activity_unmatched"})


@dataclass(frozen=True)
class PracticalActivity:
    """一個結構完整的教材實作活動。

    block_ids：標記、任務說明、Procedure 標籤、步驟，依閱讀順序（不含已移除的頁首頁尾）。
    matched_block_id：同一文件中重複同一方法、可匯出的正文區塊；None = 對不到。
    step_match：(對上的步驟數, 步驟總數)。
    """

    title: str
    block_ids: tuple[str, ...]
    step_ids: tuple[str, ...]
    pages: tuple[int, ...]
    text: str
    matched_block_id: Optional[str]
    step_match: tuple[int, int]


def _stem_tokens(text: str) -> set[str]:
    """比對用詞集合：小寫英文詞去掉常用虛詞，簡單去字尾（-s、-ing），讓 hands／hand、applying／apply 對得上。"""
    out = set()
    for w in re.findall(r"[a-z]+", text.lower()):
        if w in _NEGATIONS:
            out.add(w)
            continue
        if w in _STOPWORDS or len(w) < 3:
            continue
        if w.endswith("ing") and len(w) > 5:
            w = w[:-3]
        out.add(w.rstrip("s") or w)
    return out


def _target_segments(b: TextBlock) -> list[str]:
    """正文區塊切成可比對先後的片段：表格逐列，其他依換行或句號。"""
    if b.block_type == "table" and b.cleaned_table_data:
        return [" ".join(c or "" for c in row) for row in b.cleaned_table_data]
    return [s for s in re.split(r"\n|(?<=[.!?])\s+", b.cleaned_text or "") if s.strip()]


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+")


def match_steps(steps: list[str], target: TextBlock) -> tuple[int, bool, bool]:
    """活動步驟對正文區塊：回傳 (對上的步驟數, 先後順序是否一致, 是否有明確相反的步驟)。

    逐句、局部比對（2026-10-22 獨立複查後改）：
    - 每個步驟先切句；每一句只對正文的「一個」片段（表格一列或一句），
      取不含否定詞的實詞重疊最多者（同分時優先否定一致的，再取較前面的）。
    - 該句實詞有 >= ACTIVITY_STEP_MATCH 出現在那一個片段，且兩邊否定詞集合完全相同，這句才算對上；
      一步的所有句子都對上，這步才算對上。否定只在同一對裡比，不能借用別步、別列的 not。
    - 實詞對得上、否定卻不同（「Do not cover the lips」對「Cover the lips」，或反過來）→ 明確相反，
      第三個回傳值為 True，呼叫端應整個活動不算重複，不能只扣一步後讓其餘步驟跨過 80% 門檻。
    - 順序以各句對到的片段位置判斷，必須不倒退。

    限制：詞袋比對，不懂條件句的作用域（if／only／unless）；只保證否定詞層級的一致。
    """
    segs = _target_segments(target)
    seg_core = [_stem_tokens(s) - _NEGATIONS for s in segs]
    seg_neg = [_stem_tokens(s) & _NEGATIONS for s in segs]
    matched, positions, contradicted = 0, [], False
    for step in steps:
        sentences = [x for x in _SENTENCE_SPLIT_RE.split(step) if (_stem_tokens(x) - _NEGATIONS)]
        if not sentences or not segs:
            continue
        ok, pos = True, []
        for sent in sentences:
            toks = _stem_tokens(sent)
            core, neg = toks - _NEGATIONS, toks & _NEGATIONS
            best = max(range(len(segs)), key=lambda i: (len(core & seg_core[i]), seg_neg[i] == neg, -i))
            covered = len(core & seg_core[best]) / len(core) >= ACTIVITY_STEP_MATCH
            if covered and seg_neg[best] != neg:
                contradicted = True
            if not covered or seg_neg[best] != neg:
                ok = False
            pos.append(best)
        if ok:
            matched += 1
            positions += pos
    return matched, positions == sorted(positions), contradicted


def practical_activities(doc: Document, reasons: dict[str, Optional[str]]) -> list[PracticalActivity]:
    """找出結構完整的實作活動，並比對是否重複同一文件中可匯出的正文方法。

    參數：
        doc：已清洗的文件。
        reasons：`exclusion_reason` 的結果（只把 None 的區塊當比對對象；活動本身應為 'exercise'）。

    回傳：
        PracticalActivity 清單，依閱讀順序。

    結構（缺一不算，維持 'exercise' 排除）：
        'Activity N' 標記 → 含 'Materials Required:' 的任務說明 → 'Procedure' 標籤 → >= 1 條項目符號步驟；
        之後遇到下一個活動、測驗標記、學習目標／詞彙表標記、章節標題或其他型別的區塊就結束。
        每個成員都要解析完整（ok、不待確認）、原本就在練習區（'exercise' 或標籤）、不在測驗區、
        沒有填空線，步驟不是問句。
    """
    blocks = sorted(doc.blocks, key=lambda x: x.order)
    stop = re.compile(f"(?:{OBJECTIVES_MARKER_RE.pattern})|(?:{GLOSSARY_MARKER_RE.pattern})", re.I | re.M)
    quiz = marker_region_ids(doc.blocks, QUIZ_MARKER_RE, STICKY_EXERCISE_MARKER_RE, stop)
    margin = margin_heading_texts(doc.blocks)
    units: list[list[TextBlock]] = []
    cur: Optional[list[TextBlock]] = None
    for b in blocks:
        if b.is_removed:
            continue                     # 跨頁時夾在中間的頁首頁尾（TX001 p.29 頁邊欄 'Notes'）
        text = _block_text(b)
        if ACTIVITY_MARKER_RE.match(text) and b.block_type in ("label", "heading", "paragraph"):
            cur = [b]
            units.append(cur)
            continue
        if cur is None:
            continue
        segs = _marker_segments(text)
        ends = (any(QUIZ_MARKER_RE.match(s) or stop.match(s) for s in segs)
                or (b.block_type == "heading" and " ".join(text.lower().split()) not in margin)
                or b.block_type not in ("paragraph", "label", "list_item"))
        if ends:
            cur = None
        else:
            cur.append(b)

    out: list[PracticalActivity] = []
    targets = [b for b in blocks if reasons.get(b.block_id) is None and b.block_type in MATCH_TARGET_TYPES]
    for u in units:
        kinds = [b.block_type for b in u]
        if (len(u) < 4 or kinds[1] != "paragraph" or not MATERIALS_RE.search(_block_text(u[1]))
                or kinds[2] != "label" or not PROCEDURE_LABEL_RE.match(_block_text(u[2]))):
            continue
        steps = u[3:]
        if not all(b.block_type == "list_item" and STEP_BULLET_RE.match(_block_text(b))
                   and "?" not in _block_text(b) for b in steps):
            continue
        if any(b.needs_review or b.parse_status != "ok" or b.block_id in quiz
               or FILL_IN_BLANK_RE.search(b.cleaned_text or "")
               or (b.block_type != "label" and reasons.get(b.block_id) not in PRACTICE_REGION_REASONS) for b in u):
            continue
        step_texts = [STEP_BULLET_RE.sub("", _block_text(b)) for b in steps]
        best: Optional[str] = None
        best_n = 0
        if len(steps) >= ACTIVITY_MIN_STEPS:
            for t in targets:
                n, ordered, contradicted = match_steps(step_texts, t)
                if (ordered and not contradicted and n >= ACTIVITY_MIN_MATCHED_SHARE * len(steps)
                        and n > best_n):
                    best, best_n = t.block_id, n
        out.append(PracticalActivity(
            title=_block_text(u[0]),
            block_ids=tuple(b.block_id for b in u),
            step_ids=tuple(b.block_id for b in steps),
            pages=tuple(sorted({b.page_number for b in u if b.page_number})),
            text="\n".join(_block_text(b) for b in u),
            matched_block_id=best,
            step_match=(best_n, len(steps)),
        ))
    return out


def is_boilerplate(b: TextBlock) -> bool:
    text = (b.cleaned_text or "").strip()
    return len(text) <= BOILERPLATE_MAX_CHARS and bool(BOILERPLATE_RE.search(text))


def top_section(b: TextBlock) -> str:
    return (b.section or "").split(" > ")[0]


def body_start_order(doc: Document) -> Optional[int]:
    """第一個正文章節的 order；找不到（例如沒有 Abstract 的網頁文章）回傳 None，不排除任何前言。"""
    for b in sorted(doc.blocks, key=lambda x: x.order):
        if BODY_START_RE.match(top_section(b)):
            return b.order
    return None


def exclusion_reason(b: TextBlock, body_start: Optional[int], keyword_ids: frozenset = frozenset(),
                     exercise_ids: frozenset = frozenset()) -> Optional[str]:
    """回傳排除原因；None 表示可以用於 RAG。exercise_ids 見 `exercise_block_ids`。"""
    if b.is_removed:
        return "removed"
    if not (b.cleaned_text or "").strip():
        return "no_text"
    if b.needs_review or b.parse_status != "ok":   # partial（例如數學字型對應錯誤）一定也是待確認
        return "needs_review"
    if b.block_type == "toc_entry":
        return "table_of_contents"
    if b.block_type not in CONTENT_TYPES:
        return "not_content"                    # heading、label、equation_label：資訊已在 section 裡
    if body_start is not None and b.order < body_start:
        return "front_matter"
    if EXCLUDED_SECTION_RE.match(top_section(b)):
        return "excluded_section"
    if b.block_id in keyword_ids:
        return "keywords"
    if b.block_id in exercise_ids:
        return "exercise"
    if is_boilerplate(b):
        return "boilerplate"
    return None


def classify_blocks(doc: Document) -> dict[str, Optional[str]]:
    """{block_id: 排除原因或 None}。

    實作活動（見 `practical_activities`）的成員由 'exercise' 改記：
    'practical_activity_repeat'（步驟與正文重複，由 05 附在正文那筆當第二定位）或
    'practical_activity_unmatched'（對不到正文，不匯出）。測驗與答案維持 'exercise'。
    """
    start = body_start_order(doc)
    keywords = frozenset(keyword_block_ids(doc))
    exercises = frozenset(exercise_block_ids(doc))
    reasons = {b.block_id: exclusion_reason(b, start, keywords, exercises) for b in doc.blocks}
    for act in practical_activities(doc, reasons):
        tag = "practical_activity_repeat" if act.matched_block_id else "practical_activity_unmatched"
        for i in act.block_ids:
            if reasons[i] == "exercise":
                reasons[i] = tag
    return reasons


def eligible_blocks(doc: Document) -> list[TextBlock]:
    reasons = classify_blocks(doc)
    return [b for b in doc.blocks if reasons[b.block_id] is None]
