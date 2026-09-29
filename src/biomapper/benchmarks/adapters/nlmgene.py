"""NLM-Gene name-input gene-normalization adapter (CURIE-equality arm, ambiguity-partitioned).

Islamaj Doğan et al. 2021 (J. Biomed. Inform., doi:10.1016/j.jbi.2021.103779). 550 PubMed abstracts,
~15K gene mentions across ~28 species, doubly annotated by six NLM indexers. The gold (surface form
-> NCBI Gene id) is HUMAN-CURATED from literature, NOT a database-table join, so even though the gold
NAMESPACE (NCBI Gene) sits downstream in BioMapper's resolution path, the gold MAPPING is independent
of BioMapper's xref path by construction. This is the arm's independent name-input accuracy anchor
(HGNC's 96.3% is partly circular; NLM-Gene replaces the *accuracy* claim).

Distributed as one BioC XML file per PMID under ``{source_url}/Corpus/{pmid}.BioC.XML`` (+
``Pmidlist.Train.txt`` / ``Pmidlist.Test.txt``). Each ``<annotation>`` of ``type=Gene`` carries the
surface ``<text>`` and an ``NCBI Gene identifier`` infon (comma- OR semicolon-separated when the
curators judged the span to denote several genes). ``type=GeneRIF`` spans are gene-RELATED descriptive references, NOT
normalization mentions, and are excluded (verify against NLM-Gene-datadescription.docx).

AMBIGUITY PARTITION (the design point): mentions are grouped by exact surface form; the union of gold
NCBI Gene ids over all occurrences of a form is its referent set. A form with exactly one referent is
UNAMBIGUOUS (scored for accuracy by ``curie_scorer.score_curie``); a form with >= ``ambiguous_min``
referents (or any single multi-id annotation) is AMBIGUOUS (scored for EITL flag-rate by
``nlmgene_scorer``). Splitting BEFORE scoring is mandatory: a bare context-stripped ambiguous form is
genuinely unanswerable, so demanding one right answer there would deflate the accuracy number — the
split IS the mitigation for the context-stripping threat.

Network (the 550 GETs) is isolated behind ``fetch_corpus``; the BioC parse + aggregation + partition
transforms run on an in-memory iterable of (pmid, xml_text) and are fully unit-testable offline.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

# defusedxml, NOT stdlib xml.etree: the corpus is downloaded XML with a ``BioC.dtd`` DOCTYPE, and
# stdlib parsers are vulnerable to XXE / billion-laughs. ``fromstring`` returns standard Element
# objects, so ``.iter``/``.findall``/``.find`` below are unchanged.
from defusedxml.ElementTree import fromstring as _xml_fromstring

from biomapper.benchmarks.adapters.backbones import sha256_bytes, subsample_csv_bytes
from biomapper.benchmarks.config import NLMGENE, NLMGENE_AMBIGUOUS_MIN_GENES, CurieDatasetConfig

CURIE_DELIM = "|"
GENE_NAMESPACE = "NCBIGene"
GOLD_COLUMN = "gold_ncbigene"
PARTITION_COLUMN = "partition"
UNAMBIGUOUS = "unambiguous"
AMBIGUOUS = "ambiguous"
NEEDS_FETCH_SENTINEL = "fetch"


@dataclass(frozen=True)
class GeneMention:
    """One ``type=Gene`` annotation. ``gene_ids`` has >1 entry for a curator-annotated multi-gene span."""

    mention: str
    gene_ids: tuple[str, ...]


def parse_bioc_documents(docs: Iterable[tuple[str, str]]) -> Iterator[GeneMention]:
    """Yield one GeneMention per ``type=Gene`` annotation across the (pmid, xml_text) docs.

    ``type=GeneRIF`` and any non-Gene annotations are skipped (they are gene-related descriptive
    spans, not normalization mentions). The ``NCBI Gene identifier`` infon is comma-split into the
    referent id list; a blank/absent id or blank surface text is skipped.
    """
    for _pmid, xml_text in docs:
        root = _xml_fromstring(xml_text)
        for ann in root.iter("annotation"):
            infons = {i.get("key"): (i.text or "") for i in ann.findall("infon")}
            if infons.get("type") != "Gene":
                continue
            raw_ids = infons.get("NCBI Gene identifier", "").strip()
            if not raw_ids:
                continue
            # NLM-Gene multi-gene spans are delimited by EITHER a comma or a semicolon across the
            # corpus (e.g. "12458,12772,12775" and "5595;5594"); split on both so a semicolon span
            # is not fused into one malformed id and mis-routed to the unambiguous partition.
            ids = tuple(x.strip() for x in raw_ids.replace(";", ",").split(",") if x.strip())
            if not ids:
                continue
            text_el = ann.find("text")
            mention = (text_el.text or "").strip() if text_el is not None else ""
            if not mention:
                continue
            yield GeneMention(mention=mention, gene_ids=ids)


def _curie(gene_id: str) -> str:
    return f"{GENE_NAMESPACE}:{gene_id}"


def aggregate_surface_forms(mentions: Iterable[GeneMention]) -> dict[str, set[str]]:
    """Map each exact (stripped) surface form to the union of its NCBI Gene ids across occurrences.

    Grouping is by exact surface text (no case-fold): BioMapper receives the string verbatim, so
    over-merging by case would fabricate ambiguity. A single multi-id annotation already contributes
    >=2 ids to its form's set, so it lands in the ambiguous partition without special-casing.
    """
    referents: dict[str, set[str]] = {}
    for m in mentions:
        referents.setdefault(m.mention, set()).update(m.gene_ids)
    return referents


def build_nlmgene_input_df(
    mentions: Iterable[GeneMention], *, ambiguous_min: int = NLMGENE_AMBIGUOUS_MIN_GENES
) -> pd.DataFrame:
    """Deduped one-row-per-surface-form input_df with held-out gold + partition label.

    Columns: ``mention`` (the query handed to the mapper), ``gold_ncbigene`` (|-joined NCBIGene
    CURIEs, held out for the scorer), ``partition`` ("unambiguous" | "ambiguous"). A form whose
    referent set has >= ``ambiguous_min`` distinct genes is AMBIGUOUS. Sorted by mention so the
    persisted CSV (and its SHA) is deterministic.
    """
    referents = aggregate_surface_forms(mentions)
    rows: list[dict[str, str]] = []
    for mention in sorted(referents):
        gene_ids = sorted(referents[mention], key=lambda s: (len(s), s))
        gold = CURIE_DELIM.join(_curie(g) for g in gene_ids)
        partition = AMBIGUOUS if len(gene_ids) >= ambiguous_min else UNAMBIGUOUS
        rows.append({NLMGENE.name_column: mention, GOLD_COLUMN: gold, PARTITION_COLUMN: partition})
    return pd.DataFrame(rows, columns=[NLMGENE.name_column, GOLD_COLUMN, PARTITION_COLUMN])


@dataclass(frozen=True)
class NlmGeneBundle:
    input_df: pd.DataFrame
    card: dict[str, Any]


def subsample_filename(key: str) -> str:
    """Filename for the persisted deduped input_df beside the dataset card."""
    return f"{key}_subsample.csv"


def build_card(
    input_df: pd.DataFrame,
    *,
    n_mentions: int,
    n_documents: int,
    source_sha: str,
    config: CurieDatasetConfig,
) -> dict[str, Any]:
    """Dataset card: partition sizes, provenance SHA, gold identity, and the honesty notes."""
    n = len(input_df)
    n_amb = int((input_df[PARTITION_COLUMN] == AMBIGUOUS).sum())
    return {
        "dataset": config.key,
        "arm": config.arm,
        "entity_type": config.entity_type,
        "input_type": config.input_type,
        "target_vocabs": list(config.target_vocabs),
        "gold_curie_columns": {ns: col for ns, col in config.gold_curie_columns},
        "n_surface_forms": n,
        "n_unambiguous": n - n_amb,
        "n_ambiguous": n_amb,
        "ambiguous_min_genes": NLMGENE_AMBIGUOUS_MIN_GENES,
        "n_mentions_scanned": n_mentions,
        "n_documents": n_documents,
        # The corpus URL is a mutable mirror, so URL alone cannot reconstruct the scored set; the
        # persisted deduped input_df (SHA below) is the reproducibility pin (backbone discipline).
        "subsample_sha256": source_sha,
        "subsample_filename": subsample_filename(config.key),
        "source_label": config.source_label,
        "source_url": config.source_url,
        "license": config.license,
        # HONESTY: gold namespace (NCBIGene) is downstream in BioMapper's path, but the gold MAPPING is
        # human-curated -> independent. Mentions were annotated IN CONTEXT; BioMapper gets a bare form,
        # so ambiguous forms are unanswerable by construction -> scored on flag-rate, never accuracy.
        "independence_note": (
            "human-curated per-mention gold (independent mapping); ambiguous partition is "
            "context-stripped by design and scored on EITL flag-rate, not accuracy"
        ),
    }


def subsample_csv_bytes_local(input_df: pd.DataFrame) -> bytes:
    """Alias kept for readability; identical to backbones.subsample_csv_bytes (the SHA-pinned bytes)."""
    return subsample_csv_bytes(input_df)


def persist_input_df(bundle: NlmGeneBundle, out_dir: Path | str) -> Path:
    """Write the exact deduped input_df beside the card so the run is reconstructable. Returns the path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / subsample_filename(bundle.card["dataset"])
    path.write_bytes(subsample_csv_bytes(bundle.input_df))
    return path


