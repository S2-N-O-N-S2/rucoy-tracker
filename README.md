# Rucoy Tracker 🎮

Muestra en tiempo real los jugadores **conectados** (online) de uno o varios clanes de **Rucoy Online**, junto con su nivel, ordenados de mayor a menor.

---

## Estructura del proyecto

```
rucoy-tracker/
├── app.py                  ← Servidor backend (Flask + scraping)
├── requirements.txt        ← Lista de dependencias Python
├── templates/
│   └── index.html          ← Interfaz web (HTML + CSS + JS)
└── static/                 ← Carpeta para recursos estáticos (vacía por ahora)
```

---

## Cómo funciona (resumen técnico)

```
Navegador  ──fetch──▶  Flask (app.py)  ──requests──▶  rucoyonline.com
                            │
                     BeautifulSoup
                     (parsea el HTML)
                            │
                      JSON con jugadores
                      online y niveles
           ◀──JSON──────────┘
```

**¿Por qué un backend?**
El navegador no puede pedir `rucoyonline.com` directamente porque ese sitio no permite
solicitudes desde otros dominios (política CORS). Flask actúa como intermediario:
hace la petición desde el servidor Python (sin restricciones CORS) y devuelve el
resultado ya procesado al navegador como JSON.

---

## Instalación paso a paso en Windows

### Requisitos previos

- **Python 3.10 o superior** instalado.
  Verificar con: `python --version`
  Descargar en: https://www.python.org/downloads/

### Paso 1 — Abrir una terminal en la carpeta del proyecto

```powershell
cd "C:\Users\SrAno\Desktop\Huntear247\rucoy-tracker"
```

### Paso 2 — Crear un entorno virtual (recomendado)

Un entorno virtual aísla las dependencias del proyecto para no mezclarlas con
otras instalaciones de Python en tu PC.

```powershell
python -m venv venv
```

Activarlo:

```powershell
.\venv\Scripts\Activate.ps1
```

> Si aparece un error de permisos en PowerShell, ejecuta primero:
> `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

Cuando el entorno está activo, el prompt mostrará `(venv)` al inicio.

### Paso 3 — Instalar las dependencias

```powershell
pip install -r requirements.txt
```

Esto instala Flask, BeautifulSoup, requests y las demás librerías.

### Paso 4 — Ejecutar el servidor

```powershell
python app.py
```

Verás algo como:

```
 * Running on http://127.0.0.1:5000
 * Debug mode: on
```

### Paso 5 — Abrir la aplicación

Abre tu navegador y ve a:

```
http://localhost:5000
```

¡Ya está corriendo! Escribe el nombre de un clan (por ejemplo `Celestial Oath`)
y pulsa **Actualizar**.

---

## Uso

- Escribe uno o varios clanes, uno por línea o separados por coma.
- Pulsa **Actualizar** (o `Ctrl + Enter` en el cuadro de texto).
- Para actualización automática, activa el checkbox **Auto** y elige el intervalo (1–5 minutos).
- Los jugadores aparecen ordenados de mayor a menor nivel.

---

## Solución de problemas comunes

| Problema | Causa probable | Solución |
|---|---|---|
| `ModuleNotFoundError: flask` | Dependencias no instaladas | Ejecutar `pip install -r requirements.txt` con el venv activo |
| La página no carga en el navegador | El servidor no está corriendo | Ejecutar `python app.py` y dejar la terminal abierta |
| "El clan no existe" | Nombre mal escrito | Verificar el nombre en `rucoyonline.com/guild/NombreClan` |
| "No se pudo conectar" | Sin internet o el sitio caído | Verificar conexión a internet |
| `lxml` falla al instalar | Falta compilador C en Windows | Instalar con `pip install lxml --only-binary :all:` |

---

## Detener el servidor

En la terminal donde corre `app.py`, presiona `Ctrl + C`.

Para desactivar el entorno virtual:

```powershell
deactivate
```
