"""Workforce from the official annual-account copy (Regnskapsregisteret PDF), read with OCR.

Norwegian annual accounts must disclose the number of full-time equivalents (årsverk). Brønnøysund serves each
filing as a scanned PDF at an organisation-number keyed URL, so identity is exact by construction. Pages are rendered
with pdfium and read with Tesseract (if installed) or RapidOCR (pip). Pages are tried in the order where the payroll
note usually sits, and work stops at the first explicit, unambiguous company-level statement.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata

ACCOUNT_COPY = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/{year}"
PAGE_ORDER = (4, 3, 5, 2, 6, 7, 8, 9, 10)
NUM = r"([0-9oO][0-9oO .,]{0,9})"

# (priority, measure, pattern) on folded OCR text (æøå -> ae/o/a, lower-case, collapsed whitespace)
PATTERNS = [
    (0, "full_time_equivalents", re.compile(r"(?:antall|tal\s*pa|tall\s*pa)\s*a{1,2}rs\s?verk\s*(?:sysselsatt\s*)?i\s*(?:regnskapsaret|rekneskapsaret|aret)(?:\s*20\d\d)?\s*(?:er|:|=)?\s*" + NUM)),
    (0, "full_time_equivalents", re.compile(r"gjennomsnittlig\s*antall\s*a{1,2}rs\s?verk(?:\s*sysselsatt)?(?:\s*i\s*(?:regnskapsaret|rekneskapsaret|aret))?\s*(?:er|:|=)?\s*" + NUM)),
    (0, "full_time_equivalents", re.compile(r"(?:selskapet|foretaket|stiftelsen|virksomheten)\s*har(?:\s*i\s*20\d\d)?(?:\s*hatt)?(?:\s*sysselsatt)?\s*" + NUM + r"\s*a{1,2}rs\s?verk")),
    (0, "full_time_equivalents", re.compile(r"antall\s*a{1,2}rs\s?verk\s*(?:er|:|=)\s*" + NUM)),
    (1, "employees", re.compile(r"gjennomsnittlig(?:e)?\s*antall\s*ansatte(?:\s*i\s*(?:regnskapsaret|rekneskapsaret|aret))?\s*(?:er|:|=)?\s*" + NUM)),
    (1, "employees", re.compile(r"antall\s*ansatte(?:\s*pr\.?\s*31\.?12\.?)?\s*(?:er|:|=)\s*" + NUM)),
]
ZERO = re.compile(r"(?:selskapet|stiftelsen|foretaket|sameiet|virksomheten|det)\s*(?:har\s*ingen\s*(ansatte|a{1,2}rsverk)|har\s*ikke\s*hatt\s*(?:noen\s*)?ansatte|hadde\s*ingen\s*ansatte|har\s*ikke\s*ansatte)")
GROUP = re.compile(r"konsern|group|datterselskap")


def pdf_fingerprint(data: bytes) -> str:
    """Stable content hash: Brreg regenerates the trailer /ID on every download, the document itself is unchanged."""
    return hashlib.sha256(re.sub(rb"/ID\s*\[\s*<[0-9A-Fa-f]*>\s*<[0-9A-Fa-f]*>\s*\]", b"", data)).hexdigest()


def fold(text: str) -> str:
    text = (text or "").lower().replace("ø", "o").replace("æ", "ae").replace("å", "a")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[ \t]+", " ", text)


def _number(raw: str):
    raw = raw.strip()
    # "1 00" is an OCR-split decimal ("1.00"), not one hundred.
    split_decimal = re.fullmatch(r"([0-9oO]{1,4}) ([0-9oO]{2})", raw)
    if split_decimal:
        raw = f"{split_decimal.group(1)}.{split_decimal.group(2)}"
    cleaned = raw.replace("o", "0").replace("O", "0").replace(" ", "").strip(".,")
    if not cleaned:
        return None
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    elif "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if not 0 <= value <= 100000:
        return None
    return int(value) if value.is_integer() else round(value, 2)


def extract_workforce(text: str):
    """Return (value, measure, span) for one explicit company-level statement, or None when absent/conflicting."""
    flat = re.sub(r"\s+", " ", fold(text))
    found = []
    for priority, measure, pattern in PATTERNS:
        for match in pattern.finditer(flat):
            span = match.group(0).strip()
            if GROUP.search(flat[max(0, match.start() - 60):match.end()]):
                continue
            raw = match.group(1).strip()
            if priority == 0 and pattern is PATTERNS[0][2] and re.fullmatch(r"\d{3,}", raw) and raw.endswith("00"):
                continue  # Brreg's template prints two decimals; "100" here is most likely "1.00" with a lost dot: abstain
            value = _number(raw)
            if value is not None:
                found.append((priority, value, measure, span[:220]))
    for match in ZERO.finditer(flat):
        span = match.group(0).strip()
        if not GROUP.search(span):
            measure = "full_time_equivalents" if match.group(1) and "verk" in match.group(1) else "employees"
            found.append((0, 0, measure, span[:220]))
    if not found:
        return None
    best = min(item[0] for item in found)
    top = [item for item in found if item[0] == best]
    if len({item[1] for item in top}) != 1:
        return None  # conflicting statements: abstain
    chosen = sorted(top, key=lambda item: item[2] != "full_time_equivalents")[0]
    return chosen[1], chosen[2], chosen[3]


# --------------------------------------------------------------------------- OCR backends (one per worker process)
_ENGINE = None
_BACKEND = None


def backend_name() -> str | None:
    """Detect an OCR backend without loading models in the parent process (override: SIGNALPOST_OCR_BACKEND)."""
    forced = os.environ.get("SIGNALPOST_OCR_BACKEND", "").strip().lower()
    if forced in ("none", "off"):
        return None
    if forced == "rapidocr":
        return "rapidocr"
    if shutil.which("tesseract"):
        return "tesseract"
    try:
        import importlib.util

        if importlib.util.find_spec("rapidocr_onnxruntime") and importlib.util.find_spec("pypdfium2"):
            return "rapidocr"
    except Exception:
        return None
    return None


def _init_worker(backend: str) -> None:
    global _ENGINE, _BACKEND
    _BACKEND = backend
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    if backend == "rapidocr":
        from rapidocr_onnxruntime import RapidOCR

        _ENGINE = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)


def _ocr_image(image) -> str:
    if _BACKEND == "tesseract":
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "page.png")
            image.save(path)
            out = subprocess.run(["tesseract", path, "stdout", "-l", "nor+eng" if _has_nor() else "eng", "--psm", "6"],
                                 capture_output=True, text=True, timeout=90)
            return out.stdout
    import numpy as np

    result, _ = _ENGINE(np.array(image), use_cls=False)
    return "\n".join(item[1] for item in result or [])


_NOR = None


def _has_nor() -> bool:
    global _NOR
    if _NOR is None:
        try:
            out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=20)
            _NOR = "nor" in out.stdout.split()
        except Exception:
            _NOR = False
    return _NOR


def ocr_pdf_for_workforce(org: str, year: str, pdf_bytes: bytes, max_pages: int = 7) -> dict:
    """Runs inside a worker process."""
    import pypdfium2 as pdfium

    digest = hashlib.sha256(pdf_bytes).hexdigest()
    result = {"organisation_number": org, "year": year, "sha256": digest, "fingerprint": pdf_fingerprint(pdf_bytes),
              "pages_read": [], "status": "no_statement"}
    try:
        pdf = pdfium.PdfDocument(pdf_bytes)
    except Exception as exc:
        result.update(status="unreadable_pdf", error=type(exc).__name__)
        return result
    count = len(pdf)
    result["page_count"] = count
    order = [i for i in PAGE_ORDER if i < count] + [i for i in range(1, count) if i not in PAGE_ORDER]
    for index in order[:max_pages]:
        try:
            image = pdf[index].render(scale=0.7, grayscale=True).to_pil()
            text = _ocr_image(image)
        except Exception as exc:
            result.update(status="ocr_error", error=type(exc).__name__)
            return result
        result["pages_read"].append(index + 1)
        hit = extract_workforce(text)
        if hit:
            value, measure, span = hit
            result.update(status="accepted", value=value, measure=measure, span=span, page=index + 1, backend=_BACKEND)
            return result
    return result
