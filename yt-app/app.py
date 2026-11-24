import os
import re
import queue
import threading
from flask import Flask, render_template_string, request, Response, jsonify
import yt_dlp

app = Flask(__name__)
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/srv/music")
PLAYLISTS_DIR = os.environ.get("PLAYLISTS_DIR", "/srv/playlists")
PLAYLIST_MEDIA_BASE_DIR = os.environ.get("PLAYLIST_MEDIA_BASE_DIR", "/music/Mp3")

os.makedirs(MUSIC_DIR, exist_ok=True)
os.makedirs(PLAYLISTS_DIR, exist_ok=True)

MEDIA_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".mp4", ".mkv", ".webm"}

# Globalna kolejka na logi i stan pobierania
log_queue = queue.Queue()
playlist_lock = threading.Lock()


class WebLogger:
    def debug(self, msg):
        if not msg.startswith('[debug] '):
            log_queue.put(msg)

    def info(self, msg):
        log_queue.put(msg)

    def warning(self, msg):
        log_queue.put(f"OSTRZEŻENIE: {msg}")

    def error(self, msg):
        log_queue.put(f"BŁĄD: {msg}")


def sanitize_subdir(subdir: str) -> str:
    """
    Zwraca bezpieczną względną ścieżkę podkatalogu wewnątrz MUSIC_DIR
    lub pusty ciąg znaków dla katalogu głównego.
    """
    if not subdir:
        return ""
    cleaned = subdir.strip().replace("\\", "/").strip("/")
    if not cleaned:
        return ""

    parts = []
    for part in cleaned.split("/"):
        part = part.strip()
        if part in ("", ".", ".."):
            continue
        safe_part = re.sub(r"[^\w\s\-.]", "_", part, flags=re.UNICODE).strip(" .")
        if safe_part:
            parts.append(safe_part)

    return os.path.join(*parts) if parts else ""


def get_target_dir(subdir: str) -> str:
    safe_sub = sanitize_subdir(subdir)
    target = os.path.abspath(os.path.join(MUSIC_DIR, safe_sub))
    base = os.path.abspath(MUSIC_DIR)
    if target != base and not target.startswith(base + os.sep):
        return base
    return target


def get_subdirs():
    """Zwraca listę podkatalogów (w tym zagnieżdżonych) względem MUSIC_DIR."""
    if not os.path.exists(MUSIC_DIR):
        return []
    dirs = []
    base_abs = os.path.abspath(MUSIC_DIR)
    for root, dirnames, _ in os.walk(base_abs):
        dirnames.sort(key=str.lower)
        for d in dirnames:
            full_path = os.path.join(root, d)
            rel_path = os.path.relpath(full_path, base_abs).replace("\\", "/")
            dirs.append(rel_path)
    return sorted(dirs, key=str.lower)


def get_files(subdir: str = ""):
    target_dir = get_target_dir(subdir)
    if not os.path.exists(target_dir):
        return []
    files_info = []
    for filename in sorted(os.listdir(target_dir), reverse=True):
        filepath = os.path.join(target_dir, filename)
        if os.path.isfile(filepath):
            size_bytes = os.path.getsize(filepath)
            size_mb = f"{size_bytes / (1024 * 1024):.2f} MB"
            files_info.append({"name": filename, "size": size_mb})
    return files_info


