import os
import re
import sys
import json
import time
import argparse
import logging
from datetime import datetime

import mysql.connector


DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "charset": "utf8mb4",
    "collation": "utf8mb4_unicode_ci",
    "autocommit": False,
    "connection_timeout": 20,
}

BAZA_DATE = os.environ.get("DB_NAME", "zivro_admin")
TABELA = "firme_active_produse_servicii"
TABELA_COMPLETA = "`" + BAZA_DATE + "`.`" + TABELA + "`"

DIRECTOR = "firme_produse_servicii_parts"


FISIERE = ["firme_produse_servicii_200K.json"] + [
    "firme_produse_servicii_part_%05d.json" % i for i in range(1, 17)
]

SEPARATOR = " | "

DIM_BATCH = 500
DIM_CHUNK = 4 * 1024 * 1024

FISIER_STARE = os.path.join(DIRECTOR, "import_checkpoint.json")

MAX_DENUMIRE = 500
MAX_DOMENIU = 255
MAX_CAEN = 8
MAX_CUI = 20

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("import")


DDL = """
CREATE TABLE IF NOT EXISTS {tabela} (
  `firma_id`         BIGINT UNSIGNED NOT NULL,
  `denumire`         VARCHAR({max_denumire}) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cui`              VARCHAR({max_cui}) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_caen`         VARCHAR({max_caen}) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_caen_folosit` VARCHAR({max_caen}) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `activitate`       TEXT COLLATE utf8mb4_unicode_ci,
  `domeniu`          VARCHAR({max_domeniu}) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `produse`          MEDIUMTEXT COLLATE utf8mb4_unicode_ci,
  `servicii`         MEDIUMTEXT COLLATE utf8mb4_unicode_ci,
  `numar_produse`    SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  `numar_servicii`   SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  `sursa`            VARCHAR(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `created_at`       TIMESTAMP NULL DEFAULT NULL,
  `updated_at`       TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`firma_id`),
  KEY `fps_cui` (`cui`),
  KEY `fps_cod_caen` (`cod_caen`),
  KEY `fps_cod_caen_folosit` (`cod_caen_folosit`),
  KEY `fps_sursa` (`sursa`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
""".format(
    tabela=TABELA_COMPLETA,
    max_denumire=MAX_DENUMIRE,
    max_cui=MAX_CUI,
    max_caen=MAX_CAEN,
    max_domeniu=MAX_DOMENIU,
)

COLOANE = [
    "firma_id",
    "denumire",
    "cui",
    "cod_caen",
    "cod_caen_folosit",
    "activitate",
    "domeniu",
    "produse",
    "servicii",
    "numar_produse",
    "numar_servicii",
    "sursa",
    "created_at",
    "updated_at",
]

ACTUALIZEAZA = [c for c in COLOANE if c not in ("firma_id", "created_at")]


class FisierIncomplet(Exception):
    pass


def citeste_obiecte(cale, dim_chunk=DIM_CHUNK):
    """Parcurge un array JSON de la disc, obiect cu obiect.

    Tine in memorie doar bucata curenta: citeste din fisier numai cand
    ce a ramas in buffer nu mai contine un obiect complet.
    """
    decoder = json.JSONDecoder()
    scan = decoder.raw_decode
    albe = " \t\r\n,"

    buf = ""
    poz = 0
    deschis = False
    epuizat = False

    with open(cale, "r", encoding="utf-8") as f:
        while True:
            if not epuizat:
                bucata = f.read(dim_chunk)

                if bucata:
                    buf = buf[poz:] + bucata
                    poz = 0
                else:
                    epuizat = True

            while True:
                while poz < len(buf) and buf[poz] in albe:
                    poz += 1

                if not deschis:
                    if poz >= len(buf):
                        break

                    if buf[poz] != "[":
                        raise FisierIncomplet("fisierul nu incepe cu '['")

                    deschis = True
                    poz += 1

                    continue

                if poz >= len(buf):
                    break

                if buf[poz] == "]":
                    return

                try:
                    obiect, poz = scan(buf, poz)
                except ValueError:
                    break

                yield obiect

            if epuizat:
                if not deschis:
                    raise FisierIncomplet("fisierul nu incepe cu '['")

                if poz >= len(buf):
                    raise FisierIncomplet("fisierul se termina fara ']'")

                raise FisierIncomplet(
                    "ultimul obiect este trunchiat (fisierul nu s-a scris complet)"
                )

