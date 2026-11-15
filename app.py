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
VERSION = "0.2.24"
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
    "version": "1.3.10",
    "commit": "2a9a3f45956ea5964cdbab317a6f6c70c7beb1f1",
    "files": {
        "worker.js": "0c0757c91a302e81bfbbc82888db561a37212beb",
        "manifest.json": "af416ecfb9a76f227341fe819e502366b878a15e",
        "flow.js": "f968135ba08735b9884334da351f00874e4d1ea1",
        "popup.html": "d1f3fe3d89500eb7788fc312842525a9c22193c4"
    }
}
BUNDLED_EXTENSION_FILES = {
    "worker.js": "Y29uc3QgYmFzZSA9ICJodHRwOi8vMTI3LjAuMC4xOjc4NzciOwpmdW5jdGlvbiBmbG93VGFiKHVybCkgeyByZXR1cm4gL15odHRwczpcL1wvKGZsb3dcLmdvb2dsZVwuY29tfGxhYnNcLmdvb2dsZSlcLy8udGVzdCh1cmwgfHwgIiIpOyB9CmFzeW5jIGZ1bmN0aW9uIGFwaSh0b2tlbiwgcGF0aCwgYm9keSkgewogIGNvbnN0IHIgPSBhd2FpdCBmZXRjaChiYXNlICsgcGF0aCwgewogICAgaGVhZGVyczogeyJYLVZlVGF5LVRva2VuIjp0b2tlbiwgLi4uKGJvZHkgPyB7IkNvbnRlbnQtVHlwZSI6Ym9keS50eXBlIHx8ICJhcHBsaWNhdGlvbi9qc29uIn0gOiB7fSl9LAogICAgbWV0aG9kOiBib2R5ID8gIlBPU1QiIDogIkdFVCIsIGJvZHkKICB9KTsKICBpZiAoIXIub2spIHRocm93IEVycm9yKChhd2FpdCByLnRleHQoKSkuc2xpY2UoMCwyNTApKTsKICByZXR1cm4gci5qc29uKCk7Cn0KY2hyb21lLnJ1bnRpbWUub25NZXNzYWdlLmFkZExpc3RlbmVyKChtLHNlbmRlcixyZXBseSkgPT4gewogIChhc3luYygpPT57CiAgICBpZiAoIWZsb3dUYWIoc2VuZGVyLnRhYj8udXJsKSkgdGhyb3cgRXJyb3IoIkNo4buJIG5o4bqtbiB5w6p1IGPhuqd1IHThu6sgdGFiIEZsb3cuIik7CiAgICBjb25zdCBrZXkgPSAicGFpcmluZzoiICsgc2VuZGVyLnRhYi5pZDsKICAgIGlmIChtLm9wID09PSAicGFpciIpIHsKICAgICAgaWYgKCEvXlthLWYwLTldezMyfSQvLnRlc3QobS50b2tlbiB8fCAiIikpIHRocm93IEVycm9yKCJNw6Mga+G6v3QgbuG7kWkga2jDtG5nIGjhu6NwIGzhu4cuIik7CiAgICAgIGNvbnN0IGpvYiA9IGF3YWl0IGFwaShtLnRva2VuLCAiL25leHQiKTsKICAgICAgaWYgKGpvYi5kb25lKSB0aHJvdyBFcnJvcigiVuG6vSBUYXkgY2jGsGEgY8OzIGPhuqNuaCBjaOG7nS4iKTsKICAgICAgYXdhaXQgY2hyb21lLnN0b3JhZ2Uuc2Vzc2lvbi5zZXQoe1trZXldOm0udG9rZW59KTsKICAgICAgcmV0dXJuIGpvYjsKICAgIH0KICAgIGNvbnN0IHNhdmVkID0gYXdhaXQgY2hyb21lLnN0b3JhZ2Uuc2Vzc2lvbi5nZXQoa2V5KTsKICAgIGNvbnN0IHBhaXJpbmcgPSBzYXZlZFtrZXldOwogICAgaWYgKG0ub3AgPT09ICJyZXN1bWUiKSByZXR1cm4gcGFpcmluZyA/IGFwaShwYWlyaW5nLCIvbmV4dCIpIDoge2RvbmU6dHJ1ZX07CiAgICBpZiAoIXBhaXJpbmcpIHRocm93IEVycm9yKCJUYWIgY2jGsGEgxJHGsOG7o2MgVuG6vSBUYXkga+G6v3QgbuG7kWkuIik7CiAgICBpZiAobS5vcCA9PT0gImFjdGl2YXRlIikgewogICAgICBhd2FpdCBjaHJvbWUudGFicy51cGRhdGUoc2VuZGVyLnRhYi5pZCwge2FjdGl2ZTp0cnVlfSk7CiAgICAgIGF3YWl0IGNocm9tZS53aW5kb3dzLnVwZGF0ZShzZW5kZXIudGFiLndpbmRvd0lkLCB7Zm9jdXNlZDp0cnVlfSk7CiAgICAgIHJldHVybiB7YWN0aXZlOnRydWV9OwogICAgfQogICAgaWYgKG0ub3AgPT09ICJuZXh0IikgcmV0dXJuIGFwaShwYWlyaW5nLCIvbmV4dCIpOwogICAgaWYgKG0ub3AgPT09ICJlcnJvciIpIHJldHVybiBhcGkocGFpcmluZywiL2Vycm9yIixKU09OLnN0cmluZ2lmeSh7bWVzc2FnZTptLm1lc3NhZ2V9KSk7CiAgICBpZiAobS5vcCA9PT0gImltYWdlIiAmJiAvXlthLWYwLTldezMyfSQvLnRlc3QobS5qb2IpICYmIE51bWJlci5pc0ludGVnZXIobS5pbmRleCkpIHsKICAgICAgbGV0IGltYWdlOwogICAgICBpZiAobS5kYXRhICYmIC9eZGF0YTppbWFnZVwvKHBuZ3x3ZWJwfGpwZWcpO2Jhc2U2NCwvLnRlc3QobS5kYXRhKSkgewogICAgICAgIGltYWdlID0gYXdhaXQgKGF3YWl0IGZldGNoKG0uZGF0YSkpLmJsb2IoKTsKICAgICAgfSBlbHNlIHsKICAgICAgICBjb25zdCB1ID0gbmV3IFVSTChtLnVybCk7CiAgICAgICAgaWYgKHUucHJvdG9jb2wgIT09ICJodHRwczoiIHx8ICEvKF58XC4pZ29vZ2xldXNlcmNvbnRlbnRcLmNvbSQvLnRlc3QodS5ob3N0bmFtZSkpIHRocm93IEVycm9yKCJLaMO0bmcgeMOhYyDEkeG7i25oIMSRxrDhu6NjIMSR4buLYSBjaOG7iSDhuqNuaCBr4bq/dCBxdeG6oy4iKTsKICAgICAgICBjb25zdCByID0gYXdhaXQgZmV0Y2godS5ocmVmLHtjcmVkZW50aWFsczoib21pdCIscmVkaXJlY3Q6ImVycm9yIn0pOwogICAgICAgIGlmICghci5vaykgdGhyb3cgRXJyb3IoIktow7RuZyB04bqjaSDEkcaw4bujYyDhuqNuaCBr4bq/dCBxdeG6oy4iKTsKICAgICAgICBpbWFnZSA9IGF3YWl0IHIuYmxvYigpOwogICAgICB9CiAgICAgIGlmIChpbWFnZS5zaXplID4gMjQqMTAyNCoxMDI0IHx8ICFpbWFnZS50eXBlLnN0YXJ0c1dpdGgoImltYWdlLyIpKSB0aHJvdyBFcnJvcigi4bqibmgga+G6v3QgcXXhuqMga2jDtG5nIGjhu6NwIGzhu4cuIik7CiAgICAgIHJldHVybiBhcGkocGFpcmluZywiL2ltYWdlLyIrbS5qb2IrIi8iK20uaW5kZXgsaW1hZ2UpOwogICAgfQogICAgdGhyb3cgRXJyb3IoIlnDqnUgY+G6p3Uga2jDtG5nIGjhu6NwIGzhu4cuIik7CiAgfSkoKS50aGVuKHJlc3VsdD0+cmVwbHkoe29rOnRydWUscmVzdWx0fSksZXJyb3I9PnJlcGx5KHtvazpmYWxzZSxlcnJvcjplcnJvci5tZXNzYWdlfSkpOwogIHJldHVybiB0cnVlOwp9KTsKY2hyb21lLnRhYnMub25SZW1vdmVkLmFkZExpc3RlbmVyKHRhYklkID0+IGNocm9tZS5zdG9yYWdlLnNlc3Npb24ucmVtb3ZlKCJwYWlyaW5nOiIgKyB0YWJJZCkpOwoKY29uc3QgZXh0ZW5zaW9uQ2hlY2sgPSAidmV0YXktY2hlY2stbG9jYWwtdXBkYXRlIjsKZnVuY3Rpb24gcmVnaXN0ZXJDaGVjaygpIHsKICBjaHJvbWUuYWxhcm1zLmNyZWF0ZShleHRlbnNpb25DaGVjaywge3BlcmlvZEluTWludXRlczoxfSk7Cn0KY2hyb21lLnJ1bnRpbWUub25JbnN0YWxsZWQuYWRkTGlzdGVuZXIocmVnaXN0ZXJDaGVjayk7CmNocm9tZS5ydW50aW1lLm9uU3RhcnR1cC5hZGRMaXN0ZW5lcihyZWdpc3RlckNoZWNrKTsKY2hyb21lLmFsYXJtcy5vbkFsYXJtLmFkZExpc3RlbmVyKGFzeW5jIGFsYXJtID0+IHsKICBpZiAoYWxhcm0ubmFtZSAhPT0gZXh0ZW5zaW9uQ2hlY2spIHJldHVybjsKICB0cnkgewogICAgY29uc3QgY3VycmVudCA9IGNocm9tZS5ydW50aW1lLmdldE1hbmlmZXN0KCk7CiAgICBjb25zdCByZXNwb25zZSA9IGF3YWl0IGZldGNoKGNocm9tZS5ydW50aW1lLmdldFVSTCgibWFuaWZlc3QuanNvbiIpICsgIj90PSIgKyBEYXRlLm5vdygpLCB7Y2FjaGU6Im5vLXN0b3JlIn0pOwogICAgaWYgKCFyZXNwb25zZS5vaykgcmV0dXJuOwogICAgY29uc3QgZGlzayA9IGF3YWl0IHJlc3BvbnNlLmpzb24oKTsKICAgIGlmICghL15cZCtcLlxkK1wuXGQrJC8udGVzdChkaXNrLnZlcnNpb24pIHx8IGRpc2submFtZSAhPT0gY3VycmVudC5uYW1lIHx8CiAgICAgICAgZGlzay5tYW5pZmVzdF92ZXJzaW9uICE9PSBjdXJyZW50Lm1hbmlmZXN0X3ZlcnNpb24pIHJldHVybjsKICAgIGNvbnN0IG9sZCA9IGN1cnJlbnQudmVyc2lvbi5zcGxpdCgiLiIpLm1hcChOdW1iZXIpOwogICAgY29uc3QgbmV4dCA9IGRpc2sudmVyc2lvbi5zcGxpdCgiLiIpLm1hcChOdW1iZXIpOwogICAgY29uc3QgY2hhbmdlZCA9IG5leHQuZmluZEluZGV4KCh2YWx1ZSxpKT0+dmFsdWUhPT1vbGRbaV0pOwogICAgaWYgKGNoYW5nZWQgPCAwIHx8IG5leHRbY2hhbmdlZF0gPCBvbGRbY2hhbmdlZF0pIHJldHVybjsKICAgIGZvcihjb25zdCBrZXkgb2YgWyJwZXJtaXNzaW9ucyIsImhvc3RfcGVybWlzc2lvbnMiLCJvcHRpb25hbF9wZXJtaXNzaW9ucyIsIm9wdGlvbmFsX2hvc3RfcGVybWlzc2lvbnMiXSkgewogICAgICBpZihKU09OLnN0cmluZ2lmeShbLi4uKGN1cnJlbnRba2V5XXx8W10pXS5zb3J0KCkpIT09SlNPTi5zdHJpbmdpZnkoWy4uLihkaXNrW2tleV18fFtdKV0uc29ydCgpKSkgcmV0dXJuOwogICAgfQogICAgY29uc3QgbWF0Y2hlcyA9IHZhbHVlID0+ICh2YWx1ZS5jb250ZW50X3NjcmlwdHMgfHwgW10pLmZsYXRNYXAoc2NyaXB0PT5zY3JpcHQubWF0Y2hlcyB8fCBbXSkuc29ydCgpOwogICAgaWYoSlNPTi5zdHJpbmdpZnkobWF0Y2hlcyhjdXJyZW50KSkhPT1KU09OLnN0cmluZ2lmeShtYXRjaGVzKGRpc2spKSkgcmV0dXJuOwogICAgY29uc3QgaWRsZSA9IGF3YWl0IGZldGNoKGJhc2UrIi9leHRlbnNpb24taWRsZSIse2NhY2hlOiJuby1zdG9yZSJ9KTsKICAgIGlmKGlkbGUub2sgJiYgKGF3YWl0IGlkbGUuanNvbigpKS5pZGxlID09PSB0cnVlKSBjaHJvbWUucnVudGltZS5yZWxvYWQoKTsKICB9IGNhdGNoIHt9Cn0pOwo=",
    "popup.html": "PCFkb2N0eXBlIGh0bWw+PGh0bWwgbGFuZz0idmkiPjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij48c3R5bGU+Ym9keXtmb250OjE0cHggc3lzdGVtLXVpO3dpZHRoOjI3MHB4O3BhZGRpbmc6MTRweH08L3N0eWxlPjxiPlbhur0gVGF5IMK3IEdvb2dsZSBGbG93PC9iPjxwPlRyb25nIFbhur0gVGF5LCBi4bqlbSBU4buxIHThuqFvIHZpZGVvIMSR4buDIOG7qW5nIGThu6VuZyBt4bufIHRhYiBGbG93IHbDoCB04buxIGvhur90IG7hu5FpLjwvcD48L2h0bWw+Cg==",
    "flow.js": "KCgpID0+IHsKICBjb25zdCBwYWlyaW5nID0gL14jdmV0YXk9KFthLWYwLTldezMyfSkkLy5leGVjKGxvY2F0aW9uLmhhc2gpPy5bMV07CiAgaWYgKHdpbmRvdy5fX3ZlVGF5UnVubmluZykgcmV0dXJuOwogIHdpbmRvdy5fX3ZlVGF5UnVubmluZyA9IHRydWU7CiAgY29uc3Qgc2xlZXAgPSAobXMpID0+IG5ldyBQcm9taXNlKHJlc29sdmUgPT4gc2V0VGltZW91dChyZXNvbHZlLG1zKSk7CiAgY29uc3QgdmlzaWJsZSA9IG5vZGUgPT4gbm9kZS5nZXRDbGllbnRSZWN0cygpLmxlbmd0aCAmJiBnZXRDb21wdXRlZFN0eWxlKG5vZGUpLnZpc2liaWxpdHkgIT09ICJoaWRkZW4iOwogIGNvbnN0IHNlbmQgPSBhc3luYyAobWVzc2FnZSkgPT4gewogICAgY29uc3QgcmVzcG9uc2UgPSBhd2FpdCBjaHJvbWUucnVudGltZS5zZW5kTWVzc2FnZShtZXNzYWdlKTsKICAgIGlmICghcmVzcG9uc2U/Lm9rKSB0aHJvdyBFcnJvcihyZXNwb25zZT8uZXJyb3IgfHwgIk3huqV0IGvhur90IG7hu5FpIHbhu5tpIFbhur0gVGF5LiIpOwogICAgcmV0dXJuIHJlc3BvbnNlLnJlc3VsdDsKICB9OwogIGNvbnN0IGNhbmRpZGF0ZXMgPSAoKSA9PiBbLi4uZG9jdW1lbnQuaW1hZ2VzXS5maWx0ZXIoaW1nID0+IGltZy5jb21wbGV0ZSAmJiBpbWcubmF0dXJhbFdpZHRoID49IDM4NCAmJgogICAgaW1nLm5hdHVyYWxIZWlnaHQgPj0gMzg0ICYmIHZpc2libGUoaW1nKSAmJiAvXihibG9iOnxodHRwczpcL1wvW14vXSpcLmdvb2dsZXVzZXJjb250ZW50XC5jb21cLykvaS50ZXN0KGltZy5jdXJyZW50U3JjKSk7CiAgY29uc3QgY2FwdGlvbiA9IGJ1dHRvbiA9PiAoYnV0dG9uLmdldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIpIHx8IGJ1dHRvbi5nZXRBdHRyaWJ1dGUoInRpdGxlIikgfHwgYnV0dG9uLnRleHRDb250ZW50IHx8ICIiKS50cmltKCk7CiAgZnVuY3Rpb24gcHJvbXB0RmllbGQoKSB7CiAgICBjb25zdCBmaWVsZHMgPSBbLi4uZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgndGV4dGFyZWEsW2NvbnRlbnRlZGl0YWJsZT0idHJ1ZSJdLFtjb250ZW50ZWRpdGFibGU9InBsYWludGV4dC1vbmx5Il0nKV0KICAgICAgLmZpbHRlcihmaWVsZCA9PiB2aXNpYmxlKGZpZWxkKSAmJiAhZmllbGQuY2xvc2VzdCgnW2luZXJ0XSxbYXJpYS1oaWRkZW49InRydWUiXSxbcm9sZT0ic2VhcmNoIl0nKQogICAgICAgICYmICFmaWVsZC5kaXNhYmxlZCAmJiAhZmllbGQucmVhZE9ubHkgJiYgZmllbGQuZ2V0QXR0cmlidXRlKCJhcmlhLWRpc2FibGVkIikgIT09ICJ0cnVlIik7CiAgICBjb25zdCBsYWJlbHMgPSBmaWVsZCA9PiBbImFyaWEtbGFiZWwiLCAicGxhY2Vob2xkZXIiLCAiZGF0YS1wbGFjZWhvbGRlciIsICJ0aXRsZSJdCiAgICAgIC5tYXAobmFtZSA9PiBmaWVsZC5nZXRBdHRyaWJ1dGUobmFtZSkgfHwgIiIpLmpvaW4oIiAiKTsKICAgIGNvbnN0IGVsaWdpYmxlID0gZmllbGRzLmZpbHRlcihmaWVsZCA9PiAhL3NlYXJjaHx0w6xtIGtp4bq/bXxyZW5hbWV8xJHhu5VpIHTDqm58cHJvamVjdCBuYW1lfHTDqm4gZOG7sSDDoW4vaS50ZXN0KGxhYmVscyhmaWVsZCkpCiAgICAgICYmICFmaWVsZHMuc29tZShwYXJlbnQgPT4gcGFyZW50ICE9PSBmaWVsZCAmJiBwYXJlbnQuY29udGFpbnMoZmllbGQpKSk7CiAgICBjb25zdCBtYXRjaGVzID0gZWxpZ2libGUuZmlsdGVyKGZpZWxkID0+IC9wcm9tcHR8d2hhdCAoPzpkb3x3b3VsZCkgeW91fGRlc2NyaWJlfG3DtCB04bqjfGLhuqFuIG114buRbnx04bqhbyBu4buZaSBkdW5nL2kudGVzdChsYWJlbHMoZmllbGQpKSk7CiAgICBpZiAobWF0Y2hlcy5sZW5ndGggPT09IDEpIHJldHVybiBtYXRjaGVzWzBdOwogICAgcmV0dXJuIGVsaWdpYmxlLmxlbmd0aCA9PT0gMSA/IGVsaWdpYmxlWzBdIDogbnVsbDsKICB9CiAgY29uc3QgcmF0aW9MYWJlbCA9IHJhdGlvID0+IG5ldyBSZWdFeHAoIig/Ol58W14wLTldKSIgKyByYXRpby5yZXBsYWNlKCI6IiwgIlxccypbOuKItl1cXHMqIikgKyAiKD8hWzAtOV0pIiwgInUiKTsKICBhc3luYyBmdW5jdGlvbiBzZWxlY3RSYXRpbyhyYXRpbykgewogICAgY29uc3Qgc2VsZWN0ZWQgPSBub2RlID0+IFsiYXJpYS1jaGVja2VkIiwgImFyaWEtc2VsZWN0ZWQiLCAiYXJpYS1wcmVzc2VkIl0uc29tZShrZXkgPT4gbm9kZS5nZXRBdHRyaWJ1dGUoa2V5KSA9PT0gInRydWUiKQogICAgICB8fCBub2RlLmdldEF0dHJpYnV0ZSgiZGF0YS1zdGF0ZSIpID09PSAiY2hlY2tlZCIgfHwgbm9kZS5nZXRBdHRyaWJ1dGUoImRhdGEtc3RhdGUiKSA9PT0gImFjdGl2ZSI7CiAgICBjb25zdCBjb250cm9scyA9ICgpID0+IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCJidXR0b24sW3JvbGU9YnV0dG9uXSxbcm9sZT1yYWRpb10sW3JvbGU9b3B0aW9uXSxbcm9sZT1tZW51aXRlbXJhZGlvXSxbcm9sZT10YWJdIildCiAgICAgIC5maWx0ZXIobm9kZSA9PiB2aXNpYmxlKG5vZGUpICYmICFub2RlLmRpc2FibGVkICYmIG5vZGUuZ2V0QXR0cmlidXRlKCJhcmlhLWRpc2FibGVkIikgIT09ICJ0cnVlIik7CiAgICBjb25zdCBsYWJlbHMgPSBub2RlID0+IFtjYXB0aW9uKG5vZGUpLCBub2RlLmdldEF0dHJpYnV0ZSgiZGF0YS12YWx1ZSIpIHx8ICIiLCBub2RlLmlubmVyVGV4dCB8fCAiIl0uam9pbigiICIpOwogICAgY29uc3QgZXhhY3QgPSByYXRpb0xhYmVsKHJhdGlvKTsKICAgIGNvbnN0IGhhc1JhdGlvID0gcmF0aW9MYWJlbCgiWzAtOV0rOlswLTldKyIpOwogICAgY29uc3QgdHJpZ2dlciA9IGNvbnRyb2xzKCkuZmlsdGVyKG5vZGUgPT4gL2FzcGVjdCByYXRpb3x04bu3IGzhu4d8dOG7iSBs4buHfGNyb3BfKD86bGFuZHNjYXBlfHBvcnRyYWl0KS9pdS50ZXN0KGxhYmVscyhub2RlKSkKICAgICAgfHwgKG5vZGUuZ2V0QXR0cmlidXRlKCJhcmlhLWhhc3BvcHVwIikgJiYgaGFzUmF0aW8udGVzdChsYWJlbHMobm9kZSkpKSk7CiAgICBjb25zdCBvcHRpb25zID0gKCkgPT4gY29udHJvbHMoKS5maWx0ZXIobm9kZSA9PiAhdHJpZ2dlci5pbmNsdWRlcyhub2RlKSAmJiBleGFjdC50ZXN0KGxhYmVscyhub2RlKSkpOwogICAgY29uc3QgbW9kZWxTZXR0aW5ncyA9IGNvbnRyb2xzKCkuZmlsdGVyKG5vZGUgPT4gL05hbm8gQmFuYW5hfEltYWdlbi9pdS50ZXN0KGxhYmVscyhub2RlKSkgJiYgL1t4w5ddWzEtNF1cYi9pdS50ZXN0KGxhYmVscyhub2RlKSkpOwogICAgY29uc3Qgc2V0dGluZ3MgPSBtb2RlbFNldHRpbmdzLmxlbmd0aCA9PT0gMSA/IG1vZGVsU2V0dGluZ3NbMF0gOiBjb250cm9scygpLmZpbmQobm9kZSA9PiAvXijEkGnhu4F1IGtp4buHbiBrw61jaCBob+G6oXQgY8OgaSDEkeG6t3R8U2V0dGluZ3MgdHJpZ2dlcikkL2l1LnRlc3QoY2FwdGlvbihub2RlKSkpOwogICAgbGV0IG9wZW5lZCA9IGZhbHNlOwogICAgaWYgKCFvcHRpb25zKCkubGVuZ3RoICYmIHNldHRpbmdzKSB7CiAgICAgIHNldHRpbmdzLmNsaWNrKCk7CiAgICAgIG9wZW5lZCA9IHRydWU7CiAgICAgIGNvbnN0IGltYWdlID0gYXdhaXQgd2FpdEZvcigoKSA9PiBjb250cm9scygpLmZpbmQobm9kZSA9PiBub2RlLmdldEF0dHJpYnV0ZSgicm9sZSIpID09PSAicmFkaW8iICYmIC9eKEltYWdlfEjDrG5oIOG6o25ofOG6om5oKSQvaXUudGVzdChjYXB0aW9uKG5vZGUpKSksIDMpOwogICAgICBpZiAoaW1hZ2UgJiYgIXNlbGVjdGVkKGltYWdlKSkgaW1hZ2UuY2xpY2soKTsKICAgIH0KICAgIGlmICghb3B0aW9ucygpLmxlbmd0aCkgYXdhaXQgd2FpdEZvcigoKSA9PiBvcHRpb25zKCkubGVuZ3RoLCAzKTsKICAgIGlmICghb3B0aW9ucygpLmxlbmd0aCAmJiB0cmlnZ2VyLmxlbmd0aCA9PT0gMSkgdHJpZ2dlclswXS5jbGljaygpOwogICAgY29uc3Qgb3B0aW9uID0gYXdhaXQgd2FpdEZvcigoKSA9PiB7CiAgICAgIGlmICh0cmlnZ2VyLmxlbmd0aCA9PT0gMSAmJiBleGFjdC50ZXN0KGxhYmVscyh0cmlnZ2VyWzBdKSkgJiYgIW9wdGlvbnMoKS5sZW5ndGgpIHJldHVybiB0cmlnZ2VyWzBdOwogICAgICBjb25zdCBtYXRjaGVzID0gb3B0aW9ucygpOwogICAgICByZXR1cm4gbWF0Y2hlcy5sZW5ndGggPT09IDEgPyBtYXRjaGVzWzBdIDogbnVsbDsKICAgIH0sIDUpOwogICAgaWYgKCFvcHRpb24pIHRocm93IEVycm9yKCJLaMO0bmcgdMOsbSB0aOG6pXkgdOG7tyBs4buHICIgKyByYXRpbyArICIgdHJvbmcgR29vZ2xlIEZsb3cuIik7CiAgICBpZiAoIXRyaWdnZXIuaW5jbHVkZXMob3B0aW9uKSAmJiAhc2VsZWN0ZWQob3B0aW9uKSkgb3B0aW9uLmNsaWNrKCk7CiAgICBjb25zdCBzaW5nbGUgPSBjb250cm9scygpLmZpbHRlcihub2RlID0+IC9eW3jDl11ccyoxJC9pdS50ZXN0KGNhcHRpb24obm9kZSkpKTsKICAgIGlmIChzaW5nbGUubGVuZ3RoID09PSAxICYmICFzZWxlY3RlZChzaW5nbGVbMF0pKSBzaW5nbGVbMF0uY2xpY2soKTsKICAgIGlmIChvcGVuZWQgJiYgb3B0aW9ucygpLmxlbmd0aCAmJiBzZXR0aW5ncy5pc0Nvbm5lY3RlZCkgc2V0dGluZ3MuY2xpY2soKTsKICB9CiAgYXN5bmMgZnVuY3Rpb24gcnVuKCkgewogICAgbGV0IGFwcGxpZWRSYXRpbzsKICAgIHdoaWxlICh0cnVlKSB7CiAgICAgIGNvbnN0IGpvYiA9IGF3YWl0IHNlbmQoe29wOiJuZXh0In0pOwogICAgICBpZiAoam9iLmRvbmUpIHJldHVybjsKICAgICAgY29uc3QgYWdlbnQgPSBbLi4uZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW3JvbGU9ImNoZWNrYm94Il0saW5wdXRbdHlwZT0iY2hlY2tib3giXScpXS5maW5kKG5vZGUgPT4gdmlzaWJsZShub2RlKQogICAgICAgICYmIC9eKFTDoWMgbmjDom58QWdlbnQpJC9pdS50ZXN0KChub2RlLmdldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIpIHx8IG5vZGUudGV4dENvbnRlbnQgfHwgIiIpLnRyaW0oKSkKICAgICAgICAmJiAobm9kZS5jaGVja2VkIHx8IG5vZGUuZ2V0QXR0cmlidXRlKCJhcmlhLWNoZWNrZWQiKSA9PT0gInRydWUiKSk7CiAgICAgIGlmIChhZ2VudCkgeyBhZ2VudC5jbGljaygpOyBhd2FpdCBzbGVlcCgzMDApOyB9CiAgICAgIGxldCBmaWVsZCA9IGF3YWl0IHdhaXRGb3IocHJvbXB0RmllbGQsIDI1KTsKICAgICAgaWYgKCFmaWVsZCkgdGhyb3cgRXJyb3IoIktow7RuZyB4w6FjIMSR4buLbmggxJHGsOG7o2Mgw7QgcHJvbXB0IGR1eSBuaOG6pXQuIE3hu58gdHJhbmcgdOG6oW8g4bqjbmggdHJvbmcgZOG7sSDDoW4gRmxvdy4iKTsKICAgICAgY29uc3QgcmF0aW8gPSAvXlThuqFvIG3hu5l0IOG6o25oIG1pbmggaOG7jWEgdOG7tyBs4buHIChcZCs6XGQrKSAvdS5leGVjKGpvYi5wcm9tcHQpPy5bMV07CiAgICAgIGlmIChyYXRpbyAmJiByYXRpbyAhPT0gYXBwbGllZFJhdGlvKSB7CiAgICAgICAgYXdhaXQgc2VsZWN0UmF0aW8ocmF0aW8pOwogICAgICAgIGFwcGxpZWRSYXRpbyA9IHJhdGlvOwogICAgICAgIGZpZWxkID0gYXdhaXQgd2FpdEZvcihwcm9tcHRGaWVsZCwgMTApOwogICAgICAgIGlmICghZmllbGQpIHRocm93IEVycm9yKCJLaMO0bmcgdGjhuqV5IMO0IHByb21wdCBzYXUga2hpIGNo4buNbiB04bu3IGzhu4cuIik7CiAgICAgIH0KICAgICAgYXdhaXQgc2VuZCh7b3A6ImFjdGl2YXRlIn0pOwogICAgICBpZiAoIWF3YWl0IHdhaXRGb3IoKCkgPT4gZG9jdW1lbnQuaGFzRm9jdXMoKSAmJiAhZG9jdW1lbnQuaGlkZGVuLCA2KSkKICAgICAgICB0aHJvdyBFcnJvcigiQ2hyb21lIGNoxrBhIMSRxrBhIHRhYiBGbG93IGzDqm4gdHLGsOG7m2MgxJHhu4MgZ+G7rWkgcHJvbXB0LiIpOwogICAgICBmaWVsZC5mb2N1cygpOwogICAgICBpZiAoZmllbGQgaW5zdGFuY2VvZiBIVE1MVGV4dEFyZWFFbGVtZW50KSB7CiAgICAgICAgT2JqZWN0LmdldE93blByb3BlcnR5RGVzY3JpcHRvcihIVE1MVGV4dEFyZWFFbGVtZW50LnByb3RvdHlwZSwidmFsdWUiKS5zZXQuY2FsbChmaWVsZCxqb2IucHJvbXB0KTsKICAgICAgICBmaWVsZC5kaXNwYXRjaEV2ZW50KG5ldyBJbnB1dEV2ZW50KCJpbnB1dCIse2J1YmJsZXM6dHJ1ZSxkYXRhOmpvYi5wcm9tcHQsaW5wdXRUeXBlOiJpbnNlcnRUZXh0In0pKTsKICAgICAgfSBlbHNlIHsKICAgICAgICBjb25zdCBzZWxlY3Rpb24gPSB3aW5kb3cuZ2V0U2VsZWN0aW9uKCk7CiAgICAgICAgY29uc3QgcmFuZ2UgPSBkb2N1bWVudC5jcmVhdGVSYW5nZSgpOwogICAgICAgIHJhbmdlLnNlbGVjdE5vZGVDb250ZW50cyhmaWVsZCk7CiAgICAgICAgc2VsZWN0aW9uLnJlbW92ZUFsbFJhbmdlcygpOwogICAgICAgIHNlbGVjdGlvbi5hZGRSYW5nZShyYW5nZSk7CiAgICAgICAgaWYgKCFkb2N1bWVudC5leGVjQ29tbWFuZCgiaW5zZXJ0VGV4dCIsZmFsc2Usam9iLnByb21wdCkpIHRocm93IEVycm9yKCJLaMO0bmcgxJFp4buBbiDEkcaw4bujYyBwcm9tcHQgdHJvbmcgRmxvdy4iKTsKICAgICAgfQogICAgICBhd2FpdCBzbGVlcCg1MDApOwogICAgICBjb25zdCBidXR0b25zID0gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoImJ1dHRvbixbcm9sZT1idXR0b25dIildLmZpbHRlcihiPT52aXNpYmxlKGIpJiYhYi5kaXNhYmxlZCYmYi5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSE9PSJ0cnVlIik7CiAgICAgIGNvbnN0IHN0YXJ0ID0gYnV0dG9ucy5maWx0ZXIoYnV0dG9uID0+IC9eKELhuq90IMSR4bqndSB04bqhb3xTdGFydCBnZW5lcmF0aW5nfFN0YXJ0IGNyZWF0aW5nKSQvaXUudGVzdChjYXB0aW9uKGJ1dHRvbikpKTsKICAgICAgbGV0IGdlbmVyYXRlID0gc3RhcnQubGVuZ3RoID09PSAxID8gc3RhcnQgOiBidXR0b25zLmZpbHRlcihiPT4vXihHZW5lcmF0ZSBJbWFnZXxDcmVhdGUgSW1hZ2V8VOG6oW8gaMOsbmgg4bqjbmh8VOG6oW8g4bqjbmgpJC9pLnRlc3QoY2FwdGlvbihiKSkpOwogICAgICBpZiAoZ2VuZXJhdGUubGVuZ3RoID09PSAwICYmIG1hdGNoaW5nKC9OYW5vIEJhbmFuYXxJbWFnZW4vaSkubGVuZ3RoID09PSAxKQogICAgICAgIGdlbmVyYXRlID0gYnV0dG9ucy5maWx0ZXIoYj0+L14oR2VuZXJhdGV8VOG6oW8pJC9pLnRlc3QoY2FwdGlvbihiKSkpOwogICAgICBpZiAoZ2VuZXJhdGUubGVuZ3RoID09PSAwKSB7CiAgICAgICAgY29uc3QgZWRpdG9yID0gZmllbGQuY2xvc2VzdCgiZm9ybSIpIHx8IGZpZWxkLnBhcmVudEVsZW1lbnQ7CiAgICAgICAgZ2VuZXJhdGUgPSBidXR0b25zLmZpbHRlcihidXR0b24gPT4gL14oYXJyb3dfZm9yd2FyZHxzZW5kfGfhu61pfGLhuq90IMSR4bqndSB04bqhb3xzdGFydCBnZW5lcmF0aW5nfHN0YXJ0IGNyZWF0aW5nKSQvaXUudGVzdChjYXB0aW9uKGJ1dHRvbikpCiAgICAgICAgICAmJiAoZWRpdG9yPy5jb250YWlucyhidXR0b24pIHx8IGZpZWxkLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpLmJvdHRvbSA8PSBidXR0b24uZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCkuYm90dG9tCiAgICAgICAgICAgICYmIE1hdGguYWJzKGZpZWxkLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpLnJpZ2h0IC0gYnV0dG9uLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpLnJpZ2h0KSA8IDEwMCkpOwogICAgICB9CiAgICAgIGlmIChnZW5lcmF0ZS5sZW5ndGggIT09IDEpIHRocm93IEVycm9yKCJLaMO0bmcgbmjhuq1uIGRp4buHbiDEkcaw4bujYyBuw7p0IHThuqFvIOG6o25oLiBLaeG7g20gdHJhIGNo4bq/IMSR4buZIOG6om5oLCBtb2RlbCB2w6AgaOG6oW4gbeG7qWMgRmxvdy4iKTsKICAgICAgY29uc3QgYmVmb3JlID0gbmV3IFNldChjYW5kaWRhdGVzKCkubWFwKGk9PmkuY3VycmVudFNyYykpOwogICAgICBjb25zdCBlZGl0b3JCb3ggPSBmaWVsZC5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKTsKICAgICAgY29uc3QgbmVhcmJ5QXJyb3dzID0gYnV0dG9ucy5maWx0ZXIoYnV0dG9uID0+IHsKICAgICAgICBjb25zdCBib3ggPSBidXR0b24uZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCk7CiAgICAgICAgcmV0dXJuIGJveC50b3AgPj0gZWRpdG9yQm94LmJvdHRvbSAtIDggJiYgYm94LnRvcCA8PSBlZGl0b3JCb3guYm90dG9tICsgMTAwCiAgICAgICAgICAmJiBNYXRoLmFicyhib3gucmlnaHQgLSBlZGl0b3JCb3gucmlnaHQpIDwgMTAwCiAgICAgICAgICAmJiAoWy4uLmJ1dHRvbi5xdWVyeVNlbGVjdG9yQWxsKCJtYXQtaWNvbixbZGF0YS1pY29uXSxbYXJpYS1oaWRkZW49dHJ1ZV0saW1nIildLnNvbWUoaWNvbiA9PgogICAgICAgICAgICAvXihhcnJvd19mb3J3YXJkfHNlbmQpJC9pLnRlc3QoKGljb24uZ2V0QXR0cmlidXRlKCJkYXRhLWljb24iKSB8fCBpY29uLmdldEF0dHJpYnV0ZSgiYWx0IikgfHwgaWNvbi50ZXh0Q29udGVudCB8fCAiIikudHJpbSgpKSkKICAgICAgICAgICAgfHwgL14oQuG6r3QgxJHhuqd1IHThuqFvfFN0YXJ0IGdlbmVyYXRpbmd8U3RhcnQgY3JlYXRpbmcpJC9pdS50ZXN0KGNhcHRpb24oYnV0dG9uKSkpOwogICAgICB9KTsKICAgICAgY29uc3Qgc3VibWl0ID0gbmVhcmJ5QXJyb3dzLmxlbmd0aCA9PT0gMSA/IG5lYXJieUFycm93c1swXSA6IGdlbmVyYXRlWzBdOwogICAgICBzdWJtaXQuc2Nyb2xsSW50b1ZpZXcoe2Jsb2NrOiJuZWFyZXN0IiwgaW5saW5lOiJuZWFyZXN0In0pOwogICAgICBzdWJtaXQuY2xpY2soKTsKICAgICAgY29uc3Qgc3VibWl0dGVkID0gYXdhaXQgd2FpdEZvcigoKSA9PiAhZmllbGQuaXNDb25uZWN0ZWQKICAgICAgICB8fCAhKGZpZWxkLnZhbHVlID8/IGZpZWxkLmlubmVyVGV4dCA/PyBmaWVsZC50ZXh0Q29udGVudCA/PyAiIikudHJpbSgpCiAgICAgICAgfHwgY2FuZGlkYXRlcygpLnNvbWUoaW1hZ2UgPT4gIWJlZm9yZS5oYXMoaW1hZ2UuY3VycmVudFNyYykpLCAxMik7CiAgICAgIGlmICghc3VibWl0dGVkKSB7CiAgICAgICAgY29uc3QgaGludCA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoImRpdiIpOwogICAgICAgIGhpbnQudGV4dENvbnRlbnQgPSAiVuG6vSBUYXkgxJFhbmcgY2jhu50uIELhuqVtIG3FqWkgdMOqbiBC4bqvdCDEkeG6p3UgdOG6oW8gxJHhu4MgdGnhur9wIHThu6VjIGPhuqNuaCAiICsgKGpvYi5pbmRleCArIDEpICsgIi4iOwogICAgICAgIE9iamVjdC5hc3NpZ24oaGludC5zdHlsZSwge3Bvc2l0aW9uOiJmaXhlZCIsIGJvdHRvbToiMTQwcHgiLCByaWdodDoiMjRweCIsIHpJbmRleDoiMjE0NzQ4MzY0NyIsIGJhY2tncm91bmQ6IiNmZmYiLCBjb2xvcjoiIzIyMiIsIHBhZGRpbmc6IjEycHgiLCBib3JkZXJSYWRpdXM6IjhweCIsIHBvaW50ZXJFdmVudHM6Im5vbmUifSk7CiAgICAgICAgZG9jdW1lbnQuYm9keS5hcHBlbmQoaGludCk7CiAgICAgICAgdHJ5IHsKICAgICAgICAgIGlmICghYXdhaXQgd2FpdEZvcigoKSA9PiAhZmllbGQuaXNDb25uZWN0ZWQgfHwgIShmaWVsZC52YWx1ZSA/PyBmaWVsZC5pbm5lclRleHQgPz8gIiIpLnRyaW0oKQogICAgICAgICAgICB8fCBjYW5kaWRhdGVzKCkuc29tZShpbWFnZSA9PiAhYmVmb3JlLmhhcyhpbWFnZS5jdXJyZW50U3JjKSksIDM1KSkKICAgICAgICAgICAgdGhyb3cgRXJyb3IoIkZsb3cgY2jGsGEgbmjhuq1uIGzhu4duaCB04bqhbyDhuqNuaCBjaG8gY+G6o25oICIgKyAoam9iLmluZGV4ICsgMSkgKyAiLiIpOwogICAgICAgIH0gZmluYWxseSB7IGhpbnQucmVtb3ZlKCk7IH0KICAgICAgfQogICAgICBsZXQgcmVzdWx0LCBzdGFibGVTaW5jZSA9IDA7CiAgICAgIGZvciAobGV0IGVsYXBzZWQ9MDtlbGFwc2VkPDE3NTtlbGFwc2VkKyspIHsKICAgICAgICBhd2FpdCBzbGVlcCgxMDAwKTsKICAgICAgICBjb25zdCBmb3VuZCA9IFsuLi5uZXcgU2V0KGNhbmRpZGF0ZXMoKS5tYXAoaT0+aS5jdXJyZW50U3JjKS5maWx0ZXIoc3JjPT4hYmVmb3JlLmhhcyhzcmMpKSldOwogICAgICAgIGlmIChmb3VuZC5sZW5ndGggPiAxKSB0aHJvdyBFcnJvcigiRmxvdyB0cuG6oyBuaGnhu4F1IOG6o25oLiBDaOG7jW4gbeG7mXQga+G6v3QgcXXhuqMgbeG7l2kgbMaw4bujdCBy4buTaSBjaOG6oXkgbOG6oWkuIik7CiAgICAgICAgaWYgKGZvdW5kWzBdICE9PSByZXN1bHQpIHsgcmVzdWx0PWZvdW5kWzBdOyBzdGFibGVTaW5jZT1lbGFwc2VkOyB9CiAgICAgICAgaWYgKHJlc3VsdCAmJiBlbGFwc2VkLXN0YWJsZVNpbmNlPj0zKSBicmVhazsKICAgICAgfQogICAgICBpZiAoIXJlc3VsdCkgdGhyb3cgRXJyb3IoIkNoxrBhIG5o4bqtbiDEkcaw4bujYyDhuqNuaCBt4bubaSB04burIEZsb3cuIik7CiAgICAgIGxldCBkYXRhOwogICAgICB0cnkgewogICAgICAgIGNvbnN0IHJlc3BvbnNlID0gYXdhaXQgZmV0Y2gocmVzdWx0KTsKICAgICAgICBpZiAoIXJlc3BvbnNlLm9rKSB0aHJvdyBFcnJvcigiS2jDtG5nIHThuqNpIMSRxrDhu6NjIOG6o25oIHRyw6puIHRhYi4iKTsKICAgICAgICBjb25zdCBibG9iID0gYXdhaXQgcmVzcG9uc2UuYmxvYigpOwogICAgICAgIGlmICghYmxvYi50eXBlLnN0YXJ0c1dpdGgoImltYWdlLyIpIHx8IGJsb2Iuc2l6ZSA+IDI0KjEwMjQqMTAyNCkgdGhyb3cgRXJyb3IoIuG6om5oIGtow7RuZyBo4bujcCBs4buHLiIpOwogICAgICAgIGRhdGEgPSBhd2FpdCBuZXcgUHJvbWlzZSgocmVzb2x2ZSxyZWplY3QpPT57Y29uc3QgcmVhZGVyPW5ldyBGaWxlUmVhZGVyKCk7cmVhZGVyLm9ubG9hZD0oKT0+cmVzb2x2ZShyZWFkZXIucmVzdWx0KTtyZWFkZXIub25lcnJvcj1yZWplY3Q7cmVhZGVyLnJlYWRBc0RhdGFVUkwoYmxvYik7fSk7CiAgICAgIH0gY2F0Y2ggKGVycm9yKSB7IGlmIChyZXN1bHQuc3RhcnRzV2l0aCgiYmxvYjoiKSkgdGhyb3cgZXJyb3I7IH0KICAgICAgYXdhaXQgc2VuZCh7b3A6ImltYWdlIixqb2I6am9iLmpvYixpbmRleDpqb2IuaW5kZXgsdXJsOnJlc3VsdCxkYXRhfSk7CiAgICB9CiAgfQogIGFzeW5jIGZ1bmN0aW9uIHdhaXRGb3IoZmluZCwgc2Vjb25kcykgewogICAgZm9yIChsZXQgaT0wO2k8c2Vjb25kcyoyO2krKykgewogICAgICBjb25zdCB2YWx1ZT1maW5kKCk7CiAgICAgIGlmICh2YWx1ZSkgcmV0dXJuIHZhbHVlOwogICAgICBhd2FpdCBzbGVlcCg1MDApOwogICAgfQogICAgcmV0dXJuIG51bGw7CiAgfQogIGNvbnN0IHByb2plY3RPcGVuID0gKCkgPT4gL1wvcHJvamVjdHM/XC9bXi9dKy8udGVzdChsb2NhdGlvbi5wYXRobmFtZSk7CiAgY29uc3QgbWF0Y2hpbmcgPSAocGF0dGVybikgPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoImJ1dHRvbixbcm9sZT1idXR0b25dLFtyb2xlPXRhYl0sYSIpXQogICAgLmZpbHRlcihpdGVtPT52aXNpYmxlKGl0ZW0pJiYhaXRlbS5kaXNhYmxlZCYmaXRlbS5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSE9PSJ0cnVlIgogICAgICAmJiAocGF0dGVybi50ZXN0KGNhcHRpb24oaXRlbSkpIHx8IHBhdHRlcm4udGVzdCgoaXRlbS5pbm5lclRleHQgfHwgaXRlbS50ZXh0Q29udGVudCB8fCAiIikucmVwbGFjZSgvXHMrL2d1LCAiICIpLnRyaW0oKSkpKTsKICBhc3luYyBmdW5jdGlvbiBjb25uZWN0KCkgewogICAgY29uc3Qgc3RhdGUgPSBhd2FpdCBzZW5kKHBhaXJpbmcgPyB7b3A6InBhaXIiLHRva2VuOnBhaXJpbmd9IDoge29wOiJyZXN1bWUifSk7CiAgICBpZiAoc3RhdGUuZG9uZSkgcmV0dXJuOwogICAgaWYgKHBhaXJpbmcpIGhpc3RvcnkucmVwbGFjZVN0YXRlKGhpc3Rvcnkuc3RhdGUsICIiLCBsb2NhdGlvbi5wYXRobmFtZSArIGxvY2F0aW9uLnNlYXJjaCk7IAogICAgaWYgKGRvY3VtZW50LnJlYWR5U3RhdGUgPT09ICJsb2FkaW5nIikgYXdhaXQgbmV3IFByb21pc2UocmVzb2x2ZT0+ZG9jdW1lbnQuYWRkRXZlbnRMaXN0ZW5lcigiRE9NQ29udGVudExvYWRlZCIscmVzb2x2ZSx7b25jZTp0cnVlfSkpOwogICAgaWYgKCFwcm9qZWN0T3BlbigpKSB7CiAgICAgIGlmICghcGFpcmluZykgdGhyb3cgRXJyb3IoIkZsb3cgduG6q24g4bufIHRyYW5nIGNo4bunIHNhdSBraGkgdOG6oW8gZOG7sSDDoW4uIEtp4buDbSB0cmEgxJHEg25nIG5o4bqtcCBy4buTaSBjaOG6oXkgbOG6oWkuIik7CiAgICAgIGNvbnN0IGNyZWF0ZSA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gewogICAgICAgIGNvbnN0IGNob2ljZXMgPSBtYXRjaGluZygvXig/Oig/OmFkZCg/Ol9jaXJjbGV8X2JveCk/fHBsdXN8XCspXHMqKT8oPzpOZXcgcHJvamVjdHxDcmVhdGUgcHJvamVjdHxDcmVhdGUgbmV3IHByb2plY3R8ROG7sSDDoW4gbeG7m2l8VOG6oW8gZOG7sSDDoW58VOG6oW8gZOG7sSDDoW4gbeG7m2kpJC9pdSk7CiAgICAgICAgY29uc3QgY29udHJvbHMgPSBjaG9pY2VzLmZpbHRlcihpdGVtID0+ICFjaG9pY2VzLnNvbWUob3RoZXIgPT4gb3RoZXIgIT09IGl0ZW0gJiYgaXRlbS5jb250YWlucyhvdGhlcikpKTsKICAgICAgICByZXR1cm4gY29udHJvbHNbMF0gfHwgbnVsbDsKICAgICAgfSwzNSk7CiAgICAgIGlmICghY3JlYXRlKSB0aHJvdyBFcnJvcigiS2jDtG5nIHTDrG0gdGjhuqV5IG7DunQgdOG6oW8gZOG7sSDDoW4gRmxvdy4gS2nhu4NtIHRyYSBDaHJvbWUgxJHDoyDEkcSDbmcgbmjhuq1wIHbDoCB4ZW0gdHJhbmcgRmxvdyBjw7MgdGjDtG5nIGLDoW8gZ8OsLiIpOwogICAgICBjcmVhdGUuY2xpY2soKTsKICAgICAgaWYgKCFhd2FpdCB3YWl0Rm9yKHByb2plY3RPcGVuLDQ1KSkgdGhyb3cgRXJyb3IoIkZsb3cgY2jGsGEgbeG7nyBk4buxIMOhbiBt4bubaSBzYXUga2hpIGLhuqVtIHThuqFvLiIpOwogICAgfQogICAgaWYgKCFhd2FpdCB3YWl0Rm9yKCgpPT5kb2N1bWVudC5xdWVyeVNlbGVjdG9yKCd0ZXh0YXJlYSxbY29udGVudGVkaXRhYmxlPSJ0cnVlIl0nKSw0NSkpCiAgICAgIHRocm93IEVycm9yKCJE4buxIMOhbiBt4bubaSBjaMawYSBoaeG7h24gw7QgcHJvbXB0LiIpOwogICAgCiAgICBhd2FpdCBydW4oKTsKICB9CiAgY29ubmVjdCgpLmNhdGNoKGFzeW5jIGVycm9yID0+IHsKICAgIHRyeSB7YXdhaXQgc2VuZCh7b3A6ImVycm9yIixtZXNzYWdlOmVycm9yLm1lc3NhZ2V9KTt9IGNhdGNoIHsgY29uc29sZS53YXJuKCJW4bq9IFRheTogZ2jDqXAgbuG7kWkgaG/hurdjIGfhu61pIHRy4bqhbmcgdGjDoWkgdGjhuqV0IGLhuqFpLiIsIGVycm9yLm1lc3NhZ2UpOyB9CiAgfSkuZmluYWxseSgoKT0+e3dpbmRvdy5fX3ZlVGF5UnVubmluZz1mYWxzZTt9KTsKfSkoKTs=",
    "manifest.json": "ewogICJtYW5pZmVzdF92ZXJzaW9uIjogMywKICAibmFtZSI6ICJW4bq9IFRheSDCtyBHb29nbGUgRmxvdyIsCiAgInZlcnNpb24iOiAiMS4zLjEwIiwKICAiYWN0aW9uIjogewogICAgImRlZmF1bHRfcG9wdXAiOiAicG9wdXAuaHRtbCIKICB9LAogICJiYWNrZ3JvdW5kIjogewogICAgInNlcnZpY2Vfd29ya2VyIjogIndvcmtlci5qcyIKICB9LAogICJwZXJtaXNzaW9ucyI6IFsKICAgICJzdG9yYWdlIiwKICAgICJhbGFybXMiCiAgXSwKICAiaG9zdF9wZXJtaXNzaW9ucyI6IFsKICAgICJodHRwOi8vMTI3LjAuMC4xOjc4NzcvKiIsCiAgICAiaHR0cHM6Ly8qLmdvb2dsZXVzZXJjb250ZW50LmNvbS8qIiwKICAgICJodHRwczovL2Zsb3cuZ29vZ2xlLmNvbS8qIiwKICAgICJodHRwczovL2xhYnMuZ29vZ2xlLyoiCiAgXSwKICAiY29udGVudF9zY3JpcHRzIjogWwogICAgewogICAgICAibWF0Y2hlcyI6IFsKICAgICAgICAiaHR0cHM6Ly9mbG93Lmdvb2dsZS5jb20vKiIsCiAgICAgICAgImh0dHBzOi8vbGFicy5nb29nbGUvKiIKICAgICAgXSwKICAgICAgImpzIjogWwogICAgICAgICJmbG93LmpzIgogICAgICBdLAogICAgICAicnVuX2F0IjogImRvY3VtZW50X3N0YXJ0IgogICAgfQogIF0KfQo="
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
                if not state["event"].wait(330):
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