def load_nlmgene(docs: Iterable[tuple[str, str]], config: CurieDatasetConfig = NLMGENE) -> NlmGeneBundle:
    """Parse (pmid, xml_text) docs -> mentions -> deduped/partitioned input_df + card (with SHA pin)."""
    docs = list(docs)
    mentions = list(parse_bioc_documents(docs))
    input_df = build_nlmgene_input_df(mentions)
    sha = sha256_bytes(subsample_csv_bytes(input_df))
    card = build_card(input_df, n_mentions=len(mentions), n_documents=len(docs), source_sha=sha, config=config)
    return NlmGeneBundle(input_df=input_df, card=card)


def read_local_corpus(directory: Path | str) -> Iterator[tuple[str, str]]:
    """Read a local directory of ``{pmid}.BioC.XML`` files (downloaded once, pinned). Offline."""
    directory = Path(directory)
    files = sorted(directory.glob("*.BioC.XML"))
    if not files:
        raise FileNotFoundError(f"no *.BioC.XML files under {directory}")
    for f in files:
        yield (f.name.split(".")[0], f.read_text(encoding="utf-8"))


def read_pmid_list(text: str) -> list[str]:
    """Parse a Pmidlist.*.txt into PMIDs (one per line; blanks/comments skipped)."""
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]


def fetch_corpus(config: CurieDatasetConfig = NLMGENE, *, timeout: float = 60.0) -> Iterator[tuple[str, str]]:
    """Download the corpus (network seam; NOT unit-tested): read the Train+Test PMID lists, then GET
    each ``{source_url}/Corpus/{pmid}.BioC.XML``. Yields (pmid, xml_text).
    """
    import requests

    base = config.source_url.rstrip("/")
    pmids: list[str] = []
    for name in ("Pmidlist.Train.txt", "Pmidlist.Test.txt"):
        resp = requests.get(f"{base}/{name}", timeout=timeout)
        resp.raise_for_status()
        pmids.extend(read_pmid_list(resp.text))
    for pmid in pmids:
        resp = requests.get(f"{base}/Corpus/{pmid}.BioC.XML", timeout=timeout)
        resp.raise_for_status()
        yield (pmid, resp.text)
