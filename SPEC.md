# Voice Atelier — implementation contract v1

Local RU/EN voice cloning and prompt-based voice design studio. Target Windows, RTX 3060 Ti 8 GB, RAM 32 GB. No Python in PATH initially. Independent project, never modify DSH.

## Ownership
Backend agent owns `studio/`, `tests/`, `requirements.txt`, `requirements-engine.txt`, `pyproject.toml` if needed. Frontend agent owns `web/` only. Parent owns Windows portable bootstrap/launch scripts, docs and integration review. Do not edit another owner's files without coordination.

## Runtime
FastAPI serves vanilla JS/CSS web on 127.0.0.1:8765, no frontend build. Python 3.11 isolated `.venv`. All user data/models/cache under project `data/` by default (VOICE_ATELIER_HOME override). SQLite persistence. One serial job worker, no concurrent GPU jobs. Models never auto-download during generation. Base local-only loading; download only explicit UI request, pinned HF revisions with safetensors only (no trust_remote_code, pickle or .pt remote weights). Qwen3-TTS primary engine, 0.6B-Base fallback and 1.7B-VoiceDesign separate mode. Use sdpa, bf16 CUDA, float32 CPU opt-in. Unload previous model before switching. No Chatterbox in v1 due dependency conflict.

## Security / truthfulness
Loopback bind, validated Host and same-origin Origin for mutations, no CORS; per-launch token injected as escaped meta `studio-token` into index (web template uses __STUDIO_TOKEN__). API header `X-Studio-Token` required except health. Audio downloads via authenticated fetch->blob in UI. CSP no external scripts/fonts. Generated UUID filenames, no client paths, capped uploads and subprocess timeouts. No remote TTS, LLM or automatic network. User acknowledges rights/permission to each reference; acknowledgement recorded in metadata/exports. Not a proof of consent. Clone Base has NO free-form emotion instructions: reject nonempty `instruction` in clone mode. Design creates NEW identity from instruction, NEVER claims to preserve uploaded voice identity. Reference delivery can be selected by uploading separate clips. Rate is pitch-preserving ffmpeg atempo POSTPROCESS, pause is inserted silence, both labeled. Seed reproducible only best effort, not guaranteed bit-exact. Cancellation cooperative between chunks; show limitation.

## JSON contracts (freeze; coordinate additions)
All errors {"error":{"code":str,"message":str,"details":object?}} with appropriate HTTP status. Success plain entities or {items:[]} lists. Job polling every ~1 second, done=result; no fake production engine. IDs strings UUID.

GET /api/health -> {ok:true,version:str}
GET /api/status -> {version,offline,engine_available,device:{name,vram_mb,cuda},loaded_model,capabilities:{languages:["ru","en",...],clone_instruction:false,design_instruction:true,rate:{min:0.5,max:2,mode:"postprocess"},seed:true},paths:{data:str},warnings:[str]}
GET /api/models -> {items:[{id,label,repo,revision,kind:"clone"|"design",size_gb,license,installed,status,description}]}
POST /api/models/{id}/download {accept_license:true} -> job (explicit only); offline env disables.
POST /api/engine/unload {} -> job

Voice {id,name,language,transcript,consent_basis,consent_at,created_at,duration,sample_rate,quality:{peak_dbfs,rms_dbfs,clipped_fraction,silence_fraction,warnings:[str]},source:"upload"|"design"}
GET /api/voices -> {items:[Voice]}
POST /api/voices multipart: audio(file), name, language=ru, transcript, consent=true, consent_basis="own"|"permission"|"licensed", trim_start=0, trim_end(optional seconds) -> Voice. Max upload 50MB, decoded reference max60sec, selected ref 2..30sec; suggest 6..20sec dry speech. Accept wav/flac/mp3/m4a/ogg/webm through bounded ffmpeg. Clean normalized mono 24k WAV saved, no automatic denoise claims.
GET /api/voices/{id}/audio -> wav
DELETE /api/voices/{id} -> {ok:true}; reject voice in project.

