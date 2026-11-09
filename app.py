"""
app.py — Backend de Rucoy Tracker
Flask + BeautifulSoup para scraping de rucoyonline.com

Endpoints:
  GET /                           → sirve el frontend
  GET /api/guilds                 → lista de todos los clanes (para autocompletado)
  GET /api/online?clan=X[&clan=Y] → jugadores online por clan
  GET /api/skills?name=X          → skills de un jugador buscando en highscores
"""

import re
import time
import urllib.parse
import threading

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

# ── Configuración ─────────────────────────────────────────────────────────────
BASE_URL_GUILD      = "https://www.rucoyonline.com/guild/{}"
BASE_URL_GUILDS     = "https://www.rucoyonline.com/guilds"
BASE_URL_CHARACTER  = "https://www.rucoyonline.com/characters/{}"
BASE_URL_HIGHSCORE  = "https://www.rucoyonline.com/highscores/{skill}/{year}/{month}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

REQUEST_TIMEOUT = 12
ONLINE_TAG_TEXT = "Online"   # texto exacto del badge; cambiar aquí si el sitio lo renombra

# ── Caché de clanes ───────────────────────────────────────────────────────────
# Se carga una vez al arrancar el servidor y se refresca cada hora.
_cache_guilds: list[str] = []
_cache_guilds_ts: float  = 0.0
_CACHE_TTL = 3600  # segundos

_cache_lock   = threading.Lock()
_loading_lock = threading.Lock()   # evita que dos hilos carguen la lista a la vez
_is_loading   = False              # flag para saber si hay carga en curso


def _cargar_lista_clanes() -> list[str]:
    """
    Descarga TODAS las páginas de rucoyonline.com/guilds y extrae los nombres
    de todos los clanes. Paginación: /guilds?page=N, 50 clanes por página.
    """
    nombres: list[str] = []
    vistos:  set[str]  = set()
    pagina = 1

    while True:
        url = BASE_URL_GUILDS if pagina == 1 else f"{BASE_URL_GUILDS}?page={pagina}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                break
        except Exception:
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        encontrados_en_pagina = 0
        for h4 in soup.find_all("h4"):
            enlace = h4.find("a", href=True)
            if enlace and enlace["href"].startswith("/guild/"):
                nombre = urllib.parse.unquote(enlace["href"][len("/guild/"):]).strip()
                if nombre and nombre not in vistos:
                    vistos.add(nombre)
                    nombres.append(nombre)
                    encontrados_en_pagina += 1

        if encontrados_en_pagina == 0:
            break
        pagina += 1
        time.sleep(0.3)

    return sorted(nombres, key=lambda s: s.lower())


def obtener_lista_clanes() -> list[str]:
    """
    Devuelve la lista de clanes con caché TTL de 1 hora.

    FIX: La carga pesada (80 peticiones HTTP) se hace FUERA del lock para
    no bloquear a otros hilos que piden la lista mientras se carga.
    Solo el swap final de la caché usa el lock (operación instantánea).
    """
    global _cache_guilds, _cache_guilds_ts, _is_loading

    # Lectura rápida: si la caché está fresca, devolver sin lock
    if _cache_guilds and (time.time() - _cache_guilds_ts) < _CACHE_TTL:
        return list(_cache_guilds)

    # Evitar que dos hilos carguen simultáneamente
    with _loading_lock:
        # Re-verificar tras adquirir el lock (otro hilo pudo haberla cargado ya)
        if _cache_guilds and (time.time() - _cache_guilds_ts) < _CACHE_TTL:
            return list(_cache_guilds)

        # Carga pesada FUERA del cache_lock
        nuevos = _cargar_lista_clanes()

        if nuevos:  # solo actualizar si obtuvimos datos
            with _cache_lock:
                _cache_guilds    = nuevos
                _cache_guilds_ts = time.time()

        return list(_cache_guilds) if _cache_guilds else nuevos


# ── Scraping de un clan ───────────────────────────────────────────────────────

