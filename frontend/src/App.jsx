import { useEffect, useRef, useState } from "react";

const VOICES = [
  { id: "en-US-JennyNeural", label: "Jenny (friendly)" },
  { id: "en-US-GuyNeural", label: "Guy (calm)" },
  { id: "en-US-AriaNeural", label: "Aria (energetic)" },
];

function pickMimeType() {
  // Chrome/Edge record WebM, Safari records MP4. The backend converts both.
  for (const type of ["audio/webm", "audio/mp4"]) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(type)) return type;
  }
  return "";
}

function formatTime(s) {
  if (s == null) return "–";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60).toString().padStart(2, "0");
  return `${m}:${sec}`;
}

export default function App() {
  const [audio, setAudio] = useState(null); // { blob, name, url }
  const [recording, setRecording] = useState(false);
  const [recordSeconds, setRecordSeconds] = useState(0);
  const [voice, setVoice] = useState(VOICES[0].id);
  const [course, setCourse] = useState("");
  const [title, setTitle] = useState("");
  const [loading, setLoading] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);

  const recorderRef = useRef(null);
  const chunksRef = useRef([]);
  const lectureAudioRef = useRef(null);

  // Recording timer
  useEffect(() => {
    if (!recording) return;
    const t = setInterval(() => setRecordSeconds((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [recording]);

  // Processing timer
  useEffect(() => {
    if (!loading) return;
    setElapsed(0);
    const t = setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [loading]);

  function setAudioSource(blob, name) {
    if (audio?.url) URL.revokeObjectURL(audio.url);
    setAudio({ blob, name, url: URL.createObjectURL(blob) });
    setResult(null);
    setError(null);
  }

  async function startRecording() {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = pickMimeType();
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      chunksRef.current = [];
      recorder.ondataavailable = (e) => e.data.size && chunksRef.current.push(e.data);
      recorder.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const type = recorder.mimeType || "audio/webm";
        const ext = type.includes("mp4") ? "m4a" : "webm";
        setAudioSource(new Blob(chunksRef.current, { type }), `recording.${ext}`);
      };
      recorder.start();
      recorderRef.current = recorder;
      setRecordSeconds(0);
      setRecording(true);
    } catch (e) {
      setError({ error: "Microphone unavailable.", detail: e.message });
    }
  }

  function stopRecording() {
    recorderRef.current?.stop();
    setRecording(false);
  }

  function onFile(e) {
    const file = e.target.files?.[0];
    if (file) setAudioSource(file, file.name);
  }

  async function submit() {
    if (!audio) return;
    setLoading(true);
    setError(null);
    setResult(null);
    const form = new FormData();
    form.append("audio", audio.blob, audio.name);
    form.append("voice", voice);
    if (course) form.append("course", course);
    if (title) form.append("title", title);
    try {
      const res = await fetch("/process", { method: "POST", body: form });
      const data = await res.json().catch(() => ({ error: `Server returned ${res.status}` }));
      if (!res.ok) setError({ status: res.status, ...data });
      else setResult(data);
    } catch (e) {
      setError({ error: "Could not reach the server.", detail: e.message });
    } finally {
      setLoading(false);
    }
  }

  function jumpTo(seconds) {
    const el = lectureAudioRef.current;
    if (!el || seconds == null) return;
    el.currentTime = seconds;
    el.play();
  }

  return (
    <div className="page">
      <header>
        <h1>Lecture to Study Guide</h1>
        <p className="sub">Record or upload a lecture segment. Get notes, a glossary and a quiz.</p>
      </header>

      <section className="card">
        <h2>1. Add a lecture segment</h2>
        <div className="row">
          {recording ? (
            <button className="btn danger" onClick={stopRecording}>
              ■ Stop ({formatTime(recordSeconds)})
            </button>
          ) : (
            <button className="btn" onClick={startRecording} disabled={loading}>
              ● Record
            </button>
          )}
          <span className="or">or</span>
          <label className="btn secondary">
            Upload file
            <input type="file" accept=".wav,.mp3,.m4a,.webm,.ogg,.aiff,audio/*" onChange={onFile} hidden />
          </label>
        </div>

        {audio && (
          <div className="preview">
            <span className="muted">{audio.name}</span>
            <audio ref={lectureAudioRef} src={audio.url} controls />
          </div>
        )}

        <div className="row fields">
          <input placeholder="Course (optional)" value={course} onChange={(e) => setCourse(e.target.value)} />
          <input placeholder="Title (optional)" value={title} onChange={(e) => setTitle(e.target.value)} />
          <select value={voice} onChange={(e) => setVoice(e.target.value)}>
            {VOICES.map((v) => (
              <option key={v.id} value={v.id}>Recap voice: {v.label}</option>
            ))}
          </select>
        </div>

        <button className="btn primary" onClick={submit} disabled={!audio || loading || recording}>
          {loading ? `Processing… ${elapsed}s` : "Submit"}
        </button>
      </section>

      {error && (
        <section className="card error">
          <strong>{error.status ? `Error ${error.status}: ` : ""}{error.error}</strong>
          {error.detail && <p>{error.detail}</p>}
        </section>
      )}

      {result && <Results r={result} onJump={jumpTo} />}
    </div>
  );
}

function Results({ r, onJump }) {
  const [revealed, setRevealed] = useState({});
  const seg = r.segment;

  return (
    <>
      <section className="card stats">
        <Stat label="Duration" value={formatTime(seg.duration_seconds)} />
        <Stat label="STT confidence" value={`${Math.round(seg.confidence * 100)}%`} />
        <Stat label="Key terms / min" value={r.key_terms_per_minute} />
        <Stat
          label="Time per stage"
          value={Object.entries(r.stage_ms || {}).map(([k, v]) => `${k} ${(v / 1000).toFixed(1)}s`).join(" · ")}
        />
      </section>

      <section className="card">
        <h2>Spoken recap</h2>
        {r.recap_audio ? (
          <audio controls src={`data:audio/mpeg;base64,${r.recap_audio}`} />
        ) : (
          <p className="muted">Audio unavailable{r.recap_error ? `: ${r.recap_error}` : "."}</p>
        )}
        <p className="muted">{r.recap_text}</p>
      </section>

      <section className="card">
        <h2>Notes</h2>
        {r.notes?.length ? (
          r.notes.map((n, i) => (
            <div key={i} className="note">
              <h3>{n.heading}</h3>
              <ul>{n.points.map((p, j) => <li key={j}>{p}</li>)}</ul>
            </div>
          ))
        ) : (
          <p className="muted">Structured notes arrive with the study-information step.</p>
        )}
      </section>

      <section className="card">
        <h2>Glossary</h2>
        {r.glossary?.length ? (
          <table>
            <thead>
              <tr><th>Term</th><th>Definition</th><th>Mentions</th><th>First said</th></tr>
            </thead>
            <tbody>
              {r.glossary.map((g) => (
                <tr key={g.term}>
                  <td><strong>{g.term}</strong></td>
                  <td className="muted">{g.definition || "—"}</td>
                  <td>{g.mentions ?? "—"}</td>
                  <td>
                    {g.first_mention_s != null ? (
                      <button className="link" onClick={() => onJump(g.first_mention_s)} title="Replay this moment">
                        ▶ {formatTime(g.first_mention_s)}
                      </button>
                    ) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">No key terms found.</p>
        )}
      </section>

      <section className="card">
        <h2>Self-check quiz</h2>
        {r.quiz?.length ? (
          r.quiz.map((q, i) => (
            <div key={i} className="quiz">
              <p><strong>{i + 1}. {q.question}</strong></p>
              <ol type="A">
                {q.choices.map((c, j) => (
                  <li key={j} className={revealed[i] && j === q.answer_index ? "correct" : ""}>{c}</li>
                ))}
              </ol>
              <button className="link" onClick={() => setRevealed({ ...revealed, [i]: !revealed[i] })}>
                {revealed[i] ? "Hide answer" : "Show answer"}
              </button>
            </div>
          ))
        ) : (
          <p className="muted">Quiz questions arrive with the study-information step.</p>
        )}
      </section>

      <section className="card">
        <h2>Key phrases</h2>
        <div className="tags">
          {r.key_phrases?.map((p) => <span key={p} className="tag">{p}</span>)}
        </div>
      </section>

      <section className="card">
        <h2>Named entities</h2>
        {r.entities?.length ? (
          <table>
            <thead><tr><th>Text</th><th>Category</th><th>Confidence</th></tr></thead>
            <tbody>
              {r.entities.map((e) => (
                <tr key={e.text + e.category}>
                  <td>{e.text}</td>
                  <td>{e.category}{e.subcategory ? ` · ${e.subcategory}` : ""}</td>
                  <td>{Math.round(e.confidence * 100)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">None found.</p>
        )}
      </section>

      <section className="card">
        <h2>Transcript</h2>
        <p className="transcript">{r.transcript}</p>
      </section>
    </>
  );
}

function Stat({ label, value }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  );
}
