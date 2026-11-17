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
VERSION = "0.2.55"
PATCH_ABI = "vetay-windows-026"
PATCH_ROOT = "https://raw.githubusercontent.com/taikhoanzaizai-ux/vetay/"

if (ROOT / "ffmpeg.exe").is_file():
    os.environ["FFMPEG_BINARY"] = str(ROOT / "ffmpeg.exe")
LOCK = threading.Lock()
LAST_ANALYSIS = {}
WHISPER_MODEL = None
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
    global WHISPER_MODEL
    if WHISPER_MODEL is None:
        WHISPER_MODEL = WhisperModel("small", device="cpu", compute_type="int8", download_root=str(DATA / "models"))
    segments, _ = WHISPER_MODEL.transcribe(str(voice), language="vi", vad_filter=True, word_timestamps=True)
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

def prompt_for(text, ratio="9:16", shared_prompt=""):
    rules = (
        (r"(?:chia|phân bổ).{0,45}(?:lương|ngân sách|phong bì)|(?:lương|ngân sách).{0,35}(?:chia|phong bì)",
         "Góc máy chếch từ trên xuống. Nhân vật chia tiền vào các phong bì trên bàn; dùng biểu tượng nhà, khiên và ly cà phê nếu cảnh có đủ ba khoản."),
        (r"chia.{0,35}(?:phần|tuần)|mỗi phần|500[.,]000",
         "Góc nhìn từ trên xuống: bàn tay đặt từng phần ngân sách vào các ô tuần trên lịch. Làm nổi bật các phần bằng hình khối lặp lại."),
        (r"lịch sử (?:giao dịch|thanh toán)|tiền đã đi đâu",
         "Cận cảnh nhân vật nhìn điện thoại, nét mặt ngạc nhiên. Trên màn hình là vài dòng giao dịch biểu thị bằng hình khối và biểu tượng mua sắm."),
        (r"quỹ dự phòng|tài khoản riêng|để dành|tiết kiệm|dành dụm",
         "Góc nhìn ba phần tư. Nhân vật chuyển một phần tiền sang hũ hoặc phong bì riêng có biểu tượng chiếc khiên; làm rõ đường di chuyển của tiền."),
        (r"nhận lương|lương (?:về|đã về)",
         "Cảnh trung tại bàn, nhân vật nhìn điện thoại khi lương về. Dùng mũi tên hướng vào ví để thể hiện dòng tiền."),
        (r"cà phê|ăn vặt|đồ uống",
         "Cận cảnh bàn tay thanh toán cạnh ly cà phê hoặc món ăn được nhắc đến. Bố trí ví và món mua thành hai điểm nhìn rõ ràng."),
        (r"nhón gót|bắp chân",
         "Góc nhìn ngang, lấy rõ từ đầu gối đến bàn chân. Thể hiện tư thế nhón gót và phần bắp chân bằng chuyển động dễ hiểu."),
        (r"thanh toán|chi tiêu|chi phí",
         "Góc nhìn chếch qua vai. Nhân vật đối chiếu các khoản chi trên bàn, với hóa đơn và đồng xu được nhóm theo nội dung đang kể."),
        (r"một năm|mỗi tháng|mỗi tuần|365 ngày|cuối tháng",
         "Dùng lịch để bàn làm chủ thể. Nhân vật chỉ vào mốc thời gian, dùng các ô lịch và mũi tên để biểu thị khoảng thời gian được nhắc."),
    )
    scene = next((visual for key, visual in rules if re.search(key, text, re.I)), "")
    if not scene:
        scene = ("Chuyển ý chính thành một hành động hoặc quan hệ nhìn thấy được. "
                 "Đặt chủ thể ở tiền cảnh, chọn bối cảnh phù hợp với hành động và dùng đạo cụ để làm rõ ý.")
    style = (shared_prompt or "").strip() or (
        "Nền trắng, nét đậm rõ, ít chi tiết, xanh ngọc và vàng nhạt. "
        "Không chữ, số, logo hoặc watermark."
    )
    return (f"Tạo một ảnh minh họa tỷ lệ {ratio} để chuyển thành video vẽ tay.\n"
            f"CẢNH CẦN VẼ — {scene}\n"
            f"PHONG CÁCH — Áp dụng trực tiếp vào nhân vật, đạo cụ, nền và bố cục cảnh trên: {style}\n"
            f"Ý NGHĨA CẢNH — {text}\n"
            "Chỉ minh họa nội dung của cảnh này. Không chép nguyên lời đọc lên ảnh. "
            "Ưu tiên yêu cầu chung nếu gợi ý đạo cụ hoặc góc nhìn xung đột với phong cách đã chọn.")


def timecode(frame):
    ms = round(frame * 1000 / 24)
    minutes, remainder = divmod(ms, 60000)
    seconds, fraction = divmod(remainder, 1000)
    return f"{minutes:02}:{seconds:02}.{fraction:03}"

def highlight_text(text):
    text = " ".join(text.split())
    number = re.search(
        r"(?<!\w)\d+(?:[.,]\d+)*(?:\s*(?:tỷ|triệu|nghìn|ngàn))?"
        r"(?:\s*(?:đồng|đ|%|kg|km|giờ|phút|giây|ngày|tuần|tháng|năm|lần|buổi|tuổi)\b|%)",
        text, re.IGNORECASE,
    )
    if number:
        return number.group().strip()
    phrase = re.split(r"[,;!?。]|(?<!\d)\.(?!\d)", text, maxsplit=1)[0].strip()
    return " ".join((phrase or text).split()[:7])[:74].rstrip(" ,.:;")


