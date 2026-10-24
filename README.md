# BillBot market updater

This repository contains the existing GitHub-based energy and banking market updater. The original daily updater and the **Lovable/Supabase refresh workflow are deliberately preserved**.

## New: separate static JSON feed

The standalone BillBot website can read market data from GitHub without contacting Lovable or Supabase.

- `public-data/manifest.json` — data timestamp, counts and index locations.
- `public-data/energy/ELECTRICITY/index.json` and `.../GAS/index.json` — postcode/distributor lookups.
- `public-data/energy/{FUEL}/chunk-XXXX.json` — plan details for pricing in the browser.
- `public-data/nbn.json` and `public-data/savings.json` — current offer catalogues.

All JSON is derived from the checksum-validated SQLite snapshot in the existing GitHub Release. The new `updater/build_market_csv.py` and `updater/export_web_data.py` leave the main updater logic unchanged.

Run locally with an existing, validated Release download:

```sh
mkdir -p .cloud/work
# Place the release's latest.db.gz in .cloud/work/snapshot.db.gz
# Place the accompanying manifest.json in .cloud/work/manifest.json
python -m unittest updater.test_export_web_data -v
python updater/export_web_data.py
```

## Safe deployment

The `publish-static-feed.yml` workflow uses the latest already-published Release to build and commit the `public-data/` feed, without running another full market scan. It can be run manually on `main`. It also runs automatically after a successful existing updater workflow.

On the `chatgpt/static-feed-20261024` preview branch, it runs after source changes and publishes the **preview feed only on that branch**. This does not affect the live Lovable app.

After verifying the preview, merge the pull request. The production website uses:

`https://raw.githubusercontent.com/oswade/bbdata/main/public-data/manifest.json`

## Safety

- Do not remove `.github/workflows/billbot-web-refresh.yml` while the Lovable site still needs it.
- Do not commit `.cloud/`, credentials or user bill data.
- The feed is public, includes public market products only, and must not contain private customer information.
- If snapshot verification or source validation fails, the workflow fails and does not publish incomplete data.
