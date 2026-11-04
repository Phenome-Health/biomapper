"""Unit 1: the resumable EntityCache lives in a pandas-free core module.

The MCP server needs the cache, but ``biomapper.benchmarks`` imports pandas through its
``__init__``. The code was MOVED to ``biomapper._checkpoint``; the old path re-exports the very same
objects so the benchmark suite's caches and imports are unchanged.
"""

from __future__ import annotations

import subprocess
import sys

from biomapper import _checkpoint
from biomapper.benchmarks import checkpoint


def test_benchmarks_checkpoint_reexports_the_moved_objects() -> None:
    assert checkpoint.EntityCache is _checkpoint.EntityCache
    assert checkpoint.CachePinMismatch is _checkpoint.CachePinMismatch
    assert checkpoint.cache_key is _checkpoint.cache_key
    assert checkpoint.PIN_FIELDS == _checkpoint.PIN_FIELDS
    assert checkpoint.CACHE_FILENAME == _checkpoint.CACHE_FILENAME
    assert checkpoint.PROGRESS_FILENAME == _checkpoint.PROGRESS_FILENAME
    assert checkpoint.CACHE_EXCLUDED_FIELDS == _checkpoint.CACHE_EXCLUDED_FIELDS


def test_core_checkpoint_and_pins_import_without_pandas() -> None:
    code = (
        "import sys\n"
        "sys.modules['pandas'] = None\n"
        "from biomapper._checkpoint import EntityCache, cache_key, CachePinMismatch\n"
        "from biomapper._pins import collect_pins\n"
        "assert 'pandas' not in [m for m in sys.modules if sys.modules[m] is not None]\n"
        "assert 'biomapper.benchmarks' not in sys.modules\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_a_cache_written_through_one_path_is_read_through_the_other(tmp_path) -> None:
    from biomapper.models import MappingResult

    pin = {"api_endpoint": "x", "entity_type": "metabolite"}
    key = checkpoint.cache_key("glucose", None, {})
    old = checkpoint.EntityCache(tmp_path, pin)
    old.commit_batch(
        vocab=None,
        batch_index=0,
        keyed_results=[(key, MappingResult(query_name="glucose", chosen_kg_id="CHEBI:17234"))],
    )
    new = _checkpoint.EntityCache(tmp_path, pin)
    hit = new.get(key)
    assert hit is not None and hit.chosen_kg_id == "CHEBI:17234"