Project {id,title,created_at,updated_at,segments:[Segment]}
Segment {id,text,voice_id:null|str,mode:"clone"|"design",model_id:"qwen-base-1.7b"|"qwen-base-0.6b"|"qwen-design-1.7b",language:"ru"|"en"...,instruction:"",rate:1.0,pause_ms:300,seed:42,selected_take_id:null|str,takes:[Take]}
Take {id,segment_id,created_at,duration,seed,model_id,input_hash,stale:bool}
GET /api/projects -> {items:[Project summary with id,title,updated_at]}
POST /api/projects {title} -> Project with empty segments
GET /api/projects/{id} -> Project
PUT /api/projects/{id} {title,segments:[Segment]} -> Project. Preserve server-owned takes, selected_take_id allowed only own segment take. Input changes mark existing takes stale based on actual engine/text/ref/params hash. Max 200 segments, text <=10000 chars per segment, instruction<=1500.
DELETE /api/projects/{id} -> {ok:true}
POST /api/projects/{id}/generate {segment_ids:[str] or omitted for all,force:bool=false} -> Job. Generate each missing/stale segment, use real engine, chunk on punctuation <=240 chars for 8GB. Include pauses only in mix, not individual take. Nonempty instruction in clone =>422, missing transcript =>422. Keep previous takes and select new complete take. Never silently truncate generated speech: surface max_new_tokens exhaustion if detectable. No pretend % for CUDA step.
POST /api/projects/{id}/select {segment_id,take_id} -> Project
GET /api/takes/{id}/audio -> wav

POST /api/design {name,text,instruction,language:"ru",seed:42,save_as_voice:false,consent:true} -> Job. Uses design model, real free-form prompt. Result {take_id,voice_id?}. Save-as-voice creates synthetic Voice with reference=generated clip and verbatim transcript, consent_basis=synthetic. Do not claim same identity as any clone.

GET /api/jobs -> {items:[Job]}
GET /api/jobs/{id} -> Job
POST /api/jobs/{id}/cancel {} -> Job
Job {id,kind,state:"queued"|"running"|"done"|"failed"|"canceled",progress:0..1,stage:str,cancel_requested:bool,error:null|{code,message},result:null|object,created_at}

POST /api/projects/{id}/export {format:"wav"|"zip",normalize:bool=false,allow_stale:bool=false} -> Job
GET /api/exports/{id}/download -> bytes
ZIP includes ordered segment wav files, mixed.wav, script.txt and manifest.json (model revision, text, voice consent basis, ref hashes, generation params, SHA256 outputs). Export blocks missing/stale selected takes unless explicit allow_stale true. Mix sample rate=24000 native; no fake 48k quality upgrade. Insert silence pause_ms after each segment except last. Peak normalization optional and labeled, NOT EBU loudness mastering.

## UI spec
Brand Voice Atelier. Russian default, EN toggle, all strings localized. Elegant dark studio aesthetic with warm accent, sidebar tabs Studio / Voices / Voice Design / Models / Guide, responsive, keyboard/ARIA/focus, no CDNs. Studio multi-voice project/script editor autosave with visible state, split paragraphs into segments, per-line voice/model/language/rate/pause/seed; add/reorder/delete, takes A/B selection+preview and stale badges; batch generate and export. Clone mode no instruction input (honest note). Design mode instruction input + presets explicitly for new designed identity. Voices upload/record via MediaRecorder, trim numeric/range and audio preview+waveform possible, transcript+rights required, saved quality warnings. Models explicit install + license acceptance and download progress+cancel. Persistent jobs drawer with cancel/error/progress. Empty states, no models installed onboarding, GPU/status visible. Audio fetched as blob with token. UI must not fabricate voices/waveforms/model ready state. Can waveform actual audio via WebAudio. Recording requires loopback secure context; stop tracks on cancel/navigation.

## Frozen integration deltas (supersede earlier field details)
- Voice upload accepts exactly one of `file` or `audio` multipart fields.
- PATCH /api/voices/{id} {name?,transcript?} updates editable metadata only; changing transcript marks existing clone takes stale, renaming does not.
- GET /api/designs returns persisted designed takes with text/instruction/voice association.
- Design text (both standalone and design-mode project segments) is limited to 240 characters; status.capabilities.max_design_chars=240. Save the generated identity, then use Base cloning for long speech. This avoids independently regenerated identities across chunks.
- Public Take includes warnings:string[] from actual generation; empty warnings is not proof of quality.
- A model is installed only after a verified atomic manifest matches pinned id/repo/revision and all recorded file sizes. Partial files alone are never installed.
- Export provenance must distinguish actual rendered take inputs from current edited segment inputs when allow_stale=true; never include local filesystem paths in the public ZIP manifest.

## Verification
Backend tests with explicitly injected deterministic test adapter only under tests; no production fake or silent audio fallback. Test API auth/origin/host, consent/upload/trim, project preservation/stale, serial jobs cancellation, missing engine/model failure, download safetensors manifest/offline restrictions, mix duration/pause/zip metadata, reference dependencies, text instruction gates. UI static syntax and browser smoke when tool available. Real GPU audio smoke requires explicit model download approval; until measured say NOT RUN. Report truthfully.
