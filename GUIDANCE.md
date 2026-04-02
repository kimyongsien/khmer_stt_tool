# Guidance For Future Development

## Backend Choice

Use Flask for this tool because it is simple and fast to iterate for annotation workflows.

FastAPI is a good option if you later need:
- Separate frontend app (React/Vue) consuming APIs.
- Heavy async endpoints.
- OpenAPI docs and strict typed API contracts.

For current needs (single server-rendered annotation UI), Flask is the most straightforward.

## Performance Guidance

1. Keep verify/unverify as lightweight JSON endpoint.
2. Avoid re-rendering full page for each verify action.
3. Keep audio processing/transcription only in process step.
4. Paginate chunk rendering to reduce DOM size.

## Data Annotation UX Guidance

1. Default gender is Female unless user changes it.
2. Verify button state:
   - Unverified: orange
   - Verified: green
3. Keep pagination fixed at page bottom.
4. Keep controls large enough for fast repetitive review.

## Safety/Config Guidance

1. Do not hardcode GEMINI_API_KEY in source.
2. Keep media serving restricted to khmer_stt_data paths.
3. For production, disable debug mode and add authentication.

## Scaling Guidance

1. Replace JSON state file with DB table.
2. Add user/session IDs to avoid collisions.
3. Add background task queue for long process jobs.
