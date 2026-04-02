import os
import re
import json
import zipfile
import shutil
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, send_file, jsonify
import pandas as pd
import soundfile as sf
import librosa
import google.generativeai as genai

# =========================================================
# CONFIG
# =========================================================

BASE_DIR = Path.cwd() / "khmer_stt_data"
RAW_DIR = BASE_DIR / "raw_audio"
PROCESSED_DIR = BASE_DIR / "processed_audio"
PREVIEW_DIR = BASE_DIR / "preview_audio"
CHUNKS_DIR = BASE_DIR / "chunks"
CSV_DIR = BASE_DIR / "csv"
CSV_PATH = CSV_DIR / "dataset.csv"
EXPORT_DIR = BASE_DIR / "exports"
STATE_DIR = BASE_DIR / "session_state"
STATE_PATH = STATE_DIR / "state.json"

CSV_COLUMNS = [
    "speaker_id",
    "topic",
    "subtopic",
    "paragraph_id",
    "sentence_id",
    "transcript",
    "duration",
    "audio_path",
    "save_dir",
    "start",
    "end",
    "gender",
]

PER_PAGE = 10

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024


# =========================================================
# FOLDER HELPERS
# =========================================================

def ensure_base_dirs():
    for d in [RAW_DIR, PROCESSED_DIR, PREVIEW_DIR, CHUNKS_DIR, CSV_DIR, EXPORT_DIR, STATE_DIR]:
        d.mkdir(parents=True, exist_ok=True)


ensure_base_dirs()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyD4qW9TmBYsn-t1_yAZ64XqS5HJ_3THJKM").strip()
if not GEMINI_API_KEY:
    print("WARNING: GEMINI_API_KEY is not set.")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    MODEL = genai.GenerativeModel("gemini-2.5-flash")
else:
    MODEL = None


# =========================================================
# HELPERS
# =========================================================

def sanitize_name(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"[^\w\-]+", "_", stem, flags=re.UNICODE)
    stem = re.sub(r"_+", "_", stem).strip("_")
    return stem or "audio"


def format_time_range(start: float, end: float) -> str:
    return f"{start:.1f}-{end:.1f}s"


def int_duration(start: float, end: float) -> int:
    return int(max(0, end - start))


def ui_to_csv_speaker(value: str) -> str:
    v = (value or "").strip().lower()
    if v in {"agents", "agent"}:
        return "agents"
    if v in {"customers", "customer"}:
        return "customers"
    return "customers"


def csv_to_ui_speaker(value: str) -> str:
    return "Agents" if ui_to_csv_speaker(value) == "agents" else "Customers"


def ui_to_csv_gender(value: str) -> str:
    v = (value or "").strip().lower()
    if v == "male":
        return "male"
    if v == "female":
        return "female"
    return "female"


def csv_to_ui_gender(value: str) -> str:
    return "Male" if ui_to_csv_gender(value) == "male" else "Female"


def ensure_csv():
    ensure_base_dirs()
    if not CSV_PATH.exists():
        pd.DataFrame(columns=CSV_COLUMNS).to_csv(CSV_PATH, index=False, encoding="utf-8-sig")


def load_csv() -> pd.DataFrame:
    ensure_csv()
    try:
        df = pd.read_csv(CSV_PATH)
    except Exception:
        df = pd.DataFrame(columns=CSV_COLUMNS)

    for col in CSV_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    return df[CSV_COLUMNS].copy()


def save_csv(df: pd.DataFrame):
    ensure_base_dirs()
    out = df.copy()
    for col in CSV_COLUMNS:
        if col not in out.columns:
            out[col] = ""

    out["sentence_id"] = pd.to_numeric(out["sentence_id"], errors="coerce").fillna(0).astype(int)
    out["duration"] = pd.to_numeric(out["duration"], errors="coerce").fillna(0).astype(int)
    out["start"] = pd.to_numeric(out["start"], errors="coerce").fillna(0).astype(int)
    out["end"] = pd.to_numeric(out["end"], errors="coerce").fillna(0).astype(int)
    out["save_dir"] = out["save_dir"].fillna("").astype(str)
    out["audio_path"] = out["audio_path"].fillna("").astype(str)

    out = out.sort_values(by=["save_dir", "sentence_id"], kind="stable").reset_index(drop=True)
    out.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")


