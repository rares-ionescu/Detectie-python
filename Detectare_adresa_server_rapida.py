import mysql.connector
import requests
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
import urllib.parse
import argparse
import json
import re
import sys
import time
import random
import os
import signal
import threading
import unicodedata
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from queue import Queue, Empty

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

FISIER_IESIRE = "rezultate_finale.json"

SALVEAZA_IN_DB = False

NUM_WORKERS = 5

API_HOST = os.environ.get("CONTACT_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("CONTACT_PORT", "8078"))
API_KEY = os.environ.get("CONTACT_API_KEY", "").strip()

API_LUCRATORI = int(os.environ.get("CONTACT_LUCRATORI", "1"))

API_JOBURI_PASTRATE = int(os.environ.get("CONTACT_JOBURI_PASTRATE", "200"))

REPORNIRE_DUPA_ANALIZA = os.environ.get("CONTACT_REPORNIRE", "0").strip().lower() in ("1", "true", "da")

REPORNIRE_INTARZIERE = float(os.environ.get("CONTACT_REPORNIRE_INTARZIERE", "1.0"))

COD_REPORNIRE = 7

CHROME_VERSIUNE = int(os.environ.get("CHROME_VERSIUNE", "152"))

CHROME_INVIZIBIL = os.environ.get("CHROME_INVIZIBIL", "1").strip().lower() not in ("0", "false", "nu")

CHROME_HEADLESS = os.environ.get("CHROME_HEADLESS", "0").strip().lower() in ("1", "true", "da")

DIRECTOARE_EXCLUSE = [
    "listafirme.ro", "termene.ro", "harta-firmelor.ro", "sicap.ai",
    "totalfirme.ro", "romanian-companies.eu", "lege5.ro", "bizoo.ro",
    "firme.info", "confidas.ro", "risco.ro", "infocui.ro", "topfirme.com",
    "doingbusiness.ro", "facebook.com", "linkedin.com", "instagram.com",
    "youtube.com", "openapi.ro", "e-licitatie.ro", "seap.ro", "romanian-companies.ro"
]

for _flux in (sys.stdout, sys.stderr):
    try:
        _flux.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

lock_print = threading.Lock()
lock_fisier = threading.Lock()
lock_creare_driver = threading.Lock()

opreste_event = threading.Event()
ruleaza_event = threading.Event()
ruleaza_event.set()

def log(msg):
    with lock_print:
        try:
            print(msg)
        except Exception:
            try:
                print(str(msg).encode("ascii", "replace").decode("ascii"))
            except Exception:
                pass


def gestioneaza_semnal_oprire(signum, frame):
    if not opreste_event.is_set():
        log("\n>>> Semnal de oprire primit (Ctrl+C). Workerii termina firma curenta si se opresc...")
        opreste_event.set()
        ruleaza_event.set()
    else:
        log(">>> Oprire deja in curs, asteptati inchiderea browserelor...")


def inregistreaza_semnale():
    signal.signal(signal.SIGINT, gestioneaza_semnal_oprire)
    signal.signal(signal.SIGTERM, gestioneaza_semnal_oprire)


def thread_comenzi():
    log("\n>>> INFO: Tasteaza 'p' (Pauza), 'r' (Reluare) sau 'q' (Oprire) + ENTER. <<<\n")
    while not opreste_event.is_set():
        try:
            cmd = input().strip().lower()
            if cmd == 'p':
                if ruleaza_event.is_set():
                    ruleaza_event.clear()
                    log("\n>>> PAUZA: Workerii se vor opri temporar dupa ce termina firma curenta. Tasteaza 'r' + ENTER pentru reluare.\n")
            elif cmd == 'r':
                if not ruleaza_event.is_set():
                    ruleaza_event.set()
                    log("\n>>> RELUARE: Workerii isi reiau activitatea.\n")
            elif cmd == 'q':
                gestioneaza_semnal_oprire(None, None)
                break
        except EOFError:
            break


def spune(raport, mesaj, pas=None, progres=None):
    log("    " + mesaj)

    if raport:
        raport(mesaj, pas=pas, progres=progres)


def rezumat_gasite(adresa, telefon, email):
    gasite = [
        eticheta for eticheta, valoare in
        (("adresă", adresa), ("telefon", telefon), ("email", email))
        if valoare
    ]

    return ", ".join(gasite) if gasite else "nimic"


def tip_eroare(e):
    return type(e).__name__


def curata_valoare(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip(" |-:\n\t,.;")


def fara_diacritice(text):
    descompus = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in descompus if not unicodedata.combining(c))


JUDETE = (
    "alba", "arad", "arges", "bacau", "bihor", "bistrita-nasaud", "bistrita", "botosani",
    "braila", "brasov", "bucuresti", "buzau", "calarasi", "caras-severin", "cluj",
    "constanta", "covasna", "dambovita", "dolj", "galati", "giurgiu", "gorj", "harghita",
    "hunedoara", "ialomita", "iasi", "ilfov", "maramures", "mehedinti", "mures", "neamt",
    "olt", "prahova", "salaj", "satu mare", "sibiu", "suceava", "teleorman", "timis",
    "tulcea", "valcea", "vaslui", "vrancea",
)

RE_LITERA = re.compile(r"[a-zA-ZăâîșțĂÂÎȘȚ]")

CEDILE = str.maketrans({"ş": "ș", "Ş": "Ș", "ţ": "ț", "Ţ": "Ț"})

RE_STRADA = re.compile(
    r"(?i)\b(str|strada|b-?dul|bd|bulevardul|calea|sos|soseaua|aleea|piata|splaiul|"
    r"drumul|intrarea|prelungirea|cartierul|cartier|cart)\b\.?"
)

RE_LOCALITATE = re.compile(
    r"(?i)\b(mun|municipiul|oras|orasul|com|comuna|sat|satul|sector|jud|judetul|localitatea)\b\.?"
)

RE_NUMAR_ADRESA = re.compile(r"(?i)\b(nr|numarul|bl|sc|ap|et)\b\.?\s*\d+")

RE_COD_POSTAL = re.compile(r"\b\d{6}\b")

RE_GUNOI_ADRESA = re.compile(
    r"(?i)(cookie|javascript|https?://|www\.|@|politica de confiden|termeni|"
    r"drepturi rezervate|program(?:ul)? de lucru|luni\s*[-–]\s*vineri|vezi harta|"
    r"cifra de afaceri|cod caen|nr\. reg\. com|newsletter|abonare)"
)


def are_judet(marunt):
    return any(re.search(r"\b%s\b" % re.escape(judet), marunt) for judet in JUDETE)


def pare_adresa(text):
    if not text:
        return False

    if len(text) < 10 or len(text) > 200:
        return False

    if len(RE_LITERA.findall(text)) < 5:
        return False

    if len(text.split()) > 30:
        return False

    if RE_GUNOI_ADRESA.search(text):
        return False

    marunt = fara_diacritice(text).lower()

    if RE_STRADA.search(marunt) or RE_LOCALITATE.search(marunt) or RE_COD_POSTAL.search(marunt):
        return True

    if are_judet(marunt):
        return True

    return bool(re.search(r"\d", text) and text.count(",") >= 1 and len(text.split()) >= 4)


def calitate_adresa(text):
    if not text:
        return 0

    marunt = fara_diacritice(text).lower()
    scor = 0

    if RE_STRADA.search(marunt):
        scor += 8

    if RE_NUMAR_ADRESA.search(marunt) or re.search(r"\b\d{1,4}[a-z]?\b", marunt):
        scor += 5

    if RE_LOCALITATE.search(marunt):
        scor += 5

    if are_judet(marunt):
        scor += 4

    if RE_COD_POSTAL.search(marunt):
        scor += 3

    if text.count(",") >= 2:
        scor += 2

    if len(text) < 18:
        scor -= 6

    if len(text) > 150:
        scor -= 4

    return max(0, min(25, scor))


def curata_adresa(text):
    if not text:
        return ""

    text = curata_valoare(text)
    text = re.sub(
        r"(?i)^\s*(adres[aă](\s+(complet[aă]|sediu(lui)?|social[aă]|fiscal[aă]))?|"
        r"sediu(l)?(\s+(social|central))?|loca[țt]ie|punct de lucru)\s*[:\-–]?\s*",
        "", text,
    )
    text = re.split(
        r"(?i)\b(telefon|tel\.|tel|fax|mobil|e-?mail|web|site|program|orar|deschis|"
        r"închis|inchis|cod caen|cui|vezi|hart[aă])\b",
        text,
    )[0]
    text = re.split(r"[|•·–]", text)[0]
    text = curata_valoare(text)
    text = text.translate(CEDILE)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"(?i)\bjud\s+", "jud. ", text)
    text = re.sub(r"(?i)\bnr\s+(\d)", r"nr. \1", text)

    if text and not re.search(r"[a-zăâîșț]", text):
        text = text.title()
        text = re.sub(
            r"(?i)\b(Nr|Bl|Sc|Ap|Et|Jud|Mun|Com|Str|Bd|Sos|Cod)\b",
            lambda potrivire: potrivire.group(1).lower(),
            text,
        )
        text = text[0].upper() + text[1:]

    return text if pare_adresa(text) else ""


