"""2026-11-03 的修改：focus-v1（N01、W03、N05）、limit-v2（範圍外限制句）、
insufficient-assist-v1（P08）、neighbor-v1（N10 鄰段補位）。

主張文字取自 T4 跑測 05912ad 的實際回答（data/review/route-v4-compare-20261101/ai.json）。
"""
from __future__ import annotations

from app import answer_focus as F
from app import exit_assist as E
from app import postprocess as PP
from app import preferences as pref
from app import retrieval as R

N01 = "我嘴唇容易乾，但又想擦霧面，有什麼辦法讓它不要看起來乾乾的？"
W03 = "豆沙色跟奶茶色差在哪裡？"
N05 = "過年回家想擦有喜氣的顏色，有什麼建議？"
P08 = "我明天要面試。擦藍調紅比擦奶茶色，會讓面試官覺得我更專業的機率增加幾個百分點？請給研究數據和樣本條件。"


def _delivered(texts, limits=()):
    """組一份最小的 Delivered：每條主張都是 answer 角色、引用同一個單位。"""
    return {"status": "delivered", "claims": [{"claim_no": i, "text": t, "unit_ids": ["u1"], "role": "answer"}
                                              for i, t in enumerate(texts, 1)],
            "limitations": list(limits)}


# ---------------------------------------------------------------- focus-v1
def test_n01_unasked_ingredients_trimmed():
    """N01：沒問成分 → 「含有神經醯胺或荷荷巴油的」拿掉、主要做法保留；旗標記前後文字。"""
    out = F.apply(_delivered(["在塗抹前使用含有神經醯胺或荷荷巴油的護唇膏進行短暫濕敷，並擦掉多餘油脂。",
                              "選擇含有保濕因子或含油、透明質酸的保濕霧面配方。"]), N01)
    texts = [c["text"] for c in out["claims"]]
    assert texts == ["在塗抹前使用護唇膏進行短暫濕敷，並擦掉多餘油脂。", "選擇保濕霧面配方。"]
    assert {f["flag"] for f in out["flags"]} == {"unasked_ingredient_trimmed"}
    assert out["focus_version"] == F.FOCUS_VERSION


def test_ingredient_kept_when_asked():
    """問題本身問成分（或點名成分）就不動。"""
    text = "選擇含有神經醯胺的護唇膏。"
    out = F.apply(_delivered([text]), "護唇膏要選什麼成分？")
    assert out["claims"][0]["text"] == text


def test_w03_skin_effect_segment_dropped_and_color_name_limit():
    """W03：沒提膚色 → 「與黃皮互補提亮膚色」那段拿掉；問色名差別 → 加色名不是跨品牌定義的限制。"""
    out = F.apply(_delivered(["奶茶色適合日系透明感妝容，但對深唇不友好，容易顯髒；豆沙色則能與黃皮互補提亮膚色，且遮蓋力較高。"]),
                  W03)
    assert out["claims"][0]["text"] == "奶茶色適合日系透明感妝容，但對深唇不友好，容易顯髒。"
    assert "「豆沙色」「奶茶色」這類色名是各品牌自己取的，同一個名字在不同品牌可能是差很多的顏色。" in out["limitations"]


def test_skin_effect_kept_when_user_mentions_skin():
    """使用者自己提到膚色就不拿掉（不擋使用者要的內容）。"""
    text = "豆沙色能與黃皮互補提亮膚色。"
    out = F.apply(_delivered([text]), "我是黃皮，豆沙色適合嗎？")
    assert out["claims"][0]["text"] == text


def test_skin_effect_whole_claim_removed():
    """整條都是膚色效果 → 整條不交付，記 removed。"""
    out = F.apply(_delivered(["豆沙色能提亮膚色。", "豆沙色是紅色融入灰色調。"]), W03)
    assert [c["text"] for c in out["claims"]] == ["豆沙色是紅色融入灰色調。"]
    assert out["removed"][0]["rule"] == "unverified_skin_effect"


def test_n05_festive_keeps_red_drops_other_colors():
    """N05：已有紅色象徵喜氣 → 轉去桃色、橙色的主張不交付；加文化背景限制。"""
    out = F.apply(_delivered(["在華人文化中，紅色象徵著好運、快樂與繁榮，是極具喜氣的顏色。",
                              "如果您是暖色調膚色，可以選擇暖色系的桃色或珊瑚色。",
                              "如果您是深色膚色，可以選擇最鮮豔的橙色或焦橙色。"]), N05)
    assert [c["claim_no"] for c in out["claims"]] == [1]
    assert {r["rule"] for r in out["removed"]} == {"festive_offtopic_color"}
    assert F.FESTIVE_LIMIT in out["limitations"]


def test_festive_without_red_claim_unchanged():
    """回答裡沒有紅色喜氣那條，就不判斷其他色系（避免把唯一的回答刪光）。"""
    out = F.apply(_delivered(["可以選擇桃色或珊瑚色。"]), N05)
    assert len(out["claims"]) == 1 and F.FESTIVE_LIMIT not in out["limitations"]


def test_color_name_limit_needs_definition_question():
    """只提色名、沒有問定義或差別，不加限制。"""
    assert F.color_name_limit("我是冷底，想擦豆沙色去上班") is None
    assert F.color_name_limit("乾燥玫瑰色是什麼樣的顏色？") == "「乾燥玫瑰色」這類色名是各品牌自己取的，同一個名字在不同品牌可能是差很多的顏色。"


def test_color_name_limit_survives_color_code_rewrite():
    """T4 30a9e1d：色名參照附色塊時，「色號＋沒有」的限制會被改寫成色碼固定句；色名限制不能被吃掉。"""
    from app import color_reference as CR
    lim = F.color_name_limit(W03)
    out, changed = CR.reconcile_limitations([lim], {"matches": [{"n": 2}]})
    assert out == [lim] and changed == []


