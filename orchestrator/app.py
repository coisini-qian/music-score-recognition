from __future__ import annotations

import base64
import datetime as dt
import json
import mimetypes
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ORCHESTRATOR_DIR / "config.json"
CONFIG_EXAMPLE_PATH = ORCHESTRATOR_DIR / "config.example.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8765,
    "work_dir": "",
    "audiveris_command": "",
    "musicxml_to_midi_command": "",
    "pianobooster_command": "",
    "vmpk_command": "",
}


def load_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as fp:
            loaded = json.load(fp)
        if isinstance(loaded, dict):
            config.update(loaded)
    return config


def build_work_root(config: dict[str, Any]) -> Path:
    raw = str(config.get("work_dir") or "").strip()
    if raw:
        work_root = Path(raw)
    else:
        work_root = Path(tempfile.gettempdir()) / "music-score-recognition-hub"
    work_root.mkdir(parents=True, exist_ok=True)
    return work_root


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def render_template(template: str, **values: str) -> str:
    data = {key: str(value) for key, value in values.items()}
    data.setdefault("root", str(ROOT))
    data.setdefault("workdir", data.get("workdir", ""))
    return template.format_map(_SafeDict(data))


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


def decode_data_url(data_url: str) -> tuple[bytes, str]:
    if ";base64," not in data_url:
        raise ValueError("Expected a base64 data URL")
    header, payload = data_url.split(";base64,", 1)
    mime = header.split(":", 1)[1] if ":" in header else "image/png"
    return base64.b64decode(payload), mime


def save_image(data_url: str, target_dir: Path) -> Path:
    blob, mime = decode_data_url(data_url)
    extension = mimetypes.guess_extension(mime) or ".png"
    image_path = target_dir / f"input{extension}"
    image_path.write_bytes(blob)
    return image_path


def run_command(command: str, cwd: Path | None = None) -> dict[str, Any]:
    proc = subprocess.run(
        command,
        cwd=str(cwd or ROOT),
        shell=True,
        capture_output=True,
        text=True,
    )
    return {
        "command": command,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def start_command(command: str, cwd: Path | None = None) -> dict[str, Any]:
    subprocess.Popen(command, cwd=str(cwd or ROOT), shell=True)
    return {"command": command, "started": True}


def find_latest_file(directory: Path, suffixes: set[str]) -> Path | None:
    if not directory.exists():
        return None
    candidates: list[Path] = []
    for item in directory.rglob("*"):
        if item.is_file() and item.suffix.lower() in suffixes:
            candidates.append(item)
    if not candidates:
        return None
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]


def ensure_job_dir(work_root: Path) -> Path:
    job_dir = work_root / now_stamp()
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.config = load_config()
        self.work_root = build_work_root(self.config)
        self.last_result: dict[str, Any] | None = None

    def reload(self) -> None:
        with self.lock:
            self.config = load_config()
            self.work_root = build_work_root(self.config)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "config": {
                    "host": self.config.get("host", DEFAULT_CONFIG["host"]),
                    "port": self.config.get("port", DEFAULT_CONFIG["port"]),
                    "work_dir": str(self.work_root),
                    "audiveris_command": bool(self.config.get("audiveris_command")),
                    "musicxml_to_midi_command": bool(self.config.get("musicxml_to_midi_command")),
                    "pianobooster_command": bool(self.config.get("pianobooster_command")),
                    "vmpk_command": bool(self.config.get("vmpk_command")),
                },
                "last_result": self.last_result,
            }


STATE = AppState()


