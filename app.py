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
VERSION = "0.2.10"
PATCH_ABI = "vetay-windows-026"
PATCH_ROOT = "https://raw.githubusercontent.com/taikhoanzaizai-ux/vetay/"

if (ROOT / "ffmpeg.exe").is_file():
    os.environ["FFMPEG_BINARY"] = str(ROOT / "ffmpeg.exe")
LOCK = threading.Lock()
LAST_ANALYSIS = {}
FLOW_LOGIN_PROCESS = None
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

def normalize_flow_url(value):
    from urllib.parse import urlsplit, urlunsplit
    value = (value or "").strip().strip('<>"“”').strip()
    if not value:
        raise ValueError("Ô Đường dẫn dự án Google Flow đang trống. Hãy dán link từ thanh địa chỉ trong dự án.")
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    allowed = {"labs.google", "flow.google", "flow.google.com"}
    if (parsed.scheme != "https" or parsed.hostname not in allowed
        or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("Link Flow phải dùng HTTPS và có tên miền flow.google.com, flow.google hoặc labs.google.")
    if any(character.isspace() for character in value):
        raise ValueError("Hãy dán riêng đường dẫn Flow, không kèm tiêu đề hoặc văn bản khác.")
    return urlunsplit(("https", parsed.hostname, parsed.path, parsed.query, parsed.fragment))

def open_flow_sign_in(url):
    global FLOW_LOGIN_PROCESS
    if LOCK.locked():
        return "Đợi tác vụ đang chạy kết thúc trước khi mở cửa sổ đăng nhập."
    try:
        url = normalize_flow_url(url or "https://flow.google.com/")
        candidates = [Path(os.environ[key]) / "Microsoft/Edge/Application/msedge.exe"
                      for key in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA") if os.environ.get(key)]
        edge = next((path for path in candidates if path.is_file()), None)
        if edge is None:
            raise RuntimeError("Không tìm thấy Microsoft Edge.")
        FLOW_LOGIN_PROCESS = subprocess.Popen([
            str(edge), f"--user-data-dir={DATA / 'flow-profile'}", "--disable-background-mode", url])
        return ("Đăng nhập Google trong Edge vừa mở. Sau khi vào được Flow, "
                "đóng toàn bộ cửa sổ Edge của hồ sơ này rồi bấm Tự tạo video.")
    except Exception as error:
        return f"Không mở được Edge: {error}"

def flow_images(url, model, prompts, folder, report):
    from playwright.sync_api import sync_playwright
    url = normalize_flow_url(url)
    report("Đang mở Edge để kết nối Google Flow…")
    with sync_playwright() as play:
        busy = ("Cửa sổ Edge đăng nhập Flow của Vẽ Tay vẫn đang mở. "
                "Trong chính cửa sổ Edge đó, nhấn Alt+F4 rồi bấm lại Tự tạo video. "
                "Nếu vẫn báo bận, thoát hẳn Edge qua menu ⋯ → Đóng Microsoft Edge.")
        if FLOW_LOGIN_PROCESS is not None:
            try:
                FLOW_LOGIN_PROCESS.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(busy) from error
        try:
            browser = play.chromium.launch_persistent_context(
                str(DATA / "flow-profile"), channel="msedge", headless=False,
                accept_downloads=True, chromium_sandbox=True, args=["--disable-background-mode"])
        except Exception as error:
            detail = str(error)
            if any(marker in detail.lower() for marker in ("opening in existing browser session",
                       "profile is already in use", "processsingleton", "user data directory is already in use")):
                raise RuntimeError(busy) from error
            raise RuntimeError("Không mở được cửa sổ Flow: " + detail.splitlines()[0]) from error
        try:
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            from urllib.parse import urlsplit
            if urlsplit(page.url).hostname == "accounts.google.com":
                raise RuntimeError("Cần đăng nhập trước. Bấm Mở Edge để đăng nhập Flow trong Vẽ Tay.")
            field = page.locator('textarea:visible, [contenteditable="true"]:visible')
            report("Đang đợi mở dự án Flow. Nếu được hỏi, hãy đăng nhập Google trong Edge.")
            field.last.wait_for(timeout=180000)
            exact_model = re.compile(r"^" + re.escape(model) + r"$", re.I)
            report(f"Đang kiểm tra model {model} trong Google Flow…")
            if not page.get_by_text(exact_model).first.is_visible():
                picker = page.get_by_role("button", name=re.compile(r"Nano Banana|Imagen|Veo|Model|Mô hình", re.I)).filter(visible=True)
                if picker.count() == 1:
                    picker.click(timeout=5000)
                    image_tab = page.get_by_text(re.compile(r"^(Image|Hình ảnh|Ảnh)$", re.I), exact=True).filter(visible=True)
                    if image_tab.count() == 1:
                        image_tab.click(timeout=5000)
                    choice = page.get_by_text(exact_model).filter(visible=True)
                    if choice.count() == 1:
                        choice.click(timeout=5000)
            try:
                page.get_by_text(exact_model).first.wait_for(timeout=25000)
            except Exception as error:
                raise RuntimeError(f"Không thấy {model} trong dự án Flow. Chọn chế độ Ảnh và kiểm tra model có trên tài khoản.") from error
            for i, prompt in enumerate(prompts):
                report(f"Flow: đang tạo ảnh {i+1}/{len(prompts)}…")
                field.last.fill(prompt, timeout=15000)
                generate = page.get_by_role("button", name=re.compile(r"^(Generate Image|Generate|Tạo hình ảnh|Tạo ảnh)$", re.I))
                download = page.get_by_role("button", name=re.compile(r"^(Download|Tải xuống)( image| ảnh)?$", re.I))
                existing = download.element_handles()
                if generate.count() != 1:
                    raise RuntimeError("Không nhận ra nút tạo ảnh duy nhất trong Flow. Hãy mở dự án ở chế độ Ảnh.")
                generate.click(timeout=15000)
                previous = len(existing)
                page.wait_for_function(
                    """n => [...document.querySelectorAll('button')].filter(b =>
                        /^(Download|Tải xuống)( image| ảnh)?$/i.test((b.getAttribute('aria-label') || b.textContent || '').trim())
                        ).length > n""", arg=previous, timeout=180000)
                with page.expect_download(timeout=20000) as event:
                    candidates = download.element_handles()
                    new_items = [item for item in candidates if not item.evaluate("(node, old) => old.includes(node)", existing)]
                    if len(new_items) != 1:
                        raise RuntimeError("Không xác định được file ảnh mới duy nhất trong Flow.")
                    new_items[0].click()
                path = folder / f"flow-{i:02}.png"
                event.value.save_as(path)
                yield path
        except Exception as error:
            raise RuntimeError(f"Flow dừng: {error}. Kiểm tra đăng nhập, hạn mức và giao diện dự án.") from error
        finally:
            browser.close()

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

def update():
    if not LOCK.acquire(blocking=False):
        return "Đợi video xuất xong trước khi cập nhật."
    restart = False
    try:
        manifest = json.loads(patch_read(PATCH_ROOT + "main/update.json", 16384))
        version = manifest["version"]
        if version_tuple(version) <= version_tuple(VERSION):
            return f"Đã ở bản mới nhất ({VERSION})."
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


def ui(check=False):
    import gradio as gr
    DATA.mkdir(parents=True, exist_ok=True)
    def run(script, voice, pictures, provider, url, model, ratio, render, progress=gr.Progress()):
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
                if provider == "flow":
                    pictures = list(flow_images((url or "").strip(), model, [row[-1] for row in rows], folder, report))
                    yield rows, None, f"Google Flow đã tạo ảnh {len(pictures)}/{len(rows)}."
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
        yield from run(script, voice, None, None, None, None, ratio, False)

    def generate_video(script, voice, pictures, provider, url, model, ratio):
        yield from run(script, voice, pictures, provider, url, model, ratio, True)

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
                provider = gr.Radio([("Google Flow (thử nghiệm)", "flow"), ("Ảnh có sẵn", "files")],
                                    value="flow", label="Nguồn ảnh minh họa")
                url = gr.Textbox(label="Đường dẫn dự án Google Flow", placeholder="https://flow.google.com/… hoặc link dự án Labs Flow", info="Vẽ Tay tự gửi prompt theo từng cảnh. Lần đầu, đăng nhập trong cửa sổ Edge do ứng dụng mở.")
                model = gr.Radio(["Nano Banana 2.1", "Nano Banana 2 Lite"], value="Nano Banana 2.1",
                                 label="Model ảnh trong Google Flow")
                flow_login = gr.Button("Mở Edge để đăng nhập Flow")
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
        gr.Button("Cập nhật qua GitHub").click(update, outputs=status)
        flow_login.click(open_flow_sign_in, inputs=url, outputs=task)
        analysis.click(analyze, [script, voice, ratio], [table, video, task])
        generate.click(generate_video, [script, voice, pictures, provider, url, model, ratio], [table, video, task])
    if check:
        engine()
        print(f"VeTay {VERSION}: {len(app.config['components'])} controls.")
        return
    import webview
    _, local_url, _ = app.queue(default_concurrency_limit=1).launch(
        server_name="127.0.0.1", prevent_thread_lock=True, inbrowser=False,
        share=False, allowed_paths=[str(DATA)], quiet=True, css=css)
    try:
        webview.create_window("Vẽ Tay", local_url, width=1080, height=900, min_size=(740, 620))
        webview.start(gui="edgechromium")
    finally:
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

