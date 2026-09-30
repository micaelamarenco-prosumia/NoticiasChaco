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
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode, quote

import feedparser
import requests
from bs4 import BeautifulSoup

try:  # imita a un navegador Chrome real: esquiva muchos bloqueos anti-bots
    from curl_cffi import requests as navegador
except ImportError:
    navegador = None

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
EXCLUIR_SIEMPRE = ["horoscopo", "quiniela", "receta", "farandula"]

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
        term = simplificar(term).strip()
        if not term:
            continue
        if term.endswith("*"):
            if " " + term[:-1] in texto_simple:
                return term
        elif " " + term + " " in texto_simple:
            return term
    return None


def pasa_filtro(titulo, resumen, estado):
    """Devuelve (True/False, motivo)."""
    f = estado["filtro"]
    if not f.get("activo", FILTRO_ACTIVO):
        return True, "filtro apagado"
    texto = simplificar(f"{titulo} {resumen}")
    if (t := aparece(texto, EXCLUIR_SIEMPRE + f["excluir"])):
        return False, f"excluida por '{t}'"
    if (t := aparece(texto, TERMINOS_FUERTES + LEGISLADORES + REFERENTES + INTENDENTES + f["incluir"])):
        return True, t
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


# ---------------------------------------------------------------- Telegram
def enviar(texto):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    datos = {"chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": False}
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
    "/filtro_on – volver a filtrar\n\n"
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
                cabecera = f"🗞 <b>{html.escape(nombre)}</b>"
                texto = f"{cabecera}\n{html.escape(titulo)}\n{link}" if titulo else f"{cabecera}\n{link}"
                if not enviar(texto):
                    continue  # no se marca como vista: se reintenta en la próxima vuelta
                enviadas += 1
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
                revisar(nombre, cfg, estado)
            except Exception as e:
                log.warning("%s: error inesperado (%s). Sigo con el próximo.", nombre, e)
            guardar_estado(estado)
        time.sleep(INTERVALO + random.randint(0, 15))


if __name__ == "__main__":
    main()