RE_TELEFON_VALID = re.compile(
    r"^(?:"
    r"07\d{8}"
    r"|021\d{7}|031\d{7}"
    r"|02[3-6]\d{7}"
    r"|03[3-7]\d{7}"
    r"|0800\d{6}|0900\d{6}"
    r")$"
)

RE_CANDIDAT_TELEFON = re.compile(r"(?<![\d/.])(?:\+40|0040|0)[\d .\-]{8,13}(?![\d])")

RE_ETICHETA_TELEFON = re.compile(
    r"(?i)(?:telefon|tel\.?|mobil|phone|contact|fix)\s*:?\s*"
    r"((?:\+40|0040|0)[\d .\-]{8,13})"
)

RE_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

DOMENII_EMAIL_IGNORATE = (
    "example.com", "example.org", "domain.com", "email.com", "yourdomain",
    "sentry.io", "wixpress.com", "wix.com", "squarespace.com", "shopify.com",
    "godaddy.com", "sentry-next.wixpress.com", "schema.org", "w3.org",
    "metricbiz.ro", "mfinante.gov.ro", "anaf.ro", "demoanaf.ro",
    "listafirme.eu", "firme.ro", "cuifirma.ro", "datefirme.ro", "targetare.ro",
    "dashiro.ro", "infobel.com", "northdata.com", "northdata.de", "dnb.com",
) + tuple(DIRECTOARE_EXCLUSE)

UTILIZATORI_EMAIL_IGNORATI = (
    "your", "youremail", "nume", "exemplu", "example", "email", "adresa",
    "user", "username", "test", "noreply", "no-reply", "donotreply",
)

EXTENSII_IN_EMAIL = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".css", ".js")


def normalizeaza_telefon(brut):
    if not brut:
        return None

    brut = brut.strip()

    if len(re.findall(r"[ .\-]", brut)) > 4:
        return None

    cifre = re.sub(r"\D", "", brut)

    if cifre.startswith("0040"):
        cifre = "0" + cifre[4:]
    elif cifre.startswith("40") and len(cifre) == 11:
        cifre = "0" + cifre[2:]

    if len(cifre) != 10:
        return None

    if len(set(cifre[1:])) == 1 or len(set(cifre[-6:])) == 1:
        return None

    return cifre if RE_TELEFON_VALID.match(cifre) else None


def telefoane_din_text(text, cui=""):
    if not text:
        return []

    gasite = []
    vazute = set()

    def adauga(brut, etichetat):
        numar = normalizeaza_telefon(brut)

        if not numar or numar in vazute or (cui and cui in numar):
            return

        vazute.add(numar)
        gasite.append((numar, etichetat))

    for potrivire in RE_ETICHETA_TELEFON.finditer(text):
        adauga(potrivire.group(1), True)

    for potrivire in RE_CANDIDAT_TELEFON.finditer(text):
        adauga(potrivire.group(0), False)

    return gasite


def email_valid(adresa):
    if not adresa or len(adresa) > 150:
        return False

    adresa = adresa.lower()

    if adresa.endswith(EXTENSII_IN_EMAIL):
        return False

    utilizator, _, domeniu = adresa.partition("@")

    if utilizator in UTILIZATORI_EMAIL_IGNORATI:
        return False

    return not any(d in domeniu for d in DOMENII_EMAIL_IGNORATE)


def emailuri_din_text(text):
    if not text:
        return []

    gasite = []
    vazute = set()

    for potrivire in RE_EMAIL.finditer(text):
        adresa = potrivire.group(0).lower().strip(".")

        if adresa in vazute or not email_valid(adresa):
            continue

        vazute.add(adresa)
        gasite.append(adresa)

    return gasite


def linkuri_de_contact(driver):
    telefoane, emailuri = [], []

    try:
        href_uri = driver.execute_script(
            "return Array.from(document.querySelectorAll('a[href^=\"tel:\"], a[href^=\"mailto:\"]'))"
            ".map(a => a.getAttribute('href')).slice(0, 40);"
        ) or []
    except Exception:
        return [], []

    for href in href_uri:
        href = urllib.parse.unquote(str(href or "")).strip()

        if href.lower().startswith("tel:"):
            numar = normalizeaza_telefon(href[4:])
            if numar and numar not in telefoane:
                telefoane.append(numar)
        elif href.lower().startswith("mailto:"):
            adresa = href[7:].split("?")[0].strip().lower()
            if email_valid(adresa) and adresa not in emailuri:
                emailuri.append(adresa)

    return telefoane, emailuri


def conecteaza_db():
    return mysql.connector.connect(
        host="127.0.0.1",
        port=13307,
        user="root",
        password=os.getenv("DB_PASSWORD", "9cec884a3ec863f7"),
        database="zivro_admin",
        charset="utf8mb4",
        collation="utf8mb4_general_ci"
    )


def preia_firme_din_db(conexiune):
    cursor = conexiune.cursor(dictionary=True)
    interogare = """
        SELECT f.id, f.denumire, f.cui, fa.id as id_adresa
        FROM firme f
        LEFT JOIN firme_adrese fa ON f.id = fa.firma_id
        WHERE f.stare_firma = 'Activă'
          AND (fa.adresa_completa_ai IS NULL OR TRIM(fa.adresa_completa_ai) = '')
    """
    cursor.execute(interogare)
    firme = cursor.fetchall()
    cursor.close()
    return firme


