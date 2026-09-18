import os
import re
import sys
import csv
import json
import time
import logging
from datetime import date
from collections import defaultdict

import requests
import mysql.connector


DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 13307,
    "user": "root",
    "password": "9cec884a3ec863f7",
    "charset": "utf8mb4",
    "collation": "utf8mb4_general_ci",
}

TABELA_FIRME = "zivro_admin.firme"
TABELA_CATALOG = "zivro_connect.catalog_produse_servicii_romania"

ANAF_URL = "https://webservicesp.anaf.ro/api/PlatitorTvaRest/v9/tva"

BATCH_SIZE = 100
DB_PAGE_SIZE = 10000
PART_SIZE = 100000

DELAY_ANAF = 0.10

TIMEOUT_CONNECT = 10
TIMEOUT_READ = 60
MAX_REINCERCARI = 4

OUTPUT_DIR = "firme_produse_servicii_parts"
STATE_FILE = os.path.join(OUTPUT_DIR, "checkpoint.json")

FISIER_CORESPONDENTA = "corespondenta_caen.csv"

CURATA_BRANDURI = True

logging.basicConfig(level=logging.INFO, format="%(message)s")

logging.getLogger("mysql.connector").setLevel(logging.WARNING)

log = logging.getLogger("detectare")

ANAF_SESSION = requests.Session()

ANAF_SESSION.headers.update(
    {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connection": "keep-alive",
    }
)


CORESPONDENTA_IMPLICITA = {
    "4789": "4778",
    "4799": "4792",
    "4110": "4100",
    "4120": "4100",
    "5610": "5611",
    "2219": "2212",
    "4719": "4712",
    "7022": "7020",
    "5144": "4644",
    "8690": "8699",
    "6201": "6210",
    "7310": "7311",
    "4652": "4650",
    "6810": "6811",
    "9103": "9122",
    "5211": "4711",
    "5250": "4779",
    "7460": "8001",
    "8009": "8001",
    "2524": "2222",
    "4511": "4781",
    "4675": "4685",
}


def conecteaza():
    return mysql.connector.connect(**DB_CONFIG)


def get_firma_dupa_cui(cui):
    cui = re.sub(r"\D", "", str(cui or ""))

    conn = conecteaza()

    try:
        cur = conn.cursor(dictionary=True)

        cur.execute(
            f"""
            SELECT id, denumire, cui
            FROM {TABELA_FIRME}
            WHERE stare_firma = 'Activă'
              AND REPLACE(
                    REPLACE(
                        UPPER(TRIM(cui)),
                        'RO',
                        ''
                    ),
                    ' ',
                    ''
                  ) = %s
            ORDER BY id
            LIMIT 1
            """,
            (cui,),
        )

        firma = cur.fetchone()

        cur.close()

        return firma

    finally:
        conn.close()


def get_firme_dupa_id(ultimul_id, limita):
    conn = conecteaza()

    try:
        cur = conn.cursor(dictionary=True)

        cur.execute(
            f"""
            SELECT id, denumire, cui
            FROM {TABELA_FIRME}
            WHERE stare_firma = 'Activă'
              AND cui IS NOT NULL
              AND TRIM(cui) <> ''
              AND id > %s
            ORDER BY id
            LIMIT %s
            """,
            (int(ultimul_id), int(limita)),
        )

        firme = cur.fetchall()

        cur.close()

        return firme

    finally:
        conn.close()


def get_catalog():
    conn = conecteaza()

    try:
        cur = conn.cursor(dictionary=True)

        cur.execute(
            f"""
            SELECT
                cod_caen,
                denumire_activitate,
                domeniu_principal,
                denumire,
                tip
            FROM {TABELA_CATALOG}
            """
        )

        randuri = cur.fetchall()

        cur.close()

    finally:
        conn.close()

    catalog = defaultdict(
        lambda: {"activitate": None, "domeniu": None, "produse": [], "servicii": []}
    )

    for r in randuri:
        cod = str(r["cod_caen"]).strip().zfill(4)

        intr = catalog[cod]

        if not intr["activitate"]:
            intr["activitate"] = r["denumire_activitate"]

            intr["domeniu"] = r["domeniu_principal"]

        den = (r["denumire"] or "").strip()

        if not den:
            continue

        tip = (r["tip"] or "").strip().lower()

        if tip.startswith("serv"):
            intr["servicii"].append(den)
        else:
            intr["produse"].append(den)

    return catalog


