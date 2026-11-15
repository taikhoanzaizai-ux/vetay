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
VERSION_FILE = ROOT / "version.txt"
VERSION = VERSION_FILE.read_text().strip() if VERSION_FILE.is_file() else "0.2.0"
RELEASE_API = "https://api.github.com/repos/taikhoanzaizai-ux/vetay/releases/latest"
if (ROOT / "ffmpeg.exe").is_file():
    os.environ["FFMPEG_BINARY"] = str(ROOT / "ffmpeg.exe")
LOCK = threading.Lock()

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
    parts, _ = model.transcribe(str(voice), language="vi", vad_filter=True)
    return [(float(p.start), p.text.strip()) for p in parts if p.text.strip()]

def scenes(parts, seconds, maximum):
    if not parts or seconds <= 0:
        raise ValueError("Không tìm thấy lời đọc hoặc âm thanh trống.")
    target = max(4, seconds / maximum)
    result = [(0.0, parts[0][1])]
    for start, text in parts[1:]:
        previous, combined = result[-1]
        if len(result) < maximum and start - previous >= target and seconds - start >= 2:
            result.append((start, text))
        else:
            result[-1] = (previous, combined + " " + text)
    return [(round(start * 24), round((result[i+1][0] if i+1 < len(result) else seconds) * 24), text)
            for i, (start, text) in enumerate(result)]

def prompt_for(text):
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
    return ("Tạo một ảnh minh họa dọc 9:16 để chuyển thành video vẽ tay. "
            "Nền trắng trơn, đường viền đậm rõ, ít chi tiết, chủ thể tách nền, "
            "màu xanh ngọc và vàng nhạt. Không chữ, số, logo hoặc watermark. "
            + direction + "Dùng lời đọc sau làm ngữ cảnh, không chép thành chữ trên ảnh: " + text)

def timecode(frame):
    ms = round(frame * 1000 / 24)
    minutes, remainder = divmod(ms, 60000)
    seconds, fraction = divmod(remainder, 1000)
    return f"{minutes:02}:{seconds:02}.{fraction:03}"

def scene_rows(plan):
    return [[i, timecode(start), timecode(end), round((end-start)/24, 3), text, prompt_for(text)]
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

def flow_images(url, model, prompts, folder, report):
    from playwright.sync_api import sync_playwright
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"labs.google", "flow.google"}:
        raise ValueError("Dán URL dự án Flow chính thức của Google.")
    with sync_playwright() as play:
        browser = play.chromium.launch_persistent_context(str(DATA / "flow-profile"), channel="msedge",
                                                          headless=False, accept_downloads=True)
        try:
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            page.get_by_text(re.compile(r"^" + re.escape(model) + r"$", re.I)).first.wait_for(timeout=180000)
            for i, prompt in enumerate(prompts):
                report(f"Flow: đang tạo ảnh {i+1}/{len(prompts)}…")
                field = page.locator('textarea:visible, [contenteditable="true"]:visible').last
                field.fill(prompt, timeout=15000)
                generate = page.get_by_role("button", name=re.compile(r"^(Generate Image|Generate|Tạo hình ảnh|Tạo ảnh)$", re.I))
                download = page.get_by_role("button", name=re.compile(r"^(Download|Tải xuống)( image| ảnh)?$", re.I))
                existing = download.element_handles()
                generate.last.click(timeout=15000)
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

