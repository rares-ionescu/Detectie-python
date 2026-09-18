import os
import re
import sys
import json
import time
import argparse
import logging
import uuid
import threading
import unicodedata
from collections import OrderedDict
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

DIRECTOR_INTRARE = "firme_produse_servicii_parts"
FISIER_INTRARE = "firme_produse_servicii_200K.json"
DIRECTOR_IESIRE = "verificare_web"
FISIER_IESIRE = os.path.join(DIRECTOR_IESIRE, "firme_verificate_web.json")
FISIER_STARE = os.path.join(DIRECTOR_IESIRE, "checkpoint_verificare.json")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL_IMPLICIT = os.environ.get("OLLAMA_MODEL", "qwen3.5:9b")

NUM_CTX = 16384
TEMPERATURA = 0.1
LIMITA_IMPLICITA = 100

GOOGLE_URL = "https://www.googleapis.com/customsearch/v1"
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CX = os.environ.get("GOOGLE_CX", "")
MOTOR_IMPLICIT = os.environ.get("MOTOR_CAUTARE", "auto")
BACKENDS_DDGS = ["duckduckgo", "startpage", "mojeek", "yahoo", "brave"]

MAX_REZULTATE_CAUTARE = 10
MAX_PAGINI_CITITE = 5
MAX_CARACTERE_PAGINA = 6000
MAX_CARACTERE_TOTAL = 30000
TIMEOUT_PAGINA = 15
TIMEOUT_OLLAMA = 600
DELAY_CAUTARE = 1.5
MAX_REINCERCARI = 3

API_HOST = os.environ.get("ANALIZA_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("ANALIZA_PORT", "8077"))
API_KEY = os.environ.get("ANALIZA_API_KEY", "").strip()

API_LUCRATORI = int(os.environ.get("ANALIZA_LUCRATORI", "2"))

API_JOBURI_PASTRATE = int(os.environ.get("ANALIZA_JOBURI_PASTRATE", "200"))

REPORNIRE_DUPA_ANALIZA = os.environ.get("ANALIZA_REPORNIRE", "0").strip().lower() in ("1", "true", "da")

REPORNIRE_INTARZIERE = float(os.environ.get("ANALIZA_REPORNIRE_INTARZIERE", "1.0"))

COD_REPORNIRE = 7

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

DOMENII_DIRECTOARE = {
    "listafirme.ro", "listafirme.eu", "firme.ro", "totalfirme.ro", "targetare.ro",
    "dashiro.ro", "risco.ro", "termene.ro", "confidas.ro", "infocui.ro",
    "cuifirma.ro", "mfinante.gov.ro", "openapi.ro", "datefirme.ro", "bilanturi.ro",
    "companyinfo.ro", "romanian-companies.eu", "paginiaurii.ro", "firme360.ro",
    "datefirma.ro", "cauta.ro", "rrf.ro", "cui-firme.ro", "analizafiscala.ro",
    "demoanaf.ro", "northdata.de", "northdata.com", "infobel.com", "openmoney.md",
    "all.biz", "4id.ro", "data.gov.ro", "registruladministrativ.ro", "metricbiz.ro",
    "icapb2b.ro", "clientsolutions.io", "dnb.com", "bizoo.ro", "anuntul.ro",
    "facebook.com", "linkedin.com", "instagram.com", "youtube.com", "olx.ro",
    "publi24.ro",
}

EXTENSII_IGNORATE = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar",
    ".jpg", ".jpeg", ".png", ".gif", ".mp4",
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("ddgs").setLevel(logging.ERROR)
logging.getLogger("primp").setLevel(logging.ERROR)

log = logging.getLogger("verificare")

SESIUNE = requests.Session()
SESIUNE.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8",
    }
)

SCHEMA_RASPUNS = {
    "type": "object",
    "properties": {
        "are_informatii": {"type": "boolean"},
        "site_oficial": {"type": "string"},
        "produse_finale": {"type": "array", "items": {"type": "string"}},
        "servicii_finale": {"type": "array", "items": {"type": "string"}},
        "motivatie": {"type": "string"},
    },
    "required": [
        "are_informatii",
        "produse_finale",
        "servicii_finale",
        "motivatie",
    ],
}

