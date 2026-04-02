# Khmer STT Tool v4 (Flask Version)

This project is now migrated from Gradio to Flask + HTML/CSS/JS for better UI customization and faster interaction.

## Why Flask (not Gradio)

- You need fully custom layout and colors (white/black/blue design).
- You need custom pagination UI at the bottom.
- You need faster verify click behavior by avoiding full page component re-renders.

Flask works very well here because:
- Backend audio/transcription logic stays in Python.
- Frontend can be fully controlled with templates and CSS.
- Verify toggle can be done with lightweight AJAX calls.

## Project Structure

- app.py: Flask backend and core STT logic.
- templates/index.html: Main UI page.
- static/styles.css: Main styling.
- static/app.js: Fast verify/unverify button actions.
- khmer_stt_data/session_state/state.json: Current annotation session state.

## Features

- Upload audio and process with Gemini.
- Preview chunk audio.
- Edit transcript, speaker, gender.
- Verify/unverify per chunk.
- Finalize verified chunks to output folder and CSV.
- Export dataset ZIP.
- Pagination with 10 chunks per page and bottom controls.

## Run

1. Install dependencies:
   pip install -r requirements.txt

2. Set API key:
   set GEMINI_API_KEY=your_key_here

3. Start server:
   python app.py

4. Open:
   http://127.0.0.1:7860

## Notes

- This implementation stores current UI state in a JSON file under khmer_stt_data/session_state.
- For multi-user or production, move state to a database and use user sessions.
