from pathlib import Path
import argparse
import hashlib
import json
import sys
import urllib.request
import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "VeTay"
VERSION = "0.2.15"
PATCH_ABI = "vetay-windows-026"
PATCH_ROOT = "https://raw.githubusercontent.com/taikhoanzaizai-ux/vetay/"

if (ROOT / "ffmpeg.exe").is_file():
    os.environ["FFMPEG_BINARY"] = str(ROOT / "ffmpeg.exe")
LOCK = threading.Lock()
LAST_ANALYSIS = {}
VIDEO_SIZES = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "4:3": (1440, 1080),
    "21:9": (2520, 1080),
}

def engine():
    path = ROOT / "engine"
    if not (path / "handdraw" / "render.py").is_file():
        raise RuntimeError("Bộ render Whiteboard Animation VI bị thiếu trong bản cài.")
    sys.path.insert(0, str(path))
    from handdraw.render import render_video
    from handdraw.settings import Settings
    return path, render_video, Settings

def ffmpeg(*args):
    exe = os.environ.get("FFMPEG_BINARY") or shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("Không tìm thấy FFmpeg. Cài FFmpeg hoặc đặt FFMPEG_BINARY.")
    return subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
                          check=True, capture_output=True)

def duration(path):
    import wave
    with wave.open(str(path), "rb") as track:
        return track.getnframes() / track.getframerate()

def transcribe(voice):
    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8", download_root=str(DATA / "models"))
    segments, _ = model.transcribe(str(voice), language="vi", vad_filter=True, word_timestamps=True)
    return [(float(word.start), float(word.end), word.word)
            for segment in segments for word in (segment.words or ()) if word.word.strip()]

def scenes(words, seconds):
    if not words or seconds <= 0:
        raise ValueError("Không tìm thấy lời đọc hoặc âm thanh trống.")
    blocks = [(0.0, [])]
    previous_end = 0.0
    previous_word = ""
    for start, end, word in words:
        scene_start, current = blocks[-1]
        elapsed = start - scene_start
        pause = start - previous_end
        sentence_end = bool(re.search(r'[.!?…]+[)"”\']*$', previous_word.rstrip()))
        can_cut = current and seconds - start >= 2
        if can_cut and (elapsed >= 14 or (elapsed >= 4.5 and pause >= .55)
                        or (elapsed >= 6.5 and sentence_end)):
            current = []
            blocks.append((start, current))
        current.append(word)
        previous_end = end
        previous_word = word
    end_frame = round(seconds * 24)
    result = []
    for i, (start, words) in enumerate(blocks):
        first = round(start * 24)
        last = round(blocks[i+1][0] * 24) if i+1 < len(blocks) else end_frame
        text = "".join(words).strip()
        # Whiteboard Animation VI accepts up to 120 seconds for a single render.
        while last - first > 120 * 24:
            result.append((first, first + 120 * 24, text))
            first += 120 * 24
        if last > first:
            result.append((first, last, text))
    return result

def prompt_for(text, ratio="9:16"):
    rules = (
        (r"cà phê|ăn vặt|đồ uống", "ly cà phê và món ăn nhẹ đặt cạnh ví tiền"),
        (r"tiết kiệm|dành dụm", "hũ tiết kiệm, bàn tay bỏ đồng xu vào hũ"),
        (r"nhón gót|bắp chân", "người đang nhón gót, góc nhìn ngang dễ thấy bàn chân"),
        (r"thanh toán|chi tiêu|chi phí", "ví tiền, hóa đơn và vài đồng xu"),
        (r"một năm|mỗi tháng|mỗi tuần|365 ngày", "lịch để bàn và mũi tên chỉ thời gian"),
    )
    visuals = next((art for key, art in rules if re.search(key, text, re.I)), "")
    direction = (f"Gợi ý hình ảnh trọng tâm: {visuals}. " if visuals else
                 "Chọn một chủ thể và một hành động cụ thể có trong lời đọc. ")
    return (f"Tạo một ảnh minh họa tỷ lệ {ratio} để chuyển thành video vẽ tay. "
            "Nền trắng trơn, đường viền đậm rõ, ít chi tiết, chủ thể tách nền, "
            "màu xanh ngọc và vàng nhạt. Không chữ, số, logo hoặc watermark. "
            + direction + "Dùng lời đọc sau làm ngữ cảnh, không chép thành chữ trên ảnh: " + text)