def highlight_overlay(text, width, height, target):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(image)
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    size = max(14, min(width, height) // 13)
    padding = max(8, size // 2)
    max_width = width * 86 // 100 - 2 * padding
    for font_size in range(size, 9, -1):
        try:
            font = ImageFont.truetype(str(fonts / "arialbd.ttf"), font_size)
        except OSError:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", font_size)
        words = text.split()
        lines = [""]
        for word in words:
            trial = (lines[-1] + " " + word).strip()
            if lines[-1] and draw.textlength(trial, font=font) > max_width:
                lines.append(word)
            else:
                lines[-1] = trial
        if len(lines) <= 2 and all(draw.textlength(line, font=font) <= max_width for line in lines):
            break
    else:
        return False
    label = "\n".join(lines)
    spacing = max(3, font_size // 5)
    left, top, right, bottom = draw.multiline_textbbox((0, 0), label, font=font, spacing=spacing)
    box_width = right - left + padding * 2
    box_height = bottom - top + padding * 2
    x = (width - box_width) // 2
    y = height - (height // 12) - box_height
    draw.rounded_rectangle((x, y, x + box_width, y + box_height), radius=padding,
                           fill="#FFF1B8", outline="#08776B", width=max(2, padding // 5))
    draw.multiline_text((x + padding - left, y + padding - top), label, font=font,
                        fill="#07594F", spacing=spacing, align="center")
    image.save(target)
    return True


def scene_rows(plan, ratio="9:16", highlights=False, shared_prompt="", for_flow=True):
    image_ratio = {"21:9": "16:9", "4:5": "3:4"}.get(ratio, ratio) if for_flow else ratio
    if image_ratio != ratio:
        shared_prompt = (shared_prompt or "").strip() + "\nGiữ chủ thể trong vùng giữa để đặt vào video tỷ lệ " + ratio + "."
    return [[i, timecode(start), timecode(end), round((end-start)/24, 3), text, prompt_for(text, image_ratio, shared_prompt), highlight_text(text) if highlights else ""]
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

def render_folder(voice, pictures, plan, destination, report, ratio="9:16", highlights=False, full_resolution=True):
    from PIL import Image, ImageOps
    import numpy as np
    path, render_video, Settings = engine()
    width, height = VIDEO_SIZES[ratio]
    preview_width, preview_height = (width, height) if full_resolution else tuple(size // 5 * 2 for size in (width, height))
    if len(pictures) < len(plan):
        raise ValueError(f"Cần {len(plan)} ảnh, hiện có {len(pictures)}.")
    with tempfile.TemporaryDirectory(prefix="vetay-", dir=destination.parent) as work:
        work = Path(work)
        clips = []
        for i, ((start, end, text), picture) in enumerate(zip(plan, pictures)):
            report(f"Whiteboard Animation VI: cảnh {i+1}/{len(plan)}…")
            if picture is None:
                clip = work / f"blank-{i:04}.mp4"
                ffmpeg("-f", "lavfi", "-i", f"color=white:s={preview_width}x{preview_height}:r=24",
                       "-frames:v", str(end-start), "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", clip)
            else:
                with Image.open(picture) as raw:
                    art = ImageOps.contain(ImageOps.exif_transpose(raw).convert("RGB"), (preview_width, preview_height), method=Image.Resampling.LANCZOS)
                canvas = Image.new("RGB", (preview_width, preview_height), "white")
                canvas.paste(art, ((preview_width-art.width)//2, (preview_height-art.height)//2))
                settings = Settings(device="cpu", encoder="cpu", width=preview_width, height=preview_height, fps=24, fit="contain",
                                    duration=(end-start)/24, draw_percent=70, mode="e",
                                    subject_first=(path / "assets/v181d/model.safetensors").is_file())
                clip, _ = render_video(np.array(canvas), settings, output_dir=work)
            if highlights and (label := highlight_text(text)):
                overlay = work / f"highlight-{i:04}.png"
                if highlight_overlay(label, preview_width, preview_height, overlay):
                    titled = work / f"titled-{i:04}.mp4"
                    delay = min(1.3, (end-start)/48)
                    ffmpeg("-i", clip, "-i", overlay, "-filter_complex",
                           f"[0:v][1:v]overlay=enable='gte(t,{delay:.3f})':eof_action=repeat",
                           "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", titled)
                    clip = titled
            clips.append(clip)
        listing = work / "clips.txt"
        listing.write_text("".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
        output = work / "complete.mp4"
        ffmpeg("-f", "concat", "-safe", "0", "-i", listing, "-i", voice, "-map", "0:v", "-map", "1:a",
               "-vf", f"scale={width}:{height}:flags=lanczos", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
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
    "version": "1.3.29",
    "commit": "cae916926a416b7ca24417784230af4ffd5f4c30",
    "files": {
        "manifest.json": "3c84d438ee5c4b7c66f24a62b0890f3bba062e0f",
        "popup.html": "d1f3fe3d89500eb7788fc312842525a9c22193c4",
        "worker.js": "660c7e6e25905dab10945c110c8a79fe5532324c",
        "flow.js": "e6cee1bf0df8aa604c7c2313fcde743a3f75d7c7"
    }
}
BUNDLED_EXTENSION_FILES = {
    "manifest.json": "ewogICJtYW5pZmVzdF92ZXJzaW9uIjogMywKICAibmFtZSI6ICJW4bq9IFRheSDCtyBHb29nbGUgRmxvdyIsCiAgInZlcnNpb24iOiAiMS4zLjI5IiwKICAiYWN0aW9uIjogewogICAgImRlZmF1bHRfcG9wdXAiOiAicG9wdXAuaHRtbCIKICB9LAogICJiYWNrZ3JvdW5kIjogewogICAgInNlcnZpY2Vfd29ya2VyIjogIndvcmtlci5qcyIKICB9LAogICJwZXJtaXNzaW9ucyI6IFsKICAgICJzdG9yYWdlIiwKICAgICJhbGFybXMiLAogICAgImRlYnVnZ2VyIiwKICAgICJkb3dubG9hZHMiCiAgXSwKICAiaG9zdF9wZXJtaXNzaW9ucyI6IFsKICAgICJodHRwOi8vMTI3LjAuMC4xLyoiLAogICAgImh0dHBzOi8vKi5nb29nbGV1c2VyY29udGVudC5jb20vKiIsCiAgICAiaHR0cHM6Ly9mbG93Lmdvb2dsZS5jb20vKiIsCiAgICAiaHR0cHM6Ly9sYWJzLmdvb2dsZS8qIiwKICAgICJodHRwczovL211c2UuYWkvKiIKICBdLAogICJjb250ZW50X3NjcmlwdHMiOiBbCiAgICB7CiAgICAgICJtYXRjaGVzIjogWwogICAgICAgICJodHRwczovL2Zsb3cuZ29vZ2xlLmNvbS8qIiwKICAgICAgICAiaHR0cHM6Ly9sYWJzLmdvb2dsZS8qIgogICAgICBdLAogICAgICAianMiOiBbCiAgICAgICAgImZsb3cuanMiCiAgICAgIF0sCiAgICAgICJydW5fYXQiOiAiZG9jdW1lbnRfc3RhcnQiCiAgICB9LAogICAgewogICAgICAibWF0Y2hlcyI6IFsKICAgICAgICAiaHR0cHM6Ly9tdXNlLmFpLyoiCiAgICAgIF0sCiAgICAgICJqcyI6IFsKICAgICAgICAiZmxvdy5qcyIKICAgICAgXSwKICAgICAgInJ1bl9hdCI6ICJkb2N1bWVudF9zdGFydCIKICAgIH0KICBdCn0K",
    "popup.html": "PCFkb2N0eXBlIGh0bWw+PGh0bWwgbGFuZz0idmkiPjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij48c3R5bGU+Ym9keXtmb250OjE0cHggc3lzdGVtLXVpO3dpZHRoOjI3MHB4O3BhZGRpbmc6MTRweH08L3N0eWxlPjxiPlbhur0gVGF5IMK3IEdvb2dsZSBGbG93PC9iPjxwPlRyb25nIFbhur0gVGF5LCBi4bqlbSBU4buxIHThuqFvIHZpZGVvIMSR4buDIOG7qW5nIGThu6VuZyBt4bufIHRhYiBGbG93IHbDoCB04buxIGvhur90IG7hu5FpLjwvcD48L2h0bWw+Cg==",
    "worker.js": "Y29uc3QgYmFzZSA9ICJodHRwOi8vMTI3LjAuMC4xOjc4NzciOwpmdW5jdGlvbiBmbG93VGFiKHVybCkgeyByZXR1cm4gL15odHRwczpcL1wvKGZsb3dcLmdvb2dsZVwuY29tfGxhYnNcLmdvb2dsZSlcLy8udGVzdCh1cmwgfHwgIiIpOyB9CmFzeW5jIGZ1bmN0aW9uIGFwaSh7dG9rZW4sIHBvcnR9LCBwYXRoLCBib2R5KSB7CiAgY29uc3QgciA9IGF3YWl0IGZldGNoKCJodHRwOi8vMTI3LjAuMC4xOiIgKyBwb3J0ICsgcGF0aCwgewogICAgaGVhZGVyczogeyJYLVZlVGF5LVRva2VuIjp0b2tlbiwgLi4uKGJvZHkgPyB7IkNvbnRlbnQtVHlwZSI6Ym9keS50eXBlIHx8ICJhcHBsaWNhdGlvbi9qc29uIn0gOiB7fSl9LAogICAgbWV0aG9kOiBib2R5ID8gIlBPU1QiIDogIkdFVCIsIGJvZHkKICB9KTsKICBpZiAoIXIub2spIHRocm93IEVycm9yKChhd2FpdCByLnRleHQoKSkuc2xpY2UoMCwyNTApKTsKICByZXR1cm4gci5qc29uKCk7Cn0KY29uc3QgcGVuZGluZ0Rvd25sb2FkcyA9IG5ldyBNYXAoKTsKY2hyb21lLmRvd25sb2Fkcy5vbkRldGVybWluaW5nRmlsZW5hbWUuYWRkTGlzdGVuZXIoKGl0ZW0sIHN1Z2dlc3QpID0+IHsKICBjb25zdCBwZW5kaW5nID0gWy4uLnBlbmRpbmdEb3dubG9hZHMudmFsdWVzKCldLmZpbHRlcihlbnRyeSA9PiAhZW50cnkuaWQgJiYgRGF0ZS5ub3coKSA8IGVudHJ5LmRlYWRsaW5lKTsKICBpZiAocGVuZGluZy5sZW5ndGggIT09IDEpIHJldHVybjsKICBjb25zdCBlbnRyeSA9IHBlbmRpbmdbMF07CiAgY29uc3QgdXJscyA9IFtpdGVtLnVybCwgaXRlbS5maW5hbFVybF0uZmlsdGVyKEJvb2xlYW4pOwogIGNvbnN0IGlzSW1hZ2UgPSAvXmltYWdlXC8oPzpwbmd8anBlZ3x3ZWJwKSQvaS50ZXN0KGl0ZW0ubWltZSB8fCAiIikKICAgIHx8IC9cLihwbmd8anBlP2d8d2VicCkkL2kudGVzdChpdGVtLmZpbGVuYW1lIHx8ICIiKTsKICBjb25zdCBmcm9tRmxvdyA9IGl0ZW0ucmVmZXJyZXIgPT09IGVudHJ5LnBhZ2UKICAgIHx8IHVybHMuc29tZSh1cmwgPT4gL15ibG9iOmh0dHBzOlwvXC9mbG93XC5nb29nbGVcLmNvbVwvL2kudGVzdCh1cmwpKQogICAgfHwgKGlzSW1hZ2UgJiYgIWl0ZW0ucmVmZXJyZXIgJiYgdXJscy5zb21lKHVybCA9PgogICAgICAvXmh0dHBzOlwvXC8oPzpbYS16MC05LV0rXC4pKig/Omdvb2dsZXVzZXJjb250ZW50XC5jb218Z3N0YXRpY1wuY29tKVwvL2kudGVzdCh1cmwpKSk7CiAgY29uc3QgbXVzZSA9IGVudHJ5LmZpbGVuYW1lICE9PSB1bmRlZmluZWQ7CiAgY29uc3QgZnJvbU11c2UgPSBtdXNlICYmIGlzSW1hZ2UgJiYKICAgIChpdGVtLmZpbGVuYW1lIHx8ICIiKS5zcGxpdCgvW1xcL10vKS5wb3AoKS50b0xvd2VyQ2FzZSgpID09PSBlbnRyeS5maWxlbmFtZS50b0xvd2VyQ2FzZSgpICYmCiAgICAoaXRlbS5yZWZlcnJlciA9PT0gZW50cnkucGFnZSB8fCB1cmxzLnNvbWUodXJsID0+IHVybC5zdGFydHNXaXRoKCJibG9iOmh0dHBzOi8vbXVzZS5haS8iKSB8fCB1cmwuc3RhcnRzV2l0aCgiaHR0cHM6Ly9tdXNlLmFpLyIpKSk7CiAgaWYgKG11c2UgPyAhZnJvbU11c2UgOiAhZnJvbUZsb3cpIHJldHVybjsKICBjb25zdCBleHRlbnNpb24gPSAvXC4ocG5nfGpwZT9nfHdlYnApJC9pLmV4ZWMoaXRlbS5maWxlbmFtZSB8fCAiIik/LlsxXSB8fCAicG5nIjsKICBlbnRyeS5pZCA9IGl0ZW0uaWQ7CiAgc3VnZ2VzdCh7ZmlsZW5hbWU6KG11c2UgPyAiVmVUYXktTXVzZS8iIDogIlZlVGF5LTJLLyIpICsgIiIgKyBlbnRyeS5zdGVtICsgIi4iICsgZXh0ZW5zaW9uLCBjb25mbGljdEFjdGlvbjoib3ZlcndyaXRlIn0pOwp9KTsKY2hyb21lLnJ1bnRpbWUub25NZXNzYWdlLmFkZExpc3RlbmVyKChtLHNlbmRlcixyZXBseSkgPT4gewogIChhc3luYygpPT57CiAgICBjb25zdCBtdXNlID0gc2VuZGVyLmZyYW1lSWQgPT09IDAgJiYgc2VuZGVyLnRhYj8udXJsPy5zdGFydHNXaXRoKCJodHRwczovL211c2UuYWkvIik7CiAgICBpZiAoIW11c2UgJiYgIWZsb3dUYWIoc2VuZGVyLnRhYj8udXJsKSkgdGhyb3cgRXJyb3IoIlRhYiBraMO0bmcgdGh14buZYyBHb29nbGUgRmxvdyBob+G6t2MgTXVzZS4iKTsKICAgIGNvbnN0IGtleSA9ICJwYWlyaW5nOiIgKyBzZW5kZXIudGFiLmlkOwogICAgaWYgKG0ub3AgPT09ICJwYWlyIikgewogICAgICBpZiAoIS9eW2EtZjAtOV17MzJ9JC8udGVzdChtLnRva2VuIHx8ICIiKSkgdGhyb3cgRXJyb3IoIk3DoyBr4bq/dCBu4buRaSBraMO0bmcgaOG7o3AgbOG7hy4iKTsKICAgICAgY29uc3QgcG9ydCA9IG0ucG9ydCA/PyA3ODc3OwogICAgICBpZiAoIU51bWJlci5pc0ludGVnZXIocG9ydCkgfHwgcG9ydCA8IDEwMjQgfHwgcG9ydCA+IDY1NTM1KSB0aHJvdyBFcnJvcigiQ+G7lW5nIFbhur0gVGF5IGtow7RuZyBo4bujcCBs4buHLiIpOwogICAgICBjb25zdCBjb25uZWN0aW9uID0ge3Rva2VuOm0udG9rZW4sIHBvcnR9OwogICAgICBjb25zdCBqb2IgPSBhd2FpdCBhcGkoY29ubmVjdGlvbiwgIi9uZXh0Iik7CiAgICAgIGlmIChqb2IuZG9uZSkgdGhyb3cgRXJyb3IoIlbhur0gVGF5IGNoxrBhIGPDsyBj4bqjbmggY2jhu50uIik7CiAgICAgIGlmIChtdXNlICE9PSAoam9iLnByb3ZpZGVyID09PSAibXVzZSIpKSB0aHJvdyBFcnJvcigiTmd14buTbiDhuqNuaCBraMO0bmcga2jhu5twIHTDoWMgduG7pS4iKTsKICAgICAgYXdhaXQgY2hyb21lLnN0b3JhZ2Uuc2Vzc2lvbi5zZXQoe1trZXldOmNvbm5lY3Rpb259KTsKICAgICAgcmV0dXJuIGpvYjsKICAgIH0KICAgIGNvbnN0IHNhdmVkID0gYXdhaXQgY2hyb21lLnN0b3JhZ2Uuc2Vzc2lvbi5nZXQoa2V5KTsKICAgIGNvbnN0IHBhaXJpbmcgPSB0eXBlb2Ygc2F2ZWRba2V5XSA9PT0gInN0cmluZyIgPyB7dG9rZW46c2F2ZWRba2V5XSxwb3J0Ojc4Nzd9IDogc2F2ZWRba2V5XTsKICAgIGlmIChtLm9wID09PSAicmVzdW1lIikgcmV0dXJuIHBhaXJpbmcgPyBhcGkocGFpcmluZywiL25leHQiKSA6IHtkb25lOnRydWV9OwogICAgaWYgKCFwYWlyaW5nKSB0aHJvdyBFcnJvcigiVGFiIGNoxrBhIMSRxrDhu6NjIFbhur0gVGF5IGvhur90IG7hu5FpLiIpOwogICAgaWYgKG11c2UgJiYgIVsibmV4dCIsImJhdGNoIiwicHJvZ3Jlc3MiLCJlcnJvciIsImFybU11c2UiLCJ0cmFuc2Zlck11c2UiLCJza2lwTXVzZSJdLmluY2x1ZGVzKG0ub3ApKQogICAgICB0aHJvdyBFcnJvcigiVGhhbyB0w6FjIGtow7RuZyBo4bujcCBs4buHIHRyb25nIHRhYiBNdXNlLiIpOwogICAgaWYgKG0ub3AgPT09ICJza2lwTXVzZSIpIHsKICAgICAgY29uc3Qgam9iID0gYXdhaXQgYXBpKHBhaXJpbmcsICIvbmV4dCIpOwogICAgICBpZiAoIW11c2UgfHwgam9iLnByb3ZpZGVyICE9PSAibXVzZSIgfHwgam9iLmpvYiAhPT0gbS5qb2IgfHwgam9iLmluZGV4ICE9PSBtLmluZGV4KQogICAgICAgIHRocm93IEVycm9yKCJD4bqjbmggTXVzZSBj4bqnbiBi4buPIHF1YSBraMO0bmcga2jhu5twLiIpOwogICAgICBwZW5kaW5nRG93bmxvYWRzLmRlbGV0ZShrZXkpOwogICAgICByZXR1cm4gYXBpKHBhaXJpbmcsICIvc2tpcE11c2UvIiArIGpvYi5qb2IgKyAiLyIgKyBqb2IuaW5kZXgsICJ7fSIpOwogICAgfQogICAgaWYgKG0ub3AgPT09ICJhcm1NdXNlIiB8fCBtLm9wID09PSAidHJhbnNmZXJNdXNlIikgewogICAgICBjb25zdCBqb2IgPSBhd2FpdCBhcGkocGFpcmluZywgIi9uZXh0Iik7CiAgICAgIGlmICghbXVzZSB8fCBqb2IucHJvdmlkZXIgIT09ICJtdXNlIiB8fCBqb2Iuam9iICE9PSBtLmpvYiB8fCBqb2IuaW5kZXggIT09IG0uaW5kZXgpCiAgICAgICAgdGhyb3cgRXJyb3IoIlTDoWMgduG7pSDhuqNuaCBNdXNlIMSRw6MgxJHhu5VpLiIpOwogICAgICBpZiAobS5vcCA9PT0gImFybU11c2UiKSB7CiAgICAgICAgY29uc3QgcGFnZSA9IG5ldyBVUkwoc2VuZGVyLnRhYi51cmwpOwogICAgICAgIGNvbnN0IGZpbGVuYW1lID0gcGFnZS5zZWFyY2hQYXJhbXMuZ2V0KCJmaWxlIik/LnNwbGl0KCIvIikucG9wKCk7CiAgICAgICAgY29uc3QgdGFnID0gInZldGF5IiArIGpvYi5qb2Iuc2xpY2UoMCwgMTIpICsgImMiICsgU3RyaW5nKGpvYi5pbmRleCArIDEpLnBhZFN0YXJ0KDUsICIwIik7CiAgICAgICAgaWYgKHBhZ2UucGF0aG5hbWUgIT09ICIvbGlicmFyeS9tZWRpYSIgfHwgZmlsZW5hbWUgIT09IG0uZmlsZW5hbWUgfHwKICAgICAgICAgICAgIWZpbGVuYW1lPy50b0xvd2VyQ2FzZSgpLmluY2x1ZGVzKHRhZykgfHwgIS9cLihwbmd8anBlP2d8d2VicCkkL2kudGVzdChmaWxlbmFtZSkpCiAgICAgICAgICB0aHJvdyBFcnJvcigi4bqibmggxJFhbmcgbeG7nyBraMO0bmcga2jhu5twIG3DoyBj4bqjbmggTXVzZS4iKTsKICAgICAgICBwZW5kaW5nRG93bmxvYWRzLnNldChrZXksIHtzdGVtOiBqb2Iuam9iICsgIi0iICsgam9iLmluZGV4LCBmaWxlbmFtZSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBwYWdlOiBzZW5kZXIudGFiLnVybCwgZGVhZGxpbmU6IERhdGUubm93KCkgKyA5MDAwMH0pOwogICAgICAgIHJldHVybiB7YXJtZWQ6dHJ1ZX07CiAgICAgIH0KICAgICAgY29uc3QgZW50cnkgPSBwZW5kaW5nRG93bmxvYWRzLmdldChrZXkpOwogICAgICBpZiAoIWVudHJ5Py5pZCkgcmV0dXJuIHtyZWNlaXZlZDpmYWxzZX07CiAgICAgIGNvbnN0IFtpdGVtXSA9IGF3YWl0IGNocm9tZS5kb3dubG9hZHMuc2VhcmNoKHtpZDplbnRyeS5pZH0pOwogICAgICBpZiAoaXRlbT8uc3RhdGUgPT09ICJpbnRlcnJ1cHRlZCIpIHRocm93IEVycm9yKCJDaHJvbWUgxJHDoyBuZ+G6r3QgbMaw4bujdCB04bqjaSDhuqNuaCBNdXNlLiIpOwogICAgICBpZiAoaXRlbT8uc3RhdGUgIT09ICJjb21wbGV0ZSIpIHJldHVybiB7cmVjZWl2ZWQ6ZmFsc2V9OwogICAgICBjb25zdCByZXN1bHQgPSBhd2FpdCBhcGkocGFpcmluZywgIi9kb3dubG9hZE11c2UvIiArIGpvYi5qb2IgKyAiLyIgKyBqb2IuaW5kZXgsCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBKU09OLnN0cmluZ2lmeSh7cGF0aDppdGVtLmZpbGVuYW1lfSkpOwogICAgICBwZW5kaW5nRG93bmxvYWRzLmRlbGV0ZShrZXkpOwogICAgICByZXR1cm4gcmVzdWx0OwogICAgfQogICAgaWYgKG0ub3AgPT09ICJhcm0yayIgfHwgbS5vcCA9PT0gInRyYW5zZmVyMmsiKSB7CiAgICAgIGNvbnN0IGpvYiA9IGF3YWl0IGFwaShwYWlyaW5nLCIvbmV4dCIpOwogICAgICBpZiAoam9iLmRvbmUgfHwgam9iLmpvYiAhPT0gbS5qb2IgfHwgam9iLmluZGV4ICE9PSBtLmluZGV4KSB0aHJvdyBFcnJvcigiVMOhYyB24bulIOG6o25oIDJLIMSRw6MgxJHhu5VpLiIpOwogICAgICBpZiAobS5vcCA9PT0gImFybTJrIikgewogICAgICAgIHBlbmRpbmdEb3dubG9hZHMuc2V0KGtleSx7c3RlbTpqb2Iuam9iICsgIi0iICsgam9iLmluZGV4LCBkZWFkbGluZTpEYXRlLm5vdygpKzE1MDAwMCxwYWdlOnNlbmRlci50YWIudXJsfSk7CiAgICAgICAgcmV0dXJuIHthcm1lZDp0cnVlfTsKICAgICAgfQogICAgICBjb25zdCBlbnRyeSA9IHBlbmRpbmdEb3dubG9hZHMuZ2V0KGtleSk7CiAgICAgIGlmICghZW50cnk/LmlkKSByZXR1cm4ge3JlY2VpdmVkOmZhbHNlfTsKICAgICAgY29uc3QgW2l0ZW1dID0gYXdhaXQgY2hyb21lLmRvd25sb2Fkcy5zZWFyY2goe2lkOmVudHJ5LmlkfSk7CiAgICAgIGlmIChpdGVtPy5zdGF0ZSA9PT0gImludGVycnVwdGVkIikgdGhyb3cgRXJyb3IoIkNocm9tZSDEkcOjIG5n4bqvdCBsxrDhu6N0IHThuqNpIOG6o25oIDJLLiIpOwogICAgICBpZiAoaXRlbT8uc3RhdGUgIT09ICJjb21wbGV0ZSIpIHJldHVybiB7cmVjZWl2ZWQ6ZmFsc2V9OwogICAgICBjb25zdCByZXN1bHQgPSBhd2FpdCBhcGkocGFpcmluZywiL2Rvd25sb2FkMmsvIiArIGpvYi5qb2IgKyAiLyIgKyBqb2IuaW5kZXgsSlNPTi5zdHJpbmdpZnkoe3BhdGg6aXRlbS5maWxlbmFtZX0pKTsKICAgICAgcGVuZGluZ0Rvd25sb2Fkcy5kZWxldGUoa2V5KTsKICAgICAgcmV0dXJuIHJlc3VsdDsKICAgIH0KICAgIGlmIChtLm9wID09PSAic3VibWl0IikgewogICAgICBjb25zdCB0YWIgPSBhd2FpdCBjaHJvbWUudGFicy5nZXQoc2VuZGVyLnRhYi5pZCk7CiAgICAgIGNvbnN0IHBhZ2UgPSBuZXcgVVJMKHRhYi51cmwpOwogICAgICBpZiAoc2VuZGVyLmZyYW1lSWQgIT09IDAgfHwgcGFnZS5vcmlnaW4gIT09ICJodHRwczovL2Zsb3cuZ29vZ2xlLmNvbSIKICAgICAgICB8fCAhL15cL3Byb2plY3RcL1teL10rKD86XC8uKik/JC8udGVzdChwYWdlLnBhdGhuYW1lKSkKICAgICAgICB0aHJvdyBFcnJvcigiTOG7h25oIGLhuqVtIGtow7RuZyDEkeG6v24gdOG7qyBtw6BuIGThu7Egw6FuIGhv4bq3YyBtw6BuIHhlbSDhuqNuaCBGbG93LiIpOwogICAgICBpZiAoIVttLngsbS55XS5ldmVyeShOdW1iZXIuaXNGaW5pdGUpIHx8IG0ueCA8IDAgfHwgbS55IDwgMCB8fCBtLnggPiAxMDAwMCB8fCBtLnkgPiAxMDAwMCkKICAgICAgICB0aHJvdyBFcnJvcigiTsO6dCBGbG93IG7hurFtIG5nb8OgaSB2w7luZyBjw7MgdGjhu4MgYuG6pW0uIik7CiAgICAgIGNvbnN0IGpvYiA9IGF3YWl0IGFwaShwYWlyaW5nLCIvbmV4dCIpOwogICAgICBpZiAoam9iLmRvbmUgfHwgam9iLmpvYiAhPT0gbS5qb2IpIHRocm93IEVycm9yKCJUw6FjIHbhu6UgVuG6vSBUYXkgxJHDoyDEkeG7lWkuIik7CiAgICAgIGNvbnN0IHRhcmdldCA9IHt0YWJJZDp0YWIuaWR9OwogICAgICBhd2FpdCBjaHJvbWUuZGVidWdnZXIuYXR0YWNoKHRhcmdldCwiMS4zIik7CiAgICAgIHRyeSB7CiAgICAgICAgY29uc3QgaW5wdXQgPSAodHlwZSxidXR0b25zKSA9PiBjaHJvbWUuZGVidWdnZXIuc2VuZENvbW1hbmQodGFyZ2V0LCJJbnB1dC5kaXNwYXRjaE1vdXNlRXZlbnQiLAogICAgICAgICAge3R5cGUseDptLngseTptLnksYnV0dG9uOiJsZWZ0IixidXR0b25zLGNsaWNrQ291bnQ6MX0pOwogICAgICAgIGF3YWl0IGlucHV0KCJtb3VzZVByZXNzZWQiLDEpOwogICAgICAgIGF3YWl0IGlucHV0KCJtb3VzZVJlbGVhc2VkIiwwKTsKICAgICAgICBpZiAobS5jbGlja3MgPT09IDIpIHsKICAgICAgICAgIGNvbnN0IHNlY29uZCA9ICh0eXBlLGJ1dHRvbnMpID0+IGNocm9tZS5kZWJ1Z2dlci5zZW5kQ29tbWFuZCh0YXJnZXQsIklucHV0LmRpc3BhdGNoTW91c2VFdmVudCIsCiAgICAgICAgICAgIHt0eXBlLHg6bS54LHk6bS55LGJ1dHRvbjoibGVmdCIsYnV0dG9ucyxjbGlja0NvdW50OjJ9KTsKICAgICAgICAgIGF3YWl0IHNlY29uZCgibW91c2VQcmVzc2VkIiwxKTsKICAgICAgICAgIGF3YWl0IHNlY29uZCgibW91c2VSZWxlYXNlZCIsMCk7CiAgICAgICAgfQogICAgICB9IGZpbmFsbHkgewogICAgICAgIGF3YWl0IGNocm9tZS5kZWJ1Z2dlci5kZXRhY2godGFyZ2V0KTsKICAgICAgfQogICAgICByZXR1cm4ge2NsaWNrZWQ6dHJ1ZX07CiAgICB9CiAgICBpZiAobS5vcCA9PT0gImFjdGl2YXRlIikgewogICAgICBhd2FpdCBjaHJvbWUudGFicy51cGRhdGUoc2VuZGVyLnRhYi5pZCwge2FjdGl2ZTp0cnVlfSk7CiAgICAgIGF3YWl0IGNocm9tZS53aW5kb3dzLnVwZGF0ZShzZW5kZXIudGFiLndpbmRvd0lkLCB7Zm9jdXNlZDp0cnVlfSk7CiAgICAgIHJldHVybiB7YWN0aXZlOnRydWV9OwogICAgfQogICAgaWYgKG0ub3AgPT09ICJuZXh0IikgcmV0dXJuIGFwaShwYWlyaW5nLCIvbmV4dCIpOwogICAgaWYgKG0ub3AgPT09ICJiYXRjaCIpIHJldHVybiBhcGkocGFpcmluZywiL2JhdGNoIik7CiAgICBpZiAobS5vcCA9PT0gInByb2dyZXNzIikgcmV0dXJuIGFwaShwYWlyaW5nLCIvcHJvZ3Jlc3MiLEpTT04uc3RyaW5naWZ5KHtjb3VudDptLmNvdW50LHN0YWdlOm0uc3RhZ2V9KSk7CiAgICBpZiAobS5vcCA9PT0gImVycm9yIikgcmV0dXJuIGFwaShwYWlyaW5nLCIvZXJyb3IiLEpTT04uc3RyaW5naWZ5KHttZXNzYWdlOm0ubWVzc2FnZX0pKTsKICAgIGlmIChtLm9wID09PSAiaW1hZ2UiICYmIC9eW2EtZjAtOV17MzJ9JC8udGVzdChtLmpvYikgJiYgTnVtYmVyLmlzSW50ZWdlcihtLmluZGV4KSkgewogICAgICBsZXQgaW1hZ2U7CiAgICAgIGlmIChtLmRhdGEgJiYgL15kYXRhOmltYWdlXC8ocG5nfHdlYnB8anBlZyk7YmFzZTY0LC8udGVzdChtLmRhdGEpKSB7CiAgICAgICAgaW1hZ2UgPSBhd2FpdCAoYXdhaXQgZmV0Y2gobS5kYXRhKSkuYmxvYigpOwogICAgICB9IGVsc2UgewogICAgICAgIGNvbnN0IHUgPSBuZXcgVVJMKG0udXJsKTsKICAgICAgICBpZiAodS5wcm90b2NvbCAhPT0gImh0dHBzOiIgfHwgIS8oXnxcLilnb29nbGV1c2VyY29udGVudFwuY29tJC8udGVzdCh1Lmhvc3RuYW1lKSkgdGhyb3cgRXJyb3IoIktow7RuZyB4w6FjIMSR4buLbmggxJHGsOG7o2MgxJHhu4thIGNo4buJIOG6o25oIGvhur90IHF14bqjLiIpOwogICAgICAgIGNvbnN0IHIgPSBhd2FpdCBmZXRjaCh1LmhyZWYse2NyZWRlbnRpYWxzOiJvbWl0IixyZWRpcmVjdDoiZXJyb3IifSk7CiAgICAgICAgaWYgKCFyLm9rKSB0aHJvdyBFcnJvcigiS2jDtG5nIHThuqNpIMSRxrDhu6NjIOG6o25oIGvhur90IHF14bqjLiIpOwogICAgICAgIGltYWdlID0gYXdhaXQgci5ibG9iKCk7CiAgICAgIH0KICAgICAgaWYgKGltYWdlLnNpemUgPiAyNCoxMDI0KjEwMjQgfHwgIWltYWdlLnR5cGUuc3RhcnRzV2l0aCgiaW1hZ2UvIikpIHRocm93IEVycm9yKCLhuqJuaCBr4bq/dCBxdeG6oyBraMO0bmcgaOG7o3AgbOG7hy4iKTsKICAgICAgcmV0dXJuIGFwaShwYWlyaW5nLCIvaW1hZ2UvIittLmpvYisiLyIrbS5pbmRleCxpbWFnZSk7CiAgICB9CiAgICB0aHJvdyBFcnJvcigiWcOqdSBj4bqndSBraMO0bmcgaOG7o3AgbOG7hy4iKTsKICB9KSgpLnRoZW4ocmVzdWx0PT5yZXBseSh7b2s6dHJ1ZSxyZXN1bHR9KSxlcnJvcj0+cmVwbHkoe29rOmZhbHNlLGVycm9yOmVycm9yLm1lc3NhZ2V9KSk7CiAgcmV0dXJuIHRydWU7Cn0pOwpjaHJvbWUudGFicy5vblJlbW92ZWQuYWRkTGlzdGVuZXIodGFiSWQgPT4gY2hyb21lLnN0b3JhZ2Uuc2Vzc2lvbi5yZW1vdmUoInBhaXJpbmc6IiArIHRhYklkKSk7Cgpjb25zdCBleHRlbnNpb25DaGVjayA9ICJ2ZXRheS1jaGVjay1sb2NhbC11cGRhdGUiOwpmdW5jdGlvbiByZWdpc3RlckNoZWNrKCkgewogIGNocm9tZS5hbGFybXMuY3JlYXRlKGV4dGVuc2lvbkNoZWNrLCB7cGVyaW9kSW5NaW51dGVzOjF9KTsKfQpjaHJvbWUucnVudGltZS5vbkluc3RhbGxlZC5hZGRMaXN0ZW5lcihyZWdpc3RlckNoZWNrKTsKY2hyb21lLnJ1bnRpbWUub25TdGFydHVwLmFkZExpc3RlbmVyKHJlZ2lzdGVyQ2hlY2spOwpjaHJvbWUuYWxhcm1zLm9uQWxhcm0uYWRkTGlzdGVuZXIoYXN5bmMgYWxhcm0gPT4gewogIGlmIChhbGFybS5uYW1lICE9PSBleHRlbnNpb25DaGVjaykgcmV0dXJuOwogIHRyeSB7CiAgICBjb25zdCBjdXJyZW50ID0gY2hyb21lLnJ1bnRpbWUuZ2V0TWFuaWZlc3QoKTsKICAgIGNvbnN0IHJlc3BvbnNlID0gYXdhaXQgZmV0Y2goY2hyb21lLnJ1bnRpbWUuZ2V0VVJMKCJtYW5pZmVzdC5qc29uIikgKyAiP3Q9IiArIERhdGUubm93KCksIHtjYWNoZToibm8tc3RvcmUifSk7CiAgICBpZiAoIXJlc3BvbnNlLm9rKSByZXR1cm47CiAgICBjb25zdCBkaXNrID0gYXdhaXQgcmVzcG9uc2UuanNvbigpOwogICAgaWYgKCEvXlxkK1wuXGQrXC5cZCskLy50ZXN0KGRpc2sudmVyc2lvbikgfHwgZGlzay5uYW1lICE9PSBjdXJyZW50Lm5hbWUgfHwKICAgICAgICBkaXNrLm1hbmlmZXN0X3ZlcnNpb24gIT09IGN1cnJlbnQubWFuaWZlc3RfdmVyc2lvbikgcmV0dXJuOwogICAgY29uc3Qgb2xkID0gY3VycmVudC52ZXJzaW9uLnNwbGl0KCIuIikubWFwKE51bWJlcik7CiAgICBjb25zdCBuZXh0ID0gZGlzay52ZXJzaW9uLnNwbGl0KCIuIikubWFwKE51bWJlcik7CiAgICBjb25zdCBjaGFuZ2VkID0gbmV4dC5maW5kSW5kZXgoKHZhbHVlLGkpPT52YWx1ZSE9PW9sZFtpXSk7CiAgICBpZiAoY2hhbmdlZCA8IDAgfHwgbmV4dFtjaGFuZ2VkXSA8IG9sZFtjaGFuZ2VkXSkgcmV0dXJuOwogICAgZm9yKGNvbnN0IGtleSBvZiBbInBlcm1pc3Npb25zIiwiaG9zdF9wZXJtaXNzaW9ucyIsIm9wdGlvbmFsX3Blcm1pc3Npb25zIiwib3B0aW9uYWxfaG9zdF9wZXJtaXNzaW9ucyJdKSB7CiAgICAgIGlmKEpTT04uc3RyaW5naWZ5KFsuLi4oY3VycmVudFtrZXldfHxbXSldLnNvcnQoKSkhPT1KU09OLnN0cmluZ2lmeShbLi4uKGRpc2tba2V5XXx8W10pXS5zb3J0KCkpKSByZXR1cm47CiAgICB9CiAgICBjb25zdCBtYXRjaGVzID0gdmFsdWUgPT4gKHZhbHVlLmNvbnRlbnRfc2NyaXB0cyB8fCBbXSkuZmxhdE1hcChzY3JpcHQ9PnNjcmlwdC5tYXRjaGVzIHx8IFtdKS5zb3J0KCk7CiAgICBpZihKU09OLnN0cmluZ2lmeShtYXRjaGVzKGN1cnJlbnQpKSE9PUpTT04uc3RyaW5naWZ5KG1hdGNoZXMoZGlzaykpKSByZXR1cm47CiAgICBjb25zdCBpZGxlID0gYXdhaXQgZmV0Y2goYmFzZSsiL2V4dGVuc2lvbi1pZGxlIix7Y2FjaGU6Im5vLXN0b3JlIn0pOwogICAgaWYoaWRsZS5vayAmJiAoYXdhaXQgaWRsZS5qc29uKCkpLmlkbGUgPT09IHRydWUpIGNocm9tZS5ydW50aW1lLnJlbG9hZCgpOwogIH0gY2F0Y2gge30KfSk7Cg==",
    "flow.js": "aWYgKGxvY2F0aW9uLm9yaWdpbiAhPT0gImh0dHBzOi8vbXVzZS5haSIpIHsKKCgpID0+IHsKICBjb25zdCBwYWlyaW5nID0gL14jdmV0YXk9KFthLWYwLTldezMyfSkoPzotKFswLTldezQsNX0pKT8kLy5leGVjKGxvY2F0aW9uLmhhc2gpOwogIGlmICh3aW5kb3cuX192ZVRheVJ1bm5pbmcpIHJldHVybjsKICB3aW5kb3cuX192ZVRheVJ1bm5pbmcgPSB0cnVlOwogIGNvbnN0IHNsZWVwID0gKG1zKSA9PiBuZXcgUHJvbWlzZShyZXNvbHZlID0+IHNldFRpbWVvdXQocmVzb2x2ZSxtcykpOwogIGNvbnN0IHZpc2libGUgPSBub2RlID0+IG5vZGUuZ2V0Q2xpZW50UmVjdHMoKS5sZW5ndGggJiYgZ2V0Q29tcHV0ZWRTdHlsZShub2RlKS52aXNpYmlsaXR5ICE9PSAiaGlkZGVuIjsKICBjb25zdCBzZW5kID0gYXN5bmMgKG1lc3NhZ2UpID0+IHsKICAgIGNvbnN0IHJlc3BvbnNlID0gYXdhaXQgY2hyb21lLnJ1bnRpbWUuc2VuZE1lc3NhZ2UobWVzc2FnZSk7CiAgICBpZiAoIXJlc3BvbnNlPy5vaykgdGhyb3cgRXJyb3IocmVzcG9uc2U/LmVycm9yIHx8ICJN4bqldCBr4bq/dCBu4buRaSB24bubaSBW4bq9IFRheS4iKTsKICAgIHJldHVybiByZXNwb25zZS5yZXN1bHQ7CiAgfTsKICBmdW5jdGlvbiByZXN1bHRJbWFnZXMoY2FyZCkgewogICAgcmV0dXJuIFsuLi5jYXJkLnF1ZXJ5U2VsZWN0b3JBbGwoImltZyIpXS5maWx0ZXIoaW1nID0+IHsKICAgICAgaWYgKCFpbWcuY29tcGxldGUgfHwgIWltZy5uYXR1cmFsV2lkdGggfHwgIWltZy5uYXR1cmFsSGVpZ2h0IHx8ICF2aXNpYmxlKGltZykpIHJldHVybiBmYWxzZTsKICAgICAgY29uc3Qgc3JjID0gaW1nLmN1cnJlbnRTcmMgfHwgaW1nLnNyYzsKICAgICAgY29uc3QgdXJsID0gbmV3IFVSTChzcmMsIGxvY2F0aW9uLmhyZWYpOwogICAgICBjb25zdCBtZWRpYSA9IC9eKMOUIGhp4buDbiB0aOG7iyBow6xuaCDhuqNuaCBj4bunYSBuZ8aw4budaSBkw7luZ3xVc2VyIGltYWdlIHRpbGUpJC9pdS50ZXN0KGltZy5hbHQudHJpbSgpKTsKICAgICAgY29uc3Qgb3JpZ2luYWwgPSAvXihibG9iOnxodHRwczpcL1wvW14vXSpcLmdvb2dsZXVzZXJjb250ZW50XC5jb21cLykvaS50ZXN0KHNyYyk7CiAgICAgIHJldHVybiBvcmlnaW5hbCB8fCAobWVkaWEgJiYgWyJodHRwczoiLCAiZGF0YToiLCAiYmxvYjoiXS5pbmNsdWRlcyh1cmwucHJvdG9jb2wpKTsKICAgIH0pOwogIH0KICBjb25zdCBjYXB0aW9uID0gYnV0dG9uID0+IChidXR0b24uZ2V0QXR0cmlidXRlKCJhcmlhLWxhYmVsIikgfHwgYnV0dG9uLmdldEF0dHJpYnV0ZSgidGl0bGUiKSB8fCBidXR0b24udGV4dENvbnRlbnQgfHwgIiIpLnRyaW0oKTsKICBmdW5jdGlvbiBwcm9tcHRGaWVsZCgpIHsKICAgIGNvbnN0IGZpZWxkcyA9IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCd0ZXh0YXJlYSxbY29udGVudGVkaXRhYmxlPSJ0cnVlIl0sW2NvbnRlbnRlZGl0YWJsZT0icGxhaW50ZXh0LW9ubHkiXScpXQogICAgICAuZmlsdGVyKGZpZWxkID0+IHZpc2libGUoZmllbGQpICYmICFmaWVsZC5jbG9zZXN0KCdbaW5lcnRdLFthcmlhLWhpZGRlbj0idHJ1ZSJdLFtyb2xlPSJzZWFyY2giXScpCiAgICAgICAgJiYgIWZpZWxkLmRpc2FibGVkICYmICFmaWVsZC5yZWFkT25seSAmJiBmaWVsZC5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSAhPT0gInRydWUiKTsKICAgIGNvbnN0IGxhYmVscyA9IGZpZWxkID0+IFsiYXJpYS1sYWJlbCIsICJwbGFjZWhvbGRlciIsICJkYXRhLXBsYWNlaG9sZGVyIiwgInRpdGxlIl0KICAgICAgLm1hcChuYW1lID0+IGZpZWxkLmdldEF0dHJpYnV0ZShuYW1lKSB8fCAiIikuam9pbigiICIpOwogICAgY29uc3QgZWxpZ2libGUgPSBmaWVsZHMuZmlsdGVyKGZpZWxkID0+ICEvc2VhcmNofHTDrG0ga2nhur9tfHJlbmFtZXzEkeG7lWkgdMOqbnxwcm9qZWN0IG5hbWV8dMOqbiBk4buxIMOhbi9pLnRlc3QobGFiZWxzKGZpZWxkKSkKICAgICAgJiYgIWZpZWxkcy5zb21lKHBhcmVudCA9PiBwYXJlbnQgIT09IGZpZWxkICYmIHBhcmVudC5jb250YWlucyhmaWVsZCkpKTsKICAgIGNvbnN0IG1hdGNoZXMgPSBlbGlnaWJsZS5maWx0ZXIoZmllbGQgPT4gL3Byb21wdHx3aGF0ICg/OmRvfHdvdWxkKSB5b3V8ZGVzY3JpYmV8bcO0IHThuqN8YuG6oW4gbXXhu5FufHThuqFvIG7hu5lpIGR1bmcvaS50ZXN0KGxhYmVscyhmaWVsZCkpKTsKICAgIGlmIChtYXRjaGVzLmxlbmd0aCA9PT0gMSkgcmV0dXJuIG1hdGNoZXNbMF07CiAgICByZXR1cm4gZWxpZ2libGUubGVuZ3RoID09PSAxID8gZWxpZ2libGVbMF0gOiBudWxsOwogIH0KICBjb25zdCByYXRpb0xhYmVsID0gcmF0aW8gPT4gbmV3IFJlZ0V4cCgiKD86XnxbXjAtOV0pIiArIHJhdGlvLnJlcGxhY2UoIjoiLCAiXFxzKls64oi2XVxccyoiKSArICIoPyFbMC05XSkiLCAidSIpOwogIGFzeW5jIGZ1bmN0aW9uIHNlbGVjdFJhdGlvKHJhdGlvKSB7CiAgICBjb25zdCBzZWxlY3RlZCA9IG5vZGUgPT4gWyJhcmlhLWNoZWNrZWQiLCAiYXJpYS1zZWxlY3RlZCIsICJhcmlhLXByZXNzZWQiXS5zb21lKGtleSA9PiBub2RlLmdldEF0dHJpYnV0ZShrZXkpID09PSAidHJ1ZSIpCiAgICAgIHx8IG5vZGUuZ2V0QXR0cmlidXRlKCJkYXRhLXN0YXRlIikgPT09ICJjaGVja2VkIiB8fCBub2RlLmdldEF0dHJpYnV0ZSgiZGF0YS1zdGF0ZSIpID09PSAiYWN0aXZlIjsKICAgIGNvbnN0IGNvbnRyb2xzID0gKCkgPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoImJ1dHRvbixbcm9sZT1idXR0b25dLFtyb2xlPXJhZGlvXSxbcm9sZT1vcHRpb25dLFtyb2xlPW1lbnVpdGVtcmFkaW9dLFtyb2xlPXRhYl0iKV0KICAgICAgLmZpbHRlcihub2RlID0+IHZpc2libGUobm9kZSkgJiYgIW5vZGUuZGlzYWJsZWQgJiYgbm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSAhPT0gInRydWUiKTsKICAgIGNvbnN0IGxhYmVscyA9IG5vZGUgPT4gW2NhcHRpb24obm9kZSksIG5vZGUuZ2V0QXR0cmlidXRlKCJkYXRhLXZhbHVlIikgfHwgIiIsIG5vZGUuaW5uZXJUZXh0IHx8ICIiXS5qb2luKCIgIik7CiAgICBjb25zdCBleGFjdCA9IHJhdGlvTGFiZWwocmF0aW8pOwogICAgY29uc3QgaWNvbk5hbWUgPSAiY3JvcF8iICsgcmF0aW8ucmVwbGFjZSgiOiIsICJfIik7CiAgICBjb25zdCBtYXRjaGVzUmF0aW8gPSBub2RlID0+IGV4YWN0LnRlc3QobGFiZWxzKG5vZGUpKSB8fCBbLi4ubm9kZS5xdWVyeVNlbGVjdG9yQWxsKCJtYXQtaWNvbixbcm9sZT1pbWddLHN2ZyIpXQogICAgICAuc29tZShpY29uID0+IFtpY29uLnRleHRDb250ZW50LCBpY29uLmdldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIpLCBpY29uLmdldEF0dHJpYnV0ZSgiZGF0YS1pY29uIildCiAgICAgICAgLnNvbWUobGFiZWwgPT4gbGFiZWw/LnRyaW0oKSA9PT0gaWNvbk5hbWUpKTsKICAgIGNvbnN0IGhhc1JhdGlvID0gcmF0aW9MYWJlbCgiWzAtOV0rOlswLTldKyIpOwogICAgY29uc3QgaXNPcHRpb24gPSBub2RlID0+IFsicmFkaW8iLCAib3B0aW9uIiwgIm1lbnVpdGVtcmFkaW8iXS5pbmNsdWRlcyhub2RlLmdldEF0dHJpYnV0ZSgicm9sZSIpKTsKICAgIGNvbnN0IHRyaWdnZXIgPSBjb250cm9scygpLmZpbHRlcihub2RlID0+ICFpc09wdGlvbihub2RlKSAmJiAobm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtaGFzcG9wdXAiKQogICAgICA/IC9hc3BlY3QgcmF0aW98dOG7tyBs4buHfHThu4kgbOG7hy9pdS50ZXN0KGxhYmVscyhub2RlKSkgfHwgaGFzUmF0aW8udGVzdChsYWJlbHMobm9kZSkpCiAgICAgIDogL14oPzphc3BlY3QgcmF0aW98dOG7tyBs4buHKD86IGtodW5nIGjDrG5oKT98dOG7iSBs4buHKD86IGtodW5nIGjDrG5oKT8pJC9pdS50ZXN0KGNhcHRpb24obm9kZSkpKSk7CiAgICBjb25zdCBvcHRpb25zID0gKCkgPT4gY29udHJvbHMoKS5maWx0ZXIobm9kZSA9PiAhdHJpZ2dlci5pbmNsdWRlcyhub2RlKSAmJiBub2RlICE9PSBzZXR0aW5ncyAmJiBtYXRjaGVzUmF0aW8obm9kZSkpOwogICAgY29uc3QgbW9kZWxTZXR0aW5ncyA9IGNvbnRyb2xzKCkuZmlsdGVyKG5vZGUgPT4gL05hbm8gQmFuYW5hfEltYWdlbi9pdS50ZXN0KGxhYmVscyhub2RlKSkgJiYgL1t4w5ddWzEtNF1cYi9pdS50ZXN0KGxhYmVscyhub2RlKSkpOwogICAgY29uc3Qgc2V0dGluZ3MgPSBjb250cm9scygpLmZpbmQobm9kZSA9PiAvXijEkGnhu4F1IGtp4buHbiBrw61jaCBob+G6oXQgY8OgaSDEkeG6t3R8U2V0dGluZ3MgdHJpZ2dlcnxHZW5lcmF0aW9uIHNldHRpbmdzKSQvaXUudGVzdChjYXB0aW9uKG5vZGUpKSkKICAgICAgfHwgKG1vZGVsU2V0dGluZ3MubGVuZ3RoID09PSAxID8gbW9kZWxTZXR0aW5nc1swXSA6IG51bGwpOwogICAgbGV0IG9wZW5lZCA9IGZhbHNlOwogICAgaWYgKCFvcHRpb25zKCkubGVuZ3RoICYmIHNldHRpbmdzKSB7CiAgICAgIHNldHRpbmdzLmNsaWNrKCk7CiAgICAgIG9wZW5lZCA9IHRydWU7CiAgICAgIGNvbnN0IGltYWdlID0gYXdhaXQgd2FpdEZvcigoKSA9PiBjb250cm9scygpLmZpbmQobm9kZSA9PiBub2RlLmdldEF0dHJpYnV0ZSgicm9sZSIpID09PSAicmFkaW8iICYmIC9eKEltYWdlfEjDrG5oIOG6o25ofOG6om5oKSQvaXUudGVzdChjYXB0aW9uKG5vZGUpKSksIDMpOwogICAgICBpZiAoaW1hZ2UgJiYgIXNlbGVjdGVkKGltYWdlKSkgaW1hZ2UuY2xpY2soKTsKICAgIH0KICAgIGlmICghb3B0aW9ucygpLmxlbmd0aCkgYXdhaXQgd2FpdEZvcigoKSA9PiBvcHRpb25zKCkubGVuZ3RoLCAzKTsKICAgIGlmICghb3B0aW9ucygpLmxlbmd0aCAmJiB0cmlnZ2VyLmxlbmd0aCA9PT0gMSkgdHJpZ2dlclswXS5jbGljaygpOwogICAgY29uc3Qgb3B0aW9uID0gYXdhaXQgd2FpdEZvcigoKSA9PiB7CiAgICAgIGlmICh0cmlnZ2VyLmxlbmd0aCA9PT0gMSAmJiBleGFjdC50ZXN0KGxhYmVscyh0cmlnZ2VyWzBdKSkgJiYgIW9wdGlvbnMoKS5sZW5ndGgpIHJldHVybiB0cmlnZ2VyWzBdOwogICAgICBjb25zdCBtYXRjaGVzID0gb3B0aW9ucygpOwogICAgICBjb25zdCByYWRpb3MgPSBtYXRjaGVzLmZpbHRlcihpc09wdGlvbik7CiAgICAgIHJldHVybiByYWRpb3MubGVuZ3RoID09PSAxID8gcmFkaW9zWzBdIDogbWF0Y2hlcy5sZW5ndGggPT09IDEgPyBtYXRjaGVzWzBdIDogbnVsbDsKICAgIH0sIDEyKTsKICAgIGlmICghb3B0aW9uKSB0aHJvdyBFcnJvcigiS2jDtG5nIGNo4buNbiDEkcaw4bujYyB04bu3IGzhu4cgIiArIHJhdGlvICsgIiB0cm9uZyBGbG93ICgiICsgKHNldHRpbmdzID8gIsSRw6MgdGjhuqV5IG7DunQgY8OgaSDEkeG6t3Qg4bqjbmgiIDogImNoxrBhIHRo4bqleSBuw7p0IGPDoGkgxJHhurd0IOG6o25oIikgKyAiKS4iKTsKICAgIGlmICghdHJpZ2dlci5pbmNsdWRlcyhvcHRpb24pICYmICFzZWxlY3RlZChvcHRpb24pKSBvcHRpb24uY2xpY2soKTsKICAgIGNvbnN0IHNpbmdsZSA9IGNvbnRyb2xzKCkuZmlsdGVyKG5vZGUgPT4gL15beMOXXVxzKjEkL2l1LnRlc3QoY2FwdGlvbihub2RlKSkpOwogICAgaWYgKHNpbmdsZS5sZW5ndGggPT09IDEgJiYgIXNlbGVjdGVkKHNpbmdsZVswXSkpIHNpbmdsZVswXS5jbGljaygpOwogICAgaWYgKG9wZW5lZCAmJiBvcHRpb25zKCkubGVuZ3RoICYmIHNldHRpbmdzLmlzQ29ubmVjdGVkKSBzZXR0aW5ncy5jbGljaygpOwogIH0KICBhc3luYyBmdW5jdGlvbiBydW4oKSB7CiAgICBsZXQgYXBwbGllZFJhdGlvOwogICAgY29uc3QgYmF0Y2ggPSBhd2FpdCBzZW5kKHtvcDoiYmF0Y2gifSk7CiAgICBpZiAoYmF0Y2guZG9uZSkgcmV0dXJuOwogICAgY29uc3QgcXVldWVkID0gW107CiAgICBjb25zdCByZXN1bHRzID0gW107CiAgICBjb25zdCBjYXJkcyA9ICgpID0+IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbcm9sZT0iZ3JvdXAiXVthcmlhLWxhYmVsXScpXTsKICAgIGNvbnN0IG5vcm1hbGl6ZSA9IHRleHQgPT4gKHRleHQgfHwgIiIpLnJlcGxhY2UoL1xzKy9ndSwgIiAiKS50cmltKCk7CiAgICBjb25zdCBpbml0aWFsQ2FyZHMgPSBjYXJkcygpOwogICAgbGV0IG5ld2VzdEZpcnN0ID0gdHJ1ZTsKICAgIGxldCBsYXN0U3VibWl0ID0gMDsKICAgIGZ1bmN0aW9uIGNoZWNrRmxvd0Jsb2NrKCkgewogICAgICBjb25zdCBub3RpY2VzID0gWy4uLmNhcmRzKCksIC4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tyb2xlPSJhbGVydCJdLFtyb2xlPSJzdGF0dXMiXScpXTsKICAgICAgaWYgKG5vdGljZXMuc29tZShub2RlID0+IHZpc2libGUobm9kZSkgJiYKICAgICAgICAgIC9jaMO6bmcgdMO0aSBuaOG6rW4gdGjhuqV5IGPDsyBob+G6oXQgxJHhu5luZyBi4bqldCB0aMaw4budbmd8d2Ugbm90aWNlZCBzb21lIHVudXN1YWwgYWN0aXZpdHl8d2UgZGV0ZWN0ZWQgdW51c3VhbCBhY3Rpdml0eS9pLnRlc3Qobm9kZS5pbm5lclRleHQgfHwgIiIpKSkKICAgICAgICB0aHJvdyBFcnJvcigiRmxvdyBiw6FvIGhv4bqhdCDEkeG7mW5nIGLhuqV0IHRoxrDhu51uZy4gVuG6vSBUYXkgxJHDoyBk4burbmcgZ+G7rWkgcHJvbXB0LiBIw6N5IGtp4buDbSB0cmEgRmxvdyB0cm9uZyBDaHJvbWUgdHLGsOG7m2Mga2hpIHRo4butIGzhuqFpLiIpOwogICAgfQogICAgYXN5bmMgZnVuY3Rpb24gc3VibWl0U2NlbmUoam9iKSB7CiAgICAgIGNoZWNrRmxvd0Jsb2NrKCk7CiAgICAgIGNvbnN0IHtwcm9tcHR9ID0gam9iOwogICAgICBjb25zdCBhZ2VudCA9IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbcm9sZT0iY2hlY2tib3giXSxpbnB1dFt0eXBlPSJjaGVja2JveCJdJyldLmZpbmQobm9kZSA9PiB2aXNpYmxlKG5vZGUpCiAgICAgICAgJiYgL14oVMOhYyBuaMOibnxBZ2VudCkkL2l1LnRlc3QoKG5vZGUuZ2V0QXR0cmlidXRlKCJhcmlhLWxhYmVsIikgfHwgbm9kZS50ZXh0Q29udGVudCB8fCAiIikudHJpbSgpKQogICAgICAgICYmIChub2RlLmNoZWNrZWQgfHwgbm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtY2hlY2tlZCIpID09PSAidHJ1ZSIpKTsKICAgICAgaWYgKGFnZW50KSB7IGFnZW50LmNsaWNrKCk7IGF3YWl0IHNsZWVwKDMwMCk7IH0KICAgICAgbGV0IGZpZWxkID0gYXdhaXQgd2FpdEZvcihwcm9tcHRGaWVsZCwgMjUpOwogICAgICBpZiAoIWZpZWxkKSB0aHJvdyBFcnJvcigiS2jDtG5nIHjDoWMgxJHhu4tuaCDEkcaw4bujYyDDtCBwcm9tcHQgZHV5IG5o4bqldC4gTeG7nyB0cmFuZyB04bqhbyDhuqNuaCB0cm9uZyBk4buxIMOhbiBGbG93LiIpOwogICAgICBhd2FpdCBzZW5kKHtvcDoiYWN0aXZhdGUifSk7CiAgICAgIGNvbnN0IHJhdGlvID0gL15U4bqhbyBt4buZdCDhuqNuaCBtaW5oIGjhu41hIHThu7cgbOG7hyAoXGQrOlxkKykgL3UuZXhlYyhqb2IucHJvbXB0KT8uWzFdOwogICAgICBpZiAocmF0aW8gJiYgcmF0aW8gIT09IGFwcGxpZWRSYXRpbykgewogICAgICAgIGF3YWl0IHNlbGVjdFJhdGlvKHJhdGlvKTsKICAgICAgICBhcHBsaWVkUmF0aW8gPSByYXRpbzsKICAgICAgICBmaWVsZCA9IGF3YWl0IHdhaXRGb3IocHJvbXB0RmllbGQsIDEwKTsKICAgICAgICBpZiAoIWZpZWxkKSB0aHJvdyBFcnJvcigiS2jDtG5nIHRo4bqleSDDtCBwcm9tcHQgc2F1IGtoaSBjaOG7jW4gdOG7tyBs4buHLiIpOwogICAgICB9CiAgICAgIGZpZWxkLmZvY3VzKCk7CiAgICAgIGlmIChmaWVsZCBpbnN0YW5jZW9mIEhUTUxUZXh0QXJlYUVsZW1lbnQpIHsKICAgICAgICBPYmplY3QuZ2V0T3duUHJvcGVydHlEZXNjcmlwdG9yKEhUTUxUZXh0QXJlYUVsZW1lbnQucHJvdG90eXBlLCJ2YWx1ZSIpLnNldC5jYWxsKGZpZWxkLGpvYi5wcm9tcHQpOwogICAgICAgIGZpZWxkLmRpc3BhdGNoRXZlbnQobmV3IElucHV0RXZlbnQoImlucHV0Iix7YnViYmxlczp0cnVlLGRhdGE6am9iLnByb21wdCxpbnB1dFR5cGU6Imluc2VydFRleHQifSkpOwogICAgICB9IGVsc2UgewogICAgICAgIGNvbnN0IHNlbGVjdGlvbiA9IHdpbmRvdy5nZXRTZWxlY3Rpb24oKTsKICAgICAgICBjb25zdCByYW5nZSA9IGRvY3VtZW50LmNyZWF0ZVJhbmdlKCk7CiAgICAgICAgcmFuZ2Uuc2VsZWN0Tm9kZUNvbnRlbnRzKGZpZWxkKTsKICAgICAgICBzZWxlY3Rpb24ucmVtb3ZlQWxsUmFuZ2VzKCk7CiAgICAgICAgc2VsZWN0aW9uLmFkZFJhbmdlKHJhbmdlKTsKICAgICAgICBpZiAoIWRvY3VtZW50LmV4ZWNDb21tYW5kKCJpbnNlcnRUZXh0IixmYWxzZSxqb2IucHJvbXB0KSkgdGhyb3cgRXJyb3IoIktow7RuZyDEkWnhu4FuIMSRxrDhu6NjIHByb21wdCB0cm9uZyBGbG93LiIpOwogICAgICB9CiAgICAgIGF3YWl0IHNsZWVwKDUwMCk7CiAgICAgIGNvbnN0IGJ1dHRvbnMgPSBbLi4uZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgiYnV0dG9uLFtyb2xlPWJ1dHRvbl0iKV0uZmlsdGVyKGI9PnZpc2libGUoYikmJiFiLmRpc2FibGVkJiZiLmdldEF0dHJpYnV0ZSgiYXJpYS1kaXNhYmxlZCIpIT09InRydWUiKTsKICAgICAgY29uc3Qgc3RhcnQgPSBidXR0b25zLmZpbHRlcihidXR0b24gPT4gL14oQuG6r3QgxJHhuqd1IHThuqFvfFN0YXJ0IGdlbmVyYXRpbmd8U3RhcnQgY3JlYXRpbmcpJC9pdS50ZXN0KGNhcHRpb24oYnV0dG9uKSkpOwogICAgICBsZXQgZ2VuZXJhdGUgPSBzdGFydC5sZW5ndGggPT09IDEgPyBzdGFydCA6IGJ1dHRvbnMuZmlsdGVyKGI9Pi9eKEdlbmVyYXRlIEltYWdlfENyZWF0ZSBJbWFnZXxU4bqhbyBow6xuaCDhuqNuaHxU4bqhbyDhuqNuaCkkL2kudGVzdChjYXB0aW9uKGIpKSk7CiAgICAgIGlmIChnZW5lcmF0ZS5sZW5ndGggPT09IDAgJiYgbWF0Y2hpbmcoL05hbm8gQmFuYW5hfEltYWdlbi9pKS5sZW5ndGggPT09IDEpCiAgICAgICAgZ2VuZXJhdGUgPSBidXR0b25zLmZpbHRlcihiPT4vXihHZW5lcmF0ZXxU4bqhbykkL2kudGVzdChjYXB0aW9uKGIpKSk7CiAgICAgIGlmIChnZW5lcmF0ZS5sZW5ndGggPT09IDApIHsKICAgICAgICBjb25zdCBlZGl0b3IgPSBmaWVsZC5jbG9zZXN0KCJmb3JtIikgfHwgZmllbGQucGFyZW50RWxlbWVudDsKICAgICAgICBnZW5lcmF0ZSA9IGJ1dHRvbnMuZmlsdGVyKGJ1dHRvbiA9PiAvXihhcnJvd19mb3J3YXJkfHNlbmR8Z+G7rWl8YuG6r3QgxJHhuqd1IHThuqFvfHN0YXJ0IGdlbmVyYXRpbmd8c3RhcnQgY3JlYXRpbmcpJC9pdS50ZXN0KGNhcHRpb24oYnV0dG9uKSkKICAgICAgICAgICYmIChlZGl0b3I/LmNvbnRhaW5zKGJ1dHRvbikgfHwgZmllbGQuZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCkuYm90dG9tIDw9IGJ1dHRvbi5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKS5ib3R0b20KICAgICAgICAgICAgJiYgTWF0aC5hYnMoZmllbGQuZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCkucmlnaHQgLSBidXR0b24uZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCkucmlnaHQpIDwgMTAwKSk7CiAgICAgIH0KICAgICAgaWYgKGdlbmVyYXRlLmxlbmd0aCAhPT0gMSkgdGhyb3cgRXJyb3IoIktow7RuZyBuaOG6rW4gZGnhu4duIMSRxrDhu6NjIG7DunQgdOG6oW8g4bqjbmguIEtp4buDbSB0cmEgY2jhur8gxJHhu5kg4bqibmgsIG1vZGVsIHbDoCBo4bqhbiBt4bupYyBGbG93LiIpOwogICAgICBjb25zdCBwcmlvciA9IG5ldyBTZXQoY2FyZHMoKSk7CiAgICAgIGNvbnN0IGVkaXRvckJveCA9IGZpZWxkLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgICBjb25zdCBuZWFyYnlBcnJvd3MgPSBidXR0b25zLmZpbHRlcihidXR0b24gPT4gewogICAgICAgIGNvbnN0IGJveCA9IGJ1dHRvbi5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKTsKICAgICAgICByZXR1cm4gYm94LnRvcCA+PSBlZGl0b3JCb3guYm90dG9tIC0gOCAmJiBib3gudG9wIDw9IGVkaXRvckJveC5ib3R0b20gKyAxMDAKICAgICAgICAgICYmIE1hdGguYWJzKGJveC5yaWdodCAtIGVkaXRvckJveC5yaWdodCkgPCAxMDAKICAgICAgICAgICYmIChbLi4uYnV0dG9uLnF1ZXJ5U2VsZWN0b3JBbGwoIm1hdC1pY29uLFtkYXRhLWljb25dLFthcmlhLWhpZGRlbj10cnVlXSxpbWciKV0uc29tZShpY29uID0+CiAgICAgICAgICAgIC9eKGFycm93X2ZvcndhcmR8c2VuZCkkL2kudGVzdCgoaWNvbi5nZXRBdHRyaWJ1dGUoImRhdGEtaWNvbiIpIHx8IGljb24uZ2V0QXR0cmlidXRlKCJhbHQiKSB8fCBpY29uLnRleHRDb250ZW50IHx8ICIiKS50cmltKCkpKQogICAgICAgICAgICB8fCAvXihC4bqvdCDEkeG6p3UgdOG6oW98U3RhcnQgZ2VuZXJhdGluZ3xTdGFydCBjcmVhdGluZykkL2l1LnRlc3QoY2FwdGlvbihidXR0b24pKSk7CiAgICAgIH0pOwogICAgICBjb25zdCBzdWJtaXQgPSBuZWFyYnlBcnJvd3MubGVuZ3RoID09PSAxID8gbmVhcmJ5QXJyb3dzWzBdIDogZ2VuZXJhdGVbMF07CiAgICAgIHN1Ym1pdC5zY3JvbGxJbnRvVmlldyh7YmxvY2s6Im5lYXJlc3QiLCBpbmxpbmU6Im5lYXJlc3QifSk7CiAgICAgIGNvbnN0IGJveCA9IHN1Ym1pdC5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKTsKICAgICAgY29uc3QgeCA9IGJveC5sZWZ0ICsgYm94LndpZHRoIC8gMjsKICAgICAgY29uc3QgeSA9IGJveC50b3AgKyBib3guaGVpZ2h0IC8gMjsKICAgICAgaWYgKCFzdWJtaXQuY29udGFpbnMoZG9jdW1lbnQuZWxlbWVudEZyb21Qb2ludCh4LHkpKSkgdGhyb3cgRXJyb3IoIk7DunQgdOG6oW8g4bqjbmggxJFhbmcgYuG7iyBjaGUuIik7CiAgICAgIGF3YWl0IHNsZWVwKE1hdGgubWF4KDAsIDgwMDAgLSAoRGF0ZS5ub3coKSAtIGxhc3RTdWJtaXQpKSk7CiAgICAgIGNoZWNrRmxvd0Jsb2NrKCk7CiAgICAgIGxhc3RTdWJtaXQgPSBEYXRlLm5vdygpOwogICAgICBhd2FpdCBzZW5kKHtvcDoic3VibWl0Iiwgam9iOmpvYi5qb2IsIHgsIHl9KTsKICAgICAgY29uc3QgZmluZENhcmQgPSAoKSA9PiB7CiAgICAgICAgY29uc3QgbWF0Y2hlcyA9IGNhcmRzKCkuZmlsdGVyKGNhcmQgPT4gIXByaW9yLmhhcyhjYXJkKSAmJgogICAgICAgICAgKG5vcm1hbGl6ZShjYXJkLmdldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIpKSA9PT0gbm9ybWFsaXplKHByb21wdCkKICAgICAgICAgICB8fCBub3JtYWxpemUoY2FyZC50ZXh0Q29udGVudCkuaW5jbHVkZXMobm9ybWFsaXplKHByb21wdCkpKSk7CiAgICAgICAgcmV0dXJuIG1hdGNoZXMubGVuZ3RoID09PSAxID8gbWF0Y2hlc1swXSA6IG51bGw7CiAgICAgIH07CiAgICAgIGNvbnN0IGNhcmQgPSBhd2FpdCB3YWl0Rm9yKCgpID0+IHsgY2hlY2tGbG93QmxvY2soKTsgcmV0dXJuIGZpbmRDYXJkKCk7IH0sIDIwKTsKICAgICAgaWYgKCFjYXJkKSB0aHJvdyBFcnJvcigiRmxvdyBjaMawYSB4w6FjIG5o4bqtbiDEkcOjIG5o4bqtbiBwcm9tcHQgY+G6o25oICIgKyAoam9iLmluZGV4ICsgMSkgKyAiLiIpOwogICAgICByZXR1cm4ge2pvYiwgY2FyZCwgZmluZENhcmR9OwogICAgfQogICAgZm9yIChjb25zdCBbb2Zmc2V0LCBwcm9tcHRdIG9mIGJhdGNoLnByb21wdHMuZW50cmllcygpKSB7CiAgICAgIGNvbnN0IGpvYiA9IHtqb2I6YmF0Y2guam9iLCBpbmRleDpiYXRjaC5pbmRleCArIG9mZnNldCwgcHJvbXB0fTsKICAgICAgY29uc3QgZW50cnkgPSBhd2FpdCBzdWJtaXRTY2VuZShqb2IpOwogICAgICBjb25zdCBjYXJkID0gZW50cnkuY2FyZDsKICAgICAgY29uc3QgY3VycmVudENhcmRzID0gY2FyZHMoKTsKICAgICAgbmV3ZXN0Rmlyc3QgJiY9IGN1cnJlbnRDYXJkcy5sZW5ndGggPT09IGluaXRpYWxDYXJkcy5sZW5ndGggKyBxdWV1ZWQubGVuZ3RoICsgMQogICAgICAgICYmIGN1cnJlbnRDYXJkc1swXSA9PT0gY2FyZDsKICAgICAgcXVldWVkLnB1c2goZW50cnkpOwogICAgICBhd2FpdCBzZW5kKHtvcDoicHJvZ3Jlc3MiLCBjb3VudDpxdWV1ZWQubGVuZ3RoLCBzdGFnZToic3VibWl0dGVkIn0pOwogICAgfQogICAgY29uc3QgY2xhaW1lZCA9IG5ldyBTZXQoKTsKICAgIGZvciAoY29uc3Qgb3JpZ2luYWwgb2YgcXVldWVkKSB7CiAgICAgIGNvbnN0IHtqb2J9ID0gb3JpZ2luYWw7CiAgICAgIGxldCBlbnRyeSA9IG9yaWdpbmFsOwogICAgICBsZXQgcmVzdWx0OwogICAgICBmb3IgKGxldCBhdHRlbXB0ID0gMDsgYXR0ZW1wdCA8IDM7IGF0dGVtcHQrKykgewogICAgICAgIGNvbnN0IHtjYXJkLCBmaW5kQ2FyZH0gPSBlbnRyeTsKICAgICAgICBjb25zdCBzdGF0dXMgPSBhd2FpdCB3YWl0Rm9yKCgpID0+IHsKICAgICAgICBjaGVja0Zsb3dCbG9jaygpOwogICAgICAgIGNvbnN0IGdyaWQgPSBjYXJkcygpOwogICAgICAgIGNvbnN0IHJlcGxhY2VtZW50ID0gYXR0ZW1wdCA9PT0gMCAmJiBuZXdlc3RGaXJzdCAmJiBpbml0aWFsQ2FyZHMubGVuZ3RoID09PSAwICYmIGdyaWQubGVuZ3RoID09PSBxdWV1ZWQubGVuZ3RoCiAgICAgICAgICAmJiBncmlkLmV2ZXJ5KGl0ZW0gPT4gcmVzdWx0SW1hZ2VzKGl0ZW0pLmxlbmd0aCA9PT0gMSkKICAgICAgICAgID8gZ3JpZFtxdWV1ZWQubGVuZ3RoIC0gMSAtIHF1ZXVlZC5maW5kSW5kZXgoZW50cnkgPT4gZW50cnkuY2FyZCA9PT0gY2FyZCldIDogbnVsbDsKICAgICAgICBjb25zdCBjdXJyZW50ID0gKGNhcmQuaXNDb25uZWN0ZWQgJiYgcmVzdWx0SW1hZ2VzKGNhcmQpLmxlbmd0aCA/IGNhcmQgOiBmaW5kQ2FyZCgpKSB8fCByZXBsYWNlbWVudCB8fCAoY2FyZC5pc0Nvbm5lY3RlZCA/IGNhcmQgOiBudWxsKTsKICAgICAgICBpZiAoIWN1cnJlbnQgfHwgY2xhaW1lZC5oYXMoY3VycmVudCkpIHJldHVybiBudWxsOwogICAgICAgIGNvbnN0IG1lc3NhZ2UgPSBub3JtYWxpemUoY3VycmVudC5pbm5lclRleHQpOwogICAgICAgIGlmICgvaOG6oW4gbeG7qWN8a2jDtG5nIMSR4bunIHTDrW4gZOG7pW5nfGNow61uaCBzw6FjaHx1bnVzdWFsIGFjdGl2aXR5fHBvbGljeSB2aW9sYXRpb258bm90IGVub3VnaCBjcmVkaXRzfHF1b3RhL2kudGVzdChtZXNzYWdlKSAmJiAhcmVzdWx0SW1hZ2VzKGN1cnJlbnQpLmxlbmd0aCkKICAgICAgICAgIHRocm93IEVycm9yKCJGbG93IHnDqnUgY+G6p3Uga2nhu4NtIHRyYSBjaMOtbmggc8OhY2ggaG/hurdjIGjhuqFuIG3hu6ljIOG7nyBj4bqjbmggIiArIChqb2IuaW5kZXggKyAxKSArICIuIik7CiAgICAgICAgaWYgKC904bqhbyBxdcOhIG5oYW5ofHJlcXVlc3RpbmcgZ2VuZXJhdGlvbnMgdG9vIHF1aWNrbHl8cmF0ZSBsaW1pdHxnZW5lcmF0aW9uIGZhaWxlZHxraMO0bmcgdGjhu4MgdOG6oW98dOG6oW8g4bqjbmggdGjhuqV0IGLhuqFpL2kudGVzdChtZXNzYWdlKSAmJiAhcmVzdWx0SW1hZ2VzKGN1cnJlbnQpLmxlbmd0aCkKICAgICAgICAgIHJldHVybiB7ZmFpbGVkOnRydWV9OwogICAgICAgIGNvbnN0IHVybHMgPSBbLi4ubmV3IFNldChyZXN1bHRJbWFnZXMoY3VycmVudCkubWFwKGltYWdlID0+IGltYWdlLmN1cnJlbnRTcmMgfHwgaW1hZ2Uuc3JjKSldOwogICAgICAgIGlmICh1cmxzLmxlbmd0aCAhPT0gMSkgcmV0dXJuIG51bGw7CiAgICAgICAgY2xhaW1lZC5hZGQoY3VycmVudCk7CiAgICAgICAgcmV0dXJuIHtjYXJkOmN1cnJlbnR9OwogICAgICAgIH0sIDE1MCk7CiAgICAgICAgaWYgKHN0YXR1cz8uY2FyZCkgeyByZXN1bHQgPSBzdGF0dXMuY2FyZDsgYnJlYWs7IH0KICAgICAgICBpZiAoIXN0YXR1cz8uZmFpbGVkIHx8IGF0dGVtcHQgPT09IDIpIGJyZWFrOwogICAgICAgIGF3YWl0IHNlbmQoe29wOiJwcm9ncmVzcyIsIGNvdW50OnJlc3VsdHMubGVuZ3RoICsgMSwgc3RhZ2U6InJldHJ5In0pOwogICAgICAgIGF3YWl0IHNsZWVwKGF0dGVtcHQgPT09IDAgPyAzMDAwMCA6IDYwMDAwKTsKICAgICAgICBlbnRyeSA9IGF3YWl0IHN1Ym1pdFNjZW5lKGpvYik7CiAgICAgIH0KICAgICAgaWYgKCFyZXN1bHQpIHRocm93IEVycm9yKCJLaMO0bmcgbmjhuq1uIMSRxrDhu6NjIOG6o25oIGPhuqNuaCAiICsgKGpvYi5pbmRleCArIDEpICsgIi4gS2nhu4NtIHRyYSB0cuG6oW5nIHRow6FpIGPhuqNuaCB0cm9uZyBGbG93LiIpOwogICAgICByZXN1bHRzLnB1c2goe2pvYiwgcmVzdWx0fSk7CiAgICAgIGF3YWl0IHNlbmQoe29wOiJwcm9ncmVzcyIsIGNvdW50OnJlc3VsdHMubGVuZ3RofSk7CiAgICB9CiAgICBmb3IgKGNvbnN0IHtqb2IsIHJlc3VsdDpjYXJkfSBvZiByZXN1bHRzKSB7CiAgICAgIGlmICghY2FyZC5pc0Nvbm5lY3RlZCkgdGhyb3cgRXJyb3IoIlRo4bq7IOG6o25oIGPhuqNuaCAiICsgKGpvYi5pbmRleCArIDEpICsgIiDEkcOjIHRoYXkgxJHhu5VpLiIpOwogICAgICBjb25zdCBhY3RpdmF0ZUNvbnRyb2wgPSBhc3luYyAobm9kZSwgY2xpY2tzPTEpID0+IHsKICAgICAgICBhd2FpdCBzZW5kKHtvcDoiYWN0aXZhdGUifSk7CiAgICAgICAgbm9kZS5zY3JvbGxJbnRvVmlldyh7YmxvY2s6ImNlbnRlciIsIGlubGluZToibmVhcmVzdCJ9KTsKICAgICAgICBjb25zdCBib3ggPSBub2RlLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgICAgIGNvbnN0IHggPSBib3gubGVmdCArIGJveC53aWR0aCAvIDIsIHkgPSBib3gudG9wICsgYm94LmhlaWdodCAvIDI7CiAgICAgICAgaWYgKCFub2RlLmNvbnRhaW5zKGRvY3VtZW50LmVsZW1lbnRGcm9tUG9pbnQoeCx5KSkpIHRocm93IEVycm9yKCJOw7p0IHThuqNpIOG6o25oIEZsb3cgxJFhbmcgYuG7iyBjaGUuIik7CiAgICAgICAgYXdhaXQgc2VuZCh7b3A6InN1Ym1pdCIsIGpvYjpqb2Iuam9iLCB4LCB5LCBjbGlja3N9KTsKICAgICAgfTsKICAgICAgY29uc3QgdGlsZSA9IHJlc3VsdEltYWdlcyhjYXJkKVswXTsKICAgICAgYXdhaXQgYWN0aXZhdGVDb250cm9sKHRpbGUsIDIpOwogICAgICBjb25zdCBkb3dubG9hZCA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gbWF0Y2hpbmcoL14oVOG6o2kgbuG7mWkgZHVuZyBuZ2hlIG5ow6xuIHh14buRbmd8RG93bmxvYWQgbWVkaWF8RG93bmxvYWQpJC9pdSlbMF0sIDEyKTsKICAgICAgaWYgKCFkb3dubG9hZCkgdGhyb3cgRXJyb3IoIktow7RuZyB0aOG6pXkgbsO6dCB04bqjaSDhuqNuaCBn4buRYyBj4bunYSBj4bqjbmggIiArIChqb2IuaW5kZXggKyAxKSArICIuIik7CiAgICAgIGF3YWl0IGFjdGl2YXRlQ29udHJvbChkb3dubG9hZCk7CiAgICAgIGNvbnN0IG9wdGlvbiA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tyb2xlPSJtZW51aXRlbSJdLGJ1dHRvbicpXQogICAgICAgIC5maW5kKGl0ZW0gPT4gdmlzaWJsZShpdGVtKSAmJiAvXjJLKD86XHN8JCkvaXUudGVzdChjYXB0aW9uKGl0ZW0pKSAmJiAhaXRlbS5kaXNhYmxlZCAmJiBpdGVtLmdldEF0dHJpYnV0ZSgiYXJpYS1kaXNhYmxlZCIpICE9PSAidHJ1ZSIpLCA4KTsKICAgICAgaWYgKCFvcHRpb24pIHRocm93IEVycm9yKCJGbG93IGtow7RuZyBjw7MgdMO5eSBjaOG7jW4gdOG6o2kgMksgY2hvIGPhuqNuaCAiICsgKGpvYi5pbmRleCArIDEpICsgIi4iKTsKICAgICAgYXdhaXQgc2VuZCh7b3A6ImFybTJrIixqb2I6am9iLmpvYixpbmRleDpqb2IuaW5kZXh9KTsKICAgICAgYXdhaXQgYWN0aXZhdGVDb250cm9sKG9wdGlvbik7CiAgICAgIGNvbnN0IGRlYWRsaW5lID0gRGF0ZS5ub3coKSArIDE1MDAwMDsKICAgICAgbGV0IHJlY2VpdmVkID0gZmFsc2U7CiAgICAgIHdoaWxlICghcmVjZWl2ZWQgJiYgRGF0ZS5ub3coKSA8IGRlYWRsaW5lKSB7CiAgICAgICAgcmVjZWl2ZWQgPSAoYXdhaXQgc2VuZCh7b3A6InRyYW5zZmVyMmsiLGpvYjpqb2Iuam9iLGluZGV4OmpvYi5pbmRleH0pKS5yZWNlaXZlZDsKICAgICAgICBpZiAoIXJlY2VpdmVkKSBhd2FpdCBzbGVlcCgxMDAwKTsKICAgICAgfQogICAgICBpZiAoIXJlY2VpdmVkKSB0aHJvdyBFcnJvcigiQ2hyb21lIGNoxrBhIHThuqNpIHhvbmcg4bqjbmggMksgY2hvIGPhuqNuaCAiICsgKGpvYi5pbmRleCArIDEpICsgIi4iKTsKICAgICAgaWYgKGpvYi5pbmRleCAhPT0gYmF0Y2guaW5kZXggKyBiYXRjaC5wcm9tcHRzLmxlbmd0aCAtIDEpIHsKICAgICAgICBjb25zdCBiYWNrID0gYXdhaXQgd2FpdEZvcigoKSA9PiBtYXRjaGluZygvXihOw7p0IHF1YXkgbOG6oWkgxJHhu4MgcXVheSB24buBIHRyYW5nIHRyxrDhu5tjfEdvIGJhY2t8QmFjaykkL2l1KVswXSwgOCk7CiAgICAgICAgaWYgKCFiYWNrKSB0aHJvdyBFcnJvcigiS2jDtG5nIHRo4bqleSBuw7p0IHRy4bufIGzhuqFpIGzGsOG7m2kg4bqjbmggRmxvdy4iKTsKICAgICAgICBhd2FpdCBhY3RpdmF0ZUNvbnRyb2woYmFjayk7CiAgICAgICAgYXdhaXQgd2FpdEZvcigoKSA9PiBjYXJkcygpLmxlbmd0aCA+IDAsIDEyKTsKICAgICAgICBmb3IgKGNvbnN0IGVudHJ5IG9mIHJlc3VsdHMpIHsKICAgICAgICAgIGlmIChlbnRyeS5yZXN1bHQuaXNDb25uZWN0ZWQpIGNvbnRpbnVlOwogICAgICAgICAgY29uc3QgcmVwbGFjZW1lbnQgPSBuZXdlc3RGaXJzdCAmJiBpbml0aWFsQ2FyZHMubGVuZ3RoID09PSAwICYmIGNhcmRzKCkubGVuZ3RoID09PSBxdWV1ZWQubGVuZ3RoCiAgICAgICAgICAgID8gY2FyZHMoKVtxdWV1ZWQubGVuZ3RoIC0gMSAtIChlbnRyeS5qb2IuaW5kZXggLSBiYXRjaC5pbmRleCldIDogbnVsbDsKICAgICAgICAgIGlmIChyZXBsYWNlbWVudCkgZW50cnkucmVzdWx0ID0gcmVwbGFjZW1lbnQ7CiAgICAgICAgfQogICAgICB9CiAgICB9CiAgfQogIGFzeW5jIGZ1bmN0aW9uIHdhaXRGb3IoZmluZCwgc2Vjb25kcykgewogICAgZm9yIChsZXQgaT0wO2k8c2Vjb25kcyoyO2krKykgewogICAgICBjb25zdCB2YWx1ZT1maW5kKCk7CiAgICAgIGlmICh2YWx1ZSkgcmV0dXJuIHZhbHVlOwogICAgICBhd2FpdCBzbGVlcCg1MDApOwogICAgfQogICAgcmV0dXJuIG51bGw7CiAgfQogIGNvbnN0IHByb2plY3RPcGVuID0gKCkgPT4gL1wvcHJvamVjdHM/XC9bXi9dKy8udGVzdChsb2NhdGlvbi5wYXRobmFtZSk7CiAgY29uc3QgbWF0Y2hpbmcgPSAocGF0dGVybikgPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoImJ1dHRvbixbcm9sZT1idXR0b25dLFtyb2xlPXRhYl0sYSIpXQogICAgLmZpbHRlcihpdGVtPT52aXNpYmxlKGl0ZW0pJiYhaXRlbS5kaXNhYmxlZCYmaXRlbS5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSE9PSJ0cnVlIgogICAgICAmJiAocGF0dGVybi50ZXN0KGNhcHRpb24oaXRlbSkpIHx8IHBhdHRlcm4udGVzdCgoaXRlbS5pbm5lclRleHQgfHwgaXRlbS50ZXh0Q29udGVudCB8fCAiIikucmVwbGFjZSgvXHMrL2d1LCAiICIpLnRyaW0oKSkpKTsKICBhc3luYyBmdW5jdGlvbiBjb25uZWN0KCkgewogICAgY29uc3Qgc3RhdGUgPSBhd2FpdCBzZW5kKHBhaXJpbmcgPyB7b3A6InBhaXIiLHRva2VuOnBhaXJpbmdbMV0scG9ydDpOdW1iZXIocGFpcmluZ1syXSB8fCA3ODc3KX0gOiB7b3A6InJlc3VtZSJ9KTsKICAgIGlmIChzdGF0ZS5kb25lKSByZXR1cm47CiAgICBpZiAocGFpcmluZykgaGlzdG9yeS5yZXBsYWNlU3RhdGUoaGlzdG9yeS5zdGF0ZSwgIiIsIGxvY2F0aW9uLnBhdGhuYW1lICsgbG9jYXRpb24uc2VhcmNoKTsgCiAgICBpZiAoZG9jdW1lbnQucmVhZHlTdGF0ZSA9PT0gImxvYWRpbmciKSBhd2FpdCBuZXcgUHJvbWlzZShyZXNvbHZlPT5kb2N1bWVudC5hZGRFdmVudExpc3RlbmVyKCJET01Db250ZW50TG9hZGVkIixyZXNvbHZlLHtvbmNlOnRydWV9KSk7CiAgICBpZiAoIXByb2plY3RPcGVuKCkpIHsKICAgICAgaWYgKCFwYWlyaW5nKSB0aHJvdyBFcnJvcigiRmxvdyB24bqrbiDhu58gdHJhbmcgY2jhu6cgc2F1IGtoaSB04bqhbyBk4buxIMOhbi4gS2nhu4NtIHRyYSDEkcSDbmcgbmjhuq1wIHLhu5NpIGNo4bqheSBs4bqhaS4iKTsKICAgICAgY29uc3QgY3JlYXRlID0gYXdhaXQgd2FpdEZvcigoKSA9PiB7CiAgICAgICAgY29uc3QgY2hvaWNlcyA9IG1hdGNoaW5nKC9eKD86KD86YWRkKD86X2NpcmNsZXxfYm94KT98cGx1c3xcKylccyopPyg/Ok5ldyBwcm9qZWN0fENyZWF0ZSBwcm9qZWN0fENyZWF0ZSBuZXcgcHJvamVjdHxE4buxIMOhbiBt4bubaXxU4bqhbyBk4buxIMOhbnxU4bqhbyBk4buxIMOhbiBt4bubaSkkL2l1KTsKICAgICAgICBjb25zdCBjb250cm9scyA9IGNob2ljZXMuZmlsdGVyKGl0ZW0gPT4gIWNob2ljZXMuc29tZShvdGhlciA9PiBvdGhlciAhPT0gaXRlbSAmJiBpdGVtLmNvbnRhaW5zKG90aGVyKSkpOwogICAgICAgIHJldHVybiBjb250cm9sc1swXSB8fCBudWxsOwogICAgICB9LDM1KTsKICAgICAgaWYgKCFjcmVhdGUpIHRocm93IEVycm9yKCJLaMO0bmcgdMOsbSB0aOG6pXkgbsO6dCB04bqhbyBk4buxIMOhbiBGbG93LiBLaeG7g20gdHJhIENocm9tZSDEkcOjIMSRxINuZyBuaOG6rXAgdsOgIHhlbSB0cmFuZyBGbG93IGPDsyB0aMO0bmcgYsOhbyBnw6wuIik7CiAgICAgIGNyZWF0ZS5jbGljaygpOwogICAgICBpZiAoIWF3YWl0IHdhaXRGb3IocHJvamVjdE9wZW4sNDUpKSB0aHJvdyBFcnJvcigiRmxvdyBjaMawYSBt4bufIGThu7Egw6FuIG3hu5tpIHNhdSBraGkgYuG6pW0gdOG6oW8uIik7CiAgICB9CiAgICBpZiAoIWF3YWl0IHdhaXRGb3IoKCk9PmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3IoJ3RleHRhcmVhLFtjb250ZW50ZWRpdGFibGU9InRydWUiXScpLDQ1KSkKICAgICAgdGhyb3cgRXJyb3IoIkThu7Egw6FuIG3hu5tpIGNoxrBhIGhp4buHbiDDtCBwcm9tcHQuIik7CiAgICAKICAgIGF3YWl0IHJ1bigpOwogIH0KICBjb25uZWN0KCkuY2F0Y2goYXN5bmMgZXJyb3IgPT4gewogICAgdHJ5IHthd2FpdCBzZW5kKHtvcDoiZXJyb3IiLG1lc3NhZ2U6ZXJyb3IubWVzc2FnZX0pO30gY2F0Y2ggeyBjb25zb2xlLndhcm4oIlbhur0gVGF5OiBnaMOpcCBu4buRaSBob+G6t2MgZ+G7rWkgdHLhuqFuZyB0aMOhaSB0aOG6pXQgYuG6oWkuIiwgZXJyb3IubWVzc2FnZSk7IH0KICB9KS5maW5hbGx5KCgpPT57d2luZG93Ll9fdmVUYXlSdW5uaW5nPWZhbHNlO30pOwp9KSgpOwp9IGVsc2UgewooKCkgPT4gewogIGNvbnN0IG1hdGNoID0gL14jdmV0YXk9KFthLWYwLTldezMyfSktKFxkezQsNX0pJC8uZXhlYyhsb2NhdGlvbi5oYXNoKTsKICBjb25zdCBrZXkgPSAidmV0YXktbXVzZS10YXNrIjsKICBpZiAoIW1hdGNoICYmICFzZXNzaW9uU3RvcmFnZS5nZXRJdGVtKGtleSkpIHJldHVybjsKICBjb25zdCBwYXVzZSA9IG1zID0+IG5ldyBQcm9taXNlKHJlc29sdmUgPT4gc2V0VGltZW91dChyZXNvbHZlLCBtcykpOwogIGNvbnN0IGxhYmVsID0gbm9kZSA9PiAobm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtbGFiZWwiKSB8fCBub2RlLnRpdGxlIHx8IG5vZGUudGV4dENvbnRlbnQgfHwgIiIpLnRyaW0oKTsKICBjb25zdCB2aXNpYmxlID0gbm9kZSA9PiBub2RlLmdldENsaWVudFJlY3RzKCkubGVuZ3RoICYmICFub2RlLmRpc2FibGVkICYmIG5vZGUuZ2V0QXR0cmlidXRlKCJhcmlhLWRpc2FibGVkIikgIT09ICJ0cnVlIjsKICBjb25zdCBjb250cm9scyA9IHBhdHRlcm4gPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoImJ1dHRvbixbcm9sZT1idXR0b25dIildLmZpbHRlcihub2RlID0+IHZpc2libGUobm9kZSkgJiYgcGF0dGVybi50ZXN0KGxhYmVsKG5vZGUpKSk7CiAgY29uc3Qgc2VuZCA9IGFzeW5jIG1lc3NhZ2UgPT4gewogICAgY29uc3QgcmVzcG9uc2UgPSBhd2FpdCBjaHJvbWUucnVudGltZS5zZW5kTWVzc2FnZShtZXNzYWdlKTsKICAgIGlmICghcmVzcG9uc2U/Lm9rKSB0aHJvdyBFcnJvcihyZXNwb25zZT8uZXJyb3IgfHwgIk3huqV0IGvhur90IG7hu5FpIHbhu5tpIFbhur0gVGF5LiIpOwogICAgcmV0dXJuIHJlc3BvbnNlLnJlc3VsdDsKICB9OwogIGFzeW5jIGZ1bmN0aW9uIHdhaXRGb3IoZmluZCwgbXMsIG1lc3NhZ2UpIHsKICAgIGNvbnN0IGVuZCA9IERhdGUubm93KCkgKyBtczsKICAgIHdoaWxlIChEYXRlLm5vdygpIDwgZW5kKSB7CiAgICAgIGNvbnN0IHZhbHVlID0gZmluZCgpOwogICAgICBpZiAodmFsdWUpIHJldHVybiB2YWx1ZTsKICAgICAgYXdhaXQgcGF1c2UoNTAwKTsKICAgIH0KICAgIHRocm93IG1lc3NhZ2UgaW5zdGFuY2VvZiBFcnJvciA/IG1lc3NhZ2UgOiBFcnJvcihtZXNzYWdlKTsKICB9CiAgZnVuY3Rpb24gc2F2ZShzdGF0ZSkgewogICAgc2Vzc2lvblN0b3JhZ2Uuc2V0SXRlbShrZXksIEpTT04uc3RyaW5naWZ5KHN0YXRlKSk7CiAgfQogIGNsYXNzIFNjZW5lRXJyb3IgZXh0ZW5kcyBFcnJvciB7fQogIGNvbnN0IHRhZ0ZvciA9IChqb2IsIGluZGV4LCBhdHRlbXB0KSA9PgogICAgInZldGF5IiArIGpvYi5qb2Iuc2xpY2UoMCwgMTIpICsgImMiICsgU3RyaW5nKGluZGV4ICsgMSkucGFkU3RhcnQoNSwgIjAiKSArICJyIiArIGF0dGVtcHQ7CiAgYXN5bmMgZnVuY3Rpb24gYWR2YW5jZShzdGF0ZSkgewogICAgY29uc3QgbmV4dCA9IGF3YWl0IHNlbmQoe29wOiJuZXh0In0pOwogICAgaWYgKG5leHQuZG9uZSkgewogICAgICBzZXNzaW9uU3RvcmFnZS5yZW1vdmVJdGVtKGtleSk7CiAgICB9IGVsc2UgaWYgKG5leHQuaW5kZXggPCBzdGF0ZS5iYXRjaEVuZCkgewogICAgICBzYXZlKHtwaGFzZToibGlicmFyeSIsIGpvYjpuZXh0LmpvYiwgaW5kZXg6bmV4dC5pbmRleCwKICAgICAgICAgICAgdGFnOnRhZ0ZvcihuZXh0LCBuZXh0LmluZGV4LCAwKSwgYmF0Y2hFbmQ6c3RhdGUuYmF0Y2hFbmR9KTsKICAgICAgbG9jYXRpb24uYXNzaWduKCJodHRwczovL211c2UuYWkvbGlicmFyeS9tZWRpYT92aWV3PWltYWdlcyIpOwogICAgfSBlbHNlIHsKICAgICAgc2F2ZSh7cGhhc2U6ImNvbXBvc2UiLCBkZWxheTozMDAwMH0pOwogICAgICBsb2NhdGlvbi5hc3NpZ24oImh0dHBzOi8vbXVzZS5haS8iKTsKICAgIH0KICB9CiAgYXN5bmMgZnVuY3Rpb24gcnVuKCkgewogICAgY29uc3Qgc3RhdGUgPSBtYXRjaCA/IHtwaGFzZToiY29tcG9zZSJ9IDogSlNPTi5wYXJzZShzZXNzaW9uU3RvcmFnZS5nZXRJdGVtKGtleSkpOwogICAgY29uc3Qgam9iID0gYXdhaXQgc2VuZChtYXRjaCA/IHtvcDoicGFpciIsIHRva2VuOm1hdGNoWzFdLCBwb3J0Ok51bWJlcihtYXRjaFsyXSl9IDoge29wOiJyZXN1bWUifSk7CiAgICBpZiAoam9iLmRvbmUpIHsKICAgICAgc2Vzc2lvblN0b3JhZ2UucmVtb3ZlSXRlbShrZXkpOwogICAgICByZXR1cm47CiAgICB9CiAgICBpZiAoam9iLnByb3ZpZGVyICE9PSAibXVzZSIpIHRocm93IEVycm9yKCJUw6FjIHbhu6UgaGnhu4duIHThuqFpIGtow7RuZyB0aHXhu5ljIE11c2UuIik7CiAgICBpZiAoIW1hdGNoICYmIHN0YXRlLnBoYXNlICE9PSAiY29tcG9zZSIgJiYgKHN0YXRlLmpvYiAhPT0gam9iLmpvYiB8fCBzdGF0ZS5pbmRleCAhPT0gam9iLmluZGV4KSkKICAgICAgdGhyb3cgRXJyb3IoIkPhuqNuaCBNdXNlIMSRw6MgdGhheSDEkeG7lWkuIEjDo3kgYuG6r3QgxJHhuqd1IGzhuqFpIHThu6sgVuG6vSBUYXkuIik7CiAgICBpZiAobWF0Y2gpIHsKICAgICAgc2F2ZShzdGF0ZSk7CiAgICAgIGhpc3RvcnkucmVwbGFjZVN0YXRlKGhpc3Rvcnkuc3RhdGUsICIiLCBsb2NhdGlvbi5wYXRobmFtZSArIGxvY2F0aW9uLnNlYXJjaCk7CiAgICB9CiAgICBpZiAoc3RhdGUucGhhc2UgPT09ICJjb21wb3NlIikgewogICAgICBpZiAoc3RhdGUuZGVsYXkpIHsKICAgICAgICBhd2FpdCBzZW5kKHtvcDoicHJvZ3Jlc3MiLCBjb3VudDpqb2IuaW5kZXggKyAxLCBzdGFnZTpzdGF0ZS5hdHRlbXB0ID8gInJldHJ5IiA6ICJiYXRjaCJ9KTsKICAgICAgICBhd2FpdCBwYXVzZShzdGF0ZS5kZWxheSk7CiAgICAgIH0KICAgICAgY29uc3QgZmllbGQgPSBhd2FpdCB3YWl0Rm9yKCgpID0+IHsKICAgICAgICBjb25zdCBlZGl0b3JzID0gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ3RleHRhcmVhLFtjb250ZW50ZWRpdGFibGU9dHJ1ZV0sW2NvbnRlbnRlZGl0YWJsZT1wbGFpbnRleHQtb25seV0saW5wdXRbdHlwZT10ZXh0XScpXQogICAgICAgICAgLmZpbHRlcihub2RlID0+IHZpc2libGUobm9kZSkgJiYgIW5vZGUucmVhZE9ubHkgJiYgIW5vZGUuY2xvc2VzdCgnW2luZXJ0XSxbYXJpYS1oaWRkZW49dHJ1ZV0nKSk7CiAgICAgICAgY29uc3QgbGFiZWxPZiA9IG5vZGUgPT4gW25vZGUsIC4uLm5vZGUucXVlcnlTZWxlY3RvckFsbCgnW2RhdGEtcGxhY2Vob2xkZXJdLFtwbGFjZWhvbGRlcl0nKV0KICAgICAgICAgIC5mbGF0TWFwKGVsID0+IFsnYXJpYS1sYWJlbCcsJ3BsYWNlaG9sZGVyJywnZGF0YS1wbGFjZWhvbGRlcicsJ3RpdGxlJ10ubWFwKGF0dHIgPT4gZWwuZ2V0QXR0cmlidXRlKGF0dHIpIHx8ICcnKSkKICAgICAgICAgIC5jb25jYXQoKG5vZGUuZ2V0QXR0cmlidXRlKCdhcmlhLWxhYmVsbGVkYnknKSB8fCAnJykuc3BsaXQoL1xzKy8pLmZpbHRlcihCb29sZWFuKQogICAgICAgICAgICAubWFwKGlkID0+IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKT8udGV4dENvbnRlbnQgfHwgJycpKTsKICAgICAgICBjb25zdCBpbnB1dHMgPSBlZGl0b3JzLmZpbHRlcihub2RlID0+ICFlZGl0b3JzLnNvbWUoaW5uZXIgPT4gbm9kZSAhPT0gaW5uZXIgJiYgbm9kZS5jb250YWlucyhpbm5lcikpKTsKICAgICAgICBjb25zdCBuYW1lZCA9IGlucHV0cy5maWx0ZXIobm9kZSA9PiBsYWJlbE9mKG5vZGUpLnNvbWUodGV4dCA9PiAvXihOaOG6r24gdGlufE1lc3NhZ2V8U2VuZCBhIG1lc3NhZ2V8VHlwZSBhIG1lc3NhZ2UpKD86JHxbLuKApl0pL2l1LnRlc3QodGV4dC50cmltKCkpKSk7CiAgICAgICAgaWYgKG5hbWVkLmxlbmd0aCA9PT0gMSkgcmV0dXJuIG5hbWVkWzBdOwogICAgICAgIGNvbnN0IGNvbXBvc2VyID0gaW5wdXRzLmZpbHRlcihub2RlID0+ICFub2RlLm1hdGNoZXMoJ2lucHV0JykgJiYKICAgICAgICAgICFub2RlLmNsb3Nlc3QoJ1tyb2xlPXNlYXJjaF0sYXNpZGUsbmF2LFtyb2xlPWRpYWxvZ10nKSAmJgogICAgICAgICAgbm9kZS5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKS5ib3R0b20gPiBpbm5lckhlaWdodCAqIDAuNyk7CiAgICAgICAgcmV0dXJuIG5hbWVkLmxlbmd0aCA9PT0gMCAmJiBjb21wb3Nlci5sZW5ndGggPT09IDEgPyBjb21wb3NlclswXSA6IG51bGw7CiAgICAgIH0sIDMwMDAwLCAiS2jDtG5nIHRo4bqleSDDtCBOaOG6r24gdGluLiBIw6N5IMSRxINuZyBuaOG6rXAgTXVzZSB0cm9uZyBDaHJvbWUgcuG7k2kgdGjhu60gbOG6oWkuIik7CiAgICAgIGlmICgoZmllbGQudmFsdWUgPz8gZmllbGQuaW5uZXJUZXh0ID8/IGZpZWxkLnRleHRDb250ZW50KS50cmltKCkpIHRocm93IEVycm9yKCLDlCBOaOG6r24gdGluIE11c2UgxJFhbmcgY8OzIGLhuqNuIG5ow6FwLiIpOwogICAgICBjb25zdCBiYXRjaCA9IGF3YWl0IHNlbmQoe29wOiJiYXRjaCJ9KTsKICAgICAgaWYgKGJhdGNoLmRvbmUgfHwgYmF0Y2guam9iICE9PSBqb2Iuam9iIHx8IGJhdGNoLmluZGV4ICE9PSBqb2IuaW5kZXgpCiAgICAgICAgdGhyb3cgRXJyb3IoIkRhbmggc8OhY2ggY+G6o25oIE11c2UgxJHDoyB0aGF5IMSR4buVaS4iKTsKICAgICAgY29uc3QgYXR0ZW1wdCA9IHN0YXRlLmF0dGVtcHQgfHwgMDsKICAgICAgY29uc3QgcHJvbXB0cyA9IGJhdGNoLnByb21wdHMuc2xpY2UoMCwgYXR0ZW1wdCA/IDEgOiAxMCk7CiAgICAgIGNvbnN0IGJhdGNoRW5kID0gYXR0ZW1wdCA/IHN0YXRlLmJhdGNoRW5kIDogam9iLmluZGV4ICsgcHJvbXB0cy5sZW5ndGg7CiAgICAgIGNvbnN0IHRhZyA9IHRhZ0Zvcihqb2IsIGpvYi5pbmRleCwgYXR0ZW1wdCk7CiAgICAgIGNvbnN0IGZpbGVzID0gcHJvbXB0cy5tYXAoKGRlc2NyaXB0aW9uLCBvZmZzZXQpID0+IHsKICAgICAgICBjb25zdCBjb2RlID0gdGFnRm9yKGpvYiwgam9iLmluZGV4ICsgb2Zmc2V0LCBhdHRlbXB0KTsKICAgICAgICByZXR1cm4gbmV3IEZpbGUoW2NvZGUgKyAiXG4iICsgZGVzY3JpcHRpb25dLCBjb2RlICsgIi50eHQiLCB7dHlwZToidGV4dC9wbGFpbiJ9KTsKICAgICAgfSk7CiAgICAgIGNvbnN0IHByb21wdCA9ICLEkOG7jWMgIiArIGZpbGVzLmxlbmd0aCArICIgdOG7h3AgVFhUIMSRw61uaCBrw6htLiBU4bqhbyDEkcO6bmcgbeG7mXQg4bqjbmggdOG7qyBt4buXaSB04buHcCB2w6AgbMawdSB04bqldCBj4bqjIHbDoG8gVGjGsCB2aeG7h24gaMOsbmgg4bqjbmguICIgKwogICAgICAgICLEkOG6t3QgdMOqbiBt4buXaSDhuqNuaCBi4bqvdCDEkeG6p3UgYuG6sW5nIG3DoyDhu58gZMOybmcgxJHhuqd1IGPhu6dhIHThu4dwIHTGsMahbmcg4bupbmcsIGdp4buvIG5ndXnDqm4gbcOjLiAiICsKICAgICAgICAiS2jDtG5nIMSRxrBhIG3DoyB2w6BvIG7hu5lpIGR1bmcg4bqjbmguIELDoW8gaG/DoG4gdOG6pXQga2hpIMSRw6MgdOG6oW8geG9uZyB0b8OgbiBi4buZLiBNw6MgxJHhuqd1IMSR4bujdDogIiArIHRhZzsKICAgICAgZmllbGQuZm9jdXMoKTsKICAgICAgY29uc3QgZGF0YSA9IG5ldyBEYXRhVHJhbnNmZXIoKTsKICAgICAgZmlsZXMuZm9yRWFjaChmaWxlID0+IGRhdGEuaXRlbXMuYWRkKGZpbGUpKTsKICAgICAgZmllbGQuZGlzcGF0Y2hFdmVudChuZXcgQ2xpcGJvYXJkRXZlbnQoInBhc3RlIiwge2NsaXBib2FyZERhdGE6ZGF0YSwgYnViYmxlczp0cnVlLCBjYW5jZWxhYmxlOnRydWV9KSk7CiAgICAgIGNvbnN0IGNvbXBvc2VyVGV4dCA9ICgpID0+IHsKICAgICAgICBsZXQgbm9kZSA9IGZpZWxkOwogICAgICAgIGZvciAobGV0IGxldmVsID0gMDsgbm9kZSAmJiBsZXZlbCA8IDc7IGxldmVsKyssIG5vZGUgPSBub2RlLnBhcmVudEVsZW1lbnQpIHsKICAgICAgICAgIGNvbnN0IGNvbnRlbnQgPSBub2RlLnRleHRDb250ZW50IHx8ICIiOwogICAgICAgICAgY29uc3QgbGFiZWxzID0gWy4uLm5vZGUucXVlcnlTZWxlY3RvckFsbCgnW2FyaWEtbGFiZWxdLFt0aXRsZV0nKV0ubWFwKGVsID0+CiAgICAgICAgICAgIChlbC5nZXRBdHRyaWJ1dGUoJ2FyaWEtbGFiZWwnKSB8fCAnJykgKyAnICcgKyAoZWwudGl0bGUgfHwgJycpKS5qb2luKCIgIik7CiAgICAgICAgICBpZiAoZmlsZXMuZXZlcnkoZmlsZSA9PiAoY29udGVudCArICIgIiArIGxhYmVscykuaW5jbHVkZXMoZmlsZS5uYW1lKSkpIHJldHVybiB0cnVlOwogICAgICAgIH0KICAgICAgICByZXR1cm4gZmFsc2U7CiAgICAgIH07CiAgICAgIGF3YWl0IHdhaXRGb3IoY29tcG9zZXJUZXh0LCAyMDAwMCwgIk11c2UgY2jGsGEgbmjhuq1uIMSR4bunICIgKyBmaWxlcy5sZW5ndGggKyAiIHThu4dwLiDEkMOjIGThu6tuZyB0csaw4bubYyBraGkgZ+G7rWkuIik7CiAgICAgIGlmIChmaWVsZCBpbnN0YW5jZW9mIEhUTUxUZXh0QXJlYUVsZW1lbnQgfHwgZmllbGQgaW5zdGFuY2VvZiBIVE1MSW5wdXRFbGVtZW50KSB7CiAgICAgICAgT2JqZWN0LmdldE93blByb3BlcnR5RGVzY3JpcHRvcihPYmplY3QuZ2V0UHJvdG90eXBlT2YoZmllbGQpLCAidmFsdWUiKS5zZXQuY2FsbChmaWVsZCwgcHJvbXB0KTsKICAgICAgICBmaWVsZC5kaXNwYXRjaEV2ZW50KG5ldyBJbnB1dEV2ZW50KCJpbnB1dCIsIHtidWJibGVzOnRydWUsIGRhdGE6cHJvbXB0fSkpOwogICAgICB9IGVsc2UgaWYgKCFkb2N1bWVudC5leGVjQ29tbWFuZCgiaW5zZXJ0VGV4dCIsIGZhbHNlLCBwcm9tcHQpKSB7CiAgICAgICAgdGhyb3cgRXJyb3IoIktow7RuZyDEkWnhu4FuIMSRxrDhu6NjIGzhu4duaCB04bqhbyDhuqNuaCBNdXNlLiIpOwogICAgICB9CiAgICAgIGNvbnN0IGJ1dHRvbiA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gewogICAgICAgIGNvbnN0IGJ1dHRvbnMgPSBjb250cm9scygvXihH4butaXxTZW5kfFNlbmQgbWVzc2FnZSkkL2l1KTsKICAgICAgICByZXR1cm4gYnV0dG9ucy5sZW5ndGggPT09IDEgPyBidXR0b25zWzBdIDogbnVsbDsKICAgICAgfSwgODAwMCwgIktow7RuZyB0w6xtIMSRxrDhu6NjIG7DunQgR+G7rWkgY+G7p2EgTXVzZS4iKTsKICAgICAgY29uc3QgbmV4dCA9IHtwaGFzZToiYXdhaXQiLCBqb2I6am9iLmpvYiwgaW5kZXg6am9iLmluZGV4LCB0YWcsIGF0dGVtcHQsIGJhdGNoRW5kfTsKICAgICAgc2F2ZShuZXh0KTsKICAgICAgYnV0dG9uLmNsaWNrKCk7CiAgICAgIGF3YWl0IHNlbmQoe29wOiJwcm9ncmVzcyIsIGNvdW50OmpvYi5pbmRleCArIHByb21wdHMubGVuZ3RoLCBzdGFnZToic3VibWl0dGVkIn0pOwogICAgICBhd2FpdCB3YWl0Rm9yKCgpID0+ICEoZmllbGQudmFsdWUgPz8gZmllbGQudGV4dENvbnRlbnQpLmluY2x1ZGVzKHRhZyksIDEyMDAwLCAiTXVzZSBjaMawYSBuaOG6rW4gcHJvbXB0LiIpOwogICAgICByZXR1cm4gYXdhaXQgcmVzdWx0KG5leHQsIGpvYik7CiAgICB9CiAgICBpZiAoc3RhdGUucGhhc2UgPT09ICJhd2FpdCIpIHJldHVybiBhd2FpdCByZXN1bHQoc3RhdGUsIGpvYik7CiAgICBpZiAoc3RhdGUucGhhc2UgIT09ICJsaWJyYXJ5IiAmJiBzdGF0ZS5waGFzZSAhPT0gImRvd25sb2FkIikgdGhyb3cgRXJyb3IoIlRy4bqhbmcgdGjDoWkgTXVzZSBraMO0bmcgaOG7o3AgbOG7hy4iKTsKICAgIGNvbnN0IGZpbGUgPSBuZXcgVVJMKGxvY2F0aW9uLmhyZWYpLnNlYXJjaFBhcmFtcy5nZXQoImZpbGUiKTsKICAgIGlmICghZmlsZSB8fCAhZmlsZS5zcGxpdCgiLyIpLnBvcCgpLnRvTG93ZXJDYXNlKCkuaW5jbHVkZXMoc3RhdGUudGFnKSkgewogICAgICBjb25zdCBpdGVtID0gYXdhaXQgd2FpdEZvcigoKSA9PiB7CiAgICAgICAgY29uc3QgaW1hZ2VzID0gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoImltZ1thbHRdIildLmZpbHRlcihpbWcgPT4KICAgICAgICAgIHZpc2libGUoaW1nKSAmJiBpbWcuYWx0LnRvTG93ZXJDYXNlKCkuaW5jbHVkZXMoc3RhdGUudGFnKSAmJiAvXC4od2VicHxwbmd8anBlP2cpJC9pLnRlc3QoaW1nLmFsdCkpOwogICAgICAgIGlmIChpbWFnZXMubGVuZ3RoICE9PSAxKSByZXR1cm4gbnVsbDsKICAgICAgICByZXR1cm4gaW1hZ2VzWzBdLmNsb3Nlc3QoImJ1dHRvbixbcm9sZT1idXR0b25dLGEiKSB8fCBpbWFnZXNbMF07CiAgICAgIH0sIDMwMDAwLCBuZXcgU2NlbmVFcnJvcigiS2jDtG5nIHTDrG0gxJHGsOG7o2MgxJHDum5nIOG6o25oIGPhu6dhIGPhuqNuaCB0cm9uZyBNdXNlLiIpKTsKICAgICAgaXRlbS5jbGljaygpOwogICAgfQogICAgY29uc3QgYnV0dG9uID0gYXdhaXQgd2FpdEZvcigoKSA9PiB7CiAgICAgIGNvbnN0IGZpbGUgPSBuZXcgVVJMKGxvY2F0aW9uLmhyZWYpLnNlYXJjaFBhcmFtcy5nZXQoImZpbGUiKTsKICAgICAgaWYgKCFmaWxlPy5zcGxpdCgiLyIpLnBvcCgpLnRvTG93ZXJDYXNlKCkuaW5jbHVkZXMoc3RhdGUudGFnKSkgcmV0dXJuIG51bGw7CiAgICAgIGNvbnN0IGJ1dHRvbnMgPSBjb250cm9scygvXihU4bqjaSB4deG7kW5nfERvd25sb2FkKSQvaXUpLmZpbHRlcihub2RlID0+IG5vZGUuY2xvc2VzdCgnW3JvbGU9ImRpYWxvZyJdLGRpYWxvZycpKTsKICAgICAgcmV0dXJuIGJ1dHRvbnMubGVuZ3RoID09PSAxID8gYnV0dG9uc1swXSA6IG51bGw7CiAgICB9LCAxMjAwMCwgbmV3IFNjZW5lRXJyb3IoIktow7RuZyBt4bufIMSRxrDhu6NjIOG6o25oIMSRw7puZyBtw6MgY+G6o25oLiIpKTsKICAgIGNvbnN0IGZpbGVuYW1lID0gbmV3IFVSTChsb2NhdGlvbi5ocmVmKS5zZWFyY2hQYXJhbXMuZ2V0KCJmaWxlIikuc3BsaXQoIi8iKS5wb3AoKTsKICAgIGF3YWl0IHNlbmQoe29wOiJwcm9ncmVzcyIsIGNvdW50OmpvYi5pbmRleCArIDEsIHN0YWdlOiJnZW5lcmF0ZWQifSk7CiAgICBhd2FpdCBzZW5kKHtvcDoiYXJtTXVzZSIsIGpvYjpqb2Iuam9iLCBpbmRleDpqb2IuaW5kZXgsIGZpbGVuYW1lfSk7CiAgICBidXR0b24uY2xpY2soKTsKICAgIGNvbnN0IGRlYWRsaW5lID0gRGF0ZS5ub3coKSArIDkwMDAwOwogICAgd2hpbGUgKERhdGUubm93KCkgPCBkZWFkbGluZSkgewogICAgICBpZiAoKGF3YWl0IHNlbmQoe29wOiJ0cmFuc2Zlck11c2UiLCBqb2I6am9iLmpvYiwgaW5kZXg6am9iLmluZGV4fSkpLnJlY2VpdmVkKSB7CiAgICAgICAgYXdhaXQgYWR2YW5jZShzdGF0ZSk7CiAgICAgICAgcmV0dXJuOwogICAgICB9CiAgICAgIGF3YWl0IHBhdXNlKDgwMCk7CiAgICB9CiAgICB0aHJvdyBuZXcgU2NlbmVFcnJvcigiQ2hyb21lIGNoxrBhIHThuqNpIHhvbmcg4bqjbmggTXVzZS4iKTsKICB9CiAgYXN5bmMgZnVuY3Rpb24gcmVzdWx0KHN0YXRlLCBqb2IpIHsKICAgIGNvbnN0IGV4cGVjdGVkQ291bnQgPSBzdGF0ZS5hdHRlbXB0ID8gMSA6IHN0YXRlLmJhdGNoRW5kIC0gam9iLmluZGV4OwogICAgY29uc3QgdGFncyA9IEFycmF5LmZyb20oe2xlbmd0aDpleHBlY3RlZENvdW50fSwgKF8sIG9mZnNldCkgPT4KICAgICAgdGFnRm9yKGpvYiwgam9iLmluZGV4ICsgb2Zmc2V0LCBzdGF0ZS5hdHRlbXB0IHx8IDApKTsKICAgIGNvbnN0IGRlYWRsaW5lID0gRGF0ZS5ub3coKSArIDI0NTAwMDsKICAgIHdoaWxlIChEYXRlLm5vdygpIDwgZGVhZGxpbmUpIHsKICAgICAgY29uc3QgdGV4dCA9IGRvY3VtZW50LmJvZHk/LmlubmVyVGV4dCB8fCAiIjsKICAgICAgY29uc3QgYXQgPSB0ZXh0Lmxhc3RJbmRleE9mKHN0YXRlLnRhZyk7CiAgICAgIGlmIChhdCA+PSAwKSB7CiAgICAgICAgY29uc3QgbmVhcmJ5ID0gdGV4dC5zbGljZShNYXRoLm1heCgwLCBhdCAtIDEyMDApLCBhdCArIDQwMDApOwogICAgICAgIGNvbnN0IGFmdGVyID0gdGV4dC5zbGljZShhdCk7CiAgICAgICAgaWYgKC9o4bq/dCAoPzpo4bqhbiBt4bupY3xsxrDhu6N0KXzEkcOjIMSR4bqhdCBnaeG7m2kgaOG6oW58KD86d2Vla2x5fHVzYWdlfHJhdGUpIGxpbWl0fHF1b3RhIGV4Y2VlZGVkL2l1LnRlc3QoYWZ0ZXIpKQogICAgICAgICAgdGhyb3cgRXJyb3IoIk11c2UgYsOhbyBnaeG7m2kgaOG6oW4gc+G7rSBk4bulbmcuIMSQw6MgZOG7q25nIGfhu61pIHRow6ptIHByb21wdC4iKTsKICAgICAgICBjb25zdCBjb21wbGV0ZSA9IC9ob8OgbiB04bqldFxzKlvigJTigJM6LV1ccyrEkcOjIHThuqFvIHhvbmd8xJHDoyB04bqhbyB4b25nIHRvw6BuIGLhu5l8Y29tcGxldGVkXHMqW+KAlOKAkzotXVxzKig/OmFsbHxnZW5lcmF0ZWQpL2l1LnRlc3QobmVhcmJ5KTsKICAgICAgICBpZiAoY29tcGxldGUgJiYgdGFncy5ldmVyeSh0YWcgPT4gdGV4dC5pbmNsdWRlcyh0YWcpKSkgYnJlYWs7CiAgICAgICAgaWYgKC9raMO0bmcgdGjhu4MgdOG6oW8g4bqjbmh8dOG6oW8g4bqjbmggdGjhuqV0IGLhuqFpfGltYWdlIGdlbmVyYXRpb24gZmFpbGVkL2l1LnRlc3QoYWZ0ZXIpKSBicmVhazsKICAgICAgfQogICAgICBhd2FpdCBwYXVzZSgxMDAwKTsKICAgIH0KICAgIC8vIE11c2UgY2FuIHNhdmUgaW1hZ2VzIGV2ZW4gd2hlbiB0aGUgY29tcGxldGlvbiBtZXNzYWdlIGlzIGFic2VudC4KICAgIHNhdmUoey4uLnN0YXRlLCBwaGFzZToibGlicmFyeSJ9KTsKICAgIGxvY2F0aW9uLmFzc2lnbigiaHR0cHM6Ly9tdXNlLmFpL2xpYnJhcnkvbWVkaWE/dmlldz1pbWFnZXMiKTsKICB9CiAgcnVuKCkuY2F0Y2goYXN5bmMgZXJyb3IgPT4gewogICAgY29uc3Qgc3RhdGUgPSBKU09OLnBhcnNlKHNlc3Npb25TdG9yYWdlLmdldEl0ZW0oa2V5KSB8fCAibnVsbCIpOwogICAgdHJ5IHsKICAgICAgaWYgKGVycm9yIGluc3RhbmNlb2YgU2NlbmVFcnJvciAmJiBzdGF0ZT8uam9iKSB7CiAgICAgICAgaWYgKCFzdGF0ZS5hdHRlbXB0KSB7CiAgICAgICAgICBzYXZlKHtwaGFzZToiY29tcG9zZSIsIGF0dGVtcHQ6MSwgZGVsYXk6MjAwMDAsIGJhdGNoRW5kOnN0YXRlLmJhdGNoRW5kfSk7CiAgICAgICAgICBsb2NhdGlvbi5hc3NpZ24oImh0dHBzOi8vbXVzZS5haS8iKTsKICAgICAgICB9IGVsc2UgewogICAgICAgICAgYXdhaXQgc2VuZCh7b3A6InNraXBNdXNlIiwgam9iOnN0YXRlLmpvYiwgaW5kZXg6c3RhdGUuaW5kZXh9KTsKICAgICAgICAgIGF3YWl0IGFkdmFuY2Uoc3RhdGUpOwogICAgICAgIH0KICAgICAgICByZXR1cm47CiAgICAgIH0KICAgICAgdGhyb3cgZXJyb3I7CiAgICB9IGNhdGNoIChmYWlsdXJlKSB7CiAgICAgIHNlc3Npb25TdG9yYWdlLnJlbW92ZUl0ZW0oa2V5KTsKICAgICAgdHJ5IHsgYXdhaXQgc2VuZCh7b3A6ImVycm9yIiwgbWVzc2FnZTpmYWlsdXJlLm1lc3NhZ2V9KTsgfSBjYXRjaCB7fQogICAgfQogIH0pOwp9KSgpOwoKfQo="
}

def sync_chrome_bundle(allow_muse=False):
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
        muse_upgrade = (
            allow_muse and ref is BUNDLED_EXTENSION
            and scopes(manifest) == sorted([*scopes(old), "https://muse.ai/*"])
            and sorted(manifest["host_permissions"]) == sorted([*old["host_permissions"], "https://muse.ai/*"])
        )
        if (old["name"] != manifest["name"] or (scopes(old) != scopes(manifest) and not muse_upgrade)
            or any(sorted(old.get(key, [])) != sorted(manifest.get(key, []))
                   for key in permissions
                   if not (key == "host_permissions" and muse_upgrade)
                   if not (key == "permissions" and ref is BUNDLED_EXTENSION
                           and sorted(old.get(key, [])) == ["alarms", "debugger", "storage"]
                           and sorted(manifest.get(key, [])) == ["alarms", "debugger", "downloads", "storage"])
                   and not (key == "host_permissions" and ref is BUNDLED_EXTENSION
                           and sorted(item.replace("http://127.0.0.1:7877/*", "http://127.0.0.1/*")
                                      for item in old.get(key, [])) == sorted(manifest.get(key, [])))
                   and not (key == "permissions" and ref is BUNDLED_EXTENSION
                           and sorted(old.get(key, [])) == ["alarms", "storage"]
                           and sorted(manifest.get(key, [])) == ["alarms", "debugger", "storage"]))):
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
        version, _ = sync_chrome_bundle(allow_muse=True)
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
        manifest = json.loads(patch_read(PATCH_ROOT + f"main/update.json?t={time.time_ns()}", 16384))
        version = manifest["version"]
        companion = ""
        if (DATA / "chrome-extension" / "manifest.json").is_file():
            try:
                ext_version, changed = sync_chrome_bundle()
            except ValueError:
                ext_version, changed = None, False
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


def open_chrome_flow(token, port, provider):
    candidates = [Path(os.environ[key]) / "Google/Chrome/Application/chrome.exe"
                  for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA") if os.environ.get(key)]
    chrome = next((path for path in candidates if path.is_file()), None)
    if chrome is None:
        raise RuntimeError("Không tìm thấy Google Chrome trên máy.")
    website = "https://muse.ai/" if provider == "muse" else "https://flow.google.com/"
    project = website + f"#vetay={token}-{port}"
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
                if self.path not in ("/next", "/batch"):
                    return self.response(404, "Không tìm thấy.")
                with bridge.guard:
                    state = bridge.job
                    index = len(state["paths"]) if state else 0
                    if not state or state["error"] or index >= len(state["prompts"]):
                        return self.response(200, {"done": True})
                    result = {"job": state["id"], "index": index, "prompt": state["prompts"][index], "provider": state["provider"]}
                    if self.path == "/batch":
                        result["prompts"] = state["prompts"][index:]
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
                        if self.path == "/progress" and size <= 256:
                            progress = json.loads(self.rfile.read(size))
                            count = progress["count"]
                            if type(count) is not int or not 1 <= count <= len(state["prompts"]):
                                return self.response(400, "Số cảnh không hợp lệ.")
                            key = {"submitted": "submitted", "retry": "retrying", "batch": "batching"}.get(progress.get("stage"), "generated")
                            state[key] = max(state.get(key, 0), count)
                        elif (state["provider"] == "muse" and index < len(state["prompts"])
                              and self.path == f"/skipMuse/{state['id']}/{index}" and size == 2):
                            self.rfile.read(size)
                            state["paths"].append(None)
                        elif self.path == "/error" and size <= 8192:
                            state["error"] = str(json.loads(self.rfile.read(size))["message"])[:600]
                        elif index < len(state["prompts"]) and self.path in (
                            f"/image/{state['id']}/{index}", f"/download2k/{state['id']}/{index}",
                            f"/downloadMuse/{state['id']}/{index}"
                        ):
                            payload = self.rfile.read(size)
                            if len(payload) != size:
                                raise ValueError("Ảnh chưa tải đủ.")
                            if self.path.startswith(("/download2k/", "/downloadMuse/")):
                                is_muse = self.path.startswith("/downloadMuse/")
                                if is_muse != (state["provider"] == "muse"):
                                    raise ValueError("Nguồn ảnh không khớp tác vụ.")
                                expected_folder = "VeTay-Muse" if is_muse else "VeTay-2K"
                                if size > 8192:
                                    raise ValueError("Đường dẫn ảnh 2K không hợp lệ.")
                                downloaded = Path(json.loads(payload)["path"])
                                expected = f"{state['id']}-{index}"
                                if (not downloaded.is_absolute() or downloaded.stem != expected
                                        or downloaded.parent.name != expected_folder or downloaded.is_symlink()
                                        or downloaded.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}):
                                    raise ValueError("Chrome không lưu ảnh 2K đúng thư mục của tác vụ.")
                                with downloaded.open("rb") as source:
                                    payload = source.read(24 * 1024 * 1024 + 1)
                                if len(payload) > 24 * 1024 * 1024:
                                    raise ValueError("Ảnh 2K vượt quá giới hạn dung lượng.")
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

        try:
            server = ExclusiveServer(("127.0.0.1", port), Handler)
        except OSError:
            if port != 7877:
                raise
            server = ExclusiveServer(("127.0.0.1", 0), Handler)
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def images(self, prompts, folder, report, provider="chrome"):
        name = "Muse" if provider == "muse" else "Google Flow"
        state = {"id": uuid.uuid4().hex, "prompts": prompts, "folder": folder,
                 "paths": [], "event": threading.Event(), "error": None, "provider": provider}
        with self.guard:
            self.token = uuid.uuid4().hex
            self.job = state
        try:
            open_chrome_flow(self.token, self.port, provider)
            report(f"Đã mở {name}. Đang chờ tiện ích Chrome.")
            while True:
                if not state["event"].wait(330):
                    raise RuntimeError(f"Hết thời gian chờ ảnh {name}. Kiểm tra tab Chrome và phiên bản tiện ích.")
                with self.guard:
                    state["event"].clear()
                    if state["error"]:
                        raise RuntimeError(name + ": " + state["error"])
                    count = len(state["paths"])
                    if count == len(prompts):
                        return state["paths"].copy()
                if state.pop("batching", None) is not None:
                    report("Muse đã xong một đợt 10 cảnh. Nghỉ 30 giây trước đợt tiếp theo.")
                    continue
                if (retry := state.pop("retrying", None)) is not None:
                    report(f"Đang chờ thử lại cảnh {retry} bằng {name}.")
                    continue
                report(f"Đã gửi {state.get('submitted', 0)}/{len(prompts)} prompt. {name} đã tạo {state.get('generated', count)}/{len(prompts)} ảnh. Vẽ Tay đã nhận {count}/{len(prompts)} ảnh.")
        finally:
            with self.guard:
                self.job = None

def ui(check=False):
    prepare_data()
    import gradio as gr
    bridge = ChromeFlowBridge()
    chrome_server = None
    def choose_output_folder(current):
        command = r"""
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Windows.Forms
$picker = New-Object System.Windows.Forms.FolderBrowserDialog
$picker.Description = 'Ve Tay - MP4'
$picker.SelectedPath = $env:VETAY_OUTPUT_PICKER
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
try {
    if ($picker.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::Write($picker.SelectedPath)
    }
} finally { $picker.Dispose(); $owner.Dispose() }
"""
        env = dict(os.environ, VETAY_OUTPUT_PICKER=current or str(DATA))
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-STA", "-Command", command],
                env=env, capture_output=True, check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise gr.Error("Không mở được hộp chọn thư mục. Bạn có thể nhập đường dẫn trực tiếp.") from error
        return result.stdout.decode("utf-8").strip() or current

    def open_output_folder(current):
        folder = Path((current or "").strip().strip('"') or DATA).expanduser()
        if not folder.is_absolute() or not folder.is_dir():
            raise gr.Error("Thư mục lưu video chưa tồn tại.")
        os.startfile(str(folder))

    def run(script, voice, pictures, provider, ratio, highlights, render, output_folder=None, shared_prompt="", full_resolution=True, progress=gr.Progress()):
        nonlocal chrome_server

        def report(message):
            progress(None, desc=message)
        if not LOCK.acquire(blocking=False):
            yield gr.skip(), gr.skip(), "Một tác vụ Vẽ Tay vẫn đang chạy. Đợi tác vụ đó kết thúc rồi thử lại."
            return
        rows = []
        try:
            save_folder = Path((output_folder or "").strip().strip('"') or DATA).expanduser()
            if render:
                if not save_folder.is_absolute() or not save_folder.is_dir():
                    raise ValueError("Chọn một thư mục lưu video đã tồn tại.")
                save_folder = save_folder.resolve()
                with tempfile.TemporaryFile(dir=save_folder):
                    pass
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
                cache = LAST_ANALYSIS.get("wav")
                if LAST_ANALYSIS.get("key") == key and cache is not None and cache.is_file():
                    wav, plan = cache, LAST_ANALYSIS["plan"]
                else:
                    yield [], None, "Đang chuẩn bị lời đọc…"
                    wav = prepare_audio(script, voice, folder)
                    yield [], None, "Đang nhận dạng lời đọc và chia cảnh…"
                    plan = scenes(transcribe(wav), duration(wav))
                    next_cache = DATA / f"analysis-{uuid.uuid4().hex}.wav"
                    os.replace(wav, next_cache)
                    wav = next_cache
                    LAST_ANALYSIS.update(key=key, plan=plan, wav=wav)
                    if cache is not None:
                        try:
                            cache.unlink(missing_ok=True)
                        except OSError:
                            pass
                rows = scene_rows(plan, ratio, highlights, shared_prompt, provider != "files")
                if not render:
                    yield rows, None, f"Sẵn sàng: {len(rows)} cảnh và prompt."
                    return
                if provider in {"chrome", "muse"}:
                    if chrome_server is None:
                        try:
                            chrome_server = bridge.serve()
                        except OSError as error:
                            raise RuntimeError(
                                f"Không mở được cổng nội bộ cho Chrome: {error}"
                            ) from error
                    name = "Muse" if provider == "muse" else "Google Flow"
                    yield rows, None, f"Đang mở {name} trong Chrome…"
                    pictures = bridge.images([row[5] for row in rows], folder, report, provider)

                if not pictures:
                    raise ValueError("Chọn ảnh theo thứ tự cảnh hoặc chọn Google Flow.")
                missing = [str(i) for i, picture in enumerate(pictures, 1) if picture is None]
                if len(missing) == len(pictures):
                    raise ValueError("Muse không tạo được ảnh cho cảnh " + ", ".join(missing) + ".")
                note = f" · Cảnh thiếu ảnh (nền trắng): {', '.join(missing)}" if missing else ""
                dest = DATA / f"VeTay-{uuid.uuid4().hex[:8]}.mp4" 
                path = render_folder(wav, pictures, plan, dest, report, ratio, highlights, full_resolution)
                saved = save_folder / dest.name
                if saved != dest:
                    try:
                        with tempfile.TemporaryDirectory(prefix="vetay-export-", dir=save_folder) as staging:
                            copy = Path(staging) / dest.name
                            shutil.copyfile(dest, copy)
                            os.replace(copy, saved)
                    except OSError as error:
                        yield rows, (path, str(dest)), f"Đã dựng MP4 tại {dest}. Không lưu được sang {save_folder}: {error}"
                        return
                yield rows, (path, str(saved)), f"Đã lưu MP4 · {len(rows)} cảnh · {saved}{note}"
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or b"").decode("utf-8", errors="replace").strip()
            yield rows, None, "Lỗi xử lý âm thanh hoặc video: " + detail[-1500:]
        except Exception as error:
            yield rows, None, "Tác vụ dừng: " + str(error)
        finally:
            LOCK.release()

    def analyze(script, voice, ratio, highlights, shared_prompt):
        yield from run(script, voice, None, None, ratio, highlights, False, shared_prompt=shared_prompt)

    def generate_video(script, voice, pictures, provider, ratio, highlights, output_folder, shared_prompt, full_resolution):
        for rows, result, message in run(
            script, voice, pictures, provider, ratio, highlights, True, output_folder, shared_prompt, full_resolution
        ):
            preview_path, saved_path = result if isinstance(result, tuple) else (None, "")
            yield rows, preview_path, message, saved_path

    def reveal_video(saved_path):
        video_file = Path(saved_path or "")
        if not video_file.is_absolute() or not video_file.is_file() or video_file.suffix.lower() != ".mp4":
            raise gr.Error("Chưa có tệp MP4 để mở.")
        subprocess.Popen(["explorer.exe", "/select,", str(video_file)])

    css = "body, .gradio-container, button, textarea, input { font-family: 'Segoe UI', Arial, sans-serif !important; }"
    with gr.Blocks(title="Vẽ Tay") as app:
        gr.HTML('<div lang="vi"><h1>Vẽ Tay</h1></div>')
        with gr.Row():
            script = gr.Textbox(label="Kịch bản tiếng Việt", lines=4)
            shared_prompt = gr.Textbox(label="Prompt chung cho toàn video", lines=4,
                                      placeholder="Ví dụ: Nền kem, nét vẽ bút chì, giữ cùng nhân vật và bảng màu qua mọi cảnh.")
        with gr.Row():
            with gr.Column():
                voice = gr.Audio(label="Bản thu lời đọc (MP3 / WAV)", sources=["upload"], type="filepath")

                ratio = gr.Dropdown([(f"{key} · {w} × {h}", key) for key, (w,h) in VIDEO_SIZES.items()],
                                    value="9:16", label="Tỷ lệ video")
            with gr.Column():
                provider = gr.Radio([("Google Flow qua tiện ích Chrome", "chrome"), ("Muse qua Chrome", "muse"), ("Ảnh có sẵn", "files")],
                                    value="chrome", label="Nguồn ảnh minh họa")

                pictures = gr.File(label="Ảnh có sẵn theo thứ tự cảnh", file_count="multiple",
                                   file_types=["image"], type="filepath")
        video = gr.Video(label="Xem trước video MP4", height=260, elem_id="vetay-preview")
        full_resolution = gr.Checkbox(value=True, label="Dựng ở độ phân giải đầy đủ")
        highlights = gr.Checkbox(value=True, label="Chữ nhấn theo cảnh")
        with gr.Row():
            analysis = gr.Button("Phân tích và chia cảnh")
            generate = gr.Button("Tự tạo video", variant="primary")
        table = gr.Dataframe(headers=["Cảnh", "Bắt đầu", "Kết thúc", "Thời lượng (giây)",
                                      "Lời đọc", "Prompt tạo ảnh", "Chữ nhấn"],
                             datatype=["number", "str", "str", "number", "str", "str", "str"],
                             label="Bảng phân cảnh theo lời đọc", wrap=True, interactive=False)
        task = gr.Textbox(label="Tiến độ dựng", interactive=False)
        preview_path = gr.State()
        with gr.Row():
            saved_video = gr.Textbox(label="Tệp MP4 đã lưu", interactive=False)
            show_video = gr.Button("Hiện tệp MP4 trong thư mục")
        show_video.click(reveal_video, [saved_video], None, queue=False)
        with gr.Row():
            output_folder = gr.Textbox(value=str(DATA), label="Thư mục lưu MP4")
            choose_folder = gr.Button("Chọn thư mục")
            open_folder = gr.Button("Mở thư mục lưu")
        choose_folder.click(choose_output_folder, [output_folder], [output_folder], queue=False)
        open_folder.click(open_output_folder, [output_folder], None, queue=False)
        status = gr.Textbox(label="Trạng thái cập nhật", interactive=False)
        gr.Button("Chuẩn bị tiện ích Chrome").click(prepare_chrome_bundle, outputs=status)
        gr.Button("Cập nhật qua GitHub").click(update, outputs=status)
        analysis.click(analyze, [script, voice, ratio, highlights, shared_prompt], [table, video, task])
        start_generation = generate.click(
            fn=lambda: "Đã nhận lệnh tạo video. Đang chờ bắt đầu dựng…",
            outputs=task, queue=False, show_progress="hidden",
        )
        generation = start_generation.success(
            generate_video, [script, voice, pictures, provider, ratio, highlights, output_folder, shared_prompt, full_resolution],
            [table, preview_path, task, saved_video],
            show_progress="minimal",
        )
        preview = generation.success(
            fn=lambda path: path, inputs=[preview_path], outputs=[video], queue=False, show_progress="hidden",
        )
        preview.success(
            fn=None, inputs=[video], outputs=None, queue=False, show_progress="hidden",
            js="""(video) => {
                if (video) document.getElementById('vetay-preview')?.scrollIntoView({behavior: 'smooth', block: 'start'});
                return [];
            }""",
        )
    if check:
        engine()
        print(f"VeTay {VERSION}: {len(app.config['components'])} controls.")
        return
    import webview
    try:
        chrome_server = bridge.serve()
    except OSError:
        pass
    threading.Thread(target=refresh_existing_extension, daemon=True).start()
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