PROMPT_SISTEM = """Ești un analist de date meticulos care verifică și curăță activitatea firmelor din România.

Primești:
1. Datele firmei (denumire, CUI, cod CAEN).
2. Liste INIȚIALE generice de produse și servicii.
3. Extrase text din pagini web descărcate despre firmă.

Sarcina ta:
Returnează listele FINALE de produse și servicii pe care firma le oferă, respectând strict următoarele reguli:
REGULI DE BAZĂ (LOGICĂ):
- ANALIZĂ SITE OFICIAL: Dacă printre paginile descărcate identifici site-ul oficial al firmei, folosește detaliile de acolo pentru a ACTUALIZA și RAFINA masiv listele (adaugă produse/servicii reale descrise pe site, șterge ce e irelevant).
- Dacă paginile web confirmă activitatea, filtrează listele: PĂSTREAZĂ ce se potrivește, ELIMINĂ ce nu are legătură, ADAUGĂ activități noi găsite pe web.
- Dacă paginile web NU conțin informații reale (ex. directoare de firme, lipsă date), setează "are_informatii": false. În acest caz, NU șterge nimic din lipsă de dovezi! Păstrează toate elementele inițiale, dar trece-le prin regulile de curățare de mai jos.
- SEPARARE STRICTĂ: Dacă un serviciu este pus greșit în lista de produse inițiale, MUTĂ-L în lista "servicii_finale" (și invers). Produs = bun fizic sau digital; Serviciu = acțiune prestată.

REGULI DE CURĂȚARE ȘI GRAMATICĂ:
- Corectează erorile gramaticale și de exprimare din elementele inițiale (ex: folosește corect "pe care" în loc de "care", repară dezacordurile).
- Folosește corect diacriticele românești (ă, â, î, ș, ț).
- Fii concis: o activitate/produs să aibă 2-8 cuvinte (fără text publicitar).

ALTE CÂMPURI:
- "site_oficial": URL-ul de bază doar dacă ești absolut sigur că îi aparține firmei (din dovezile primite), altfel null.
- "motivatie": scurt rezumat (1-2 propoziții) cu decizia luată.

Răspunde exclusiv cu JSON valid."""

class LimitaGoogle(Exception):
    pass

def spune(raport, mesaj, pas=None, progres=None):
    log.info("  %s", mesaj)

    if raport:
        raport(mesaj, pas=pas, progres=progres)

def pune_majuscula(text):
    text = str(text or "").strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]

def normalizeaza(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()

def fara_diacritice(text):
    descompus = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in descompus if not unicodedata.combining(c))

def dedup(lista):
    vazute = set()
    rezultat = []
    for element in lista or []:
        element = normalizeaza(element)
        if not element: continue
        k = element.lower()
        if k in vazute: continue
        vazute.add(k)
        rezultat.append(element)
    return rezultat

def domeniu(url):
    potrivire = re.match(r"https?://([^/]+)", str(url or ""), re.I)
    if not potrivire: return ""
    gazda = potrivire.group(1).lower()
    return gazda[4:] if gazda.startswith("www.") else gazda

def este_director(url, cui=""):
    gazda = domeniu(url)
    if any(gazda == d or gazda.endswith("." + d) for d in DOMENII_DIRECTOARE):
        return True
    return bool(cui) and cui in str(url)

def curata_nume_firma(denumire):
    nume = normalizeaza(denumire)
    nume = re.sub(
        r"\b(S\.?R\.?L\.?|S\.?A\.?|P\.?F\.?A\.?|I\.?I\.?|I\.?F\.?|"
        r"PERSOAN[AĂ] FIZIC[AĂ] AUTORIZAT[AĂ]|"
        r"[IÎ]NTREPRINDERE (INDIVIDUAL[AĂ]|FAMILIAL[AĂ])|"
        r"CABINET (INDIVIDUAL|MEDICAL)|SOCIETATE CIVIL[AĂ])\b\.?",
        "", nume, flags=re.I,
    )
    return normalizeaza(nume)