def timecode(frame):
    ms = round(frame * 1000 / 24)
    minutes, remainder = divmod(ms, 60000)
    seconds, fraction = divmod(remainder, 1000)
    return f"{minutes:02}:{seconds:02}.{fraction:03}"

def scene_rows(plan, ratio="9:16"):
    return [[i, timecode(start), timecode(end), round((end-start)/24, 3), text, prompt_for(text, ratio)]
            for i, (start, end, text) in enumerate(plan, 1)]

def prepare_audio(script, voice, folder):
    wav = folder / "voice.wav"
    if not voice:
        if not script or not script.strip():
            raise ValueError("Nhập kịch bản hoặc tải bản thu.")
        import edge_tts
        speech = folder / "speech.mp3"
        asyncio.run(edge_tts.Communicate(script.strip(), "vi-VN-NamMinhNeural").save(str(speech)))
        voice = speech
    ffmpeg("-i", voice, "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", wav)
    return wav

def render_folder(voice, pictures, plan, destination, report, ratio="9:16"):
    from PIL import Image, ImageOps
    import numpy as np
    path, render_video, Settings = engine()
    width, height = VIDEO_SIZES[ratio]
    preview_width, preview_height = ((size * 2 // 5) // 2 * 2 for size in (width, height))
    if len(pictures) < len(plan):
        raise ValueError(f"Cần {len(plan)} ảnh, hiện có {len(pictures)}.")
    with tempfile.TemporaryDirectory(prefix="vetay-", dir=destination.parent) as work:
        work = Path(work)
        clips = []
        for i, ((start, end, _), picture) in enumerate(zip(plan, pictures)):
            report(f"Whiteboard Animation VI: cảnh {i+1}/{len(plan)}…")
            with Image.open(picture) as raw:
                art = ImageOps.contain(ImageOps.exif_transpose(raw).convert("RGB"), (preview_width, preview_height))
            canvas = Image.new("RGB", (preview_width, preview_height), "white")
            canvas.paste(art, ((preview_width-art.width)//2, (preview_height-art.height)//2))
            settings = Settings(device="cpu", encoder="cpu", width=preview_width, height=preview_height, fps=24, fit="contain",
                                duration=(end-start)/24, draw_percent=70, mode="e",
                                subject_first=(path / "assets/v181d/model.safetensors").is_file())
            clip, _ = render_video(np.array(canvas), settings, output_dir=work)
            clips.append(clip)
        listing = work / "clips.txt"
        listing.write_text("".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
        output = work / "complete.mp4"
        ffmpeg("-f", "concat", "-safe", "0", "-i", listing, "-i", voice, "-map", "0:v", "-map", "1:a",
               "-vf", f"scale={width}:{height}:flags=lanczos", "-c:v", "libx264", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-af", "apad", "-shortest", "-movflags", "+faststart", output)
        os.replace(output, destination)
    return str(destination)

def patch_read(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "VeTay-Windows", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Gói cập nhật vượt kích thước cho phép.")
    return data

def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Số phiên bản không hợp lệ.")
    return tuple(map(int, value.split(".")))

def sync_chrome_bundle():
    folder = DATA / "chrome-extension"
    ref = json.loads(patch_read(PATCH_ROOT + "main/companion.json", 16384))
    revision = ref["commit"]
    names = {"manifest.json", "worker.js", "flow.js", "popup.html"}
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or set(ref["files"]) != names:
        raise ValueError("Danh sách tệp tiện ích không hợp lệ.")
    manifest_path = folder / "manifest.json"
    old = json.loads(manifest_path.read_bytes()) if manifest_path.is_file() else None
    if old and version_tuple(old["version"]) >= version_tuple(ref["version"]):
        return old["version"], False
    content = {}
    for name, expected in ref["files"].items():
        data = patch_read(PATCH_ROOT + revision + "/chrome-extension/" + name, 1024 * 1024)
        blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{40}", expected) or blob != expected:
            raise ValueError(f"Tệp tiện ích {name} không khớp mã kiểm tra GitHub.")
        content[name] = data
    manifest = json.loads(content["manifest.json"])
    if manifest["version"] != ref["version"] or manifest["manifest_version"] != 3:
        raise ValueError("Phiên bản tiện ích tải về không khớp.")
    if old:
        permissions = ("permissions", "host_permissions", "optional_permissions", "optional_host_permissions")
        scopes = lambda m: sorted(pattern for script in m.get("content_scripts", []) for pattern in script.get("matches", []))
        if (old["name"] != manifest["name"] or scopes(old) != scopes(manifest)
            or any(sorted(old.get(key, [])) != sorted(manifest.get(key, [])) for key in permissions)):
            raise ValueError("Bản tiện ích mới thay đổi quyền Chrome và cần xác nhận riêng.")
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chrome-patch-", dir=DATA) as temp:
        pending = Path(temp)
        backup = {name: (folder / name).read_bytes() for name in names if (folder / name).is_file()}
        written = []
        try:
            for name in ("worker.js", "flow.js", "popup.html", "manifest.json"):
                path = pending / name
                path.write_bytes(content[name])
                os.replace(path, folder / name)
                written.append(name)
        except OSError:
            for name in reversed(written):
                target = folder / name
                if name in backup:
                    path = pending / name
                    path.write_bytes(backup[name])
                    os.replace(path, target)
                else:
                    target.unlink(missing_ok=True)
            raise
    return manifest["version"], True

def prepare_chrome_bundle():
    if not LOCK.acquire(blocking=False):
        return "Đợi tác vụ đang chạy kết thúc trước khi chuẩn bị tiện ích."
    try:
        version, _ = sync_chrome_bundle()
        folder = DATA / "chrome-extension"
        os.startfile(str(folder))
        return (f"Tiện ích {version} tại {folder}. Trong Chrome, mở chrome://extensions và "
                "nạp thư mục này một lần. Giữ nguyên đường dẫn để tự cập nhật sau này.")
    except Exception as error:
        return f"Không chuẩn bị được tiện ích Chrome: {error}"
    finally:
        LOCK.release()

def refresh_existing_extension():
    if not (DATA / "chrome-extension" / "manifest.json").is_file():
        return
    with LOCK:
        try:
            sync_chrome_bundle()
        except (OSError, ValueError, KeyError):
            pass

def update():
    if not LOCK.acquire(blocking=False):
        return "Đợi video xuất xong trước khi cập nhật."
    restart = False
    try:
        manifest = json.loads(patch_read(PATCH_ROOT + "main/update.json", 16384))
        version = manifest["version"]
        companion = ""
        if (DATA / "chrome-extension" / "manifest.json").is_file():
            ext_version, changed = sync_chrome_bundle()
            if changed:
                companion = f" Đã đồng bộ tiện ích Chrome {ext_version}. Chrome sẽ tự nạp lại khi rảnh."
        if version_tuple(version) <= version_tuple(VERSION):
            return f"Vẽ Tay đang ở bản mới nhất ({VERSION})." + companion
        if manifest["abi"] != PATCH_ABI:
            return "Bản này cần nâng cấp thư viện. Tải bộ cài tại trang GitHub Releases."
        revision = manifest["commit"]
        expected = manifest["git_blob"]
        if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value)
                   for value in (revision, expected)):
            raise ValueError("Mã kiểm tra gói cập nhật không hợp lệ.")
        payload = patch_read(PATCH_ROOT + revision + "/app.py", 1024 * 1024)
        actual = hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()
        if actual != expected:
            raise ValueError("Gói tải về không khớp mã kiểm tra GitHub.")
        source = payload.decode("utf-8")
        if f'VERSION = "{version}"' not in source.splitlines():
            raise ValueError("Phiên bản trong gói cập nhật không khớp.")
        target = ROOT / "app.py"
        compile(source, str(target), "exec")
        with tempfile.TemporaryDirectory(prefix="vetay-patch-", dir=ROOT) as folder:
            pending = Path(folder) / "app.py"
            pending.write_bytes(payload)
            backup = Path(folder) / "previous.py"
            shutil.copyfile(target, backup)
            os.replace(pending, target)
            try:
                subprocess.Popen([sys.executable, str(target)], cwd=ROOT)
            except OSError:
                os.replace(backup, target)
                raise
        restart = True
        threading.Timer(1.5, lambda: os._exit(0)).start()
        return f"Đã cập nhật nhẹ lên {version}. Đang mở lại Vẽ Tay…"
    except Exception as error:
        return f"Cập nhật thất bại: {error}"
    finally:
        if not restart:
            LOCK.release()


def open_chrome_flow(token):
    candidates = [Path(os.environ[key]) / "Google/Chrome/Application/chrome.exe"
                  for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA") if os.environ.get(key)]
    chrome = next((path for path in candidates if path.is_file()), None)
    if chrome is None:
        raise RuntimeError("Không tìm thấy Google Chrome trên máy.")
    project = "https://flow.google.com/#vetay=" + token
    subprocess.Popen([str(chrome), "--new-tab", project])

class ChromeFlowBridge:
    def __init__(self):
        self.token = uuid.uuid4().hex
        self.guard = threading.Lock()
        self.job = None

    def serve(self, port=7877):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        bridge = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def response(self, status, data):
                content = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_GET(self):
                import hmac
                if self.path == "/extension-idle":
                    with bridge.guard:
                        idle = bridge.job is None and not LOCK.locked()
                    return self.response(200, {"idle": idle})
                if not hmac.compare_digest(self.headers.get("X-VeTay-Token", ""), bridge.token):
                    return self.response(403, "Mã kết nối không đúng.")
                if self.path != "/next":
                    return self.response(404, "Không tìm thấy.")
                with bridge.guard:
                    state = bridge.job
                    index = len(state["paths"]) if state else 0
                    if not state or state["error"] or index >= len(state["prompts"]):
                        return self.response(200, {"done": True})
                    result = {"job": state["id"], "index": index, "prompt": state["prompts"][index]}
                self.response(200, result)

            def do_POST(self):
                import hmac
                import io
                from PIL import Image, ImageOps
                if not hmac.compare_digest(self.headers.get("X-VeTay-Token", ""), bridge.token):
                    return self.response(403, "Mã kết nối không đúng.")
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if size <= 0 or size > 24 * 1024 * 1024:
                        raise ValueError("Kích thước dữ liệu không hợp lệ.")
                    self.connection.settimeout(20)
                    with bridge.guard:
                        state = bridge.job
                        if not state or state["error"]:
                            return self.response(409, "Không có tác vụ đang chờ.")
                        index = len(state["paths"])
                        if self.path == "/error" and size <= 8192:
                            state["error"] = str(json.loads(self.rfile.read(size))["message"])[:600]
                        elif self.path == f"/image/{state['id']}/{index}" and index < len(state["prompts"]):
                            payload = self.rfile.read(size)
                            if len(payload) != size:
                                raise ValueError("Ảnh chưa tải đủ.")
                            with Image.open(io.BytesIO(payload)) as image:
                                if image.width * image.height > 32_000_000:
                                    raise ValueError("Ảnh quá lớn.")
                                target = state["folder"] / f"chrome-{index:04}.png"
                                ImageOps.exif_transpose(image).convert("RGB").save(target)
                            state["paths"].append(target)
                        else:
                            return self.response(409, "Ảnh không khớp mã hoặc thứ tự cảnh.")
                        state["event"].set()
                    self.response(200, {"received": True})
                except Exception as error:
                    self.response(400, str(error)[:300])

        import socket
        class ExclusiveServer(ThreadingHTTPServer):
            allow_reuse_address = False

            def server_bind(self):
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

        server = ExclusiveServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def images(self, prompts, folder, report):
        state = {"id": uuid.uuid4().hex, "prompts": prompts, "folder": folder,
                 "paths": [], "event": threading.Event(), "error": None}
        with self.guard:
            self.token = uuid.uuid4().hex
            self.job = state
        try:
            open_chrome_flow(self.token)
            report("Đã mở Google Flow. Đang chờ tiện ích tạo dự án mới…")
            while True:
                if not state["event"].wait(240):
                    raise RuntimeError("Hết thời gian chờ Chrome. Kiểm tra tiện ích Vẽ Tay bản 1.2.0, tài khoản Flow và chế độ tạo ảnh.")
                with self.guard:
                    state["event"].clear()
                    if state["error"]:
                        raise RuntimeError("Google Flow: " + state["error"])
                    count = len(state["paths"])
                    if count == len(prompts):
                        return state["paths"].copy()
                report(f"Chrome đã trả {count}/{len(prompts)} ảnh.")
        finally:
            with self.guard:
                self.job = None

def ui(check=False):
    import gradio as gr
    DATA.mkdir(parents=True, exist_ok=True)
    bridge = ChromeFlowBridge()
    chrome_server = None
    def run(script, voice, pictures, provider, ratio, render, progress=gr.Progress()):
        def report(message):
            progress(None, desc=message)
        if not LOCK.acquire(blocking=False):
            raise gr.Error("Một tác vụ đang chạy.")
        rows = []
        try:
            with tempfile.TemporaryDirectory(prefix="input-", dir=DATA) as folder:
                folder = Path(folder)
                fingerprint = hashlib.sha256()
                if voice:
                    with Path(voice).open("rb") as source:
                        while chunk := source.read(1024 * 1024):
                            fingerprint.update(chunk)
                else:
                    fingerprint.update((script or "").encode("utf-8"))
                key = ("voice" if voice else "script", fingerprint.hexdigest())
                cache = DATA / "last-analysis.wav"
                if LAST_ANALYSIS.get("key") == key and cache.is_file():
                    wav, plan = cache, LAST_ANALYSIS["plan"]
                else:
                    yield [], None, "Đang chuẩn bị lời đọc…"
                    wav = prepare_audio(script, voice, folder)
                    yield [], None, "Đang nhận dạng lời đọc và chia cảnh…"
                    plan = scenes(transcribe(wav), duration(wav))
                    os.replace(wav, cache)
                    wav = cache
                    LAST_ANALYSIS.update(key=key, plan=plan)
                rows = scene_rows(plan, ratio)
                yield rows, None, f"Sẵn sàng: {len(rows)} cảnh và prompt."
                if not render:
                    return
                if provider == "chrome":
                    if chrome_server is None:
                        raise RuntimeError("Cổng kết nối Chrome 7877 đang bận. Đóng các bản Vẽ Tay khác rồi mở lại.")
                    yield rows, None, "Đang mở Google Flow và kết nối tự động với tiện ích Chrome…"
                    pictures = bridge.images([row[-1] for row in rows], folder, report)

                if not pictures:
                    raise ValueError("Chọn ảnh theo thứ tự cảnh hoặc chọn Google Flow.")
                dest = DATA / f"VeTay-{uuid.uuid4().hex[:8]}.mp4"
                path = render_folder(wav, pictures, plan, dest, report, ratio)
                yield rows, path, f"Đã xuất MP4 · {len(rows)} cảnh."
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or b"").decode("utf-8", errors="replace").strip()
            yield rows, None, "Lỗi xử lý âm thanh hoặc video: " + detail[-1500:]
        except Exception as error:
            yield rows, None, "Tác vụ dừng: " + str(error)
        finally:
            LOCK.release()

    def analyze(script, voice, ratio):
        yield from run(script, voice, None, None, ratio, False)

    def generate_video(script, voice, pictures, provider, ratio):
        yield from run(script, voice, pictures, provider, ratio, True)

    css = "body, .gradio-container, button, textarea, input { font-family: 'Segoe UI', Arial, sans-serif !important; }"
    with gr.Blocks(title="Vẽ Tay") as app:
        gr.HTML('<div lang="vi"><h1>Vẽ Tay</h1></div>')
        with gr.Row():
            with gr.Column():
                script = gr.Textbox(label="Kịch bản tiếng Việt", lines=5)
                voice = gr.Audio(label="Bản thu lời đọc (MP3 / WAV)", sources=["upload"], type="filepath")

                ratio = gr.Dropdown([(f"{key} · {w} × {h}", key) for key, (w,h) in VIDEO_SIZES.items()],
                                    value="9:16", label="Tỷ lệ video")
            with gr.Column():
                provider = gr.Radio([("Google Flow qua tiện ích Chrome", "chrome"), ("Ảnh có sẵn", "files")],
                                    value="chrome", label="Nguồn ảnh minh họa")

                pictures = gr.File(label="Ảnh có sẵn theo thứ tự cảnh", file_count="multiple",
                                   file_types=["image"], type="filepath")
        with gr.Row():
            analysis = gr.Button("Phân tích và chia cảnh")
            generate = gr.Button("Tự tạo video", variant="primary")
        table = gr.Dataframe(headers=["Cảnh", "Bắt đầu", "Kết thúc", "Thời lượng (giây)",
                                      "Lời đọc", "Prompt Google Flow"],
                             datatype=["number", "str", "str", "number", "str", "str"],
                             label="Bảng phân cảnh theo lời đọc", wrap=True, interactive=False)
        task = gr.Textbox(label="Tiến độ dựng", interactive=False)
        video = gr.Video(label="Video MP4")
        status = gr.Textbox(label="Trạng thái cập nhật", interactive=False)
        gr.Button("Chuẩn bị tiện ích Chrome").click(prepare_chrome_bundle, outputs=status)
        gr.Button("Cập nhật qua GitHub").click(update, outputs=status)
        analysis.click(analyze, [script, voice, ratio], [table, video, task])
        generate.click(generate_video, [script, voice, pictures, provider, ratio], [table, video, task])
    if check:
        engine()
        print(f"VeTay {VERSION}: {len(app.config['components'])} controls.")
        return
    import webview
    try:
        chrome_server = bridge.serve()
        threading.Thread(target=refresh_existing_extension, daemon=True).start()
    except OSError:
        pass
    _, local_url, _ = app.queue(default_concurrency_limit=1).launch(
        server_name="127.0.0.1", prevent_thread_lock=True, inbrowser=False,
        share=False, allowed_paths=[str(DATA)], quiet=True, css=css)
    try:
        webview.create_window("Vẽ Tay", local_url, width=1080, height=900, min_size=(740, 620))
        webview.start(gui="edgechromium")
    finally:
        if chrome_server is not None:
            chrome_server.shutdown()
            chrome_server.server_close()
        app.close()

if __name__ == "__main__":
    cli = argparse.ArgumentParser()
    cli.add_argument("--smoke", nargs=3, metavar=("VOICE", "IMAGE", "OUTPUT"))
    cli.add_argument("--check", action="store_true")
    cli.add_argument("--ratio", choices=VIDEO_SIZES, default="9:16")
    args = cli.parse_args()
    if args.smoke:
        voice, image, out = map(lambda item: Path(item).resolve(), args.smoke)
        render_folder(voice, [image], [(0, 48, "Smoke test")], out, print, args.ratio)
    else:
        ui(check=args.check)

