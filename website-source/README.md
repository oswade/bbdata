# Standalone BillBot website — safe deployment source

The *existing Lovable app remains unchanged*. This separate GitHub Pages website is deployed from an independent source archive in this folder.

## Source archive required

Upload the full source ZIP from ChatGPT, **BillBot_Full_Web_Source_Android_Parity.zip**, here under the filename:

`website-source/billbot-web.zip`

To upload it in GitHub's web interface, go to `https://github.com/oswade/bbdata/tree/main/website-source`, choose **Add file → Upload files**, select the ZIP and commit it to `main`. This is the only source handoff necessary. The entire website source is inside the archive. Do **not** unzip the archive in the updater repository or replace existing workflow files.

### Automatic deployment

The separate `.github/workflows/deploy-billbot-web.yml` workflow runs automatically when this ZIP changes on `main`. It:

1. Verifies the ZIP and required pricing/data files.
2. Rejects accidental Lovable/Supabase service URLs in the source.
3. Installs dependencies and runs Vitest regression tests.
4. Builds the full React website for the `/bbdata/` project-path URL.
5. Adds GitHub Pages SPA fallback and uploads a checked build.
6. Deploys to `https://oswade.github.io/bbdata/` (subject to GitHub Pages being enabled under Settings → Pages → GitHub Actions).

If automatic deployment is disabled, enable GitHub Pages with **Settings → Pages → Build and deployment → Source → GitHub Actions**, then run the deployment workflow manually.

## Data access

The website only reads public JSON from `https://raw.githubusercontent.com/oswade/bbdata/main/public-data/`.
User postcode/bill data stays in browser local storage. It does not change the daily updater, the market JSON feed, or the current Lovable refresh workflow.

## Android parity additions

The source ZIP retains the Lovable web UI, comparison tabs and pricing engine and adds Android v1.4.46's regional 8-chip electricity/gas household presets and regression tests covering indicative two-band and three-band TOU plans. Billing PDF extraction is deliberately not re-enabled: it would require a secure AI backend or user-provided key.

## Checks / release gate

- ZIP CRC check and TypeScript parser check are completed locally before source upload.
- The GitHub Actions workflow must report green before using the hosted site as the replacement.
- After deployment, verify postcode 3168, UNITED ENERGY, gas MULTINET, NBN500 and both savings rankings in the browser. No upstream Lovable endpoint should be called.
