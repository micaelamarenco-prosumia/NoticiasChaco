"""
Bot de Telegram: avisa cada noticia nueva publicada en portales del Chaco.

Cómo funciona, para cada portal y en este orden:
  1. Lee su feed RSS (lo más confiable).
  2. Si no hay RSS o está bloqueado, lee la portada y detecta los links de notas.
  3. Si el portal bloquea el acceso (error 403, muy común en servidores como Railway),
     lee las notas de ese portal a través de Google Noticias.
- La primera vez que lee un portal (o una fuente nueva de ese portal) solo memoriza
  lo publicado, para no inundarte de mensajes.
- Si un portal falla muchas veces seguidas, te avisa por Telegram; y te avisa cuando se recupera.
- Todos los días a las 9:00 manda un resumen ("Destacados") con las notas de política provincial
  publicadas desde las 17:00 del día anterior, en un solo mensaje, dividido en
  Sección 1 (Política / Elecciones) y Sección 2 (Gestión), sin notas repetidas entre portales.

Configuración (variables de entorno, opcionales si ya están cargadas abajo):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, INTERVALO_SEGUNDOS (por defecto 90)
"""

import html
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode, quote

import feedparser
import requests
from bs4 import BeautifulSoup

try:  # imita a un navegador Chrome real: esquiva muchos bloqueos anti-bots
    from curl_cffi import requests as navegador
except ImportError:
    navegador = None

try:  # convierte los links de Google Noticias en el link original de la nota
    from googlenewsdecoder import gnewsdecoder as googlenewsdecoder
except ImportError:
    googlenewsdecoder = None

# ---------------------------------------------------------------- portales
# "feeds": direcciones de RSS conocidas (si quedan vacías, el bot las busca solo)
PORTALES = {
    "Diario TAG":        {"home": "https://www.diariotag.com/",            "feeds": ["https://www.diariotag.com/feed/"]},
    "Primera Línea":     {"home": "https://diarioprimeralinea.com.ar/",    "feeds": ["https://diarioprimeralinea.com.ar/feed/"]},
    "La Voz del Chaco":  {"home": "https://www.diariolavozdelchaco.com/",  "feeds": ["https://www.diariolavozdelchaco.com/feed/"]},
    "Data Chaco":        {"home": "https://www.datachaco.com/",            "feeds": ["https://www.datachaco.com/rss"]},
    "Chaco Día por Día": {"home": "https://chacodiapordia.com/",           "feeds": []},
    "Diario Norte":      {"home": "https://www.diarionorte.com/",          "feeds": []},
    "Diario Chaco":      {"home": "https://www.diariochaco.com/",          "feeds": ["https://www.diariochaco.com/rss"]},
}

# ---------------------------------------------------------------- config
TOKEN = os.getenv("TELEGRAM_TOKEN", "8474071759:AAGB6KS1CS9nDlL4Gf9qoom7O-l_0NJvtPA").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "702800514").strip()
INTERVALO = int(os.getenv("INTERVALO_SEGUNDOS", "90"))
ARCHIVO_ESTADO = os.getenv("ARCHIVO_ESTADO", "estado_bot.json")
MAX_VISTOS_POR_SITIO = 1500
FALLOS_PARA_AVISAR = 10            # vueltas seguidas sin poder leer un portal antes de avisarte
REBUSCAR_FEED_CADA = 30 * 60       # si un portal no tenía RSS, lo vuelve a buscar cada 30 min

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
}

RUTAS_FEED = ["feed/", "rss/", "rss", "feed", "rss.xml", "feed.xml", "index.xml"]
SEGMENTOS_NO_NOTA = {
    "categoria", "category", "tag", "tags", "seccion", "secciones", "author", "autor",
    "page", "pagina", "buscar", "search", "contacto", "wp-admin", "wp-login", "login",
    "registro", "publicidad", "staff", "politica-de-privacidad", "feed", "rss",
}

# ---------------------------------------------------------------- filtro político provincial
# Se busca en el título (y en el resumen, cuando el diario lo publica en su RSS).
# No importan mayúsculas ni tildes. Un término que termina en * vale como prefijo (ej: "chaquen*").
# Estas listas se pueden ampliar desde Telegram con /agregar y /excluir (ver /ayuda).
FILTRO_ACTIVO = True

# Alcanza con que aparezca uno de estos para que la nota pase.
TERMINOS_FUERTES = [
    # Ejecutivo
    "zdero", "gobernador", "gobernadora", "vicegobernador", "vicegobernadora", "schneider",
    "peche", "meiriño", "julio ferro", "julio ferró", "gabinete provincial", "gobierno provincial",
    "gobierno del chaco", "ministro de gobierno", "casa de gobierno", "boletin oficial",
    # Legislativo
    "legislatura", "legislador*", "diputados provinciales", "diputado provincial", "diputada provincial",
    "camara de diputados del chaco", "presupuesto provincial", "presupuesto 2027",
    # Judicial
    "poder judicial", "superior tribunal", "stj", "procurador general", "procuracion general",
    "ministerio publico", "consejo de la magistratura", "jury de enjuiciamiento", "tribunal de cuentas",
    "fiscalia de estado", "fiscal de estado",
    # Empresas y organismos del Estado chaqueño
    "sameep", "secheep", "nbch", "nuevo banco del chaco", "loteria chaqueña", "fiduciaria del norte",
    "ecom", "ipduv", "instituto de vivienda", "insssep", "administracion tributaria provincial",
    "vialidad provincial", "puerto barranqueras", "sefecha", "livio gutierrez",
]

