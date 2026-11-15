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
        raise RuntimeError("B? render Whiteboard Animation VI b? thi?u trong b?n c�i.")
    sys.path.insert(0, str(path))
    from handdraw.render import render_video
    from handdraw.settings import Settings
    return path, render_video, Settings

def ffmpeg(*args):
    exe = os.environ.get("FFMPEG_BINARY") or shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("Kh�ng t�m th?y FFmpeg. C�i FFmpeg ho?c d?t FFMPEG_BINARY.")
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
        raise ValueError("Kh�ng t�m th?y l?i d?c ho?c �m thanh tr?ng.")
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
    return ("Create one vertical 9:16 whiteboard illustration for a Vietnamese explainer. "
            "Plain white background, bold clean ink outlines, a few clearly separated subjects, "
            "muted teal and amber accents, no letters, no captions, no numbers. Illustrate this narration: " + text)

def flow_images(url, model, prompts, folder, report):
    from playwright.sync_api import sync_playwright
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"labs.google", "flow.google"}:
        raise ValueError("D�n URL d? �n Flow ch�nh th?c c?a Google.")
    with sync_playwright() as play:
        browser = play.chromium.launch_persistent_context(str(DATA / "flow-profile"), channel="msedge",
                                                          headless=False, accept_downloads=True)
        try:
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            page.get_by_text(re.compile(r"^" + re.escape(model) + r"$", re.I)).first.wait_for(timeout=180000)
            for i, prompt in enumerate(prompts):
                report(f"Flow: dang t?o ?nh {i+1}/{len(prompts)}.")
                field = page.locator('textarea:visible, [contenteditable="true"]:visible').last
                field.fill(prompt, timeout=15000)
                generate = page.get_by_role("button", name=re.compile(r"^(Generate Image|Generate|T?o h�nh ?nh|T?o ?nh)$", re.I))
                download = page.get_by_role("button", name=re.compile(r"^(Download|T?i xu?ng)( image| ?nh)?$", re.I))
                existing = download.element_handles()
                generate.last.click(timeout=15000)
                previous = len(existing)
                page.wait_for_function(
                    """n => [...document.querySelectorAll('button')].filter(b =>
                        /^(Download|T?i xu?ng)( image| ?nh)?$/i.test((b.getAttribute('aria-label') || b.textContent || '').trim())
                        ).length > n""", arg=previous, timeout=180000)
                with page.expect_download(timeout=20000) as event:
                    candidates = download.element_handles()
                    new_items = [item for item in candidates if not item.evaluate("(node, old) => old.includes(node)", existing)]
                    if len(new_items) != 1:
                        raise RuntimeError("Kh�ng x�c d?nh du?c file ?nh m?i duy nh?t trong Flow.")
                    new_items[0].click()
                path = folder / f"flow-{i:02}.png"
                event.value.save_as(path)
                yield path
        except Exception as error:
            raise RuntimeError(f"Flow d?ng: {error}. Ki?m tra dang nh?p, h?n m?c v� giao di?n d? �n.") from error
        finally:
            browser.close()

def render_folder(voice, pictures, plan, destination, report):
    from PIL import Image, ImageOps
    import numpy as np
    path, render_video, Settings = engine()
    if len(pictures) < len(plan):
        raise ValueError(f"C?n {len(plan)} ?nh, hi?n c� {len(pictures)}.")
    with tempfile.TemporaryDirectory(prefix="vetay-", dir=destination.parent) as work:
        work = Path(work)
        clips = []
        for i, ((start, end, _), picture) in enumerate(zip(plan, pictures)):
            report(f"Whiteboard Animation VI: c?nh {i+1}/{len(plan)}.")
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
        return "D?i video xu?t xong tru?c khi c?p nh?t."
    try:
        request = urllib.request.Request(RELEASE_API, headers={"User-Agent": "VeTay-Windows"})
        with urllib.request.urlopen(request, timeout=20) as response:
            release = json.load(response)
        def triplet(value):
            if not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
                raise ValueError("Phi�n b?n GitHub kh�ng h?p l?.")
            return tuple(map(int, value.lstrip("v").split(".")))
        tag = release["tag_name"]
        if triplet(tag) <= triplet(VERSION):
            return f"Da ? b?n m?i nh?t ({VERSION})."
        asset = next(item for item in release["assets"] if item["name"] == "VeTay-Windows-Setup.exe")
        checksum = asset.get("digest", "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", checksum):
            raise RuntimeError("GitHub chua cung c?p ma ki?m tra c?a b? c�i.")
        url = asset["browser_download_url"]
        expected_url = f"https://github.com/taikhoanzaizai-ux/vetay/releases/download/{tag}/VeTay-Windows-Setup.exe"
        if url != expected_url:
            raise RuntimeError("Du?ng d?n b? c�i kh�ng d�ng repo.")
        stage = Path(tempfile.mkdtemp(prefix="update-", dir=DATA))
        exe = stage / asset["name"]
        try:
            digest = hashlib.sha256()
            with urllib.request.urlopen(url, timeout=35) as response, exe.open("wb") as handle:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > asset["size"]:
                        raise RuntimeError("K�ch thu?c b? c�i kh�ng h?p l?.")
                    digest.update(chunk)
                    handle.write(chunk)
            if total != asset["size"] or f"sha256:{digest.hexdigest()}" != checksum:
                raise RuntimeError("B? c�i t?i v? kh�ng qua ki?m tra SHA-256.")
            subprocess.Popen([str(exe), "/CLOSEAPPLICATIONS"], cwd=stage)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        threading.Timer(2, lambda: os._exit(0)).start()
        return f"Da t?i {tag}. Ti?p t?c trong c?a s? c�i d?t."
    except Exception as error:
        return f"C?p nh?t th?t b?i: {error}"
    finally:
        LOCK.release()

