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

# ---------------------------------------------------------------------------
# Configurare
# ---------------------------------------------------------------------------

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
DELAY_ANAF = 1.1
TIMEOUT = 60
MAX_REINCERCARI = 3

OUTPUT_JSON = "firme_produse_servicii.json"
FISIER_CORESPONDENTA = "corespondenta_caen.csv"

CURATA_BRANDURI = True   # scoate "(Kaufland, Lidl, ...)" din descrieri

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("mysql.connector").setLevel(logging.WARNING)
log = logging.getLogger("detectare")


# corespondente cod vechi (Rev.1/Rev.2) -> cod Rev.3
# se pot suprascrie / completa din corespondenta_caen.csv
CORESPONDENTA_IMPLICITA = {
    "4789": "4778", "4799": "4792", "4110": "4100", "4120": "4100",
    "5610": "5611", "2219": "2212", "4719": "4712", "7022": "7020",
    "5144": "4644", "8690": "8699", "6201": "6210", "7310": "7311",
    "4652": "4650", "6810": "6811", "9103": "9122", "5211": "4711",
    "5250": "4779", "7460": "8001", "8009": "8001", "2524": "2222",
    "4511": "4781", "4675": "4685",
}


# ---------------------------------------------------------------------------
# Baza de date (read-only)
# ---------------------------------------------------------------------------

def conecteaza():
    return mysql.connector.connect(**DB_CONFIG)


def get_firme(limita=None):
    conn = conecteaza()
    try:
        cur = conn.cursor(dictionary=True)
        sql = f"""
            SELECT id, denumire, cui
            FROM {TABELA_FIRME}
            WHERE stare_firma = 'Activă'
              AND cui IS NOT NULL AND TRIM(cui) <> ''
            ORDER BY id
        """
        if limita:
            sql += f" LIMIT {int(limita)}"
        cur.execute(sql)
        firme = cur.fetchall()
        cur.close()
        return firme
    finally:
        conn.close()


def get_catalog():
    conn = conecteaza()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(f"""
            SELECT cod_caen, denumire_activitate, domeniu_principal,
                   denumire, tip
            FROM {TABELA_CATALOG}
        """)
        randuri = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    catalog = defaultdict(lambda: {
        "activitate": None, "domeniu": None, "produse": [], "servicii": []
    })
    for r in randuri:
        cod = str(r["cod_caen"]).strip().zfill(4)
        intr = catalog[cod]
        if not intr["activitate"]:
            intr["activitate"] = r["denumire_activitate"]
            intr["domeniu"] = r["domeniu_principal"]
        den = (r["denumire"] or "").strip()
        if not den:
            continue
        if (r["tip"] or "").strip().lower().startswith("serv"):
            intr["servicii"].append(den)
        else:
            intr["produse"].append(den)
    return catalog


# ---------------------------------------------------------------------------
# ANAF
# ---------------------------------------------------------------------------

def cui_numeric(cui):
    cifre = re.sub(r"\D", "", str(cui or ""))
    return int(cifre) if cifre else None


def anaf_lot(cuiuri, zi):
    payload = [{"cui": c, "data": zi} for c in cuiuri]
    for i in range(1, MAX_REINCERCARI + 1):
        try:
            r = requests.post(ANAF_URL, json=payload, timeout=TIMEOUT,
                              headers={"Content-Type": "application/json"})
            if r.status_code == 429:
                time.sleep(10 * i)
                continue
            r.raise_for_status()
            out = {}
            for item in r.json().get("found", []):
                dg = item.get("date_generale") or {}
                c = dg.get("cui")
                if c is not None:
                    out[int(c)] = {
                        "cod_caen": dg.get("cod_CAEN"),
                        "denumire_anaf": dg.get("denumire"),
                    }
            return out
        except requests.RequestException as e:
            log.warning("   ANAF eroare (%d/%d): %s", i, MAX_REINCERCARI, e)
            if i < MAX_REINCERCARI:
                time.sleep(5 * i)
    return {}


# ---------------------------------------------------------------------------
# Utilitare
# ---------------------------------------------------------------------------

def incarca_corespondenta():
    mapare = dict(CORESPONDENTA_IMPLICITA)
    if os.path.exists(FISIER_CORESPONDENTA):
        with open(FISIER_CORESPONDENTA, "r", encoding="utf-8-sig", newline="") as f:
            for rand in csv.DictReader(f):
                v = (rand.get("cod_vechi") or "").strip().zfill(4)
                n = (rand.get("cod_nou") or "").strip().zfill(4)
                if v and n and n != "0000":
                    mapare[v] = n
    return mapare


def fara_branduri(text):
    if not text:
        return text
    def repl(m):
        nume = re.findall(r"\b[A-ZĂÂÎȘȚ][\w\-]{2,}", m.group(1))
        return "" if len(nume) >= 2 else m.group(0)
    text = re.sub(r"\s*\(([^()]*)\)", repl, text)
    return re.sub(r"\s{2,}", " ", text).strip(" ,;")


def dedup(lista):
    vaz, out = set(), []
    for x in lista:
        k = x.lower()
        if k not in vaz:
            vaz.add(k)
            out.append(x)
    return out


def incarca_existente():
    if not os.path.exists(OUTPUT_JSON):
        return {}
    try:
        with open(OUTPUT_JSON, "r", encoding="utf-8") as f:
            return {str(x["id"]): x for x in json.load(f)}
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


def salveaza(rezultate):
    lista = sorted(rezultate.values(), key=lambda x: x["id"])
    tmp = OUTPUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lista, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUTPUT_JSON)


