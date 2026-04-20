# biomapper → biomapper Full Rename (v1.0.0)

## Context

The `biomapper` package (Python client for the BioMapper2 API) is being published as `biomapper` on PyPI, replacing the old biomapper package. This is a full rename — package name, module name, imports, docs, everything.

**Source:** `~/trentleslie@gmail.com/Google Drive/projects/biomapper/` (v0.4.0, tag v0.4.0, all tests passing)
**Target:** This folder (`~/trentleslie@gmail.com/Google Drive/projects/biomapper/`) — will become v1.0.0
**PyPI:** `biomapper` (name already registered, owned by trentleslie)
**GitHub repos:**
- `trentleslie/biomapper` — dev (create new, or repurpose existing `arpanauts/biomapper`)
- `Phenome-Health/biomapper` — production mirror (like `Phenome-Health/biomapper` is now)

---

## Step 0: Prepare this folder

**Nuke everything in this folder except this file.** The old biomapper code (v0.6.0, cli/integrations/validation) is fully superseded and lives in git history on `arpanauts/biomapper` if ever needed.

```bash
# From this directory:
# Remove all old files EXCEPT this instruction file
find . -maxdepth 1 -not -name '.' -not -name 'RENAME_INSTRUCTIONS.md' -not -name '.git' -exec rm -rf {} +
```

Decide whether to keep the `.git` directory (preserving history from `arpanauts/biomapper`) or start fresh. **Recommendation:** Start fresh — the old history is irrelevant to the new package.

```bash
rm -rf .git
git init
```

---

## Step 1: Copy biomapper source

Copy these items from `~/trentleslie@gmail.com/Google Drive/projects/biomapper/`:

```
src/biomapper/          → src/biomapper/
tests/                 → tests/
notebooks/             → notebooks/
.github/               → .github/
pyproject.toml
README.md
LICENSE
.gitignore
.env                   → .env (contains BIOMAPPER_API_KEY — keep as-is)
```

**Do NOT copy:** `.git/`, `dist/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, `.coverage`, `.venv/`, `poetry.lock` (will regenerate), screenshots.

---

## Step 2: Rename module directory

```bash
mv src/biomapper src/biomapper
```

---

## Step 3: Global find-and-replace

These replacements must be applied across ALL `.py`, `.toml`, `.yml`, `.md`, `.ipynb` files:

### Python imports and references
| Find | Replace |
|------|---------|
| `from biomapper` | `from biomapper` |
| `import biomapper` | `import biomapper` |
| `biomapper.` (in code) | `biomapper.` |
| `"biomapper"` (in strings) | `"biomapper"` |
| `'biomapper'` (in strings) | `'biomapper'` |

### pyproject.toml specific changes
| Field | Old | New |
|-------|-----|-----|
| `name` | `"biomapper"` | `"biomapper"` |
| `version` | `"0.4.0"` | `"1.0.0"` |
| `description` | Current text | `"Python client for the BioMapper2 API — map biological entities to standardized knowledge graph identifiers"` (keep same) |
| `homepage` | `Phenome-Health/biomapper` | `trentleslie/biomapper` |
| `repository` | `Phenome-Health/biomapper` | `trentleslie/biomapper` |
| `documentation` | `Phenome-Health/biomapper#readme` | `trentleslie/biomapper#readme` |
| `packages` | `{include = "biomapper", from = "src"}` | `{include = "biomapper", from = "src"}` |

### CI (.github/workflows/ci.yml)
| Find | Replace |
|------|---------|
| `poetry run mypy src/biomapper/` | `poetry run mypy src/biomapper/` |

### pytest config (in pyproject.toml)
| Find | Replace |
|------|---------|
| `--cov=biomapper` | `--cov=biomapper` |

### README.md
| Find | Replace |
|------|---------|
| `biomapper` (package name references) | `biomapper` |
| `pip install biomapper` | `pip install biomapper` |
| `from biomapper import` | `from biomapper import` |
| GitHub URL references to biomapper repos | `trentleslie/biomapper` |

### Notebook (biomapper_tutorial.ipynb → biomapper_tutorial.ipynb)
- Rename the file
- Replace all `biomapper` references inside cells
- Replace all `from biomapper` / `import biomapper` in code cells

### src/biomapper/__init__.py
The module docstring currently says:
```python
"""biomapper — Python client for the BioMapper2 API.
```
Change to:
```python
"""biomapper — Python client for the BioMapper2 API.
```
And update the quick start example if it references `biomapper`.

---

## Step 4: Version bump rationale