# ---------------------------------------------------------------- limit-v2
def test_outside_limit_only_when_relevant():
    """範圍外的限制句只寫碰到的那一類；非唇彩對象（O01 裙子）不加。"""
    assert pref.outside_limit_note("診斷") == "不提供膚況、用藥或過敏診斷。"
    assert pref.outside_limit_note("有沒有貨") == "不提供下單、付款或庫存查詢。"
    assert pref.outside_limit_note("幫我看照片") == "不提供照片判讀。"
    assert pref.outside_limit_note("什麼顏色的裙子") is None
    parsed = pref.RuleParser().parse("我是冷膚色，該穿什麼顏色的裙子？")
    lims = pref.limitations(pref.empty_state(),
                            parsed.intent, parsed.secondary_intents, outside_text=parsed.outside_text)
    assert not any("照片判讀" in x or "診斷" in x for x in lims)


def test_system_note_heading_plain():
    """「系統註記」改成「補充說明」，商品註記不再用內部用語。"""
    text = PP.render_delivered({"claims": [], "system_notes": [PP.PRODUCT_NOTE]})
    assert text.startswith("補充說明：") and "主張" not in text and "交付" not in text


# ---------------------------------------------------------------- insufficient-assist-v1
def test_p08_insufficient_question():
    """P08：拿掉要數字的子句，剩下的需求改問一般做法；不是要數字的問題不代答。"""
    assert E.insufficient_question(P08) == "我明天要面試，擦藍調紅比擦奶茶色，一般可以怎麼選？"
    assert E.insufficient_question("豆沙色適合什麼場合？") is None
    assert E.insufficient_question("請給研究數據。") is None


# ---------------------------------------------------------------- neighbor-v1
class _Rows(list):
    """模擬 psycopg 的查詢結果：可迭代，也有 fetchall()。"""
    def fetchall(self):
        return list(self)


class _FakeConn:
    """只回答 neighbor_fill 會下的三種查詢：取原文、active 索引、續段距離。"""
    def __init__(self, texts):
        self.texts = texts

    def execute(self, sql, params=()):
        if "quote_text FROM usable_chunks" in sql:
            return _Rows({"chunk_key": k, "quote_text": t} for k, t in self.texts.items() if k in params[0])
        if "index_versions" in sql:
            return _Rows([{"id": 1, "embedding_dim": 768}])
        keys = params[-1]
        return _Rows({"chunk_id": 900 + i, "chunk_key": k, "doc_key": k.split("_")[0], "version_label": "v1",
                      "page_start": None, "page_end": None, "content_sha256": "x", "index_label": "idx",
                      "distance": 0.6} for i, k in enumerate(keys))


class _Emb:
    def embed(self, texts):
        return [[0.1] * 768 for _ in texts]


def _hit(rank, key):
    return R.Hit(rank=rank, chunk_id=rank, chunk_key=key, doc_key=key.split("_")[0], version_label="v1",
                 page_start=None, page_end=None, content_sha256="x", index_label="idx", distance=0.2)


def test_n10_neighbor_fill(monkeypatch):
    """N10：h0178 第 1 名、h0177 只是社群嵌入、h0179 以 Get the look 開頭 → 補在 h0178 後面，池子大小不變。"""
    monkeypatch.setattr(R, "active_index", lambda conn: {"id": 1, "embedding_dim": 768})
    conn = _FakeConn({"PR012_h0179": "Get the look: You can skip the foundation...",
                      "PR012_h0180": "View this post on Instagram"})
    hits = [_hit(1, "PR012_h0178"), _hit(2, "AR004_p0003_b006#s3"), _hit(3, "PR015_h0153")]
    out, trace = R.neighbor_fill(conn, hits, "user", "法式妝", _Emb(), k=3)
    assert [h.chunk_key for h in out] == ["PR012_h0178", "PR012_h0179", "AR004_p0003_b006#s3"]
    assert [h.rank for h in out] == [1, 2, 3]
    assert trace == [{"anchor": "PR012_h0178", "anchor_rank": 1, "added": "PR012_h0179", "cosine": 0.4}]


def test_neighbor_not_how_to_stops(monkeypatch):
    """第一個有內容的續段不是做法段就不補；不是網頁編號的段落不看。"""
    monkeypatch.setattr(R, "active_index", lambda conn: {"id": 1, "embedding_dim": 768})
    conn = _FakeConn({"PR012_h0175": "Get the look: ...", "PR012_h0174": "Winged eyeliner is timeless"})
    hits = [_hit(1, "PR012_h0173"), _hit(2, "AR002_p0011_b009#s2")]
    out, trace = R.neighbor_fill(conn, hits, "user", "q", _Emb(), k=2)
    assert [h.chunk_key for h in out] == ["PR012_h0173", "AR002_p0011_b009#s2"] and trace == []


# ---------------------------------------------------------------- insufficient-assist-v1：同請求第二次問答
def test_second_qa_does_not_write_retrieval_hits():
    """T4 跑測 P08 實際出錯（retrieval_hits 主鍵撞號 → 500）：write_hits=False 時只更新 requests，不寫召回表。"""
    from app import qa

    class _Conn:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=()):
            self.sql.append(sql)

        def cursor(self):
            raise AssertionError("不應寫 retrieval_hits")

    conn = _Conn()
    res = qa.QaResult("answered", hits=[_hit(1, "PR012_h0178")])
    qa.record(conn, "rid", res, write_hits=False)
    assert len(conn.sql) == 1 and "UPDATE requests" in conn.sql[0]