def cui_numeric(cui):
    cifre = re.sub(r"\D", "", str(cui or ""))

    if not cifre:
        return None

    return int(cifre)


def anaf_lot(cuiuri, zi):
    if not cuiuri:
        return {}

    payload = [{"cui": cui, "data": zi} for cui in cuiuri]

    for incercare in range(1, MAX_REINCERCARI + 1):
        try:
            response = ANAF_SESSION.post(
                ANAF_URL, json=payload, timeout=(TIMEOUT_CONNECT, TIMEOUT_READ)
            )

            if response.status_code == 429:
                asteptare = min(30, 2 * incercare)

                log.warning("ANAF 429. Astept %d secunde...", asteptare)

                time.sleep(asteptare)

                continue

            if response.status_code >= 500:
                asteptare = min(20, 2 * incercare)

                log.warning(
                    "ANAF %d. Retry peste %d secunde...",
                    response.status_code,
                    asteptare,
                )

                time.sleep(asteptare)

                continue

            response.raise_for_status()

            continut = response.json()

            rezultat = {}

            for item in continut.get("found", []):
                dg = item.get("date_generale") or {}

                cui = dg.get("cui")

                if cui is None:
                    continue

                rezultat[int(cui)] = {
                    "cod_caen": dg.get("cod_CAEN"),
                    "denumire_anaf": dg.get("denumire"),
                }

            return rezultat

        except (
            requests.Timeout,
            requests.ConnectionError,
            requests.RequestException,
            ValueError,
        ) as e:
            log.warning("ANAF eroare %d/%d: %s", incercare, MAX_REINCERCARI, e)

            if incercare < MAX_REINCERCARI:
                time.sleep(incercare * 2)

    return None


def incarca_corespondenta():
    mapare = dict(CORESPONDENTA_IMPLICITA)

    if not os.path.exists(FISIER_CORESPONDENTA):
        return mapare

    with open(FISIER_CORESPONDENTA, "r", encoding="utf-8-sig", newline="") as f:
        for rand in csv.DictReader(f):
            vechi = (rand.get("cod_vechi") or "").strip().zfill(4)

            nou = (rand.get("cod_nou") or "").strip().zfill(4)

            if vechi and nou and nou != "0000":
                mapare[vechi] = nou

    return mapare


def fara_branduri(text):
    if not text:
        return text

    def repl(match):
        nume = re.findall(r"\b[A-ZĂÂÎȘȚ][\w\-]{2,}", match.group(1))

        if len(nume) >= 2:
            return ""

        return match.group(0)

    text = re.sub(r"\s*\(([^()]*)\)", repl, text)

    return re.sub(r"\s{2,}", " ", text).strip(" ,;")


def dedup(lista):
    vazute = set()
    rezultat = []

    for element in lista:
        element = str(element).strip()

        if not element:
            continue

        cheie = element.lower()

        if cheie in vazute:
            continue

        vazute.add(cheie)

        rezultat.append(element)

    return rezultat


def construieste(firma, cod, catalog, corespondenta):
    if cod:
        cod = str(cod).strip().zfill(4)
    else:
        cod = ""

    cod_folosit = cod

    if cod and cod not in catalog and cod in corespondenta:
        cod_folosit = corespondenta[cod]

    intrare = catalog.get(cod_folosit)

    if not intrare:
        return {
            "id": firma["id"],
            "denumire": firma["denumire"],
            "cui": firma["cui"],
            "cod_caen": cod,
            "cod_caen_folosit": cod_folosit,
            "activitate": None,
            "domeniu": None,
            "produse": [],
            "servicii": [],
        }

    produse = dedup(intrare["produse"])

    servicii = dedup(intrare["servicii"])

    if CURATA_BRANDURI:
        produse = [
            rezultat for rezultat in (fara_branduri(x) for x in produse) if rezultat
        ]

        servicii = [
            rezultat for rezultat in (fara_branduri(x) for x in servicii) if rezultat
        ]

    produse = dedup(produse)

    servicii = dedup(servicii)

    return {
        "id": firma["id"],
        "denumire": firma["denumire"],
        "cui": firma["cui"],
        "cod_caen": cod,
        "cod_caen_folosit": cod_folosit,
        "activitate": intrare["activitate"],
        "domeniu": intrare["domeniu"],
        "produse": produse,
        "servicii": servicii,
    }