def rebuild_all_playlists() -> list[str]:
    """
    Przebudowuje pliki .m3u w PLAYLISTS_DIR na podstawie katalogów w MUSIC_DIR,
    wpisując ścieżki docelowe z prefiksem PLAYLIST_MEDIA_BASE_DIR (/srv/disk0/Music/Mp3).
    """
    with playlist_lock:
        os.makedirs(PLAYLISTS_DIR, exist_ok=True)
        base_abs = os.path.abspath(MUSIC_DIR)
        host_base = PLAYLIST_MEDIA_BASE_DIR.rstrip("/")
        created = []

        candidate_dirs = [base_abs]
        for root, dirnames, _ in os.walk(base_abs):
            dirnames.sort(key=str.lower)
            for d in dirnames:
                candidate_dirs.append(os.path.join(root, d))

        for dir_path in candidate_dirs:
            media_files = []
            for entry in sorted(os.listdir(dir_path), key=str.lower):
                full_path = os.path.join(dir_path, entry)
                if os.path.isfile(full_path):
                    ext = os.path.splitext(entry)[1].lower()
                    if ext in MEDIA_EXTENSIONS:
                        rel_file = os.path.relpath(full_path, base_abs).replace("\\", "/")
                        host_file_path = f"{host_base}/{rel_file}"
                        media_files.append(host_file_path)

            rel_dir = os.path.relpath(dir_path, base_abs)
            if rel_dir == ".":
                playlist_base = "music"
            else:
                playlist_base = rel_dir.replace("\\", "/").replace("/", "_")

            playlist_path = os.path.join(PLAYLISTS_DIR, f"{playlist_base}.m3u")

            if not media_files:
                if os.path.exists(playlist_path):
                    os.remove(playlist_path)
                continue

            lines = ["#EXTM3U"] + media_files

            with open(playlist_path, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines) + "\n")

            created.append(os.path.basename(playlist_path))

        return created