def obtener_miembros_clan(nombre_clan: str) -> dict:
    """
    Descarga la página del clan y extrae los miembros online con nivel.

    Retorna:
        {
          "clan": str,
          "online": [ {"nombre": str, "nivel": int}, ... ],
          "total_online": int,
          "total_miembros": int,
          "error": str | None
        }
    """
    nombre_codificado = urllib.parse.quote(nombre_clan.strip())
    url = BASE_URL_GUILD.format(nombre_codificado)

    resultado = {
        "clan": nombre_clan.strip(),
        "online": [],
        "total_online": 0,
        "total_miembros": 0,
        "error": None,
    }

    # ── Petición HTTP ─────────────────────────────────────────────────────────
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        resultado["error"] = "El sitio no respondió a tiempo (timeout)."
        return resultado
    except requests.exceptions.ConnectionError:
        resultado["error"] = "Sin conexión. Revisa tu internet."
        return resultado
    except requests.exceptions.RequestException as e:
        resultado["error"] = f"Error de red: {e}"
        return resultado

    if resp.status_code == 404:
        resultado["error"] = f'El clan "{nombre_clan.strip()}" no existe.'
        return resultado
    if resp.status_code != 200:
        resultado["error"] = f"HTTP {resp.status_code} al consultar el clan."
        return resultado

    # ── Parseo ────────────────────────────────────────────────────────────────
    soup = BeautifulSoup(resp.text, "html.parser")

    # Buscar la tabla usando td en el encabezado (el sitio NO usa <th>)
    tabla = None
    for t in soup.find_all("table"):
        primera_fila = t.find("tr")
        if primera_fila:
            celdas_hdr = [c.get_text(strip=True) for c in primera_fila.find_all(["td", "th"])]
            if "Name" in celdas_hdr and "Level" in celdas_hdr:
                tabla = t
                break

    if tabla is None:
        resultado["error"] = (
            "No se encontró la tabla de miembros. "
            "El clan puede no tener miembros o el sitio cambió."
        )
        return resultado

    filas = tabla.find_all("tr")
    miembros_online = []
    total = 0

    for fila in filas:
        celdas = fila.find_all("td")
        if len(celdas) < 2:
            continue  # fila de encabezado
        total += 1

        celda_nombre = celdas[0]

        # Detectar badge "Online" en spans (class="label-success") de la fila,
        # ignorando el <a> que contiene el nombre del jugador.
        esta_online = False
        for span in celda_nombre.find_all("span"):
            if span.get_text(strip=True) == ONLINE_TAG_TEXT:
                esta_online = True
                break

        if not esta_online:
            continue

        # Nombre: en el <a>
        enlace = celda_nombre.find("a")
        nombre = enlace.get_text(strip=True) if enlace else celda_nombre.get_text(strip=True)

        # Nivel: segunda celda
        try:
            nivel = int(celdas[1].get_text(strip=True))
        except ValueError:
            nivel = 0

        miembros_online.append({"nombre": nombre, "nivel": nivel})

    # Ordenar mayor → menor nivel
    miembros_online.sort(key=lambda x: x["nivel"], reverse=True)

    resultado["online"]         = miembros_online
    resultado["total_online"]   = len(miembros_online)
    resultado["total_miembros"] = total
    return resultado


# ── Skills: born date + highscores ───────────────────────────────────────────

# Mapa de nombre de mes en inglés → número
_MESES = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}

# Los 4 skills que existen en los highscores
SKILLS = ["melee", "distance", "magic", "defense"]