def add_or_replace_csv_rows(rows_to_add: list[dict]):
    df = load_csv()
    if rows_to_add:
        target_save_dir = str(rows_to_add[0]["save_dir"])
        df = df[df["save_dir"].astype(str) != target_save_dir]
        df = pd.concat([df, pd.DataFrame(rows_to_add)], ignore_index=True)
    save_csv(df)


def extract_json_block(text: str) -> dict:
    text = (text or "").strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    fenced = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except Exception:
            pass

    brace = re.search(r"(\{.*\})", text, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(1))
        except Exception:
            pass

    raise ValueError(f"Invalid JSON from Gemini. Raw response: {text[:1000]}")


def load_state() -> dict:
    ensure_base_dirs()
    if not STATE_PATH.exists():
        return {"rows": [], "raw_audio_path": "", "status": "Ready."}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"rows": [], "raw_audio_path": "", "status": "Ready."}


def save_state(state: dict):
    ensure_base_dirs()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def reset_storage():
    if BASE_DIR.exists():
        shutil.rmtree(BASE_DIR, ignore_errors=True)
    ensure_base_dirs()
    save_state({"rows": [], "raw_audio_path": "", "status": "History cleared. Storage reset complete."})


def clamp_page(page: int, total: int) -> int:
    if total <= 0:
        return 1
    return max(1, min(page, total))


def build_pagination(page: int, total_pages: int) -> list[int | str]:
    if total_pages <= 1:
        return [1]

    pages: list[int | str] = []
    window = {1, 2, total_pages - 1, total_pages, page - 1, page, page + 1}
    valid = sorted(p for p in window if 1 <= p <= total_pages)

    last = 0
    for p in valid:
        if p - last > 1:
            pages.append("...")
        pages.append(p)
        last = p
    return pages


def preprocess_audio(raw_path: Path) -> Path:
    ensure_base_dirs()
    y, sr = librosa.load(str(raw_path), sr=None, mono=True)

    if sr != 16000:
        y = librosa.resample(y, orig_sr=sr, target_sr=16000)
        sr = 16000

    if len(y) > 0:
        y = librosa.util.normalize(y)
        y, _ = librosa.effects.trim(y, top_db=30)

    clean_path = PROCESSED_DIR / f"{sanitize_name(raw_path.name)}_clean.wav"
    sf.write(str(clean_path), y, sr)
    return clean_path


def export_audio_slice(source_audio_path: Path, start: float, end: float, out_path: Path):
    data, sr = sf.read(str(source_audio_path))
    s = max(0, int(start * sr))
    e = max(s, int(end * sr))

    if e <= s:
        raise ValueError("Invalid slice range")

    chunk = data[s:e]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_path), chunk, sr)


def save_preview_chunk(clean_audio_path: Path, start: float, end: float, save_dir: str, audio_name: str) -> Path:
    preview_folder = PREVIEW_DIR / save_dir
    preview_folder.mkdir(parents=True, exist_ok=True)
    out_path = preview_folder / audio_name
    export_audio_slice(clean_audio_path, start, end, out_path)
    return out_path


def gemini_transcribe(clean_audio_path: Path) -> dict:
    if not MODEL:
        raise ValueError("GEMINI_API_KEY is not set.")

    with open(clean_audio_path, "rb") as f:
        audio_bytes = f.read()

    prompt = """
You are a Khmer speech transcription and speaker labeling system.

Return ONLY strict JSON.
Do not use markdown.
Do not add explanation.
Do not add any text before or after JSON.

Schema:
{
  "segments": [
    {
      "start": 0.0,
      "end": 3.5,
      "speaker_id": "agents",
      "text": "..."
    }
  ]
}

Rules:
- "speaker_id" must be only "agents" or "customers"
- Output segments in chronological order
- Use timestamps in seconds
- Do not overlap segments if possible
- Transcribe exactly what is spoken
- Use Khmer Unicode for Khmer speech
- Keep English words in English if code-switching happens
- Do not translate
- Do not summarize
- Do not paraphrase
- Do not include speaker names inside text
- Keep segments review-friendly, strictly under 15 seconds
"""

    response = MODEL.generate_content([
        {"mime_type": "audio/wav", "data": audio_bytes},
        prompt,
    ])

    raw_text = response.text if hasattr(response, "text") and response.text else str(response)
    return extract_json_block(raw_text)


