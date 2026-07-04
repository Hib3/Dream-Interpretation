# GitHub Pages Operations

Live app:

- https://hib3.github.io/Dream-Interpretation/

Repository:

- https://github.com/Hib3/Dream-Interpretation

## What Is Published

- `index.html`, `styles.css`, `app.js`
- `data/dream_terms.min.json` for the browser app
- `data/dream_terms.json` for readable full data
- `data/source_registry.json` for source status

`data/raw/` is intentionally ignored because it contains heavy source caches.

## Current Dataset

- entries: 22150
- duplicate `term_normalized`: 0
- languages: `en`, `tr`, `zh-Hant`, `my`
- implemented sources: 15

## Verify Locally

```powershell
python scripts/build_dream_terms.py
python -m json.tool data\dream_terms.json > $null
python -m json.tool data\dream_terms.min.json > $null
python -m json.tool data\source_registry.json > $null
node --check app.js
```

## Deploy

```powershell
git add .
git commit -m "Update dream dictionary"
git push
```

GitHub Actions deploys Pages from `master`.

## Update Policy

- Add more sources through `scripts/build_dream_terms.py`.
- Keep source status in `data/source_registry.json`.
- Avoid exclusive copyright / all-rights-reserved sources.
- If a source has no explicit license but publishes raw JSON/CSV without exclusive notices, record that uncertainty in `license_note`.
- Reject datasets that do not contain interpretation or meaning text.