def este_persoana_fizica(denumire):
    return bool(
        re.search(
            r"\b(P\.?F\.?A\.?|I\.?I\.?|I\.?F\.?|PERSOAN[AĂ] FIZIC[AĂ]|"
            r"[IÎ]NTREPRINDERE (INDIVIDUAL[AĂ]|FAMILIAL[AĂ])|CABINET)\b",
            normalizeaza(denumire), flags=re.I,
        )
    )

def mesaj_google(raspuns):
    try:
        mesaj = (raspuns.json().get("error", {}).get("message") or "").strip()
        return mesaj or "raspuns neasteptat"
    except ValueError:
        return raspuns.text[:200]

def verifica_google():
    try:
        raspuns = SESIUNE.get(GOOGLE_URL, params={"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": "test", "num": 1}, timeout=30)
    except Exception as e:
        log.error("Nu pot contacta Google: %s", e)
        return False
    if raspuns.status_code >= 400:
        log.error("Google a refuzat cheia (%d): %s", raspuns.status_code, mesaj_google(raspuns))
        return False
    return True

def cauta_google(interogare, max_rezultate):
    parametri = {"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": interogare, "num": min(10, max(1, max_rezultate)), "hl": "ro", "gl": "ro"}
    for incercare in range(1, MAX_REINCERCARI + 1):
        try:
            raspuns = SESIUNE.get(GOOGLE_URL, params=parametri, timeout=30)
            if raspuns.status_code in (403, 429):
                raise LimitaGoogle(mesaj_google(raspuns))
            if raspuns.status_code >= 400:
                return []
            return [{"titlu": normalizeaza(e.get("title")), "url": e.get("link") or "", "rezumat": normalizeaza(e.get("snippet"))} for e in (raspuns.json().get("items") or [])]
        except LimitaGoogle:
            raise
        except Exception:
            if incercare < MAX_REINCERCARI: time.sleep(2 * incercare)
    return []

def cauta_ddgs(interogare, max_rezultate):
    from ddgs import DDGS
    for backend in BACKENDS_DDGS:
        try:
            gasite = DDGS(timeout=20).text(interogare, region="ro-ro", max_results=max_rezultate, backend=backend)
            if gasite:
                return [{"titlu": normalizeaza(g.get("title")), "url": g.get("href") or g.get("url") or "", "rezumat": normalizeaza(g.get("body") or g.get("description"))} for g in gasite]
        except Exception:
            continue
    return []

def cauta_web(firma, max_rezultate, motor, numar_interogari=3, raport=None):
    nume = curata_nume_firma(firma.get("denumire"))
    cui = re.sub(r"\D", "", str(firma.get("cui") or ""))
    interogari = ['"{}" {}'.format(nume, cui).strip(), "{} produse servicii".format(nume), "{} site oficial contact".format(nume)][: max(1, numar_interogari)]
    rezultate = []
    vazute = set()
    for pozitie, interogare in enumerate(interogari, 1):
        spune(
            raport,
            "Caut pe web (%s), interogarea %d din %d: %s" % (motor, pozitie, len(interogari), interogare),
            pas="cautare",
            progres=5 + int(15.0 * pozitie / len(interogari)),
        )
        gasite = cauta_google(interogare, max_rezultate) if motor == "google" else cauta_ddgs(interogare, max_rezultate)
        noi = 0
        for gasit in gasite:
            url = gasit["url"]
            if url and url not in vazute:
                vazute.add(url)
                rezultate.append(gasit)
                noi += 1
        spune(raport, "%d rezultate noi (%d în total)" % (noi, len(rezultate)))
        time.sleep(DELAY_CAUTARE)
    return rezultate

def jetoane_nume(denumire):
    nume = fara_diacritice(curata_nume_firma(denumire)).lower()
    return [j for j in re.split(r"[^a-z0-9]+", nume) if len(j) >= 3]

