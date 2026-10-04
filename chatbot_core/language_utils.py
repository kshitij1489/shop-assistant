# chatbot_core/utils/language_utils.py
from typing import Tuple, Optional
import logging, os, subprocess, tempfile
from django.conf import settings
from openai import OpenAI
from chatbot_core.llm.chains import structured_chain, text_chain
from chatbot_core.llm.schemas import TranslationResult
import re

try:
    from langdetect import detect_langs
except Exception:
    detect_langs = None

log = logging.getLogger(__name__)

def _audio_client():
    # Dashboard, migrations and text setup do not require audio credentials.
    return OpenAI(api_key=settings.OPENAI_API_KEY)

FFMPEG_VERBOSE = getattr(settings, "FFMPEG_VERBOSE", False)
SUPPORTED_TTS_VOICES = {"nova","shimmer","echo","onyx","fable","alloy","ash","sage","coral"}

# Map languages to valid voices (pick any you like from the supported set)
VOICE_MAP = {
    "en": "nova",
    "hi": "alloy",    # previously "hindi-voice-1" -> use a valid voice
    "es": "shimmer",  # previously "spanish-voice-1"
    "bn": "sage",     # previously "bengali-voice-1"
}

def _normalize_voice(voice: Optional[str]) -> str:
    """Ensure the voice is one of the supported TTS voices; else fall back."""
    v = (voice or "").strip().lower()
    return v if v in SUPPORTED_TTS_VOICES else "nova"

def choose_voice_for_language(lang: str) -> Optional[str]:
    """Pick a voice for a language; always returns a supported voice."""
    return _normalize_voice(VOICE_MAP.get(lang, "nova"))

def to_wav(input_path: str) -> str:
    base, _ = os.path.splitext(input_path)
    wav_path = base + ".wav"

    loglevel = "error" if not FFMPEG_VERBOSE else "info"
    stderr_target = subprocess.PIPE if FFMPEG_VERBOSE else subprocess.DEVNULL

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", loglevel,
        "-nostats",
        "-y",
        "-i", input_path,
        "-ar", "16000",
        "-ac", "1",
        wav_path,
    ]

    try:
        res = subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=stderr_target,
            text=True,
        )
        if FFMPEG_VERBOSE and res.stderr:
            log.debug("ffmpeg: %s", res.stderr.strip())
        return wav_path
    except subprocess.CalledProcessError as e:
        # when verbose, include stderr in the exception log
        if FFMPEG_VERBOSE and e.stderr:
            log.error("ffmpeg failed: %s", e.stderr.strip())
        raise

def tts_to_mp3(text: str, voice: str = "nova") -> str:
    mp3_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3").name
    try:
        safe_voice = _normalize_voice(voice)
        with _audio_client().audio.speech.with_streaming_response.create(
            model=getattr(settings, "TTS_MODEL", "tts-1"),  # or "gpt-4o-mini-tts"
            voice=safe_voice,
            input=text,
            response_format="mp3",
        ) as resp:
            resp.stream_to_file(mp3_path)
        return mp3_path
    except Exception:
        log.exception("TTS generation failed")
        try:
            os.unlink(mp3_path)
        except Exception:
            pass
        raise

HINDI_HINT_WORDS = {"kya","hai","ka","ke","ki","kahan","kisko","kab","bhai","namaste","shukriya"}

def heuristic_detect_roman_hinglish(text: str) -> Optional[str]:
    tokens = {t.lower().strip(".,!?;:()[]") for t in text.split()}
    if tokens & HINDI_HINT_WORDS:
        return "hi"
    return None


def _cheap_local_lang_guess(text: str) -> Tuple[str, float]:
    """Fast local guess before calling LLM (avoids false-positive 'ca' on short English)."""
    if not text:
        return "en", 0.0
    # langdetect
    if detect_langs:
        try:
            langs = detect_langs(text)
            if langs:
                top = langs[0]
                return top.lang, float(top.prob)
        except Exception:
            pass
    # roman Hinglish heuristic
    hint = heuristic_detect_roman_hinglish(text)
    if hint:
        return hint, 0.6
    # crude English-ish check
    ascii_ratio = sum(1 for ch in text if ord(ch) < 128) / max(len(text), 1)
    if ascii_ratio > 0.98 and re.search(r"[a-zA-Z]", text):
        return "en", 0.7
    return "en", 0.0