def download_worker(url, fmt, subdir):
    target_dir = get_target_dir(subdir)
    os.makedirs(target_dir, exist_ok=True)

    ydl_opts = {
        'outtmpl': os.path.join(target_dir, '%(title)s [%(id)s].%(ext)s'),
        'noplaylist': True,
        'js_runtimes': {'node': {}},
        'extractor_args': {'youtube': {'player_client': ['android']}},
        'logger': WebLogger(),
    }

    if fmt == "mp3":
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    else:
        ydl_opts['format'] = 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4] / bv+ba/b'
        ydl_opts['merge_output_format'] = 'mp4'

    try:
        log_queue.put(f"=== ROZPOCZĘTO POBIERANIE ({fmt.upper()}) do: {target_dir} ===")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        log_queue.put("=== POBIERANIE ZAKOŃCZONE SUKCESEM ===")
        rebuilt = rebuild_all_playlists()
        log_queue.put(f"Zaktualizowano playlisty ({len(rebuilt)}): {', '.join(rebuilt) if rebuilt else 'brak'}")
    except Exception as e:
        log_queue.put(f"BŁĄD KRYTYCZNY: {e}")

    log_queue.put("DONE")


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YT Downloader Live</title>
    <style>
        body { font-family: sans-serif; max-width: 720px; margin: 30px auto; padding: 20px; background: #121212; color: #fff; }
        label { display: block; margin-bottom: 6px; font-size: 14px; color: #ccc; }
        input[type="text"], select {
            width: 100%; padding: 12px; margin-bottom: 14px; border-radius: 6px;
            border: 1px solid #333; background: #222; color: #fff; box-sizing: border-box; font-size: 14px;
        }
        .dir-row { display: flex; gap: 10px; }
        .dir-row > div { flex: 1; }
        .hint { font-size: 12px; color: #888; margin-top: -8px; margin-bottom: 14px; }
        .btn-group { display: flex; gap: 10px; margin-bottom: 12px; }
        button { flex: 1; padding: 12px; border: none; border-radius: 6px; cursor: pointer; font-weight: bold; font-size: 14px; }
        .btn-mp4 { background: #e50914; color: white; }
        .btn-mp3 { background: #1db954; color: white; }
        .btn-rebuild { width: 100%; background: #2b5cb8; color: white; margin-bottom: 12px; }
        .status-msg { font-size: 13px; color: #9ae6b4; margin-bottom: 18px; min-height: 18px; word-break: break-word; }

        .terminal {
            background: #000; color: #0f0; font-family: monospace; padding: 15px;
            border-radius: 6px; height: 200px; overflow-y: scroll; border: 1px solid #333;
            white-space: pre-wrap; font-size: 13px; display: none; margin-bottom: 20px;
        }

        .files-container { border-top: 1px solid #333; padding-top: 20px; }
        .file-list { list-style: none; padding: 0; margin: 0; }
        .file-item {
            display: flex; justify-content: space-between; align-items: center;
            padding: 10px; background: #1e1e1e; margin-bottom: 8px; border-radius: 4px; font-size: 14px;
        }
        .file-name { word-break: break-all; margin-right: 10px; }
        .file-size { color: #888; font-size: 12px; white-space: nowrap; }
    </style>
</head>
<body>
    <h2>YT Downloader (Live Logs)</h2>
    <form id="download-form" onsubmit="return false;">
        <label for="url">Link z YouTube</label>
        <input type="text" id="url" name="url" placeholder="Wklej link z YouTube..." required>

        <div class="dir-row">
            <div>
                <label for="subdir-select">Wybierz istniejący podkatalog</label>
                <select id="subdir-select" onchange="onSelectChange()">
                    <option value="">(katalog główny)</option>
                    {% for d in subdirs %}
                    <option value="{{ d }}" {% if d == current_subdir %}selected{% endif %}>{{ d }}</option>
                    {% endfor %}
                </select>
            </div>
            <div>
                <label for="subdir-input">Lub wpisz / utwórz nowy podkatalog</label>
                <input type="text" id="subdir-input" value="{{ current_subdir }}" placeholder="np. rock lub synthwave/80s" oninput="onInputChange()">
            </div>
        </div>
        <div class="hint">Docelowa ścieżka: <strong id="target-preview">{{ music_dir }}</strong></div>

        <div class="btn-group">
            <button type="button" onclick="startDownload('mp4')" class="btn-mp4">Pobierz MP4</button>
            <button type="button" onclick="startDownload('mp3')" class="btn-mp3">Pobierz MP3</button>
        </div>

        <button type="button" onclick="rebuildPlaylists()" class="btn-rebuild">🔄 Przebuduj playlisty (.m3u)</button>
        <div id="rebuild-status" class="status-msg"></div>
    </form>

    <div id="terminal" class="terminal">Oczekiwanie na start...\\n</div>

    <div class="files-container">
        <h3>Pliki w <span id="files-dir-label">{{ target_dir }}</span>:</h3>
        <ul class="file-list" id="file-list">
            {% for file in files %}
            <li class="file-item">
                <span class="file-name">📄 {{ file.name }}</span>
                <span class="file-size">{{ file.size }}</span>
            </li>
            {% else %}
            <li class="file-item"><span class="file-name" style="color:#888;">Brak plików w tym katalogu.</span></li>
            {% endfor %}
        </ul>
    </div>

    <script>
        const BASE_DIR = {{ music_dir | tojson }};

        function getActiveSubdir() {
            const inputVal = document.getElementById('subdir-input').value.trim();
            return inputVal;
        }

        function updatePreviewAndFiles() {
            const sub = getActiveSubdir().replace(/^\\/+|\\/+$/g, '');
            const fullPath = sub ? `${BASE_DIR}/${sub}` : BASE_DIR;
            document.getElementById('target-preview').innerText = fullPath;
            document.getElementById('files-dir-label').innerText = fullPath;

            fetch('/api/files?subdir=' + encodeURIComponent(sub))
                .then(r => r.json())
                .then(data => {
                    const list = document.getElementById('file-list');
                    list.innerHTML = '';
                    if (!data.files || data.files.length === 0) {
                        list.innerHTML = '<li class="file-item"><span class="file-name" style="color:#888;">Brak plików w tym katalogu.</span></li>';
                        return;
                    }
                    data.files.forEach(f => {
                        const li = document.createElement('li');
                        li.className = 'file-item';
                        li.innerHTML = `<span class="file-name">📄 ${escapeHtml(f.name)}</span><span class="file-size">${escapeHtml(f.size)}</span>`;
                        list.appendChild(li);
                    });
                });
        }

        function onSelectChange() {
            const select = document.getElementById('subdir-select');
            document.getElementById('subdir-input').value = select.value;
            updatePreviewAndFiles();
        }

        function onInputChange() {
            const val = document.getElementById('subdir-input').value.trim();
            const select = document.getElementById('subdir-select');
            const exists = Array.from(select.options).some(opt => opt.value === val);
            select.value = exists ? val : '';
            updatePreviewAndFiles();
        }

        function escapeHtml(str) {
            return str.replace(/[&<>"']/g, m => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
            }[m]));
        }

        function rebuildPlaylists() {
            const statusEl = document.getElementById('rebuild-status');
            statusEl.style.color = '#ccc';
            statusEl.innerText = 'Przebudowywanie playlist...';

            fetch('/api/rebuild-playlists', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (data.ok) {
                        statusEl.style.color = '#9ae6b4';
                        statusEl.innerText = `Gotowe (${data.count}) w ${data.playlists_dir}: ${data.playlists.join(', ') || 'brak'}`;
                    } else {
                        statusEl.style.color = '#feb2b2';
                        statusEl.innerText = 'Błąd: ' + (data.error || 'nieznany');
                    }
                })
                .catch(err => {
                    statusEl.style.color = '#feb2b2';
                    statusEl.innerText = 'Błąd połączenia: ' + err;
                });
        }

        function startDownload(format) {
            const urlInput = document.getElementById('url').value.trim();
            const subdir = getActiveSubdir();

            if (!urlInput) {
                alert('Wklej najpierw link!');
                return;
            }

            const terminal = document.getElementById('terminal');
            terminal.style.display = 'block';
            terminal.innerText = 'Rozpoczynanie zadania...\\n';

            const params = new URLSearchParams();
            params.append('url', urlInput);
            params.append('format', format);
            params.append('subdir', subdir);

            fetch('/download', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: params.toString()
            }).then(response => {
                if (response.ok) {
                    const evtSource = new EventSource('/logs');
                    evtSource.onmessage = function(event) {
                        if (event.data === "DONE") {
                            evtSource.close();
                            terminal.innerText += "\\n--- Zakończono! Odświeżam listę plików... ---\\n";
                            setTimeout(() => {
                                const currentSub = encodeURIComponent(getActiveSubdir());
                                window.location.href = '/?subdir=' + currentSub;
                            }, 1200);
                        } else {
                            terminal.innerText += event.data + "\\n";
                            terminal.scrollTop = terminal.scrollHeight;
                        }
                    };
                }
            });
        }
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    current_subdir = sanitize_subdir(request.args.get("subdir", ""))
    target_dir = get_target_dir(current_subdir)
    return render_template_string(
        HTML_TEMPLATE,
        files=get_files(current_subdir),
        subdirs=get_subdirs(),
        current_subdir=current_subdir,
        music_dir=MUSIC_DIR,
        target_dir=target_dir,
    )


@app.route("/api/files")
def api_files():
    subdir = request.args.get("subdir", "")
    return jsonify({"files": get_files(subdir)})


@app.route("/api/rebuild-playlists", methods=["POST"])
def api_rebuild_playlists():
    try:
        created = rebuild_all_playlists()
        return jsonify({
            "ok": True,
            "count": len(created),
            "playlists": created,
            "playlists_dir": PLAYLISTS_DIR,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/download", methods=["POST"])
def download():
    url = request.form.get("url")
    fmt = request.form.get("format")
    subdir = request.form.get("subdir", "")

    while not log_queue.empty():
        log_queue.get()

    t = threading.Thread(target=download_worker, args=(url, fmt, subdir))
    t.start()
    return "", 204


@app.route("/logs")
def logs():
    def generate():
        while True:
            try:
                msg = log_queue.get(timeout=20)
                yield f"data: {msg}\n\n"
                if msg == "DONE":
                    break
            except queue.Empty:
                yield "data: [keep-alive]\n\n"

    return Response(generate(), mimetype="text/event-stream", headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
        'Connection': 'keep-alive'
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)