def render_folder(voice, pictures, plan, destination, report):
    from PIL import Image, ImageOps
    import numpy as np
    path, render_video, Settings = engine()
    if len(pictures) < len(plan):
        raise ValueError(f"Cần {len(plan)} ảnh, hiện có {len(pictures)}.")
    with tempfile.TemporaryDirectory(prefix="vetay-", dir=destination.parent) as work:
        work = Path(work)
        clips = []
        for i, ((start, end, _), picture) in enumerate(zip(plan, pictures)):
            report(f"Whiteboard Animation VI: cảnh {i+1}/{len(plan)}…")
            with Image.open(picture) as raw:
                art = ImageOps.contain(ImageOps.exif_transpose(raw).convert("RGB"), (432, 768))
            canvas = Image.new("RGB", (432, 768), "white")
            canvas.paste(art, ((432-art.width)//2, (768-art.height)//2))
            settings = Settings(device="cpu", encoder="cpu", width=432, height=768, fps=24,
                                duration=(end-start)/24, draw_percent=70, mode="e",
                                subject_first=(path / "assets/v181d/model.safetensors").is_file())
            clip, _ = render_video(np.array(canvas), settings, output_dir=work)
            clips.append(clip)
        listing = work / "clips.txt"
        listing.write_text("".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
        output = work / "complete.mp4"
        ffmpeg("-f", "concat", "-safe", "0", "-i", listing, "-i", voice, "-map", "0:v", "-map", "1:a",
               "-vf", "scale=1080:1920:flags=lanczos", "-c:v", "libx264", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-af", "apad", "-shortest", "-movflags", "+faststart", output)
        os.replace(output, destination)
    return str(destination)

def update():
    if not LOCK.acquire(blocking=False):
        return "Đợi video xuất xong trước khi cập nhật."
    try:
        request = urllib.request.Request(RELEASE_API, headers={"User-Agent": "VeTay-Windows"})
        with urllib.request.urlopen(request, timeout=20) as response:
            release = json.load(response)
        def triplet(value):
            if not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
                raise ValueError("Phiên bản GitHub không hợp lệ.")
            return tuple(map(int, value.lstrip("v").split(".")))
        tag = release["tag_name"]
        if triplet(tag) <= triplet(VERSION):
            return f"Đã ở bản mới nhất ({VERSION})."
        asset = next(item for item in release["assets"] if item["name"] == "VeTay-Windows-Setup.exe")
        checksum = asset.get("digest", "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", checksum):
            raise RuntimeError("GitHub chưa cung cấp mã kiểm tra của bộ cài.")
        url = asset["browser_download_url"]
        expected_url = f"https://github.com/taikhoanzaizai-ux/vetay/releases/download/{tag}/VeTay-Windows-Setup.exe"
        if url != expected_url:
            raise RuntimeError("Đường dẫn bộ cài không đúng repo.")
        stage = Path(tempfile.mkdtemp(prefix="update-", dir=DATA))
        exe = stage / asset["name"]
        try:
            digest = hashlib.sha256()
            with urllib.request.urlopen(url, timeout=35) as response, exe.open("wb") as handle:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > asset["size"]:
                        raise RuntimeError("Kích thước bộ cài không hợp lệ.")
                    digest.update(chunk)
                    handle.write(chunk)
            if total != asset["size"] or f"sha256:{digest.hexdigest()}" != checksum:
                raise RuntimeError("Bộ cài tải về không qua kiểm tra SHA-256.")
            subprocess.Popen([str(exe), "/CLOSEAPPLICATIONS", "/RESTARTVETAY", f"/DIR={ROOT}"], cwd=stage)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        threading.Timer(2, lambda: os._exit(0)).start()
        return f"Đã tải {tag}. Tiếp tục trong cửa sổ cài đặt."
    except Exception as error:
        return f"Cập nhật thất bại: {error}"
    finally:
        LOCK.release()

def ui(check=False):
    import gradio as gr
    DATA.mkdir(parents=True, exist_ok=True)
    def run(script, voice, pictures, provider, url, model, maximum, render, progress=gr.Progress()):
        def report(message):
            progress(None, desc=message)
        if not LOCK.acquire(blocking=False):
            raise gr.Error("Một tác vụ đang chạy.")
        rows = []
        try:
            with tempfile.TemporaryDirectory(prefix="input-", dir=DATA) as folder:
                folder = Path(folder)
                report("Đang chuẩn bị lời đọc…")
                wav = prepare_audio(script, voice, folder)
                report("Đang nhận dạng lời đọc và chia cảnh…")
                plan = scenes(transcribe(wav), duration(wav), int(maximum))
                rows = scene_rows(plan)
                yield rows, None, f"Đã phân tích {len(rows)} cảnh và viết prompt."
                if not render:
                    return
                if provider == "flow":
                    pictures = list(flow_images(url.strip(), model, [row[-1] for row in rows], folder, report))
                if not pictures:
                    raise ValueError("Chọn ảnh theo thứ tự cảnh hoặc chọn Google Flow.")
                dest = DATA / f"VeTay-{uuid.uuid4().hex[:8]}.mp4"
                path = render_folder(wav, pictures, plan, dest, report)
                yield rows, path, f"Đã xuất MP4 · {len(rows)} cảnh."
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or b"").decode("utf-8", errors="replace").strip()
            yield rows, None, "Lỗi xử lý âm thanh hoặc video: " + detail[-1500:]
        except Exception as error:
            yield rows, None, "Tác vụ dừng: " + str(error)
        finally:
            LOCK.release()

    def analyze(script, voice, maximum):
        yield from run(script, voice, None, None, None, None, maximum, False)

    def generate_video(script, voice, pictures, provider, url, model, maximum):
        yield from run(script, voice, pictures, provider, url, model, maximum, True)

    css = "body, .gradio-container, button, textarea, input { font-family: 'Segoe UI', Arial, sans-serif !important; }"
    with gr.Blocks(title="Vẽ Tay") as app:
        gr.HTML('<div lang="vi"><h1>Vẽ Tay</h1></div>')
        with gr.Row():
            with gr.Column():
                script = gr.Textbox(label="Kịch bản tiếng Việt", lines=5)
                voice = gr.Audio(label="Bản thu lời đọc (MP3 / WAV)", sources=["upload"], type="filepath")
                maximum = gr.Slider(1, 30, value=5, step=1, label="Số cảnh tối đa")
            with gr.Column():
                provider = gr.Radio([("Google Flow (thử nghiệm)", "flow"), ("Ảnh có sẵn", "files")],
                                    value="flow", label="Nguồn ảnh minh họa")
                url = gr.Textbox(label="Đường dẫn dự án Google Flow", placeholder="https://labs.google/…")
                model = gr.Radio(["Nano Banana 2.1", "Nano Banana 2 Lite"], value="Nano Banana 2.1",
                                 label="Model ảnh trong Google Flow")
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
        analysis.click(analyze, [script, voice, maximum], [table, video, task])
        generate.click(generate_video, [script, voice, pictures, provider, url, model, maximum], [table, video, task])
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
    args = cli.parse_args()
    if args.smoke:
        voice, image, out = map(lambda item: Path(item).resolve(), args.smoke)
        render_folder(voice, [image], [(0, 48, "Smoke test")], out, print)
    else:
        ui(check=args.check)

