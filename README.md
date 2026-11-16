# smerovka-maps

Offline mapové balíky (© prispievatelia OpenStreetMap, licencia ODbL) pre navigáciu Smerovka.
Súbory sú v sekcii Releases.

## Aktualizácia

Workflow **Zostavenie máp** (`.github/workflows/build-maps.yml`) beží automaticky 3. dňa v mesiaci:
stiahne aktuálne OpenStreetMap dáta (Geofabrik) a mapu (Protomaps), postaví trasy (Valhalla),
vyhľadávanie a balíky po krajinách a zverejní ich ako nový release. Appka si novú verziu nájde sama.
Ručne: Actions → Zostavenie máp → Run workflow.
