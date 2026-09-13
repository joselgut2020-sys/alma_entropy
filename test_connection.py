import os
import asyncio
import queue
from pathlib import Path

import numpy as np
import sounddevice as sd

from google import genai
from google.genai import types

from tools.tools import (
    evaluate_options,
    validate_proposal,
    register_outcome,
    request_human,
)


MODEL = "gemini-3.1-flash-live-preview"

TOOL_FUNCTIONS = {
    "evaluate_options": evaluate_options,
    "validate_proposal": validate_proposal,
    "register_outcome": register_outcome,
    "request_human": request_human,
}

TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="evaluate_options",
        description=(
            "Devuelve las opciones de pago autorizadas para la cuota del "
            "cliente, según la causa de la dificultad y lo que propone."
        ),
        parameters_json_schema={
            "type": "object",
            "properties": {
                "cause": {
                    "type": "string",
                    "description": "Motivo por el que el cliente no puede pagar en la fecha original.",
                },
                "proposed_date": {
                    "type": "string",
                    "description": "Fecha que propone el cliente, si menciona una.",
                },
                "proposed_amount": {
                    "type": "number",
                    "description": "Monto que propone el cliente, si menciona uno.",
                },
            },
            "required": ["cause"],
        },
    ),
    types.FunctionDeclaration(
        name="validate_proposal",
        description=(
            "Valida si la fecha/monto/opción que propone el cliente está "
            "dentro de lo autorizado por evaluate_options."
        ),
        parameters_json_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Fecha propuesta."},
                "amount": {"type": "number", "description": "Monto propuesto."},
                "option_id": {
                    "type": "string",
                    "description": "ID de la opción elegida (ej. OP-01), si corresponde.",
                },
            },
            "required": ["date"],
        },
    ),
    types.FunctionDeclaration(
        name="register_outcome",
        description=(
            "Registra el resultado final de la llamada. Llamar solo cuando "
            "el cliente ya confirmó, se negó, o se acordó un siguiente paso."
        ),
        parameters_json_schema={
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["agreement", "refusal", "next_step"],
                    "description": "Resultado de la gestión.",
                },
                "date": {"type": "string"},
                "amount": {"type": "number"},
                "option_id": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["outcome"],
        },
    ),
    types.FunctionDeclaration(
        name="request_human",
        description=(
            "Escala la conversación a un asesor humano. Usar si el cliente "
            "lo pide explícitamente o no quiere hablar con una máquina."
        ),
        parameters_json_schema={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Por qué se escala a un asesor humano.",
                },
                "callback_window": {
                    "type": "string",
                    "description": "Ventana de tiempo en la que lo llamarán, si se acordó una.",
                },
            },
            "required": ["reason"],
        },
    ),
]

INPUT_RATE = 16000
OUTPUT_RATE = 24000
CHANNELS = 1
BLOCKSIZE = 1024

PROMPTS_PATH = Path(__file__).resolve().with_name("prompts") / "system_base.md"

audio_queue = queue.Queue()
stop_session = asyncio.Event()

# Sin cancelación de eco de hardware, el micrófono capta la propia voz de
# Alma saliendo del parlante y se la manda de vuelta a Gemini como si fuera
# el usuario -- Alma termina "conversando con su propio eco". Este Event
# calla el envío del mic mientras Alma está hablando (medio dúplex por
# software) para evitar el bucle de realimentación.
send_audio = asyncio.Event()
send_audio.set()


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
    escuchar el tono/reglas reales por voz sin tener aún el builder
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


def audio_callback(indata, frames, time_info, status):
    if status:
        print("Audio:", status)

    audio_queue.put(indata.copy().tobytes())