def validate_segments(payload: dict, audio_duration: float) -> list[dict]:
    segments = payload.get("segments", [])
    repaired = []

    for item in segments:
        try:
            start = float(item.get("start", 0))
            end = float(item.get("end", 0))
            speaker_id = ui_to_csv_speaker(item.get("speaker_id", "customers"))
            text = str(item.get("text", "")).strip()

            if start < 0:
                start = 0.0
            if end > audio_duration:
                end = audio_duration

            if end <= start or (end - start) < 0.3 or not text:
                continue

            repaired.append({"start": start, "end": end, "speaker_id": speaker_id, "text": text})
        except Exception:
            continue

    repaired.sort(key=lambda x: x["start"])

    fixed = []
    prev_end = None
    for seg in repaired:
        start = seg["start"]
        end = seg["end"]

        if prev_end is not None and start < prev_end and prev_end - start <= 0.3:
            start = prev_end

        if end <= start:
            continue

        fixed.append({"start": start, "end": end, "speaker_id": seg["speaker_id"], "text": seg["text"]})
        prev_end = end

    return fixed


def process_audio(upload_path: str, original_name: str, topic: str, subtopic: str) -> tuple[list[dict], str, str]:
    ensure_base_dirs()

    if not upload_path:
        return [], "", "Error: no audio file selected."

    if not GEMINI_API_KEY:
        return [], "", "Error: GEMINI_API_KEY is not set."

    source = Path(upload_path)
    if not source.exists():
        return [], "", f"Error: uploaded temp file not found: {source}"

    safe_stem = sanitize_name(original_name or source.name)
    suffix = Path(original_name).suffix.lower() if original_name else source.suffix.lower()
    raw_path = RAW_DIR / f"{safe_stem}{suffix}"

    with open(source, "rb") as fsrc, open(raw_path, "wb") as fdst:
        fdst.write(fsrc.read())

    clean_path = preprocess_audio(raw_path)
    info = sf.info(str(clean_path))
    audio_duration = info.frames / info.samplerate

    gemini_payload = gemini_transcribe(clean_path)
    segments = validate_segments(gemini_payload, audio_duration)

    if not segments:
        return [], str(raw_path), "Error: no valid segments returned from Gemini."

    rows = []
    for i, seg in enumerate(segments, start=1):
        temp_audio_name = f"{safe_stem}_temp_{i:02d}.wav"
        preview_path = save_preview_chunk(
            clean_audio_path=clean_path,
            start=seg["start"],
            end=seg["end"],
            save_dir=safe_stem,
            audio_name=temp_audio_name,
        )

        rows.append(
            {
                "topic": (topic or "").strip(),
                "subtopic": (subtopic or "").strip(),
                "paragraph_id": 1,
                "source_sentence_id": i,
                "audio_name": temp_audio_name,
                "save_dir": safe_stem,
                "start": seg["start"],
                "end": seg["end"],
                "duration": int_duration(seg["start"], seg["end"]),
                "speaker_id": seg["speaker_id"],
                "gemini_speaker_id": seg["speaker_id"],
                "transcript": seg["text"],
                "gemini_text": seg["text"],
                "gender": "female",
                "verified": False,
                "raw_audio_path": str(raw_path),
                "clean_audio_path": str(clean_path),
                "preview_audio_path": str(preview_path),
                "finalized": False,
            }
        )

    return rows, str(raw_path), f"Ready for review. {len(rows)} chunks created."


