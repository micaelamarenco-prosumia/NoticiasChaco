"""
Bot de Telegram: avisa cada noticia nueva publicada en portales del Chaco.

Cómo funciona:
- Para cada portal busca su feed RSS (la forma más confiable de detectar notas nuevas).
- Si el portal no tiene RSS, lee la portada y detecta los links que parecen notas.
- La primera vez que ve un portal solo "memoriza" lo que ya está publicado (no te inunda).
- Desde ahí, cada nota nueva se envía al chat de Telegram configurado.

Configuración (variables de entorno):
  TELEGRAM_TOKEN      Token que te da @BotFather
  TELEGRAM_CHAT_ID    Tu chat id (o el de un grupo/canal)
  INTERVALO_SEGUNDOS  Cada cuánto revisa (por defecto 90)

Para averiguar tu chat id: mandale cualquier mensaje al bot y corré
  python main.py --chat-id
"""

import html
import json
import logging
import os
import random
import sys
import time
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import feedparser
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- portales
PORTALES = {
    "Diario TAG": "https://www.diariotag.com/",
    "Primera Línea": "https://diarioprimeralinea.com.ar/",
    "La Voz del Chaco": "https://www.diariolavozdelchaco.com/",
    "Data Chaco": "https://www.datachaco.com/",
    "Chaco Día por Día": "https://chacodiapordia.com/",
    "Diario Norte": "https://www.diarionorte.com/",
    "Diario Chaco": "https://www.diariochaco.com/",
}

# ---------------------------------------------------------------- config
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
INTERVALO = int(os.getenv("INTERVALO_SEGUNDOS", "90"))
ARCHIVO_ESTADO = os.getenv("ARCHIVO_ESTADO", "estado_bot.json")
MAX_VISTOS_POR_SITIO = 1000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9",
}

RUTAS_FEED = ["feed/", "rss/", "feed", "rss", "rss.xml", "feed.xml", "index.xml", "?format=feed&type=rss"]
PALABRAS_NO_NOTA = (
    "categoria", "category", "tag", "tags", "seccion", "author", "autor",
    "page", "pagina", "buscar", "search", "contacto", "wp-admin", "wp-login",
    "login", "registro", "suscrib", "publicidad", "staff", "politica-de-privacidad",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%d/%m %H:%M:%S",
)
log = logging.getLogger("bot")


# ---------------------------------------------------------------- utilidades
def descargar(url):
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r


def normalizar(url):
    """Saca #anclas, parámetros de tracking y la barra final, para no repetir notas."""
    p = urlparse(url)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "amp"))])
    ruta = p.path.rstrip("/") or "/"
    host = p.netloc.lower().replace("www.", "")
    return urlunparse(("https", host, ruta, "", query, ""))


def mismo_dominio(url, home):
    return urlparse(url).netloc.lower().replace("www.", "") == urlparse(home).netloc.lower().replace("www.", "")


def cargar_estado():
    if os.path.exists(ARCHIVO_ESTADO):
        with open(ARCHIVO_ESTADO, encoding="utf-8") as f:
            return json.load(f)
    return {"vistos": {}, "feeds": {}}


def guardar_estado(estado):
    tmp = ARCHIVO_ESTADO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ARCHIVO_ESTADO)


# ---------------------------------------------------------------- RSS
def feed_valido(url):
    try:
        d = feedparser.parse(descargar(url).content)
        return len(d.entries) > 0
    except Exception:
        return False


def buscar_feed(home):
    """Busca el RSS del portal: primero en el HTML de la portada, después en rutas típicas."""
    candidatos = []
    try:
        soup = BeautifulSoup(descargar(home).text, "html.parser")
        for link in soup.find_all("link", rel="alternate"):
            tipo = (link.get("type") or "").lower()
            href = link.get("href")
            if href and ("rss" in tipo or "atom" in tipo) and "comment" not in href.lower():
                candidatos.append(urljoin(home, href))
    except Exception as e:
        log.warning("No pude leer la portada de %s: %s", home, e)
    candidatos += [urljoin(home, r) for r in RUTAS_FEED]

    for c in dict.fromkeys(candidatos):  # sin duplicados, respetando el orden
        if feed_valido(c):
            return c
    return None