# Diputados provinciales (Legislatura del Chaco, composición desde el 10/12/2025).
# Apellidos poco comunes van solos; los comunes van con nombre, para no confundir con otras personas.
LEGISLADORES = [
    # Mandato 2025-2029 — Chaco Puede + / La Libertad Avanza
    "julio ferro", "julio cesar ferro", "maggio", "zukiewicz", "botteri", "jorge gomez",
    "jorge fernando gomez", "jarenko", "ivan garcia", "ivan joaquin garcia", "maria elena rodriguez",
    # Mandato 2025-2029 — Frente Chaco Merece Más
    "chomiak", "katia blanc", "moser", "insaurralde", "benitez molas", "lucas nass",
    # Mandato 2025-2029 — Frente Primero Chaco (Laura Fogar reemplazó a Magda Ayala en agosto 2026)
    "honcheruk", "fogar",
    # Mandato 2023-2027
    "bisonni", "blasco", "carmen delgado", "delgado britto", "gyoker", "salom", "samuel vargas",
    "wannesson", "maida with", "cavana", "analia flores", "guillon", "perez pons", "slimel",
    "cubells", "schwartz", "josefina gonzalez",
]

# Referentes políticos provinciales que no ocupan cargo en los tres poderes.
REFERENTES = ["capitanich"]

# Intendentes confirmados. Faltan municipios: se pueden sumar con /agregar o pidiéndolo para dejarlos fijos.
INTENDENTES = [
    "nikisch",                  # Resistencia
    "cipolini",                 # Presidencia Roque Sáenz Peña
    "magda ayala", "ciles ayala",  # Barranqueras
    "mariela soto",             # Colonia Popular
    "alicia leiva",             # Colonias Unidas
    "liliana pascua",           # Enrique Urien
    "ines ortega",              # Fuerte Esperanza
    "stacchiotti",              # Gancedo
    "acerbo",                   # General Capdevila
    "mitoire",                  # La Eduvigis
    "alba sanchez",             # La Tigra
    "panzardi",                 # Laguna Blanca
    "judith gomez",             # Los Frentones
    "piccilli",                 # Pampa Almirón
    "seifert",                  # Pampa del Infierno
    "elba lezcano",             # Samuhú
    "marcela duarte",           # Tres Isletas
    # Adelaida Maggio (Santa Sylvina) ya está en la lista de legisladores.
]

# Estos solo pasan si además hay contexto chaqueño o si la nota no es claramente nacional.
TERMINOS_DEBILES = [
    "gobierno", "ministro", "ministra", "ministerio", "secretario", "secretaria", "subsecretari*",
    "funcionari*", "diputad*", "senador*", "sesion", "proyecto de ley", "ley", "decreto",
    "juez", "jueza", "jueces", "fiscal", "fiscalia", "tribunal", "camara", "oposicion", "oficialismo",
    "presupuesto", "gestion", "licitacion", "obra publica", "paritaria*", "estatales", "docentes",
    "intendente", "intendenta", "intendentes", "intendencia", "municipio", "municipios",
    "municipalidad", "municipal", "concejo", "concejal*", "viceintendente", "viceintendenta",
]

CONTEXTO_CHACO = [
    "chaco", "chaquen*", "resistencia", "barranqueras", "fontana", "vilelas", "saenz peña",
    "villa angela", "castelli", "charata", "provincial", "provinciales",
]

# Si aparece uno de estos y NO hay contexto chaqueño, la nota se descarta.
MARCAS_NACIONALES = [
    "milei", "nacion", "nacional", "casa rosada", "congreso", "senado", "adorni", "caputo",
    "bullrich", "kicillof", "francos", "karina milei", "anses", "indec", "arca", "afip", "bcra",
    "banco central", "corte suprema", "fmi", "trump", "eeuu", "estados unidos", "buenos aires",
    "cordoba", "santa fe", "corrientes", "mendoza", "papa leon",
]

# Si aparece uno de estos, la nota se descarta siempre.
EXCLUIR_SIEMPRE = [
    "horoscopo", "quiniela", "receta", "farandula",
    # Personas que no interesan
    "infran", "valdes",
    # Fútbol
    "futbol*", "futbolist*", "gol", "goles", "golazo", "goleada", "goleador*", "hinchas", "hinchada",
    "scaloni", "messi", "seleccion argentina", "albiceleste", "afa", "liga profesional",
    "torneo federal", "primera nacional", "liga chaqueña", "copa libertadores", "copa sudamericana",
    "copa argentina", "mundial 2026", "fichaje", "director tecnico", "boca juniors", "river plate",
    "sarmiento de resistencia", "for ever",
]

# Pronóstico del tiempo: se descarta, salvo que la nota nombre a alguien del gobierno o a una
# institución provincial (ej: "Zdero recorrió las zonas afectadas por las lluvias" sí pasa).
CLIMA = [
    "pronostico*", "clima", "climatic*", "meteorolog*", "smn", "temperatura*", "termometro",
    "calor", "caluroso", "ola de calor", "frio", "fresco", "helada*", "lluvia*", "lloviznas",
    "tormenta*", "chaparron*", "nublado", "despejado", "humedad", "viento*", "rafagas",
    "alerta amarilla", "alerta naranja", "alerta roja", "alerta por", "como estara el tiempo",
    "el tiempo en", "el tiempo para",
]