def finalize_audio(rows: list[dict]) -> tuple[list[dict], str]:
    ensure_base_dirs()

    if not rows:
        return rows, "Error: no audio loaded."

    verified_rows = [dict(r) for r in rows if r.get("verified", False)]
    if not verified_rows:
        return rows, "Error: no verified chunks to finalize."

    base_name = verified_rows[0]["save_dir"]
    clean_audio_path = Path(verified_rows[0]["clean_audio_path"])
    final_chunk_folder = CHUNKS_DIR / base_name
    final_chunk_folder.mkdir(parents=True, exist_ok=True)

    verified_rows.sort(key=lambda x: float(x["start"]))
    final_csv_rows = []

    for old_file in final_chunk_folder.glob("*.wav"):
        try:
            old_file.unlink()
        except Exception:
            pass

    for new_idx, row in enumerate(verified_rows, start=1):
        final_audio_name = f"{base_name}_{new_idx:02d}.wav"
        final_chunk_path = final_chunk_folder / final_audio_name

        export_audio_slice(
            source_audio_path=clean_audio_path,
            start=float(row["start"]),
            end=float(row["end"]),
            out_path=final_chunk_path,
        )

        final_csv_rows.append(
            {
                "speaker_id": row["speaker_id"],
                "topic": row["topic"],
                "subtopic": row["subtopic"],
                "paragraph_id": 1,
                "sentence_id": new_idx,
                "transcript": row["transcript"],
                "duration": int_duration(float(row["start"]), float(row["end"])),
                "audio_path": final_audio_name,
                "save_dir": base_name,
                "start": int(float(row["start"])),
                "end": int(float(row["end"])),
                "gender": row["gender"],
            }
        )

    add_or_replace_csv_rows(final_csv_rows)

    for r in rows:
        r["finalized"] = False

    verified_sorted = sorted([r for r in rows if r.get("verified", False)], key=lambda x: float(x["start"]))
    for new_idx, verified in enumerate(verified_sorted, start=1):
        verified["finalized"] = True
        verified["final_sentence_id"] = new_idx
        verified["final_audio_name"] = f"{base_name}_{new_idx:02d}.wav"

    return rows, f"Finalize complete: {len(final_csv_rows)} segments saved."


def export_dataset_zip() -> tuple[Path, str]:
    ensure_csv()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = EXPORT_DIR / f"dataset_export_{timestamp}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if CSV_PATH.exists():
            zf.write(CSV_PATH, arcname="dataset.csv")

        if CHUNKS_DIR.exists():
            for file_path in CHUNKS_DIR.rglob("*"):
                if file_path.is_file():
                    arcname = Path("chunks") / file_path.relative_to(CHUNKS_DIR)
                    zf.write(file_path, arcname=str(arcname))

    return zip_path, f"Export ready: {zip_path.name}"


def safe_media_path(path_str: str) -> Path | None:
    if not path_str:
        return None
    p = Path(path_str)
    if not p.exists() or not p.is_file():
        return None

    allowed_roots = [BASE_DIR.resolve()]
    rp = p.resolve()
    if any(str(rp).startswith(str(root)) for root in allowed_roots):
        return rp
    return None


# =========================================================
# ROUTES
# =========================================================