def obtener_datos_personaje(nombre_jugador: str) -> dict | None:
    """
    Consulta /characters/<nombre> y extrae en una sola petición:
      - born: (año, mes) como enteros
      - clan: nombre del clan o None
      - online: True/False

    Estructura HTML de la página de personaje de rucoyonline.com:
      La sección "Character Information" tiene filas <tr> con 2 <td>:
        <tr><td>Name</td><td>Ossor</td></tr>
        <tr><td>Level</td><td>195</td></tr>
        <tr><td>Guild</td><td>Wwwwwwwwwwwwwwwwwwwwwwwwm</td></tr>
        <tr><td>Title</td><td>...</td></tr>
        <tr><td>Last online</td><td>...</td></tr>
      Y fuera de esa tabla:
        <tr><td>Born</td><td>May 26, 2020</td></tr>

      El clan NO tiene <a> — es texto plano en el <td>.
      El badge Online es un <span> con texto exacto "Online".
    """
    url = BASE_URL_CHARACTER.format(urllib.parse.quote(nombre_jugador.strip()))
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None
    except Exception:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    born          = None
    clan          = None
    nivel         = None
    online        = False
    nombre_actual = None
    last_online   = None  # "currently online", "about 5 hours ago", etc.

    # ── Online: span con texto exacto "Online" ───────────────────────────────
    for span in soup.find_all("span"):
        if span.get_text(strip=True) == ONLINE_TAG_TEXT:
            online = True
            break

    # ── Datos del personaje: recorrer TODAS las filas del documento ──────────
    # Usamos un conjunto de etiquetas conocidas para no confundir con las
    # filas de la tabla de kills/deaths que tiene otro formato.
    ETIQUETAS_INFO = {"Name", "Level", "Guild", "Title", "Last online", "Born"}

    for fila in soup.find_all("tr"):
        celdas = fila.find_all("td", recursive=False)  # solo hijos directos
        if len(celdas) < 2:
            continue

        etiqueta = celdas[0].get_text(strip=True)
        if etiqueta not in ETIQUETAS_INFO:
            continue

        valor_td = celdas[1]

        if etiqueta == "Name":
            nombre_actual = valor_td.get_text(strip=True) or None

        elif etiqueta == "Level":
            try:
                nivel = int(valor_td.get_text(strip=True))
            except ValueError:
                pass

        elif etiqueta == "Last online":
            last_online = valor_td.get_text(strip=True) or None

        elif etiqueta == "Guild":
            # El clan viene como texto plano (sin <a>) en esta página
            enlace = valor_td.find("a", href=True)
            if enlace and "/guild/" in enlace.get("href", ""):
                clan = enlace.get_text(strip=True)
            else:
                texto = valor_td.get_text(strip=True)
                if texto and texto != "-":
                    clan = texto

        elif etiqueta == "Born":
            texto = valor_td.get_text(strip=True)
            m = re.match(r"(\w+)\s+\d+,\s+(\d{4})", texto)
            if m:
                mes_nombre = m.group(1).lower()
                anio       = int(m.group(2))
                mes        = _MESES.get(mes_nombre)
                if mes:
                    born = (anio, mes)

    # Si no encontramos Born con recursive=False (algunos parsers lo anidan),
    # intentamos con búsqueda más amplia sobre cualquier td del documento
    if born is None:
        for td in soup.find_all("td"):
            if td.get_text(strip=True) == "Born":
                # El valor está en el siguiente td del mismo tr padre
                padre = td.parent
                if padre:
                    todos = padre.find_all("td")
                    idx = todos.index(td)
                    if idx + 1 < len(todos):
                        texto = todos[idx + 1].get_text(strip=True)
                        m = re.match(r"(\w+)\s+\d+,\s+(\d{4})", texto)
                        if m:
                            mes_nombre = m.group(1).lower()
                            anio       = int(m.group(2))
                            mes        = _MESES.get(mes_nombre)
                            if mes:
                                born = (anio, mes)
                                break

    # Igual para Guild si aún no lo tenemos
    if clan is None:
        for td in soup.find_all("td"):
            if td.get_text(strip=True) == "Guild":
                padre = td.parent
                if padre:
                    todos = padre.find_all("td")
                    idx = todos.index(td)
                    if idx + 1 < len(todos):
                        valor = todos[idx + 1]
                        enlace = valor.find("a", href=True)
                        if enlace and "/guild/" in enlace.get("href", ""):
                            clan = enlace.get_text(strip=True)
                        else:
                            texto = valor.get_text(strip=True)
                            if texto and texto != "-":
                                clan = texto
                        break

    # Fallback para Level
    if nivel is None:
        for td in soup.find_all("td"):
            if td.get_text(strip=True) == "Level":
                padre = td.parent
                if padre:
                    todos = padre.find_all("td")
                    idx = todos.index(td)
                    if idx + 1 < len(todos):
                        try:
                            nivel = int(todos[idx + 1].get_text(strip=True))
                        except ValueError:
                            pass
                        break

    if born is None:
        return None

    return {"born": born, "clan": clan, "nivel": nivel, "online": online,
            "nombre_actual": nombre_actual, "last_online": last_online}


