import os
import re
import asyncio
import threading
import time
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ─────────────────────────────────────────────
# CONFIGURACIÓN DE ENTORNO
# ─────────────────────────────────────────────
API_ID         = int(os.getenv("API_ID", "0"))
API_HASH       = os.getenv("API_HASH", "")
PUBLIC_URL     = os.getenv("PUBLIC_URL", "").rstrip("/")
SESSION_STRING = os.getenv("SESSION_STRING", None)
PORT           = int(os.getenv("PORT", 8080))

# ─────────────────────────────────────────────
# BOT ÚNICO
# ─────────────────────────────────────────────
AZURA_BOT = "@AzuraSearchServices_bot"

# ─────────────────────────────────────────────
# TIEMPOS DE ESPERA
#   FIRST_RESPONSE_TIMEOUT : máximo para recibir el PRIMER mensaje del bot
#   SILENCE_TIMEOUT        : segundos de silencio tras el ÚLTIMO mensaje
#                            para considerar la respuesta completa
#   ABSOLUTE_TIMEOUT       : techo absoluto sin importar qué
# ─────────────────────────────────────────────
FIRST_RESPONSE_TIMEOUT = 40
SILENCE_TIMEOUT        = 4
ABSOLUTE_TIMEOUT       = 50

# ─────────────────────────────────────────────
# DIRECTORIO DE DESCARGAS
# ─────────────────────────────────────────────
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ═════════════════════════════════════════════
# DEDUPLICACIÓN DE PETICIONES EN VUELO
#
# Problema raíz: si el cliente HTTP hace retry o
# llegan dos peticiones idénticas en paralelo,
# se enviaba el mismo comando dos veces a Telegram,
# generando el ANTISPAM y resultados duplicados.
#
# Solución: un registro global (_in_flight) protegido
# por un Lock de threading. Si el mismo comando ya
# está siendo procesado:
#   → el segundo hilo espera el resultado del primero
#   → se devuelve el mismo resultado a ambos callers
#   → Telegram sólo recibe UN mensaje
# ═════════════════════════════════════════════
_in_flight: dict[str, dict] = {}
_in_flight_lock = threading.Lock()


def _get_or_create_slot(key: str) -> tuple[bool, dict]:
    """
    Devuelve (is_owner, slot).
    - is_owner=True  → este hilo debe ejecutar la consulta
    - is_owner=False → otro hilo ya la está ejecutando; esperar slot['event']
    """
    with _in_flight_lock:
        if key in _in_flight:
            return False, _in_flight[key]
        slot = {"event": threading.Event(), "result": None}
        _in_flight[key] = slot
        return True, slot


def _finish_slot(key: str, result: dict):
    """Almacena resultado, señala a los waiters y limpia el registro."""
    with _in_flight_lock:
        slot = _in_flight.get(key)
    if slot:
        slot["result"] = result
        slot["event"].set()
    with _in_flight_lock:
        _in_flight.pop(key, None)


def run_command_dedup(command: str) -> dict:
    """
    Punto de entrada para Flask.
    Garantiza que el mismo comando se ejecuta UNA SOLA VEZ
    aunque lleguen peticiones duplicadas o concurrentes.
    """
    key = command.strip().lower()
    is_owner, slot = _get_or_create_slot(key)

    if is_owner:
        try:
            result = _run_event_loop(command)
        except Exception as exc:
            result = {"status": "error", "message": str(exc)}
        finally:
            _finish_slot(key, result)
        return result
    else:
        # Esperar a que el owner termine (máx. ABSOLUTE_TIMEOUT + 5 s de margen)
        slot["event"].wait(timeout=ABSOLUTE_TIMEOUT + 5)
        return slot.get("result") or {
            "status": "error",
            "message": "Tiempo de espera agotado (petición duplicada).",
        }