@app.get("/")
def index():
    page = request.args.get("page", default=1, type=int)
    state = load_state()
    rows = state.get("rows", [])

    total_items = len(rows)
    total_pages = max(1, (total_items + PER_PAGE - 1) // PER_PAGE)
    page = clamp_page(page, total_pages)

    start = (page - 1) * PER_PAGE
    end = start + PER_PAGE
    visible_rows = rows[start:end]

    page_items = build_pagination(page, total_pages)

    return render_template(
        "index.html",
        rows=visible_rows,
        page=page,
        total_pages=total_pages,
        page_items=page_items,
        total_items=total_items,
        per_page=PER_PAGE,
        status=state.get("status", "Ready."),
        raw_audio_path=state.get("raw_audio_path", ""),
        format_time_range=format_time_range,
        csv_to_ui_speaker=csv_to_ui_speaker,
        csv_to_ui_gender=csv_to_ui_gender,
    )


@app.post("/process")
def process_route():
    ensure_base_dirs()

    page = request.form.get("page", default=1, type=int)
    topic = request.form.get("topic", "")
    subtopic = request.form.get("subtopic", "")
    uploaded = request.files.get("audio_file")

    if not uploaded or not uploaded.filename:
        state = load_state()
        state["status"] = "Error: no audio file selected."
        save_state(state)
        return redirect(url_for("index", page=page))

    temp_upload = RAW_DIR / f"_upload_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{sanitize_name(uploaded.filename)}"
    uploaded.save(temp_upload)

    try:
        rows, raw_audio_path, status = process_audio(str(temp_upload), uploaded.filename, topic, subtopic)
        state = {"rows": rows, "raw_audio_path": raw_audio_path, "status": status}
        save_state(state)
    except Exception as e:
        state = load_state()
        state["status"] = f"Error: {e}"
        save_state(state)
    finally:
        try:
            temp_upload.unlink(missing_ok=True)
        except Exception:
            pass

    return redirect(url_for("index", page=1))


@app.post("/process-ajax")
def process_ajax_route():
    ensure_base_dirs()

    topic = request.form.get("topic", "")
    subtopic = request.form.get("subtopic", "")
    uploaded = request.files.get("audio_file")

    if not uploaded or not uploaded.filename:
        msg = "Error: no audio file selected."
        state = load_state()
        state["status"] = msg
        save_state(state)
        return jsonify({"ok": False, "message": msg}), 400

    temp_upload = RAW_DIR / f"_upload_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{sanitize_name(uploaded.filename)}"
    uploaded.save(temp_upload)

    try:
        rows, raw_audio_path, status = process_audio(str(temp_upload), uploaded.filename, topic, subtopic)
        state = {"rows": rows, "raw_audio_path": raw_audio_path, "status": status}
        save_state(state)
        return jsonify({"ok": True, "message": status})
    except Exception as e:
        msg = f"Error: {e}"
        state = load_state()
        state["status"] = msg
        save_state(state)
        return jsonify({"ok": False, "message": msg}), 500
    finally:
        try:
            temp_upload.unlink(missing_ok=True)
        except Exception:
            pass


@app.post("/verify")
def verify_route():
    payload = request.get_json(silent=True) or {}
    index = int(payload.get("index", -1))
    transcript = (payload.get("transcript") or "").strip()
    speaker_ui = payload.get("speaker", "Customers")
    gender_ui = payload.get("gender", "Female")
    action = payload.get("action", "verify")

    state = load_state()
    rows = state.get("rows", [])

    if index < 0 or index >= len(rows):
        return jsonify({"ok": False, "message": "Invalid row index."}), 400

    rows[index]["transcript"] = transcript
    rows[index]["speaker_id"] = ui_to_csv_speaker(speaker_ui)
    rows[index]["gender"] = ui_to_csv_gender(gender_ui)

    if action == "verify":
        if not transcript:
            return jsonify({"ok": False, "message": "Transcript is empty."}), 400
        rows[index]["verified"] = True
        msg = f"Verified segment {index + 1}"
    else:
        rows[index]["verified"] = False
        msg = f"Unverified segment {index + 1}"

    state["rows"] = rows
    state["status"] = msg
    save_state(state)

    return jsonify({"ok": True, "message": msg, "verified": rows[index]["verified"]})


@app.post("/export")
def export_route():
    state = load_state()
    rows = state.get("rows", [])

    try:
        rows, finalize_msg = finalize_audio(rows)
        state["rows"] = rows
        state["status"] = finalize_msg
        save_state(state)

        zip_path, export_msg = export_dataset_zip()
        state["status"] = export_msg
        save_state(state)

        return send_file(zip_path, as_attachment=True)
    except Exception as e:
        state["status"] = f"Error: {e}"
        save_state(state)
        return redirect(url_for("index"))


@app.post("/clear-history")
def clear_history_route():
    try:
        reset_storage()
    except Exception as e:
        state = {"rows": [], "raw_audio_path": "", "status": f"Error while clearing history: {e}"}
        save_state(state)
    return redirect(url_for("index", page=1))


@app.get("/media")
def media_route():
    path_str = request.args.get("path", "")
    safe_path = safe_media_path(path_str)
    if not safe_path:
        return "Not found", 404
    return send_file(safe_path)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=7860, debug=True)