Set version to `1.0.0` because:
- The old `biomapper` on PyPI was at 0.6.0 — publishing 0.4.0 would be a downgrade
- 1.0.0 signals a clean break and deliberate replacement
- biomapper's roadmap already planned 1.0.0 as the stability cut
- The `rate_limit_delay` deprecation from biomapper 0.3.0 can be finalized: either keep the deprecation warning or remove it entirely for 1.0.0 (recommend **removing** `rate_limit_delay` entirely since 1.0.0 was the planned removal point)

**Decision needed:** Remove `rate_limit_delay` parameter entirely for 1.0.0? The biomapper roadmap said 1.0.0 removes it. If yes:
- Delete `rate_limit_delay` from `BioMapperClient.__init__` and `map_entities`
- Remove the `DeprecationWarning` logic
- Remove from tests
- Update README

If you want to keep it for now, leave it with the deprecation warning.

---

## Step 5: Git setup and remotes

```bash
git init
git add .
git commit -m "feat: biomapper 1.0.0 — full rename from biomapper

Complete rename of the biomapper package to biomapper for PyPI publication.
This replaces the old biomapper package (v0.6.0) with the new BioMapper2
API client, formerly known as biomapper.

Breaking changes from old biomapper:
- Entirely new API surface (BioMapperClient, map_entity, map_entities)
- No backwards compatibility with biomapper <=0.6.0

Equivalent to biomapper v0.4.0 with full module rename."

git remote add origin git@github.com:trentleslie/biomapper.git
git remote add phenome git@github.com:Phenome-Health/biomapper.git
```

**Note:** You'll need to create `trentleslie/biomapper` on GitHub first. The existing `arpanauts/biomapper` repo can be archived.

---

## Step 6: Verify

```bash
# Install dependencies
poetry install --all-extras

# Lint
poetry run ruff check .

# Type check
poetry run mypy src/biomapper/

# Tests
poetry run pytest

# Sanity check imports
python -c "from biomapper import map_entity, map_entities, BioMapperClient; print('OK')"
```

All 95+ tests should pass. Coverage should remain >=80%.

---

## Step 7: Build and publish

```bash
# Build
poetry build

# Verify the built artifacts say "biomapper"
ls dist/  # Should show biomapper-1.0.0-py3-none-any.whl and biomapper-1.0.0.tar.gz

# Publish (use PyPI token scoped to biomapper project, or the unscoped token)
poetry publish
# Or: twine upload dist/*
```

---

## Step 8: Post-publish

1. **Tag:** `git tag v1.0.0 && git push origin main --tags`
2. **Push to phenome:** `git push phenome main --tags`
3. **Create GitHub repos** if not already done:
   - `trentleslie/biomapper` (dev)
   - `Phenome-Health/biomapper` (production)
4. **Archive** `arpanauts/biomapper` on GitHub (old code)
5. **Update biomapper README** to note it has been renamed to biomapper
6. **Update Replit UI** to depend on `biomapper>=1.0.0` instead of `biomapper`
7. **Update vault notes:**
   - `Active 🎯/Work/Projects/biomapper/biomapper.md` — note the rename
   - Weekly note — mark PyPI publish as done

---

## Files checklist

After all steps, verify these files exist and contain NO references to "biomapper" (except in historical context like changelogs):

- [ ] `src/biomapper/__init__.py`
- [ ] `src/biomapper/client.py`
- [ ] `src/biomapper/dataset.py`
- [ ] `src/biomapper/exceptions.py`
- [ ] `src/biomapper/mapper.py`
- [ ] `src/biomapper/models.py`
- [ ] `src/biomapper/extras/__init__.py`
- [ ] `src/biomapper/extras/metabolon/__init__.py`
- [ ] `src/biomapper/extras/metabolon/export.py`
- [ ] `src/biomapper/extras/metabolon/preprocessing.py`
- [ ] `tests/conftest.py`
- [ ] `tests/__init__.py`
- [ ] `tests/test_client.py`
- [ ] `tests/test_dataset.py`
- [ ] `tests/test_export.py`
- [ ] `tests/test_mapper.py`
- [ ] `tests/test_metabolon.py`
- [ ] `tests/test_models.py`
- [ ] `pyproject.toml`
- [ ] `README.md`
- [ ] `.github/workflows/ci.yml`
- [ ] `notebooks/biomapper_tutorial.ipynb`

**Final grep to confirm:**
```bash
grep -r "biomapper" src/ tests/ pyproject.toml .github/ README.md notebooks/
# Should return ZERO results
```

---

## Summary

| Item | Value |
|------|-------|
| Package name | `biomapper` |
| Version | `1.0.0` |
| Module import | `from biomapper import ...` |
| PyPI | `pip install biomapper` |
| Dev repo | `trentleslie/biomapper` |
| Prod repo | `Phenome-Health/biomapper` |
| Old package | `biomapper` (archived, README updated) |
| Old biomapper | `arpanauts/biomapper` v0.6.0 (archived) |
