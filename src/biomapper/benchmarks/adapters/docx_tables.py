"""Deterministic table extraction from a .docx, using only the standard library.

The Hajjar-100 gold set ships as a Word table inside the paper's supplement, so it has to be
parsed rather than read as CSV. This is stdlib ``zipfile`` + ``ElementTree`` on purpose: adding
``python-docx`` for one table would put a dependency between the pinned bytes and the parsed
gold, and a dependency upgrade that changed cell-text assembly would silently move a benchmark
number.

Determinism is the whole point. The same bytes must always produce the same table, in the same
order, so the regenerated gold can be SHA-pinned and compared across runs.

Verified 2026-09-23: the parse of the pinned supplement
(``sha256 a58ca331…``) reproduces ``hajjar_100_gold.csv`` byte-for-byte modulo line endings.
That settles the provenance question about that file — it is a faithful re-parse of this docx
with CRLF endings, not a re-derivation of unknown origin.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

DOCUMENT_PART = "word/document.xml"


def _cell_text(cell: ET.Element) -> str:
    """Concatenate a table cell's runs, one line per paragraph.

    Runs are joined with no separator (Word splits a single word across runs for formatting, so
    inserting anything would corrupt values like an InChIKey), while paragraphs are joined with
    a newline so a genuinely multi-line cell is not silently flattened into one token.
    """
    lines: list[str] = []
    for paragraph in cell.iter(f"{W}p"):
        lines.append("".join(node.text or "" for node in paragraph.iter(f"{W}t")))
    return "\n".join(lines).strip()


def _reject_doctype(document_xml: bytes) -> None:
    """Refuse a document part that declares a DOCTYPE.

    A real .docx never does. ``xml.etree`` already refuses to resolve external entities, but it
    will still process an internal entity definition, which is the billion-laughs vector. Every
    caller here parses SHA-pinned bytes, so this is defence in depth rather than the primary
    control — but it costs one scan and removes the class outright without pulling in another
    XML library.
    """
    head = document_xml[:4096].lstrip()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in document_xml[:4096]:
        raise ValueError(
            "document.xml declares a DOCTYPE or ENTITY. A legitimate .docx does not; refusing to "
            "parse it rather than risk entity expansion."
        )


def tables(raw: bytes) -> list[list[list[str]]]:
    """Every table in the document, in document order, as rows of cell strings.

    Raises:
        SourceIntegrityError-compatible ``ValueError`` if the archive carries no document part,
        which means the bytes are not a .docx at all — worth failing on rather than returning
        an empty table list that would read as "no tables in this document".
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if DOCUMENT_PART not in archive.namelist():
            raise ValueError(
                f"archive has no {DOCUMENT_PART}; these bytes are not a .docx "
                f"(members: {archive.namelist()[:10]})"
            )
        document_xml = archive.read(DOCUMENT_PART)

    _reject_doctype(document_xml)
    root = ET.fromstring(document_xml)
    body = root.find(f"{W}body")
    if body is None:  # pragma: no cover - malformed document
        return []

    out: list[list[list[str]]] = []
    for table in body.iter(f"{W}tbl"):
        rows = [
            [_cell_text(cell) for cell in row.findall(f"{W}tc")] for row in table.findall(f"{W}tr")
        ]
        out.append(rows)
    return out


def table_with_header(raw: bytes, required_columns: tuple[str, ...]) -> list[list[str]]:
    """The first table whose header row contains every name in ``required_columns``.

    Selecting by header rather than by index is deliberate: the Hajjar supplement holds three
    tables (the gold set, then two competitor-comparison tables), and an index would silently
    start scoring a competitor table if the publisher ever reordered the document.

    Raises:
        ValueError: when no table matches, listing the headers that were found so the mismatch
            is diagnosable from the error alone.
    """
    found: list[list[str]] = []
    for table in tables(raw):
        if not table:
            continue
        header = table[0]
        found.append(header)
        if all(column in header for column in required_columns):
            return table
    raise ValueError(
        f"no table carries all of {list(required_columns)}. Headers found: {found}. "
        f"The supplement's format changed; re-resolve the column mapping deliberately rather "
        f"than loosening this match."
    )
