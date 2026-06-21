# Fotó szétválogató

Streamlit kisalkalmazás: egy mappa képeit gyorsan szétválogathatod előnézettel — másolás vagy áthelyezés egy vagy több célmappába (kategóriák).

> **Online / Deploy:** lásd a [Deploy — Streamlit Community Cloud](#deploy--streamlit-community-cloud-online) szakaszt a README alján. Repó: `github.com/sapstar74/photo-sorter`, belépési pont: `organizer_metal_app.py`.

## Telepítés

```bash
cd photo_sorter
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

A `requirements.txt` tartalmazza az **ImageHash** csomagot is (a határoló kép összehasonlításához). Ha korábban rendszer-szintű `python3`-mal indítottad a Streamlitet **aktivált venv nélkül**, telepítsd a függőségeket, vagy mindig a **`.venv`** Pythonját használd (lásd indítás lent).

## Indítás (kézi szétválogató: `app.py`)

A `.streamlit/config.toml` **nem köti meg a portot** (a Streamlit Community Cloud kompatibilitás miatt; a felhő a 8501-es porton ellenőriz). Helyben a 8510-es portot a `--server.port 8510` kapcsolóval kérheted. Egyszerre csak egy Streamlit app fusson ebből a mappából (vagy állítsd le a másik alkalmazást).

```bash
cd photo_sorter
source .venv/bin/activate
python3 -m streamlit run app.py --server.port 8510
```

Cím: **http://127.0.0.1:8510** (vagy az alapértelmezett **8501**, ha nem adsz meg portot)

## Használat

1. **Forrás mappa** és **cél gyökér**: beírhatod az útvonalat, vagy a **Tallózás…** gombbal (macOS: Finder / `osascript`; máshol: `zenity` vagy külön folyamatban futó tallózó — nem a Streamlit folyamatban futó Tk). Felhőben / headless környezetben a tallózás nem mindig működik — ott marad a kézi bevitel.
2. **Cél gyökér**: ide kerülnek a kategória almappák (ha üresen hagyod, alapértelmezés: `forras/szétválogatva` a megadott forrás alatt).
3. **Kategóriák**: soronként egy mappanév (pl. `Család`, `Utazás`, `_kuka`). Minden névhez egy gomb jelenik meg.
4. **Művelet**: másolás vagy áthelyezés.
5. **Képek betöltése** után lépkedhetsz: kategória gomb → a fájl a megfelelő almappába kerül, jön a következő kép.

Támogatott kiterjesztések: `.jpg`, `.jpeg`, `.png`, `.webp`, `.gif` (és nagybetűs változatok). A HEIC megjelenítéshez külön bővítmény kell; ilyen fájlokat egyelőre kihagyja a lista, ha nem nyitható meg.

## Megjegyzés

Ez a mappa a `qr_decoder_project` melletti önálló projekt; külön git repót indíthatsz benne (`git init`), ha szeretnéd verziókezelni.

---

## TAG / mappa rendező (fém adatlap OCR + határoló kép)

Második alkalmazás: a forrás mappában lévő **képek és PDF-ek** **fájlnév** szerint (lexikografikusan, tipikusan időbélyeg a névben), majd **módosítás ideje** másodlagosan kerülnek feldolgozásra; **fémlapon olvasható azonosító** ad **TAG / mappa** nevet; **határoló fénykép** (referenciához hasonló) zárja a szegmenst.

**Öt lépés** (Streamlit lapok):

1. **Kiindulás** — forrás, cél gyökér, határoló referencia, hash csúszkák, rekurzió; **Kiindulási terv** = határolók automatikus azonosítása.
2. **Határolók** — **Ez nem határoló kép** csak **jelöl** (lista); **kézi határolók**: natív **Tallózás** (több fájl egyszerre) vagy útvonal mező. A terv **újraszámolása nem** ezen a lapon van.
3. **TAG / mappa** — **Képek frissítése** a határolók és követő képek előnézetéhez (2. lépés alapján); OCR szerkesztés.
4. **Terv véglegesítése** — **Terv újraszámolása** (2. lépés jelölései + kényszerített határolók → TAG/mappa, cache ha lehet), majd kihagyandó képek (multiselect).
5. **Válogatás indítása** — másolás / áthelyezés, opció: TAG nélküli fájlok → `_nincs_köteg` (belső mappanév, kompatibilitás).

### Rendszerkövetelmény: Tesseract OCR

- **macOS:** `brew install tesseract tesseract-lang`
- A `pytesseract` a rendszer `tesseract` binárist hívja. Ha nem találja, állítsd: `export TESSDATA_PREFIX=...` (lásd Tesseract dokumentáció).
- **Streamlit** ≥ 1.33 (ajánlott; régebbi verzió is futhat, de a teljes oldal újrafut jelölésnél).

### Indítás

A **`.streamlit/config.toml`** nem köti meg a portot (felhő-kompatibilitás miatt). Helyben rögzített portot a `--server.port 8510` kapcsolóval kérhetsz.

```bash
cd photo_sorter
source .venv/bin/activate
python3 -m streamlit run organizer_metal_app.py --server.port 8510
```

Böngésző: **http://127.0.0.1:8510** (vagy az alapértelmezett **8501**)

- **Forrás** és **cél gyökér** mező mellett **Tallózás…** (macOS: Finder; egyébként `zenity` vagy külön Python-folyamat tallózója); felhő / headless esetén írd be az útvonalat kézzel.

1. Az **1 — Kiindulás** lapon töltsd fel a **határoló** referenciát, állítsd a paramétereket, majd **Kiindulási terv készítése**.
2. A **2 — Határolók** lapon jelölj (nem határoló, kényszerített határoló tallózással / útvonallal).
3. A **3 — TAG / mappa** lapon szerkeszd az OCR neveket.
4. A **4 — Terv véglegesítése** lapon **Terv újraszámolása** (csak ha a 2. lapon volt módosítás — ilyenkor **progress sáv** + állapotszöveg látszik a gomb alatt), majd jelöld ki a kihagyandó képeket.
5. Az **5** lapon **Válogatás végrehajtása**.

### Kimeneti szerkezet (minden TAG / mappa szegmensre)

```
<cél gyökér>/
  <OCR_mappa_név>/
    jegyzőkönyv/     ← .pdf fájlok
    fotók/           ← a szegmenshez tartozó képek (beleértve a névadó fémlapos képet is)
```

- Ha **már létezik** `<OCR_mappa_név>`, a program **nem** hoz létre új nevet: ugyanazt a mappát használja, a fájlok a meglévő `jegyzőkönyv` / `fotók` alá kerülnek (ütközés esetén új fájlnév).

### Határoló

- A **feltöltő mezőbe** húzd a referencia képet (Streamlit drag-and-drop), vagy válassz fájlt.
- Az egyeztetés **pHash + average hash** (Pillow: **EXIF orientáció** alkalmazása után, RGB, átlátszóság fehér háttérrel). Telefonos fotóknál az EXIF nélkül gyakran **nem talált** egyezést a régi logika.
- A csúszka **pHash Hamming** fő küszöb; ha az nem elég, az **aHash** csak akkor „ment”, ha a **pHash nem nőtt túl sokat** a küszöbhöz képest (kevesebb téves találat, mint a régi tiszta aHash-OR logikánál). Ha kevés a találat, **növeld** a csúszkát, vagy a referenciát hasonlóbb fény/távolság mellett készítsd.
- **Téves határoló:** **Ez nem határoló kép** = csak **jelölés** (lista); a TAG-terv a **4 — Terv véglegesítése** lapon a **Terv újraszámolása** gombbal frissül. **Kényszerített határoló**: **Tallózás — több kép** vagy teljes útvonal — a hash-alapú felismerést megkerüli. **Összes „nem határoló” kijelölés törlése** kiüríti a demóciós listát. (A referencia a legutóbbi sikeres tervkészítésből marad a munkamenetben.)
- **Sebesség:** a terv készítése a képek hash-eit **több szálon** előszámolja. A **terv újraszámolása** (4. lépés, kézi nem-határoló / kényszerített lista után) ugyanabból a mappa-bejárásból **cache-ből** futhat: nincs újra teljes hash-számítás; OCR csak azokon a képeken, amelyek korábban határolóként kiestek az OCR-ből. Ha közben megváltoztatod a forrás útvonalat, a rekurziót vagy a határoló csúszkákat, teljes újraszámítás fut.

### OCR korlátok

- A fémlap OCR **automatikus** szürkeárnyalat-optimalizálást használ (percentilis, világosság, CLAHE); a határoló pHash-ra nincs hatása.
- A terv **fájllistáinál** a fájlnév fölé húzva a böngésző **tooltip** mutatja a teljes útvonalat; a **popover** gombbal (címke: sorszám · fájlnév) kattintva nyílik a **képelőnézet** (Streamlit nem támogatja megbízhatóan a hoverre megjelenő nagy képet).
- A fémlap felismerés **heurisztikus** (Tesseract több PSM mód). Rossz fény, tükröződés, ferde lapos kép → hibás név vagy „TAG nélküli” kép.
- A TAG nélküli képek opcionálisan a cél `_nincs_köteg/fotók` (és PDF-ek `jegyzőkönyv`) alá tehetők — ez a **mappanév** a lemezen maradhat (kompatibilitás); a felületen **TAG nélküli** szerepel.

### macOS: „Operation not permitted” a Documents mappánál

Ha a forrás a `Documents` alatt van és **PermissionError** / „Operation not permitted” jelenik meg: a Streamlitet futtató appnak (Terminal / Cursor) adj **Teljes lemezhez való hozzáférést** a Rendszerbeállítások → Adatvédelem és biztonság menüben, vagy használj olyan forrásmappát, amit az app sandbox nélkül olvas (pl. a projekt alatti tesztmappa).

---

## Deploy — Streamlit Community Cloud (online)

Az alkalmazás publikus telepítésre kész a **Streamlit Community Cloudon**.

- **Repó:** `github.com/sapstar74/photo-sorter` (publikus), ág: `main`.
- **Belépési pont (main file):** `organizer_metal_app.py`.
- **Python függőségek:** `requirements.txt` (a `pytest` külön a `requirements-dev.txt`-ben, nem kerül a futtatásba).
- **Rendszercsomagok:** `packages.txt` — a Streamlit Cloud `apt`-tal telepíti. Tartalma: `tesseract-ocr`, `tesseract-ocr-hun`, `tesseract-ocr-eng` (a `pytesseract` ezt a rendszer-binárist hívja, magyar + angol nyelvvel), valamint `libgl1` és `libglib2.0-0` (az OpenCV / Pillow képkezeléshez).
- **Konfiguráció:** `.streamlit/config.toml` — **nem rögzít portot** (a Streamlit Cloud a 8501-es porton ellenőriz; egy korábbi `port = 8510` pin okozta a health check hibát, ezért eltávolítottuk). Helyi futtatáskor `--server.port 8510` adható. A `headless = true` biztonságos felhős futáshoz.

### Helyi mappa vs. Képek feltöltése (felhő-mód)

A felhőben az app **nem** fér hozzá a géped lemezéhez és nem tud Findert/zenityt nyitni. Ezért a felület tetején **Bemenet módja** választó van:

- **Helyi mappa** *(asztali használat)*: a megszokott folyamat — forrás/cél mappa útvonala + **Tallózás**; a fájlok **helyben** mozognak/másolódnak. Natív tallózás hiányában a gombok no-opok (figyelmeztetés jelenik meg).
- **Képek feltöltése** *(felhő / headless — itt az alapértelmezett)*: feltöltöd a képeket és PDF-eket (`st.file_uploader`), ezek egy szerveroldali **ideiglenes mappába** kerülnek (a fájlnév adja a feldolgozási sorrendet). Ugyanaz a `metal_batch_logic` pipeline fut (`build_plan` → 5 lépéses varázsló → `execute_plan`), csak a kimenet egy **ideiglenes mappába** épül, amit a végén **ZIP-ként letöltesz** (`st.download_button`). Helyi fájl nem mozdul.

A mód automatikusan **feltöltésre** vált, ha nincs natív tallózó (felhő/headless). Felülbírálható a `PHOTO_SORTER_INPUT_MODE` környezeti változóval (`upload` / `local`), vagy a felületi választóval.

### Telepítés a Streamlit Community Cloudon (kézi lépések)

A Streamlit Cloudnak **nincs CLI-je** — a deploy a böngészőből történik:

1. Menj a [share.streamlit.io](https://share.streamlit.io) oldalra, és jelentkezz be a **GitHub** fiókkal (`sapstar74`).
2. **Create app** → **Deploy a public app from GitHub**.
3. Add meg pontosan:
   - **Repository:** `sapstar74/photo-sorter`
   - **Branch:** `main`
   - **Main file path:** `organizer_metal_app.py`
4. **Deploy** — az első build telepíti a `packages.txt` (apt) és `requirements.txt` (pip) csomagjait; ez néhány percig tarthat (a Tesseract miatt).

Build után az app az upload-módban fut: a felhasználó feltölti a képeket + a határoló referenciát, és a végén ZIP-et tölt le.

