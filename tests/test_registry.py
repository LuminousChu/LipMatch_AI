"""文獻主清冊、ETL 設定、書目三者一致（2026-10-17 改編號後的防護）。

- 已登錄 ETL 的清冊筆數 = config/documents.json = config/sources.json 的 doc_id 集合。
- 已登錄的 doc_id 依新編碼規則（TH／AR／PR／TX／GD＋三位數），原始檔名以 doc_id 開頭。
- 舊編號（LEEDS_2024、TW001、CHARTON_2026）只能出現在「原 doc_id …」「（原 …）」「資料庫舊文件 …」註記中。
"""
import json
import re

from tests.conftest import ROOT

RETIRED = {"LEEDS_2024": "TH013", "TW001": "TH014", "CHARTON_2026": "AR015"}
NEW_ID = re.compile(r"^(TH|AR|PR|TX|GD)\d{3}$")


def _load():
    registry = json.loads((ROOT / "manifests" / "literature_registry.json").read_text(encoding="utf-8"))
    docs = json.loads((ROOT / "config" / "documents.json").read_text(encoding="utf-8"))["documents"]
    sources = json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))
    return registry, docs, {k: v for k, v in sources.items() if not k.startswith("_")}


def test_registered_sets_match():
    registry, docs, sources = _load()
    registered = {r["doc_id"]: r for r in registry["records"] if r["registration_status"] == "etl_registered"}
    assert len(registry["records"]) == registry["record_count"]
    assert set(registered) == {d["doc_id"] for d in docs} == set(sources)
    assert len(registered) == 26  # 22 份＋PR032～PR035（ingest-pongo）
    for d in docs:
        assert registered[d["doc_id"]]["local_path"] == d["path"], d["doc_id"]


def test_registered_ids_follow_coding_rule():
    _, docs, _ = _load()
    for d in docs:
        assert NEW_ID.match(d["doc_id"]), d["doc_id"]
        assert d["path"].rsplit("/", 1)[-1].startswith(d["doc_id"] + "_"), d["path"]


def test_retired_ids_only_in_former_id_notes():
    registry, docs, sources = _load()
    assert not set(RETIRED) & ({d["doc_id"] for d in docs} | set(sources)
                               | {r["doc_id"] for r in registry["records"]})
    text = (ROOT / "manifests" / "literature_registry.json").read_text(encoding="utf-8")
    for old in RETIRED:
        for m in re.finditer(re.escape(old), text):
            before = text[max(0, m.start() - 10):m.start()]
            assert before.endswith(("原 doc_id ", "（原 ", "資料庫舊文件 ")), f"{old} 出現在非註記位置：…{before}{old}"
    by_id = {r["doc_id"]: r for r in registry["records"]}
    for old, new in RETIRED.items():
        assert by_id[new]["notes"].startswith(f"原 doc_id {old}")
