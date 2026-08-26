# date-render

Automatische render-werker voor de **Date-locaties** op https://thom-fotografie.pages.dev.
Publieke repo = onbeperkte gratis GitHub Actions-minuten.

## Hoe het werkt
1. Iemand voegt op de site (Beheren) een stad/plek toe -> Cloudflare zet 'm op `pending` en
   stuurt een `repository_dispatch` (event `render-city`) hierheen, met de plekken in de payload.
2. De workflow (`.github/workflows/render.yml`):
   - `render_city.py`: OSM-data ophalen (`export_osm.py`) -> Blender headless (`export_glb.py`
     voor de `.glb`, `render_blender.py` voor de poster) -> WebP.
   - checkt de privé-site `website-git` uit, zet de nieuwe `.glb` + poster + `locations.json`
     erin, commit terug en deployt naar Cloudflare Pages.
   - meldt de status (`ready`/`error`) terug aan de site (`/api/render-status`).
3. Cloudflare start automatisch de volgende stad uit de wachtrij (max. 1 tegelijk).

## Scripts
- `render_city.py` - orchestrator (payload -> .glb + poster).
- `export_osm.py`, `export_glb.py`, `render_blender.py` - kopie uit het Blender-project
  (`...\mbo\maps`). **Let op:** als je daar de look aanpast, kopieer de scripts opnieuw.
- `publish.py` - kopieert de output naar de site-checkout + upsert `locations.json`.

## Secrets (Settings -> Secrets -> Actions)
`CF_TOKEN`, `CF_ACCOUNT_ID`, `RENDER_CALLBACK_SECRET`, `CALLBACK_URL`, `SITE_REPO_TOKEN`.
Zie `Planning/PROVISIONING-date-editor.md` in de site-repo.

## Lokaal testen
```bash
BLENDER="/pad/naar/blender" BAG=0 RESX=800 RESY=1000 SAMPLES=8 \
  python render_city.py --payload payload.json
# payload.json: {"slug":"...","city":"...","spots":[{"label":"..","lat":..,"lon":..}]}
```