# Policiales: se descartan, salvo que la nota nombre a alguien del gobierno o a una institución
# provincial (ej: "Zdero entregó patrulleros a la Policía" sí pasa).
POLICIALES = [
    "policia*", "comisaria", "detenid*", "detuvieron", "demorad*", "aprehendid*", "allanamiento*",
    "robo", "robos", "robaron", "asalto", "asaltaron", "hurto", "motochorro*", "delincuente*",
    "homicidio", "asesinato", "asesinad*", "crimen", "femicidio", "apuñal*", "baleado", "tiroteo",
    "disparo*", "arma blanca", "abuso", "violacion", "secuestr*", "narco*", "droga*", "marihuana",
    "cocaina", "estafa*", "accidente*", "choque", "chocaron", "siniestro vial", "atropell*", "vuelco",
    "murio", "muerte", "cadaver", "hallaron muerto", "investigan", "imputad*", "prision preventiva",
]

# ---------------------------------------------------------------- resumen diario ("Destacados")
HORA_RESUMEN = int(os.getenv("HORA_RESUMEN", "9"))   # hora de envío (Argentina)
HORA_INICIO_RESUMEN = 17                             # junta las notas desde las 17:00 del día anterior
LARGO_MAXIMO_MENSAJE = 4000                          # Telegram corta en 4096; se deja margen

# Si el título (o el comienzo del resumen) tiene alguno de estos, la nota va a la
# SECCIÓN 1: Política / Elecciones. Todo lo demás va a la SECCIÓN 2: Gestión.
# Incluye las críticas de la oposición a medidas de gestión.
TERMINOS_POLITICA = [
    # Elecciones
    "eleccion*", "electoral*", "comicios", "candidat*", "precandidat*", "campaña", "boleta*",
    "urnas", "escrutinio", "votantes", "padron", "encuesta*", "sondeo*",
    "listas", "armado de listas", "encabeza la lista", "encabezara", "interna", "internas",
    "alianza*", "frente electoral", "reeleccion", "postulacion", "postula*",
    # Partidos y espacios
    "pj", "peronismo", "peronista*", "justicialis*", "kirchner*", "ucr", "radicalismo",
    "la libertad avanza", "lla", "libertari*", "partido*", "militancia", "militante*",
    "oposicion", "opositor*", "oficialismo", "oficialista*", "bloque opositor", "bloque oficialista",
    "capitanich",
    # Dichos, cruces y críticas
    "critico", "criticaron", "critica a", "criticas a", "duras criticas", "cuestiono", "cuestionaron",
    "cuestionan", "cuestionamiento*", "apunto contra", "apuntaron contra", "apuntan contra",
    "arremetio", "fustigo", "disparo contra", "cargo contra",
    "rechazo", "rechazan", "rechazaron", "repudi*", "respondio a", "le respondio", "salio a responder",
    "cruce", "se cruzaron", "chicana*", "polemica", "polemico", "reclamo al gobierno",
]

# Palabras que no se tienen en cuenta al comparar títulos entre portales.
PALABRAS_VACIAS = set(
    "a al ante bajo con contra de del desde durante e el en entre esta este hacia hasta la las le les "
    "lo los mas para pero por que se sin sobre su sus tras un una unos unas y ya fue son es sera "
    "como cual donde cuando muy tambien hoy ayer manana tras".split()
)

# Palabras con mayúscula que NO cuentan como "nombre propio distinto" al comparar títulos.
MAYUSCULAS_COMUNES = set(
    "gobierno provincia provincial estado ministerio ministro ministra legislatura municipio "
    "municipalidad chaco gobernador gobernadora poder judicial ejecutivo camara concejo policia "
    "banco nacion superior tribunal justicia diputados senado".split()
)

os.environ["TZ"] = "America/Argentina/Buenos_Aires"  # registros con hora de Argentina
if hasattr(time, "tzset"):
    time.tzset()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%d/%m %H:%M:%S")
log = logging.getLogger("bot")


# ---------------------------------------------------------------- descargas
def descargar(url):
    """Primero intenta como un Chrome real; si no se puede, con requests normal."""
    if navegador is not None:
        try:
            r = navegador.get(url, impersonate="chrome", timeout=25, headers={"Accept-Language": HEADERS["Accept-Language"]})
            if r.status_code < 400:
                return r
            error = f"HTTP {r.status_code}"
        except Exception as e:
            error = str(e)[:80]
        log.debug("curl_cffi falló en %s (%s), pruebo con requests", url, error)
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r


def dominio(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def normalizar(url):
    """Saca #anclas, parámetros de tracking y la barra final, para no repetir notas."""
    p = urlparse(url)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "amp"))])
    return urlunparse(("https", p.netloc.lower().removeprefix("www."), p.path.rstrip("/") or "/", "", query, ""))


def clave_titulo(titulo):
    """Versión simplificada del título, para no mandar la misma nota dos veces por fuentes distintas."""
    t = unicodedata.normalize("NFKD", titulo.lower())
    t = "".join(c for c in t if c.isalnum() or c == " ")
    t = " ".join(t.split())
    return "t:" + t[:90] if len(t) > 15 else None