def notas_desde_feed(feed_url, home):
    d = feedparser.parse(descargar(feed_url).content)
    notas = []
    for e in reversed(d.entries):  # de la más vieja a la más nueva
        link = e.get("link")
        if link and mismo_dominio(link, home):
            notas.append((normalizar(link), link, (e.get("title") or "").strip()))
    return notas


# ---------------------------------------------------------------- portada
def parece_nota(url, home):
    if not mismo_dominio(url, home):
        return False
    ruta = urlparse(url).path.lower().strip("/")
    if not ruta or any(p in ruta.split("/") or ruta.startswith(p) for p in PALABRAS_NO_NOTA):
        return False
    if ruta.endswith((".jpg", ".png", ".pdf", ".webp", ".gif", ".xml")):
        return False
    ultimo = ruta.split("/")[-1]
    # Las notas suelen tener un "slug" largo con guiones, o un id numérico
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
        # Nos quedamos con el texto más largo que apunte a esa nota (suele ser el título)
        if clave not in encontradas or len(titulo) > len(encontradas[clave][2]):
            encontradas[clave] = (clave, link.split("#")[0], titulo)
    return list(encontradas.values())


# ---------------------------------------------------------------- Telegram
def enviar(texto):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    datos = {"chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": False}
    for _ in range(5):
        r = requests.post(url, data=datos, timeout=20)
        if r.status_code == 429:  # Telegram pide esperar
            espera = r.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(espera + 1)
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
        print("No encontré mensajes. Mandale un mensaje al bot (o agregalo al grupo/canal) y volvé a correr esto.")
    for cid, nombre in chats.items():
        print(f"Chat id: {cid}   ({nombre})")


# ---------------------------------------------------------------- ciclo
def revisar(nombre, home, estado):
    vistos = estado["vistos"].get(nombre)
    primera_vez = vistos is None
    vistos = set(vistos or [])

    if nombre not in estado["feeds"]:
        estado["feeds"][nombre] = buscar_feed(home)
        log.info("%s → %s", nombre, f"RSS: {estado['feeds'][nombre]}" if estado["feeds"][nombre] else "sin RSS, leo la portada")

    notas = []
    feed = estado["feeds"][nombre]
    if feed:
        try:
            notas = notas_desde_feed(feed, home)
        except Exception as e:
            log.warning("%s: falló el RSS (%s), pruebo con la portada", nombre, e)
    if not notas:
        notas = notas_desde_portada(home)

    lista = list(estado["vistos"].get(nombre, []))
    nuevas = [n for n in notas if n[0] not in vistos]

    if primera_vez:
        log.info("%s: %d notas actuales memorizadas (no se envían)", nombre, len(nuevas))
        lista += [n[0] for n in nuevas]
    else:
        for clave, link, titulo in nuevas:
            cabecera = f"🗞 <b>{html.escape(nombre)}</b>"
            texto = f"{cabecera}\n{html.escape(titulo)}\n{link}" if titulo else f"{cabecera}\n{link}"
            if enviar(texto):  # si falla, no se marca como vista y se reintenta en la próxima vuelta
                log.info("Enviada: %s | %s", nombre, titulo[:70])
                lista.append(clave)
                time.sleep(1.2)

    # Guardamos solo las más recientes para que el archivo no crezca sin límite
    estado["vistos"][nombre] = lista[-MAX_VISTOS_POR_SITIO:]


def main():
    if not TOKEN:
        sys.exit("Falta TELEGRAM_TOKEN.")
    if "--chat-id" in sys.argv:
        mostrar_chat_ids()
        return
    if not CHAT_ID:
        sys.exit("Falta TELEGRAM_CHAT_ID. Corré: python main.py --chat-id")

    estado = cargar_estado()
    enviar("✅ Bot de noticias del Chaco activo. Te aviso cada nota nueva.")
    log.info("Monitoreando %d portales cada %d segundos", len(PORTALES), INTERVALO)

    while True:
        for nombre, home in PORTALES.items():
            try:
                revisar(nombre, home, estado)
            except Exception as e:
                log.warning("%s: error (%s). Sigo con el próximo.", nombre, e)
            guardar_estado(estado)
        time.sleep(INTERVALO + random.randint(0, 15))


if __name__ == "__main__":
    main()