def construieste(firma, cod, catalog, corespondenta):
    """Construieste inregistrarea finala pentru o firma."""
    cod = str(cod).strip().zfill(4) if cod else ""
    cod_folosit = cod
    if cod and cod not in catalog and cod in corespondenta:
        cod_folosit = corespondenta[cod]

    intr = catalog.get(cod_folosit)
    if not intr:
        return {
            "id": firma["id"], "denumire": firma["denumire"],
            "cui": firma["cui"], "cod_caen": cod,
            "activitate": None, "domeniu": None,
            "produse": [], "servicii": [],
        }

    produse = dedup(intr["produse"])
    servicii = dedup(intr["servicii"])
    if CURATA_BRANDURI:
        produse = [p for p in (fara_branduri(x) for x in produse) if p]
        servicii = [s for s in (fara_branduri(x) for x in servicii) if s]

    return {
        "id": firma["id"], "denumire": firma["denumire"],
        "cui": firma["cui"], "cod_caen": cod,
        "activitate": intr["activitate"], "domeniu": intr["domeniu"],
        "produse": produse, "servicii": servicii,
    }


# ---------------------------------------------------------------------------
# Verificare rapida
# ---------------------------------------------------------------------------

def verificare_rapida():
    log.info("=== VERIFICARE RAPIDA ===\n")

    log.info("1. Baza de date...")
    try:
        firme = get_firme(10)
        log.info("   OK - %d firme citite", len(firme))
    except mysql.connector.Error as e:
        log.error("   ESUAT: %s", e)
        return

    log.info("2. Catalog produse/servicii...")
    try:
        catalog = get_catalog()
        log.info("   OK - %d coduri CAEN in catalog", len(catalog))
    except mysql.connector.Error as e:
        log.error("   ESUAT: %s", e)
        return

    log.info("3. ANAF...")
    harta = {cui_numeric(f["cui"]): f for f in firme if cui_numeric(f["cui"])}
    raspuns = anaf_lot(list(harta.keys()), date.today().isoformat())
    if not raspuns:
        log.error("   ESUAT - niciun raspuns. Verifica versiunea din URL.")
        return
    log.info("   OK - %d/%d firme returnate", len(raspuns), len(harta))

    log.info("\n4. Rezultat:\n")
    corespondenta = incarca_corespondenta()
    gasite = 0
    for cui, firma in harta.items():
        cod = raspuns.get(cui, {}).get("cod_caen")
        rec = construieste(firma, cod, catalog, corespondenta)
        are = bool(rec["produse"] or rec["servicii"])
        gasite += are
        log.info("   %s (CAEN %s)", rec["denumire"][:50], rec["cod_caen"] or "-")
        if are:
            log.info("      %s", rec["activitate"])
            log.info("      %d produse, %d servicii",
                     len(rec["produse"]), len(rec["servicii"]))
            if rec["produse"]:
                log.info("      ex: %s", rec["produse"][0][:90])
        else:
            log.info("      (fara potrivire in catalog)")

    log.info("\n   %d/%d cu produse/servicii", gasite, len(harta))
    log.info("\nTotul functioneaza. Ruleaza fara --check pentru procesare completa.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if "--check" in sys.argv:
        verificare_rapida()
        return

    limita = None
    if "--test" in sys.argv:
        i = sys.argv.index("--test")
        limita = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 200

    log.info("Citesc catalogul...")
    catalog = get_catalog()
    log.info("   %d coduri CAEN", len(catalog))

    corespondenta = incarca_corespondenta()
    log.info("   %d corespondente cod vechi -> Rev.3", len(corespondenta))

    log.info("Citesc firmele...")
    firme = get_firme(limita)
    log.info("   %d firme active", len(firme))

    rezultate = incarca_existente()
    if rezultate:
        log.info("   %d deja procesate", len(rezultate))

    ramase = [f for f in firme if str(f["id"]) not in rezultate]
    if not ramase:
        log.info("Nimic de facut.")
        return

    loturi = (len(ramase) + BATCH_SIZE - 1) // BATCH_SIZE
    log.info("   %d de procesat, %d loturi (~%.0f min)\n",
             len(ramase), loturi, loturi * DELAY_ANAF / 60)

    zi = date.today().isoformat()
    cu_date = 0

    for n in range(loturi):
        lot = ramase[n * BATCH_SIZE:(n + 1) * BATCH_SIZE]
        harta = {}
        for f in lot:
            c = cui_numeric(f["cui"])
            if c:
                harta[c] = f
        if not harta:
            continue

        raspuns = anaf_lot(list(harta.keys()), zi)

        for cui, firma in harta.items():
            cod = raspuns.get(cui, {}).get("cod_caen")
            rec = construieste(firma, cod, catalog, corespondenta)
            if rec["produse"] or rec["servicii"]:
                cu_date += 1
            rezultate[str(firma["id"])] = rec

        salveaza(rezultate)
        log.info("Lot %d/%d | cu produse/servicii: %d | total: %d",
                 n + 1, loturi, cu_date, len(rezultate))
        time.sleep(DELAY_ANAF)

    procesate = len(ramase)
    log.info("\n--- GATA ---")
    log.info("Procesate: %d | cu produse/servicii: %d (%.1f%%)",
             procesate, cu_date, cu_date * 100 / procesate if procesate else 0)
    log.info("Salvat in %s", OUTPUT_JSON)


if __name__ == "__main__":
    main()