def cale_parte(numar):
    return os.path.join(OUTPUT_DIR, f"firme_produse_servicii_part_{numar:05d}.json")


def lista_parti():
    rezultate = []

    if not os.path.exists(OUTPUT_DIR):
        return rezultate

    pattern = re.compile(r"^firme_produse_servicii_part_(\d+)\.json$")

    for nume in os.listdir(OUTPUT_DIR):
        match = pattern.match(nume)

        if not match:
            continue

        rezultate.append((int(match.group(1)), os.path.join(OUTPUT_DIR, nume)))

    rezultate.sort(key=lambda x: x[0])

    return rezultate


def initializeaza_parte(numar):
    cale = cale_parte(numar)

    if not os.path.exists(cale):
        with open(cale, "w", encoding="utf-8") as f:
            f.write("[\n]")

    return cale


def numara_inregistrari_json(cale):
    if not os.path.exists(cale):
        return 0

    pattern = re.compile(rb'"id"\s*:')

    total = 0
    rest = b""

    with open(cale, "rb") as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)

            if not chunk:
                break

            continut = rest + chunk

            total += len(pattern.findall(continut))

            rest = continut[-32:]

    return total


def ultimul_id_din_json(cale):
    if not os.path.exists(cale):
        return 0

    dimensiune = os.path.getsize(cale)

    if dimensiune <= 0:
        return 0

    bloc = min(dimensiune, 8 * 1024 * 1024)

    with open(cale, "rb") as f:
        f.seek(dimensiune - bloc)

        continut = f.read()

    rezultate = re.findall(rb'"id"\s*:\s*(\d+)', continut)

    if not rezultate:
        return 0

    return int(rezultate[-1])


def gaseste_ultima_parte():
    parti = lista_parti()

    if not parti:
        return None

    return parti[-1]


def salveaza_lot_json(numar_parte, rezultate, count_parte):
    if not rezultate:
        return

    cale = initializeaza_parte(numar_parte)

    obiecte = []

    for rec in rezultate:
        obiecte.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))

    continut_nou = (",\n".join(obiecte)).encode("utf-8")

    with open(cale, "r+b") as f:
        f.seek(0, os.SEEK_END)

        pozitie = f.tell()

        ultimul = None

        while pozitie > 0:
            pozitie -= 1

            f.seek(pozitie)

            caracter = f.read(1)

            if caracter not in (b" ", b"\n", b"\r", b"\t"):
                ultimul = caracter
                break

        if ultimul != b"]":
            raise RuntimeError(f"Fisier JSON invalid: {cale}")

        f.seek(pozitie)

        f.truncate()

        if count_parte > 0:
            f.write(b",\n")

        f.write(continut_nou)

        f.write(b"\n]")

        f.flush()

        os.fsync(f.fileno())


def salveaza_state(ultimul_id, numar_parte, count_parte, procesate_total):
    date_state = {
        "ultimul_id": int(ultimul_id),
        "numar_parte": int(numar_parte),
        "count_parte": int(count_parte),
        "procesate_total": int(procesate_total),
    }

    tmp = STATE_FILE + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(date_state, f, ensure_ascii=False)

    for incercare in range(1, 6):
        try:
            os.replace(tmp, STATE_FILE)

            return

        except PermissionError:
            if incercare >= 5:
                log.warning("Nu am putut actualiza checkpoint.json.")

                return

            time.sleep(0.1 * incercare)


def citeste_state():
    if not os.path.exists(STATE_FILE):
        return None

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception:
        return None