def simplificar(texto):
    """minúsculas, sin tildes (conserva la ñ), sin signos."""
    t = texto.lower().replace("ñ", "\0")
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    t = t.replace("\0", "ñ")
    return " " + " ".join(re.sub(r"[^\wñ]+", " ", t).split()) + " "


def aparece(texto_simple, terminos):
    for term in terminos:
        prefijo = term.strip().endswith("*")
        t = simplificar(term).strip()
        if not t:
            continue
        if prefijo:
            if " " + t in texto_simple:
                return t + "*"
        elif " " + t + " " in texto_simple:
            return t
    return None


def pasa_filtro(titulo, resumen, estado, forzar=False):
    """Devuelve (True/False, motivo). Con forzar=True filtra aunque el filtro esté apagado."""
    f = estado["filtro"]
    if not forzar and not f.get("activo", FILTRO_ACTIVO):
        return True, "filtro apagado"
    texto = simplificar(f"{titulo} {resumen}")
    if (t := aparece(texto, EXCLUIR_SIEMPRE + f["excluir"])):
        return False, f"excluida por '{t}'"
    fuerte = aparece(texto, TERMINOS_FUERTES + LEGISLADORES + REFERENTES + INTENDENTES + f["incluir"])
    if (p := aparece(texto, POLICIALES)) and not fuerte:
        return False, f"policial ('{p}')"
    if (c := aparece(texto, CLIMA)) and not fuerte:
        return False, f"pronóstico/clima ('{c}')"
    if fuerte:
        return True, fuerte
    debil = aparece(texto, TERMINOS_DEBILES)
    if debil:
        if aparece(texto, CONTEXTO_CHACO):
            return True, debil
        if not aparece(texto, MARCAS_NACIONALES):
            return True, debil
        return False, "nacional"
    return False, "no es política provincial"


# ---------------------------------------------------------------- estado
def cargar_estado():
    estado = {}
    if os.path.exists(ARCHIVO_ESTADO):
        try:
            with open(ARCHIVO_ESTADO, encoding="utf-8") as f:
                estado = json.load(f)
        except Exception:
            estado = {}
    for k in ("vistos", "feeds", "fuentes_usadas", "fallos", "avisado"):
        estado.setdefault(k, {})
    estado.setdefault("filtro", {})
    estado["filtro"].setdefault("incluir", [])
    estado["filtro"].setdefault("excluir", [])
    estado.setdefault("telegram_offset", 0)
    estado.setdefault("para_resumen", [])               # notas enviadas, para el resumen de las 9
    estado.setdefault("registro_desde", time.time())    # desde cuándo el bot viene guardando notas
    return estado


def guardar_estado(estado):
    tmp = ARCHIVO_ESTADO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False)
    os.replace(tmp, ARCHIVO_ESTADO)


# ---------------------------------------------------------------- fuente 1: RSS
def leer_feed(url):
    d = feedparser.parse(descargar(url).content)
    return d if d.entries else None


def buscar_feed(nombre, cfg):
    candidatos = list(cfg.get("feeds", []))
    try:
        soup = BeautifulSoup(descargar(cfg["home"]).text, "html.parser")
        for link in soup.find_all("link", rel="alternate"):
            tipo = (link.get("type") or "").lower()
            href = link.get("href")
            if href and ("rss" in tipo or "atom" in tipo) and "comment" not in href.lower():
                candidatos.append(urljoin(cfg["home"], href))
    except Exception:
        pass
    candidatos += [urljoin(cfg["home"], r) for r in RUTAS_FEED]
    for c in dict.fromkeys(candidatos):
        try:
            if leer_feed(c):
                return c
        except Exception:
            continue
    return None


def notas_desde_feed(feed_url, home):
    d = leer_feed(feed_url)
    notas = []
    for e in reversed(d.entries if d else []):  # de la más vieja a la más nueva
        link = e.get("link")
        if link and dominio(link) == dominio(home):
            resumen = BeautifulSoup(e.get("summary") or "", "html.parser").get_text(" ", strip=True)[:500]
            notas.append((normalizar(link), link, (e.get("title") or "").strip(), resumen))
    return notas


# ---------------------------------------------------------------- fuente 2: portada
def parece_nota(url, home):
    if dominio(url) != dominio(home):
        return False
    ruta = urlparse(url).path.lower().strip("/")
    if not ruta or SEGMENTOS_NO_NOTA & set(ruta.split("/")):
        return False
    if ruta.endswith((".jpg", ".jpeg", ".png", ".pdf", ".webp", ".gif", ".xml", ".mp4")):
        return False
    ultimo = ruta.split("/")[-1]
    return ultimo.count("-") >= 3 or (any(ch.isdigit() for ch in ruta) and len(ruta) > 15 and "-" in ruta)


def notas_desde_portada(home):
    soup = BeautifulSoup(descargar(home).text, "html.parser")
    encontradas = {}
    for a in soup.find_all("a", href=True):
        link = urljoin(home, a["href"].strip())
        if not link.startswith("http") or not parece_nota(link, home):
            continue
        clave = normalizar(link)
        titulo = a.get_text(" ", strip=True) or a.get("title", "")
        if clave not in encontradas or len(titulo) > len(encontradas[clave][2]):
            encontradas[clave] = (clave, link.split("#")[0], titulo, "")
    return list(encontradas.values())