def _run_event_loop(command: str) -> dict:
    """Crea un event loop propio y corre la corutina async (thread-safe)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(send_azura_command(command))
    finally:
        loop.close()


# ─────────────────────────────────────────────
# LIMPIAR CLAVE → snake_case ASCII
# ─────────────────────────────────────────────
def clean_key(raw: str) -> str:
    raw = re.sub(r'[^\w\s]', '', raw, flags=re.UNICODE)
    key = re.sub(r'\s+', '_', raw.strip().lower())
    key = re.sub(r'[^\x00-\x7F_]', '', key)
    key = re.sub(r'_+', '_', key).strip('_')
    return key


# ─────────────────────────────────────────────
# PARSER UNIVERSAL  (CLAVE: VALOR → dict)
# ─────────────────────────────────────────────
def universal_parser(raw_text: str) -> dict:
    if not raw_text:
        return {}

    parsed: dict[str, str] = {}
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or ':' not in line:
            continue
        idx       = line.index(':')
        key_raw   = line[:idx].strip()
        value_raw = line[idx + 1:].strip()
        if not key_raw or not value_raw:
            continue
        key = clean_key(key_raw)
        if not key:
            continue
        val = re.sub(r'\s+', ' ', value_raw).strip()
        if key in parsed:
            parsed[key] = f"{parsed[key]} | {val}"
        else:
            parsed[key] = val
    return parsed


# ─────────────────────────────────────────────
# CONSTRUIR RESPUESTA FINAL
# ─────────────────────────────────────────────
def build_response(messages: list, file_urls: list) -> dict:
    """
    Parsea cada mensaje de forma independiente y fusiona los resultados
    respetando el orden de llegada:
      - El orden de las claves sigue el orden en que aparecen en el primer mensaje.
      - Los valores de mensajes posteriores se concatenan con ' | ' en orden.
    """
    raw_parts: list[str] = []
    parsed_per_msg: list[dict] = []

    for m in messages:
        text = (m.get("message") or "").strip()
        if not text:
            continue
        raw_parts.append(text)
        parsed_per_msg.append(universal_parser(text))

    if not raw_parts:
        return {"status": "error", "message": "Sin respuesta del bot."}

    # Fusionar manteniendo el orden de primera aparición de cada clave
    merged: dict[str, str] = {}
    for msg_parsed in parsed_per_msg:
        for key, value in msg_parsed.items():
            if key in merged:
                merged[key] = f"{merged[key]} | {value}"
            else:
                merged[key] = value

    combined = "\n".join(raw_parts)
    response: dict = {"status": "success"}

    if merged:
        response["data"]        = merged
        response["raw_message"] = combined
    else:
        response["message"] = combined

    if file_urls:
        response.setdefault("data", {})
        response["data"]["urls"] = file_urls  # type: ignore[index]

    return response


# ═════════════════════════════════════════════
# NÚCLEO ASYNC — ENVÍO ÚNICO + ESPERA INTELIGENTE
# ═════════════════════════════════════════════
async def send_azura_command(command: str) -> dict:
    """
    Envía el comando UNA SOLA VEZ a @AzuraSearchServices_bot.

    Lógica de espera:
      1. Espera hasta FIRST_RESPONSE_TIMEOUT s para el PRIMER mensaje.
         → Si no llega nada: devuelve "No hay resultados para esta consulta".
      2. Tras el primer mensaje, espera SILENCE_TIMEOUT s de silencio
         después del ÚLTIMO mensaje para considerar la respuesta completa.
      3. En ningún caso supera ABSOLUTE_TIMEOUT s totales.

    ANTISPAM:
      Si el bot responde sólo con un mensaje de antispam se detecta
      y se devuelve el tiempo de espera para que el caller reintente.
    """
    if API_ID == 0 or not API_HASH or not SESSION_STRING:
        return {"status": "error", "message": "Credenciales de Telegram no configuradas."}

    client = None
    try:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            return {"status": "error", "message": "Sesión de Telegram no autorizada."}

        bot_entity        = await client.get_entity(AZURA_BOT)
        messages_received: list[dict] = []
        last_msg_time: list[float | None] = [None]

        @client.on(events.NewMessage(incoming=True))
        async def _handler(event):
            if event.sender_id != bot_entity.id:
                return
            raw = (event.raw_text or "").strip()
            messages_received.append({
                "message":       raw,
                "event_message": event.message,
            })
            last_msg_time[0] = time.time()
            print(f"[Azura] Msg #{len(messages_received)}: "
                  f"{'«' + raw[:60] + '»' if raw else '(solo media)'}")

        # ── ENVÍO ÚNICO ──────────────────────────────────────
        print(f"[Azura] → Enviando: {command}")
        await client.send_message(AZURA_BOT, command)
        start = time.time()

        # ── ESPERA INTELIGENTE ────────────────────────────────
        while True:
            elapsed = time.time() - start

            # Techo absoluto
            if elapsed >= ABSOLUTE_TIMEOUT:
                print("[Azura] Techo absoluto alcanzado.")
                break

            if last_msg_time[0] is None:
                # Sin primer mensaje aún
                if elapsed >= FIRST_RESPONSE_TIMEOUT:
                    print("[Azura] Timeout: sin respuesta del bot.")
                    break
            else:
                # Verificar si es SOLO un mensaje de ANTISPAM
                if _only_antispam(messages_received):
                    print("[Azura] Sólo mensaje ANTISPAM recibido.")
                    break

                # Silencio tras el último mensaje
                silence = time.time() - last_msg_time[0]
                if silence >= SILENCE_TIMEOUT:
                    print(f"[Azura] Silencio {silence:.1f}s → respuesta completa "
                          f"({len(messages_received)} msg(s)).")
                    break

            await asyncio.sleep(0.3)

        client.remove_event_handler(_handler)

        # ── Sin respuesta ─────────────────────────────────────
        if not messages_received:
            return {
                "status":  "error",
                "message": "No hay resultados para esta consulta.",
            }

        # ── Solo ANTISPAM ─────────────────────────────────────
        if _only_antispam(messages_received):
            wait_s = _extract_antispam_seconds(messages_received)
            msg = (
                f"ANTISPAM activo. Intenta de nuevo en {wait_s} segundos."
                if wait_s else
                "ANTISPAM activo. Espera unos segundos y reintenta."
            )
            return {
                "status":             "antispam",
                "message":            msg,
                "retry_after_seconds": wait_s,
            }

        # ── Descargar archivos adjuntos ───────────────────────
        file_urls: list[dict] = []
        for msg_obj in messages_received:
            ev_msg = msg_obj.get("event_message")
            if ev_msg and getattr(ev_msg, "media", None):
                try:
                    ext   = ".pdf" if "pdf" in str(ev_msg.media).lower() else ".jpg"
                    fname = f"{int(time.time())}_{ev_msg.id}{ext}"
                    path  = await client.download_media(
                        ev_msg, file=os.path.join(DOWNLOAD_DIR, fname)
                    )
                    if path:
                        file_urls.append({
                            "url":  f"{PUBLIC_URL}/files/{fname}",
                            "type": "document",
                        })
                        print(f"[Azura] Archivo: {fname}")
                except Exception as dl_err:
                    print(f"[Azura] Error descarga: {dl_err}")

        return build_response(messages_received, file_urls)

    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    finally:
        if client:
            await client.disconnect()


# ─────────────────────────────────────────────
# HELPERS ANTISPAM
# ─────────────────────────────────────────────
_ANTISPAM_RE = re.compile(
    r'antispam|debes esperar|esperar\s+\d+\s+segundo',
    re.IGNORECASE,
)

def _only_antispam(msgs: list[dict]) -> bool:
    """True si todos los mensajes con texto son sólo ANTISPAM."""
    text_msgs = [m for m in msgs if (m.get("message") or "").strip()]
    if not text_msgs:
        return False
    return all(_ANTISPAM_RE.search(m["message"]) for m in text_msgs)

def _extract_antispam_seconds(msgs: list[dict]) -> int | None:
    """Extrae el número de segundos del mensaje ANTISPAM, si existe."""
    for m in msgs:
        text = m.get("message") or ""
        match = re.search(r'(\d+)\s+segundo', text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


# ─────────────────────────────────────────────
# VALIDACIONES
# ─────────────────────────────────────────────
def validate_placa(placa: str) -> str | None:
    if not placa:
        return "Parámetro 'placa' requerido."
    if not re.match(r'^[A-Za-z0-9]{6,7}$', placa.strip()):
        return "La placa debe tener 6 o 7 caracteres alfanuméricos. Ej: ABC123"
    return None

def validate_placa_6(placa: str) -> str | None:
    if not placa:
        return "Parámetro 'placa' requerido."
    if not re.match(r'^[A-Za-z0-9]{6}$', placa.strip()):
        return "La placa debe tener exactamente 6 caracteres. Ej: ABC123"
    return None

def validate_dni(dni: str) -> str | None:
    if not dni:
        return "Parámetro 'dni' requerido."
    if not re.match(r'^\d{8}$', dni.strip()):
        return "El DNI debe tener exactamente 8 dígitos. Ej: 45454545"
    return None

def validate_doc(doc: str) -> str | None:
    if not doc:
        return "Parámetro 'dni' o 'carnet' requerido."
    if not re.match(r'^\d{8,9}$', doc.strip()):
        return "Ingrese DNI (8 dígitos) o Carnet de Extranjería (9 dígitos)."
    return None


# ─────────────────────────────────────────────
# APP FLASK
# ─────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

# Preservar el orden de inserción de los dicts en las respuestas JSON.
# Con JSON_SORT_KEYS=True (defecto en versiones antiguas de Flask) las claves
# se ordenan alfabéticamente, destruyendo el orden original de los mensajes.
app.config["JSON_SORT_KEYS"] = False


@app.route("/files/<path:filename>")
def serve_file(filename):
    return send_from_directory(DOWNLOAD_DIR, filename)


@app.route("/health")
def health():
    return jsonify({"status": "healthy", "bot": AZURA_BOT})


@app.route("/status")
def status():
    return jsonify({
        "status": "online",
        "bot":    AZURA_BOT,
        "timeouts": {
            "first_response_sec": FIRST_RESPONSE_TIMEOUT,
            "silence_sec":        SILENCE_TIMEOUT,
            "absolute_sec":       ABSOLUTE_TIMEOUT,
        },
        "endpoints": [
            "/placav?placa=ABC123",
            "/citv?placa=ABC123",
            "/revisiones?placa=ABC123",
            "/rvt?placa=ABC123",
            "/placab?placa=ABC123",
            "/licencia?dni=45454545",
            "/mtc?dni=45454545",
            "/mtc?carnet=002436285",
            "/papeletas?placa=ABC123",
            "/soat?placa=ABC123",
            "/placar?placa=ABC123",
        ],
    })


# ══════════════════════════════════════════════
# RUTAS — cada una valida y llama run_command_dedup
# ══════════════════════════════════════════════

@app.route("/placav")
def route_placav():
    placa = request.args.get("placa", "")
    err   = validate_placa(placa)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    return jsonify(run_command_dedup(f"/placav {placa.strip().upper()}"))


@app.route("/citv")
def route_citv():
    placa = request.args.get("placa", "")
    err   = validate_placa_6(placa)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    return jsonify(run_command_dedup(f"/citv {placa.strip().upper()}"))


@app.route("/revisiones")
def route_revisiones():
    placa = request.args.get("placa", "")
    err   = validate_placa_6(placa)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    return jsonify(run_command_dedup(f"/revisiones {placa.strip().upper()}"))


@app.route("/rvt")
def route_rvt():
    placa = request.args.get("placa", "")
    err   = validate_placa_6(placa)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    return jsonify(run_command_dedup(f"/rvt {placa.strip().upper()}"))


@app.route("/placab")
def route_placab():
    placa = request.args.get("placa", "")
    err   = validate_placa(placa)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    return jsonify(run_command_dedup(f"/placab {placa.strip().upper()}"))


@app.route("/licencia")
def route_licencia():
    dni = request.args.get("dni", "")
    err = validate_dni(dni)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    return jsonify(run_command_dedup(f"/licencia {dni.strip()}"))


@app.route("/mtc")
def route_mtc():
    doc = request.args.get("dni") or request.args.get("carnet", "")
    err = validate_doc(doc)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    return jsonify(run_command_dedup(f"/mtc {doc.strip()}"))


@app.route("/papeletas")
def route_papeletas():
    placa = request.args.get("placa", "")
    err   = validate_placa(placa)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    return jsonify(run_command_dedup(f"/papeletas {placa.strip().upper()}"))


@app.route("/soat")
def route_soat():
    placa = request.args.get("placa", "")
    err   = validate_placa(placa)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    return jsonify(run_command_dedup(f"/soat {placa.strip().upper()}"))


@app.route("/placar")
def route_placar():
    placa = request.args.get("placa", "")
    err   = validate_placa(placa)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    return jsonify(run_command_dedup(f"/placar {placa.strip().upper()}"))


# ─────────────────────────────────────────────
# PUNTO DE ENTRADA
# ─────────────────────────────────────────────
if __name__ == "__main__":
    # threaded=True es requerido para que la deduplicación funcione
    # correctamente: cada request corre en su propio hilo y puede
    # esperar el resultado del hilo "owner" sin bloquear el servidor.
    app.run(host="0.0.0.0", port=PORT, threaded=True)