def determina_checkpoint_din_fisiere():
    ultima_parte = gaseste_ultima_parte()

    if ultima_parte is None:
        return None

    numar_parte = ultima_parte[0]

    cale = ultima_parte[1]

    log.info("Verific part_%05d...", numar_parte)

    count_parte = numara_inregistrari_json(cale)

    ultimul_id = ultimul_id_din_json(cale)

    if count_parte >= PART_SIZE:
        log.info("part_%05d este complet (%d firme).", numar_parte, count_parte)

        numar_parte += 1
        count_parte = 0

        initializeaza_parte(numar_parte)

    else:
        log.info(
            "Continui part_%05d de la %d/%d firme.", numar_parte, count_parte, PART_SIZE
        )

    state = citeste_state()

    procesate_total = 0

    if state:
        procesate_total = int(state.get("procesate_total", 0))

    salveaza_state(ultimul_id, numar_parte, count_parte, procesate_total)

    return {
        "ultimul_id": ultimul_id,
        "numar_parte": numar_parte,
        "count_parte": count_parte,
        "procesate_total": procesate_total,
    }


def citeste_start_cui():
    if "--start-cui" not in sys.argv:
        return None

    index = sys.argv.index("--start-cui")

    if len(sys.argv) <= index + 1:
        log.error("Lipseste CUI dupa --start-cui.")

        sys.exit(1)

    return re.sub(r"\D", "", sys.argv[index + 1])


def citeste_limita():
    for parametru in ("--limit", "--test"):
        if parametru not in sys.argv:
            continue

        index = sys.argv.index(parametru)

        if len(sys.argv) <= index + 1:
            return 200

        try:
            valoare = int(sys.argv[index + 1])

        except ValueError:
            log.error("Limita trebuie sa fie un numar.")

            sys.exit(1)

        if valoare <= 0:
            return None

        return valoare

    return None


