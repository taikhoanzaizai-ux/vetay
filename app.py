from pathlib import Path
import argparse
import base64
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
import time
import uuid

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "Data"
VERSION = "0.2.21"
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

def prepare_data():
    DATA.mkdir(parents=True, exist_ok=True)
    temp = DATA / "Temp"
    temp.mkdir(exist_ok=True)
    tempfile.tempdir = str(temp)
    os.environ["GRADIO_TEMP_DIR"] = str(temp / "gradio")
    os.environ["HF_HOME"] = str(DATA / "huggingface")

    local = os.environ.get("LOCALAPPDATA")
    marker = DATA / ".migration-complete"
    if not local or marker.exists():
        return
    legacy = Path(local) / "VeTay"
    if legacy.resolve() == DATA.resolve() or not legacy.is_dir():
        return
    files = list(legacy.glob("VeTay-*.mp4"))
    for name in ("models", "chrome-extension", "huggingface"):
        folder = legacy / name
        if folder.is_dir():
            files.extend(file for file in folder.rglob("*") if file.is_file())
    for source in files:
        target = DATA / source.relative_to(legacy)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent) as staging:
            copy = Path(staging) / source.name
            shutil.copy2(source, copy)
            if not target.exists():
                copy.replace(target)
    marker.touch()


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