def translate_with_detection(
    text: str,
    target_lang: str = "en",
    *,
    require_high_confidence_to_translate: bool = True,
) -> Tuple[str, str, float]:
    """
    Returns (translated_text, detected_source_lang, confidence[0..1]).
    - Uses a structured LLM response to detect the language and produce translation.
    - Falls back to local detection + no-op/translate() if parsing fails.
    - Will skip translation if already in target.
    """
    text = (text or "").strip()
    if not text:
        return "", "en", 0.0

    # quick local guess to avoid unnecessary LLM calls
    local_lang, local_conf = _cheap_local_lang_guess(text)
    if local_lang == target_lang and local_conf >= 0.85:
        return text, local_lang, local_conf

    # Ask the LLM to DETECT + TRANSLATE in one go, JSON-only
    prompt = (
        "You are a language detector and translator.\n"
        f"Target language: {target_lang}\n"
        "Return strict JSON with keys:\n"
        '{\n'
        '  "source_lang": "<iso_639_1>",\n'
        '  "confidence": <float 0..1>,\n'
        '  "translated": "<text translated to target OR original if already target>"\n'
        "}\n"
        "Rules:\n"
        "- Detect source language from the text.\n"
        "- If the source is already the target, return the original text as \"translated\".\n"
        "- Preserve numbers, phone numbers, SKUs, IDs exactly.\n"
        "- Do not add any extra keys or prose.\n\n"
        f"Text:\n{text}"
    )

    try:
        result = structured_chain(
            TranslationResult, "Detect the language and translate faithfully.", task="translation",
        ).invoke({"input": prompt})
        src = result.source_lang.strip().lower() or local_lang
        conf = result.confidence
        translated = result.translated.strip() or text

        # Optional guard: if confidence is too low and text looks English, override
        if require_high_confidence_to_translate and src != target_lang and conf < 0.75:
            # fall back to local guess; if likely English, skip translation
            if local_lang == target_lang and local_conf >= 0.75:
                return text, target_lang, local_conf

        return translated, src, conf
    except Exception:
        # Hard fallback: use your existing translate() with local guess
        log.exception("translate_with_detection: LLM detect+translate failed; falling back")
        src = local_lang
        if src == target_lang:
            return text, src, local_conf
        try:
            translated = translate(text, source_lang=src, target_lang=target_lang)
            return translated, src, local_conf
        except Exception:
            log.exception("translate_with_detection: fallback translate() failed")
            return text, src, local_conf

def translate(text: str, source_lang: str, target_lang: str) -> str:
    if not text or source_lang == target_lang:
        return text
    try:
        prompt = (
            f"Translate the following text from {source_lang} to {target_lang}. "
            "Preserve numbers, phone numbers, SKUs, and tokens that look like IDs or codes. "
            "Return the raw translated text only.\n\n"
            f"Text:\n{text}"
        )
        return text_chain(
            "Translate faithfully and preserve identifiers.", task="translation",
        ).invoke({"input": prompt}).strip() or text
    except Exception:
        log.exception("Translation failed")
        return text

def tts_generate_to_file(text: str, lang: str, voice_hint: Optional[str] = None) -> Optional[str]:
    try:
        voice = voice_hint or choose_voice_for_language(lang) or "nova"
        return tts_to_mp3(text, voice=voice)
    except Exception:
        log.exception("TTS generation failed")
        return None

def transcribe_and_detect(wav_path: str) -> Tuple[str, str, float]:
    try:
        with open(wav_path, "rb") as audio_file:
            resp = _audio_client().audio.transcriptions.create(
                model=getattr(settings, "STT_MODEL", "whisper-1"),
                file=audio_file,
            )
    except Exception as e:
        log.exception("Transcription RPC failed: %s", e)
        return "", "en", 0.0

    transcript = getattr(resp, "text", "") or ""
    detected_lang = getattr(resp, "language", None)

    if not transcript:
        return "", "en", 0.0
    if not detected_lang:
        routing_text, lang, conf = translate_with_detection(transcript, target_lang="en")
        return transcript, lang, conf
    return transcript, detected_lang, 0.0
