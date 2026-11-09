"""
app.py — Backend de Rucoy Tracker
Flask + BeautifulSoup para scraping de rucoyonline.com

Correcciones aplicadas (WORKER TIMEOUT fix):
  1. La carga de clanes NUNCA bloquea una petición HTTP. Corre en un hilo
     daemon y /api/guilds responde inmediatamente con cargando=true si aún
     no terminó.
  2. Timeouts explícitos (connect=5s, read=10s) en TODAS las peticiones.
  3. Manejo de 429 (rate-limit) y 403 con mensajes claros.
  4. Caché de clanes persistida en disco (guilds_cache.json) para sobrevivir
     reinicios de Railway sin re-scrapeaar.
  5. TTL de caché reducido a 5 minutos para datos frescos.
"""

import json
import os
import re
import time
import urllib.parse
import threading
import logging

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Configuración ─────────────────────────────────────────────────────────────
BASE_URL_GUILD     = "https://www.rucoyonline.com/guild/{}"
BASE_URL_GUILDS    = "https://www.rucoyonline.com/guilds"
BASE_URL_CHARACTER = "https://www.rucoyonline.com/characters/{}"
BASE_URL_HIGHSCORE = "https://www.rucoyonline.com/highscores/{skill}/{year}/{month}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Timeouts explícitos: (connect_timeout, read_timeout)
# connect: cuánto esperar para abrir la conexión TCP
# read:    cuánto esperar para recibir datos una vez conectado
TIMEOUT_CONNECT = 5
TIMEOUT_READ    = 10
TIMEOUT         = (TIMEOUT_CONNECT, TIMEOUT_READ)

ONLINE_TAG_TEXT = "Online"

# Archivo de caché en disco — Railway tiene sistema de archivos efímero
# pero sobrevive entre reinicios dentro del mismo deploy.
CACHE_FILE = os.path.join(os.path.dirname(__file__), "guilds_cache.json")
CACHE_TTL  = 5 * 60  # 5 minutos en segundos

# ── Estado de la caché de clanes ──────────────────────────────────────────────
# _guilds_ready: True cuando la lista está disponible para responder peticiones
# _guilds_lista: la lista en sí
# _guilds_ts:    timestamp unix de la última carga exitosa
# _loading:      True mientras el hilo de background está cargando
_guilds_lista: list[str] = []
_guilds_ts:    float     = 0.0
_guilds_ready: bool      = False
_loading:      bool      = False
_state_lock    = threading.Lock()   # solo protege escrituras de estado (instantáneo)


# ── Persistencia en disco ─────────────────────────────────────────────────────

def _guardar_cache_disco(nombres: list[str]) -> None:
    """Guarda la lista de clanes en guilds_cache.json."""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "guilds": nombres}, f, ensure_ascii=False)
        log.info("Caché guardada en disco (%d clanes)", len(nombres))
    except Exception as e:
        log.warning("No se pudo guardar caché en disco: %s", e)


def _cargar_cache_disco() -> list[str]:
    """
    Intenta cargar la caché desde disco.
    Devuelve la lista si el archivo existe y no ha expirado.
    """
    try:
        if not os.path.exists(CACHE_FILE):
            return []
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        edad = time.time() - data.get("ts", 0)
        if edad < CACHE_TTL:
            guilds = data.get("guilds", [])
            log.info("Caché cargada desde disco (%d clanes, %.0fs)", len(guilds), edad)
            return guilds
        log.info("Caché en disco expirada (%.0fs), ignorando", edad)
    except Exception as e:
        log.warning("Error leyendo caché de disco: %s", e)
    return []


# ── Scraping de clanes ────────────────────────────────────────────────────────

def _hacer_get(url: str, timeout=None) -> requests.Response | None:
    """
    Wrapper de requests.get con timeout explícito y manejo de errores comunes.
    Devuelve None en caso de error para que el caller lo gestione.
    """
    t = timeout or TIMEOUT
    try:
        resp = requests.get(url, headers=HEADERS, timeout=t)
        if resp.status_code == 429:
            log.warning("Rate-limit (429) en %s — pausa 10s", url)
            time.sleep(10)
            resp = requests.get(url, headers=HEADERS, timeout=t)
        if resp.status_code == 403:
            log.warning("Bloqueado (403) en %s", url)
            return None
        return resp
    except requests.exceptions.Timeout:
        log.warning("Timeout en %s", url)
        return None
    except requests.exceptions.ConnectionError:
        log.warning("ConnectionError en %s", url)
        return None
    except requests.exceptions.RequestException as e:
        log.warning("RequestException en %s: %s", url, e)
        return None