RE_SPATII = re.compile(r"\s+")


def text(valoare, maxim=None):
    if valoare is None:
        return None

    v = RE_SPATII.sub(" ", str(valoare)).strip()

    if not v:
        return None

    if maxim and len(v) > maxim:
        v = v[:maxim]

    return v


def normalizeaza_cui(valoare):
    if valoare is None:
        return None

    v = re.sub(r"[^0-9A-Z]", "", str(valoare).upper())
    v = re.sub(r"^RO", "", v)

    return v[:MAX_CUI] or None


def normalizeaza_caen(valoare):
    if valoare is None:
        return None

    v = re.sub(r"\D", "", str(valoare))

    if not v:
        return None

    return v.zfill(4)[:MAX_CAEN]


def enumerare(lista):
    """Transforma lista JSON intr-o singura casuta: elemente unice, separate prin ' | '."""
    if not lista:
        return None, 0

    if isinstance(lista, str):
        lista = [lista]

    vazute = set()
    elemente = []

    for element in lista:
        v = text(element)

        if not v:
            continue

        v = RE_SPATII.sub(" ", v.replace("|", "/")).strip(" ;,")

        if not v:
            continue

        cheie = v.casefold()

        if cheie in vazute:
            continue

        vazute.add(cheie)

        elemente.append(v)

    if not elemente:
        return None, 0

    return SEPARATOR.join(elemente), len(elemente)


def construieste_rand(obiect, sursa, acum):
    firma_id = obiect.get("id")

    if firma_id is None:
        return None, "lipseste id"

    try:
        firma_id = int(firma_id)
    except (TypeError, ValueError):
        return None, "id invalid: %r" % (firma_id,)

    produse, nr_produse = enumerare(obiect.get("produse"))
    servicii, nr_servicii = enumerare(obiect.get("servicii"))

    cod_caen = normalizeaza_caen(obiect.get("cod_caen"))

    rand = (
        firma_id,
        text(obiect.get("denumire"), MAX_DENUMIRE),
        normalizeaza_cui(obiect.get("cui")),
        cod_caen,
        normalizeaza_caen(obiect.get("cod_caen_folosit")) or cod_caen,
        text(obiect.get("activitate")),
        text(obiect.get("domeniu"), MAX_DOMENIU),
        produse,
        servicii,
        min(nr_produse, 65535),
        min(nr_servicii, 65535),
        sursa,
        acum,
        acum,
    )

    return rand, None


def conecteaza():
    return mysql.connector.connect(**DB_CONFIG)


def pregateste_tabela(conn, recreeaza):
    cur = conn.cursor()

    cur.execute(
        "CREATE DATABASE IF NOT EXISTS `%s` "
        "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci" % BAZA_DATE
    )

    if recreeaza:
        log.info("Sterg tabela %s (--recreeaza)", TABELA_COMPLETA)

        cur.execute("DROP TABLE IF EXISTS %s" % TABELA_COMPLETA)

    cur.execute(DDL)

    conn.commit()

    cur.close()


def construieste_sql(numar_randuri):
    coloane = ", ".join("`%s`" % c for c in COLOANE)

    placeholder = "(" + ", ".join(["%s"] * len(COLOANE)) + ")"

    valori = ", ".join([placeholder] * numar_randuri)

    actualizari = ", ".join("`%s` = VALUES(`%s`)" % (c, c) for c in ACTUALIZEAZA)

    return (
        "INSERT INTO %s (%s) VALUES %s ON DUPLICATE KEY UPDATE %s"
        % (TABELA_COMPLETA, coloane, valori, actualizari)
    )