def pornire_noua_dupa_cui(start_cui):
    parti = lista_parti()

    if parti:
        log.error("")
        log.error("Exista deja fisiere part_*.json.")

        log.error("Daca vrei sa continui procesarea, ruleaza FARA --start-cui.")

        log.error("Nu pornesc din nou de la CUI pentru a evita duplicatele.")

        sys.exit(1)

    firma_start = get_firma_dupa_cui(start_cui)

    if not firma_start:
        log.error("Nu am gasit firma activa cu CUI %s.", start_cui)

        sys.exit(1)

    numar_parte = 1
    count_parte = 0
    procesate_total = 0

    initializeaza_parte(numar_parte)

    ultimul_id = int(firma_start["id"]) - 1

    salveaza_state(ultimul_id, numar_parte, count_parte, procesate_total)

    log.info("")
    log.info("START MANUAL")

    log.info("Firma: %s", firma_start["denumire"])

    log.info("CUI: %s", firma_start["cui"])

    log.info("ID start: %s", firma_start["id"])

    return {
        "ultimul_id": ultimul_id,
        "numar_parte": numar_parte,
        "count_parte": count_parte,
        "procesate_total": procesate_total,
    }


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    limita = citeste_limita()
    start_cui = citeste_start_cui()

    log.info("Citesc catalogul...")

    catalog = get_catalog()

    log.info("%d coduri CAEN", len(catalog))

    corespondenta = incarca_corespondenta()

    log.info("%d corespondente CAEN", len(corespondenta))

    if start_cui:
        state = pornire_noua_dupa_cui(start_cui)

    else:
        state = determina_checkpoint_din_fisiere()

        if state is None:
            log.error("")
            log.error("Nu exista fisiere part_*.json.")

            log.error("Pentru prima pornire foloseste:")

            log.error("python PASUL-I-detectare.py --start-cui 8728833")

            return

    ultimul_id = int(state["ultimul_id"])

    numar_parte = int(state["numar_parte"])

    count_parte = int(state["count_parte"])

    procesate_total = int(state["procesate_total"])

    log.info("")
    log.info("CONTINUI DE LA ID > %d", ultimul_id)

    log.info("Partea curenta: %05d", numar_parte)

    log.info("In partea curenta: %d/%d", count_parte, PART_SIZE)

    if limita:
        log.info("Limita aceasta rulare: %d firme", limita)
    else:
        log.info("Fara limita.")

    zi = date.today().isoformat()

    procesate_rulare = 0

    start_timp = time.monotonic()

    while True:
        if limita and procesate_rulare >= limita:
            break

        if count_parte >= PART_SIZE:
            numar_parte += 1
            count_parte = 0

            initializeaza_parte(numar_parte)

            salveaza_state(ultimul_id, numar_parte, count_parte, procesate_total)

            log.info("")
            log.info("========================================")

            log.info("FISIER NOU: part_%05d", numar_parte)

            log.info("========================================")

        de_citit = DB_PAGE_SIZE

        if limita:
            de_citit = min(de_citit, limita - procesate_rulare)

        firme = get_firme_dupa_id(ultimul_id, de_citit)

        if not firme:
            log.info("Nu mai sunt firme de procesat.")

            break

        pozitie = 0

        while pozitie < len(firme):
            if limita and procesate_rulare >= limita:
                break

            if count_parte >= PART_SIZE:
                break

            spatiu_parte = PART_SIZE - count_parte

            marime_lot = min(BATCH_SIZE, len(firme) - pozitie, spatiu_parte)

            if limita:
                marime_lot = min(marime_lot, limita - procesate_rulare)

            if marime_lot <= 0:
                break

            lot = firme[pozitie : pozitie + marime_lot]

            harta_cui = {}

            for firma in lot:
                cui = cui_numeric(firma["cui"])

                if cui:
                    harta_cui[cui] = True

            raspuns_anaf = anaf_lot(list(harta_cui.keys()), zi)

            if raspuns_anaf is None:
                log.error("")
                log.error("ANAF nu raspunde dupa toate reincercarile.")

                log.error("Oprire fara avansarea checkpoint-ului.")

                log.error("Ruleaza din nou scriptul pentru a continua acelasi lot.")

                return

            rezultate_lot = []

            for firma in lot:
                cui = cui_numeric(firma["cui"])

                info_anaf = raspuns_anaf.get(cui, {}) if cui else {}

                cod_caen = info_anaf.get("cod_caen")

                rec = construieste(firma, cod_caen, catalog, corespondenta)

                rezultate_lot.append(rec)

            salveaza_lot_json(numar_parte, rezultate_lot, count_parte)

            count_lot = len(rezultate_lot)

            count_parte += count_lot
            procesate_total += count_lot
            procesate_rulare += count_lot

            ultimul_id = int(lot[-1]["id"])

            pozitie += count_lot

            salveaza_state(ultimul_id, numar_parte, count_parte, procesate_total)

            timp = time.monotonic() - start_timp

            viteza = procesate_rulare / timp if timp > 0 else 0

            log.info(
                "SALVAT +%d | ID %d | rulare %d%s | part_%05d %d/%d | %.1f firme/s",
                count_lot,
                ultimul_id,
                procesate_rulare,
                (f"/{limita}" if limita else ""),
                numar_parte,
                count_parte,
                PART_SIZE,
                viteza,
            )

            if DELAY_ANAF > 0:
                time.sleep(DELAY_ANAF)

        if count_parte >= PART_SIZE:
            log.info("")
            log.info("PART_%05d COMPLET: %d firme", numar_parte, count_parte)

            continue

    durata = time.monotonic() - start_timp

    viteza_finala = procesate_rulare / durata if durata > 0 else 0

    log.info("")
    log.info("========================================")

    log.info("GATA / OPRIT LA LIMITA")

    log.info("Ultimul ID: %d", ultimul_id)

    log.info("Procesate aceasta rulare: %d", procesate_rulare)

    log.info("Partea curenta: %05d", numar_parte)

    log.info("In partea curenta: %d/%d", count_parte, PART_SIZE)

    log.info("Durata: %.1f secunde", durata)

    log.info("Viteza medie: %.1f firme/secunda", viteza_finala)

    log.info("Director rezultate: %s", OUTPUT_DIR)


if __name__ == "__main__":
    main()