def _scraping_clanes() -> list[str]:
    """
    Descarga todas las páginas de /guilds y devuelve la lista de clanes.
    Corre en un hilo daemon — NUNCA dentro de una petición HTTP.
    """
    nombres: list[str] = []
    vistos:  set[str]  = set()
    pagina = 1

    while True:
        url = BASE_URL_GUILDS if pagina == 1 else f"{BASE_URL_GUILDS}?page={pagina}"
        resp = _hacer_get(url)
        if resp is None or resp.status_code != 200:
            log.warning("Página %d de clanes no disponible, deteniendo", pagina)
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        encontrados = 0
        for h4 in soup.find_all("h4"):
            enlace = h4.find("a", href=True)
            if enlace and enlace["href"].startswith("/guild/"):
                nombre = urllib.parse.unquote(enlace["href"][len("/guild/"):]).strip()
                if nombre and nombre not in vistos:
                    vistos.add(nombre)
                    nombres.append(nombre)
                    encontrados += 1

        if encontrados == 0:
            break

        pagina += 1
        time.sleep(0.4)  # pausa cortés entre páginas

    return sorted(nombres, key=lambda s: s.lower())


def _hilo_carga_clanes() -> None:
    """
    Hilo daemon que carga la lista de clanes en background.
    Al terminar actualiza el estado global y guarda en disco.
    Nunca bloquea peticiones HTTP.
    """
    global _guilds_lista, _guilds_ts, _guilds_ready, _loading

    with _state_lock:
        if _loading:
            return      # ya hay un hilo cargando, no duplicar
        _loading = True

    log.info("Iniciando carga de clanes en background…")
    try:
        nuevos = _scraping_clanes()
        if nuevos:
            with _state_lock:
                _guilds_lista = nuevos
                _guilds_ts    = time.time()
                _guilds_ready = True
            _guardar_cache_disco(nuevos)
            log.info("Clanes cargados: %d", len(nuevos))
        else:
            log.warning("Scraping de clanes devolvió lista vacía")
    except Exception as e:
        log.error("Error en hilo de carga de clanes: %s", e)
    finally:
        with _state_lock:
            _loading = False


def obtener_lista_clanes() -> list[str]:
    """Devuelve snapshot de la lista (lectura sin lock — Python GIL protege la asignación)."""
    return list(_guilds_lista)


def necesita_recarga() -> bool:
    """True si la caché está vacía o expirada."""
    return not _guilds_ready or (time.time() - _guilds_ts) > CACHE_TTL


def disparar_recarga_si_necesario() -> None:
    """
    Si la caché necesita recargarse y no hay un hilo corriendo,
    lanza uno nuevo en background. Retorna inmediatamente.
    """
    if necesita_recarga() and not _loading:
        t = threading.Thread(target=_hilo_carga_clanes, daemon=True)
        t.start()


# ── Scraping de clan ──────────────────────────────────────────────────────────

def obtener_miembros_clan(nombre_clan: str) -> dict:
    """Descarga la página del clan y retorna los miembros online con nivel."""
    nombre_codificado = urllib.parse.quote(nombre_clan.strip())
    url = BASE_URL_GUILD.format(nombre_codificado)

    resultado = {
        "clan": nombre_clan.strip(),
        "online": [], "total_online": 0,
        "total_miembros": 0, "error": None,
    }

    resp = _hacer_get(url)
    if resp is None:
        resultado["error"] = "El sitio no respondió. Intenta de nuevo."
        return resultado
    if resp.status_code == 404:
        resultado["error"] = f'El clan "{nombre_clan.strip()}" no existe.'
        return resultado
    if resp.status_code != 200:
        resultado["error"] = f"Error HTTP {resp.status_code} del sitio."
        return resultado

    soup = BeautifulSoup(resp.text, "html.parser")

    tabla = None
    for t in soup.find_all("table"):
        primera = t.find("tr")
        if primera:
            hdrs = [c.get_text(strip=True) for c in primera.find_all(["td", "th"])]
            if "Name" in hdrs and "Level" in hdrs:
                tabla = t
                break

    if tabla is None:
        resultado["error"] = "Tabla de miembros no encontrada. El clan puede estar vacío."
        return resultado

    miembros_online = []
    total = 0

    for fila in tabla.find_all("tr"):
        celdas = fila.find_all("td")
        if len(celdas) < 2:
            continue
        total += 1

        celda_nombre = celdas[0]
        esta_online  = any(
            s.get_text(strip=True) == ONLINE_TAG_TEXT
            for s in celda_nombre.find_all("span")
        )
        if not esta_online:
            continue

        enlace = celda_nombre.find("a")
        nombre = enlace.get_text(strip=True) if enlace else celda_nombre.get_text(strip=True)

        try:
            nivel = int(celdas[1].get_text(strip=True))
        except ValueError:
            nivel = 0

        miembros_online.append({"nombre": nombre, "nivel": nivel})

    miembros_online.sort(key=lambda x: x["nivel"], reverse=True)
    resultado["online"]         = miembros_online
    resultado["total_online"]   = len(miembros_online)
    resultado["total_miembros"] = total
    return resultado