def scrie_batch(cur, randuri):
    if not randuri:
        return

    sql = construieste_sql(len(randuri))

    parametri = [v for rand in randuri for v in rand]

    cur.execute(sql, parametri)


def citeste_stare():
    if not os.path.exists(FISIER_STARE):
        return {"fisiere_gata": []}

    try:
        with open(FISIER_STARE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"fisiere_gata": []}


def scrie_stare(stare):
    with open(FISIER_STARE, "w", encoding="utf-8") as f:
        json.dump(stare, f, ensure_ascii=False, indent=2)


def proceseaza_fisier(cale, sursa, cur, conn, doar_verifica):
    acum = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    raport = {
        "fisier": sursa,
        "obiecte": 0,
        "scrise": 0,
        "erori": 0,
        "fara_produse_si_servicii": 0,
        "id_min": None,
        "id_max": None,
        "iduri": set(),
        "duplicate_in_fisier": 0,
        "probleme": [],
        "incomplet": None,
        "durata": 0.0,
    }

    randuri = []
    inceput = time.time()

    try:
        for obiect in citeste_obiecte(cale):
            raport["obiecte"] += 1

            if not isinstance(obiect, dict):
                raport["erori"] += 1

                if len(raport["probleme"]) < 5:
                    raport["probleme"].append("element care nu este obiect JSON")

                continue

            rand, eroare = construieste_rand(obiect, sursa, acum)

            if eroare:
                raport["erori"] += 1

                if len(raport["probleme"]) < 5:
                    raport["probleme"].append(eroare)

                continue

            firma_id = rand[0]

            if firma_id in raport["iduri"]:
                raport["duplicate_in_fisier"] += 1
            else:
                raport["iduri"].add(firma_id)

            if raport["id_min"] is None or firma_id < raport["id_min"]:
                raport["id_min"] = firma_id

            if raport["id_max"] is None or firma_id > raport["id_max"]:
                raport["id_max"] = firma_id

            if rand[9] == 0 and rand[10] == 0:
                raport["fara_produse_si_servicii"] += 1

            if doar_verifica:
                continue

            randuri.append(rand)

            if len(randuri) >= DIM_BATCH:
                scrie_batch(cur, randuri)

                conn.commit()

                raport["scrise"] += len(randuri)

                randuri = []

                if raport["scrise"] % 100000 == 0:
                    log.info(
                        "    ... %s randuri scrise (%.0fs)",
                        format(raport["scrise"], ","),
                        time.time() - inceput,
                    )

    except FisierIncomplet as e:
        raport["incomplet"] = str(e)

    if randuri and not doar_verifica:
        scrie_batch(cur, randuri)

        conn.commit()

        raport["scrise"] += len(randuri)

    raport["durata"] = time.time() - inceput

    return raport


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--verifica", action="store_true",
                        help="doar verifica fisierele, nu scrie nimic in baza de date")
    parser.add_argument("--recreeaza", action="store_true",
                        help="sterge tabela si o creeaza de la zero")
    parser.add_argument("--fisier", action="append",
                        help="proceseaza doar fisierul indicat (se poate repeta)")
    parser.add_argument("--reia", action="store_true",
                        help="sare peste fisierele deja importate complet")

    args = parser.parse_args()

    baza = os.path.dirname(os.path.abspath(__file__))

    fisiere = args.fisier or FISIERE

    stare = citeste_stare()

    gata = set(stare.get("fisiere_gata", [])) if args.reia else set()

    de_procesat = []

    for nume in fisiere:
        cale = os.path.join(baza, DIRECTOR, nume)

        if not os.path.exists(cale):
            log.error("LIPSESTE: %s", nume)

            continue

        de_procesat.append((cale, nume))

    if not de_procesat:
        log.error("Niciun fisier de procesat.")

        return 1

    conn = None
    cur = None

    if not args.verifica:
        conn = conecteaza()

        pregateste_tabela(conn, args.recreeaza)

        cur = conn.cursor()

        cur.execute("SET SESSION unique_checks = 0")
        cur.execute("SET SESSION foreign_key_checks = 0")

        log.info("Conectat la %s:%s -> %s",
                 DB_CONFIG["host"], DB_CONFIG["port"], TABELA_COMPLETA)
    else:
        log.info("Mod verificare: nu se scrie nimic in baza de date.")

    log.info("")

    total_obiecte = 0
    total_scrise = 0
    total_erori = 0
    total_goale = 0
    toate_idurile = set()
    suprapuneri = 0
    incomplete = []

    inceput_total = time.time()

    for cale, nume in de_procesat:
        if nume in gata:
            log.info("%-45s SARIT (deja importat)", nume)

            continue

        marime = os.path.getsize(cale) / (1024.0 * 1024.0)

        log.info("%-45s %7.1f MB", nume, marime)

        raport = proceseaza_fisier(cale, nume, cur, conn, args.verifica)

        iduri = raport.pop("iduri")

        comune = len(toate_idurile & iduri)

        suprapuneri += comune

        toate_idurile |= iduri

        total_obiecte += raport["obiecte"]
        total_scrise += raport["scrise"]
        total_erori += raport["erori"]
        total_goale += raport["fara_produse_si_servicii"]

        log.info(
            "    %-9s obiecte=%-10s scrise=%-10s erori=%-4s id %s..%s  "
            "dubluri_in_fisier=%s  suprapuneri=%s  fara_date=%s  %.0fs",
            "INCOMPLET" if raport["incomplet"] else "OK",
            format(raport["obiecte"], ","),
            format(raport["scrise"], ","),
            raport["erori"],
            raport["id_min"],
            raport["id_max"],
            raport["duplicate_in_fisier"],
            comune,
            raport["fara_produse_si_servicii"],
            raport["durata"],
        )

        if raport["incomplet"]:
            incomplete.append(nume)

            log.warning("    ATENTIE: %s", raport["incomplet"])

        if raport["probleme"]:
            log.warning("    probleme: %s", "; ".join(raport["probleme"]))

        if not args.verifica:
            gata_lista = stare.setdefault("fisiere_gata", [])

            if nume not in gata_lista and not raport["incomplet"]:
                gata_lista.append(nume)

            stare["actualizat"] = datetime.now().isoformat(timespec="seconds")

            scrie_stare(stare)

    log.info("")
    log.info("=" * 80)
    log.info("obiecte citite        : %s", format(total_obiecte, ","))
    log.info("firme unice (dupa id) : %s", format(len(toate_idurile), ","))
    log.info("suprapuneri intre fis. : %s (acelasi id in mai multe fisiere, s-a suprascris)",
             format(suprapuneri, ","))
    log.info("fara produse/servicii : %s", format(total_goale, ","))
    log.info("erori                 : %s", total_erori)

    if incomplete:
        log.warning("fisiere incomplete    : %s", ", ".join(incomplete))

    if not args.verifica:
        log.info("randuri scrise        : %s", format(total_scrise, ","))

        cur.execute(
            "SELECT COUNT(*), SUM(numar_produse > 0), SUM(numar_servicii > 0), "
            "MAX(numar_produse), MAX(numar_servicii) FROM %s" % TABELA_COMPLETA
        )

        nr, cu_p, cu_s, max_p, max_s = cur.fetchone()

        log.info("")
        log.info("In tabela %s:", TABELA_COMPLETA)
        log.info("  randuri             : %s", format(nr, ","))
        log.info("  cu produse          : %s (max %s pe firma)", format(int(cu_p or 0), ","), max_p)
        log.info("  cu servicii         : %s (max %s pe firma)", format(int(cu_s or 0), ","), max_s)

        conn.commit()

        cur.close()

        conn.close()

    log.info("")
    log.info("Durata: %.1f minute", (time.time() - inceput_total) / 60.0)

    return 0


if __name__ == "__main__":
    sys.exit(main())