def recognize_image(data_url: str) -> dict[str, Any]:
    config = STATE.config
    job_dir = ensure_job_dir(STATE.work_root)
    input_dir = job_dir / "input"
    audiveris_dir = job_dir / "audiveris"
    convert_dir = job_dir / "convert"
    input_dir.mkdir(exist_ok=True)
    audiveris_dir.mkdir(exist_ok=True)
    convert_dir.mkdir(exist_ok=True)

    image_path = save_image(data_url, input_dir)
    result: dict[str, Any] = {
        "job_dir": str(job_dir),
        "image_path": str(image_path),
        "audiveris": None,
        "musicxml_path": None,
        "midi_path": None,
        "converter": None,
        "errors": [],
    }

    audiveris_command = str(config.get("audiveris_command") or "").strip()
    if audiveris_command:
        command = render_template(
            audiveris_command,
            input=str(image_path),
            output=str(audiveris_dir),
            workdir=str(job_dir),
        )
        result["audiveris"] = run_command(command, cwd=job_dir)
    else:
        result["errors"].append("Audiveris command is not configured.")

    musicxml = find_latest_file(audiveris_dir, {".xml", ".mxl"})
    direct_midi = find_latest_file(audiveris_dir, {".mid", ".midi", ".kar"})
    if musicxml:
        result["musicxml_path"] = str(musicxml)
    if direct_midi:
        result["midi_path"] = str(direct_midi)
    if not musicxml and not direct_midi:
        result["errors"].append("No MusicXML output was found.")

    midi_target = convert_dir / "score.mid"
    converter_command = str(config.get("musicxml_to_midi_command") or "").strip()
    if musicxml and converter_command and not result["midi_path"]:
        command = render_template(
            converter_command,
            musicxml=str(musicxml),
            midi=str(midi_target),
            output=str(convert_dir),
            workdir=str(job_dir),
        )
        result["converter"] = run_command(command, cwd=job_dir)
        if midi_target.exists():
            result["midi_path"] = str(midi_target)
        else:
            midi_found = find_latest_file(convert_dir, {".mid", ".midi", ".kar"})
            if midi_found:
                result["midi_path"] = str(midi_found)
    elif musicxml:
        result["errors"].append(
            "MusicXML was produced, but no MIDI converter is configured."
        )

    with STATE.lock:
        STATE.last_result = result

    return result


def launch_template(template: str, **values: str) -> dict[str, Any]:
    if not template:
        return {"error": "Command is not configured."}
    command = render_template(template, **values)
    return start_command(command, cwd=STATE.work_root)


HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Music Score Recognition Hub</title>
  <style>
    :root { color-scheme: dark; }
    body { margin: 0; font-family: Inter, Arial, sans-serif; background: #0f1115; color: #e8ecf1; }
    .wrap { max-width: 1180px; margin: 0 auto; padding: 24px; }
    .grid { display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 20px; }
    .card { background: #171b22; border: 1px solid #2b3340; border-radius: 16px; padding: 18px; box-shadow: 0 8px 24px rgba(0,0,0,.22); }
    h1,h2,h3 { margin: 0 0 12px; }
    h1 { font-size: 28px; }
    h2 { font-size: 18px; color: #b8c4d8; }
    label { display: block; margin: 12px 0 6px; color: #9fb0c7; font-size: 13px; }
    input[type=text] { width: 100%; box-sizing: border-box; padding: 10px 12px; background: #0f131a; border: 1px solid #344055; color: #fff; border-radius: 10px; }
    button, .btn { background: #4d78ff; color: #fff; border: 0; border-radius: 10px; padding: 10px 14px; font-weight: 600; cursor: pointer; text-decoration: none; display: inline-block; margin-right: 8px; margin-top: 8px; }
    button.secondary, .btn.secondary { background: #263244; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    video, canvas, img.preview { width: 100%; border-radius: 14px; background: #05070b; border: 1px solid #2b3340; }
    pre { white-space: pre-wrap; word-break: break-word; background: #0f131a; padding: 14px; border-radius: 12px; border: 1px solid #2b3340; max-height: 320px; overflow: auto; }
    .row { display:flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    .muted { color: #9fb0c7; font-size: 13px; }
    .status { padding: 10px 12px; border-radius: 10px; background: #0f131a; border: 1px solid #2b3340; margin-top: 10px; }
    .ok { color: #8aff8a; }
    .warn { color: #ffd36b; }
    .bad { color: #ff8a8a; }
    @media (max-width: 960px) { .grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Music Score Recognition Hub</h1>
    <p class="muted">Capture a photo, run Audiveris, optionally convert MusicXML to MIDI, then open PianoBooster or VMPK.</p>
    <div class="grid">
      <div class="card">
        <h2>Capture</h2>
        <video id="video" autoplay playsinline></video>
        <canvas id="canvas" style="display:none;"></canvas>
        <div class="row">
          <button id="cameraStart">Start Camera</button>
          <button id="capture" class="secondary">Capture Frame</button>
          <label class="btn secondary" style="margin:0;">
            Upload Image
            <input id="fileInput" type="file" accept="image/*" style="display:none;">
          </label>
          <button id="run">Run Recognition</button>
        </div>
        <img id="preview" class="preview" alt="preview" style="display:none; margin-top: 12px;">
        <div class="status" id="captureStatus">No image selected yet.</div>
      </div>
      <div class="card">
        <h2>Commands</h2>
        <label>Audiveris command template</label>
        <input id="audiverisCommand" type="text" placeholder="java -jar ... -batch -export -output \"{output}\" \"{input}\"">
        <label>MusicXML to MIDI command template</label>
        <input id="converterCommand" type="text" placeholder="Optional: convert {musicxml} to {midi}">
        <label>PianoBooster command template</label>
        <input id="pianoCommand" type="text" placeholder="C:/.../pianobooster.exe \"{midi}\"">
        <label>VMPK command template</label>
        <input id="vmpkCommand" type="text" placeholder="C:/.../vmpk.exe">
        <div class="row">
          <button id="saveConfig" class="secondary">Save Local Template</button>
          <button id="reloadState" class="secondary">Refresh State</button>
        </div>
        <div class="status" id="configStatus"></div>
      </div>
    </div>
    <div class="grid" style="margin-top: 20px;">
      <div class="card">
        <h2>Result</h2>
        <div class="row">
          <a id="openPiano" class="btn secondary" href="#" style="display:none;">Open PianoBooster</a>
          <a id="openVmpk" class="btn secondary" href="#" style="display:none;">Open VMPK</a>
          <a id="openMusicXml" class="btn secondary" href="#" style="display:none;">Open MusicXML</a>
        </div>
        <pre id="resultBox">No run yet.</pre>
      </div>
      <div class="card">
        <h2>State</h2>
        <pre id="stateBox">Loading...</pre>
      </div>
    </div>
  </div>
  <script>
    const video = document.getElementById('video');
    const canvas = document.getElementById('canvas');
    const preview = document.getElementById('preview');
    const captureStatus = document.getElementById('captureStatus');
    const configStatus = document.getElementById('configStatus');
    const resultBox = document.getElementById('resultBox');
    const stateBox = document.getElementById('stateBox');
    const openPiano = document.getElementById('openPiano');
    const openVmpk = document.getElementById('openVmpk');
    const openMusicXml = document.getElementById('openMusicXml');
    let lastImageData = '';
    let localTemplate = JSON.parse(localStorage.getItem('msr.templates') || '{}');

    function setPreview(dataUrl) {
      lastImageData = dataUrl;
      preview.src = dataUrl;
      preview.style.display = 'block';
      captureStatus.textContent = 'Image ready.';
    }

    async function startCamera() {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false });
      video.srcObject = stream;
      captureStatus.textContent = 'Camera ready.';
    }

    async function fileToDataUrl(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
    }

    document.getElementById('cameraStart').onclick = async () => {
      try { await startCamera(); } catch (err) { captureStatus.textContent = 'Camera error: ' + err.message; }
    };

    document.getElementById('capture').onclick = async () => {
      if (!video.srcObject) { captureStatus.textContent = 'Start the camera first.'; return; }
      canvas.width = video.videoWidth || 1280;
      canvas.height = video.videoHeight || 720;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      setPreview(canvas.toDataURL('image/png'));
    };

    document.getElementById('fileInput').onchange = async (event) => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      setPreview(await fileToDataUrl(file));
    };

    function fillTemplatesFromStorage() {
      document.getElementById('audiverisCommand').value = localTemplate.audiverisCommand || '';
      document.getElementById('converterCommand').value = localTemplate.converterCommand || '';
      document.getElementById('pianoCommand').value = localTemplate.pianoCommand || '';
      document.getElementById('vmpkCommand').value = localTemplate.vmpkCommand || '';
    }

    function readTemplatesFromFields() {
      return {
        audiverisCommand: document.getElementById('audiverisCommand').value.trim(),
        converterCommand: document.getElementById('converterCommand').value.trim(),
        pianoCommand: document.getElementById('pianoCommand').value.trim(),
        vmpkCommand: document.getElementById('vmpkCommand').value.trim(),
      };
    }

    document.getElementById('saveConfig').onclick = () => {
      localTemplate = readTemplatesFromFields();
      localStorage.setItem('msr.templates', JSON.stringify(localTemplate));
      configStatus.innerHTML = '<span class="ok">Saved in browser local storage.</span>';
      refreshState();
    };

    async function refreshState() {
      const response = await fetch('/api/state');
      const data = await response.json();
      stateBox.textContent = JSON.stringify(data, null, 2);
      configStatus.innerHTML = [
        data.config.audiveris_command ? '<span class="ok">Audiveris configured</span>' : '<span class="warn">Audiveris not configured</span>',
        data.config.musicxml_to_midi_command ? '<span class="ok">Converter configured</span>' : '<span class="warn">Converter optional</span>',
        data.config.pianobooster_command ? '<span class="ok">PianoBooster configured</span>' : '<span class="warn">PianoBooster not configured</span>',
        data.config.vmpk_command ? '<span class="ok">VMPK configured</span>' : '<span class="warn">VMPK not configured</span>',
      ].join('<br>');
    }

    async function openTarget(target) {
      const response = await fetch('/api/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target }),
      });
      const data = await response.json();
      resultBox.textContent = JSON.stringify(data, null, 2);
    }

    document.getElementById('run').onclick = async () => {
      if (!lastImageData) { captureStatus.textContent = 'Pick or capture an image first.'; return; }
      localTemplate = readTemplatesFromFields();
      localStorage.setItem('msr.templates', JSON.stringify(localTemplate));
      const payload = {
        image: lastImageData,
        localTemplates: localTemplate,
      };
      captureStatus.textContent = 'Running recognition...';
      const response = await fetch('/api/recognize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      resultBox.textContent = JSON.stringify(data, null, 2);
      if (data.musicxml_path) {
        openMusicXml.style.display = 'inline-block';
        openMusicXml.onclick = () => openTarget('musicxml');
      }
      if (data.midi_path) {
        openPiano.style.display = 'inline-block';
        openPiano.onclick = () => openTarget('pianobooster');
      }
      openVmpk.style.display = 'inline-block';
      openVmpk.onclick = () => openTarget('vmpk');
      captureStatus.textContent = data.midi_path ? 'Done. MIDI is ready.' : 'Done. Check MusicXML output.';
      await refreshState();
    };

    openMusicXml.onclick = () => openTarget('musicxml');
    openPiano.onclick = () => openTarget('pianobooster');
    openVmpk.onclick = () => openTarget('vmpk');

    document.getElementById('reloadState').onclick = refreshState;

    fillTemplatesFromStorage();
    refreshState();
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(HTML_PAGE)
            return
        if parsed.path == "/api/state":
            self._send_json(STATE.snapshot())
            return
        self.send_error(404, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        if parsed.path == "/api/recognize":
            try:
                payload = json.loads(body.decode("utf-8"))
                image = str(payload.get("image") or "")
                if not image:
                    self._send_json({"error": "Missing image data."}, status=400)
                    return
                with STATE.lock:
                    if isinstance(payload.get("localTemplates"), dict):
                        for key, value in payload["localTemplates"].items():
                            if key == "audiverisCommand":
                                STATE.config["audiveris_command"] = value
                            elif key == "converterCommand":
                                STATE.config["musicxml_to_midi_command"] = value
                            elif key == "pianoCommand":
                                STATE.config["pianobooster_command"] = value
                            elif key == "vmpkCommand":
                                STATE.config["vmpk_command"] = value
                result = recognize_image(image)
                self._send_json(result)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=500)
            return

        if parsed.path == "/api/launch":
            try:
                payload = json.loads(body.decode("utf-8"))
                target = str(payload.get("target") or "")
                result: dict[str, Any]
                with STATE.lock:
                    last = STATE.last_result or {}
                    musicxml_path = str(last.get("musicxml_path") or "")
                    midi_path = str(last.get("midi_path") or "")
                    if target == "pianobooster":
                        result = launch_template(
                            str(STATE.config.get("pianobooster_command") or ""),
                            midi=midi_path,
                            musicxml=musicxml_path,
                            workdir=str(STATE.work_root),
                        )
                    elif target == "vmpk":
                        result = launch_template(
                            str(STATE.config.get("vmpk_command") or ""),
                            midi=midi_path,
                            musicxml=musicxml_path,
                            workdir=str(STATE.work_root),
                        )
                    elif target == "musicxml":
                        if musicxml_path:
                            os.startfile(musicxml_path)
                            result = {"opened": musicxml_path}
                        else:
                            result = {"error": "No MusicXML file available."}
                    else:
                        result = {"error": "Unknown target."}
                self._send_json(result)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=500)
            return

        self.send_error(404, "Not found")

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    STATE.reload()
    host = str(STATE.config.get("host") or DEFAULT_CONFIG["host"])
    port = int(STATE.config.get("port") or DEFAULT_CONFIG["port"])
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Music Score Recognition Hub running at http://{host}:{port}")
    print(f"Workspace: {STATE.work_root}")
    if CONFIG_EXAMPLE_PATH.exists() and not CONFIG_PATH.exists():
        print(f"Create {CONFIG_PATH.name} from {CONFIG_EXAMPLE_PATH.name} before first use.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