def firma_este_eligibila(conexiune, firma_id):
    cursor = conexiune.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT f.stare_firma, fa.adresa_completa_ai
        FROM firme f
        LEFT JOIN firme_adrese fa ON f.id = fa.firma_id
        WHERE f.id = %s
        LIMIT 1
        """,
        (firma_id,)
    )
    rand = cursor.fetchone()
    cursor.close()

    if not rand:
        return False, "firma nu mai exista in baza de date"

    if rand["stare_firma"] != "Activă":
        return False, f"firma nu mai e activa (stare: {rand['stare_firma']})"

    adresa_curenta = rand["adresa_completa_ai"]
    if adresa_curenta is not None and str(adresa_curenta).strip() != "":
        return False, "adresa_completa_ai a fost deja completata intre timp"

    return True, ""


def actualizeaza_db(conexiune, firma_id, id_adresa, adresa, telefon, email):
    cursor = conexiune.cursor()
    adresa_salvata = adresa if adresa else "nedetectat"
    telefon_salvat = telefon if telefon else "nedetectat"
    email_salvat = email if email else "nedetectat"

    if id_adresa:
        cursor.execute(
            "UPDATE firme_adrese SET adresa_completa_ai = %s, telefon_ai = %s, adresa_mail_ai = %s, data_colectare_ai = NOW() WHERE id = %s",
            (adresa_salvata, telefon_salvat, email_salvat, id_adresa)
        )
    else:
        cursor.execute(
            "INSERT INTO firme_adrese (firma_id, adresa_completa_ai, telefon_ai, adresa_mail_ai, data_colectare_ai) VALUES (%s, %s, %s, %s, NOW())",
            (firma_id, adresa_salvata, telefon_salvat, email_salvat)
        )

    conexiune.commit()
    cursor.close()


def ascunde_din_bara(driver):
    if os.name != "nt" or not CHROME_INVIZIBIL or CHROME_HEADLESS:
        return

    pid_browser = getattr(driver, "browser_pid", None)

    if not pid_browser:
        return

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)

        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW = 0x00040000
        SW_HIDE, SW_SHOWNOACTIVATE = 0, 4

        ia_stil = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        pune_stil = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)

        ferestre = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def aduna(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            if pid.value == pid_browser and user32.IsWindowVisible(hwnd):
                ferestre.append(hwnd)

            return True

        for _ in range(20):
            del ferestre[:]
            user32.EnumWindows(aduna, 0)

            if ferestre:
                break

            time.sleep(0.25)

        for hwnd in ferestre:
            stil = ia_stil(hwnd, GWL_EXSTYLE)
            user32.ShowWindow(hwnd, SW_HIDE)
            pune_stil(hwnd, GWL_EXSTYLE, (stil | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)

        return len(ferestre)
    except Exception as e:
        log("    (nu am putut scoate fereastra Chrome din bara: %s)" % tip_eroare(e))


def creeaza_driver():
    options = uc.ChromeOptions()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-popup-blocking")
    options.page_load_strategy = "eager"

    if CHROME_INVIZIBIL and not CHROME_HEADLESS:
        options.add_argument("--window-position=-32000,-32000")

    if os.name != "nt":
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

    with lock_creare_driver:
        driver = uc.Chrome(
            options=options,
            version_main=CHROME_VERSIUNE,
            headless=CHROME_HEADLESS,
        )

    ascunde_din_bara(driver)

    driver.set_page_load_timeout(20)
    return driver


def asteapta_body(driver, timeout=4):
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
        )
    except TimeoutException:
        pass


SCOR_LINK_SITE = 100
SCOR_ANAF = 98
SCOR_ANAF_SEDIU = 94
SCOR_STRUCTURAT_SITE = 92
SCOR_ETICHETA_SITE = 85
SCOR_FISA_GOOGLE = 75
SCOR_ANAF_CONTACT = 70
SCOR_LINK_CATALOG = 65
SCOR_STRUCTURAT_CATALOG = 60
SCOR_ETICHETA_CATALOG = 55
SCOR_TEXT_SITE = 40
SCOR_TEXT_CATALOG = 25

BONUS_CONSENS = 8
BONUS_CONSENS_MAXIM = 24
BONUS_LOCALITATE = 6
PRAG_ASEMANARE = 0.5
PRAG_CONTINERE = 0.8

CUVINTE_GENERICE = set(JUDETE) | {
    "sector", "romania", "cladirea", "corp", "spatiul", "parter", "birou", "camera",
}

PRAG_ADRESA_SIGURA = 90
PRAG_CONTACT_SIGUR = 85

GAZDE_TEHNICE = (
    "google.", "gstatic.com", "googleusercontent.com", "googleapis.com",
    "youtube.com", "schema.org", "w3.org", "blogger.com", "policies.google",
    "support.google", "accounts.google", "maps.google", "translate.google",
    "bing.com", "microsoft.com", "msn.com", "duckduckgo.com", "yahoo.com",
    "ecosia.org", "startpage.com", "wikipedia.org", "wikimedia.org",
)

GAZDE_SOCIALE = (
    "facebook.com", "linkedin.com", "instagram.com", "twitter.com", "x.com",
    "tiktok.com", "pinterest.com", "olx.ro", "publi24.ro", "anuntul.ro",
)

PAGINI_CONTACT = ("contact", "contacte", "date-de-contact", "despre-noi", "despre", "about")

CAI_CONTACT = ("/contact", "/contact.html", "/contact/")

MARCAJE_BLOCARE = (
    "just a moment", "verifying you are human", "attention required",
    "access denied", "enable javascript", "captcha", "unusual traffic",
    "nu a fost gasita nicio firma", "pagina nu a fost gasita", "404 not found",
)

URLURI_ANAF = (
    "https://webservicesp.anaf.ro/api/PlatitorTvaRest/v9/tva",
    "https://webservicesp.anaf.ro/PlatitorTvaRest/api/v8/ws/tva",
    "https://webservicesp.anaf.ro/PlatitorTvaRest/api/v7/ws/tva",
)

CATALOAGE_DIRECTE = (
    ("harta-firmelor.ro", "https://harta-firmelor.ro/firma/{cui}"),
    ("demoanaf.ro", "https://demoanaf.ro/verificare-cui/{cui}"),
    ("listafirme.ro", "https://www.listafirme.ro/cauta/?q={cui}"),
    ("infocui.ro", "https://infocui.ro/cauta?q={cui}"),
)

MAX_CATALOAGE_GASITE = int(os.environ.get("CONTACT_MAX_CATALOAGE", "4"))

MAX_SITEURI = int(os.environ.get("CONTACT_MAX_SITEURI", "2"))

OPRIRE_RAPIDA = os.environ.get("CONTACT_OPRIRE_RAPIDA", "0").strip().lower() in ("1", "true", "da")

RE_ADRESA_ETICHETA = re.compile(
    r"(?i)(?:adres[aă](?:\s+(?:complet[aă]|sediu(?:lui)?|social[aă]|fiscal[aă]))?|"
    r"sediu(?:l)?(?:\s+(?:social|central))?|punct de lucru)\s*[:\-–]\s*([^\n]{10,160})"
)

RE_ADRESA_LIBERA = re.compile(
    r"(?i)((?:str\.|strada|b-?dul|bd\.|bulevardul|calea|[șs]os\.|[șs]oseaua|aleea|"
    r"pia[țt]a|splaiul|drumul|intrarea)\s+[^\n,;|]{2,50}"
    r"(?:\s*,\s*(?:nr\.?|num[ăa]rul)?\s*\d[\w\-/]{0,6})?"
    r"(?:\s*,\s*[^\n;|]{2,60}){0,2})"
)


def jetoane_nume(denumire):
    nume = re.sub(
        r"\b(S\.?R\.?L\.?|S\.?A\.?|P\.?F\.?A\.?|I\.?I\.?|I\.?F\.?)\b\.?",
        "", str(denumire or ""), flags=re.I,
    )
    nume = fara_diacritice(nume).lower()

    return [j for j in re.split(r"[^a-z0-9]+", nume) if len(j) >= 3]


def gazda_din_url(url):
    potrivire = re.match(r"https?://([^/]+)", str(url or ""), re.I)

    if not potrivire:
        return ""

    gazda = potrivire.group(1).lower()

    return gazda[4:] if gazda.startswith("www.") else gazda


def desfa_redirect(href):
    try:
        analiza = urllib.parse.urlparse(str(href or ""))

        if "duckduckgo.com" in analiza.netloc or analiza.path.startswith("/url"):
            parametri = urllib.parse.parse_qs(analiza.query)

            for cheie in ("uddg", "url", "u", "q"):
                valoare = (parametri.get(cheie) or [""])[0]

                if valoare.startswith("http"):
                    return valoare
    except Exception:
        pass

    return href


def aceeasi_adresa(unele, altele):
    if not unele or not altele:
        return False

    comune = unele & altele

    if len(comune) / float(len(unele | altele)) >= PRAG_ASEMANARE:
        return True

    if len(comune) / float(min(len(unele), len(altele))) < PRAG_CONTINERE:
        return False

    return any(len(jeton) >= 4 and jeton not in CUVINTE_GENERICE for jeton in comune)


def cheie_candidat(camp, valoare):
    if camp != "adresa":
        return str(valoare or "").strip().lower()

    marunt = fara_diacritice(valoare).lower()
    marunt = re.sub(
        r"\b(judetul|jud|municipiul|mun|orasul|oras|comuna|com|satul|sat|strada|str|"
        r"bulevardul|b-dul|bdul|bd|calea|soseaua|sos|aleea|numarul|nr|bloc|bl|scara|sc|"
        r"etaj|et|apartament|ap|romania|ro)\b\.?",
        " ", marunt,
    )
    marunt = re.sub(r"[^a-z0-9]+", " ", marunt)

    return " ".join(sorted(set(marunt.split())))


class Cautare:

    def __init__(self, nume, cui):
        self.nume = str(nume or "").strip()
        self.cui = re.sub(r"\D", "", str(cui or ""))
        self.jetoane = jetoane_nume(self.nume)
        self.campuri = {}
        self.linkuri = []
        self.gazde_vizitate = set()
        self.site = ""
        self.reper = ""

    def adauga(self, camp, valoare, sursa, scor):
        valoare = str(valoare or "").strip()

        if not valoare:
            return

        self.campuri.setdefault(camp, []).append(
            {"valoare": valoare, "sursa": sursa, "scor": scor}
        )

    def adauga_linkuri(self, hrefuri):
        for href in hrefuri or []:
            href = desfa_redirect(str(href or "").strip())

            if href.startswith("http") and href not in self.linkuri:
                self.linkuri.append(href)

    def scor_grup(self, camp, grup):
        surse = {c["sursa"] for c in grup}
        scor = max(c["scor"] for c in grup)
        scor += min(BONUS_CONSENS_MAXIM, BONUS_CONSENS * (len(surse) - 1))

        if camp == "adresa":
            valoare = max(grup, key=lambda c: calitate_adresa(c["valoare"]))["valoare"]
            scor += calitate_adresa(valoare)

            if self.reper and self.reper in fara_diacritice(valoare).lower():
                scor += BONUS_LOCALITATE

        return scor

    def grupuri(self, camp):
        grupate = []

        for candidat in self.campuri.get(camp) or []:
            cheie = cheie_candidat(camp, candidat["valoare"])
            jetoane = set(cheie.split())

            for grup in grupate:
                if camp == "adresa":
                    potrivit = aceeasi_adresa(jetoane, grup["jetoane"])
                else:
                    potrivit = cheie == grup["cheie"]

                if potrivit:
                    grup["candidati"].append(candidat)
                    break
            else:
                grupate.append({"cheie": cheie, "jetoane": jetoane, "candidati": [candidat]})

        return [grup["candidati"] for grup in grupate]

    def cel_mai_bun(self, camp):
        grupuri = self.grupuri(camp)

        if not grupuri:
            return "", ""

        grup = max(grupuri, key=lambda g: self.scor_grup(camp, g))

        if camp == "adresa":
            ales = max(grup, key=lambda c: (calitate_adresa(c["valoare"]), len(c["valoare"])))
        else:
            ales = max(grup, key=lambda c: c["scor"])

        return ales["valoare"], ales["sursa"]

    def scor_maxim(self, camp):
        grupuri = self.grupuri(camp)

        if not grupuri:
            return 0

        return max(self.scor_grup(camp, grup) for grup in grupuri)

    def alternative(self, camp, ales):
        vazute = set()
        altele = []

        for candidat in sorted(self.campuri.get(camp) or [], key=lambda c: -c["scor"]):
            valoare = candidat["valoare"]

            if valoare == ales or valoare.lower() in vazute:
                continue

            vazute.add(valoare.lower())
            altele.append("%s (%s)" % (valoare, candidat["sursa"]))

        return altele

    def rezumat(self):
        parti = []

        for camp in ("adresa", "telefon", "email"):
            numar = len(self.grupuri(camp))

            if numar:
                parti.append("%s (%d %s)" % (camp, numar, "variantă" if numar == 1 else "variante"))

        return ", ".join(parti) if parti else "nimic"

    def destul(self):
        return (
            self.scor_maxim("adresa") >= PRAG_ADRESA_SIGURA
            and self.scor_maxim("telefon") >= PRAG_CONTACT_SIGUR
            and self.scor_maxim("email") >= PRAG_CONTACT_SIGUR
        )


def text_pagina(driver):
    try:
        return driver.execute_script("return document.body.innerText;") or ""
    except Exception:
        return ""


def url_curent(driver):
    try:
        return driver.current_url or ""
    except Exception:
        return ""


def linkuri_pagina(driver, limita=150):
    try:
        return driver.execute_script(
            "return Array.from(document.querySelectorAll('a[href]'))"
            ".map(a => a.href).filter(h => h && h.indexOf('http') === 0).slice(0, %d);" % limita
        ) or []
    except Exception:
        return []


def texte_selector(driver, selector, limita=6):
    try:
        elemente = driver.find_elements(By.CSS_SELECTOR, selector)[:limita]
        return [e.text.strip() for e in elemente if e.text and e.text.strip()]
    except Exception:
        return []


def deschide(driver, url, timeout=5, pauza=(0.3, 0.6)):
    driver.get(url)
    asteapta_body(driver, timeout=timeout)
    time.sleep(random.uniform(*pauza))

    return text_pagina(driver)


def pagina_inutila(text):
    marunt = fara_diacritice(text or "").lower()

    if len(marunt.strip()) < 150:
        return True

    return any(marcaj in marunt[:3000] for marcaj in MARCAJE_BLOCARE)


def pagina_despre_firma(cautare, text):
    if not text:
        return False

    if cautare.cui and cautare.cui in re.sub(r"\D", "", text[:30000]):
        return True

    marunt = fara_diacritice(text[:30000]).lower()
    jetoane = cautare.jetoane[:3]

    return bool(jetoane) and all(jeton in marunt for jeton in jetoane)


def adresa_din_postala(nod):
    parti = [
        str(nod.get("streetAddress") or "").strip(),
        str(nod.get("postalCode") or "").strip(),
        str(nod.get("addressLocality") or "").strip(),
        str(nod.get("addressRegion") or "").strip(),
    ]

    return ", ".join(parte for parte in parti if parte)


def parcurge_structurat(nod, adrese, telefoane, emailuri, adancime=0):
    if adancime > 6:
        return

    if isinstance(nod, list):
        for element in nod[:40]:
            parcurge_structurat(element, adrese, telefoane, emailuri, adancime + 1)

        return

    if not isinstance(nod, dict):
        return

    adresa = nod.get("address")

    if isinstance(adresa, str):
        adrese.append(adresa)
    elif isinstance(adresa, dict):
        adrese.append(adresa_din_postala(adresa))
    elif isinstance(adresa, list):
        for element in adresa:
            if isinstance(element, str):
                adrese.append(element)
            elif isinstance(element, dict):
                adrese.append(adresa_din_postala(element))

    for cheie, cos in (("telephone", telefoane), ("email", emailuri)):
        valoare = nod.get(cheie)

        if isinstance(valoare, str):
            cos.append(valoare)
        elif isinstance(valoare, list):
            cos.extend(str(v) for v in valoare if isinstance(v, str))

    for valoare in list(nod.values())[:40]:
        if isinstance(valoare, (list, dict)):
            parcurge_structurat(valoare, adrese, telefoane, emailuri, adancime + 1)


def date_structurate(driver):
    adrese, telefoane, emailuri = [], [], []

    try:
        blocuri = driver.execute_script(
            "return Array.from(document.querySelectorAll('script[type$=\"ld+json\"]'))"
            ".map(s => s.textContent).slice(0, 12);"
        ) or []
    except Exception:
        blocuri = []

    for bloc in blocuri:
        try:
            parcurge_structurat(json.loads(bloc), adrese, telefoane, emailuri)
        except Exception:
            continue

    try:
        adrese.extend(driver.execute_script(
            "return Array.from(document.querySelectorAll('address, [itemprop=\"address\"]'))"
            ".map(a => a.innerText).slice(0, 5);"
        ) or [])
    except Exception:
        pass

    return adrese, telefoane, emailuri


def aduna_din_pagina(cautare, driver, text, sursa, pe_site_firmei):
    scor_link = SCOR_LINK_SITE if pe_site_firmei else SCOR_LINK_CATALOG
    scor_structurat = SCOR_STRUCTURAT_SITE if pe_site_firmei else SCOR_STRUCTURAT_CATALOG
    scor_eticheta = SCOR_ETICHETA_SITE if pe_site_firmei else SCOR_ETICHETA_CATALOG
    scor_text = SCOR_TEXT_SITE if pe_site_firmei else SCOR_TEXT_CATALOG

    telefoane_link, emailuri_link = linkuri_de_contact(driver)

    for numar in telefoane_link:
        cautare.adauga("telefon", numar, sursa, scor_link)

    for adresa in emailuri_link:
        cautare.adauga("email", adresa, sursa, scor_link)

    adrese, telefoane, emailuri = date_structurate(driver)

    for adresa in adrese:
        cautare.adauga("adresa", curata_adresa(adresa), sursa, scor_structurat)

    for numar in telefoane:
        cautare.adauga("telefon", normalizeaza_telefon(numar), sursa, scor_structurat)

    for adresa in emailuri:
        adresa = str(adresa or "").strip().lower()

        if email_valid(adresa):
            cautare.adauga("email", adresa, sursa, scor_structurat)

    for potrivire in list(RE_ADRESA_ETICHETA.finditer(text or ""))[:5]:
        cautare.adauga("adresa", curata_adresa(potrivire.group(1)), sursa, scor_eticheta)

    for potrivire in list(RE_ADRESA_LIBERA.finditer(text or ""))[:4]:
        cautare.adauga("adresa", curata_adresa(potrivire.group(1)), sursa, scor_text)

    for numar, etichetat in telefoane_din_text(text, cautare.cui):
        cautare.adauga("telefon", numar, sursa, scor_eticheta if etichetat else scor_text)

    for adresa in emailuri_din_text(text):
        cautare.adauga("email", adresa, sursa, scor_text)

    cautare.adauga_linkuri(linkuri_pagina(driver))


def esenta_localitate(text):
    marunt = RE_LOCALITATE.sub(" ", fara_diacritice(text).lower())
    marunt = re.sub(r"[^a-z0-9 -]+", " ", marunt)

    return re.sub(r"\s+", " ", marunt).strip()


def adresa_din_bloc(bloc, prefix):
    if not isinstance(bloc, dict):
        return ""

    def camp(nume):
        return curata_valoare(str(bloc.get(prefix + nume) or ""))

    strada = camp("denumire_Strada")
    numar = camp("numar_Strada")
    detalii = camp("detalii_Adresa")
    localitate = camp("denumire_Localitate")
    judet = camp("denumire_Judet")
    cod = camp("cod_Postal")

    parti = []

    if strada:
        parti.append(strada if RE_STRADA.match(fara_diacritice(strada)) else "Str. " + strada)

    if numar:
        parti.append(numar if re.search(r"(?i)\bnr\b", numar) else "nr. " + numar)

    if detalii:
        parti.append(detalii)

    if localitate:
        parti.append(localitate)

    if judet and esenta_localitate(judet) not in esenta_localitate(localitate):
        parti.append("jud. " + judet)

    if cod and len(re.sub(r"\D", "", cod)) == 6:
        parti.append(cod)

    return ", ".join(parti)


def localitate_de_referinta(*blocuri):
    for bloc, prefix in blocuri:
        localitate = esenta_localitate(str((bloc or {}).get(prefix + "denumire_Localitate") or ""))
        jetoane = [j for j in localitate.split() if len(j) >= 4 and not j.isdigit()]

        if jetoane:
            return max(jetoane, key=len)

    return ""


def interogheaza_anaf(cui):
    if not cui:
        return None

    corp = [{"cui": int(cui), "data": datetime.now().strftime("%Y-%m-%d")}]
    antete = {"Content-Type": "application/json", "User-Agent": USER_AGENT}

    for url in URLURI_ANAF:
        try:
            raspuns = requests.post(url, json=corp, headers=antete, timeout=15)

            if raspuns.status_code != 200:
                continue

            gasite = (raspuns.json() or {}).get("found") or []

            if gasite:
                return gasite[0]
        except Exception:
            continue

    return None


def sursa_anaf(driver, cautare, raport=None):
    spune(raport, "Interoghez registrul ANAF pentru CUI %s" % (cautare.cui or "—"), pas="anaf", progres=10)

    fisa = interogheaza_anaf(cautare.cui)

    if not fisa:
        spune(raport, "Registrul ANAF nu are date pentru acest CUI.")
        return

    generale = fisa.get("date_generale") if isinstance(fisa.get("date_generale"), dict) else {}
    fiscal = fisa.get("adresa_domiciliu_fiscal") if isinstance(fisa.get("adresa_domiciliu_fiscal"), dict) else {}
    sediu = fisa.get("adresa_sediu_social") if isinstance(fisa.get("adresa_sediu_social"), dict) else {}

    for bloc, prefix, scor in ((fiscal, "d", SCOR_ANAF), (sediu, "s", SCOR_ANAF_SEDIU)):
        cautare.adauga("adresa", curata_adresa(adresa_din_bloc(bloc, prefix)), "registrul ANAF", scor)

    cautare.adauga(
        "adresa",
        curata_adresa(str(generale.get("adresa") or fisa.get("adresa") or "")),
        "registrul ANAF",
        SCOR_ANAF,
    )

    if cautare.campuri.get("adresa"):
        cautare.reper = localitate_de_referinta((fiscal, "d"), (sediu, "s"))

    numar = normalizeaza_telefon(str(generale.get("telefon") or fisa.get("telefon") or ""))

    if numar:
        cautare.adauga("telefon", numar, "registrul ANAF", SCOR_ANAF_CONTACT)

    spune(raport, "Din registrul ANAF: " + cautare.rezumat())


def sursa_google(driver, cautare, raport=None):
    spune(raport, "Caut pe Google: %s %s" % (cautare.nume, cautare.cui), pas="google", progres=20)

    interogare = urllib.parse.quote("%s %s" % (cautare.nume, cautare.cui))
    text = deschide(driver, "https://www.google.com/search?q=" + interogare)

    for valoare in texte_selector(driver, 'a[data-dtype="d3ph"]'):
        cautare.adauga("telefon", normalizeaza_telefon(valoare), "fișa Google", SCOR_FISA_GOOGLE)

    for valoare in texte_selector(driver, "span.LrzXr"):
        numar = normalizeaza_telefon(valoare)

        if numar:
            cautare.adauga("telefon", numar, "fișa Google", SCOR_FISA_GOOGLE)
        else:
            cautare.adauga("adresa", curata_adresa(valoare), "fișa Google", SCOR_FISA_GOOGLE)

    potrivire = re.search(r"Num[aă]r de telefon:[\s\n]*([^\n]+)", text, re.IGNORECASE)

    if potrivire:
        cautare.adauga("telefon", normalizeaza_telefon(potrivire.group(1)), "fișa Google", SCOR_FISA_GOOGLE)

    aduna_din_pagina(cautare, driver, text, "Google", pe_site_firmei=False)
    spune(raport, "Din fișa Google: " + cautare.rezumat())


def sursa_google_contact(driver, cautare, raport=None):
    spune(raport, "Caut pe Google datele de contact", pas="google-contact", progres=55)

    interogare = urllib.parse.quote(
        '"%s" %s adresă sediu telefon email contact' % (cautare.nume, cautare.cui)
    )
    text = deschide(driver, "https://www.google.com/search?q=" + interogare)

    aduna_din_pagina(cautare, driver, text, "căutare Google contact", pe_site_firmei=False)
    spune(raport, "Din căutarea de contact: " + cautare.rezumat())


def sursa_bing(driver, cautare, raport=None):
    spune(raport, "Caut pe Bing", pas="bing", progres=62)

    interogare = urllib.parse.quote(
        '"%s" %s contact adresă telefon' % (cautare.nume, cautare.cui)
    )
    text = deschide(driver, "https://www.bing.com/search?setlang=ro&cc=RO&q=" + interogare)

    aduna_din_pagina(cautare, driver, text, "Bing", pe_site_firmei=False)
    spune(raport, "Din Bing: " + cautare.rezumat())


def sursa_duckduckgo(driver, cautare, raport=None):
    spune(raport, "Caut pe DuckDuckGo", pas="duckduckgo", progres=68)

    interogare = urllib.parse.quote(
        '"%s" %s contact adresă telefon' % (cautare.nume, cautare.cui)
    )
    text = deschide(driver, "https://html.duckduckgo.com/html/?kl=ro-ro&q=" + interogare)

    aduna_din_pagina(cautare, driver, text, "DuckDuckGo", pe_site_firmei=False)
    spune(raport, "Din DuckDuckGo: " + cautare.rezumat())


def scor_gazda(gazda, jetoane):
    if not gazda or any(g in gazda for g in GAZDE_TEHNICE):
        return -1

    if any(d in gazda for d in DIRECTOARE_EXCLUSE):
        return -1

    curata = re.sub(r"[^a-z0-9]", "", gazda.split(".")[0])
    scor = 0

    if jetoane and any(jeton in curata for jeton in jetoane):
        scor += 10

    if jetoane and curata and any(curata in jeton for jeton in jetoane):
        scor += 3

    if gazda.endswith(".ro"):
        scor += 2

    if gazda.count(".") <= 1:
        scor += 1

    return scor


def siteuri_candidate(cautare, limita=MAX_SITEURI):
    vazute = set(cautare.gazde_vizitate)
    gasite = []

    for pozitie, href in enumerate(cautare.linkuri):
        gazda = gazda_din_url(href)

        if not gazda or gazda in vazute:
            continue

        scor = scor_gazda(gazda, cautare.jetoane)

        if scor < 1:
            continue

        vazute.add(gazda)
        gasite.append({"url": href, "gazda": gazda, "scor": scor, "pozitie": pozitie})

    gasite.sort(key=lambda site: (-site["scor"], site["pozitie"]))

    return gasite[:limita]


def urmeaza_paginile_de_contact(driver, cautare, eticheta, sigur, raport=None, limita=2):
    baza = url_curent(driver)
    gazda_baza = gazda_din_url(baza)

    try:
        hrefuri = driver.execute_script(
            "const c = %s; const out = [];"
            "for (const a of document.querySelectorAll('a[href]')) {"
            "  const t = ((a.textContent || '') + ' ' + a.getAttribute('href')).toLowerCase();"
            "  if (c.some(x => t.includes(x)) && out.indexOf(a.href) === -1) out.push(a.href);"
            "  if (out.length >= 6) break;"
            "}"
            "return out;" % json.dumps(list(PAGINI_CONTACT))
        ) or []
    except Exception:
        hrefuri = []

    if not hrefuri and baza:
        hrefuri = [urllib.parse.urljoin(baza, cale) for cale in CAI_CONTACT]

    deschise = 0
    vazute = set()

    for href in hrefuri:
        href = str(href or "")

        if not href.startswith("http") or href in vazute:
            continue

        if gazda_din_url(href) != gazda_baza:
            continue

        vazute.add(href)

        try:
            spune(raport, "Deschid pagina de contact: %s" % href)
            text = deschide(driver, href)

            if pagina_inutila(text):
                continue

            aduna_din_pagina(cautare, driver, text, eticheta, pe_site_firmei=sigur)
            spune(raport, "După pagina de contact: " + cautare.rezumat())
            deschise += 1
        except Exception as e:
            spune(raport, "Pagina de contact nu a putut fi citită (%s)." % tip_eroare(e))

        if deschise >= limita:
            break


def sursa_site_firmei(driver, cautare, raport=None):
    siteuri = siteuri_candidate(cautare)

    if not siteuri:
        spune(raport, "Nu am găsit un site propriu al firmei în rezultate.", pas="site", progres=40)
        return

    for pozitie, site in enumerate(siteuri):
        if site["gazda"] in cautare.gazde_vizitate:
            continue

        cautare.gazde_vizitate.add(site["gazda"])
        sigur = site["scor"] >= 10
        eticheta = "site-ul firmei" if sigur else "site-ul " + site["gazda"]

        try:
            spune(
                raport,
                "Deschid %s: %s" % (eticheta, site["url"]),
                pas="site",
                progres=40 + pozitie * 5,
            )
            text = deschide(driver, site["url"])

            if pagina_inutila(text):
                spune(raport, "Pagina nu a putut fi citită.")
                continue

            if sigur and not cautare.site:
                cautare.site = site["url"]

            aduna_din_pagina(cautare, driver, text, eticheta, pe_site_firmei=sigur)
            spune(raport, "După prima pagină a site-ului: " + cautare.rezumat())

            urmeaza_paginile_de_contact(driver, cautare, eticheta, sigur, raport)
        except Exception as e:
            spune(raport, "Site-ul nu a putut fi citit (%s)." % tip_eroare(e))


def sursa_cataloage_directe(driver, cautare, raport=None):
    if not cautare.cui:
        return

    for pozitie, (eticheta, sablon) in enumerate(CATALOAGE_DIRECTE):
        if eticheta in cautare.gazde_vizitate:
            continue

        cautare.gazde_vizitate.add(eticheta)

        try:
            spune(
                raport,
                "Încerc %s pentru CUI %s" % (eticheta, cautare.cui),
                pas="cataloage",
                progres=72 + pozitie * 3,
            )
            text = deschide(driver, sablon.format(cui=cautare.cui), timeout=6, pauza=(0.4, 0.8))

            if pagina_inutila(text) or not pagina_despre_firma(cautare, text):
                spune(raport, "%s nu are fișa firmei (sau cere verificare anti-bot)." % eticheta)
                continue

            aduna_din_pagina(cautare, driver, text, eticheta, pe_site_firmei=False)
            spune(raport, "Din %s: %s" % (eticheta, cautare.rezumat()))
        except Exception as e:
            spune(raport, "%s nu a răspuns (%s)." % (eticheta, tip_eroare(e)))


def cataloage_de_urmat(cautare, limita=MAX_CATALOAGE_GASITE):
    vazute = set(cautare.gazde_vizitate)
    alese = []

    for href in cautare.linkuri:
        gazda = gazda_din_url(href)

        if not gazda or gazda in vazute:
            continue

        if any(g in gazda for g in GAZDE_TEHNICE) or any(g in gazda for g in GAZDE_SOCIALE):
            continue

        este_catalog = any(d in gazda for d in DIRECTOARE_EXCLUSE)
        are_cui = bool(cautare.cui) and cautare.cui in re.sub(r"\D", "", href)

        if not (este_catalog or are_cui):
            continue

        vazute.add(gazda)
        alese.append({"url": href, "gazda": gazda})

        if len(alese) >= limita:
            break

    return alese


def sursa_cataloage_gasite(driver, cautare, raport=None):
    pagini = cataloage_de_urmat(cautare)

    if not pagini:
        spune(raport, "Nicio pagină nouă de deschis din rezultate.", pas="pagini", progres=88)
        return

    spune(
        raport,
        "Mai deschid %d pagini găsite în căutări: %s"
        % (len(pagini), ", ".join(pagina["gazda"] for pagina in pagini)),
        pas="pagini",
        progres=88,
    )

    for pagina in pagini:
        cautare.gazde_vizitate.add(pagina["gazda"])

        try:
            text = deschide(driver, pagina["url"], timeout=6)

            if pagina_inutila(text) or not pagina_despre_firma(cautare, text):
                spune(raport, "%s nu confirmă firma — o sar." % pagina["gazda"])
                continue

            aduna_din_pagina(cautare, driver, text, pagina["gazda"], pe_site_firmei=False)
            spune(raport, "Din %s: %s" % (pagina["gazda"], cautare.rezumat()))
        except Exception as e:
            spune(raport, "%s nu a răspuns (%s)." % (pagina["gazda"], tip_eroare(e)))


SURSE = (
    ("registrul ANAF", sursa_anaf, False),
    ("Google", sursa_google, False),
    ("site-ul firmei", sursa_site_firmei, False),
    ("căutare Google contact", sursa_google_contact, True),
    ("Bing", sursa_bing, True),
    ("DuckDuckGo", sursa_duckduckgo, True),
    ("cataloage de firme", sursa_cataloage_directe, True),
    ("pagini găsite în căutări", sursa_cataloage_gasite, True),
)


def rezultat_din_cautare(cautare, raport=None):
    adresa, sursa_adresa = cautare.cel_mai_bun("adresa")
    telefon, sursa_telefon = cautare.cel_mai_bun("telefon")
    email, sursa_email = cautare.cel_mai_bun("email")

    provenienta = {}

    for camp, sursa in (("adresa", sursa_adresa), ("telefon", sursa_telefon), ("email", sursa_email)):
        if sursa:
            provenienta[camp] = sursa

    alternative = {}

    for camp, ales in (("adresa", adresa), ("telefon", telefon), ("email", email)):
        altele = cautare.alternative(camp, ales)

        if altele:
            alternative[camp] = altele[:5]
            spune(raport, "Alte variante pentru %s: %s" % (camp, "; ".join(alternative[camp])))

    spune(
        raport,
        "Gata. Am ales: " + rezumat_gasite(adresa, telefon, email),
        pas="gata",
        progres=100,
    )

    return {
        "adresa": adresa or "",
        "telefon": telefon or "",
        "email": email or "",
        "site": cautare.site or "",
        "provenienta": provenienta,
        "alternative": alternative,
    }


def analizeaza_cu_driver(driver, nume, cui, raport=None):
    cautare = Cautare(nume, cui)

    for eticheta, sursa, optionala in SURSE:
        if optionala and OPRIRE_RAPIDA and cautare.destul():
            spune(raport, "Am deja date sigure pe toate câmpurile — sar peste %s." % eticheta)
            continue

        try:
            sursa(driver, cautare, raport)
        except Exception as e:
            spune(raport, "Sursa „%s” a eșuat (%s), continui cu următoarea." % (eticheta, tip_eroare(e)))

    return rezultat_din_cautare(cautare, raport)


def analizeaza_o_firma(nume, cui, raport=None):
    spune(raport, "Pornesc browserul pentru %s (CUI %s)." % (nume, cui), pas="pornire", progres=5)

    driver = creeaza_driver()

    try:
        return analizeaza_cu_driver(driver, nume, cui, raport)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def scrie_rezultat_json(rezultat):
    with lock_fisier:
        rez_curat = {k: v for k, v in rezultat.items() if not k.startswith("_")}
        json_str = json.dumps(rez_curat, indent=4, ensure_ascii=False)
        json_str = "    " + json_str.replace("\n", "\n    ")

        if not os.path.exists(FISIER_IESIRE) or os.path.getsize(FISIER_IESIRE) == 0:
            with open(FISIER_IESIRE, "w", encoding="utf-8") as f:
                f.write("[\n" + json_str + "\n]")
        else:
            with open(FISIER_IESIRE, "rb+") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                seek_len = min(size, 50)
                f.seek(-seek_len, os.SEEK_END)
                tail = f.read()
                idx = tail.rfind(b']')
                if idx != -1:
                    f.seek(-seek_len + idx, os.SEEK_END)
                    f.truncate()
                    f.write(b",\n" + json_str.encode("utf-8") + b"\n]")


def consolideaza_json_final(rezultate):
    with lock_fisier:
        with open(FISIER_IESIRE, "w", encoding="utf-8") as f:
            json.dump(rezultate, f, indent=4, ensure_ascii=False)


def worker(id_worker, coada_intrare, coada_iesire, total):
    try:
        driver = creeaza_driver()
    except Exception as e:
        log(f"[W{id_worker}] Nu am putut porni Chrome: {e}")
        return

    try:
        while not opreste_event.is_set():
            ruleaza_event.wait() 
            if opreste_event.is_set():
                break

            try:
                index, firma = coada_intrare.get_nowait()
            except Empty:
                break

            try:
                nume = str(firma["denumire"]).strip()
                cui = str(firma["cui"]).strip()

                log(f"[W{id_worker}] [{index}/{total}] {cui} - {nume}")

                gasite = analizeaza_cu_driver(driver, nume, cui)

                rezultat = {
                    "nume_firma": nume,
                    "cui": cui,
                    "adresa_completa": gasite["adresa"] or "nedetectat",
                    "telefon": gasite["telefon"] or "nedetectat",
                    "email": gasite["email"] or "nedetectat",
                    "_firma_id": firma["id"],
                    "_id_adresa": firma["id_adresa"],
                }
                coada_iesire.put(rezultat)

            except Exception as e:
                log(f"[W{id_worker}] Eroare la procesarea firmei {firma.get('cui')}: {e}")
            finally:
                coada_intrare.task_done()

        if opreste_event.is_set():
            log(f"[W{id_worker}] Oprire solicitata - inchid browserul.")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def thread_scriere(coada_iesire, total, stop_event):
    conexiune_db = conecteaza_db() if SALVEAZA_IN_DB else None
    rezultate_complete = []
    procesate = 0
    sarite = 0

    try:
        while not (stop_event.is_set() and coada_iesire.empty()):
            try:
                rezultat = coada_iesire.get(timeout=0.5)
            except Empty:
                continue

            scrie_rezultat_json(rezultat)

            rezultate_complete.append(rezultat)
            procesate += 1

            if SALVEAZA_IN_DB:
                eligibila, motiv = firma_este_eligibila(conexiune_db, rezultat["_firma_id"])
                if eligibila:
                    actualizeaza_db(
                        conexiune_db,
                        rezultat["_firma_id"],
                        rezultat["_id_adresa"],
                        None if rezultat["adresa_completa"] == "nedetectat" else rezultat["adresa_completa"],
                        None if rezultat["telefon"] == "nedetectat" else rezultat["telefon"],
                        None if rezultat["email"] == "nedetectat" else rezultat["email"],
                    )
                else:
                    sarite += 1
                    log(f"    -> Sarit la scriere in DB ({rezultat['cui']}): {motiv}")

            if procesate % 10 == 0 or procesate == total:
                log(f">>> Progres: {procesate}/{total} firme procesate ({sarite} sarite la scriere in DB).")
    finally:
        if conexiune_db:
            conexiune_db.close()

    rezultate_curate = [
        {k: v for k, v in r.items() if not k.startswith("_")} for r in rezultate_complete
    ]
    consolideaza_json_final(rezultate_curate)


def proceseaza_firme_rapid():
    conexiune = conecteaza_db()
    firme = preia_firme_din_db(conexiune)
    conexiune.close()

    if not firme:
        log("Nicio firma de procesat.")
        return

    total = len(firme)

    if os.path.exists(FISIER_IESIRE):
        os.remove(FISIER_IESIRE)

    coada_intrare = Queue()
    for i, firma in enumerate(firme, start=1):
        coada_intrare.put((i, firma))

    coada_iesire = Queue()
    stop_event = threading.Event()

    t_scriere = threading.Thread(target=thread_scriere, args=(coada_iesire, total, stop_event))
    t_scriere.start()

    t_comenzi = threading.Thread(target=thread_comenzi, daemon=True)
    t_comenzi.start()

    threads_workeri = []
    for w_id in range(1, NUM_WORKERS + 1):
        t = threading.Thread(target=worker, args=(w_id, coada_intrare, coada_iesire, total))
        t.start()
        threads_workeri.append(t)

    for t in threads_workeri:
        t.join()

    stop_event.set()
    t_scriere.join()

    if opreste_event.is_set():
        log("\nOprit la cerere - rezultatele procesate pana acum au fost salvate.")
    else:
        log("\nS-a finalizat!")


JOBURI = OrderedDict()
LACAT_JOBURI = threading.Lock()


def acum_iso():
    return datetime.now().isoformat(timespec="seconds")


def instantaneu_job(job, cu_jurnal=True):
    copie = {
        "job_id": job["job_id"],
        "cerere_id": job["cerere_id"],
        "firma_id": job["firma_id"],
        "cui": job["cui"],
        "denumire": job["denumire"],
        "status": job["status"],
        "pas": job["pas"],
        "progres": job["progres"],
        "creat_la": job["creat_la"],
        "pornit_la": job["pornit_la"],
        "finalizat_la": job["finalizat_la"],
        "rezultat": job["rezultat"],
        "eroare": job["eroare"],
    }

    if cu_jurnal:
        copie["jurnal"] = list(job["jurnal"])

    return copie


def taie_joburi_vechi():
    incheiate = [i for i, j in JOBURI.items() if j["status"] in ("finalizata", "esuata")]

    for job_id in incheiate[: max(0, len(incheiate) - API_JOBURI_PASTRATE)]:
        JOBURI.pop(job_id, None)


def programeaza_repornire(job_id):
    if not REPORNIRE_DUPA_ANALIZA:
        return

    with LACAT_JOBURI:
        job = JOBURI.get(job_id)

        if job is None or job["status"] not in ("finalizata", "esuata") or job.get("livrat"):
            return

        job["livrat"] = True

        ocupat = any(j["status"] in ("in_asteptare", "in_lucru") for j in JOBURI.values())

    if ocupat:
        return

    log(">>> Rezultatul a fost preluat de Admin - inchid serviciul, fereastra il reporneste.")
    threading.Timer(REPORNIRE_INTARZIERE, lambda: os._exit(COD_REPORNIRE)).start()


def noteaza(job_id, mesaj, pas=None, progres=None):
    with LACAT_JOBURI:
        job = JOBURI.get(job_id)

        if job is None:
            return

        job["jurnal"].append({"la": acum_iso(), "mesaj": mesaj})

        if pas:
            job["pas"] = pas

        if progres is not None:
            job["progres"] = max(job["progres"], min(100, int(progres)))


def ruleaza_job(job_id, nume, cui):
    with LACAT_JOBURI:
        job = JOBURI.get(job_id)

        if job is None:
            return

        job["status"] = "in_lucru"
        job["pornit_la"] = acum_iso()

    try:
        rezultat = analizeaza_o_firma(
            nume,
            cui,
            raport=lambda mesaj, pas=None, progres=None: noteaza(job_id, mesaj, pas, progres),
        )

        with LACAT_JOBURI:
            job = JOBURI.get(job_id)

            if job is not None:
                job["status"] = "finalizata"
                job["progres"] = 100
                job["pas"] = "gata"
                job["rezultat"] = rezultat
                job["finalizat_la"] = acum_iso()

            taie_joburi_vechi()

    except Exception as e:
        log(f"Jobul {job_id} a esuat: {type(e).__name__}: {e}")

        with LACAT_JOBURI:
            job = JOBURI.get(job_id)

            if job is not None:
                job["status"] = "esuata"
                job["pas"] = "eroare"
                job["eroare"] = f"{type(e).__name__}: {e}"
                job["finalizat_la"] = acum_iso()
                job["jurnal"].append({"la": acum_iso(), "mesaj": f"Căutarea a eșuat: {e}"})

            taie_joburi_vechi()


def construieste_server():
    from flask import Flask, jsonify, request

    aplicatie = Flask(__name__)
    executor = ThreadPoolExecutor(max_workers=API_LUCRATORI, thread_name_prefix="contact")

    def cere_cheie():
        if not API_KEY:
            return None

        if (request.headers.get("X-API-Key") or "").strip() != API_KEY:
            return jsonify({"eroare": "cheie API invalida"}), 401

        return None

    @aplicatie.get("/sanatate")
    def sanatate():
        refuz = cere_cheie()
        if refuz:
            return refuz

        with LACAT_JOBURI:
            in_lucru = sum(1 for j in JOBURI.values() if j["status"] in ("in_asteptare", "in_lucru"))
            total = len(JOBURI)

        return jsonify({
            "ok": True,
            "chrome": CHROME_VERSIUNE,
            "joburi_in_lucru": in_lucru,
            "joburi_in_memorie": total,
        })

    @aplicatie.post("/analiza-contact")
    def porneste_analiza():
        refuz = cere_cheie()
        if refuz:
            return refuz

        date = request.get_json(silent=True) or {}

        cui = re.sub(r"\D", "", str(date.get("cui") or ""))
        denumire = curata_valoare(str(date.get("denumire") or ""))

        if not cui:
            return jsonify({"eroare": "lipseste CUI-ul firmei"}), 422

        if not denumire:
            return jsonify({"eroare": "lipseste denumirea firmei"}), 422

        job_id = uuid.uuid4().hex

        job = {
            "job_id": job_id,
            "cerere_id": date.get("cerere_id"),
            "firma_id": date.get("firma_id"),
            "cui": cui,
            "denumire": denumire,
            "status": "in_asteptare",
            "pas": "in_coada",
            "progres": 0,
            "creat_la": acum_iso(),
            "pornit_la": None,
            "finalizat_la": None,
            "jurnal": [{"la": acum_iso(), "mesaj": "Cerere primită, căutarea e în coadă."}],
            "rezultat": None,
            "eroare": None,
        }

        with LACAT_JOBURI:
            JOBURI[job_id] = job
            instantaneu = instantaneu_job(job)

        executor.submit(ruleaza_job, job_id, denumire, cui)

        log(f"Job {job_id} pornit pentru {denumire} (CUI {cui})")

        return jsonify(instantaneu), 202

    @aplicatie.get("/analiza-contact/<job_id>")
    def stare_analiza(job_id):
        refuz = cere_cheie()
        if refuz:
            return refuz

        try:
            de_la = max(0, int(request.args.get("de_la", 0)))
        except ValueError:
            de_la = 0

        with LACAT_JOBURI:
            job = JOBURI.get(job_id)

            if job is None:
                return jsonify({"eroare": "job inexistent"}), 404

            instantaneu = instantaneu_job(job, cu_jurnal=False)
            jurnal = list(job["jurnal"])

        instantaneu["jurnal"] = jurnal[de_la:]
        instantaneu["jurnal_total"] = len(jurnal)

        programeaza_repornire(job_id)

        return jsonify(instantaneu)

    return aplicatie


def porneste_server(argumente):
    if not API_KEY:
        log(
            ">>> ATENTIE: CONTACT_API_KEY nu e setata - serviciul accepta orice apel. "
            "Seteaz-o si pune aceeasi valoare in .env-ul Adminului (ANALIZA_CONTACT_API_KEY)."
        )

    aplicatie = construieste_server()

    log(
        f"Serviciu de date de contact pornit pe http://{argumente.host}:{argumente.port} "
        f"(Chrome {CHROME_VERSIUNE}, {API_LUCRATORI} cautari in paralel)"
    )

    aplicatie.run(host=argumente.host, port=argumente.port, threaded=True, debug=False, use_reloader=False)

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Detecteaza adresa, telefonul si emailul unei firme."
    )
    parser.add_argument("--server", action="store_true",
                        help="porneste serviciul HTTP folosit de Zivro Admin "
                             "(nu proceseaza firmele din baza de date)")
    parser.add_argument("--host", default=API_HOST)
    parser.add_argument("--port", type=int, default=API_PORT)
    argumente = parser.parse_args()

    if argumente.server:
        return porneste_server(argumente)

    inregistreaza_semnale()
    proceseaza_firme_rapid()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