# ── Skills ────────────────────────────────────────────────────────────────────

_MESES = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}
SKILLS = ["melee", "distance", "magic", "defense"]


def obtener_datos_personaje(nombre_jugador: str) -> dict | None:
    """Extrae born, clan, nivel, online y last_online de /characters/<nombre>."""
    url  = BASE_URL_CHARACTER.format(urllib.parse.quote(nombre_jugador.strip()))
    resp = _hacer_get(url)
    if resp is None or resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    born = clan = nivel = last_online = nombre_actual = None
    online = False

    for span in soup.find_all("span"):
        if span.get_text(strip=True) == ONLINE_TAG_TEXT:
            online = True
            break

    ETIQUETAS = {"Name", "Level", "Guild", "Title", "Last online", "Born"}

    for fila in soup.find_all("tr"):
        celdas = fila.find_all("td", recursive=False)
        if len(celdas) < 2:
            continue
        etiqueta = celdas[0].get_text(strip=True)
        if etiqueta not in ETIQUETAS:
            continue
        val = celdas[1]

        if etiqueta == "Name":
            nombre_actual = val.get_text(strip=True) or None
        elif etiqueta == "Level":
            try:
                nivel = int(val.get_text(strip=True))
            except ValueError:
                pass
        elif etiqueta == "Last online":
            last_online = val.get_text(strip=True) or None
        elif etiqueta == "Guild":
            a = val.find("a", href=True)
            if a and "/guild/" in a.get("href", ""):
                clan = a.get_text(strip=True)
            else:
                t = val.get_text(strip=True)
                if t and t != "-":
                    clan = t
        elif etiqueta == "Born":
            m = re.match(r"(\w+)\s+\d+,\s+(\d{4})", val.get_text(strip=True))
            if m:
                mes = _MESES.get(m.group(1).lower())
                if mes:
                    born = (int(m.group(2)), mes)

    # Fallbacks con búsqueda amplia
    if born is None:
        for td in soup.find_all("td"):
            if td.get_text(strip=True) == "Born":
                padre = td.parent
                if padre:
                    todos = padre.find_all("td")
                    idx = todos.index(td)
                    if idx + 1 < len(todos):
                        m = re.match(r"(\w+)\s+\d+,\s+(\d{4})",
                                     todos[idx+1].get_text(strip=True))
                        if m:
                            mes = _MESES.get(m.group(1).lower())
                            if mes:
                                born = (int(m.group(2)), mes)
                                break

    if clan is None:
        for td in soup.find_all("td"):
            if td.get_text(strip=True) == "Guild":
                padre = td.parent
                if padre:
                    todos = padre.find_all("td")
                    idx = todos.index(td)
                    if idx + 1 < len(todos):
                        a = todos[idx+1].find("a", href=True)
                        if a and "/guild/" in a.get("href", ""):
                            clan = a.get_text(strip=True)
                        else:
                            t = todos[idx+1].get_text(strip=True)
                            if t and t != "-":
                                clan = t
                        break

    if nivel is None:
        for td in soup.find_all("td"):
            if td.get_text(strip=True) == "Level":
                padre = td.parent
                if padre:
                    todos = padre.find_all("td")
                    idx = todos.index(td)
                    if idx + 1 < len(todos):
                        try:
                            nivel = int(todos[idx+1].get_text(strip=True))
                        except ValueError:
                            pass
                        break

    if born is None:
        return None

    return {"born": born, "clan": clan, "nivel": nivel,
            "online": online, "nombre_actual": nombre_actual,
            "last_online": last_online}