# ---------------------------------------------------------------- fuente 3: Google Noticias
def notas_desde_google(home):
    q = quote(f"site:{dominio(home)} when:1d")
    url = f"https://news.google.com/rss/search?q={q}&hl=es-419&gl=AR&ceid=AR:es-419"
    d = feedparser.parse(requests.get(url, headers=HEADERS, timeout=25).content)
    entradas = sorted(d.entries, key=lambda e: e.get("published_parsed") or time.gmtime(0))
    notas = []
    for e in entradas:
        titulo = (e.get("title") or "").strip()
        fuente = (e.get("source") or {}).get("title", "")
        if fuente and titulo.endswith(" - " + fuente):
            titulo = titulo[: -len(fuente) - 3]
        link = e.get("link")
        if link:
            notas.append((link, link, titulo, ""))
    return notas


def link_real_de_google(link_google):
    """Convierte el link de Google Noticias en el link original de la nota."""
    if googlenewsdecoder is None:
        return None
    try:
        r = googlenewsdecoder(link_google, interval=1, timeout=15)
        ok = r.get("success", r.get("status"))  # según la versión de la librería
        if ok and str(r.get("decoded_url", "")).startswith("http"):
            return r["decoded_url"]
        log.info("   no pude obtener el link original: %s", r.get("message", "")[:80])
    except Exception as e:
        log.info("   no pude obtener el link original: %s", str(e)[:80])
    return None


def titulo_de_la_nota(url):
    """Lee el título completo desde la página de la nota (og:title, o el <h1>)."""
    try:
        soup = BeautifulSoup(descargar(url).text, "html.parser")
    except Exception:
        return None
    for meta in (soup.find("meta", property="og:title"), soup.find("meta", attrs={"name": "twitter:title"})):
        if meta and meta.get("content", "").strip():
            return meta["content"].strip()
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(" ", strip=True)
    return None


def completar_desde_google(nombre, link_google, titulo_google):
    """Para notas leídas vía Google: devuelve (link original, título completo)."""
    link = link_real_de_google(link_google) or link_google
    titulo = None
    if link != link_google:
        titulo = titulo_de_la_nota(link)
    if titulo:
        # saca el nombre del diario si viene pegado al final: "Título | Diario Chaco"
        m = re.match(r"^(.*\S)\s+[|–—-]\s+([^|–—-]{2,40})$", titulo)
        if m:
            sufijo = simplificar(m.group(2))
            palabras_diario = set(simplificar(nombre + " " + dominio(link)).split()) - {"de", "del", "la", "com", "ar"}
            if any(p in sufijo.split() for p in palabras_diario):
                titulo = m.group(1)
    return link, (titulo or titulo_google)


# ---------------------------------------------------------------- Telegram
def enviar(texto, vista_previa=True):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    datos = {"chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML",
             "disable_web_page_preview": not vista_previa}
    for _ in range(5):
        try:
            r = requests.post(url, data=datos, timeout=20)
        except Exception as e:
            log.warning("No pude conectar con Telegram: %s", e)
            time.sleep(5)
            continue
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if not r.ok:
            log.error("Telegram respondió %s: %s", r.status_code, r.text[:200])
        return r.ok
    return False


def mostrar_chat_ids():
    r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=20).json()
    chats = {}
    for u in r.get("result", []):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name")
    if not chats:
        print("No encontré mensajes. Mandale un mensaje al bot y volvé a correr esto.")
    for cid, nombre in chats.items():
        print(f"Chat id: {cid}   ({nombre})")


AYUDA = (
    "<b>Comandos del filtro</b>\n"
    "/filtros – ver cómo está el filtro\n"
    "/agregar palabra – que siempre pasen las notas con esa palabra o nombre\n"
    "/excluir palabra – que nunca pasen las notas con esa palabra\n"
    "/quitar palabra – sacar una palabra que agregaste o excluiste\n"
    "/filtro_off – recibir todas las notas, sin filtrar\n"
    "/filtro_on – volver a filtrar\n"
    "/resumen – mandar ahora el resumen de Destacados (desde las 17 h)\n\n"
    "Se pueden poner varias palabras: /agregar Carim Peche\n"
    "Con * al final vale como comienzo de palabra: /excluir futbol*"
)


def texto_filtros(estado):
    f = estado["filtro"]
    activo = f.get("activo", FILTRO_ACTIVO)
    inc = ", ".join(f["incluir"]) or "(ninguna)"
    exc = ", ".join(EXCLUIR_SIEMPRE + f["excluir"])
    return (
        f"Filtro: <b>{'ACTIVADO' if activo else 'APAGADO'}</b>\n"
        "Pasan las notas de política provincial: Ejecutivo, Legislatura, Poder Judicial "
        "y empresas del Estado chaqueño.\n\n"
        f"<b>Palabras que agregaste:</b> {html.escape(inc)}\n"
        f"<b>Palabras excluidas:</b> {html.escape(exc)}\n\n/ayuda para ver los comandos"
    )


