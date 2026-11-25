"""Lecture Notes Assistant - Parts B-D: speech-to-text, language analysis, spoken recap."""
import base64
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape

import azure.cognitiveservices.speech as speechsdk
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()
SPEECH_KEY = os.environ["AZURE_SPEECH_KEY"]
SPEECH_REGION = os.environ["AZURE_SPEECH_REGION"]
LANGUAGE_KEY = os.environ["AZURE_LANGUAGE_KEY"]
LANGUAGE_ENDPOINT = os.environ["AZURE_LANGUAGE_ENDPOINT"].rstrip("/")
LANGUAGE_API_VERSION = "2023-04-01"
MAX_DOC_CHARS = 5000        # Language sync API limit is 5,120 chars per document
DOCS_PER_REQUEST = 5
MAX_GLOSSARY_TERMS = 15
MIN_WORDS_FOR_STUDY_INFO = 30
DEFAULT_VOICE = "en-US-JennyNeural"
VOICES = {"en-US-JennyNeural", "en-US-GuyNeural", "en-US-AriaNeural"}
RECAP_TOP_TERMS = 5
LANGUAGE = "en-US"
ALLOWED_EXT = {".wav", ".mp3", ".webm", ".ogg", ".m4a", ".aac", ".aiff", ".aif"}
TICKS_PER_SECOND = 10_000_000  # Speech SDK offsets are in 100-ns ticks

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB


class UnsupportedAudio(Exception):
    pass


class SpeechServiceError(Exception):
    pass


class LanguageServiceError(Exception):
    pass


