import os
import time
import asyncio
import queue
from pathlib import Path

from google import genai
from google.genai import types
import sounddevice as sd


MODEL = "gemini-3.1-flash-live-preview"

PROMPTS_PATH = Path(__file__).resolve().with_name("prompts") / "system_base.md"


def _extract_fenced_block(md_text, heading_marker):
    """Saca el contenido del primer bloque ```...``` que aparece después de heading_marker."""
    idx = md_text.index(heading_marker)
    start = md_text.index("```", idx) + 3
    end = md_text.index("```", start)
    return md_text[start:end].strip("\n")


def build_test_system_instruction():
    """
    Carga el prompt real de prompts/system_base.md y rellena los
    placeholders con un cliente de prueba hardcodeado, para poder
    escuchar el tono/reglas reales por mic sin tener aún el builder
    de producción (core/system_instruction.py, todavía no existe).
    """
    md_text = PROMPTS_PATH.read_text(encoding="utf-8")

    prompt = _extract_fenced_block(md_text, "## PROMPT (voz)")
    bloque_preventiva = _extract_fenced_block(
        md_text, "**PREVENTIVA (T-3, la cuota no ha vencido):**"
    )

    replacements = {
        "{{tratamiento}}": "don",
        "{{nombre}}": "Carlos",
        "{{edad}}": "52",
        "{{producto}}": "préstamo personal",
        "{{monto_cuota}}": "ciento veinticinco dólares",
        "{{fecha_vencimiento}}": "el lunes quince de septiembre",
        "{{dias_para_vencer}}": "faltan tres días",
        "{{cuota_actual}}": "15",
        "{{cuotas_totales}}": "36",
        "{{historial}}": "bueno, catorce cuotas al día",
        "{{motivo_riesgo}}": (
            "la cuota vence pronto y queremos confirmar que podrá pagar a tiempo"
        ),
        "{{memoria}}": "sin gestiones anteriores",
        "{{registro}}": "neutral",
        "{{tratamiento_defecto}}": "usted",
        "{{fecha_hoy}}": "viernes doce de septiembre",
        "{{etapa}}": "PREVENTIVA",
        "{{bloque_etapa}}": bloque_preventiva,
    }

    for placeholder, value in replacements.items():
        prompt = prompt.replace(placeholder, value)

    return prompt


def resolve_api_key():
    """Resolve the Gemini API key from the environment or a local .env file."""
    for key_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        key_value = os.getenv(key_name)
        if key_value:
            return key_value

    dotenv_path = Path(__file__).resolve().with_name(".env")
    if dotenv_path.exists():
        for line in dotenv_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue

            key_part, value_part = [part.strip() for part in stripped.split("=", 1)]
            if key_part in {"GEMINI_API_KEY", "GOOGLE_API_KEY"}:
                value_part = value_part.strip().strip("\"'")
                os.environ[key_part] = value_part
                return value_part

    raise RuntimeError(
        "No existe GEMINI_API_KEY/GOOGLE_API_KEY en el entorno ni en el archivo .env. "
        "Copia .env.example a .env y agrega tu clave antes de ejecutar el script."
    )

INPUT_RATE = 16000
CHANNELS = 1
BLOCKSIZE = 1024

audio_queue = queue.Queue()

# Control de turnos
send_audio = asyncio.Event()
stop_session = asyncio.Event()

# Diagnóstico: para saber cuánto tiempo pasó sin tráfico real si el
# socket vuelve a caerse (ver test_mic.py:120 para el fix de ping_timeout).
_start = time.monotonic()
_last_traffic = _start


def log(msg):
    print(f"[{time.monotonic() - _start:7.2f}s] {msg}", flush=True)


_chunk_counter = 0
_last_callback_ts = None


def audio_callback(indata, frames, time_info, status):
    global _chunk_counter, _last_callback_ts
    if status:
        log(f"⚠️ Audio callback status: {status}")

    chunk = indata.copy().tobytes()

    if _chunk_counter < 3 or _chunk_counter % 50 == 0:
        log(
            f"🎙️ callback #{_chunk_counter}: frames={frames} "
            f"dtype={indata.dtype} shape={indata.shape} bytes={len(chunk)}"
        )
    _chunk_counter += 1
    _last_callback_ts = time.monotonic()

    audio_queue.put(chunk)


async def watchdog(stream):
    """Diagnóstico: heartbeat cada 5s para ver dónde se traba exactamente."""
    while not stop_session.is_set():
        await asyncio.sleep(5)
        age = (
            f"{time.monotonic() - _last_callback_ts:.1f}s"
            if _last_callback_ts is not None
            else "None"
        )
        log(
            "🩺 watchdog: "
            f"stream.active={stream.active} stream.stopped={stream.stopped} "
            f"callbacks_totales={_chunk_counter} ultimo_callback_hace={age} "
            f"queue_size={audio_queue.qsize()} sends_totales={_sent_counter} "
            f"send_audio={send_audio.is_set()}"
        )


_sent_counter = 0