def procesar_comandos(estado):
    """Lee los mensajes que le mandaste al bot y aplica los comandos del filtro."""
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getUpdates",
            params={"offset": estado["telegram_offset"], "timeout": 0}, timeout=20,
        ).json()
    except Exception as e:
        log.debug("No pude leer comandos: %s", e)
        return
    f = estado["filtro"]
    for u in r.get("result", []):
        estado["telegram_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
            continue  # solo obedece a tu chat
        texto = (msg.get("text") or "").strip()
        if not texto.startswith("/"):
            continue
        comando, _, arg = texto.partition(" ")
        comando = comando.split("@")[0].lower()
        arg = arg.strip().lower()

        if comando in ("/start", "/ayuda", "/help"):
            enviar(AYUDA)
        elif comando == "/filtros":
            enviar(texto_filtros(estado))
        elif comando == "/resumen":
            ahora = datetime.now()
            desde = ahora.replace(hour=HORA_INICIO_RESUMEN, minute=0, second=0, microsecond=0)
            if ahora < desde:
                desde -= timedelta(days=1)
            enviar_resumen(estado, desde, ahora)
        elif comando == "/filtro_on":
            f["activo"] = True
            enviar("✅ Filtro activado.")
        elif comando == "/filtro_off":
            f["activo"] = False
            enviar("⏸ Filtro apagado: te llegan todas las notas.")
        elif comando in ("/agregar", "/excluir", "/quitar") and not arg:
            enviar(f"Escribí la palabra después del comando. Ej: {comando} Zdero")
        elif comando == "/agregar":
            if arg in f["excluir"]:
                f["excluir"].remove(arg)
            if arg not in f["incluir"]:
                f["incluir"].append(arg)
            enviar(f"✅ Agregué «{html.escape(arg)}». Las notas que la mencionen te van a llegar.")
        elif comando == "/excluir":
            if arg in f["incluir"]:
                f["incluir"].remove(arg)
            if arg not in f["excluir"]:
                f["excluir"].append(arg)
            enviar(f"🚫 Excluí «{html.escape(arg)}». Las notas que la mencionen no te van a llegar.")
        elif comando == "/quitar":
            quitado = False
            for lista in (f["incluir"], f["excluir"]):
                if arg in lista:
                    lista.remove(arg)
                    quitado = True
            enviar(f"🗑 Quité «{html.escape(arg)}»." if quitado else
                   f"«{html.escape(arg)}» no estaba entre las palabras que agregaste o excluiste.")
        else:
            enviar("No conozco ese comando. /ayuda para ver la lista.")
        log.info("Comando recibido: %s %s", comando, arg)
    guardar_estado(estado)


# ---------------------------------------------------------------- resumen diario
def guardar_para_resumen(estado, nombre, link, titulo, resumen):
    lista = estado["para_resumen"]
    lista.append({"t": time.time(), "diario": nombre, "link": link,
                  "titulo": titulo or link, "resumen": (resumen or "")[:200]})
    limite = time.time() - 3 * 86400  # guarda como máximo 3 días
    estado["para_resumen"] = [n for n in lista if n["t"] >= limite]


def seccion_de(nota):
    texto = simplificar(f"{nota['titulo']} {nota.get('resumen', '')}")
    return "politica" if aparece(texto, TERMINOS_POLITICA) else "gestion"


def palabras_clave(titulo):
    """Raíces (5 letras) de las palabras importantes: 'inauguró' e 'inauguraron' cuentan igual."""
    return {w[:5] for w in simplificar(titulo).split() if w not in PALABRAS_VACIAS and len(w) > 2}


def nombres_propios(titulo):
    """Palabras con mayúscula en medio del título (lugares, personas): 'Fontana', 'Zdero'."""
    if titulo.upper() == titulo:
        return set()
    nombres = set()
    for m in re.finditer(r"\w+", titulo):
        antes = titulo[:m.start()].rstrip()
        if not antes or antes[-1] in ':."“”«»¡¿!?-–—|(':
            continue  # comienzo de oración o de cita: la mayúscula no indica nombre propio
        palabra = m.group()
        w = simplificar(palabra).strip()
        if palabra[0].isupper() and w not in PALABRAS_VACIAS and w not in MAYUSCULAS_COMUNES:
            nombres.add(w)
    return nombres


def es_misma_noticia(a, b):
    """Decide si dos notas (de portales distintos) cuentan la misma noticia."""
    if normalizar(a["link"]) == normalizar(b["link"]):
        return True
    sa, sb = simplificar(a["titulo"]).strip(), simplificar(b["titulo"]).strip()
    parecido = SequenceMatcher(None, sa, sb).ratio()
    if parecido >= 0.9:
        return True
    ka, kb = palabras_clave(a["titulo"]), palabras_clave(b["titulo"])
    if not ka or not kb:
        return False
    comunes = len(ka & kb)
    if not (parecido >= 0.75 or (comunes >= 3 and comunes / min(len(ka), len(kb)) >= 0.6)):
        return False
    # Si cada título nombra un lugar o persona que el otro no (ej: "escuela en Fontana" y
    # "escuela en Charata"), son noticias distintas aunque se parezcan.
    solo_a = nombres_propios(a["titulo"]) - set(sb.split())
    solo_b = nombres_propios(b["titulo"]) - set(sa.split())
    return not (solo_a and solo_b)


def largo_visible(texto_html):
    return len(html.unescape(re.sub(r"<[^>]+>", "", texto_html)))


def armar_resumen(notas, desde, hasta, aviso=""):
    secciones = {"politica": [], "gestion": []}
    for n in notas:
        secciones[seccion_de(n)].append(n)

    def item(n, modo):
        titulo = n["titulo"]
        if modo == "recortado" and len(titulo) > 90:
            titulo = titulo[:87].rstrip() + "…"
        if modo == "links":
            return f"• {html.escape(titulo)}\n{html.escape(n['link'])}"
        return f'• <a href="{html.escape(n["link"], quote=True)}">{html.escape(titulo)}</a>'

    def texto(modo, pol, ges, sobran=0):
        sep = "\n\n" if modo == "links" else "\n"
        cuerpo_pol = sep.join(item(n, modo) for n in pol) or "<i>Sin novedades</i>"
        cuerpo_ges = sep.join(item(n, modo) for n in ges) or "<i>Sin novedades</i>"
        partes = [
            f"📰 <b>DESTACADOS {hasta:%d/%m/%Y} - CHACO</b>",
            f"<i>Notas del {desde:%d/%m %H:%M} al {hasta:%d/%m %H:%M}</i>" + (f"\n<i>{aviso}</i>" if aviso else ""),
            f"🗳 <b>SECCIÓN 1: POLÍTICA / ELECCIONES</b>\n\n{cuerpo_pol}",
            f"🏛 <b>SECCIÓN 2: GESTIÓN</b>\n\n{cuerpo_ges}",
        ]
        if sobran:
            partes.append(f"<i>…y {sobran} notas más que no entraron en el mensaje.</i>")
        return "\n\n".join(partes)

    pol, ges = secciones["politica"], secciones["gestion"]
    # 1° título + link a la vista; 2° si no entra, título con el link incorporado (se toca el título);
    # 3° títulos recortados. Siempre en UN solo mensaje.
    for modo in ("links", "con_link", "recortado"):
        t = texto(modo, pol, ges)
        if largo_visible(t) <= LARGO_MAXIMO_MENSAJE:
            return t
    # Último recurso (día muy cargado): se sacan las últimas notas de la sección más larga.
    pol, ges, sobran = list(pol), list(ges), 0
    while largo_visible(texto("recortado", pol, ges, sobran)) > LARGO_MAXIMO_MENSAJE and (pol or ges):
        (pol if len(pol) >= len(ges) else ges).pop()
        sobran += 1
    return texto("recortado", pol, ges, sobran)


def enviar_resumen(estado, desde, hasta):
    t0, t1 = desde.timestamp(), hasta.timestamp()
    notas = sorted((n for n in estado["para_resumen"] if t0 <= n["t"] <= t1), key=lambda n: n["t"])
    unicas = []
    for n in notas:  # se queda con la primera que apareció; las repetidas de otros portales se omiten
        if not any(es_misma_noticia(n, u) for u in unicas):
            unicas.append(n)
    aviso = ""
    if estado["registro_desde"] > t0:
        aviso = (f"⚠️ El bot empezó a registrar notas a las "
                 f"{datetime.fromtimestamp(estado['registro_desde']):%H:%M del %d/%m}: puede faltar alguna.")
    ok = enviar(armar_resumen(unicas, desde, hasta, aviso), vista_previa=False)
    log.info("Resumen Destacados: %d notas, %d repetidas omitidas, %s",
             len(unicas), len(notas) - len(unicas), "enviado" if ok else "NO se pudo enviar")
    return ok


# Envío de prueba, una sola vez. Se puede borrar después (o dejar: no vuelve a mandarse).
PRUEBA_RESUMEN = datetime(2026, 10, 6, 14, 30)


def prueba_si_corresponde(estado):
    ahora = datetime.now()
    if estado.get("prueba_enviada") or ahora.date() != PRUEBA_RESUMEN.date() or ahora < PRUEBA_RESUMEN:
        return
    desde = (ahora - timedelta(days=1)).replace(hour=HORA_INICIO_RESUMEN, minute=0, second=0, microsecond=0)
    if enviar_resumen(estado, desde, ahora):
        estado["prueba_enviada"] = True
        guardar_estado(estado)


def resumen_si_corresponde(estado):
    """Manda el resumen una vez por día, a partir de la HORA_RESUMEN (si el bot estuvo caído,
    lo manda apenas vuelve, siempre que sea antes del mediodía)."""
    ahora = datetime.now()
    hoy = ahora.strftime("%Y-%m-%d")
    if estado.get("ultimo_resumen") == hoy or not (HORA_RESUMEN <= ahora.hour < 12):
        return
    hora_envio = ahora.replace(hour=HORA_RESUMEN, minute=0, second=0, microsecond=0)
    if estado["registro_desde"] > hora_envio.timestamp():
        # el bot arrancó (o se redesplegó) después de las 9: el resumen de hoy ya salió
        # o no tiene datos, así que no se manda para no duplicarlo
        estado["ultimo_resumen"] = hoy
        return
    desde = (ahora - timedelta(days=1)).replace(hour=HORA_INICIO_RESUMEN, minute=0, second=0, microsecond=0)
    if enviar_resumen(estado, desde, ahora):
        estado["ultimo_resumen"] = hoy
        guardar_estado(estado)


# ---------------------------------------------------------------- ciclo
def obtener_notas(nombre, cfg, estado):
    """Devuelve (notas, fuente_usada). Prueba RSS → portada → Google Noticias."""
    home = cfg["home"]
    errores = []

    info = estado["feeds"].get(nombre)
    if info is None or (info.get("url") is None and time.time() - info.get("buscado", 0) > REBUSCAR_FEED_CADA):
        url = buscar_feed(nombre, cfg)
        estado["feeds"][nombre] = {"url": url, "buscado": time.time()}
        log.info("%s → %s", nombre, f"RSS: {url}" if url else "sin RSS accesible")
    feed = estado["feeds"][nombre]["url"]

    if feed:
        try:
            notas = notas_desde_feed(feed, home)
            if notas:
                return notas, "rss"
        except Exception as e:
            errores.append(f"RSS: {str(e)[:70]}")
    try:
        notas = notas_desde_portada(home)
        if notas:
            return notas, "portada"
        errores.append("portada: sin notas detectadas")
    except Exception as e:
        errores.append(f"portada: {str(e)[:70]}")
    try:
        notas = notas_desde_google(home)
        if notas:
            return notas, "google"
        errores.append("Google Noticias: sin notas de las últimas 24 h")
    except Exception as e:
        errores.append(f"Google Noticias: {str(e)[:70]}")
    raise RuntimeError(" | ".join(errores))


def revisar(nombre, cfg, estado):
    try:
        notas, fuente = obtener_notas(nombre, cfg, estado)
    except Exception as e:
        fallos = estado["fallos"].get(nombre, 0) + 1
        estado["fallos"][nombre] = fallos
        log.warning("%s: no pude leerlo (%d seguidas) → %s", nombre, fallos, e)
        if fallos == FALLOS_PARA_AVISAR and not estado["avisado"].get(nombre):
            enviar(f"⚠️ No estoy pudiendo leer <b>{html.escape(nombre)}</b> hace un rato. Sigo intentando y te aviso si se recupera.")
            estado["avisado"][nombre] = True
        return

    estado["fallos"][nombre] = 0
    if estado["avisado"].pop(nombre, None):
        enviar(f"✅ Volví a leer <b>{html.escape(nombre)}</b> con normalidad.")

    vistos_lista = estado["vistos"].setdefault(nombre, [])
    vistos = set(vistos_lista)
    usadas = estado["fuentes_usadas"].setdefault(nombre, [])
    primera_vez = fuente not in usadas  # primera vez con esta fuente: solo memorizar

    nuevas = []
    for clave, link, titulo, resumen in notas:
        ct = clave_titulo(titulo)
        if clave in vistos or (ct and ct in vistos):
            continue
        nuevas.append((clave, link, titulo, resumen, ct))

    enviadas = descartadas = 0
    for clave, link, titulo, resumen, ct in nuevas:
        if not primera_vez:
            pasa, motivo = pasa_filtro(titulo, resumen, estado)
            if not pasa:
                descartadas += 1
                log.info("   descartada (%s): %s", motivo, titulo[:80])
            else:
                if fuente == "google":
                    link, titulo = completar_desde_google(nombre, link, titulo)
                    clave_real = normalizar(link)
                    if clave_real in vistos:  # ya la habías recibido leyendo el diario directo
                        vistos_lista.append(clave)
                        vistos.add(clave)
                        continue
                    vistos_lista.append(clave_real)
                    vistos.add(clave_real)
                cabecera = f"🗞 <b>{html.escape(nombre)}</b>"
                texto = f"{cabecera}\n{html.escape(titulo)}\n{link}" if titulo else f"{cabecera}\n{link}"
                if not enviar(texto):
                    continue  # no se marca como vista: se reintenta en la próxima vuelta
                enviadas += 1
                if pasa_filtro(titulo, resumen, estado, forzar=True)[0]:
                    guardar_para_resumen(estado, nombre, link, titulo, resumen)
                time.sleep(1.2)
        vistos_lista.append(clave)
        vistos.add(clave)
        if ct:
            vistos_lista.append(ct)
            vistos.add(ct)

    if primera_vez:
        usadas.append(fuente)
        log.info("%s: primera lectura vía %s, %d notas memorizadas (no se envían)", nombre, fuente, len(nuevas))
    else:
        log.info("%s: %d notas leídas vía %s, %d nuevas enviadas, %d descartadas por el filtro",
                 nombre, len(notas), fuente, enviadas, descartadas)
    estado["vistos"][nombre] = vistos_lista[-MAX_VISTOS_POR_SITIO:]


def main():
    if not TOKEN:
        sys.exit("Falta TELEGRAM_TOKEN.")
    if "--chat-id" in sys.argv:
        mostrar_chat_ids()
        return
    if not CHAT_ID:
        sys.exit("Falta TELEGRAM_CHAT_ID. Corré: python main.py --chat-id")

    estado = cargar_estado()
    enviar("✅ Bot de noticias del Chaco activo. Te aviso las notas nuevas de política provincial.\n"
           "Escribí /ayuda para ver cómo ajustar el filtro.")
    log.info("Monitoreando %d portales cada %d segundos (modo navegador: %s)",
             len(PORTALES), INTERVALO, "sí" if navegador else "no, falta curl_cffi")

    while True:
        for nombre, cfg in PORTALES.items():
            procesar_comandos(estado)
            try:
                resumen_si_corresponde(estado)
                prueba_si_corresponde(estado)
            except Exception as e:
                log.warning("No pude armar el resumen diario: %s", e)
            try:
                revisar(nombre, cfg, estado)
            except Exception as e:
                log.warning("%s: error inesperado (%s). Sigo con el próximo.", nombre, e)
            guardar_estado(estado)
        time.sleep(INTERVALO + random.randint(0, 15))


if __name__ == "__main__":
    main()