def ui(check=False):
    import gradio as gr
    DATA.mkdir(parents=True, exist_ok=True)
    def run(script, voice, pictures, provider, url, model, maximum, progress=gr.Progress()):
        def report(message):
            progress(None, desc=message)
        if not LOCK.acquire(blocking=False):
            raise gr.Error("M?t t�c v? dang ch?y.")
        try:
            with tempfile.TemporaryDirectory(prefix="input-", dir=DATA) as folder:
                folder = Path(folder)
                wav = folder / "voice.wav"
                if not voice:
                    if not script.strip():
                        raise ValueError("Nh?p k?ch b?n ho?c t?i voice.")
                    import edge_tts
                    speech = folder / "speech.mp3"
                    asyncio.run(edge_tts.Communicate(script.strip(), "vi-VN-NamMinhNeural").save(str(speech)))
                    voice = speech
                ffmpeg("-i", voice, "-ac", "1", "-ar", "24000", wav)
                report("Dang nh?n d?ng v� chia c?nh.")
                seconds = duration(wav)
                plan = scenes(transcribe(wav), seconds, int(maximum))
                if provider == "Google Flow (th? nghi?m)":
                    pictures = list(flow_images(url.strip(), model, [prompt_for(t) for _, _, t in plan], folder, report))
                if not pictures:
                    raise ValueError("Ch?n ?nh theo th? t? c?nh ho?c ch?n Google Flow.")
                dest = DATA / f"VeTay-{uuid.uuid4().hex[:8]}.mp4"
                return render_folder(wav, pictures, plan, dest, report)
        except subprocess.CalledProcessError as error:
            raise gr.Error(error.stderr.decode(errors="replace")[-2000:]) from error
        except Exception as error:
            raise gr.Error(str(error)) from error
        finally:
            LOCK.release()
    with gr.Blocks(title="V? Tay") as app:
        gr.Markdown("# V? Tay")
        script = gr.Textbox(label="K?ch b?n ti?ng Vi?t", lines=5)
        voice = gr.Audio(label="Voice g?c", type="filepath")
        provider = gr.Radio(["?nh c� s?n", "Google Flow (th? nghi?m)"], value="?nh c� s?n", label="Ngu?n ?nh")
        pictures = gr.File(label="?nh theo th? t? c?nh", file_count="multiple", file_types=["image"], type="filepath")
        url = gr.Textbox(label="Link d? �n Flow da ch?n ch? d? t?o ?nh")
        model = gr.Radio(["Nano Banana 2.1", "Nano Banana 2 Lite"], value="Nano Banana 2.1", label="Model da ch?n trong Flow")
        maximum = gr.Slider(1, 12, value=5, step=1, label="S? c?nh t?i da")
        generate = gr.Button("T?o video", variant="primary")
        video = gr.Video(label="Video MP4")
        status = gr.Textbox(label="C?p nh?t", interactive=False)
        gr.Button("C?p nh?t qua GitHub").click(update, outputs=status)
        generate.click(run, [script, voice, pictures, provider, url, model, maximum], video)
    if check:
        engine()
        print(f"VeTay {VERSION}: {len(app.config['components'])} controls.")
        return
    import webview
    _, local_url, _ = app.queue(default_concurrency_limit=1).launch(
        server_name="127.0.0.1", prevent_thread_lock=True, inbrowser=False,
        share=False, allowed_paths=[str(DATA)], quiet=True)
    try:
        webview.create_window("V? Tay", local_url, width=1080, height=900, min_size=(740, 620))
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