async def send_microphone(session, stream):
    global _sent_counter
    print("🎙️ Micrófono listo.")

    while not stop_session.is_set():

        # Esperar hasta que sea turno del usuario
        await send_audio.wait()

        try:
            # queue.Queue.get(timeout=...) corre entero dentro del hilo
            # y se rinde solo pasado el timeout (levanta queue.Empty).
            # Antes se envolvía con asyncio.wait_for(), que cancela la
            # espera pero NO el hilo bloqueado en get() -- ese hilo
            # quedaba huérfano esperando el próximo audio, y como
            # queue.Queue despierta a los que esperan en orden FIFO,
            # le robaba los primeros chunks al hilo "vivo" del siguiente
            # ciclo, dejando el loop trabado en silencio.
            audio_bytes = await asyncio.to_thread(
                audio_queue.get, True, 0.5
            )
        except queue.Empty:
            continue

        if stop_session.is_set():
            break

        if not send_audio.is_set():
            continue

        print("🎤 enviando audio...", end="\r", flush=True)

        if _sent_counter < 3 or _sent_counter % 50 == 0:
            log(
                f"➡️ send #{_sent_counter}: {len(audio_bytes)} bytes "
                f"(turno actual: {'ESCUCHANDO' if send_audio.is_set() else 'callado'})"
            )
        _sent_counter += 1

        await session.send_realtime_input(
            audio=types.Blob(
                data=audio_bytes,
                mime_type="audio/pcm;rate=16000",
            )
        )

        global _last_traffic
        _last_traffic = time.monotonic()

    print("🎙️ Micrófono detenido.")


async def receive_messages(session):
    print("📡 Escuchando Gemini...")

    first_turn = True

    # session.receive() corta el generador apenas llega turn_complete
    # (ver _is_interaction_complete en live.py). Sin este while externo,
    # después del primer turno nadie lee más el websocket: el mic sigue
    # mandando audio para siempre y Gemini nunca vuelve a "contestar".
    while not stop_session.is_set():

        async for response in session.receive():

            global _last_traffic
            _last_traffic = time.monotonic()

            if response.voice_activity:
                log(f"🔔 voice_activity: {response.voice_activity.voice_activity_type}")

            if response.go_away:
                log(f"⚠️ go_away (el server va a cortar): {response.go_away}")

            if response.server_content is None:
                continue

            server_content = response.server_content

            if server_content.interrupted:
                log("⚠️ interrupted: el cliente cortó la generación del modelo")

            if server_content.input_transcription:
                log(f"🎧 [VOS] {server_content.input_transcription.text!r}")

            if server_content.output_transcription:
                print(
                    "\n[ALMA]",
                    server_content.output_transcription.text,
                    flush=True,
                )

            if server_content.turn_complete:

                print("\n✅ TURN COMPLETE", flush=True)

                # Gemini terminó de hablar.
                # Ahora el usuario puede hablar.
                send_audio.set()

                if first_turn:
                    print()
                    print("========================================")
                    print("🎙️ AHORA HABLÁ VOS")
                    print("========================================")
                    first_turn = False

                else:
                    print()
                    print("========================================")
                    print("🎙️ SIGUIENTE TURNO")
                    print("========================================")


async def main():

    api_key = resolve_api_key()

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            # El default de la librería websockets es ping_interval=20s /
            # ping_timeout=20s. Con pausas normales de conversación (usuario
            # pensando qué decir) eso alcanza a vencer y cierra el socket con
            # "1011 keepalive ping timeout" en silencio, mucho antes de que
            # el usuario vuelva a hablar.
            async_client_args={
                "ping_interval": 30,
                "ping_timeout": 60,
            },
        ),
    )

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],

        system_instruction=build_test_system_instruction(),

        input_audio_transcription=types.AudioTranscriptionConfig(),

        output_audio_transcription=types.AudioTranscriptionConfig(),

        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,
            )
        ),

        thinking_config=types.ThinkingConfig(
            thinking_level="minimal"
        ),
    )

    async with client.aio.live.connect(
        model=MODEL,
        config=config,
    ) as session:

        print()
        print("========================================")
        print("       ALMA MULTITURN MIC TEST")
        print("========================================")
        print("Gemini Live conectado.")
        print("SIN reproducción de audio.")
        print("Hacé varias preguntas.")
        print("CTRL+C para terminar.")
        print("========================================")
        print()

        # El usuario inicia la conversación.
        send_audio.set()

        with sd.InputStream(
            samplerate=INPUT_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=BLOCKSIZE,
            callback=audio_callback,
        ) as stream:

            try:
                await asyncio.gather(
                    send_microphone(session, stream),
                    receive_messages(session),
                    watchdog(stream),
                )
            except Exception:
                idle = time.monotonic() - _last_traffic
                log(f"❌ Se cayó la sesión. Sin tráfico real por {idle:.1f}s antes del error.")
                raise
            finally:
                stop_session.set()


if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print()
        print("========================================")
        print("TEST TERMINADO")
        print("========================================")