def nivel_identitate(text, cui, jetoane):
    if cui and re.search(r"(?<!\d)%s(?!\d)" % re.escape(cui), text): return "cui"
    if jetoane:
        marunt = fara_diacritice(text).lower()
        if all(re.search(r"\b%s" % re.escape(j), marunt) for j in jetoane): return "nume"
    return ""

def identitate_tare(nivel, doar_cui):
    if nivel == "cui": return True
    return nivel == "nume" and not doar_cui

def descarca_pagina(url, cui, jetoane):
    if url.lower().split("?")[0].endswith(EXTENSII_IGNORATE): return None
    try:
        raspuns = SESIUNE.get(url, timeout=TIMEOUT_PAGINA, allow_redirects=True)
        if raspuns.status_code != 200: return None
        tip = (raspuns.headers.get("Content-Type") or "").lower()
        if "html" not in tip and "text" not in tip: return None
        if not raspuns.encoding or raspuns.encoding.lower() == "iso-8859-1": raspuns.encoding = raspuns.apparent_encoding
        supa = BeautifulSoup(raspuns.text, "html.parser")
        for eticheta in supa(["script", "style", "noscript", "svg", "iframe", "header", "footer", "nav"]):
            eticheta.decompose()
        text = re.sub(r"\s{2,}", " ", supa.get_text(" ", strip=True))
        if len(text) < 200: return None
        return {"text": text[:MAX_CARACTERE_PAGINA], "identitate": nivel_identitate(text, cui, jetoane)}
    except Exception:
        return None

def scor_rezultat(rezultat, firma, cui):
    url = rezultat["url"]
    if este_director(url, cui): return 0
    gazda_curata = re.sub(r"[^a-z0-9]", "", domeniu(url).split(".")[0])
    jetoane = jetoane_nume(firma.get("denumire"))
    if jetoane and any(j in gazda_curata for j in jetoane): return 3
    return 2

def alege_pagini(rezultate, firma, cui):
    ordonate = sorted(rezultate, key=lambda r: -scor_rezultat(r, firma, cui))
    alese = []
    gazde = set()
    for rezultat in ordonate:
        gazda = domeniu(rezultat["url"])
        if gazda in gazde: continue
        gazde.add(gazda)
        r = dict(rezultat)
        r["catalog"] = este_director(r["url"], cui)
        alese.append(r)
        if len(alese) >= MAX_PAGINI_CITITE: break
    return alese

