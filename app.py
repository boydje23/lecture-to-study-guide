"""Lecture Notes Assistant - Part B: speech-to-text."""
import json
import os
import subprocess
import tempfile
import threading
import wave

import azure.cognitiveservices.speech as speechsdk
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()
SPEECH_KEY = os.environ["AZURE_SPEECH_KEY"]
SPEECH_REGION = os.environ["AZURE_SPEECH_REGION"]
LANGUAGE = "en-US"
ALLOWED_EXT = {".wav", ".mp3", ".webm", ".ogg", ".m4a", ".aac", ".aiff", ".aif"}
TICKS_PER_SECOND = 10_000_000  # Speech SDK offsets are in 100-ns ticks

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB


class UnsupportedAudio(Exception):
    pass


class SpeechServiceError(Exception):
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