def obtener_born_date(nombre_jugador: str) -> tuple[int, int] | None:
    """Wrapper de compatibilidad — usa obtener_datos_personaje internamente."""
    datos = obtener_datos_personaje(nombre_jugador)
    if datos is None:
        return None
    return datos["born"]


def buscar_skill_en_highscore(nombre_jugador: str, skill: str, anio: int, mes: int) -> int | None:
    """
    Descarga /highscores/<skill>/<año>/<mes> y busca el nombre del jugador.
    Retorna el valor del skill si lo encuentra en el top 100, o None.
    """
    url = BASE_URL_HIGHSCORE.format(skill=skill, year=anio, month=mes)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None
    except Exception:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # La tabla tiene filas: Rank | Name | Skill_value
    # El nombre puede tener badges (Online, Supporter) en spans dentro del <td>
    # Buscamos cada fila y comparamos el texto del <a> (el nombre exacto)
    nombre_lower = nombre_jugador.strip().lower()

    for fila in soup.find_all("tr"):
        celdas = fila.find_all("td")
        if len(celdas) < 3:
            continue

        # La celda del nombre (índice 1) contiene un <a> con el nombre real
        celda_nombre = celdas[1]
        enlace = celda_nombre.find("a")
        if enlace and enlace.get_text(strip=True).lower() == nombre_lower:
            # Celda del valor del skill (índice 2)
            try:
                return int(celdas[2].get_text(strip=True))
            except ValueError:
                return None

    return None  # no está en el top 100 de ese skill/mes


def _meses_a_revisar(anio: int, mes: int, cantidad: int = 3) -> list[tuple[int, int]]:
    """
    Devuelve una lista de (año, mes) comenzando en (anio, mes) y avanzando
    'cantidad' meses consecutivos, manejando correctamente el cambio de año.

    Ejemplo: (2017, 11, 3) → [(2017,11), (2017,12), (2018,1)]
    """
    resultado = []
    a, m = anio, mes
    for _ in range(cantidad):
        resultado.append((a, m))
        m += 1
        if m > 12:
            m = 1
            a += 1
    return resultado


def obtener_skills(nombre_jugador: str) -> dict:
    """
    Proceso completo para obtener las 4 skills de un jugador.

    Estrategia de búsqueda ampliada:
      1. Obtiene el mes/año de nacimiento desde /characters/<nombre>.
      2. Para cada skill busca en el mes de nacimiento Y los 2 meses
         siguientes (3 páginas en total). Esto aumenta las posibilidades de
         encontrar al jugador porque los highscores agrupan por mes de
         creación del personaje y el mismo jugador puede aparecer en varios.
      3. Devuelve el primer valor encontrado por skill (el del mes más cercano
         al nacimiento).

    Retorna:
    {
      "nombre":    str,
      "born":      "YYYY/MM" | None,
      "melee":     int | None,
      "distance":  int | None,
      "magic":     int | None,
      "defense":   int | None,
      "born_mes":  str   (ej. "2017/09"),
      "error":     str | None
    }
    Valores None = jugador no está en top 100 en ninguno de los 3 meses.
    """
    nombre = nombre_jugador.strip()
    resultado: dict = {
        "nombre":        nombre,
        "nombre_actual": None,   # nombre actual si cambió de nombre
        "born":          None,
        "clan":          None,
        "nivel":         None,
        "online":        False,
        "last_online":   None,   # "currently online" / "about 5 hours ago" / etc.
        "melee":         None,
        "distance":      None,
        "magic":         None,
        "defense":       None,
        "error":         None,
    }

    # ── Paso 1: datos del personaje (born + clan + online) ───────────────────
    datos = obtener_datos_personaje(nombre)
    if datos is None:
        resultado["error"] = (
            "No se pudo obtener la fecha de nacimiento. "
            "El jugador puede no existir o el sitio no respondió."
        )
        return resultado

    anio_nac, mes_nac = datos["born"]
    resultado["born"]          = f"{anio_nac}/{mes_nac:02d}"
    resultado["clan"]          = datos["clan"]
    resultado["nivel"]         = datos.get("nivel")
    resultado["online"]        = datos["online"]
    resultado["last_online"]   = datos.get("last_online")
    # Guard: nombre_actual puede ser None si el parsing falló
    na = datos.get("nombre_actual")
    resultado["nombre_actual"] = na if na else nombre

    # ── Paso 2: buscar en 3 meses consecutivos por cada skill ───────────────
    # Generamos los 3 períodos a revisar una sola vez
    periodos = _meses_a_revisar(anio_nac, mes_nac, cantidad=3)

    peticion_num = 0  # contador para aplicar pausa cortés
    for skill in SKILLS:
        for (a, m) in periodos:
            if peticion_num > 0:
                time.sleep(0.3)  # pausa entre peticiones al mismo servidor
            peticion_num += 1

            valor = buscar_skill_en_highscore(nombre, skill, a, m)
            if valor is not None:
                resultado[skill] = valor
                break  # encontrado en este periodo → pasamos al siguiente skill

    return resultado


