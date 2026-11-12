"""RAG 篩選規則（lipmatch/eligibility.py）與出處（lipmatch/sources.py）的測試。"""

from __future__ import annotations

import pytest

from lipmatch.citations import find_citations
from lipmatch.eligibility import classify_blocks, eligible_blocks
from lipmatch.sources import SourceInfo, load_sources, reference_string, short_citation
from schemas import Document, SourceManifest, TextBlock


def _doc(specs: list[tuple[str, str, str]], **block_kw) -> Document:
    """specs: [(block_type, section, text)]"""
    m = SourceManifest(doc_id="t", source_path="x", source_type="pdf", sha256="0" * 64,
                       file_size_bytes=0, extractor="test")
    blocks = [TextBlock(doc_id="t", block_id=f"t_b{i:03d}", block_type=bt, order=i, page_number=i + 1,
                        section=sec, raw_text=txt, cleaned_text=txt, search_text=txt,
                        heading_level=1 if bt == "heading" else None, **block_kw)
              for i, (bt, sec, txt) in enumerate(specs)]
    return Document(stage="cleaned", manifest=m, blocks=blocks)


def test_front_matter_references_toc_and_headings_excluded():
    doc = _doc([
        ("paragraph", "Thesis Title", "The candidate confirms that the work submitted is her own."),
        ("paragraph", "Acknowledgements", "I would like to thank my supervisor."),
        ("toc_entry", "Table of Contents", "Chapter 1 …… 6"),
        ("paragraph", "Abstract", "This study investigates lipstick shades."),
        ("heading", "Chapter 5. Discussion", "5.5.2 Culture Implication"),
        ("paragraph", "Chapter 5. Discussion > 5.5.2 Culture Implication", "Orange suits yellowish skin."),
        ("paragraph", "Reference List", "Zhang, 2019. Orange lipstick."),
        ("paragraph", "Appendix 2. Experimental Results", "This table demonstrates the results."),
    ])
    r = classify_blocks(doc)
    assert [r[b.block_id] for b in doc.blocks] == [
        "front_matter", "front_matter", "table_of_contents", None,
        "not_content", None, "excluded_section", None,
    ]


def test_no_body_marker_means_no_front_matter_exclusion():
    # 網頁文章沒有 Abstract／Chapter 1：不能把整篇當成前言丟掉
    doc = _doc([("paragraph", "Best lipstick for warm undertones", "Coral and brick red flatter warm skin.")])
    assert len(eligible_blocks(doc)) == 1


def test_quality_flags_excluded():
    doc = _doc([("paragraph", "Abstract", "x")], needs_review=True, review_reasons=["check"])
    assert classify_blocks(doc)["t_b000"] == "needs_review"


def test_citation_strings():
    src = SourceInfo(title="Investigation on Facial Skin Colour Perception Induced by Different Lipstick Shades",
                     authors=["Yifei Chen"], author_citation="Chen, Y.", year=2024,
                     doc_type="Master by Research thesis", institution="University of Leeds, School of Design",
                     verified_from="p.1")
    assert short_citation(src, [75]) == "(Chen, 2024, p.75)"
    assert short_citation(src, [13, 12, 13]) == "(Chen, 2024, pp.12–13)"
    assert short_citation(src, []) == "(Chen, 2024)"
    assert reference_string(src) == ("Chen, Y. (2024). Investigation on Facial Skin Colour Perception Induced by "
                                     "Different Lipstick Shades [Master by Research thesis]. "
                                     "University of Leeds, School of Design.")


@pytest.mark.parametrize("text, expected", [
    # Leeds 論文中的真實寫法
    ("deemed particularly attractive to yellowish skin because it appears more natural (Zhang, 2019).",
     ["Zhang, 2019"]),
    ("supports previous findings (Russell, 2003; Lu et, al., 2022) which", ["Russell, 2003", "Lu et al., 2022"]),
    ("(Edwards & Duntley, 1939; Frost, 1988, 2005; Jablonski & Chaplin, 2000, 2002)",
     ["Edwards and Duntley, 1939", "Frost, 1988", "Frost, 2005", "Jablonski and Chaplin, 2000",
      "Jablonski and Chaplin, 2002"]),
    ("(Pointer, Attridge and Jacobson, 2001)", ["Pointer, Attridge and Jacobson, 2001"]),
    ("as shown by Kiritani et al. (2017) and Berns et al.(1993)", ["Kiritani et al., 2017", "Berns et al., 1993"]),
    ("Similarly, Jones and Hill (1993) found", ["Jones and Hill, 1993"]),
    ("Zhang (2019) argued", ["Zhang, 2019"]),
    ("lipsticks from Chanel Beauty (2023) were (Chanel Beauty, 2023)", ["Chanel Beauty, 2023"]),
    ("(CIE, 2019)", ["CIE, 2019"]),
    # 不是引用
    ("The total colour difference for dark orange, which is 30.39 (CN) and 31.17 (CA).", []),
    ("Data were collected in (2012) and analysed in Table 4.1.", []),
    ("The experiment was conducted in April 2024 with 75 participants.", []),
])
def test_find_citations(text, expected):
    assert find_citations(text) == expected


def test_leeds_p75_attributes_claim_to_cited_work(leeds_full_cleaned):
    b = next(b for b in leeds_full_cleaned.blocks if b.page_number == 75 and "yellowish skin" in b.cleaned_text)
    assert "Zhang, 2019" in b.cited_works


def test_sources_registry_has_leeds():
    assert load_sources()["TH013"].year == 2024


def test_leeds_eligibility(leeds_full_cleaned):
    reasons = classify_blocks(leeds_full_cleaned)
    by_id = {b.block_id: b for b in leeds_full_cleaned.blocks}
    ok = [by_id[k] for k, v in reasons.items() if v is None]
    assert ok and all(b.parse_status == "ok" and not b.needs_review and not b.is_removed for b in ok)
    assert not any(b.section.startswith(("Reference List", "Acknowledgements", "Table of Contents")) for b in ok)
    assert min(b.page_number for b in ok) == 4                 # 從 Abstract 開始，p.1–3 是前言
    assert reasons["TH013_p0014_b004"] == "needs_review"  # 數學字型對應錯誤的段落
    assert any(b.page_number == 75 and "yellowish skin" in b.cleaned_text for b in ok)
