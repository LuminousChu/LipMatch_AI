"""pytest 共用設定：載入檔名以數字開頭的 scripts/01_extract.py、scripts/02_clean.py。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Leeds 論文可以放在 data/raw/ 或其子資料夾（例如 data/raw/academic_papers/）
LEEDS_PDF = next(
    iter(sorted((ROOT / "data" / "raw").rglob("TH013_*.pdf"))),
    ROOT / "data" / "raw" / "academic_papers" / "TH013_唇彩影響臉部色彩感知_英文文獻_2024.pdf",
)
HTML_FIXTURE = ROOT / "tests" / "fixtures" / "sample_makeup_article.html"


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # dataclass 需要模組先註冊
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def extract_mod():
    return _load("lipmatch_extract", "01_extract.py")


@pytest.fixture(scope="session")
def clean_mod():
    return _load("lipmatch_clean", "02_clean.py")


@pytest.fixture(scope="session")
def leeds_extracted(extract_mod):
    if not LEEDS_PDF.exists():
        pytest.skip(f"找不到測試 PDF：{LEEDS_PDF}")
    return extract_mod.extract_pdf(LEEDS_PDF, "TH013", "1-12")


@pytest.fixture(scope="session")
def leeds_cleaned(clean_mod, leeds_extracted):
    return clean_mod.clean_document(leeds_extracted)


@pytest.fixture(scope="session")
def report_mod():
    return _load("lipmatch_report", "03_integrity_report.py")


@pytest.fixture(scope="session")
def leeds_full(extract_mod):
    """整份 Leeds 論文（94 頁）擷取結果。"""
    if not LEEDS_PDF.exists():
        pytest.skip(f"找不到測試 PDF：{LEEDS_PDF}")
    return extract_mod.extract_pdf(LEEDS_PDF, "TH013")


@pytest.fixture(scope="session")
def leeds_full_cleaned(clean_mod, leeds_full):
    return clean_mod.clean_document(leeds_full)


@pytest.fixture(scope="session")
def synthetic_pdf(tmp_path_factory):
    from tests.fixtures.synthetic_pdf import build_synthetic_pdf
    return build_synthetic_pdf(tmp_path_factory.mktemp("synthetic") / "synthetic_tables.pdf")


@pytest.fixture(scope="session")
def synthetic_extracted(extract_mod, synthetic_pdf):
    return extract_mod.extract_pdf(synthetic_pdf, "synthetic_tables")


@pytest.fixture(scope="session")
def two_column_pdf(tmp_path_factory):
    from tests.fixtures.synthetic_pdf import build_two_column_pdf
    return build_two_column_pdf(tmp_path_factory.mktemp("two_column") / "two_column.pdf")


@pytest.fixture(scope="session")
def two_column_extracted(extract_mod, two_column_pdf):
    return extract_mod.extract_pdf(two_column_pdf, "two_column")


@pytest.fixture(scope="session")
def synthetic_cleaned(clean_mod, synthetic_extracted):
    return clean_mod.clean_document(synthetic_extracted)


@pytest.fixture(scope="session")
def html_extracted(extract_mod):
    return extract_mod.extract_html(HTML_FIXTURE, "sample_makeup_article")


@pytest.fixture(scope="session")
def html_cleaned(clean_mod, html_extracted):
    return clean_mod.clean_document(html_extracted)
