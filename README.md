# ContentClipper

ContentClipper watches `input/` and turns long-form horizontal videos into up to 3 vertical 9:16 clips with burned-in subtitles and a short hook overlay.

Assumptions are configurable in `.env`: 9Router defaults to `http://127.0.0.1:20128/v1`, the model value defaults to the 9Router alias/combo `auto`, and Whisper defaults to CPU mode with the `small` model for a 4GB RAM VPS.

## Resource Rules

- Processing is strictly sequential: one worker handles one video from transcribe to render before the next queue item starts.
- Whisper is loaded once at service startup and uses `OMP_NUM_THREADS=1` plus `torch.set_num_threads(1)`.
- FFmpeg and ffprobe run through `nice -n 19`; `ionice -c3` is enabled by default via `USE_IONICE=1`.
- FFmpeg uses `-threads 2` by default.
- Source videos are moved to `processed/` on success or `failed/` on failure. They are never deleted directly.

## Install

```bash
sudo apt update
sudo apt install -y ffmpeg fonts-dejavu-core python3-venv python3-pip screen
cd /home/ubuntu/contentclipper
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

`requirements.txt` pins `torch==2.2.2+cpu` from the PyTorch CPU wheel index. This avoids installing multi-GB CUDA dependencies on a CPU-only 4GB VPS. It also pins `numpy==1.26.4` because Torch 2.2 CPU wheels are built against NumPy 1.x. It uses `openai-whisper==20250625` because older `20231117` can fail during pip build isolation with `ModuleNotFoundError: No module named 'pkg_resources'`.

Edit `.env` if needed:

- `NINEROUTER_API_KEY`: leave empty if local 9Router does not require auth.
- `NINEROUTER_MODEL`: set to the alias/combo configured in the 9Router dashboard.
- `WHISPER_MODEL`: use `small`; change to `base` if RAM is still tight during testing.
- `SUBTITLE_FONT_PATH`: must point to an existing font file, defaulting to DejaVu Sans Bold.

## Quick Test

Check watcher and file stability only:

```bash
. .venv/bin/activate
python main.py --watch-only
```

Dry-run one video through transcription and AI analysis without rendering or moving the source:

```bash
. .venv/bin/activate
python main.py --dry-run --once input/example.mp4
```

Run the full watcher:

```bash
. .venv/bin/activate
python main.py
```

For a quick long-running shell session:

```bash
screen -S contentclipper
cd /home/ubuntu/contentclipper
. .venv/bin/activate
python main.py
```

Detach from screen with `Ctrl-A` then `D`. Reattach with `screen -r contentclipper`.

## 9Router

ContentClipper calls `POST {NINEROUTER_BASE_URL}/chat/completions` with OpenAI-compatible payloads. It sends `Authorization: Bearer ...` only when `NINEROUTER_API_KEY` is non-empty.

The analyzer retries timeout, connection errors, `429`, and `5xx` responses with exponential backoff. If a target rejects `response_format`, ContentClipper retries without it and still validates JSON manually.

## Output Behavior

- Put new videos in `input/` with extension `.mp4`, `.mov`, or `.mkv`.
- Successful clips are written to `output/{source_stem}_clipN.mp4`.
- Successful sources move to `processed/`.
- Failed sources move to `failed/` with a sibling `.log` file describing the failed stage.
- Temporary audio/SRT/render files live under `tmp/` and are cleaned after each job.

## Hook Template

Hook layout is controlled by a JSON file so you can adjust the CapCut-style look without editing Python code.

For visual editing, run the built-in Hook Studio:

```bash
. .venv/bin/activate
python tools/hook_template_editor.py
```

Default local URL:

```text
http://127.0.0.1:8765/
```

If you need to open it from your laptop browser through the VPS public IP, bind publicly with a token:

```bash
python tools/hook_template_editor.py --host 0.0.0.0 --port 8765 --token pilih-token-random
```

Then open:

```text
http://SERVER_IP:8765/?token=pilih-token-random
```

Stop the editor with `Ctrl-C` after saving the template. It only edits the hook template file; it does not start video processing.

On this VPS, Hook Studio can also run as a systemd service behind Caddy on the already-public 9Router port:

```text
http://SERVER_IP:20129/hook-studio/?token=YOUR_HOOK_EDITOR_TOKEN
```

The token is read from `.env`:

```env
HOOK_EDITOR_TOKEN=change-me-long-random-token
```

The editor uses a clean layer-based layout: Layers on the left, 9:16 preview in the middle, and Properties on the right. Editable layers are Video Overlay, Hook Text, Badge, and Subtitle. It supports dragging layers in the preview, per-layer font picker, font size, text color, stroke/outline, shadow, bubble/background controls, wrapping controls, visibility toggles, and save-to-template.

Video Overlay controls apply to horizontal videos that use blurred background. You can adjust foreground video width, X/Y position, background blur strength, and optional border. Hook Text has a Bubble Box toggle with configurable bubble color and padding.

Default template:

```bash
hook_templates/capcut.json
```

Active template is selected in `.env`:

```env
HOOK_TEMPLATE_PATH=hook_templates/capcut.json
```

To create your own style:

```bash
cp hook_templates/capcut.json hook_templates/my_style.json
```

Then edit `hook_templates/my_style.json` and set:

```env
HOOK_TEMPLATE_PATH=hook_templates/my_style.json
```

Useful fields:

- `badge.enabled`: show/hide the small badge above the hook.
- `badge.text`: badge text, for example `BREAKING`, `HOT TAKE`, or `VIRAL`.
- `badge.x`, `badge.y`, `hook.x`, `hook.y`: FFmpeg drawtext positions such as `(w-text_w)/2`, `120`, `h*0.18`.
- `font_size`, `font_color`, `box_color`, `border_w`, `border_color`, `shadow_x`, `shadow_y`: visual style controls.
- `max_line_chars` and `max_lines`: hook text wrapping.
- `display_seconds` and `fade_seconds`: how long the hook appears and how fast it fades in.

JSON does not allow comments, so keep notes outside the file.

## systemd

Review `contentclipper.service` and adjust `User`, `WorkingDirectory`, and `ExecStart` if your install path differs.

```bash
sudo cp contentclipper.service /etc/systemd/system/contentclipper.service
sudo systemctl daemon-reload
sudo systemctl enable --now contentclipper
sudo journalctl -u contentclipper -f
```

The example unit uses `Restart=on-failure`, `Nice=19`, idle IO scheduling, and `MemoryMax=3500M` as a VPS safety net.

## Logs

Logs go to stdout and `logs/contentclipper.log` with rotation. FFmpeg stderr is captured and included in failures because it usually contains the exact codec/filter problem.

## Validation Checklist

- Drop one real video into `input/` and confirm max 3 vertical clips appear in `output/`.
- Confirm the source moved to `processed/` on success.
- Stop 9Router and confirm retries happen, the source moves to `failed/`, and the watcher keeps running.
- Drop two files together and confirm log timestamps show sequential processing.
- Monitor RAM with `htop`; if usage approaches 3.5GB, set `WHISPER_MODEL=base`.
- Confirm subtitles are yellow with black border, hook text appears during the first 3 seconds, and blur background is used only for horizontal sources.