# ── App Flask ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/guilds")
def api_guilds():
    """
    Devuelve la lista completa de clanes para el autocompletado.
    Opcionalmente filtra con ?q=texto (búsqueda parcial, sin distinción de mayús.)

    Respuesta: { "guilds": [...], "total": int, "cargando": bool }
    """
    query  = request.args.get("q", "").strip().lower()
    clanes = obtener_lista_clanes()

    if query:
        clanes = [c for c in clanes if query in c.lower()]

    # Limitar a 50 sugerencias para no saturar el dropdown
    return jsonify({
        "guilds":   clanes[:50],
        "total":    len(clanes),
        "cargando": len(_cache_guilds) == 0
    })


@app.route("/api/online")
def api_online():
    """
    Devuelve jugadores online de uno o más clanes.
    ?clan=X  puede repetirse para varios clanes.

    Respuesta:
    {
      "resultados": [ { clan, online, total_online, total_miembros, error }, ... ],
      "timestamp": float
    }
    """
    clanes = request.args.getlist("clan")
    if not clanes:
        return jsonify({"error": "Falta el parámetro ?clan=NombreClan"}), 400

    resultados = []
    for i, nombre in enumerate(clanes):
        if not nombre.strip():
            continue
        if i > 0:
            time.sleep(0.8)  # pausa cortés entre peticiones al mismo sitio
        resultados.append(obtener_miembros_clan(nombre))

    return jsonify({"resultados": resultados, "timestamp": time.time()})


@app.route("/api/skills")
def api_skills():
    """
    Devuelve las 4 skills de un jugador consultando su born date y highscores.
    ?name=NombreJugador

    Respuesta:
    {
      "nombre":   str,
      "born":     "YYYY/MM",
      "melee":    int | null,
      "distance": int | null,
      "magic":    int | null,
      "defense":  int | null,
      "error":    str | null
    }
    """
    nombre = request.args.get("name", "").strip()
    if not nombre:
        return jsonify({"error": "Falta el parámetro ?name=NombreJugador"}), 400

    return jsonify(obtener_skills(nombre))


# ── Precarga de clanes al arrancar ────────────────────────────────────────────
def _precarga():
    """Carga la lista de clanes en background para que el primer autocompletado
    sea instantáneo."""
    obtener_lista_clanes()

threading.Thread(target=_precarga, daemon=True).start()


if __name__ == "__main__":
    # Para desarrollo local solamente.
    # En producción (Railway/Render) gunicorn arranca la app directamente
    # sin pasar por este bloque.
    app.run(debug=True, port=5000, use_reloader=False, threaded=True)
