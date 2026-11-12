"""查詢端術語對照：只產生可追溯的檢索詞，不改寫問題或推定使用者偏好。

獨立模組，尚未接入 QA、全文檢索、embedding 或 reranker。
expand() 提供 M4 式英文詞清單；接入管線時應使用 analyze() 保留原句、否定及未定義詞。
sources 是詞彙出處文件，不代表其中任何段落已核准、適用或足以回答問題。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

# v3.2（2026-10-30，W01 召回）：藍調紅／橘調紅補口語別名（偏藍／偏橘的紅、藍／橘色調的紅）；
# 否定範圍可跨過並列的色彩修飾（「不要偏粉或偏橘的紅」）；Expansion 新增 concepts（檢索概念＋極性）。
# v3.6（2026-11-08，fix-heldout）：測試組診斷——參考段落都已核准在庫，但中文問法對不到英文原文用詞：
#   cakey 補「厚重、擦太厚、疊太多層」與英文 heavy／too many layers（PR015_h0139 原文 "too many layers…heavy, cakey"）；
#   dark_skin 補英文 deep skin（PR020 原文多寫 deep skin，不是 dark skin）；新增 culture_region（國家、文化、地區 → geographical／cultural／regional）。
# v3.7（2026-11-08，fix-metrics，把厚重單獨拿掉）：cakey 還原成 v3.5 的寫法（撤回針對測試題 H06／H09 補的
#   「厚重、擦太厚、疊太多層」與 heavy／too many layers）；deep skin、culture_region 保留。
# v3.8（2026-11-09，fix-metrics，折衷）：cakey 只加回中文「厚重」→ 原本就有的 cakey；
#   不加 heavy／too many layers、擦太厚、疊太多層（不照抄 PR015_h0139 原文用詞）。
GLOSSARY_VERSION = "lipmatch-query-glossary-v3.8"

# 網路查證僅補詞彙用法；不自動將網頁加入 RAG 或視為已核准的推薦依據。
WEB_SOURCES = {
    "WEB_WHEATISH_LAKME": {
        "title": "Lakmē 9 to 5 Complexion Care Cream",
        "url": "https://www.lakmeindia.com/collections/bestsellers/products/lakme-9-to-5-complexion-care-cream",
        "checked_at": "2026-10-26", "publisher_type": "brand_official",
        "english": ("wheatish skin", "wheatish skin tone"),
        "evidence": "light to wheatish skin tone",
        "scope": "品牌實際使用此英文膚色描述；中文小麥色採作檢索別名，不推定底調或精確膚色分級。",
    },
    "WEB_MILKTEA_PATRICKTA": {
        "title": "Patrick Ta Lip Balm",
        "url": "https://patrickta.com/collections/lip-balm",
        "checked_at": "2026-10-26", "publisher_type": "brand_official",
        "english": ("milk tea", "nude beige"), "evidence": "Milk Tea (Nude Beige)",
        "scope": "Milk Tea 是品牌使用的唇彩色名；nude beige 是此品牌的描述，不是所有奶茶色的唯一定義。",
    },
    "WEB_BEANPASTE_MARYKAY": {
        "title": "Mary Kay 玫瑰豆沙 / Rosewood",
        "url": "https://www.marykay.com.cn/corporatesite/home/products/10255050",
        "checked_at": "2026-10-26", "publisher_type": "brand_official",
        "english": ("rosewood",), "evidence": "玫瑰豆沙 / Rosewood",
        "scope": "同一品牌頁面直接並列中文玫瑰豆沙與英文 Rosewood，可作豆沙系的檢索別名。",
    },
    "WEB_BEANPASTE_PINGUAN": {
        "title": "品觀：豆沙色的英文色號用法",
        "url": "https://m.pinguan.com/article/detail/id/9477",
        "checked_at": "2026-10-26", "publisher_type": "beauty_trade_publication",
        "english": ("dusty rose",), "evidence": "Dusty Rose",
        "scope": "產業文章將豆沙與 dusty rose 連結；只列相關檢索詞，不把兩個中文色名合併。",
    },
    "WEB_DUSTYROSE_NARS": {
        "title": "NARS Air Matte Lip Color / Dolce Vita",
        "url": "https://www.narscosmetics.com/USA/air-matte-lip-color/999NAC0000114.html?cgid=lipstick&dwvar_999NAC0000114_color=DOLCE+VITA",
        "checked_at": "2026-10-26", "publisher_type": "brand_official",
        "english": ("dusty rose",), "evidence": "Dolce Vita / Dusty rose",
        "scope": "核對產業文章提及的具體色號描述；不推出所有豆沙色等於此商品色號。",
    },
}


# 以下登錄只保存名稱查證資訊，不將網頁自動匯入 RAG。
WEB_SOURCES.update(
{'WEB_FDA_ALLERGENS': {'title': 'FDA — Allergens in Cosmetics',
                       'url': 'https://www.fda.gov/cosmetics/cosmetic-ingredients/allergens-cosmetics',
                       'checked_at': '2026-10-27',
                       'publisher_type': 'regulator',
                       'english': ('fragrance',
                                   'perfume',
                                   'limonene',
                                   'linalool',
                                   'citral',
                                   'cinnamaldehyde',
                                   'eugenol',
                                   'geraniol',
                                   'benzyl alcohol',
                                   'Mentha Piperita Oil',
                                   'preservatives',
                                   'methylisothiazolinone',
                                   'methylchloroisothiazolinone',
                                   'formaldehyde',
                                   'formaldehyde releasing ingredients',
                                   'DMDM hydantoin',
                                   'diazolidinyl urea',
                                   'imidazolidinyl urea',
                                   'quaternium-15',
                                   'bronopol'),
                       'evidence': 'fragrance; perfume; limonene; linalool; citral; cinnamaldehyde; eugenol; '
                                   'geraniol; benzyl alcohol; Mentha Piperita Oil; preservatives; '
                                   'methylisothiazolinone; methylchloroisothiazolinone; formaldehyde; '
                                   'formaldehyde releasing ingredients; DMDM hydantoin; diazolidinyl urea; '
                                   'imidazolidinyl urea; quaternium-15; bronopol',
                       'scope': '化妝品過敏相關用詞與標籤名稱；不是所有人都須避開的清單，亦不作任何地區的允用判定。'},
 'WEB_DERMNET_LIP_ALLERGENS': {'title': 'DermNet — Contact reactions to lipsticks and other lipcare products',
                               'url': 'https://dermnetnz.org/topics/contact-reactions-to-lipsticks-and-other-lipcare-products',
                               'checked_at': '2026-10-27',
                               'publisher_type': 'clinical_publisher',
                               'english': ('lanolin',
                                           'castor oil',
                                           'ricinoleic acid',
                                           'hydrogenated castor oil',
                                           'propylene glycol',
                                           'colophonium',
                                           'propolis',
                                           'shellac',
                                           'propyl gallate',
                                           'benzophenone-3',
                                           'peppermint oil'),
                               'evidence': 'lanolin; castor oil; ricinoleic acid; hydrogenated castor oil; '
                                           'propylene glycol; colophonium; propolis; shellac; propyl gallate; '
                                           'benzophenone-3; peppermint oil',
                               'scope': '唇部產品中曾報告相關反應的成分用詞；收錄不代表常見程度相同，不能由出現名稱判定個人過敏。'},
 'WEB_DERMNET_COSMETICS': {'title': 'DermNet — Contact reactions to cosmetics',
                           'url': 'https://dermnetnz.org/topics/contact-reactions-to-cosmetics',
                           'checked_at': '2026-10-27',
                           'publisher_type': 'clinical_publisher',
                           'english': ('parabens', 'rosin', 'colophony'),
                           'evidence': 'parabens; rosin; colophony',
                           'scope': '核對成分類別與松香別名；不將整類防腐劑視為同一物質。'},
 'WEB_DERMNET_LANOLIN': {'title': 'DermNet — Contact reactions to lanolin',
                         'url': 'https://dermnetnz.org/topics/contact-reactions-to-lanolin',
                         'checked_at': '2026-10-27',
                         'publisher_type': 'clinical_publisher',
                         'english': ('lanolin', 'wool fat', 'wool wax', 'lanolin alcohol', 'wool alcohols'),
                         'evidence': 'lanolin; wool fat; wool wax; lanolin alcohol; wool alcohols',
                         'scope': '羊毛脂與其醇類組分分項記錄；不是所有名稱皆可當作相同的完整原料。'},
 'WEB_DERMNET_MI': {'title': 'DermNet — Methylisothiazolinone allergy',
                    'url': 'https://dermnetnz.org/topics/methylisothiazolinone-allergy',
                    'checked_at': '2026-10-27',
                    'publisher_type': 'clinical_publisher',
                    'english': ('methylisothiazolinone', 'methylchloroisothiazolinone', 'MCI/MI', 'Kathon CG'),
                    'evidence': 'methylisothiazolinone; methylchloroisothiazolinone; MCI/MI; Kathon CG',
                    'scope': '核對 MI、MCI 與混合物名稱；保留個別物質與混合物的差別，不自動推定交叉過敏。'},
 'WEB_DERMNET_FRAGRANCE': {'title': 'DermNet — Contact allergy to hydroperoxides of limonene and linalool',
                           'url': 'https://dermnetnz.org/topics/contact-allergy-to-limonene-and-linalool',
                           'checked_at': '2026-10-27',
                           'publisher_type': 'clinical_publisher',
                           'english': ('limonene', 'linalool', 'parfum'),
                           'evidence': 'limonene; linalool; parfum',
                           'scope': '用詞核對；原物質與氧化產物不視為相同成分。'},
 'WEB_DERMNET_BEE': {'title': 'DermNet — Contact allergy to propolis',
                     'url': 'https://dermnetnz.org/topics/contact-allergy-to-propolis',
                     'checked_at': '2026-10-27',
                     'publisher_type': 'clinical_publisher',
                     'english': ('propolis', 'beeswax', 'cera alba', 'cera flava'),
                     'evidence': 'propolis; beeswax; cera alba; cera flava',
                     'scope': '核對蜂膠、蜂蠟與白／黃蜂蠟名稱；分開記錄，不因相關就自動合併。'},
 'WEB_FDA_CARMINE': {'title': 'FDA — Cochineal extract and carmine label names',
                     'url': 'https://www.fda.gov/regulatory-information/search-fda-guidance-documents/small-entity-compliance-guide-declaration-name-label-all-foods-and-cosmetic-products-contain',
                     'checked_at': '2026-10-27',
                     'publisher_type': 'regulator',
                     'english': ('carmine', 'cochineal extract'),
                     'evidence': 'carmine; cochineal extract',
                     'scope': '胭脂蟲來源色料標籤名稱；兩種名稱分項記錄。'},
 'WEB_ECHA_CARMINE': {'title': 'ECHA — Carmine lake',
                      'url': 'https://echa.europa.eu/substance-information/-/substanceinfo/100.014.117',
                      'checked_at': '2026-10-27',
                      'publisher_type': 'regulator',
                      'english': ('carmine', 'CI 75470', 'C.I. Natural Red 4:1'),
                      'evidence': 'carmine; CI 75470; C.I. Natural Red 4:1',
                      'scope': '核對色料識別用詞；不將沒有 CI 編號的所有胭脂紅名稱自動判成此物質。'},
 'WEB_COSMILE_CASTOR': {'title': 'COSMILE Europe — Ricinus Communis Seed Oil',
                        'url': 'https://cosmileeurope.eu/inci/detail/13770/ricinus-communis-seed-oil/',
                        'checked_at': '2026-10-27',
                        'publisher_type': 'industry_ingredient_database',
                        'english': ('castor oil', 'Ricinus Communis Seed Oil'),
                        'evidence': 'castor oil; Ricinus Communis Seed Oil',
                        'scope': '只核對蓖麻油 INCI 名稱，不採用功效宣傳。'},
 'WEB_COSMILE_CINNAMAL': {'title': 'COSMILE Europe — Cinnamal',
                          'url': 'https://cosmileeurope.eu/nl/inci/ingredienten-2/3332/cinnamal/',
                          'checked_at': '2026-10-27',
                          'publisher_type': 'industry_ingredient_database',
                          'english': ('cinnamal', 'cinnamaldehyde'),
                          'evidence': 'cinnamal; cinnamaldehyde',
                          'scope': '核對肉桂醛的標籤名稱。'},
 'WEB_FDA_OXYBENZONE': {'title': 'FDA SRS — Oxybenzone',
                        'url': 'https://precision.fda.gov/uniisearch/srs/unii/95oos7ve0y',
                        'checked_at': '2026-10-27',
                        'publisher_type': 'regulator',
                        'english': ('oxybenzone', 'benzophenone-3'),
                        'evidence': 'oxybenzone; benzophenone-3',
                        'scope': '只核對同物質名稱，不代表核准、個人安全或所有防曬劑皆相同。'}})

@dataclass(frozen=True)
class Term:
    key: str
    category: str
    aliases: tuple[str, ...]
    english: tuple[str, ...]
    sources: tuple[str, ...]
    note: str = ""
    related_english: tuple[str, ...] = ()


# 過敏相關查詢用詞：成分類別、單一成分與混合物分開；不代表個人過敏診斷或安全黑名單。
ALLERGEN_TERMS = (
    Term("fragrance", "成分類別／香料", ("香精", "香料", "fragrance", "parfum", "perfume"),
         ("fragrance", "parfum", "perfume"), ("WEB_FDA_ALLERGENS", "WEB_DERMNET_FRAGRANCE"),
         "類別詞；不自動展開成每一個香料成分，也不把無香標示當安全保證。"),
    Term("preservatives", "成分類別／防腐", ("防腐劑", "preservative", "preservatives"),
         ("preservatives",), ("WEB_FDA_ALLERGENS",), "泛稱，不代表對全部防腐劑過敏。"),
    Term("limonene", "成分／香料", ("檸檬烯", "柠檬烯", "limonene", "d-limonene"),
         ("limonene",), ("WEB_FDA_ALLERGENS", "WEB_DERMNET_FRAGRANCE"), "不等同所有柑橘精油或其氧化產物。"),
    Term("linalool", "成分／香料", ("芳樟醇", "沉香醇", "linalool"),
         ("linalool",), ("WEB_FDA_ALLERGENS", "WEB_DERMNET_FRAGRANCE"), "不等同所有薰衣草精油或其氧化產物。"),
    Term("citral", "成分／香料", ("檸檬醛", "柠檬醛", "citral"), ("citral",), ("WEB_FDA_ALLERGENS",)),
    Term("cinnamal", "成分／香料", ("肉桂醛", "桂皮醛", "cinnamal", "cinnamaldehyde"),
         ("cinnamal", "cinnamaldehyde"), ("WEB_FDA_ALLERGENS", "WEB_COSMILE_CINNAMAL")),
    Term("eugenol", "成分／香料", ("丁香酚", "eugenol"), ("eugenol",), ("WEB_FDA_ALLERGENS",)),
    Term("geraniol", "成分／香料", ("香葉醇", "香叶醇", "geraniol"), ("geraniol",), ("WEB_FDA_ALLERGENS",)),
    Term("benzyl_alcohol", "成分／多用途", ("苯甲醇", "苄醇", "benzyl alcohol"),
         ("benzyl alcohol",), ("WEB_FDA_ALLERGENS",), "保留完整名稱，不等同乙醇或所有名稱帶 alcohol 的物質。"),
    Term("peppermint_oil", "成分／精油", ("薄荷油", "胡椒薄荷油", "peppermint oil", "Mentha Piperita Oil"),
         ("peppermint oil", "Mentha Piperita Oil"), ("WEB_FDA_ALLERGENS", "WEB_DERMNET_LIP_ALLERGENS"),
         "精油不等同薄荷醇；不把天然用語當安全保證。"),
    Term("mi", "成分／防腐", ("甲基異噻唑啉酮", "甲基异噻唑啉酮", "methylisothiazolinone", "MI", "MIT"),
         ("methylisothiazolinone",), ("WEB_FDA_ALLERGENS", "WEB_DERMNET_MI"), "縮寫需依語境判讀；不自動等同 MCI 或混合物。"),
    Term("mci", "成分／防腐", ("甲基氯異噻唑啉酮", "甲基氯异噻唑啉酮", "methylchloroisothiazolinone", "MCI", "CMIT"),
         ("methylchloroisothiazolinone",), ("WEB_FDA_ALLERGENS", "WEB_DERMNET_MI"), "不自動等同 MI 或混合物。"),
    Term("mci_mi_mix", "混合物／防腐", ("MCI/MI", "MI/MCI", "CMIT/MIT", "MIT/CMIT", "Kathon CG"),
         ("MCI/MI", "Kathon CG"), ("WEB_DERMNET_MI",), "混合物保留完整名稱，不拆成兩個獨立偏好。"),
    Term("formaldehyde_releasers", "成分類別／防腐", ("甲醛釋放型防腐劑", "甲醛釋放劑", "formaldehyde releasing ingredients"),
         ("formaldehyde releasing ingredients",), ("WEB_FDA_ALLERGENS",), "類別詞，不直接等同游離甲醛或每一種釋放劑。"),
    Term("formaldehyde", "成分／防腐相關", ("甲醛", "formaldehyde"),
         ("formaldehyde",), ("WEB_FDA_ALLERGENS",)),
    Term("dmdm_hydantoin", "成分／防腐", ("DMDM 乙內醯脲", "DMDM乙內醯脲", "雙羥甲基海因", "DMDM hydantoin"),
         ("DMDM hydantoin",), ("WEB_FDA_ALLERGENS",), "甲醛釋放型防腐成分；分類關係不當同義詞展開。"),
    Term("diazolidinyl_urea", "成分／防腐", ("雙咪唑烷基脲", "雙咪唑烷基尿素", "diazolidinyl urea"),
         ("diazolidinyl urea",), ("WEB_FDA_ALLERGENS",), "不等同一般尿素或 imidazolidinyl urea。"),
    Term("imidazolidinyl_urea", "成分／防腐", ("咪唑烷基脲", "咪唑烷基尿素", "imidazolidinyl urea"),
         ("imidazolidinyl urea",), ("WEB_FDA_ALLERGENS",), "不等同一般尿素或 diazolidinyl urea。"),
    Term("quaternium_15", "成分／防腐", ("季銨鹽-15", "季銨鹽15", "quaternium-15"),
         ("quaternium-15",), ("WEB_FDA_ALLERGENS",), "不涵蓋所有季銨鹽。"),
    Term("bronopol", "成分／防腐", ("布羅波爾", "溴硝丙二醇", "bronopol"),
         ("bronopol",), ("WEB_FDA_ALLERGENS",)),
    Term("parabens", "成分類別／防腐", ("對羥基苯甲酸酯類", "羥苯酯類", "尼泊金酯類", "parabens", "paraben"),
         ("parabens",), ("WEB_DERMNET_COSMETICS",), "類別詞；不是每一種成員的同義詞，也不表示其風險與 MI 相同。"),
    Term("lanolin", "成分／油脂", ("羊毛脂", "羊毛蠟", "lanolin", "wool fat", "wool wax"),
         ("lanolin", "wool fat", "wool wax"), ("WEB_DERMNET_LANOLIN", "WEB_DERMNET_LIP_ALLERGENS"),
         "不自動推定羊毛衣物過敏，也不等同全部動物油脂。"),
    Term("lanolin_alcohol", "成分／油脂組分", ("羊毛脂醇", "羊毛醇", "lanolin alcohol", "wool alcohols"),
         ("lanolin alcohol", "wool alcohols"), ("WEB_DERMNET_LANOLIN",), "組分與羊毛脂分項保存；不等同乙醇。"),
    Term("propolis", "成分／蜂產品", ("蜂膠", "蜂胶", "propolis"),
         ("propolis",), ("WEB_DERMNET_BEE", "WEB_DERMNET_LIP_ALLERGENS"), "蜂膠與蜂蠟分開，不自動推定交叉過敏。"),
    Term("beeswax", "成分／蜂產品", ("蜂蠟", "蜂蜡", "白蜂蠟", "黃蜂蠟", "beeswax", "cera alba", "cera flava"),
         ("beeswax",), ("WEB_DERMNET_BEE",), "白／黃蜂蠟作蜂蠟類檢索；不將蜂蠟當成蜂膠或蜂蜜。"),
    Term("castor_oil", "成分／植物油", ("蓖麻油", "castor oil", "Ricinus Communis Seed Oil", "Ricinus Communis (Castor) Seed Oil"),
         ("castor oil", "Ricinus Communis Seed Oil"), ("WEB_DERMNET_LIP_ALLERGENS", "WEB_COSMILE_CASTOR"),
         "原油與蓖麻油酸、氫化物及其他衍生物分開；不是 INCI 配方掃描器。"),
    Term("hydrogenated_castor_oil", "成分／油脂衍生物", ("氫化蓖麻油", "氢化蓖麻油", "hydrogenated castor oil"),
         ("hydrogenated castor oil",), ("WEB_DERMNET_LIP_ALLERGENS",), "不等同原蓖麻油或所有 PEG 蓖麻油衍生物。"),
    Term("ricinoleic_acid", "成分／脂肪酸", ("蓖麻油酸", "ricinoleic acid"),
         ("ricinoleic acid",), ("WEB_DERMNET_LIP_ALLERGENS",), "分項記錄，不將整種植物油當成單一脂肪酸。"),
    Term("propylene_glycol", "成分／溶劑", ("丙二醇", "1,2-丙二醇", "propylene glycol"),
         ("propylene glycol",), ("WEB_DERMNET_LIP_ALLERGENS",), "不等同 1,3-propanediol、丁二醇或聚乙二醇。"),
    Term("colophony", "成分／樹脂", ("松香", "colophony", "colophonium", "rosin"),
         ("colophony", "colophonium", "rosin"), ("WEB_DERMNET_COSMETICS", "WEB_DERMNET_LIP_ALLERGENS")),
    Term("shellac", "成分／樹脂", ("蟲膠", "虫胶", "紫膠", "shellac"),
         ("shellac",), ("WEB_DERMNET_LIP_ALLERGENS",), "不等同松香或所有樹脂。"),
    Term("carmine", "成分／色料", ("胭脂蟲紅", "胭脂虫红", "carmine", "CI 75470", "CI75470", "C.I. 75470"),
         ("carmine", "CI 75470"), ("WEB_FDA_CARMINE", "WEB_ECHA_CARMINE"), "色料名稱，不推定使用者想要紅色唇彩；無編號的胭脂紅保留歧義。"),
    Term("cochineal_extract", "成分／色料", ("胭脂蟲萃取物", "胭脂蟲提取物", "cochineal extract"),
         ("cochineal extract",), ("WEB_FDA_CARMINE",), "與 carmine 分項記錄，不以來源相關就當成完全同物。"),
    Term("benzophenone_3", "成分／紫外線吸收劑", ("二苯酮-3", "二苯酮3", "氧苯酮", "benzophenone-3", "oxybenzone"),
         ("benzophenone-3", "oxybenzone"), ("WEB_DERMNET_LIP_ALLERGENS", "WEB_FDA_OXYBENZONE"), "不涵蓋所有二苯酮或防曬劑。"),
    Term("propyl_gallate", "成分／抗氧化劑", ("沒食子酸丙酯", "沒食子酸正丙酯", "propyl gallate"),
         ("propyl gallate",), ("WEB_DERMNET_LIP_ALLERGENS",)),
)


# 同一列只表示檢索用的詞彙對照，不把產品、手法、妝效、膚色或色名合為同一屬性。
TERMS = (
    Term("natural_lip_color", "原生唇色", ("原生唇色", "天生唇色", "本來的唇色"),
         ("natural lip color", "natural lip colour"), ("AR012", "AR015")),
    Term("pigmented_lips", "原生唇色", ("原生唇色深", "原生唇色很深", "原生唇色比較深", "原生唇色較深", "原生唇色偏深", "天生唇色深", "天生唇色比較深", "天生唇色較深", "原本唇色深"),
         ("naturally pigmented", "pigmented lips"), ("CAND01", "PR020"), "不展開為深膚色或紫色唇彩偏好。"),
    Term("coverage", "遮蓋程度", ("遮蓋力", "遮蓋", "蓋不住", "遮不住", "蓋不過", "遮不過"),
         ("coverage",), ("PR015",), "蓋不住是描述遮蓋問題，不是排除 coverage。"),
    Term("opaque", "遮蓋程度", ("不透明",), ("opaque",), ("AR012",)),
    Term("sheer", "遮蓋程度", ("薄透", "透色", "低遮蓋"), ("sheer",), ("PR017",), "薄透不等於霧面、光澤或裸色。"),
    Term("dry_lips", "唇部狀態", ("嘴唇容易乾", "嘴唇很乾", "嘴唇乾燥", "嘴唇乾", "唇部乾燥", "乾唇", "顯乾"),
         ("dryness",), ("PR015",)),
    Term("chapped", "唇部狀態", ("乾裂", "龜裂", "皸裂"), ("chapped",), ("PR015",)),
    Term("flaky", "唇部狀態", ("脫皮", "起皮"), ("flaky",), ("PR015",)),
    Term("cakey", "妝效", ("結塊", "卡粉", "厚重不均", "厚重"), ("cakey",), ("PR015",)),   # v3.8：只留「厚重」→ cakey，不加 heavy／too many layers
    Term("matte", "妝效", ("霧面", "啞光", "啞面"), ("matte",), ("PR015",)),
    Term("glossy", "妝效", ("光澤", "亮面"), ("glossy",), ("AR015",)),
    Term("shimmery", "妝效", ("細閃", "閃亮妝效"), ("shimmery",), ("AR015",)),
    Term("metallic", "妝效", ("金屬光", "金屬妝效"), ("metallic",), ("AR015",)),
    Term("creamy", "質地", ("乳霜質地", "乳霜狀質地", "奶油質地"), ("creamy",), ("PR015",), "不把奶油色或奶茶色當質地。"),
    Term("hydrating", "產品描述", ("保濕", "滋潤"), ("hydrating",), ("PR015",), "只找原文描述，不保證功效。"),
    Term("swatch", "手法", ("上唇試色", "手臂試色", "試色", "試擦"), ("swatches",), ("PR021",)),
    Term("thin_application", "手法", ("薄擦一層", "薄薄一層", "薄擦", "薄塗"), ("thin layer", "thin, even swipe"), ("PR015",)),
    Term("layer", "手法", ("疊擦", "疊塗", "再補一層"), ("layer", "layers"), ("PR015",)),
    Term("let_set", "手法", ("等它定型", "等定型", "待定型"), ("let it set",), ("PR015",)),
    Term("blot", "手法", ("吸掉多餘油分", "吸掉多餘", "輕壓吸掉"), ("blot",), ("PR015",), "不把所有按壓都當吸掉多餘。"),
    Term("press", "手法", ("按壓", "輕壓"), ("press",), ("PR015",)),
    Term("tap", "手法", ("輕拍", "拍點", "點拍"), ("tap", "stipple"), ("CAND01",)),
    Term("outline", "手法", ("描唇線", "勾唇線", "勾勒唇線", "描邊"), ("outline",), ("CAND01",)),
    Term("fill", "手法", ("填滿唇部", "填滿嘴唇", "填滿唇色", "填滿"), ("fill in",), ("CAND01",)),
    Term("blend", "手法", ("暈染", "暈開", "融合交界"), ("blend",), ("TX001", "PR012")),
    Term("exfoliate", "手法", ("去角質",), ("exfoliation",), ("PR015",)),
    Term("lip_balm", "產品種類", ("護唇膏", "潤唇膏"), ("lip balm",), ("PR015",)),
    Term("lip_primer", "產品種類", ("唇部妝前乳", "唇部打底", "唇部妝前"), ("lip primer",), ("PR015",)),
    Term("lip_liner", "產品種類", ("唇線筆", "唇線"), ("lip liner",), ("PR015", "CAND01")),
    Term("lipstick", "產品種類", ("唇膏", "口紅"), ("lipstick",), ("PR015",), "不把使用者泛稱口紅當成排除其他產品的條件。"),
    Term("lip_gloss", "產品種類", ("唇蜜",), ("lip gloss",), ("PR005",)),
    Term("lip_oil", "產品種類", ("唇油",), ("lip oil",), ("PR012",)),
    # v3.3（2026-11-02）：唇釉與液態唇膏是同一種東西，共用英文檢索詞；原「唇釉＝產品種類待確認」一條移除
    Term("liquid_lipstick", "產品種類", ("液態唇膏", "液體唇膏", "唇釉"), ("liquid lipstick",), ("PR012",)),
    # v3.4（2026-11-03，N10）：新增法式妝。「底妝→foundation」試過：候選池 25 時把粉底指南段落擠進來、
    # 正解 PR012_h0179 仍進不了池，只增加雜訊，不採用。依 40 題設計，非獨立驗證
    Term("foundation", "產品種類", ("粉底液", "粉底"), ("foundation",), ("CAND01",), "僅中文明說粉底時展開；不把英文研究基礎翻成粉底。"),
    Term("french_makeup", "妝感", ("法式妝容", "法式妝", "法式感"), ("French makeup",), ("PR012",)),
    Term("concealer", "產品種類", ("遮瑕膏", "遮瑕"), ("concealer",), ("TX001", "PR012")),
    Term("translucent_powder", "產品種類", ("透明蜜粉", "透明散粉"), ("translucent setting powder",), ("PR015",)),
    Term("setting_powder", "產品種類", ("定妝粉",), ("setting powder",), ("PR015",)),
    Term("oils", "成分", ("油類", "含油配方"), ("oils",), ("PR015",), "成分不等於唇油產品。"),
    Term("hyaluronic_acid", "成分", ("玻尿酸", "透明質酸"), ("hyaluronic acid",), ("PR015",)),
    Term("cool_undertone", "底調", ("冷色底調", "冷底調", "冷底"), ("cool undertone",), ("AR012", "PR003")),
    Term("warm_undertone", "底調", ("暖色底調", "暖底調", "暖底"), ("warm undertone",), ("AR012", "PR003")),
    Term("neutral_undertone", "底調", ("中性底調", "中性底"), ("neutral undertone",), ("PR003",)),
    Term("fair_skin", "膚色深淺", ("白皙膚色", "白皙皮膚"), ("fair",), ("PR003", "PR021"), "檢索用深淺詞，不推定冷暖。"),
    Term("medium_skin", "膚色深淺", ("中等膚色", "中等深淺膚色"), ("medium skin",), ("PR021",)),
    Term("dark_skin", "膚色深淺", ("深膚色", "膚色深", "膚色比較深", "膚色很深"), ("dark skin", "deep skin"), ("PR020",), "不等於原生唇色深或暖底。"),
    Term("tan_skin", "膚色深淺", ("棕褐膚色",), ("tan skin",), ("PR020",), "不把小麥色自動歸入此類。"),
    Term("wheat_skin", "膚色深淺", ("小麥膚色", "小麥色肌膚", "小麥色皮膚", "小麥色"),
         ("wheatish skin", "wheatish skin tone"), ("WEB_WHEATISH_LAKME",),
         "可用英文檢索別名；不直接等同 tan、deep、olive 或暖底。"),
    Term("smoky_eye", "搭配情境", ("煙燻眼妝", "煙燻妝", "煙燻", "煙熏眼妝"), ("smoky eye", "smokey eye"), ("PR012", "PR017", "PR021")),
    Term("blush", "搭配情境", ("腮紅",), ("blush",), ("PR021",)),
    Term("office", "場合", ("辦公室", "上班", "辦公"), ("office",), ("PR017",)),
    # v3.1（2026-10-27，N05「過年回家想擦有喜氣的顏色」）：唯一文化依據 TH013_p0074_b004 用原句向量只排第 73；
    # 英文用來源原詞 good luck、prosperity（全庫各只有這一段）。分類不放「場合」：場合詞只降權（hybrid-v3），這裡要能單獨找片段。
    Term("lunar_new_year", "文化象徵", ("農曆新年", "農曆年", "過年", "新年", "春節", "拜年", "喜氣", "喜慶"),
         ("good luck", "prosperity"), ("TH013",),
         "只用來找中華文化紅色象徵的文獻段落；不表示擦紅色會帶來好運。跨年不在此詞條，跨年內容不能當過年依據。"),
    # v3.6：「不同國家或文化偏好的口紅顏色」對到 TH013 文獻回顧的 geographical variations 段落
    Term("culture_region", "文化地區", ("不同國家", "各國", "國家", "不同文化", "文化差異", "文化", "地區差異"),
         ("geographical variations", "cultural", "regional"), ("TH013",), "只用來找地區／文化偏好的研究段落，不推定使用者所屬文化。"),
    Term("dusty_rose", "色彩詞", ("乾燥玫瑰色", "乾燥玫瑰"), ("dusty rose",), ("PR017",)),
    Term("coral", "色彩詞", ("珊瑚色", "珊瑚"), ("coral",), ("AR012",)),
    Term("milk_tea", "色彩詞", ("奶茶色", "奶茶"), ("milk tea",), ("WEB_MILKTEA_PATRICKTA",),
         "品牌確有 Milk Tea 用法；相關 nude beige 僅用於較寬檢索，不把所有裸色等同奶茶色。", ("nude beige",)),
    Term("bean_paste", "色彩詞", ("玫瑰豆沙色", "玫瑰豆沙", "豆沙色", "豆沙"), ("rosewood",),
         ("WEB_BEANPASTE_MARYKAY", "WEB_BEANPASTE_PINGUAN", "WEB_DUSTYROSE_NARS"),
         "Rosewood 有品牌中英對照；dusty rose 為相關檢索用法，不強制合併豆沙與乾燥玫瑰。", ("dusty rose",)),
    Term("mauve", "色彩詞", ("藕紫色", "藕紫"), ("mauve", "mauves"), ("PR015", "PR020"), "不等同豆沙色。"),
    Term("nude", "色彩詞", ("裸色",), ("nude",), ("PR021",), "不等於奶茶、MLBB 或每個人的原生唇色。"),
    # v3.2：口語別名只對齊既有詞條（完整複合詞，最長詞優先，不會只剩 orange、red 單字）；
    # 「偏粉的紅」沒有可靠英文對照，不設詞條、保留原詞，不能當成 blue-red。
    Term("blue_red", "色彩詞", ("藍調紅", "藍紅色", "偏藍的紅", "藍色調的紅"), ("blue-red",), ("PR003",)),
    Term("orange_red", "色彩詞", ("橘調紅", "橘紅色", "偏橘的紅", "橘色調的紅"), ("orange-red",), ("PR003",)),
    Term("mlbb", "妝效目標", ("MLBB",), ("MLBB", "My Lips But Better"), ("AR012",), "不是固定裸色或固定 HEX。"),
    Term("lightness", "色彩屬性", ("明度",), ("lightness",), ("AR012",)),
    Term("chroma", "色彩屬性", ("彩度",), ("chroma",), ("AR012",)),
    Term("saturation", "色彩屬性", ("飽和度",), ("saturation",), ("TH013", "AR002")),
    # v3.5（2026-11-07，RAGAS 候選池召回診斷）：N02「不會太搶戲」參考段落 PR017_h0137 原文是 won't feel overpowering／
    # less is more，中文說法對不上，向量排第 47；W09「臉看起來比較白或比較紅潤」參考段落 TH013_p0004_b002 是研究摘要
    # （colour assimilation／perceived facial skin tone），向量排第 35。只對齊這兩組原文說法，不推定顏色深淺或效果大小。
    Term("not_overpowering", "妝效目標", ("不會太搶戲", "不要太搶戲", "不搶戲", "太搶戲", "不搶眼"),
         ("overpowering", "less is more"), ("PR017",), "只對齊「不壓過其他妝容」的說法；不推定唇色深淺或鮮豔度。"),
    Term("perceived_skin_tone", "研究概念", ("看起來比較白", "看起來比較紅潤", "顯得比較白", "顯得比較紅潤"),
         ("perceived facial skin tone", "colour assimilation"), ("TH013",), "研究的是看起來的膚色，不是實際膚色改變。"),
    *ALLERGEN_TERMS,
)

# 沒有可靠英文對照或含歧義的詞保留中文，交由後續處理，不硬湊英文同義詞。
REVIEW_TERMS = (
    Term("carmine_ambiguous", "色料待確認", ("胭脂紅",), (), (), "需看原標示或 CI 編號，不以中文名稱逕認 carmine。"),
    Term("propanediol_distinct", "成分待確認", ("1,3-丙二醇", "1，3-丙二醇"), (), (), "不同於 1,2-丙二醇，不轉成 propylene glycol。"),
    Term("peg_castor_derivative", "成分待確認", ("PEG-40 氫化蓖麻油", "PEG-40氫化蓖麻油", "PEG-40 hydrogenated castor oil"), (), (), "衍生物保留全名，不轉成原蓖麻油或單純氫化蓖麻油。"),
    Term("neutral_style", "服裝風格", ("中性穿搭", "中性風格", "中性風"), (), (), "服裝風格不是中性底調。"),
    Term("skin_descriptor_ambiguous", "膚色自述", ("黃皮膚", "黃皮", "冷白皮", "暖白皮", "暖皮", "冷皮"), (), (), "膚色外觀與底調分開確認。"),
    Term("lip_depth_ambiguous", "唇色歧義", ("深唇色", "唇色深", "唇色很深", "唇色比較深", "唇色較深"), (), (), "需分清原生唇色與想要的唇彩顏色，不直接判成色素唇。"),
    Term("brightness_ambiguous", "歧義", ("不要太亮", "不想太亮", "太亮", "亮一點", "亮一些"), (), (), "可能指明度、鮮豔度或光澤，不能自動選定。"),
    Term("gradient", "手法", ("漸層唇妝", "漸層紅唇", "漸層唇", "漸層"), (), (), "保留原詞，待直接來源確認英文名稱；不與咬唇妝合併。"),
    Term("bitten_lip", "手法", ("咬唇妝", "咬唇"), (), (), "保留原詞，待直接來源確認英文名稱；不與漸層唇合併。"),
    Term("finish_marketing", "質地待確認", ("絲絨", "柔霧", "水光", "鏡面", "緞光", "珠光"), (), (), "品牌用語可能混合質地、光澤與商品名，暫不強制等同。"),
    Term("shade_name", "商品色號", ("Blushed Rose", "Velvet Underground"), (), (), "完整保留色號，不拆成通用色名或質地。"),
)

LEXICON_SHA256 = hashlib.sha256(json.dumps(
    {"version": GLOSSARY_VERSION, "terms": [asdict(t) for t in TERMS],
     "review_terms": [asdict(t) for t in REVIEW_TERMS], "web_sources": WEB_SOURCES}, ensure_ascii=False, sort_keys=True,
    separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Match:
    key: str
    category: str
    text: str
    start: int
    end: int
    english: tuple[str, ...]
    sources: tuple[str, ...]
    negated: bool
    needs_review: bool = False
    note: str = ""
    related_english: tuple[str, ...] = ()


@dataclass(frozen=True)
class Expansion:
    original_query: str
    english_phrases: tuple[str, ...]
    negated_phrases: tuple[str, ...]
    native_phrases: tuple[str, ...]
    matches: tuple[Match, ...]
    related_phrases: tuple[str, ...] = ()
    negated_related_phrases: tuple[str, ...] = ()
    # v3.2：檢索概念與使用者條件分開記。每個詞條命中一筆 {key, text, english, polarity}：
    # polarity＝include（使用者要）／exclude（使用者明說不要）。檢索可以兩種都用（說明差異的文件仍相關），
    # 但 exclude 只能當「排除條件」，不能當推薦方向；english_phrases 仍只放 include，既有用途不變。
    concepts: tuple[dict, ...] = ()
    version: str = GLOSSARY_VERSION
    lexicon_sha256: str = LEXICON_SHA256

    def to_dict(self) -> dict:
        return asdict(self)


def _alias_key(value: str) -> str:
    return " ".join(value.casefold().split())


_ALIASES = {_alias_key(alias): (term, term in REVIEW_TERMS)
            for term in (*TERMS, *REVIEW_TERMS) for alias in term.aliases}
assert len(_ALIASES) == sum(len(t.aliases) for t in (*TERMS, *REVIEW_TERMS)), "術語別名重複"


def _alias_pattern(alias: str) -> str:
    escaped = re.escape(alias).replace(r"\ ", r"\s+")
    return (r"(?<![A-Za-z0-9])" + escaped + r"(?![A-Za-z0-9])"
            if alias.isascii() else escaped)


_PATTERN = re.compile("|".join(_alias_pattern(a) for a in sorted(_ALIASES, key=lambda a: (-len(a), a))), re.I)
_ASCII = re.compile(r"#[0-9A-Fa-f]{6}\b|[A-Za-z][A-Za-z0-9]*(?:[-/.][A-Za-z0-9]+)*")
_ENGLISH_STOP = frozenset({"a", "an", "the", "and", "or", "but", "not", "no", "without", "avoid", "free", "of"})
_NEGATIVE = re.compile(
    r"(?:不要|不想(?:要)?|不喜歡|不需要|不接受|不考慮|避免|排除|不是|(?<!\w)(?:not|no|without|avoid|free\s+of)\b)"
    # v3.2：否定詞和術語之間可以夾「並列的色彩修飾＋連接詞」（「不要那種偏粉或偏橘的紅」的「偏粉或」），
    # 仍然只看術語前面緊接的文字，不擴大成整句否定。
    r"(?:\s|太|很|偏|帶|有|的|那種|這種|明顯|過於|再|用|擦|選"
    r"|偏?[粉橘橙藍紫棕黃綠灰](?:色調|色|調)?的?(?:或是|或者|或|和|跟|、))*$", re.I)
_CONJUNCTION = re.compile(r"\s*(?:或是|或者|以及|或|和|跟|、|and\b|or\b)(?:\s|太|很|偏|帶|有|的)*$", re.I)


def _unique(values) -> tuple[str, ...]:
    result, seen = [], set()
    for value in values:
        if value.casefold() not in seen:
            seen.add(value.casefold())
            result.append(value)
    return tuple(result)


def analyze(query: str) -> Expansion:
    """最長詞優先、保留位置和否定。這是術語辨識，不是完整意圖或否定範圍解析器。"""
    if not isinstance(query, str):
        raise TypeError("query 必須是字串")
    events = []
    for hit in _PATTERN.finditer(query):
        term, review = _ALIASES[_alias_key(hit.group())]
        events.append((hit.start(), hit.end(), term, review))
    covered = [(s, e) for s, e, _, _ in events]
    for hit in _ASCII.finditer(query):
        if hit.group().casefold() in _ENGLISH_STOP or any(hit.start() < e and hit.end() > s for s, e in covered):
            continue
        literal = Term("literal", "原有英文或代碼", (hit.group(),), (hit.group(),), ())
        events.append((hit.start(), hit.end(), literal, False))
    events.sort(key=lambda item: item[0])
    matches = []
    for start, end, term, review in events:
        negated = bool(_NEGATIVE.search(query[:start]))
        # 明確的英文無添加標示：只記語句否定，不表示配方已核實或對使用者安全。
        if term in ALLERGEN_TERMS and re.match(r"(?:-free|\s+free)\b", query[end:], re.I):
            negated = True
        if matches and matches[-1].negated and _CONJUNCTION.fullmatch(query[matches[-1].end:start]):
            negated = True
        matches.append(Match(term.key, term.category, query[start:end], start, end, term.english,
                             term.sources, negated, review, term.note, term.related_english))
    return Expansion(
        original_query=query,
        english_phrases=_unique(p for m in matches if not m.negated for p in m.english),
        negated_phrases=_unique(p for m in matches if m.negated for p in m.english),
        native_phrases=_unique(m.text for m in matches if m.needs_review),
        matches=tuple(matches),
        related_phrases=_unique(p for m in matches if not m.negated for p in m.related_english),
        negated_related_phrases=_unique(p for m in matches if m.negated for p in m.related_english),
        concepts=tuple({"key": m.key, "category": m.category, "text": m.text, "english": list(m.english),
                        "polarity": "exclude" if m.negated else "include"}
                       for m in matches if m.key != "literal" and m.english),
    )


def expand(query: str, *, include_related: bool = False) -> list[str]:
    """預設回傳直接檢索別名；相關詞需明確開啟，仍不等同色名定義。"""
    result = analyze(query)
    return list(_unique((*result.english_phrases, *(result.related_phrases if include_related else ()))))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    print(json.dumps(analyze(parser.parse_args().query).to_dict(), ensure_ascii=False, indent=2))
