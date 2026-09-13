import os
import asyncio
import queue
from pathlib import Path

import numpy as np
import sounddevice as sd

from google import genai
from google.genai import types


MODEL = "gemini-3.1-flash-live-preview"


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
OUTPUT_RATE = 24000
CHANNELS = 1
BLOCKSIZE = 1024

audio_queue = queue.Queue()


def audio_callback(indata, frames, time, status):
    if status:
        print("Audio:", status)

    audio_queue.put(indata.copy().tobytes())


async def send_microphone(session):
    """
    Captura continuamente el micrófono y envía PCM16 16 kHz a Gemini.
    """

    print("🎙️ Micrófono activo. Hablá...")
    
    with sd.InputStream(
        samplerate=INPUT_RATE,
        channels=CHANNELS,
        dtype="int16",
        blocksize=BLOCKSIZE,
        callback=audio_callback,
    ):
        buffer = bytearray()

        while True:
            audio_bytes = await asyncio.to_thread(
                audio_queue.get
            )

            buffer.extend(audio_bytes)

            # 4096 bytes = 128 ms de audio PCM16 mono a 16 kHz
            if len(buffer) >= 4096:
                chunk = bytes(buffer)
                buffer.clear()

                await session.send_realtime_input(
                    audio=types.Blob(
                        data=chunk,
                        mime_type="audio/pcm;rate=16000",
                    )
                )


async def receive_audio(session):
    """
    Recibe audio PCM16 24 kHz de Gemini y lo reproduce
    sin bloquear el event loop.
    """

    print("🔊 Esperando respuesta de Alma...")

    output_stream = sd.OutputStream(
        samplerate=OUTPUT_RATE,
        channels=CHANNELS,
        dtype="int16",
        blocksize=BLOCKSIZE,
    )

    output_stream.start()

    try:
        async for response in session.receive():

            if response.server_content is None:
                continue

            server_content = response.server_content

            if server_content.model_turn:

                for part in server_content.model_turn.parts:

                    if part.inline_data:
                        audio_bytes = part.inline_data.data

                        audio_array = np.frombuffer(
                            audio_bytes,
                            dtype=np.int16,
                        )

                        # IMPORTANTE:
                        # sounddevice.write() puede bloquear.
                        # Lo sacamos del event loop.
                        await asyncio.to_thread(
                            output_stream.write,
                            audio_array
                        )

                    if part.text:
                        print(
                            "\n[ALMA]",
                            part.text,
                            flush=True,
                        )

            if server_content.output_transcription:
                print(
                    "\n[ALMA TRANSCRIPCIÓN]",
                    server_content.output_transcription.text,
                    flush=True,
                )

    except asyncio.CancelledError:
        raise

    finally:
        output_stream.stop()
        output_stream.close()


async def main():

    api_key = resolve_api_key()
    client = genai.Client(api_key=api_key)

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],

        system_instruction=(
            "Eres Alma, asistente virtual de Banco Agrícola. "
            "Habla en español de El Salvador. "
            "Sé amable, clara y breve. "
            "Responde naturalmente al cliente."
        ),

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
        print("       ALMA LIVE VOICE TEST")
        print("========================================")
        print("Sesión Gemini Live conectada.")
        print("Habla normalmente.")
        print("Presiona CTRL+C para terminar.")
        print("========================================")
        print()

        await asyncio.gather(
            send_microphone(session),
            receive_audio(session),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print("\n")
        print("========================================")
        print("TEST TERMINADO")
        print("========================================")