async def send_microphone(session):
    """Captura continuamente el micrófono y envía PCM16 16 kHz a Gemini."""

    print("🎙️ Micrófono activo. Hablá...")

    while not stop_session.is_set():
        try:
            # timeout en el propio Queue.get() (no en un wrapper asyncio) para
            # que el hilo termine solo sin dejar consumidores huérfanos.
            audio_bytes = await asyncio.to_thread(audio_queue.get, True, 0.5)
        except queue.Empty:
            continue

        if not send_audio.is_set():
            # Alma está hablando: descartamos lo que captó el mic (puede
            # ser eco del parlante) en vez de mandárselo a Gemini.
            continue

        await session.send_realtime_input(
            audio=types.Blob(
                data=audio_bytes,
                mime_type="audio/pcm;rate=16000",
            )
        )


async def receive_messages(session, output_stream):
    """Recibe audio y transcripciones de Gemini, reproduce la voz en tiempo real."""

    print("🔊 Esperando respuesta de Alma...")

    first_turn = True

    # session.receive() corta el generador apenas llega turn_complete, así
    # que hay que volver a pedirlo en cada turno para no dejar de escuchar
    # después de la primera respuesta.
    while not stop_session.is_set():

        async for response in session.receive():

            if response.tool_call:
                function_responses = []

                for call in response.tool_call.function_calls:
                    func = TOOL_FUNCTIONS.get(call.name)

                    if func is None:
                        result = {
                            "status": "error",
                            "message": f"Herramienta desconocida: {call.name}",
                        }
                    else:
                        result = func(**(call.args or {}))

                    print(f"\n🔧 {call.name}({call.args}) -> {result}", flush=True)

                    function_responses.append(
                        types.FunctionResponse(
                            id=call.id,
                            name=call.name,
                            response={"output": result},
                        )
                    )

                await session.send_tool_response(function_responses=function_responses)
                continue

            if response.server_content is None:
                continue

            server_content = response.server_content

            if server_content.model_turn:
                # Alma empezó a hablar: callamos el mic para no mandarle
                # de vuelta su propia voz si el parlante hace eco.
                send_audio.clear()

                for part in server_content.model_turn.parts:
                    if part.inline_data:
                        audio_array = np.frombuffer(
                            part.inline_data.data,
                            dtype=np.int16,
                        )
                        # sounddevice.write() puede bloquear: la sacamos
                        # del event loop.
                        await asyncio.to_thread(output_stream.write, audio_array)

            if server_content.input_transcription:
                print(
                    "\n[VOS]",
                    server_content.input_transcription.text,
                    flush=True,
                )

            if server_content.output_transcription:
                print(
                    "\n[ALMA]",
                    server_content.output_transcription.text,
                    flush=True,
                )

            if server_content.turn_complete:
                print("\n✅ ALMA terminó de hablar.", flush=True)

                # Ahora sí, el turno es del usuario: reabrimos el mic.
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
            # Default de la librería websockets: ping_interval=20s /
            # ping_timeout=20s. Con pausas normales de conversación eso
            # alcanza a vencer y cierra el socket en silencio.
            async_client_args={
                "ping_interval": 30,
                "ping_timeout": 60,
            },
        ),
    )

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],

        system_instruction=build_test_system_instruction(),

        tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],

        speech_config=types.SpeechConfig(
            language_code="es-US",
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name="Aoede"
                )
            ),
        ),

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
        print("       ALMA - CONVERSACIÓN EN VIVO")
        print("========================================")
        print("Sesión Gemini Live conectada.")
        print("Hablá normalmente, Alma te va a responder con voz.")
        print("Usá auriculares: sin ellos el micrófono puede captar")
        print("la voz de Alma y confundir la detección de turnos.")
        print("CTRL+C para terminar.")
        print("========================================")
        print()

        output_stream = sd.OutputStream(
            samplerate=OUTPUT_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=BLOCKSIZE,
        )
        output_stream.start()

        with sd.InputStream(
            samplerate=INPUT_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=BLOCKSIZE,
            callback=audio_callback,
        ):
            try:
                await asyncio.gather(
                    send_microphone(session),
                    receive_messages(session, output_stream),
                )
            finally:
                stop_session.set()
                output_stream.stop()
                output_stream.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print()
        print("========================================")
        print("TEST TERMINADO")
        print("========================================")