BUNDLED_EXTENSION = {
    "version": "1.3.7",
    "commit": "f0688c581911f032db6600022b00e58c16eb8483",
    "files": {
        "worker.js": "641e9def4e0f5d25f9281eb901f3aaa219fe82f9",
        "manifest.json": "1c0df4ced4b753c25c9614d4c8cdb6c5bc4beeb6",
        "flow.js": "50306972f3454be46c821714b36b98e50465c417",
        "popup.html": "d1f3fe3d89500eb7788fc312842525a9c22193c4"
    }
}
BUNDLED_EXTENSION_FILES = {
    "worker.js": "Y29uc3QgYmFzZSA9ICJodHRwOi8vMTI3LjAuMC4xOjc4NzciOwpmdW5jdGlvbiBmbG93VGFiKHVybCkgeyByZXR1cm4gL15odHRwczpcL1wvKGZsb3dcLmdvb2dsZVwuY29tfGxhYnNcLmdvb2dsZSlcLy8udGVzdCh1cmwgfHwgIiIpOyB9CmFzeW5jIGZ1bmN0aW9uIGFwaSh0b2tlbiwgcGF0aCwgYm9keSkgewogIGNvbnN0IHIgPSBhd2FpdCBmZXRjaChiYXNlICsgcGF0aCwgewogICAgaGVhZGVyczogeyJYLVZlVGF5LVRva2VuIjp0b2tlbiwgLi4uKGJvZHkgPyB7IkNvbnRlbnQtVHlwZSI6Ym9keS50eXBlIHx8ICJhcHBsaWNhdGlvbi9qc29uIn0gOiB7fSl9LAogICAgbWV0aG9kOiBib2R5ID8gIlBPU1QiIDogIkdFVCIsIGJvZHkKICB9KTsKICBpZiAoIXIub2spIHRocm93IEVycm9yKChhd2FpdCByLnRleHQoKSkuc2xpY2UoMCwyNTApKTsKICByZXR1cm4gci5qc29uKCk7Cn0KY2hyb21lLnJ1bnRpbWUub25NZXNzYWdlLmFkZExpc3RlbmVyKChtLHNlbmRlcixyZXBseSkgPT4gewogIChhc3luYygpPT57CiAgICBpZiAoIWZsb3dUYWIoc2VuZGVyLnRhYj8udXJsKSkgdGhyb3cgRXJyb3IoIkNo4buJIG5o4bqtbiB5w6p1IGPhuqd1IHThu6sgdGFiIEZsb3cuIik7CiAgICBjb25zdCBrZXkgPSAicGFpcmluZzoiICsgc2VuZGVyLnRhYi5pZDsKICAgIGlmIChtLm9wID09PSAicGFpciIpIHsKICAgICAgaWYgKCEvXlthLWYwLTldezMyfSQvLnRlc3QobS50b2tlbiB8fCAiIikpIHRocm93IEVycm9yKCJNw6Mga+G6v3QgbuG7kWkga2jDtG5nIGjhu6NwIGzhu4cuIik7CiAgICAgIGNvbnN0IGpvYiA9IGF3YWl0IGFwaShtLnRva2VuLCAiL25leHQiKTsKICAgICAgaWYgKGpvYi5kb25lKSB0aHJvdyBFcnJvcigiVuG6vSBUYXkgY2jGsGEgY8OzIGPhuqNuaCBjaOG7nS4iKTsKICAgICAgYXdhaXQgY2hyb21lLnN0b3JhZ2Uuc2Vzc2lvbi5zZXQoe1trZXldOm0udG9rZW59KTsKICAgICAgcmV0dXJuIGpvYjsKICAgIH0KICAgIGNvbnN0IHNhdmVkID0gYXdhaXQgY2hyb21lLnN0b3JhZ2Uuc2Vzc2lvbi5nZXQoa2V5KTsKICAgIGNvbnN0IHBhaXJpbmcgPSBzYXZlZFtrZXldOwogICAgaWYgKG0ub3AgPT09ICJyZXN1bWUiKSByZXR1cm4gcGFpcmluZyA/IGFwaShwYWlyaW5nLCIvbmV4dCIpIDoge2RvbmU6dHJ1ZX07CiAgICBpZiAoIXBhaXJpbmcpIHRocm93IEVycm9yKCJUYWIgY2jGsGEgxJHGsOG7o2MgVuG6vSBUYXkga+G6v3QgbuG7kWkuIik7CiAgICBpZiAobS5vcCA9PT0gIm5leHQiKSByZXR1cm4gYXBpKHBhaXJpbmcsIi9uZXh0Iik7CiAgICBpZiAobS5vcCA9PT0gImVycm9yIikgcmV0dXJuIGFwaShwYWlyaW5nLCIvZXJyb3IiLEpTT04uc3RyaW5naWZ5KHttZXNzYWdlOm0ubWVzc2FnZX0pKTsKICAgIGlmIChtLm9wID09PSAiaW1hZ2UiICYmIC9eW2EtZjAtOV17MzJ9JC8udGVzdChtLmpvYikgJiYgTnVtYmVyLmlzSW50ZWdlcihtLmluZGV4KSkgewogICAgICBsZXQgaW1hZ2U7CiAgICAgIGlmIChtLmRhdGEgJiYgL15kYXRhOmltYWdlXC8ocG5nfHdlYnB8anBlZyk7YmFzZTY0LC8udGVzdChtLmRhdGEpKSB7CiAgICAgICAgaW1hZ2UgPSBhd2FpdCAoYXdhaXQgZmV0Y2gobS5kYXRhKSkuYmxvYigpOwogICAgICB9IGVsc2UgewogICAgICAgIGNvbnN0IHUgPSBuZXcgVVJMKG0udXJsKTsKICAgICAgICBpZiAodS5wcm90b2NvbCAhPT0gImh0dHBzOiIgfHwgIS8oXnxcLilnb29nbGV1c2VyY29udGVudFwuY29tJC8udGVzdCh1Lmhvc3RuYW1lKSkgdGhyb3cgRXJyb3IoIktow7RuZyB4w6FjIMSR4buLbmggxJHGsOG7o2MgxJHhu4thIGNo4buJIOG6o25oIGvhur90IHF14bqjLiIpOwogICAgICAgIGNvbnN0IHIgPSBhd2FpdCBmZXRjaCh1LmhyZWYse2NyZWRlbnRpYWxzOiJvbWl0IixyZWRpcmVjdDoiZXJyb3IifSk7CiAgICAgICAgaWYgKCFyLm9rKSB0aHJvdyBFcnJvcigiS2jDtG5nIHThuqNpIMSRxrDhu6NjIOG6o25oIGvhur90IHF14bqjLiIpOwogICAgICAgIGltYWdlID0gYXdhaXQgci5ibG9iKCk7CiAgICAgIH0KICAgICAgaWYgKGltYWdlLnNpemUgPiAyNCoxMDI0KjEwMjQgfHwgIWltYWdlLnR5cGUuc3RhcnRzV2l0aCgiaW1hZ2UvIikpIHRocm93IEVycm9yKCLhuqJuaCBr4bq/dCBxdeG6oyBraMO0bmcgaOG7o3AgbOG7hy4iKTsKICAgICAgcmV0dXJuIGFwaShwYWlyaW5nLCIvaW1hZ2UvIittLmpvYisiLyIrbS5pbmRleCxpbWFnZSk7CiAgICB9CiAgICB0aHJvdyBFcnJvcigiWcOqdSBj4bqndSBraMO0bmcgaOG7o3AgbOG7hy4iKTsKICB9KSgpLnRoZW4ocmVzdWx0PT5yZXBseSh7b2s6dHJ1ZSxyZXN1bHR9KSxlcnJvcj0+cmVwbHkoe29rOmZhbHNlLGVycm9yOmVycm9yLm1lc3NhZ2V9KSk7CiAgcmV0dXJuIHRydWU7Cn0pOwpjaHJvbWUudGFicy5vblJlbW92ZWQuYWRkTGlzdGVuZXIodGFiSWQgPT4gY2hyb21lLnN0b3JhZ2Uuc2Vzc2lvbi5yZW1vdmUoInBhaXJpbmc6IiArIHRhYklkKSk7Cgpjb25zdCBleHRlbnNpb25DaGVjayA9ICJ2ZXRheS1jaGVjay1sb2NhbC11cGRhdGUiOwpmdW5jdGlvbiByZWdpc3RlckNoZWNrKCkgewogIGNocm9tZS5hbGFybXMuY3JlYXRlKGV4dGVuc2lvbkNoZWNrLCB7cGVyaW9kSW5NaW51dGVzOjF9KTsKfQpjaHJvbWUucnVudGltZS5vbkluc3RhbGxlZC5hZGRMaXN0ZW5lcihyZWdpc3RlckNoZWNrKTsKY2hyb21lLnJ1bnRpbWUub25TdGFydHVwLmFkZExpc3RlbmVyKHJlZ2lzdGVyQ2hlY2spOwpjaHJvbWUuYWxhcm1zLm9uQWxhcm0uYWRkTGlzdGVuZXIoYXN5bmMgYWxhcm0gPT4gewogIGlmIChhbGFybS5uYW1lICE9PSBleHRlbnNpb25DaGVjaykgcmV0dXJuOwogIHRyeSB7CiAgICBjb25zdCBjdXJyZW50ID0gY2hyb21lLnJ1bnRpbWUuZ2V0TWFuaWZlc3QoKTsKICAgIGNvbnN0IHJlc3BvbnNlID0gYXdhaXQgZmV0Y2goY2hyb21lLnJ1bnRpbWUuZ2V0VVJMKCJtYW5pZmVzdC5qc29uIikgKyAiP3Q9IiArIERhdGUubm93KCksIHtjYWNoZToibm8tc3RvcmUifSk7CiAgICBpZiAoIXJlc3BvbnNlLm9rKSByZXR1cm47CiAgICBjb25zdCBkaXNrID0gYXdhaXQgcmVzcG9uc2UuanNvbigpOwogICAgaWYgKCEvXlxkK1wuXGQrXC5cZCskLy50ZXN0KGRpc2sudmVyc2lvbikgfHwgZGlzay5uYW1lICE9PSBjdXJyZW50Lm5hbWUgfHwKICAgICAgICBkaXNrLm1hbmlmZXN0X3ZlcnNpb24gIT09IGN1cnJlbnQubWFuaWZlc3RfdmVyc2lvbikgcmV0dXJuOwogICAgY29uc3Qgb2xkID0gY3VycmVudC52ZXJzaW9uLnNwbGl0KCIuIikubWFwKE51bWJlcik7CiAgICBjb25zdCBuZXh0ID0gZGlzay52ZXJzaW9uLnNwbGl0KCIuIikubWFwKE51bWJlcik7CiAgICBjb25zdCBjaGFuZ2VkID0gbmV4dC5maW5kSW5kZXgoKHZhbHVlLGkpPT52YWx1ZSE9PW9sZFtpXSk7CiAgICBpZiAoY2hhbmdlZCA8IDAgfHwgbmV4dFtjaGFuZ2VkXSA8IG9sZFtjaGFuZ2VkXSkgcmV0dXJuOwogICAgZm9yKGNvbnN0IGtleSBvZiBbInBlcm1pc3Npb25zIiwiaG9zdF9wZXJtaXNzaW9ucyIsIm9wdGlvbmFsX3Blcm1pc3Npb25zIiwib3B0aW9uYWxfaG9zdF9wZXJtaXNzaW9ucyJdKSB7CiAgICAgIGlmKEpTT04uc3RyaW5naWZ5KFsuLi4oY3VycmVudFtrZXldfHxbXSldLnNvcnQoKSkhPT1KU09OLnN0cmluZ2lmeShbLi4uKGRpc2tba2V5XXx8W10pXS5zb3J0KCkpKSByZXR1cm47CiAgICB9CiAgICBjb25zdCBtYXRjaGVzID0gdmFsdWUgPT4gKHZhbHVlLmNvbnRlbnRfc2NyaXB0cyB8fCBbXSkuZmxhdE1hcChzY3JpcHQ9PnNjcmlwdC5tYXRjaGVzIHx8IFtdKS5zb3J0KCk7CiAgICBpZihKU09OLnN0cmluZ2lmeShtYXRjaGVzKGN1cnJlbnQpKSE9PUpTT04uc3RyaW5naWZ5KG1hdGNoZXMoZGlzaykpKSByZXR1cm47CiAgICBjb25zdCBpZGxlID0gYXdhaXQgZmV0Y2goYmFzZSsiL2V4dGVuc2lvbi1pZGxlIix7Y2FjaGU6Im5vLXN0b3JlIn0pOwogICAgaWYoaWRsZS5vayAmJiAoYXdhaXQgaWRsZS5qc29uKCkpLmlkbGUgPT09IHRydWUpIGNocm9tZS5ydW50aW1lLnJlbG9hZCgpOwogIH0gY2F0Y2gge30KfSk7Cg==",
    "manifest.json": "ewogICJtYW5pZmVzdF92ZXJzaW9uIjogMywKICAibmFtZSI6ICJW4bq9IFRheSDCtyBHb29nbGUgRmxvdyIsCiAgInZlcnNpb24iOiAiMS4zLjciLAogICJhY3Rpb24iOiB7CiAgICAiZGVmYXVsdF9wb3B1cCI6ICJwb3B1cC5odG1sIgogIH0sCiAgImJhY2tncm91bmQiOiB7CiAgICAic2VydmljZV93b3JrZXIiOiAid29ya2VyLmpzIgogIH0sCiAgInBlcm1pc3Npb25zIjogWwogICAgInN0b3JhZ2UiLAogICAgImFsYXJtcyIKICBdLAogICJob3N0X3Blcm1pc3Npb25zIjogWwogICAgImh0dHA6Ly8xMjcuMC4wLjE6Nzg3Ny8qIiwKICAgICJodHRwczovLyouZ29vZ2xldXNlcmNvbnRlbnQuY29tLyoiLAogICAgImh0dHBzOi8vZmxvdy5nb29nbGUuY29tLyoiLAogICAgImh0dHBzOi8vbGFicy5nb29nbGUvKiIKICBdLAogICJjb250ZW50X3NjcmlwdHMiOiBbCiAgICB7CiAgICAgICJtYXRjaGVzIjogWwogICAgICAgICJodHRwczovL2Zsb3cuZ29vZ2xlLmNvbS8qIiwKICAgICAgICAiaHR0cHM6Ly9sYWJzLmdvb2dsZS8qIgogICAgICBdLAogICAgICAianMiOiBbCiAgICAgICAgImZsb3cuanMiCiAgICAgIF0sCiAgICAgICJydW5fYXQiOiAiZG9jdW1lbnRfc3RhcnQiCiAgICB9CiAgXQp9Cg==",
    "flow.js": "KCgpID0+IHsKICBjb25zdCBwYWlyaW5nID0gL14jdmV0YXk9KFthLWYwLTldezMyfSkkLy5leGVjKGxvY2F0aW9uLmhhc2gpPy5bMV07CiAgaWYgKHdpbmRvdy5fX3ZlVGF5UnVubmluZykgcmV0dXJuOwogIHdpbmRvdy5fX3ZlVGF5UnVubmluZyA9IHRydWU7CiAgY29uc3Qgc2xlZXAgPSAobXMpID0+IG5ldyBQcm9taXNlKHJlc29sdmUgPT4gc2V0VGltZW91dChyZXNvbHZlLG1zKSk7CiAgY29uc3QgdmlzaWJsZSA9IG5vZGUgPT4gbm9kZS5nZXRDbGllbnRSZWN0cygpLmxlbmd0aCAmJiBnZXRDb21wdXRlZFN0eWxlKG5vZGUpLnZpc2liaWxpdHkgIT09ICJoaWRkZW4iOwogIGNvbnN0IHNlbmQgPSBhc3luYyAobWVzc2FnZSkgPT4gewogICAgY29uc3QgcmVzcG9uc2UgPSBhd2FpdCBjaHJvbWUucnVudGltZS5zZW5kTWVzc2FnZShtZXNzYWdlKTsKICAgIGlmICghcmVzcG9uc2U/Lm9rKSB0aHJvdyBFcnJvcihyZXNwb25zZT8uZXJyb3IgfHwgIk3huqV0IGvhur90IG7hu5FpIHbhu5tpIFbhur0gVGF5LiIpOwogICAgcmV0dXJuIHJlc3BvbnNlLnJlc3VsdDsKICB9OwogIGNvbnN0IGNhbmRpZGF0ZXMgPSAoKSA9PiBbLi4uZG9jdW1lbnQuaW1hZ2VzXS5maWx0ZXIoaW1nID0+IGltZy5jb21wbGV0ZSAmJiBpbWcubmF0dXJhbFdpZHRoID49IDM4NCAmJgogICAgaW1nLm5hdHVyYWxIZWlnaHQgPj0gMzg0ICYmIHZpc2libGUoaW1nKSAmJiAvXihibG9iOnxodHRwczpcL1wvW14vXSpcLmdvb2dsZXVzZXJjb250ZW50XC5jb21cLykvaS50ZXN0KGltZy5jdXJyZW50U3JjKSk7CiAgY29uc3QgY2FwdGlvbiA9IGJ1dHRvbiA9PiAoYnV0dG9uLmdldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIpIHx8IGJ1dHRvbi5nZXRBdHRyaWJ1dGUoInRpdGxlIikgfHwgYnV0dG9uLnRleHRDb250ZW50IHx8ICIiKS50cmltKCk7CiAgZnVuY3Rpb24gcHJvbXB0RmllbGQoKSB7CiAgICBjb25zdCBmaWVsZHMgPSBbLi4uZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgndGV4dGFyZWEsW2NvbnRlbnRlZGl0YWJsZT0idHJ1ZSJdLFtjb250ZW50ZWRpdGFibGU9InBsYWludGV4dC1vbmx5Il0nKV0KICAgICAgLmZpbHRlcihmaWVsZCA9PiB2aXNpYmxlKGZpZWxkKSAmJiAhZmllbGQuY2xvc2VzdCgnW2luZXJ0XSxbYXJpYS1oaWRkZW49InRydWUiXSxbcm9sZT0ic2VhcmNoIl0nKQogICAgICAgICYmICFmaWVsZC5kaXNhYmxlZCAmJiAhZmllbGQucmVhZE9ubHkgJiYgZmllbGQuZ2V0QXR0cmlidXRlKCJhcmlhLWRpc2FibGVkIikgIT09ICJ0cnVlIik7CiAgICBjb25zdCBsYWJlbHMgPSBmaWVsZCA9PiBbImFyaWEtbGFiZWwiLCAicGxhY2Vob2xkZXIiLCAiZGF0YS1wbGFjZWhvbGRlciIsICJ0aXRsZSJdCiAgICAgIC5tYXAobmFtZSA9PiBmaWVsZC5nZXRBdHRyaWJ1dGUobmFtZSkgfHwgIiIpLmpvaW4oIiAiKTsKICAgIGNvbnN0IGVsaWdpYmxlID0gZmllbGRzLmZpbHRlcihmaWVsZCA9PiAhL3NlYXJjaHx0w6xtIGtp4bq/bXxyZW5hbWV8xJHhu5VpIHTDqm58cHJvamVjdCBuYW1lfHTDqm4gZOG7sSDDoW4vaS50ZXN0KGxhYmVscyhmaWVsZCkpCiAgICAgICYmICFmaWVsZHMuc29tZShwYXJlbnQgPT4gcGFyZW50ICE9PSBmaWVsZCAmJiBwYXJlbnQuY29udGFpbnMoZmllbGQpKSk7CiAgICBjb25zdCBtYXRjaGVzID0gZWxpZ2libGUuZmlsdGVyKGZpZWxkID0+IC9wcm9tcHR8d2hhdCAoPzpkb3x3b3VsZCkgeW91fGRlc2NyaWJlfG3DtCB04bqjfGLhuqFuIG114buRbnx04bqhbyBu4buZaSBkdW5nL2kudGVzdChsYWJlbHMoZmllbGQpKSk7CiAgICBpZiAobWF0Y2hlcy5sZW5ndGggPT09IDEpIHJldHVybiBtYXRjaGVzWzBdOwogICAgcmV0dXJuIGVsaWdpYmxlLmxlbmd0aCA9PT0gMSA/IGVsaWdpYmxlWzBdIDogbnVsbDsKICB9CiAgY29uc3QgcmF0aW9MYWJlbCA9IHJhdGlvID0+IG5ldyBSZWdFeHAoIig/Ol58W14wLTldKSIgKyByYXRpby5yZXBsYWNlKCI6IiwgIlxccypbOuKItl1cXHMqIikgKyAiKD8hWzAtOV0pIiwgInUiKTsKICBhc3luYyBmdW5jdGlvbiBzZWxlY3RSYXRpbyhyYXRpbykgewogICAgY29uc3Qgc2VsZWN0ZWQgPSBub2RlID0+IFsiYXJpYS1jaGVja2VkIiwgImFyaWEtc2VsZWN0ZWQiLCAiYXJpYS1wcmVzc2VkIl0uc29tZShrZXkgPT4gbm9kZS5nZXRBdHRyaWJ1dGUoa2V5KSA9PT0gInRydWUiKQogICAgICB8fCBub2RlLmdldEF0dHJpYnV0ZSgiZGF0YS1zdGF0ZSIpID09PSAiY2hlY2tlZCIgfHwgbm9kZS5nZXRBdHRyaWJ1dGUoImRhdGEtc3RhdGUiKSA9PT0gImFjdGl2ZSI7CiAgICBjb25zdCBjb250cm9scyA9ICgpID0+IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCJidXR0b24sW3JvbGU9YnV0dG9uXSxbcm9sZT1yYWRpb10sW3JvbGU9b3B0aW9uXSxbcm9sZT1tZW51aXRlbXJhZGlvXSxbcm9sZT10YWJdIildCiAgICAgIC5maWx0ZXIobm9kZSA9PiB2aXNpYmxlKG5vZGUpICYmICFub2RlLmRpc2FibGVkICYmIG5vZGUuZ2V0QXR0cmlidXRlKCJhcmlhLWRpc2FibGVkIikgIT09ICJ0cnVlIik7CiAgICBjb25zdCBsYWJlbHMgPSBub2RlID0+IFtjYXB0aW9uKG5vZGUpLCBub2RlLmdldEF0dHJpYnV0ZSgiZGF0YS12YWx1ZSIpIHx8ICIiLCBub2RlLmlubmVyVGV4dCB8fCAiIl0uam9pbigiICIpOwogICAgY29uc3QgZXhhY3QgPSByYXRpb0xhYmVsKHJhdGlvKTsKICAgIGNvbnN0IGhhc1JhdGlvID0gcmF0aW9MYWJlbCgiWzAtOV0rOlswLTldKyIpOwogICAgY29uc3QgdHJpZ2dlciA9IGNvbnRyb2xzKCkuZmlsdGVyKG5vZGUgPT4gL2FzcGVjdCByYXRpb3x04bu3IGzhu4d8dOG7iSBs4buHfGNyb3BfKD86bGFuZHNjYXBlfHBvcnRyYWl0KS9pdS50ZXN0KGxhYmVscyhub2RlKSkKICAgICAgfHwgKG5vZGUuZ2V0QXR0cmlidXRlKCJhcmlhLWhhc3BvcHVwIikgJiYgaGFzUmF0aW8udGVzdChsYWJlbHMobm9kZSkpKSk7CiAgICBjb25zdCBvcHRpb25zID0gKCkgPT4gY29udHJvbHMoKS5maWx0ZXIobm9kZSA9PiAhdHJpZ2dlci5pbmNsdWRlcyhub2RlKSAmJiBleGFjdC50ZXN0KGxhYmVscyhub2RlKSkpOwogICAgY29uc3QgbW9kZWxTZXR0aW5ncyA9IGNvbnRyb2xzKCkuZmlsdGVyKG5vZGUgPT4gL05hbm8gQmFuYW5hfEltYWdlbi9pdS50ZXN0KGxhYmVscyhub2RlKSkgJiYgL1t4w5ddWzEtNF1cYi9pdS50ZXN0KGxhYmVscyhub2RlKSkpOwogICAgY29uc3Qgc2V0dGluZ3MgPSBtb2RlbFNldHRpbmdzLmxlbmd0aCA9PT0gMSA/IG1vZGVsU2V0dGluZ3NbMF0gOiBjb250cm9scygpLmZpbmQobm9kZSA9PiAvXijEkGnhu4F1IGtp4buHbiBrw61jaCBob+G6oXQgY8OgaSDEkeG6t3R8U2V0dGluZ3MgdHJpZ2dlcikkL2l1LnRlc3QoY2FwdGlvbihub2RlKSkpOwogICAgbGV0IG9wZW5lZCA9IGZhbHNlOwogICAgaWYgKCFvcHRpb25zKCkubGVuZ3RoICYmIHNldHRpbmdzKSB7CiAgICAgIHNldHRpbmdzLmNsaWNrKCk7CiAgICAgIG9wZW5lZCA9IHRydWU7CiAgICAgIGNvbnN0IGltYWdlID0gYXdhaXQgd2FpdEZvcigoKSA9PiBjb250cm9scygpLmZpbmQobm9kZSA9PiBub2RlLmdldEF0dHJpYnV0ZSgicm9sZSIpID09PSAicmFkaW8iICYmIC9eKEltYWdlfEjDrG5oIOG6o25ofOG6om5oKSQvaXUudGVzdChjYXB0aW9uKG5vZGUpKSksIDMpOwogICAgICBpZiAoaW1hZ2UgJiYgIXNlbGVjdGVkKGltYWdlKSkgaW1hZ2UuY2xpY2soKTsKICAgIH0KICAgIGlmICghb3B0aW9ucygpLmxlbmd0aCkgYXdhaXQgd2FpdEZvcigoKSA9PiBvcHRpb25zKCkubGVuZ3RoLCAzKTsKICAgIGlmICghb3B0aW9ucygpLmxlbmd0aCAmJiB0cmlnZ2VyLmxlbmd0aCA9PT0gMSkgdHJpZ2dlclswXS5jbGljaygpOwogICAgY29uc3Qgb3B0aW9uID0gYXdhaXQgd2FpdEZvcigoKSA9PiB7CiAgICAgIGlmICh0cmlnZ2VyLmxlbmd0aCA9PT0gMSAmJiBleGFjdC50ZXN0KGxhYmVscyh0cmlnZ2VyWzBdKSkgJiYgIW9wdGlvbnMoKS5sZW5ndGgpIHJldHVybiB0cmlnZ2VyWzBdOwogICAgICBjb25zdCBtYXRjaGVzID0gb3B0aW9ucygpOwogICAgICByZXR1cm4gbWF0Y2hlcy5sZW5ndGggPT09IDEgPyBtYXRjaGVzWzBdIDogbnVsbDsKICAgIH0sIDUpOwogICAgaWYgKCFvcHRpb24pIHRocm93IEVycm9yKCJLaMO0bmcgdMOsbSB0aOG6pXkgdOG7tyBs4buHICIgKyByYXRpbyArICIgdHJvbmcgR29vZ2xlIEZsb3cuIik7CiAgICBpZiAoIXRyaWdnZXIuaW5jbHVkZXMob3B0aW9uKSAmJiAhc2VsZWN0ZWQob3B0aW9uKSkgb3B0aW9uLmNsaWNrKCk7CiAgICBjb25zdCBzaW5nbGUgPSBjb250cm9scygpLmZpbHRlcihub2RlID0+IC9eW3jDl11ccyoxJC9pdS50ZXN0KGNhcHRpb24obm9kZSkpKTsKICAgIGlmIChzaW5nbGUubGVuZ3RoID09PSAxICYmICFzZWxlY3RlZChzaW5nbGVbMF0pKSBzaW5nbGVbMF0uY2xpY2soKTsKICAgIGlmIChvcGVuZWQgJiYgb3B0aW9ucygpLmxlbmd0aCAmJiBzZXR0aW5ncy5pc0Nvbm5lY3RlZCkgc2V0dGluZ3MuY2xpY2soKTsKICB9CiAgYXN5bmMgZnVuY3Rpb24gcnVuKCkgewogICAgbGV0IGFwcGxpZWRSYXRpbzsKICAgIHdoaWxlICh0cnVlKSB7CiAgICAgIGNvbnN0IGpvYiA9IGF3YWl0IHNlbmQoe29wOiJuZXh0In0pOwogICAgICBpZiAoam9iLmRvbmUpIHJldHVybjsKICAgICAgY29uc3QgYWdlbnQgPSBbLi4uZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW3JvbGU9ImNoZWNrYm94Il0saW5wdXRbdHlwZT0iY2hlY2tib3giXScpXS5maW5kKG5vZGUgPT4gdmlzaWJsZShub2RlKQogICAgICAgICYmIC9eKFTDoWMgbmjDom58QWdlbnQpJC9pdS50ZXN0KChub2RlLmdldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIpIHx8IG5vZGUudGV4dENvbnRlbnQgfHwgIiIpLnRyaW0oKSkKICAgICAgICAmJiAobm9kZS5jaGVja2VkIHx8IG5vZGUuZ2V0QXR0cmlidXRlKCJhcmlhLWNoZWNrZWQiKSA9PT0gInRydWUiKSk7CiAgICAgIGlmIChhZ2VudCkgeyBhZ2VudC5jbGljaygpOyBhd2FpdCBzbGVlcCgzMDApOyB9CiAgICAgIGxldCBmaWVsZCA9IGF3YWl0IHdhaXRGb3IocHJvbXB0RmllbGQsIDI1KTsKICAgICAgaWYgKCFmaWVsZCkgdGhyb3cgRXJyb3IoIktow7RuZyB4w6FjIMSR4buLbmggxJHGsOG7o2Mgw7QgcHJvbXB0IGR1eSBuaOG6pXQuIE3hu58gdHJhbmcgdOG6oW8g4bqjbmggdHJvbmcgZOG7sSDDoW4gRmxvdy4iKTsKICAgICAgY29uc3QgcmF0aW8gPSAvXlThuqFvIG3hu5l0IOG6o25oIG1pbmggaOG7jWEgdOG7tyBs4buHIChcZCs6XGQrKSAvdS5leGVjKGpvYi5wcm9tcHQpPy5bMV07CiAgICAgIGlmIChyYXRpbyAmJiByYXRpbyAhPT0gYXBwbGllZFJhdGlvKSB7CiAgICAgICAgYXdhaXQgc2VsZWN0UmF0aW8ocmF0aW8pOwogICAgICAgIGFwcGxpZWRSYXRpbyA9IHJhdGlvOwogICAgICAgIGZpZWxkID0gYXdhaXQgd2FpdEZvcihwcm9tcHRGaWVsZCwgMTApOwogICAgICAgIGlmICghZmllbGQpIHRocm93IEVycm9yKCJLaMO0bmcgdGjhuqV5IMO0IHByb21wdCBzYXUga2hpIGNo4buNbiB04bu3IGzhu4cuIik7CiAgICAgIH0KICAgICAgZmllbGQuZm9jdXMoKTsKICAgICAgaWYgKGZpZWxkIGluc3RhbmNlb2YgSFRNTFRleHRBcmVhRWxlbWVudCkgewogICAgICAgIE9iamVjdC5nZXRPd25Qcm9wZXJ0eURlc2NyaXB0b3IoSFRNTFRleHRBcmVhRWxlbWVudC5wcm90b3R5cGUsInZhbHVlIikuc2V0LmNhbGwoZmllbGQsam9iLnByb21wdCk7CiAgICAgICAgZmllbGQuZGlzcGF0Y2hFdmVudChuZXcgSW5wdXRFdmVudCgiaW5wdXQiLHtidWJibGVzOnRydWUsZGF0YTpqb2IucHJvbXB0LGlucHV0VHlwZToiaW5zZXJ0VGV4dCJ9KSk7CiAgICAgIH0gZWxzZSB7CiAgICAgICAgY29uc3Qgc2VsZWN0aW9uID0gd2luZG93LmdldFNlbGVjdGlvbigpOwogICAgICAgIGNvbnN0IHJhbmdlID0gZG9jdW1lbnQuY3JlYXRlUmFuZ2UoKTsKICAgICAgICByYW5nZS5zZWxlY3ROb2RlQ29udGVudHMoZmllbGQpOwogICAgICAgIHNlbGVjdGlvbi5yZW1vdmVBbGxSYW5nZXMoKTsKICAgICAgICBzZWxlY3Rpb24uYWRkUmFuZ2UocmFuZ2UpOwogICAgICAgIGlmICghZG9jdW1lbnQuZXhlY0NvbW1hbmQoImluc2VydFRleHQiLGZhbHNlLGpvYi5wcm9tcHQpKSB0aHJvdyBFcnJvcigiS2jDtG5nIMSRaeG7gW4gxJHGsOG7o2MgcHJvbXB0IHRyb25nIEZsb3cuIik7CiAgICAgIH0KICAgICAgYXdhaXQgc2xlZXAoNTAwKTsKICAgICAgY29uc3QgYnV0dG9ucyA9IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCJidXR0b24sW3JvbGU9YnV0dG9uXSIpXS5maWx0ZXIoYj0+dmlzaWJsZShiKSYmIWIuZGlzYWJsZWQmJmIuZ2V0QXR0cmlidXRlKCJhcmlhLWRpc2FibGVkIikhPT0idHJ1ZSIpOwogICAgICBjb25zdCBzdGFydCA9IGJ1dHRvbnMuZmlsdGVyKGJ1dHRvbiA9PiAvXihC4bqvdCDEkeG6p3UgdOG6oW98U3RhcnQgZ2VuZXJhdGluZ3xTdGFydCBjcmVhdGluZykkL2l1LnRlc3QoY2FwdGlvbihidXR0b24pKSk7CiAgICAgIGxldCBnZW5lcmF0ZSA9IHN0YXJ0Lmxlbmd0aCA9PT0gMSA/IHN0YXJ0IDogYnV0dG9ucy5maWx0ZXIoYj0+L14oR2VuZXJhdGUgSW1hZ2V8Q3JlYXRlIEltYWdlfFThuqFvIGjDrG5oIOG6o25ofFThuqFvIOG6o25oKSQvaS50ZXN0KGNhcHRpb24oYikpKTsKICAgICAgaWYgKGdlbmVyYXRlLmxlbmd0aCA9PT0gMCAmJiBtYXRjaGluZygvTmFubyBCYW5hbmF8SW1hZ2VuL2kpLmxlbmd0aCA9PT0gMSkKICAgICAgICBnZW5lcmF0ZSA9IGJ1dHRvbnMuZmlsdGVyKGI9Pi9eKEdlbmVyYXRlfFThuqFvKSQvaS50ZXN0KGNhcHRpb24oYikpKTsKICAgICAgaWYgKGdlbmVyYXRlLmxlbmd0aCA9PT0gMCkgewogICAgICAgIGNvbnN0IGVkaXRvciA9IGZpZWxkLmNsb3Nlc3QoImZvcm0iKSB8fCBmaWVsZC5wYXJlbnRFbGVtZW50OwogICAgICAgIGdlbmVyYXRlID0gYnV0dG9ucy5maWx0ZXIoYnV0dG9uID0+IC9eKGFycm93X2ZvcndhcmR8c2VuZHxn4butaXxi4bqvdCDEkeG6p3UgdOG6oW98c3RhcnQgZ2VuZXJhdGluZ3xzdGFydCBjcmVhdGluZykkL2l1LnRlc3QoY2FwdGlvbihidXR0b24pKQogICAgICAgICAgJiYgKGVkaXRvcj8uY29udGFpbnMoYnV0dG9uKSB8fCBmaWVsZC5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKS5ib3R0b20gPD0gYnV0dG9uLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpLmJvdHRvbQogICAgICAgICAgICAmJiBNYXRoLmFicyhmaWVsZC5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKS5yaWdodCAtIGJ1dHRvbi5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKS5yaWdodCkgPCAxMDApKTsKICAgICAgfQogICAgICBpZiAoZ2VuZXJhdGUubGVuZ3RoICE9PSAxKSB0aHJvdyBFcnJvcigiS2jDtG5nIG5o4bqtbiBkaeG7h24gxJHGsOG7o2MgbsO6dCB04bqhbyDhuqNuaC4gS2nhu4NtIHRyYSBjaOG6vyDEkeG7mSDhuqJuaCwgbW9kZWwgdsOgIGjhuqFuIG3hu6ljIEZsb3cuIik7CiAgICAgIGNvbnN0IGJlZm9yZSA9IG5ldyBTZXQoY2FuZGlkYXRlcygpLm1hcChpPT5pLmN1cnJlbnRTcmMpKTsKICAgICAgZ2VuZXJhdGVbMF0uY2xpY2soKTsKICAgICAgY29uc3Qgc3VibWl0dGVkID0gYXdhaXQgd2FpdEZvcigoKSA9PiAhZmllbGQuaXNDb25uZWN0ZWQKICAgICAgICB8fCAhKGZpZWxkLnZhbHVlID8/IGZpZWxkLmlubmVyVGV4dCA/PyBmaWVsZC50ZXh0Q29udGVudCA/PyAiIikudHJpbSgpCiAgICAgICAgfHwgY2FuZGlkYXRlcygpLnNvbWUoaW1hZ2UgPT4gIWJlZm9yZS5oYXMoaW1hZ2UuY3VycmVudFNyYykpLCAxMik7CiAgICAgIGlmICghc3VibWl0dGVkKSB0aHJvdyBFcnJvcigiRmxvdyB24bqrbiBnaeG7ryBwcm9tcHQgc2F1IGtoaSBi4bqlbSBC4bqvdCDEkeG6p3UgdOG6oW8uIEtp4buDbSB0cmEgdGjDtG5nIGLDoW8gdHJvbmcgZOG7sSDDoW4gRmxvdy4iKTsKICAgICAgbGV0IHJlc3VsdCwgc3RhYmxlU2luY2UgPSAwOwogICAgICBmb3IgKGxldCBlbGFwc2VkPTA7ZWxhcHNlZDwxNzU7ZWxhcHNlZCsrKSB7CiAgICAgICAgYXdhaXQgc2xlZXAoMTAwMCk7CiAgICAgICAgY29uc3QgZm91bmQgPSBbLi4ubmV3IFNldChjYW5kaWRhdGVzKCkubWFwKGk9PmkuY3VycmVudFNyYykuZmlsdGVyKHNyYz0+IWJlZm9yZS5oYXMoc3JjKSkpXTsKICAgICAgICBpZiAoZm91bmQubGVuZ3RoID4gMSkgdGhyb3cgRXJyb3IoIkZsb3cgdHLhuqMgbmhp4buBdSDhuqNuaC4gQ2jhu41uIG3hu5l0IGvhur90IHF14bqjIG3hu5dpIGzGsOG7o3QgcuG7k2kgY2jhuqF5IGzhuqFpLiIpOwogICAgICAgIGlmIChmb3VuZFswXSAhPT0gcmVzdWx0KSB7IHJlc3VsdD1mb3VuZFswXTsgc3RhYmxlU2luY2U9ZWxhcHNlZDsgfQogICAgICAgIGlmIChyZXN1bHQgJiYgZWxhcHNlZC1zdGFibGVTaW5jZT49MykgYnJlYWs7CiAgICAgIH0KICAgICAgaWYgKCFyZXN1bHQpIHRocm93IEVycm9yKCJDaMawYSBuaOG6rW4gxJHGsOG7o2Mg4bqjbmggbeG7m2kgdOG7qyBGbG93LiIpOwogICAgICBsZXQgZGF0YTsKICAgICAgdHJ5IHsKICAgICAgICBjb25zdCByZXNwb25zZSA9IGF3YWl0IGZldGNoKHJlc3VsdCk7CiAgICAgICAgaWYgKCFyZXNwb25zZS5vaykgdGhyb3cgRXJyb3IoIktow7RuZyB04bqjaSDEkcaw4bujYyDhuqNuaCB0csOqbiB0YWIuIik7CiAgICAgICAgY29uc3QgYmxvYiA9IGF3YWl0IHJlc3BvbnNlLmJsb2IoKTsKICAgICAgICBpZiAoIWJsb2IudHlwZS5zdGFydHNXaXRoKCJpbWFnZS8iKSB8fCBibG9iLnNpemUgPiAyNCoxMDI0KjEwMjQpIHRocm93IEVycm9yKCLhuqJuaCBraMO0bmcgaOG7o3AgbOG7hy4iKTsKICAgICAgICBkYXRhID0gYXdhaXQgbmV3IFByb21pc2UoKHJlc29sdmUscmVqZWN0KT0+e2NvbnN0IHJlYWRlcj1uZXcgRmlsZVJlYWRlcigpO3JlYWRlci5vbmxvYWQ9KCk9PnJlc29sdmUocmVhZGVyLnJlc3VsdCk7cmVhZGVyLm9uZXJyb3I9cmVqZWN0O3JlYWRlci5yZWFkQXNEYXRhVVJMKGJsb2IpO30pOwogICAgICB9IGNhdGNoIChlcnJvcikgeyBpZiAocmVzdWx0LnN0YXJ0c1dpdGgoImJsb2I6IikpIHRocm93IGVycm9yOyB9CiAgICAgIGF3YWl0IHNlbmQoe29wOiJpbWFnZSIsam9iOmpvYi5qb2IsaW5kZXg6am9iLmluZGV4LHVybDpyZXN1bHQsZGF0YX0pOwogICAgfQogIH0KICBhc3luYyBmdW5jdGlvbiB3YWl0Rm9yKGZpbmQsIHNlY29uZHMpIHsKICAgIGZvciAobGV0IGk9MDtpPHNlY29uZHMqMjtpKyspIHsKICAgICAgY29uc3QgdmFsdWU9ZmluZCgpOwogICAgICBpZiAodmFsdWUpIHJldHVybiB2YWx1ZTsKICAgICAgYXdhaXQgc2xlZXAoNTAwKTsKICAgIH0KICAgIHJldHVybiBudWxsOwogIH0KICBjb25zdCBwcm9qZWN0T3BlbiA9ICgpID0+IC9cL3Byb2plY3RzP1wvW14vXSsvLnRlc3QobG9jYXRpb24ucGF0aG5hbWUpOwogIGNvbnN0IG1hdGNoaW5nID0gKHBhdHRlcm4pID0+IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCJidXR0b24sW3JvbGU9YnV0dG9uXSxbcm9sZT10YWJdLGEiKV0KICAgIC5maWx0ZXIoaXRlbT0+dmlzaWJsZShpdGVtKSYmIWl0ZW0uZGlzYWJsZWQmJml0ZW0uZ2V0QXR0cmlidXRlKCJhcmlhLWRpc2FibGVkIikhPT0idHJ1ZSIKICAgICAgJiYgKHBhdHRlcm4udGVzdChjYXB0aW9uKGl0ZW0pKSB8fCBwYXR0ZXJuLnRlc3QoKGl0ZW0uaW5uZXJUZXh0IHx8IGl0ZW0udGV4dENvbnRlbnQgfHwgIiIpLnJlcGxhY2UoL1xzKy9ndSwgIiAiKS50cmltKCkpKSk7CiAgYXN5bmMgZnVuY3Rpb24gY29ubmVjdCgpIHsKICAgIGNvbnN0IHN0YXRlID0gYXdhaXQgc2VuZChwYWlyaW5nID8ge29wOiJwYWlyIix0b2tlbjpwYWlyaW5nfSA6IHtvcDoicmVzdW1lIn0pOwogICAgaWYgKHN0YXRlLmRvbmUpIHJldHVybjsKICAgIGlmIChwYWlyaW5nKSBoaXN0b3J5LnJlcGxhY2VTdGF0ZShoaXN0b3J5LnN0YXRlLCAiIiwgbG9jYXRpb24ucGF0aG5hbWUgKyBsb2NhdGlvbi5zZWFyY2gpOyAKICAgIGlmIChkb2N1bWVudC5yZWFkeVN0YXRlID09PSAibG9hZGluZyIpIGF3YWl0IG5ldyBQcm9taXNlKHJlc29sdmU9PmRvY3VtZW50LmFkZEV2ZW50TGlzdGVuZXIoIkRPTUNvbnRlbnRMb2FkZWQiLHJlc29sdmUse29uY2U6dHJ1ZX0pKTsKICAgIGlmICghcHJvamVjdE9wZW4oKSkgewogICAgICBpZiAoIXBhaXJpbmcpIHRocm93IEVycm9yKCJGbG93IHbhuqtuIOG7nyB0cmFuZyBjaOG7pyBzYXUga2hpIHThuqFvIGThu7Egw6FuLiBLaeG7g20gdHJhIMSRxINuZyBuaOG6rXAgcuG7k2kgY2jhuqF5IGzhuqFpLiIpOwogICAgICBjb25zdCBjcmVhdGUgPSBhd2FpdCB3YWl0Rm9yKCgpID0+IHsKICAgICAgICBjb25zdCBjaG9pY2VzID0gbWF0Y2hpbmcoL14oPzooPzphZGQoPzpfY2lyY2xlfF9ib3gpP3xwbHVzfFwrKVxzKik/KD86TmV3IHByb2plY3R8Q3JlYXRlIHByb2plY3R8Q3JlYXRlIG5ldyBwcm9qZWN0fEThu7Egw6FuIG3hu5tpfFThuqFvIGThu7Egw6FufFThuqFvIGThu7Egw6FuIG3hu5tpKSQvaXUpOwogICAgICAgIGNvbnN0IGNvbnRyb2xzID0gY2hvaWNlcy5maWx0ZXIoaXRlbSA9PiAhY2hvaWNlcy5zb21lKG90aGVyID0+IG90aGVyICE9PSBpdGVtICYmIGl0ZW0uY29udGFpbnMob3RoZXIpKSk7CiAgICAgICAgcmV0dXJuIGNvbnRyb2xzWzBdIHx8IG51bGw7CiAgICAgIH0sMzUpOwogICAgICBpZiAoIWNyZWF0ZSkgdGhyb3cgRXJyb3IoIktow7RuZyB0w6xtIHRo4bqleSBuw7p0IHThuqFvIGThu7Egw6FuIEZsb3cuIEtp4buDbSB0cmEgQ2hyb21lIMSRw6MgxJHEg25nIG5o4bqtcCB2w6AgeGVtIHRyYW5nIEZsb3cgY8OzIHRow7RuZyBiw6FvIGfDrC4iKTsKICAgICAgY3JlYXRlLmNsaWNrKCk7CiAgICAgIGlmICghYXdhaXQgd2FpdEZvcihwcm9qZWN0T3Blbiw0NSkpIHRocm93IEVycm9yKCJGbG93IGNoxrBhIG3hu58gZOG7sSDDoW4gbeG7m2kgc2F1IGtoaSBi4bqlbSB04bqhby4iKTsKICAgIH0KICAgIGlmICghYXdhaXQgd2FpdEZvcigoKT0+ZG9jdW1lbnQucXVlcnlTZWxlY3RvcigndGV4dGFyZWEsW2NvbnRlbnRlZGl0YWJsZT0idHJ1ZSJdJyksNDUpKQogICAgICB0aHJvdyBFcnJvcigiROG7sSDDoW4gbeG7m2kgY2jGsGEgaGnhu4duIMO0IHByb21wdC4iKTsKICAgIAogICAgYXdhaXQgcnVuKCk7CiAgfQogIGNvbm5lY3QoKS5jYXRjaChhc3luYyBlcnJvciA9PiB7CiAgICB0cnkge2F3YWl0IHNlbmQoe29wOiJlcnJvciIsbWVzc2FnZTplcnJvci5tZXNzYWdlfSk7fSBjYXRjaCB7IGNvbnNvbGUud2FybigiVuG6vSBUYXk6IGdow6lwIG7hu5FpIGhv4bq3YyBn4butaSB0cuG6oW5nIHRow6FpIHRo4bqldCBi4bqhaS4iLCBlcnJvci5tZXNzYWdlKTsgfQogIH0pLmZpbmFsbHkoKCk9Pnt3aW5kb3cuX192ZVRheVJ1bm5pbmc9ZmFsc2U7fSk7Cn0pKCk7",
    "popup.html": "PCFkb2N0eXBlIGh0bWw+PGh0bWwgbGFuZz0idmkiPjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij48c3R5bGU+Ym9keXtmb250OjE0cHggc3lzdGVtLXVpO3dpZHRoOjI3MHB4O3BhZGRpbmc6MTRweH08L3N0eWxlPjxiPlbhur0gVGF5IMK3IEdvb2dsZSBGbG93PC9iPjxwPlRyb25nIFbhur0gVGF5LCBi4bqlbSBU4buxIHThuqFvIHZpZGVvIMSR4buDIOG7qW5nIGThu6VuZyBt4bufIHRhYiBGbG93IHbDoCB04buxIGvhur90IG7hu5FpLjwvcD48L2h0bWw+Cg=="
}

def sync_chrome_bundle():
    folder = DATA / "chrome-extension"
    try:
        remote = json.loads(patch_read(PATCH_ROOT + "main/companion.json", 16384))
    except (OSError, ValueError):
        remote = BUNDLED_EXTENSION
    ref = remote if version_tuple(remote["version"]) > version_tuple(BUNDLED_EXTENSION["version"]) else BUNDLED_EXTENSION
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
        if ref is BUNDLED_EXTENSION:
            data = base64.b64decode(BUNDLED_EXTENSION_FILES[name], validate=True)
        else:
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
    while True:
        if (DATA / "chrome-extension" / "manifest.json").is_file() and LOCK.acquire(blocking=False):
            try:
                sync_chrome_bundle()
            except (OSError, ValueError, KeyError):
                pass
            finally:
                LOCK.release()
        time.sleep(60)


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
                    raise RuntimeError("Hết thời gian chờ ảnh từ Chrome. Kiểm tra tab Flow do Vẽ Tay mở và phiên bản tiện ích trong chrome://extensions.")
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
    prepare_data()
    import gradio as gr
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