def to_wav(src, dst):
    """Convert any input to 16 kHz, 16-bit, mono PCM WAV."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
             "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", dst],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        raise RuntimeError("ffmpeg is not installed on the server")
    if r.returncode != 0:
        raise UnsupportedAudio(r.stderr.strip() or "ffmpeg could not decode the file")


def wav_duration(path):
    with wave.open(path, "rb") as w:
        return w.getnframes() / w.getframerate()


def transcribe_wav(path, timeout_s=900):
    """Continuous recognition over a WAV file. Returns (segments, words)."""
    cfg = speechsdk.SpeechConfig(subscription=SPEECH_KEY, region=SPEECH_REGION)
    cfg.speech_recognition_language = LANGUAGE
    cfg.output_format = speechsdk.OutputFormat.Detailed
    cfg.request_word_level_timestamps()
    recognizer = speechsdk.SpeechRecognizer(
        speech_config=cfg, audio_config=speechsdk.audio.AudioConfig(filename=path)
    )

    segments, words, errors = [], [], []
    done = threading.Event()

    def on_recognized(evt):
        if evt.result.reason != speechsdk.ResultReason.RecognizedSpeech:
            return
        best = json.loads(evt.result.json)["NBest"][0]
        seg_conf = best.get("Confidence", 0.0)
        segments.append({"text": evt.result.text, "confidence": seg_conf})
        for w in best.get("Words", []):
            words.append({
                "word": w["Word"],
                "offset": round(w["Offset"] / TICKS_PER_SECOND, 2),
                "duration": round(w["Duration"] / TICKS_PER_SECOND, 2),
                # Real-time STT often gives confidence per segment only;
                # fall back to the segment score when a word has none.
                "confidence": round(w.get("Confidence", seg_conf), 3),
            })

    def on_canceled(evt):
        details = evt.cancellation_details
        if details.reason == speechsdk.CancellationReason.Error:
            errors.append(details.error_details)
        done.set()  # EndOfStream also arrives here when the file ends

    recognizer.recognized.connect(on_recognized)
    recognizer.canceled.connect(on_canceled)
    recognizer.session_stopped.connect(lambda evt: done.set())

    recognizer.start_continuous_recognition_async().get()
    finished = done.wait(timeout_s)
    recognizer.stop_continuous_recognition_async().get()

    if errors:
        raise SpeechServiceError(errors[0])
    if not finished:
        raise SpeechServiceError(f"Transcription timed out after {timeout_s}s")
    return segments, words


def run_stt(file_storage):
    """Shared by /transcribe now and /process later."""
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise UnsupportedAudio(
            f"'{ext or 'no extension'}' is not supported. "
            f"Use one of: {', '.join(sorted(ALLOWED_EXT))}"
        )
    with tempfile.TemporaryDirectory() as tmp:  # deleted automatically
        src = os.path.join(tmp, "input" + ext)
        wav = os.path.join(tmp, "audio.wav")
        file_storage.save(src)
        to_wav(src, wav)
        duration = wav_duration(wav)
        segments, words = transcribe_wav(wav)

    if words:
        confidence = sum(w["confidence"] for w in words) / len(words)
    elif segments:
        confidence = sum(s["confidence"] for s in segments) / len(segments)
    else:
        confidence = 0.0

    return {
        "transcript": " ".join(s["text"] for s in segments),
        "language": LANGUAGE,
        "duration_seconds": round(duration, 1),
        "confidence": round(confidence, 3),
        "words": words,
    }


# ---------------------------------------------------------------- Part C

def chunk_text(text, max_chars=MAX_DOC_CHARS):
    """Split a long transcript into documents under the Language size limit,
    breaking at sentence ends where possible."""
    chunks, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        while len(sentence) > max_chars:  # one giant unpunctuated sentence
            cut = sentence.rfind(" ", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            chunks.append(sentence[:cut])
            sentence = sentence[cut:].strip()
        if current and len(current) + len(sentence) + 1 > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


def call_language(kind, chunks):
    """Run one Language task over all chunks. Returns the per-document results."""
    url = f"{LANGUAGE_ENDPOINT}/language/:analyze-text?api-version={LANGUAGE_API_VERSION}"
    results = []
    for start in range(0, len(chunks), DOCS_PER_REQUEST):
        batch = chunks[start:start + DOCS_PER_REQUEST]
        body = {
            "kind": kind,
            "analysisInput": {"documents": [
                {"id": str(start + i), "language": "en", "text": t}
                for i, t in enumerate(batch)
            ]},
        }
        try:
            r = requests.post(url, json=body, timeout=30,
                              headers={"Ocp-Apim-Subscription-Key": LANGUAGE_KEY})
        except requests.RequestException as e:
            raise LanguageServiceError(f"Could not reach Azure Language: {e}")
        if r.status_code != 200:
            raise LanguageServiceError(f"{kind} returned {r.status_code}: {r.text[:300]}")
        data = r.json()["results"]
        if data.get("errors"):
            raise LanguageServiceError(f"{kind} document error: {data['errors'][0]}")
        results.extend(data["documents"])
    return results


def tokens(s):
    return re.findall(r"[a-z0-9']+", s.lower())


def first_mention(phrase, words):
    """Seconds into the audio where a phrase is first spoken, or None."""
    target = tokens(phrase)
    spoken = [tokens(w["word"])[0] if tokens(w["word"]) else "" for w in words]
    n = len(target)
    for i in range(len(spoken) - n + 1):
        if spoken[i:i + n] == target:
            return words[i]["offset"]
    return None


def analyze_text(text, words=None):
    words = words or []
    chunks = chunk_text(text)
    if not chunks:
        return {"key_phrases": [], "entities": [], "glossary_candidates": []}

    # The two Language tasks are independent, so run them at the same time
    with ThreadPoolExecutor(max_workers=2) as pool:
        phrase_job = pool.submit(call_language, "KeyPhraseExtraction", chunks)
        entity_job = pool.submit(call_language, "EntityRecognition", chunks)
        phrase_docs, entity_docs = phrase_job.result(), entity_job.result()

    # Key phrases: merge across chunks, keep first spelling, dedupe case-insensitively
    key_phrases, seen = [], set()
    for doc in phrase_docs:
        for p in doc["keyPhrases"]:
            if p.lower() not in seen:
                seen.add(p.lower())
                key_phrases.append(p)

    # Entities: dedupe on (text, category), keep the highest confidence
    entities = {}
    for doc in entity_docs:
        for e in doc["entities"]:
            k = (e["text"].lower(), e["category"])
            if k not in entities or e["confidenceScore"] > entities[k]["confidence"]:
                entities[k] = {"text": e["text"], "category": e["category"],
                               "subcategory": e.get("subcategory"),
                               "confidence": round(e["confidenceScore"], 3)}
    entities = list(entities.values())

    # Glossary candidates: key phrases that aren't people's names,
    # ranked by how often they're said, then by first appearance
    people = {e["text"].lower() for e in entities if e["category"] == "Person"}
    lowered = " " + " ".join(tokens(text)) + " "  # padded so counts match whole words
    candidates = []
    for order, p in enumerate(key_phrases):
        if p.lower() in people:
            continue
        mentions = lowered.count(" " + " ".join(tokens(p)) + " ")
        candidates.append({"term": p, "mentions": max(mentions, 1),
                           "first_mention_s": first_mention(p, words), "_order": order})
    candidates.sort(key=lambda c: (-c["mentions"], c["_order"]))
    for c in candidates:
        del c["_order"]

    return {"key_phrases": key_phrases, "entities": entities,
            "glossary_candidates": candidates[:MAX_GLOSSARY_TERMS]}


# ---------------------------------------------------------------- Part D

def build_recap(glossary, entities, quiz):
    """Returns (plain_text, ssml_body). The plain text is shown in the UI;
    the SSML adds pauses so a student can think before each answer."""
    terms = [g["term"] for g in glossary[:RECAP_TOP_TERMS]]
    parts_text, parts_ssml = [], []

    if len(terms) > 1:
        s = (f"This segment covers {len(glossary)} key terms. The main ones are "
             f"{', '.join(terms[:-1])}, and {terms[-1]}.")
    elif terms:
        s = f"This segment covers one key term: {terms[0]}."
    else:
        s = "I didn't find any key terms in this segment."
    parts_text.append(s)
    parts_ssml.append(escape(s))

    names = {"Person": ("person", "people"), "DateTime": ("date", "dates"),
             "Location": ("place", "places"), "Organization": ("organization", "organizations"),
             "Event": ("event", "events")}
    counts = {}
    for e in entities:
        if e["category"] in names:
            counts[e["category"]] = counts.get(e["category"], 0) + 1
    if counts:
        items = [f"{n} {names[c][0] if n == 1 else names[c][1]}" for c, n in counts.items()]
        joined = items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" and {items[-1]}"
        s = f"It mentions {joined}."
        parts_text.append(s)
        parts_ssml.append(escape(s))

    if quiz:
        parts_text.append("Here are three questions to check yourself.")
        parts_ssml.append('<break time="600ms"/>Here are three questions to check yourself.')
        for i, q in enumerate(quiz, 1):
            answer = q["choices"][q["answer_index"]]
            parts_text.append(f"Question {i}: {q['question']} Answer: {answer}.")
            parts_ssml.append(
                f'<break time="500ms"/>Question {i}. {escape(q["question"])}'
                f'<break time="3s"/>Answer: {escape(answer)}.')

    return " ".join(parts_text), " ".join(parts_ssml)


def synthesize(ssml_body, voice):
    """Neural TTS to MP3 in memory. Returns base64 audio."""
    cfg = speechsdk.SpeechConfig(subscription=SPEECH_KEY, region=SPEECH_REGION)
    cfg.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Audio16Khz32KBitRateMonoMp3)
    synth = speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=None)
    ssml = ('<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xml:lang="en-US"><voice name="{voice}">{ssml_body}</voice></speak>')
    result = synth.speak_ssml_async(ssml).get()
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        details = result.cancellation_details
        raise SpeechServiceError(f"TTS failed: {details.reason} {details.error_details}")
    return base64.b64encode(result.audio_data).decode("ascii")


@app.post("/analyze")
def analyze():
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify(error="Send JSON like {\"text\": \"...\"}."), 400
    try:
        return jsonify(analyze_text(text))
    except LanguageServiceError as e:
        return jsonify(error="Azure Language failed.", detail=str(e)), 502


@app.post("/process")
def process():
    f = request.files.get("audio")
    if not f or not f.filename:
        return jsonify(error="Send the audio as a multipart field named 'audio'."), 400
    voice = request.form.get("voice", DEFAULT_VOICE)
    if voice not in VOICES:
        return jsonify(error=f"Unknown voice. Use one of: {', '.join(sorted(VOICES))}"), 400
    timings = {}
    try:
        t0 = time.perf_counter()
        stt = run_stt(f)
        timings["stt"] = round((time.perf_counter() - t0) * 1000)

        word_count = len(stt["transcript"].split())
        if word_count < MIN_WORDS_FOR_STUDY_INFO:
            return jsonify(
                error=f"Transcript has {word_count} words; at least "
                      f"{MIN_WORDS_FOR_STUDY_INFO} are needed for study information.",
                transcript=stt["transcript"]), 422

        t0 = time.perf_counter()
        lang = analyze_text(stt["transcript"], stt["words"])
        timings["language"] = round((time.perf_counter() - t0) * 1000)
    except UnsupportedAudio as e:
        return jsonify(error="Unsupported or unreadable audio format.", detail=str(e)), 415
    except SpeechServiceError as e:
        return jsonify(error="Azure Speech failed.", detail=str(e)), 502
    except LanguageServiceError as e:
        return jsonify(error="Azure Language failed.", detail=str(e)), 502

    notes, quiz = [], []  # filled by the Foundry study-information stage (next)

    # TTS: if it fails, still return everything else instead of a 502
    recap_text, recap_ssml = build_recap(lang["glossary_candidates"], lang["entities"], quiz)
    recap_audio, recap_error = None, None
    t0 = time.perf_counter()
    try:
        recap_audio = synthesize(recap_ssml, voice)
    except SpeechServiceError as e:
        recap_error = str(e)
    timings["tts"] = round((time.perf_counter() - t0) * 1000)

    minutes = stt["duration_seconds"] / 60 or 1
    return jsonify({
        "segment": {"duration_seconds": stt["duration_seconds"],
                    "language": stt["language"], "confidence": stt["confidence"]},
        "transcript": stt["transcript"],
        "glossary": lang["glossary_candidates"],
        "key_phrases": lang["key_phrases"],
        "entities": lang["entities"],
        "notes": notes,
        "quiz": quiz,
        "study_info_status": "not_implemented",
        "key_terms_per_minute": round(len(lang["glossary_candidates"]) / minutes, 2),
        "recap_text": recap_text,
        "recap_voice": voice,
        "tts_char_count": len(recap_text),
        "recap_audio": recap_audio,
        "recap_error": recap_error,
        "stage_ms": timings,
    })


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.post("/transcribe")
def transcribe():
    f = request.files.get("audio")
    if not f or not f.filename:
        return jsonify(error="Send the audio as a multipart field named 'audio'."), 400
    try:
        return jsonify(run_stt(f))
    except UnsupportedAudio as e:
        return jsonify(error="Unsupported or unreadable audio format.", detail=str(e)), 415
    except SpeechServiceError as e:
        return jsonify(error="Azure Speech failed.", detail=str(e)), 502


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="File is larger than 25 MB."), 413


if __name__ == "__main__":
    # Port 5000 is taken by AirPlay Receiver on macOS, so use 8000.
    app.run(host="127.0.0.1", port=8000, debug=True)