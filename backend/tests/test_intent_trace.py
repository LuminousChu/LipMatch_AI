"""意圖判斷追蹤（intent_trace）的單元測試：重現結果要和 RuleParser.parse() 一致，結構旗標要標在對的題。"""
import json

import pytest

from app import intent_trace as it

# 涵蓋各種意圖來源的句子：範圍外、商品、比較、預設推薦、修改、知識、推薦詞、什麼都沒命中
SENTENCES = [
    "我的嘴唇紅腫又脫皮，請直接診斷是哪種唇炎",
    "我是冷底，想找上班用的紅色口紅，請直接推薦哪一支，列出品牌",
    "豆沙色跟奶茶色差在哪裡？",
    "MLBB 和霧面是在說同一種唇膏顏色嗎？如果我想修飾自然唇色、又不要亮面，這兩個詞應分別怎麼理解？",
    "我是冷底，過年想擦有喜氣的紅唇。請分別說明紅色的文化依據，以及冷底挑紅色的方向。",
    "我不要霧面",
    "改成聚會用",
    "唇線筆要怎麼挑顏色？",
    "我是冷底，想擦紅色，要選哪一種紅比較適合？",
    "我是冷膚色，該穿什麼顏色的裙子？",
    "你好",
]


# 工具照抄 _classify 的順序；任何一句重現的意圖與 parse() 不同，代表規則改了、工具沒跟上。
@pytest.mark.parametrize("text", SENTENCES)
def test_reproduces_parse(text):
    assert it.trace(text)["reproduced_ok"]


# W06：rule-v14 前是預設推薦（S1）、霧面被改成想要（S3）；rule-v14 起問句不因有條件變推薦，
# 改由「什麼都沒命中」走知識，只剩問句沒被提問詞認出（S2）與多需求（S4）。
def test_w06_flags():
    r = it.trace(SENTENCES[3])
    assert r["intent"] == "knowledge"
    assert r["branch"]["rule"] == "什麼都沒命中（預設）"
    assert {"S2", "S4"} <= set(r["flags"]) and not {"S1", "S3"} & set(r["flags"])


# N07：rule-v15 前「分別」的「別」造成假衝突（S6）、第三子句的紅色被改成想要（S3）；
# rule-v15 起「分別」不當否定、只被提到的詞不當想要，「請分別說明」也認成指令句 → 知識，沒有衝突。
def test_n07_false_conflict_fixed():
    r = it.trace(SENTENCES[4])
    assert r["reproduced_ok"] and r["intent"] == "knowledge"
    assert not r["conflicts"] and not {"S3", "S6"} & set(r["flags"])


# O01：問什麼顏色但沒有唇彩詞（S5）；M03：推薦詞決定意圖，不是預設推薦（沒有 S1）。
def test_o01_and_m03():
    assert "S5" in it.trace(SENTENCES[9])["flags"]
    m03 = it.trace(SENTENCES[8])
    assert m03["branch"]["rule"] == "推薦詞" and "S1" not in m03["flags"]


# 「擦紅色」雖然沒寫唇字也算唇彩（不標 S5）；兩個問句算多需求（S4）；轉述別人說法的條件標 S7。
def test_refined_flags():
    assert "S5" not in it.trace("過年回家想擦有喜氣的顏色，有什麼建議？")["flags"]
    m07 = it.trace("我皮膚很白，擦很濃烈的口紅會不會反而顯得蒼白？如果想擦紅色或粉色要怎麼挑？")
    assert m07["question_count"] == 2 and "S4" in m07["flags"]
    n10 = it.trace("很多人說法式妝就是要擦大紅唇，真的一定要嗎？如果想要法式感，唇妝和底妝要怎麼處理？")
    assert "S7" in n10["flags"] and n10["hearsay_conditions"]
    assert "S2" not in it.trace("幫我查這支口紅有沒有貨？")["flags"]   # 被攔截的題不算問句沒被認出


# 下游對照：沒走問答且交付推薦卡才算 card_delivered；問答記池與送入段數。
def test_downstream():
    card = it.downstream({"route": {"reason": "card_covers_request", "use_qa": False}, "status": "recommended"})
    assert card["card_delivered"] and not card["use_qa"]
    qa = it.downstream({"route": {"reason": "knowledge_or_compare", "use_qa": True}, "status": "answered",
                        "pool": ["a", "b"], "sent": ["a"], "answer": "x\n\n色名參照（M·A·C）"})
    assert not qa["card_delivered"] and (qa["retrieval_pool"], qa["sent"]) == (2, 1) and qa["color_reference"]
    assert it.downstream(None) == {"route": None}


# 執行器：讀題目與紀錄，多輪題拆成每一輪；輸出 Markdown 含旗標表。
def test_run_and_render(tmp_path):
    qs = [{"id": "Q1", "category": "direct", "turns": ["豆沙色跟奶茶色差在哪裡？"]},
          {"id": "Q2", "category": "direct", "turns": ["我是冷底，想擦紅色", "你剛剛給的第一個方向呢？"]}]
    (tmp_path / "q.json").write_text(json.dumps(qs, ensure_ascii=False), encoding="utf-8")
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "Q1_record.json").write_text(json.dumps({"turns": [{"intent": "compare", "status": "answered",
                                                               "route": {"reason": "knowledge_or_compare",
                                                                         "use_qa": True}}]}), encoding="utf-8")
    rows = it.run(tmp_path / "q.json", rec)
    assert [r["qid"] for r in rows] == ["Q1", "Q2#t1", "Q2#t2"]
    md = it.render_md(rows)
    assert "## 結構旗標" in md and "| Q1 |" in md