def buscar_skill_en_highscore(nombre: str, skill: str, anio: int, mes: int) -> int | None:
    url  = BASE_URL_HIGHSCORE.format(skill=skill, year=anio, month=mes)
    resp = _hacer_get(url)
    if resp is None or resp.status_code != 200:
        return None

    nombre_lower = nombre.strip().lower()
    for fila in BeautifulSoup(resp.text, "html.parser").find_all("tr"):
        celdas = fila.find_all("td")
        if len(celdas) < 3:
            continue
        a = celdas[1].find("a")
        if a and a.get_text(strip=True).lower() == nombre_lower:
            try:
                return int(celdas[2].get_text(strip=True))
            except ValueError:
                return None
    return None


def _meses_a_revisar(anio: int, mes: int, n: int = 3) -> list[tuple[int, int]]:
    res, a, m = [], anio, mes
    for _ in range(n):
        res.append((a, m))
        m += 1
        if m > 12:
            m, a = 1, a + 1
    return res


def obtener_skills(nombre_jugador: str) -> dict:
    nombre = nombre_jugador.strip()
    resultado: dict = {
        "nombre": nombre, "nombre_actual": None,
        "born": None, "clan": None, "nivel": None,
        "online": False, "last_online": None,
        "melee": None, "distance": None,
        "magic": None, "defense": None, "error": None,
    }

    datos = obtener_datos_personaje(nombre)
    if datos is None:
        resultado["error"] = (
            "No se pudo obtener los datos del jugador. "
            "Puede no existir o el sitio no respondió."
        )
        return resultado

    anio_nac, mes_nac = datos["born"]
    resultado.update({
        "born":          f"{anio_nac}/{mes_nac:02d}",
        "clan":          datos["clan"],
        "nivel":         datos.get("nivel"),
        "online":        datos["online"],
        "last_online":   datos.get("last_online"),
        "nombre_actual": datos.get("nombre_actual") or nombre,
    })

    periodos = _meses_a_revisar(anio_nac, mes_nac)
    for i, skill in enumerate(SKILLS):
        for j, (a, m) in enumerate(periodos):
            if i > 0 or j > 0:
                time.sleep(0.3)
            valor = buscar_skill_en_highscore(nombre, skill, a, m)
            if valor is not None:
                resultado[skill] = valor
                break

    return resultado


# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/guilds")
def api_guilds():
    """
    Responde INMEDIATAMENTE en todos los casos:
    - Si la lista aún se está cargando → {"cargando": true, "guilds": []}
    - Si ya está lista → filtra por ?q= y devuelve hasta 50 resultados
    En ambos casos dispara una recarga en background si la caché expiró.
    """
    # Disparar recarga si hace falta (no bloquea)
    disparar_recarga_si_necesario()

    if not _guilds_ready:
        # Todavía cargando — responder rápido para no bloquear el worker
        return jsonify({"guilds": [], "total": 0, "cargando": True})

    query  = request.args.get("q", "").strip().lower()
    clanes = obtener_lista_clanes()
    if query:
        clanes = [c for c in clanes if query in c.lower()]

    return jsonify({
        "guilds":   clanes[:50],
        "total":    len(clanes),
        "cargando": False,
    })


@app.route("/api/online")
def api_online():
    clanes = request.args.getlist("clan")
    if not clanes:
        return jsonify({"error": "Falta el parámetro ?clan=NombreClan"}), 400

    resultados = []
    for i, nombre in enumerate(clanes):
        if not nombre.strip():
            continue
        if i > 0:
            time.sleep(0.8)
        resultados.append(obtener_miembros_clan(nombre))

    return jsonify({"resultados": resultados, "timestamp": time.time()})


@app.route("/api/skills")
def api_skills():
    nombre = request.args.get("name", "").strip()
    if not nombre:
        return jsonify({"error": "Falta el parámetro ?name=NombreJugador"}), 400
    return jsonify(obtener_skills(nombre))


# ── Arranque ──────────────────────────────────────────────────────────────────
# Al importar el módulo (gunicorn o python app.py):
#   1. Intentar cargar desde disco (instantáneo si existe)
#   2. Si no hay caché válida en disco, lanzar hilo de scraping en background

_desde_disco = _cargar_cache_disco()
if _desde_disco:
    with _state_lock:
        _guilds_lista = _desde_disco
        _guilds_ts    = time.time()
        _guilds_ready = True
    log.info("Caché de clanes restaurada desde disco (%d clanes)", len(_desde_disco))
else:
    log.info("Sin caché en disco, lanzando scraping en background…")
    threading.Thread(target=_hilo_carga_clanes, daemon=True).start()


if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=False, threaded=True)