def strange_dovezi(rezultate, firma, cui, raport=None):
    alese = alege_pagini(rezultate, firma, cui)
    if not alese:
        spune(raport, "Nicio pagină de citit: căutarea nu a întors rezultate utilizabile.", pas="pagini", progres=55)
        return []
    jetoane = jetoane_nume(firma.get("denumire"))
    doar_cui = este_persoana_fizica(firma.get("denumire"))
    spune(
        raport,
        "Descarc %d pagini: %s" % (len(alese), ", ".join(domeniu(r["url"]) or r["url"] for r in alese)),
        pas="pagini",
        progres=30,
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        pagini = list(executor.map(lambda r: descarca_pagina(r["url"], cui, jetoane), alese))

    dovezi = []
    total = 0
    for rezultat, pagina in zip(alese, pagini):
        identitate = (pagina or {}).get("identitate") or nivel_identitate(rezultat["titlu"] + " " + rezultat["rezumat"], cui, jetoane)
        if rezultat["catalog"]: tip = "catalog de firme (dovada slaba)"
        elif identitate == "cui": tip = "identitate sigura (prin CUI)"
        elif identitate == "nume": tip = "identitate probabila (prin Nume)"
        else: tip = "identitate neconfirmata"

        bucata = f"SURSA: {rezultat['url']}\nTIP: {tip}\nTITLU: {rezultat['titlu']}\nREZUMAT: {rezultat['rezumat']}\n"
        if pagina: bucata += f"CONTINUT (extras din pagina): {pagina['text']}\n"

        bucata = bucata[: max(0, MAX_CARACTERE_TOTAL - total)]
        if not bucata: break

        identitate_folosita = "" if rezultat["catalog"] else identitate
        spune(raport, "%s → %s%s" % (
            domeniu(rezultat["url"]) or rezultat["url"],
            tip,
            "" if pagina else ", fără conținut citibil",
        ))
        dovezi.append({
            "url": rezultat["url"], "titlu": rezultat["titlu"], "catalog": rezultat["catalog"],
            "identitate": identitate_folosita, "identitate_tare": identitate_tare(identitate_folosita, doar_cui),
            "are_continut": bool(pagina), "text": bucata,
        })
        total += len(bucata)
        if total >= MAX_CARACTERE_TOTAL: break
    spune(raport, "Am strâns %d dovezi (%d caractere de text)." % (len(dovezi), total), pas="pagini", progres=55)
    return dovezi

def construieste_prompt(firma, dovezi):
    produse = dedup(firma.get("produse"))
    servicii = dedup(firma.get("servicii"))
    linii = [
        "DATELE FIRMEI",
        f"Denumire: {normalizeaza(firma.get('denumire'))}",
        f"CUI: {normalizeaza(firma.get('cui'))}",
        f"Cod CAEN: {normalizeaza(firma.get('cod_caen'))}",
        "",
        f"LISTA INIȚIALĂ PRODUSE ({len(produse)}):",
    ]
    linii += [f"- {p}" for p in produse] or ["(lista goala)"]
    linii += ["", f"LISTA INIȚIALĂ SERVICII ({len(servicii)}):"]
    linii += [f"- {s}" for s in servicii] or ["(lista goala)"]
    linii += ["", "PAGINI WEB GĂSITE (inclusiv conținutul site-ului oficial dacă a fost descărcat):"]
    linii += [d["text"] for d in dovezi] or ["(nu s-a gasit nicio pagina)"]
    return "\n".join(linii)

def intreaba_ollama(model, prompt, gandire, raport=None):
    corp = {
        "model": model,
        "messages": [
            {"role": "system", "content": PROMPT_SISTEM},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "format": SCHEMA_RASPUNS,
        "options": {"temperature": TEMPERATURA, "num_ctx": NUM_CTX},
    }
    if gandire: corp["think"] = False

    for incercare in range(1, MAX_REINCERCARI + 1):
        if incercare == 1:
            spune(raport, "Trimit %d caractere de context către modelul %s..." % (len(prompt), model), pas="model", progres=70)
        else:
            spune(raport, "Reîncerc la model (încercarea %d din %d)..." % (incercare, MAX_REINCERCARI))
        try:
            raspuns = SESIUNE.post(OLLAMA_URL.rstrip("/") + "/api/chat", json=corp, timeout=TIMEOUT_OLLAMA)
            if raspuns.status_code == 400 and "think" in corp:
                corp.pop("think")
                continue
            raspuns.raise_for_status()
            continut = raspuns.json().get("message", {}).get("content") or ""
            continut = re.sub(r"^```(?:json)?|```$", "", continut.strip(), flags=re.M).strip()
            return json.loads(continut)
        except json.JSONDecodeError:
            pass
        except Exception:
            time.sleep(2 * incercare)
    return None

def proceseaza_firma(firma, model, gandire, doar_cautare, motor, interogari, raport=None):
    cui = re.sub(r"\D", "", str(firma.get("cui") or ""))
    produse_initiale = dedup(firma.get("produse"))
    servicii_initiale = dedup(firma.get("servicii"))

    spune(
        raport,
        "Pornesc analiza pentru %s (CUI %s), pornind de la %d produse și %d servicii."
        % (normalizeaza(firma.get("denumire")) or "—", cui or "—", len(produse_initiale), len(servicii_initiale)),
        pas="pornire",
        progres=5,
    )

    rezultate = cauta_web(firma, MAX_REZULTATE_CAUTARE, motor, interogari, raport)
    dovezi = strange_dovezi(rezultate, firma, cui, raport)
    poate_adauga = any(d["identitate"] for d in dovezi)

    log.info(
        "  %d rezultate, %d pagini citite, %d confirma identitatea",
        len(rezultate), sum(1 for d in dovezi if d["are_continut"]), sum(1 for d in dovezi if d["identitate"]),
    )

    analiza = None
    if not doar_cautare:
        analiza = intreaba_ollama(model, construieste_prompt(firma, dovezi), gandire, raport)
        if analiza is None:
            spune(raport, "Modelul nu a răspuns cu JSON valid — păstrez listele inițiale, neatinse.", pas="model", progres=90)

    if analiza:
        produse_finale = dedup(analiza.get("produse_finale", []))
        servicii_finale = dedup(analiza.get("servicii_finale", []))
    else:
        produse_finale = produse_initiale
        servicii_finale = servicii_initiale

    if (produse_initiale or servicii_initiale) and not produse_finale and not servicii_finale:
        produse_finale = produse_initiale
        servicii_finale = servicii_initiale

    produse_finale = [pune_majuscula(p) for p in produse_finale]
    servicii_finale = [pune_majuscula(s) for s in servicii_finale]

    inregistrare = {
        "id": firma.get("id"),
        "denumire": firma.get("denumire"),
        "cui": firma.get("cui"),
        "cod_caen": firma.get("cod_caen"),
        "produse_initiale": produse_initiale,
        "servicii_initiale": servicii_initiale,
        "produse_finale": produse_finale,
        "servicii_finale": servicii_finale,
        "website_detectat": (analiza.get("site_oficial") if analiza else None),

        "motivatie": normalizeaza(analiza.get("motivatie")) if analiza else "",
        "are_informatii": bool(analiza.get("are_informatii")) if analiza else False,
        "analiza_reusita": analiza is not None,
        "surse": [
            {
                "url": d["url"],
                "titlu": d["titlu"],
                "catalog": d["catalog"],
                "identitate": d["identitate"],
                "are_continut": d["are_continut"],
            }
            for d in dovezi
        ],
    }

    mutari_produse = len(produse_initiale) != len(produse_finale)
    mutari_servicii = len(servicii_initiale) != len(servicii_finale)

    log.info(
        "  produse %d -> %d | servicii %d -> %d%s",
        len(produse_initiale), len(produse_finale),
        len(servicii_initiale), len(servicii_finale),
        " (nemodificat)" if not mutari_produse and not mutari_servicii else ""
    )

    spune(
        raport,
        "Gata: produse %d → %d, servicii %d → %d."
        % (len(produse_initiale), len(produse_finale), len(servicii_initiale), len(servicii_finale)),
        pas="gata",
        progres=100,
    )

    return inregistrare

def citeste_firme(cale, start, limita):
    with open(cale, "r", encoding="utf-8") as f:
        return json.load(f)[start : start + limita]

def citeste_json(cale, implicit):
    if not os.path.exists(cale): return implicit
    try:
        with open(cale, "r", encoding="utf-8") as f: return json.load(f)
    except Exception:
        return implicit

def scrie_json(cale, date):
    temporar = cale + ".tmp"
    with open(temporar, "w", encoding="utf-8") as f:
        json.dump(date, f, ensure_ascii=False, indent=2)
    os.replace(temporar, cale)

def verifica_ollama(model):
    try:
        raspuns = SESIUNE.get(OLLAMA_URL.rstrip("/") + "/api/tags", timeout=10)
        raspuns.raise_for_status()
        modele = raspuns.json().get("models", [])
    except Exception as e:
        log.error("Nu pot contacta Ollama la %s: %s", OLLAMA_URL, e)
        return None
    for intrare in modele:
        if intrare.get("name") == model:
            return "thinking" in (intrare.get("capabilities") or [])
    return None

def alege_motor(motor):
    if motor != "auto":
        return motor

    return "google" if GOOGLE_API_KEY and GOOGLE_CX else "ddgs"


JOBURI = OrderedDict()
LACAT_JOBURI = threading.Lock()

CAPABILITATE_GANDIRE = {}

class CerereInvalida(Exception):
    pass

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

    log.info("Rezultatul a fost preluat de Admin - inchid serviciul, fereastra il reporneste.")
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

def ruleaza_job(job_id, firma, optiuni):
    with LACAT_JOBURI:
        job = JOBURI.get(job_id)

        if job is None:
            return

        job["status"] = "in_lucru"
        job["pornit_la"] = acum_iso()

    try:
        inregistrare = proceseaza_firma(
            firma,
            optiuni["model"],
            optiuni["gandire"],
            False,
            optiuni["motor"],
            optiuni["interogari"],
            raport=lambda mesaj, pas=None, progres=None: noteaza(job_id, mesaj, pas, progres),
        )

        with LACAT_JOBURI:
            job = JOBURI.get(job_id)

            if job is not None:
                job["status"] = "finalizata"
                job["progres"] = 100
                job["pas"] = "gata"
                job["rezultat"] = inregistrare
                job["finalizat_la"] = acum_iso()

            taie_joburi_vechi()

    except Exception as e:
        log.exception("Jobul %s a esuat", job_id)

        with LACAT_JOBURI:
            job = JOBURI.get(job_id)

            if job is not None:
                job["status"] = "esuata"
                job["pas"] = "eroare"
                job["eroare"] = "%s: %s" % (type(e).__name__, e)
                job["finalizat_la"] = acum_iso()
                job["jurnal"].append({"la": acum_iso(), "mesaj": "Analiza a eșuat: %s" % e})

            taie_joburi_vechi()

SEPARATOR_ENUMERARE = " | "

def lista_din_cerere(valoare):
    if valoare is None:
        return []

    if isinstance(valoare, str):
        valoare = valoare.split(SEPARATOR_ENUMERARE)

    if not isinstance(valoare, list):
        raise CerereInvalida("produse/servicii trebuie sa fie lista sau text separat prin ' | '")

    return dedup(valoare)

def construieste_server(optiuni):
    from flask import Flask, jsonify, request

    aplicatie = Flask(__name__)
    executor = ThreadPoolExecutor(max_workers=API_LUCRATORI, thread_name_prefix="analiza")

    def cere_cheie():
        if not API_KEY:
            return None

        if (request.headers.get("X-API-Key") or "").strip() != API_KEY:
            return jsonify({"eroare": "cheie API invalida"}), 401

        return None

    @aplicatie.get("/sanatate")
    def sanatate():
        refuz = cere_cheie()
        if refuz: return refuz

        with LACAT_JOBURI:
            in_lucru = sum(1 for j in JOBURI.values() if j["status"] in ("in_asteptare", "in_lucru"))
            total = len(JOBURI)

        return jsonify({
            "ok": True,
            "model": optiuni["model"],
            "motor": optiuni["motor"],
            "ollama": OLLAMA_URL,
            "joburi_in_lucru": in_lucru,
            "joburi_in_memorie": total,
        })

    @aplicatie.post("/analiza")
    def porneste_analiza():
        refuz = cere_cheie()
        if refuz: return refuz

        date = request.get_json(silent=True) or {}

        cui = re.sub(r"\D", "", str(date.get("cui") or ""))
        denumire = normalizeaza(date.get("denumire"))

        if not denumire:
            return jsonify({"eroare": "lipseste denumirea firmei"}), 422

        try:
            produse = lista_din_cerere(date.get("produse"))
            servicii = lista_din_cerere(date.get("servicii"))
        except CerereInvalida as e:
            return jsonify({"eroare": str(e)}), 422

        firma = {
            "id": date.get("firma_id"),
            "denumire": denumire,
            "cui": cui,
            "cod_caen": normalizeaza(date.get("cod_caen")),
            "produse": produse,
            "servicii": servicii,
        }

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
            "jurnal": [{"la": acum_iso(), "mesaj": "Cerere primită, analiza e în coadă."}],
            "rezultat": None,
            "eroare": None,
        }

        with LACAT_JOBURI:
            JOBURI[job_id] = job
            instantaneu = instantaneu_job(job)

        executor.submit(ruleaza_job, job_id, firma, optiuni)

        log.info("Job %s pornit pentru %s (CUI %s)", job_id, denumire, cui or "—")

        return jsonify(instantaneu), 202

    @aplicatie.get("/analiza/<job_id>")
    def stare_analiza(job_id):
        refuz = cere_cheie()
        if refuz: return refuz

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
    motor = alege_motor(argumente.motor)

    if motor == "google" and not verifica_google():
        return 1

    gandire = verifica_ollama(argumente.model)

    if gandire is None:
        return 1

    if not API_KEY:
        log.warning(
            "ATENTIE: ANALIZA_API_KEY nu e setata — serviciul accepta orice apel. "
            "Seteaz-o si pune aceeasi valoare in .env-ul Adminului (ANALIZA_API_KEY)."
        )

    optiuni = {
        "model": argumente.model,
        "gandire": gandire,
        "motor": motor,
        "interogari": argumente.interogari,
    }

    aplicatie = construieste_server(optiuni)

    log.info(
        "Serviciu de analiza pornit pe http://%s:%d (model %s, motor %s, %d analize in paralel)",
        argumente.host, argumente.port, argumente.model, motor, API_LUCRATORI,
    )

    aplicatie.run(host=argumente.host, port=argumente.port, threaded=True, debug=False, use_reloader=False)

    return 0

def main():
    parser = argparse.ArgumentParser(description="Verifica firme cu Ollama (JSON curat)")
    parser.add_argument("--fisier", default=os.path.join(DIRECTOR_INTRARE, FISIER_INTRARE))
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limita", type=int, default=LIMITA_IMPLICITA)
    parser.add_argument("--model", default=MODEL_IMPLICIT)
    parser.add_argument("--iesire", default=FISIER_IESIRE)
    parser.add_argument("--reia", action="store_true")
    parser.add_argument("--motor", choices=["auto", "google", "ddgs"], default=MOTOR_IMPLICIT)
    parser.add_argument("--interogari", type=int, default=3, choices=[1, 2, 3])
    parser.add_argument("--doar-cautare", action="store_true")
    parser.add_argument("--server", action="store_true",
                        help="porneste serviciul HTTP folosit de Zivro Admin (nu proceseaza fisierul)")
    parser.add_argument("--host", default=API_HOST)
    parser.add_argument("--port", type=int, default=API_PORT)
    argumente = parser.parse_args()

    if argumente.server:
        return porneste_server(argumente)

    if not os.path.exists(argumente.fisier):
        log.error("Nu gasesc fisierul: %s", argumente.fisier)
        return 1

    gandire = None
    if not argumente.doar_cautare:
        gandire = verifica_ollama(argumente.model)
        if gandire is None: return 1

    os.makedirs(os.path.dirname(argumente.iesire) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(FISIER_STARE) or ".", exist_ok=True)

    firme = citeste_firme(argumente.fisier, argumente.start, argumente.limita)
    motor = alege_motor(argumente.motor)
    if motor == "google" and not verifica_google(): return 1

    procesate = set(citeste_json(FISIER_STARE, {}).get("procesate") or []) if argumente.reia else set()
    rezultate = citeste_json(argumente.iesire, []) if argumente.reia else []
    inceput = time.time()

    for pozitie, firma in enumerate(firme, 1):
        if firma.get("id") in procesate: continue
        log.info("[%d/%d] %s", pozitie, len(firme), firma.get("denumire"))

        try:
            inreg = proceseaza_firma(firma, argumente.model, gandire, argumente.doar_cautare, motor, argumente.interogari)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error("  eroare: %s", e)
            continue

        rezultate.append(inreg)
        procesate.add(firma.get("id"))
        scrie_json(argumente.iesire, rezultate)
        scrie_json(FISIER_STARE, {"procesate": sorted(procesate, key=str)})

    log.info("\nGata. %d firme in %s", len(rezultate), argumente.iesire)
    return 0

if __name__ == "__main__":
    sys.exit(main())
