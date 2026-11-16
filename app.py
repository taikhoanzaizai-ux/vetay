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
VERSION = "0.2.36"
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
        (r"cà phê|ăn vặt|đồ uống", "ly cà phê và món ăn nhẹ đặt cạnh ví tiền"),
        (r"tiết kiệm|dành dụm", "hũ tiết kiệm, bàn tay bỏ đồng xu vào hũ"),
        (r"nhón gót|bắp chân", "người đang nhón gót, góc nhìn ngang dễ thấy bàn chân"),
        (r"thanh toán|chi tiêu|chi phí", "ví tiền, hóa đơn và vài đồng xu"),
        (r"một năm|mỗi tháng|mỗi tuần|365 ngày", "lịch để bàn và mũi tên chỉ thời gian"),
    )
    visuals = next((art for key, art in rules if re.search(key, text, re.I)), "")
    direction = (f"Gợi ý hình ảnh trọng tâm: {visuals}. " if visuals else
                 "Chọn một chủ thể và một hành động cụ thể có trong lời đọc. ")
    shared_prompt = (shared_prompt or "").strip()
    style = ("Yêu cầu chung áp dụng cho mọi cảnh:\n" + shared_prompt + "\n"
             if shared_prompt else
             "Nền trắng trơn, đường viền đậm rõ, ít chi tiết, chủ thể tách nền, "
             "màu xanh ngọc và vàng nhạt. Không chữ, số, logo hoặc watermark. ")
    return (f"Tạo một ảnh minh họa tỷ lệ {ratio} để chuyển thành video vẽ tay. "
            + style + direction + "Dùng lời đọc sau làm ngữ cảnh, không chép nguyên văn lên ảnh: " + text)

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


def scene_rows(plan, ratio="9:16", highlights=False, shared_prompt=""):
    return [[i, timecode(start), timecode(end), round((end-start)/24, 3), text, prompt_for(text, ratio, shared_prompt), highlight_text(text) if highlights else ""]
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

def render_folder(voice, pictures, plan, destination, report, ratio="9:16", highlights=False):
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
        for i, ((start, end, text), picture) in enumerate(zip(plan, pictures)):
            report(f"Whiteboard Animation VI: cảnh {i+1}/{len(plan)}…")
            with Image.open(picture) as raw:
                art = ImageOps.contain(ImageOps.exif_transpose(raw).convert("RGB"), (preview_width, preview_height))
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
                           "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", titled)
                    clip = titled
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
    "version": "1.3.17",
    "commit": "65cec03b89b9ab3151dbb47d96e43c7080f6bcf8",
    "files": {
        "worker.js": "b771282f9995bdce3d79a94be64d68aa1aba42ba",
        "manifest.json": "1ccf47d9c85c7050bce92f4fc908c4ca0c6b25f1",
        "popup.html": "d1f3fe3d89500eb7788fc312842525a9c22193c4",
        "flow.js": "0e0b6dafe30f052ed19699f74ae6085d1a1916fa"
    }
}
BUNDLED_EXTENSION_FILES = {
    "worker.js": "Y29uc3QgYmFzZSA9ICJodHRwOi8vMTI3LjAuMC4xOjc4NzciOwpmdW5jdGlvbiBmbG93VGFiKHVybCkgeyByZXR1cm4gL15odHRwczpcL1wvKGZsb3dcLmdvb2dsZVwuY29tfGxhYnNcLmdvb2dsZSlcLy8udGVzdCh1cmwgfHwgIiIpOyB9CmFzeW5jIGZ1bmN0aW9uIGFwaSh7dG9rZW4sIHBvcnR9LCBwYXRoLCBib2R5KSB7CiAgY29uc3QgciA9IGF3YWl0IGZldGNoKCJodHRwOi8vMTI3LjAuMC4xOiIgKyBwb3J0ICsgcGF0aCwgewogICAgaGVhZGVyczogeyJYLVZlVGF5LVRva2VuIjp0b2tlbiwgLi4uKGJvZHkgPyB7IkNvbnRlbnQtVHlwZSI6Ym9keS50eXBlIHx8ICJhcHBsaWNhdGlvbi9qc29uIn0gOiB7fSl9LAogICAgbWV0aG9kOiBib2R5ID8gIlBPU1QiIDogIkdFVCIsIGJvZHkKICB9KTsKICBpZiAoIXIub2spIHRocm93IEVycm9yKChhd2FpdCByLnRleHQoKSkuc2xpY2UoMCwyNTApKTsKICByZXR1cm4gci5qc29uKCk7Cn0KY2hyb21lLnJ1bnRpbWUub25NZXNzYWdlLmFkZExpc3RlbmVyKChtLHNlbmRlcixyZXBseSkgPT4gewogIChhc3luYygpPT57CiAgICBpZiAoIWZsb3dUYWIoc2VuZGVyLnRhYj8udXJsKSkgdGhyb3cgRXJyb3IoIkNo4buJIG5o4bqtbiB5w6p1IGPhuqd1IHThu6sgdGFiIEZsb3cuIik7CiAgICBjb25zdCBrZXkgPSAicGFpcmluZzoiICsgc2VuZGVyLnRhYi5pZDsKICAgIGlmIChtLm9wID09PSAicGFpciIpIHsKICAgICAgaWYgKCEvXlthLWYwLTldezMyfSQvLnRlc3QobS50b2tlbiB8fCAiIikpIHRocm93IEVycm9yKCJNw6Mga+G6v3QgbuG7kWkga2jDtG5nIGjhu6NwIGzhu4cuIik7CiAgICAgIGNvbnN0IHBvcnQgPSBtLnBvcnQgPz8gNzg3NzsKICAgICAgaWYgKCFOdW1iZXIuaXNJbnRlZ2VyKHBvcnQpIHx8IHBvcnQgPCAxMDI0IHx8IHBvcnQgPiA2NTUzNSkgdGhyb3cgRXJyb3IoIkPhu5VuZyBW4bq9IFRheSBraMO0bmcgaOG7o3AgbOG7hy4iKTsKICAgICAgY29uc3QgY29ubmVjdGlvbiA9IHt0b2tlbjptLnRva2VuLCBwb3J0fTsKICAgICAgY29uc3Qgam9iID0gYXdhaXQgYXBpKGNvbm5lY3Rpb24sICIvbmV4dCIpOwogICAgICBpZiAoam9iLmRvbmUpIHRocm93IEVycm9yKCJW4bq9IFRheSBjaMawYSBjw7MgY+G6o25oIGNo4budLiIpOwogICAgICBhd2FpdCBjaHJvbWUuc3RvcmFnZS5zZXNzaW9uLnNldCh7W2tleV06Y29ubmVjdGlvbn0pOwogICAgICByZXR1cm4gam9iOwogICAgfQogICAgY29uc3Qgc2F2ZWQgPSBhd2FpdCBjaHJvbWUuc3RvcmFnZS5zZXNzaW9uLmdldChrZXkpOwogICAgY29uc3QgcGFpcmluZyA9IHR5cGVvZiBzYXZlZFtrZXldID09PSAic3RyaW5nIiA/IHt0b2tlbjpzYXZlZFtrZXldLHBvcnQ6Nzg3N30gOiBzYXZlZFtrZXldOwogICAgaWYgKG0ub3AgPT09ICJyZXN1bWUiKSByZXR1cm4gcGFpcmluZyA/IGFwaShwYWlyaW5nLCIvbmV4dCIpIDoge2RvbmU6dHJ1ZX07CiAgICBpZiAoIXBhaXJpbmcpIHRocm93IEVycm9yKCJUYWIgY2jGsGEgxJHGsOG7o2MgVuG6vSBUYXkga+G6v3QgbuG7kWkuIik7CiAgICBpZiAobS5vcCA9PT0gInN1Ym1pdCIpIHsKICAgICAgY29uc3QgdGFiID0gYXdhaXQgY2hyb21lLnRhYnMuZ2V0KHNlbmRlci50YWIuaWQpOwogICAgICBpZiAoIXRhYi5hY3RpdmUgfHwgc2VuZGVyLmZyYW1lSWQgIT09IDAgfHwgbmV3IFVSTCh0YWIudXJsKS5vcmlnaW4gIT09ICJodHRwczovL2Zsb3cuZ29vZ2xlLmNvbSIKICAgICAgICB8fCAhL15cL3Byb2plY3RcL1teL10rXC8/JC8udGVzdChuZXcgVVJMKHRhYi51cmwpLnBhdGhuYW1lKQogICAgICAgIHx8ICFbbS54LG0ueV0uZXZlcnkoTnVtYmVyLmlzRmluaXRlKSB8fCBtLnggPCAwIHx8IG0ueSA8IDAgfHwgbS54ID4gMTAwMDAgfHwgbS55ID4gMTAwMDApCiAgICAgICAgdGhyb3cgRXJyb3IoIlRhYiBk4buxIMOhbiBGbG93IGtow7RuZyBjw7JuIOG7nyB0cuG6oW5nIHRow6FpIHPhurVuIHPDoG5nLiIpOwogICAgICBjb25zdCBqb2IgPSBhd2FpdCBhcGkocGFpcmluZywiL25leHQiKTsKICAgICAgaWYgKGpvYi5kb25lIHx8IGpvYi5qb2IgIT09IG0uam9iKSB0aHJvdyBFcnJvcigiVMOhYyB24bulIFbhur0gVGF5IMSRw6MgxJHhu5VpLiIpOwogICAgICBjb25zdCB0YXJnZXQgPSB7dGFiSWQ6dGFiLmlkfTsKICAgICAgYXdhaXQgY2hyb21lLmRlYnVnZ2VyLmF0dGFjaCh0YXJnZXQsIjEuMyIpOwogICAgICB0cnkgewogICAgICAgIGNvbnN0IGlucHV0ID0gKHR5cGUsYnV0dG9ucykgPT4gY2hyb21lLmRlYnVnZ2VyLnNlbmRDb21tYW5kKHRhcmdldCwiSW5wdXQuZGlzcGF0Y2hNb3VzZUV2ZW50IiwKICAgICAgICAgIHt0eXBlLHg6bS54LHk6bS55LGJ1dHRvbjoibGVmdCIsYnV0dG9ucyxjbGlja0NvdW50OjF9KTsKICAgICAgICBhd2FpdCBpbnB1dCgibW91c2VQcmVzc2VkIiwxKTsKICAgICAgICBhd2FpdCBpbnB1dCgibW91c2VSZWxlYXNlZCIsMCk7CiAgICAgIH0gZmluYWxseSB7CiAgICAgICAgYXdhaXQgY2hyb21lLmRlYnVnZ2VyLmRldGFjaCh0YXJnZXQpOwogICAgICB9CiAgICAgIHJldHVybiB7Y2xpY2tlZDp0cnVlfTsKICAgIH0KICAgIGlmIChtLm9wID09PSAiYWN0aXZhdGUiKSB7CiAgICAgIGF3YWl0IGNocm9tZS50YWJzLnVwZGF0ZShzZW5kZXIudGFiLmlkLCB7YWN0aXZlOnRydWV9KTsKICAgICAgYXdhaXQgY2hyb21lLndpbmRvd3MudXBkYXRlKHNlbmRlci50YWIud2luZG93SWQsIHtmb2N1c2VkOnRydWV9KTsKICAgICAgcmV0dXJuIHthY3RpdmU6dHJ1ZX07CiAgICB9CiAgICBpZiAobS5vcCA9PT0gIm5leHQiKSByZXR1cm4gYXBpKHBhaXJpbmcsIi9uZXh0Iik7CiAgICBpZiAobS5vcCA9PT0gImJhdGNoIikgcmV0dXJuIGFwaShwYWlyaW5nLCIvYmF0Y2giKTsKICAgIGlmIChtLm9wID09PSAicHJvZ3Jlc3MiKSByZXR1cm4gYXBpKHBhaXJpbmcsIi9wcm9ncmVzcyIsSlNPTi5zdHJpbmdpZnkoe2NvdW50Om0uY291bnQsc3RhZ2U6bS5zdGFnZX0pKTsKICAgIGlmIChtLm9wID09PSAiZXJyb3IiKSByZXR1cm4gYXBpKHBhaXJpbmcsIi9lcnJvciIsSlNPTi5zdHJpbmdpZnkoe21lc3NhZ2U6bS5tZXNzYWdlfSkpOwogICAgaWYgKG0ub3AgPT09ICJpbWFnZSIgJiYgL15bYS1mMC05XXszMn0kLy50ZXN0KG0uam9iKSAmJiBOdW1iZXIuaXNJbnRlZ2VyKG0uaW5kZXgpKSB7CiAgICAgIGxldCBpbWFnZTsKICAgICAgaWYgKG0uZGF0YSAmJiAvXmRhdGE6aW1hZ2VcLyhwbmd8d2VicHxqcGVnKTtiYXNlNjQsLy50ZXN0KG0uZGF0YSkpIHsKICAgICAgICBpbWFnZSA9IGF3YWl0IChhd2FpdCBmZXRjaChtLmRhdGEpKS5ibG9iKCk7CiAgICAgIH0gZWxzZSB7CiAgICAgICAgY29uc3QgdSA9IG5ldyBVUkwobS51cmwpOwogICAgICAgIGlmICh1LnByb3RvY29sICE9PSAiaHR0cHM6IiB8fCAhLyhefFwuKWdvb2dsZXVzZXJjb250ZW50XC5jb20kLy50ZXN0KHUuaG9zdG5hbWUpKSB0aHJvdyBFcnJvcigiS2jDtG5nIHjDoWMgxJHhu4tuaCDEkcaw4bujYyDEkeG7i2EgY2jhu4kg4bqjbmgga+G6v3QgcXXhuqMuIik7CiAgICAgICAgY29uc3QgciA9IGF3YWl0IGZldGNoKHUuaHJlZix7Y3JlZGVudGlhbHM6Im9taXQiLHJlZGlyZWN0OiJlcnJvciJ9KTsKICAgICAgICBpZiAoIXIub2spIHRocm93IEVycm9yKCJLaMO0bmcgdOG6o2kgxJHGsOG7o2Mg4bqjbmgga+G6v3QgcXXhuqMuIik7CiAgICAgICAgaW1hZ2UgPSBhd2FpdCByLmJsb2IoKTsKICAgICAgfQogICAgICBpZiAoaW1hZ2Uuc2l6ZSA+IDI0KjEwMjQqMTAyNCB8fCAhaW1hZ2UudHlwZS5zdGFydHNXaXRoKCJpbWFnZS8iKSkgdGhyb3cgRXJyb3IoIuG6om5oIGvhur90IHF14bqjIGtow7RuZyBo4bujcCBs4buHLiIpOwogICAgICByZXR1cm4gYXBpKHBhaXJpbmcsIi9pbWFnZS8iK20uam9iKyIvIittLmluZGV4LGltYWdlKTsKICAgIH0KICAgIHRocm93IEVycm9yKCJZw6p1IGPhuqd1IGtow7RuZyBo4bujcCBs4buHLiIpOwogIH0pKCkudGhlbihyZXN1bHQ9PnJlcGx5KHtvazp0cnVlLHJlc3VsdH0pLGVycm9yPT5yZXBseSh7b2s6ZmFsc2UsZXJyb3I6ZXJyb3IubWVzc2FnZX0pKTsKICByZXR1cm4gdHJ1ZTsKfSk7CmNocm9tZS50YWJzLm9uUmVtb3ZlZC5hZGRMaXN0ZW5lcih0YWJJZCA9PiBjaHJvbWUuc3RvcmFnZS5zZXNzaW9uLnJlbW92ZSgicGFpcmluZzoiICsgdGFiSWQpKTsKCmNvbnN0IGV4dGVuc2lvbkNoZWNrID0gInZldGF5LWNoZWNrLWxvY2FsLXVwZGF0ZSI7CmZ1bmN0aW9uIHJlZ2lzdGVyQ2hlY2soKSB7CiAgY2hyb21lLmFsYXJtcy5jcmVhdGUoZXh0ZW5zaW9uQ2hlY2ssIHtwZXJpb2RJbk1pbnV0ZXM6MX0pOwp9CmNocm9tZS5ydW50aW1lLm9uSW5zdGFsbGVkLmFkZExpc3RlbmVyKHJlZ2lzdGVyQ2hlY2spOwpjaHJvbWUucnVudGltZS5vblN0YXJ0dXAuYWRkTGlzdGVuZXIocmVnaXN0ZXJDaGVjayk7CmNocm9tZS5hbGFybXMub25BbGFybS5hZGRMaXN0ZW5lcihhc3luYyBhbGFybSA9PiB7CiAgaWYgKGFsYXJtLm5hbWUgIT09IGV4dGVuc2lvbkNoZWNrKSByZXR1cm47CiAgdHJ5IHsKICAgIGNvbnN0IGN1cnJlbnQgPSBjaHJvbWUucnVudGltZS5nZXRNYW5pZmVzdCgpOwogICAgY29uc3QgcmVzcG9uc2UgPSBhd2FpdCBmZXRjaChjaHJvbWUucnVudGltZS5nZXRVUkwoIm1hbmlmZXN0Lmpzb24iKSArICI/dD0iICsgRGF0ZS5ub3coKSwge2NhY2hlOiJuby1zdG9yZSJ9KTsKICAgIGlmICghcmVzcG9uc2Uub2spIHJldHVybjsKICAgIGNvbnN0IGRpc2sgPSBhd2FpdCByZXNwb25zZS5qc29uKCk7CiAgICBpZiAoIS9eXGQrXC5cZCtcLlxkKyQvLnRlc3QoZGlzay52ZXJzaW9uKSB8fCBkaXNrLm5hbWUgIT09IGN1cnJlbnQubmFtZSB8fAogICAgICAgIGRpc2subWFuaWZlc3RfdmVyc2lvbiAhPT0gY3VycmVudC5tYW5pZmVzdF92ZXJzaW9uKSByZXR1cm47CiAgICBjb25zdCBvbGQgPSBjdXJyZW50LnZlcnNpb24uc3BsaXQoIi4iKS5tYXAoTnVtYmVyKTsKICAgIGNvbnN0IG5leHQgPSBkaXNrLnZlcnNpb24uc3BsaXQoIi4iKS5tYXAoTnVtYmVyKTsKICAgIGNvbnN0IGNoYW5nZWQgPSBuZXh0LmZpbmRJbmRleCgodmFsdWUsaSk9PnZhbHVlIT09b2xkW2ldKTsKICAgIGlmIChjaGFuZ2VkIDwgMCB8fCBuZXh0W2NoYW5nZWRdIDwgb2xkW2NoYW5nZWRdKSByZXR1cm47CiAgICBmb3IoY29uc3Qga2V5IG9mIFsicGVybWlzc2lvbnMiLCJob3N0X3Blcm1pc3Npb25zIiwib3B0aW9uYWxfcGVybWlzc2lvbnMiLCJvcHRpb25hbF9ob3N0X3Blcm1pc3Npb25zIl0pIHsKICAgICAgaWYoSlNPTi5zdHJpbmdpZnkoWy4uLihjdXJyZW50W2tleV18fFtdKV0uc29ydCgpKSE9PUpTT04uc3RyaW5naWZ5KFsuLi4oZGlza1trZXldfHxbXSldLnNvcnQoKSkpIHJldHVybjsKICAgIH0KICAgIGNvbnN0IG1hdGNoZXMgPSB2YWx1ZSA9PiAodmFsdWUuY29udGVudF9zY3JpcHRzIHx8IFtdKS5mbGF0TWFwKHNjcmlwdD0+c2NyaXB0Lm1hdGNoZXMgfHwgW10pLnNvcnQoKTsKICAgIGlmKEpTT04uc3RyaW5naWZ5KG1hdGNoZXMoY3VycmVudCkpIT09SlNPTi5zdHJpbmdpZnkobWF0Y2hlcyhkaXNrKSkpIHJldHVybjsKICAgIGNvbnN0IGlkbGUgPSBhd2FpdCBmZXRjaChiYXNlKyIvZXh0ZW5zaW9uLWlkbGUiLHtjYWNoZToibm8tc3RvcmUifSk7CiAgICBpZihpZGxlLm9rICYmIChhd2FpdCBpZGxlLmpzb24oKSkuaWRsZSA9PT0gdHJ1ZSkgY2hyb21lLnJ1bnRpbWUucmVsb2FkKCk7CiAgfSBjYXRjaCB7fQp9KTsK",
    "manifest.json": "ewogICJtYW5pZmVzdF92ZXJzaW9uIjogMywKICAibmFtZSI6ICJW4bq9IFRheSDCtyBHb29nbGUgRmxvdyIsCiAgInZlcnNpb24iOiAiMS4zLjE3IiwKICAiYWN0aW9uIjogewogICAgImRlZmF1bHRfcG9wdXAiOiAicG9wdXAuaHRtbCIKICB9LAogICJiYWNrZ3JvdW5kIjogewogICAgInNlcnZpY2Vfd29ya2VyIjogIndvcmtlci5qcyIKICB9LAogICJwZXJtaXNzaW9ucyI6IFsKICAgICJzdG9yYWdlIiwKICAgICJhbGFybXMiLAogICAgImRlYnVnZ2VyIgogIF0sCiAgImhvc3RfcGVybWlzc2lvbnMiOiBbCiAgICAiaHR0cDovLzEyNy4wLjAuMS8qIiwKICAgICJodHRwczovLyouZ29vZ2xldXNlcmNvbnRlbnQuY29tLyoiLAogICAgImh0dHBzOi8vZmxvdy5nb29nbGUuY29tLyoiLAogICAgImh0dHBzOi8vbGFicy5nb29nbGUvKiIKICBdLAogICJjb250ZW50X3NjcmlwdHMiOiBbCiAgICB7CiAgICAgICJtYXRjaGVzIjogWwogICAgICAgICJodHRwczovL2Zsb3cuZ29vZ2xlLmNvbS8qIiwKICAgICAgICAiaHR0cHM6Ly9sYWJzLmdvb2dsZS8qIgogICAgICBdLAogICAgICAianMiOiBbCiAgICAgICAgImZsb3cuanMiCiAgICAgIF0sCiAgICAgICJydW5fYXQiOiAiZG9jdW1lbnRfc3RhcnQiCiAgICB9CiAgXQp9Cg==",
    "popup.html": "PCFkb2N0eXBlIGh0bWw+PGh0bWwgbGFuZz0idmkiPjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij48c3R5bGU+Ym9keXtmb250OjE0cHggc3lzdGVtLXVpO3dpZHRoOjI3MHB4O3BhZGRpbmc6MTRweH08L3N0eWxlPjxiPlbhur0gVGF5IMK3IEdvb2dsZSBGbG93PC9iPjxwPlRyb25nIFbhur0gVGF5LCBi4bqlbSBU4buxIHThuqFvIHZpZGVvIMSR4buDIOG7qW5nIGThu6VuZyBt4bufIHRhYiBGbG93IHbDoCB04buxIGvhur90IG7hu5FpLjwvcD48L2h0bWw+Cg==",
    "flow.js": "KCgpID0+IHsKICBjb25zdCBwYWlyaW5nID0gL14jdmV0YXk9KFthLWYwLTldezMyfSkoPzotKFswLTldezQsNX0pKT8kLy5leGVjKGxvY2F0aW9uLmhhc2gpOwogIGlmICh3aW5kb3cuX192ZVRheVJ1bm5pbmcpIHJldHVybjsKICB3aW5kb3cuX192ZVRheVJ1bm5pbmcgPSB0cnVlOwogIGNvbnN0IHNsZWVwID0gKG1zKSA9PiBuZXcgUHJvbWlzZShyZXNvbHZlID0+IHNldFRpbWVvdXQocmVzb2x2ZSxtcykpOwogIGNvbnN0IHZpc2libGUgPSBub2RlID0+IG5vZGUuZ2V0Q2xpZW50UmVjdHMoKS5sZW5ndGggJiYgZ2V0Q29tcHV0ZWRTdHlsZShub2RlKS52aXNpYmlsaXR5ICE9PSAiaGlkZGVuIjsKICBjb25zdCBzZW5kID0gYXN5bmMgKG1lc3NhZ2UpID0+IHsKICAgIGNvbnN0IHJlc3BvbnNlID0gYXdhaXQgY2hyb21lLnJ1bnRpbWUuc2VuZE1lc3NhZ2UobWVzc2FnZSk7CiAgICBpZiAoIXJlc3BvbnNlPy5vaykgdGhyb3cgRXJyb3IocmVzcG9uc2U/LmVycm9yIHx8ICJN4bqldCBr4bq/dCBu4buRaSB24bubaSBW4bq9IFRheS4iKTsKICAgIHJldHVybiByZXNwb25zZS5yZXN1bHQ7CiAgfTsKICBmdW5jdGlvbiByZXN1bHRJbWFnZXMoY2FyZCkgewogICAgcmV0dXJuIFsuLi5jYXJkLnF1ZXJ5U2VsZWN0b3JBbGwoImltZyIpXS5maWx0ZXIoaW1nID0+IHsKICAgICAgaWYgKCFpbWcuY29tcGxldGUgfHwgIWltZy5uYXR1cmFsV2lkdGggfHwgIWltZy5uYXR1cmFsSGVpZ2h0IHx8ICF2aXNpYmxlKGltZykpIHJldHVybiBmYWxzZTsKICAgICAgY29uc3Qgc3JjID0gaW1nLmN1cnJlbnRTcmMgfHwgaW1nLnNyYzsKICAgICAgY29uc3QgdXJsID0gbmV3IFVSTChzcmMsIGxvY2F0aW9uLmhyZWYpOwogICAgICBjb25zdCBtZWRpYSA9IC9eKMOUIGhp4buDbiB0aOG7iyBow6xuaCDhuqNuaCBj4bunYSBuZ8aw4budaSBkw7luZ3xVc2VyIGltYWdlIHRpbGUpJC9pdS50ZXN0KGltZy5hbHQudHJpbSgpKTsKICAgICAgY29uc3Qgb3JpZ2luYWwgPSAvXihibG9iOnxodHRwczpcL1wvW14vXSpcLmdvb2dsZXVzZXJjb250ZW50XC5jb21cLykvaS50ZXN0KHNyYyk7CiAgICAgIHJldHVybiBvcmlnaW5hbCB8fCAobWVkaWEgJiYgWyJodHRwczoiLCAiZGF0YToiLCAiYmxvYjoiXS5pbmNsdWRlcyh1cmwucHJvdG9jb2wpKTsKICAgIH0pOwogIH0KICBjb25zdCBjYXB0aW9uID0gYnV0dG9uID0+IChidXR0b24uZ2V0QXR0cmlidXRlKCJhcmlhLWxhYmVsIikgfHwgYnV0dG9uLmdldEF0dHJpYnV0ZSgidGl0bGUiKSB8fCBidXR0b24udGV4dENvbnRlbnQgfHwgIiIpLnRyaW0oKTsKICBmdW5jdGlvbiBwcm9tcHRGaWVsZCgpIHsKICAgIGNvbnN0IGZpZWxkcyA9IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCd0ZXh0YXJlYSxbY29udGVudGVkaXRhYmxlPSJ0cnVlIl0sW2NvbnRlbnRlZGl0YWJsZT0icGxhaW50ZXh0LW9ubHkiXScpXQogICAgICAuZmlsdGVyKGZpZWxkID0+IHZpc2libGUoZmllbGQpICYmICFmaWVsZC5jbG9zZXN0KCdbaW5lcnRdLFthcmlhLWhpZGRlbj0idHJ1ZSJdLFtyb2xlPSJzZWFyY2giXScpCiAgICAgICAgJiYgIWZpZWxkLmRpc2FibGVkICYmICFmaWVsZC5yZWFkT25seSAmJiBmaWVsZC5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSAhPT0gInRydWUiKTsKICAgIGNvbnN0IGxhYmVscyA9IGZpZWxkID0+IFsiYXJpYS1sYWJlbCIsICJwbGFjZWhvbGRlciIsICJkYXRhLXBsYWNlaG9sZGVyIiwgInRpdGxlIl0KICAgICAgLm1hcChuYW1lID0+IGZpZWxkLmdldEF0dHJpYnV0ZShuYW1lKSB8fCAiIikuam9pbigiICIpOwogICAgY29uc3QgZWxpZ2libGUgPSBmaWVsZHMuZmlsdGVyKGZpZWxkID0+ICEvc2VhcmNofHTDrG0ga2nhur9tfHJlbmFtZXzEkeG7lWkgdMOqbnxwcm9qZWN0IG5hbWV8dMOqbiBk4buxIMOhbi9pLnRlc3QobGFiZWxzKGZpZWxkKSkKICAgICAgJiYgIWZpZWxkcy5zb21lKHBhcmVudCA9PiBwYXJlbnQgIT09IGZpZWxkICYmIHBhcmVudC5jb250YWlucyhmaWVsZCkpKTsKICAgIGNvbnN0IG1hdGNoZXMgPSBlbGlnaWJsZS5maWx0ZXIoZmllbGQgPT4gL3Byb21wdHx3aGF0ICg/OmRvfHdvdWxkKSB5b3V8ZGVzY3JpYmV8bcO0IHThuqN8YuG6oW4gbXXhu5FufHThuqFvIG7hu5lpIGR1bmcvaS50ZXN0KGxhYmVscyhmaWVsZCkpKTsKICAgIGlmIChtYXRjaGVzLmxlbmd0aCA9PT0gMSkgcmV0dXJuIG1hdGNoZXNbMF07CiAgICByZXR1cm4gZWxpZ2libGUubGVuZ3RoID09PSAxID8gZWxpZ2libGVbMF0gOiBudWxsOwogIH0KICBjb25zdCByYXRpb0xhYmVsID0gcmF0aW8gPT4gbmV3IFJlZ0V4cCgiKD86XnxbXjAtOV0pIiArIHJhdGlvLnJlcGxhY2UoIjoiLCAiXFxzKls64oi2XVxccyoiKSArICIoPyFbMC05XSkiLCAidSIpOwogIGFzeW5jIGZ1bmN0aW9uIHNlbGVjdFJhdGlvKHJhdGlvKSB7CiAgICBjb25zdCBzZWxlY3RlZCA9IG5vZGUgPT4gWyJhcmlhLWNoZWNrZWQiLCAiYXJpYS1zZWxlY3RlZCIsICJhcmlhLXByZXNzZWQiXS5zb21lKGtleSA9PiBub2RlLmdldEF0dHJpYnV0ZShrZXkpID09PSAidHJ1ZSIpCiAgICAgIHx8IG5vZGUuZ2V0QXR0cmlidXRlKCJkYXRhLXN0YXRlIikgPT09ICJjaGVja2VkIiB8fCBub2RlLmdldEF0dHJpYnV0ZSgiZGF0YS1zdGF0ZSIpID09PSAiYWN0aXZlIjsKICAgIGNvbnN0IGNvbnRyb2xzID0gKCkgPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoImJ1dHRvbixbcm9sZT1idXR0b25dLFtyb2xlPXJhZGlvXSxbcm9sZT1vcHRpb25dLFtyb2xlPW1lbnVpdGVtcmFkaW9dLFtyb2xlPXRhYl0iKV0KICAgICAgLmZpbHRlcihub2RlID0+IHZpc2libGUobm9kZSkgJiYgIW5vZGUuZGlzYWJsZWQgJiYgbm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSAhPT0gInRydWUiKTsKICAgIGNvbnN0IGxhYmVscyA9IG5vZGUgPT4gW2NhcHRpb24obm9kZSksIG5vZGUuZ2V0QXR0cmlidXRlKCJkYXRhLXZhbHVlIikgfHwgIiIsIG5vZGUuaW5uZXJUZXh0IHx8ICIiXS5qb2luKCIgIik7CiAgICBjb25zdCBleGFjdCA9IHJhdGlvTGFiZWwocmF0aW8pOwogICAgY29uc3QgaWNvbk5hbWUgPSAiY3JvcF8iICsgcmF0aW8ucmVwbGFjZSgiOiIsICJfIik7CiAgICBjb25zdCBtYXRjaGVzUmF0aW8gPSBub2RlID0+IGV4YWN0LnRlc3QobGFiZWxzKG5vZGUpKSB8fCBbLi4ubm9kZS5xdWVyeVNlbGVjdG9yQWxsKCJtYXQtaWNvbixbcm9sZT1pbWddLHN2ZyIpXQogICAgICAuc29tZShpY29uID0+IFtpY29uLnRleHRDb250ZW50LCBpY29uLmdldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIpLCBpY29uLmdldEF0dHJpYnV0ZSgiZGF0YS1pY29uIildCiAgICAgICAgLnNvbWUobGFiZWwgPT4gbGFiZWw/LnRyaW0oKSA9PT0gaWNvbk5hbWUpKTsKICAgIGNvbnN0IGhhc1JhdGlvID0gcmF0aW9MYWJlbCgiWzAtOV0rOlswLTldKyIpOwogICAgY29uc3QgdHJpZ2dlciA9IGNvbnRyb2xzKCkuZmlsdGVyKG5vZGUgPT4gL2FzcGVjdCByYXRpb3x04bu3IGzhu4d8dOG7iSBs4buHfGNyb3BfKD86bGFuZHNjYXBlfHBvcnRyYWl0KS9pdS50ZXN0KGxhYmVscyhub2RlKSkKICAgICAgfHwgKG5vZGUuZ2V0QXR0cmlidXRlKCJhcmlhLWhhc3BvcHVwIikgJiYgaGFzUmF0aW8udGVzdChsYWJlbHMobm9kZSkpKSk7CiAgICBjb25zdCBvcHRpb25zID0gKCkgPT4gY29udHJvbHMoKS5maWx0ZXIobm9kZSA9PiAhdHJpZ2dlci5pbmNsdWRlcyhub2RlKSAmJiBub2RlICE9PSBzZXR0aW5ncyAmJiBtYXRjaGVzUmF0aW8obm9kZSkpOwogICAgY29uc3QgbW9kZWxTZXR0aW5ncyA9IGNvbnRyb2xzKCkuZmlsdGVyKG5vZGUgPT4gL05hbm8gQmFuYW5hfEltYWdlbi9pdS50ZXN0KGxhYmVscyhub2RlKSkgJiYgL1t4w5ddWzEtNF1cYi9pdS50ZXN0KGxhYmVscyhub2RlKSkpOwogICAgY29uc3Qgc2V0dGluZ3MgPSBjb250cm9scygpLmZpbmQobm9kZSA9PiAvXijEkGnhu4F1IGtp4buHbiBrw61jaCBob+G6oXQgY8OgaSDEkeG6t3R8U2V0dGluZ3MgdHJpZ2dlcnxHZW5lcmF0aW9uIHNldHRpbmdzKSQvaXUudGVzdChjYXB0aW9uKG5vZGUpKSkKICAgICAgfHwgKG1vZGVsU2V0dGluZ3MubGVuZ3RoID09PSAxID8gbW9kZWxTZXR0aW5nc1swXSA6IG51bGwpOwogICAgbGV0IG9wZW5lZCA9IGZhbHNlOwogICAgaWYgKCFvcHRpb25zKCkubGVuZ3RoICYmIHNldHRpbmdzKSB7CiAgICAgIHNldHRpbmdzLmNsaWNrKCk7CiAgICAgIG9wZW5lZCA9IHRydWU7CiAgICAgIGNvbnN0IGltYWdlID0gYXdhaXQgd2FpdEZvcigoKSA9PiBjb250cm9scygpLmZpbmQobm9kZSA9PiBub2RlLmdldEF0dHJpYnV0ZSgicm9sZSIpID09PSAicmFkaW8iICYmIC9eKEltYWdlfEjDrG5oIOG6o25ofOG6om5oKSQvaXUudGVzdChjYXB0aW9uKG5vZGUpKSksIDMpOwogICAgICBpZiAoaW1hZ2UgJiYgIXNlbGVjdGVkKGltYWdlKSkgaW1hZ2UuY2xpY2soKTsKICAgIH0KICAgIGlmICghb3B0aW9ucygpLmxlbmd0aCkgYXdhaXQgd2FpdEZvcigoKSA9PiBvcHRpb25zKCkubGVuZ3RoLCAzKTsKICAgIGlmICghb3B0aW9ucygpLmxlbmd0aCAmJiB0cmlnZ2VyLmxlbmd0aCA9PT0gMSkgdHJpZ2dlclswXS5jbGljaygpOwogICAgY29uc3Qgb3B0aW9uID0gYXdhaXQgd2FpdEZvcigoKSA9PiB7CiAgICAgIGlmICh0cmlnZ2VyLmxlbmd0aCA9PT0gMSAmJiBleGFjdC50ZXN0KGxhYmVscyh0cmlnZ2VyWzBdKSkgJiYgIW9wdGlvbnMoKS5sZW5ndGgpIHJldHVybiB0cmlnZ2VyWzBdOwogICAgICBjb25zdCBtYXRjaGVzID0gb3B0aW9ucygpOwogICAgICBjb25zdCByYWRpb3MgPSBtYXRjaGVzLmZpbHRlcihub2RlID0+IFsicmFkaW8iLCAib3B0aW9uIiwgIm1lbnVpdGVtcmFkaW8iXS5pbmNsdWRlcyhub2RlLmdldEF0dHJpYnV0ZSgicm9sZSIpKSk7CiAgICAgIHJldHVybiByYWRpb3MubGVuZ3RoID09PSAxID8gcmFkaW9zWzBdIDogbWF0Y2hlcy5sZW5ndGggPT09IDEgPyBtYXRjaGVzWzBdIDogbnVsbDsKICAgIH0sIDEyKTsKICAgIGlmICghb3B0aW9uKSB0aHJvdyBFcnJvcigiS2jDtG5nIHTDrG0gdGjhuqV5IHThu7cgbOG7hyAiICsgcmF0aW8gKyAiIHRyb25nIEdvb2dsZSBGbG93LiIpOwogICAgaWYgKCF0cmlnZ2VyLmluY2x1ZGVzKG9wdGlvbikgJiYgIXNlbGVjdGVkKG9wdGlvbikpIG9wdGlvbi5jbGljaygpOwogICAgY29uc3Qgc2luZ2xlID0gY29udHJvbHMoKS5maWx0ZXIobm9kZSA9PiAvXlt4w5ddXHMqMSQvaXUudGVzdChjYXB0aW9uKG5vZGUpKSk7CiAgICBpZiAoc2luZ2xlLmxlbmd0aCA9PT0gMSAmJiAhc2VsZWN0ZWQoc2luZ2xlWzBdKSkgc2luZ2xlWzBdLmNsaWNrKCk7CiAgICBpZiAob3BlbmVkICYmIG9wdGlvbnMoKS5sZW5ndGggJiYgc2V0dGluZ3MuaXNDb25uZWN0ZWQpIHNldHRpbmdzLmNsaWNrKCk7CiAgfQogIGFzeW5jIGZ1bmN0aW9uIHJ1bigpIHsKICAgIGxldCBhcHBsaWVkUmF0aW87CiAgICBjb25zdCBiYXRjaCA9IGF3YWl0IHNlbmQoe29wOiJiYXRjaCJ9KTsKICAgIGlmIChiYXRjaC5kb25lKSByZXR1cm47CiAgICBjb25zdCBxdWV1ZWQgPSBbXTsKICAgIGNvbnN0IHJlc3VsdHMgPSBbXTsKICAgIGNvbnN0IGNhcmRzID0gKCkgPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tyb2xlPSJncm91cCJdW2FyaWEtbGFiZWxdJyldOwogICAgY29uc3Qgbm9ybWFsaXplID0gdGV4dCA9PiAodGV4dCB8fCAiIikucmVwbGFjZSgvXHMrL2d1LCAiICIpLnRyaW0oKTsKICAgIGNvbnN0IGluaXRpYWxDYXJkcyA9IGNhcmRzKCk7CiAgICBsZXQgbmV3ZXN0Rmlyc3QgPSB0cnVlOwogICAgZm9yIChjb25zdCBbb2Zmc2V0LCBwcm9tcHRdIG9mIGJhdGNoLnByb21wdHMuZW50cmllcygpKSB7CiAgICAgIGNvbnN0IGpvYiA9IHtqb2I6YmF0Y2guam9iLCBpbmRleDpiYXRjaC5pbmRleCArIG9mZnNldCwgcHJvbXB0fTsKICAgICAgY29uc3QgYWdlbnQgPSBbLi4uZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW3JvbGU9ImNoZWNrYm94Il0saW5wdXRbdHlwZT0iY2hlY2tib3giXScpXS5maW5kKG5vZGUgPT4gdmlzaWJsZShub2RlKQogICAgICAgICYmIC9eKFTDoWMgbmjDom58QWdlbnQpJC9pdS50ZXN0KChub2RlLmdldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIpIHx8IG5vZGUudGV4dENvbnRlbnQgfHwgIiIpLnRyaW0oKSkKICAgICAgICAmJiAobm9kZS5jaGVja2VkIHx8IG5vZGUuZ2V0QXR0cmlidXRlKCJhcmlhLWNoZWNrZWQiKSA9PT0gInRydWUiKSk7CiAgICAgIGlmIChhZ2VudCkgeyBhZ2VudC5jbGljaygpOyBhd2FpdCBzbGVlcCgzMDApOyB9CiAgICAgIGxldCBmaWVsZCA9IGF3YWl0IHdhaXRGb3IocHJvbXB0RmllbGQsIDI1KTsKICAgICAgaWYgKCFmaWVsZCkgdGhyb3cgRXJyb3IoIktow7RuZyB4w6FjIMSR4buLbmggxJHGsOG7o2Mgw7QgcHJvbXB0IGR1eSBuaOG6pXQuIE3hu58gdHJhbmcgdOG6oW8g4bqjbmggdHJvbmcgZOG7sSDDoW4gRmxvdy4iKTsKICAgICAgYXdhaXQgc2VuZCh7b3A6ImFjdGl2YXRlIn0pOwogICAgICBjb25zdCByYXRpbyA9IC9eVOG6oW8gbeG7mXQg4bqjbmggbWluaCBo4buNYSB04bu3IGzhu4cgKFxkKzpcZCspIC91LmV4ZWMoam9iLnByb21wdCk/LlsxXTsKICAgICAgaWYgKHJhdGlvICYmIHJhdGlvICE9PSBhcHBsaWVkUmF0aW8pIHsKICAgICAgICBhd2FpdCBzZWxlY3RSYXRpbyhyYXRpbyk7CiAgICAgICAgYXBwbGllZFJhdGlvID0gcmF0aW87CiAgICAgICAgZmllbGQgPSBhd2FpdCB3YWl0Rm9yKHByb21wdEZpZWxkLCAxMCk7CiAgICAgICAgaWYgKCFmaWVsZCkgdGhyb3cgRXJyb3IoIktow7RuZyB0aOG6pXkgw7QgcHJvbXB0IHNhdSBraGkgY2jhu41uIHThu7cgbOG7hy4iKTsKICAgICAgfQogICAgICBpZiAoIWF3YWl0IHdhaXRGb3IoKCkgPT4gZG9jdW1lbnQuaGFzRm9jdXMoKSAmJiAhZG9jdW1lbnQuaGlkZGVuLCA2KSkKICAgICAgICB0aHJvdyBFcnJvcigiQ2hyb21lIGNoxrBhIMSRxrBhIHRhYiBGbG93IGzDqm4gdHLGsOG7m2MgxJHhu4MgZ+G7rWkgcHJvbXB0LiIpOwogICAgICBmaWVsZC5mb2N1cygpOwogICAgICBpZiAoZmllbGQgaW5zdGFuY2VvZiBIVE1MVGV4dEFyZWFFbGVtZW50KSB7CiAgICAgICAgT2JqZWN0LmdldE93blByb3BlcnR5RGVzY3JpcHRvcihIVE1MVGV4dEFyZWFFbGVtZW50LnByb3RvdHlwZSwidmFsdWUiKS5zZXQuY2FsbChmaWVsZCxqb2IucHJvbXB0KTsKICAgICAgICBmaWVsZC5kaXNwYXRjaEV2ZW50KG5ldyBJbnB1dEV2ZW50KCJpbnB1dCIse2J1YmJsZXM6dHJ1ZSxkYXRhOmpvYi5wcm9tcHQsaW5wdXRUeXBlOiJpbnNlcnRUZXh0In0pKTsKICAgICAgfSBlbHNlIHsKICAgICAgICBjb25zdCBzZWxlY3Rpb24gPSB3aW5kb3cuZ2V0U2VsZWN0aW9uKCk7CiAgICAgICAgY29uc3QgcmFuZ2UgPSBkb2N1bWVudC5jcmVhdGVSYW5nZSgpOwogICAgICAgIHJhbmdlLnNlbGVjdE5vZGVDb250ZW50cyhmaWVsZCk7CiAgICAgICAgc2VsZWN0aW9uLnJlbW92ZUFsbFJhbmdlcygpOwogICAgICAgIHNlbGVjdGlvbi5hZGRSYW5nZShyYW5nZSk7CiAgICAgICAgaWYgKCFkb2N1bWVudC5leGVjQ29tbWFuZCgiaW5zZXJ0VGV4dCIsZmFsc2Usam9iLnByb21wdCkpIHRocm93IEVycm9yKCJLaMO0bmcgxJFp4buBbiDEkcaw4bujYyBwcm9tcHQgdHJvbmcgRmxvdy4iKTsKICAgICAgfQogICAgICBhd2FpdCBzbGVlcCg1MDApOwogICAgICBjb25zdCBidXR0b25zID0gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoImJ1dHRvbixbcm9sZT1idXR0b25dIildLmZpbHRlcihiPT52aXNpYmxlKGIpJiYhYi5kaXNhYmxlZCYmYi5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSE9PSJ0cnVlIik7CiAgICAgIGNvbnN0IHN0YXJ0ID0gYnV0dG9ucy5maWx0ZXIoYnV0dG9uID0+IC9eKELhuq90IMSR4bqndSB04bqhb3xTdGFydCBnZW5lcmF0aW5nfFN0YXJ0IGNyZWF0aW5nKSQvaXUudGVzdChjYXB0aW9uKGJ1dHRvbikpKTsKICAgICAgbGV0IGdlbmVyYXRlID0gc3RhcnQubGVuZ3RoID09PSAxID8gc3RhcnQgOiBidXR0b25zLmZpbHRlcihiPT4vXihHZW5lcmF0ZSBJbWFnZXxDcmVhdGUgSW1hZ2V8VOG6oW8gaMOsbmgg4bqjbmh8VOG6oW8g4bqjbmgpJC9pLnRlc3QoY2FwdGlvbihiKSkpOwogICAgICBpZiAoZ2VuZXJhdGUubGVuZ3RoID09PSAwICYmIG1hdGNoaW5nKC9OYW5vIEJhbmFuYXxJbWFnZW4vaSkubGVuZ3RoID09PSAxKQogICAgICAgIGdlbmVyYXRlID0gYnV0dG9ucy5maWx0ZXIoYj0+L14oR2VuZXJhdGV8VOG6oW8pJC9pLnRlc3QoY2FwdGlvbihiKSkpOwogICAgICBpZiAoZ2VuZXJhdGUubGVuZ3RoID09PSAwKSB7CiAgICAgICAgY29uc3QgZWRpdG9yID0gZmllbGQuY2xvc2VzdCgiZm9ybSIpIHx8IGZpZWxkLnBhcmVudEVsZW1lbnQ7CiAgICAgICAgZ2VuZXJhdGUgPSBidXR0b25zLmZpbHRlcihidXR0b24gPT4gL14oYXJyb3dfZm9yd2FyZHxzZW5kfGfhu61pfGLhuq90IMSR4bqndSB04bqhb3xzdGFydCBnZW5lcmF0aW5nfHN0YXJ0IGNyZWF0aW5nKSQvaXUudGVzdChjYXB0aW9uKGJ1dHRvbikpCiAgICAgICAgICAmJiAoZWRpdG9yPy5jb250YWlucyhidXR0b24pIHx8IGZpZWxkLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpLmJvdHRvbSA8PSBidXR0b24uZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCkuYm90dG9tCiAgICAgICAgICAgICYmIE1hdGguYWJzKGZpZWxkLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpLnJpZ2h0IC0gYnV0dG9uLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpLnJpZ2h0KSA8IDEwMCkpOwogICAgICB9CiAgICAgIGlmIChnZW5lcmF0ZS5sZW5ndGggIT09IDEpIHRocm93IEVycm9yKCJLaMO0bmcgbmjhuq1uIGRp4buHbiDEkcaw4bujYyBuw7p0IHThuqFvIOG6o25oLiBLaeG7g20gdHJhIGNo4bq/IMSR4buZIOG6om5oLCBtb2RlbCB2w6AgaOG6oW4gbeG7qWMgRmxvdy4iKTsKICAgICAgY29uc3QgcHJpb3IgPSBuZXcgU2V0KGNhcmRzKCkpOwogICAgICBjb25zdCBlZGl0b3JCb3ggPSBmaWVsZC5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKTsKICAgICAgY29uc3QgbmVhcmJ5QXJyb3dzID0gYnV0dG9ucy5maWx0ZXIoYnV0dG9uID0+IHsKICAgICAgICBjb25zdCBib3ggPSBidXR0b24uZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCk7CiAgICAgICAgcmV0dXJuIGJveC50b3AgPj0gZWRpdG9yQm94LmJvdHRvbSAtIDggJiYgYm94LnRvcCA8PSBlZGl0b3JCb3guYm90dG9tICsgMTAwCiAgICAgICAgICAmJiBNYXRoLmFicyhib3gucmlnaHQgLSBlZGl0b3JCb3gucmlnaHQpIDwgMTAwCiAgICAgICAgICAmJiAoWy4uLmJ1dHRvbi5xdWVyeVNlbGVjdG9yQWxsKCJtYXQtaWNvbixbZGF0YS1pY29uXSxbYXJpYS1oaWRkZW49dHJ1ZV0saW1nIildLnNvbWUoaWNvbiA9PgogICAgICAgICAgICAvXihhcnJvd19mb3J3YXJkfHNlbmQpJC9pLnRlc3QoKGljb24uZ2V0QXR0cmlidXRlKCJkYXRhLWljb24iKSB8fCBpY29uLmdldEF0dHJpYnV0ZSgiYWx0IikgfHwgaWNvbi50ZXh0Q29udGVudCB8fCAiIikudHJpbSgpKSkKICAgICAgICAgICAgfHwgL14oQuG6r3QgxJHhuqd1IHThuqFvfFN0YXJ0IGdlbmVyYXRpbmd8U3RhcnQgY3JlYXRpbmcpJC9pdS50ZXN0KGNhcHRpb24oYnV0dG9uKSkpOwogICAgICB9KTsKICAgICAgY29uc3Qgc3VibWl0ID0gbmVhcmJ5QXJyb3dzLmxlbmd0aCA9PT0gMSA/IG5lYXJieUFycm93c1swXSA6IGdlbmVyYXRlWzBdOwogICAgICBzdWJtaXQuc2Nyb2xsSW50b1ZpZXcoe2Jsb2NrOiJuZWFyZXN0IiwgaW5saW5lOiJuZWFyZXN0In0pOwogICAgICBjb25zdCBib3ggPSBzdWJtaXQuZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCk7CiAgICAgIGNvbnN0IHggPSBib3gubGVmdCArIGJveC53aWR0aCAvIDI7CiAgICAgIGNvbnN0IHkgPSBib3gudG9wICsgYm94LmhlaWdodCAvIDI7CiAgICAgIGlmICghc3VibWl0LmNvbnRhaW5zKGRvY3VtZW50LmVsZW1lbnRGcm9tUG9pbnQoeCx5KSkpIHRocm93IEVycm9yKCJOw7p0IHThuqFvIOG6o25oIMSRYW5nIGLhu4sgY2hlLiIpOwogICAgICBhd2FpdCBzZW5kKHtvcDoic3VibWl0Iiwgam9iOmpvYi5qb2IsIHgsIHl9KTsKICAgICAgY29uc3QgZmluZENhcmQgPSAoKSA9PiB7CiAgICAgICAgY29uc3QgbWF0Y2hlcyA9IGNhcmRzKCkuZmlsdGVyKGNhcmQgPT4gIXByaW9yLmhhcyhjYXJkKSAmJgogICAgICAgICAgKG5vcm1hbGl6ZShjYXJkLmdldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIpKSA9PT0gbm9ybWFsaXplKHByb21wdCkKICAgICAgICAgICB8fCBub3JtYWxpemUoY2FyZC50ZXh0Q29udGVudCkuaW5jbHVkZXMobm9ybWFsaXplKHByb21wdCkpKSk7CiAgICAgICAgcmV0dXJuIG1hdGNoZXMubGVuZ3RoID09PSAxID8gbWF0Y2hlc1swXSA6IG51bGw7CiAgICAgIH07CiAgICAgIGNvbnN0IGNhcmQgPSBhd2FpdCB3YWl0Rm9yKGZpbmRDYXJkLCAyMCk7CiAgICAgIGlmICghY2FyZCkgdGhyb3cgRXJyb3IoIkZsb3cgY2jGsGEgeMOhYyBuaOG6rW4gxJHDoyBuaOG6rW4gcHJvbXB0IGPhuqNuaCAiICsgKGpvYi5pbmRleCArIDEpICsgIi4iKTsKICAgICAgY29uc3QgY3VycmVudENhcmRzID0gY2FyZHMoKTsKICAgICAgbmV3ZXN0Rmlyc3QgJiY9IGN1cnJlbnRDYXJkcy5sZW5ndGggPT09IGluaXRpYWxDYXJkcy5sZW5ndGggKyBxdWV1ZWQubGVuZ3RoICsgMQogICAgICAgICYmIGN1cnJlbnRDYXJkc1swXSA9PT0gY2FyZDsKICAgICAgcXVldWVkLnB1c2goe2pvYiwgY2FyZCwgZmluZENhcmR9KTsKICAgICAgYXdhaXQgc2VuZCh7b3A6InByb2dyZXNzIiwgY291bnQ6cXVldWVkLmxlbmd0aCwgc3RhZ2U6InN1Ym1pdHRlZCJ9KTsKICAgIH0KICAgIGNvbnN0IGNsYWltZWQgPSBuZXcgU2V0KCk7CiAgICBmb3IgKGNvbnN0IHtqb2IsIGNhcmQsIGZpbmRDYXJkfSBvZiBxdWV1ZWQpIHsKICAgICAgY29uc3QgcmVzdWx0ID0gYXdhaXQgd2FpdEZvcigoKSA9PiB7CiAgICAgICAgY29uc3QgZ3JpZCA9IGNhcmRzKCk7CiAgICAgICAgY29uc3QgcmVwbGFjZW1lbnQgPSBuZXdlc3RGaXJzdCAmJiBpbml0aWFsQ2FyZHMubGVuZ3RoID09PSAwICYmIGdyaWQubGVuZ3RoID09PSBxdWV1ZWQubGVuZ3RoCiAgICAgICAgICAmJiBncmlkLmV2ZXJ5KGl0ZW0gPT4gcmVzdWx0SW1hZ2VzKGl0ZW0pLmxlbmd0aCA9PT0gMSkKICAgICAgICAgID8gZ3JpZFtxdWV1ZWQubGVuZ3RoIC0gMSAtIHF1ZXVlZC5maW5kSW5kZXgoZW50cnkgPT4gZW50cnkuY2FyZCA9PT0gY2FyZCldIDogbnVsbDsKICAgICAgICBjb25zdCBjdXJyZW50ID0gKGNhcmQuaXNDb25uZWN0ZWQgJiYgcmVzdWx0SW1hZ2VzKGNhcmQpLmxlbmd0aCA/IGNhcmQgOiBmaW5kQ2FyZCgpKSB8fCByZXBsYWNlbWVudDsKICAgICAgICBpZiAoIWN1cnJlbnQgfHwgY2xhaW1lZC5oYXMoY3VycmVudCkpIHJldHVybiBudWxsOwogICAgICAgIGNvbnN0IHVybHMgPSBbLi4ubmV3IFNldChyZXN1bHRJbWFnZXMoY3VycmVudCkubWFwKGltYWdlID0+IGltYWdlLmN1cnJlbnRTcmMgfHwgaW1hZ2Uuc3JjKSldOwogICAgICAgIGlmICh1cmxzLmxlbmd0aCAhPT0gMSkgcmV0dXJuIG51bGw7CiAgICAgICAgY2xhaW1lZC5hZGQoY3VycmVudCk7CiAgICAgICAgcmV0dXJuIHVybHNbMF07CiAgICAgIH0sIDE3NSk7CiAgICAgIGlmICghcmVzdWx0KSB0aHJvdyBFcnJvcigiS2jDtG5nIHTDrG0gdGjhuqV5IOG6o25oIGvhur90IHF14bqjIGPhu6dhIGPhuqNuaCAiICsgKGpvYi5pbmRleCArIDEpICsgIi4iKTsKICAgICAgcmVzdWx0cy5wdXNoKHtqb2IsIHJlc3VsdH0pOwogICAgICBhd2FpdCBzZW5kKHtvcDoicHJvZ3Jlc3MiLCBjb3VudDpyZXN1bHRzLmxlbmd0aH0pOwogICAgfQogICAgZm9yIChjb25zdCB7am9iLCByZXN1bHR9IG9mIHJlc3VsdHMpIHsKICAgICAgbGV0IGRhdGE7CiAgICAgIHRyeSB7CiAgICAgICAgY29uc3QgcmVzcG9uc2UgPSBhd2FpdCBmZXRjaChyZXN1bHQpOwogICAgICAgIGlmICghcmVzcG9uc2Uub2spIHRocm93IEVycm9yKCJLaMO0bmcgdOG6o2kgxJHGsOG7o2Mg4bqjbmggdHLDqm4gdGFiLiIpOwogICAgICAgIGNvbnN0IGJsb2IgPSBhd2FpdCByZXNwb25zZS5ibG9iKCk7CiAgICAgICAgaWYgKCFibG9iLnR5cGUuc3RhcnRzV2l0aCgiaW1hZ2UvIikgfHwgYmxvYi5zaXplID4gMjQqMTAyNCoxMDI0KSB0aHJvdyBFcnJvcigi4bqibmgga2jDtG5nIGjhu6NwIGzhu4cuIik7CiAgICAgICAgZGF0YSA9IGF3YWl0IG5ldyBQcm9taXNlKChyZXNvbHZlLHJlamVjdCk9Pntjb25zdCByZWFkZXI9bmV3IEZpbGVSZWFkZXIoKTtyZWFkZXIub25sb2FkPSgpPT5yZXNvbHZlKHJlYWRlci5yZXN1bHQpO3JlYWRlci5vbmVycm9yPXJlamVjdDtyZWFkZXIucmVhZEFzRGF0YVVSTChibG9iKTt9KTsKICAgICAgfSBjYXRjaCAoZXJyb3IpIHsgaWYgKHJlc3VsdC5zdGFydHNXaXRoKCJibG9iOiIpKSB0aHJvdyBlcnJvcjsgfQogICAgICBhd2FpdCBzZW5kKHtvcDoiaW1hZ2UiLGpvYjpqb2Iuam9iLGluZGV4OmpvYi5pbmRleCx1cmw6cmVzdWx0LGRhdGF9KTsKICAgIH0KICB9CiAgYXN5bmMgZnVuY3Rpb24gd2FpdEZvcihmaW5kLCBzZWNvbmRzKSB7CiAgICBmb3IgKGxldCBpPTA7aTxzZWNvbmRzKjI7aSsrKSB7CiAgICAgIGNvbnN0IHZhbHVlPWZpbmQoKTsKICAgICAgaWYgKHZhbHVlKSByZXR1cm4gdmFsdWU7CiAgICAgIGF3YWl0IHNsZWVwKDUwMCk7CiAgICB9CiAgICByZXR1cm4gbnVsbDsKICB9CiAgY29uc3QgcHJvamVjdE9wZW4gPSAoKSA9PiAvXC9wcm9qZWN0cz9cL1teL10rLy50ZXN0KGxvY2F0aW9uLnBhdGhuYW1lKTsKICBjb25zdCBtYXRjaGluZyA9IChwYXR0ZXJuKSA9PiBbLi4uZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgiYnV0dG9uLFtyb2xlPWJ1dHRvbl0sW3JvbGU9dGFiXSxhIildCiAgICAuZmlsdGVyKGl0ZW09PnZpc2libGUoaXRlbSkmJiFpdGVtLmRpc2FibGVkJiZpdGVtLmdldEF0dHJpYnV0ZSgiYXJpYS1kaXNhYmxlZCIpIT09InRydWUiCiAgICAgICYmIChwYXR0ZXJuLnRlc3QoY2FwdGlvbihpdGVtKSkgfHwgcGF0dGVybi50ZXN0KChpdGVtLmlubmVyVGV4dCB8fCBpdGVtLnRleHRDb250ZW50IHx8ICIiKS5yZXBsYWNlKC9ccysvZ3UsICIgIikudHJpbSgpKSkpOwogIGFzeW5jIGZ1bmN0aW9uIGNvbm5lY3QoKSB7CiAgICBjb25zdCBzdGF0ZSA9IGF3YWl0IHNlbmQocGFpcmluZyA/IHtvcDoicGFpciIsdG9rZW46cGFpcmluZ1sxXSxwb3J0Ok51bWJlcihwYWlyaW5nWzJdIHx8IDc4NzcpfSA6IHtvcDoicmVzdW1lIn0pOwogICAgaWYgKHN0YXRlLmRvbmUpIHJldHVybjsKICAgIGlmIChwYWlyaW5nKSBoaXN0b3J5LnJlcGxhY2VTdGF0ZShoaXN0b3J5LnN0YXRlLCAiIiwgbG9jYXRpb24ucGF0aG5hbWUgKyBsb2NhdGlvbi5zZWFyY2gpOyAKICAgIGlmIChkb2N1bWVudC5yZWFkeVN0YXRlID09PSAibG9hZGluZyIpIGF3YWl0IG5ldyBQcm9taXNlKHJlc29sdmU9PmRvY3VtZW50LmFkZEV2ZW50TGlzdGVuZXIoIkRPTUNvbnRlbnRMb2FkZWQiLHJlc29sdmUse29uY2U6dHJ1ZX0pKTsKICAgIGlmICghcHJvamVjdE9wZW4oKSkgewogICAgICBpZiAoIXBhaXJpbmcpIHRocm93IEVycm9yKCJGbG93IHbhuqtuIOG7nyB0cmFuZyBjaOG7pyBzYXUga2hpIHThuqFvIGThu7Egw6FuLiBLaeG7g20gdHJhIMSRxINuZyBuaOG6rXAgcuG7k2kgY2jhuqF5IGzhuqFpLiIpOwogICAgICBjb25zdCBjcmVhdGUgPSBhd2FpdCB3YWl0Rm9yKCgpID0+IHsKICAgICAgICBjb25zdCBjaG9pY2VzID0gbWF0Y2hpbmcoL14oPzooPzphZGQoPzpfY2lyY2xlfF9ib3gpP3xwbHVzfFwrKVxzKik/KD86TmV3IHByb2plY3R8Q3JlYXRlIHByb2plY3R8Q3JlYXRlIG5ldyBwcm9qZWN0fEThu7Egw6FuIG3hu5tpfFThuqFvIGThu7Egw6FufFThuqFvIGThu7Egw6FuIG3hu5tpKSQvaXUpOwogICAgICAgIGNvbnN0IGNvbnRyb2xzID0gY2hvaWNlcy5maWx0ZXIoaXRlbSA9PiAhY2hvaWNlcy5zb21lKG90aGVyID0+IG90aGVyICE9PSBpdGVtICYmIGl0ZW0uY29udGFpbnMob3RoZXIpKSk7CiAgICAgICAgcmV0dXJuIGNvbnRyb2xzWzBdIHx8IG51bGw7CiAgICAgIH0sMzUpOwogICAgICBpZiAoIWNyZWF0ZSkgdGhyb3cgRXJyb3IoIktow7RuZyB0w6xtIHRo4bqleSBuw7p0IHThuqFvIGThu7Egw6FuIEZsb3cuIEtp4buDbSB0cmEgQ2hyb21lIMSRw6MgxJHEg25nIG5o4bqtcCB2w6AgeGVtIHRyYW5nIEZsb3cgY8OzIHRow7RuZyBiw6FvIGfDrC4iKTsKICAgICAgY3JlYXRlLmNsaWNrKCk7CiAgICAgIGlmICghYXdhaXQgd2FpdEZvcihwcm9qZWN0T3Blbiw0NSkpIHRocm93IEVycm9yKCJGbG93IGNoxrBhIG3hu58gZOG7sSDDoW4gbeG7m2kgc2F1IGtoaSBi4bqlbSB04bqhby4iKTsKICAgIH0KICAgIGlmICghYXdhaXQgd2FpdEZvcigoKT0+ZG9jdW1lbnQucXVlcnlTZWxlY3RvcigndGV4dGFyZWEsW2NvbnRlbnRlZGl0YWJsZT0idHJ1ZSJdJyksNDUpKQogICAgICB0aHJvdyBFcnJvcigiROG7sSDDoW4gbeG7m2kgY2jGsGEgaGnhu4duIMO0IHByb21wdC4iKTsKICAgIAogICAgYXdhaXQgcnVuKCk7CiAgfQogIGNvbm5lY3QoKS5jYXRjaChhc3luYyBlcnJvciA9PiB7CiAgICB0cnkge2F3YWl0IHNlbmQoe29wOiJlcnJvciIsbWVzc2FnZTplcnJvci5tZXNzYWdlfSk7fSBjYXRjaCB7IGNvbnNvbGUud2FybigiVuG6vSBUYXk6IGdow6lwIG7hu5FpIGhv4bq3YyBn4butaSB0cuG6oW5nIHRow6FpIHRo4bqldCBi4bqhaS4iLCBlcnJvci5tZXNzYWdlKTsgfQogIH0pLmZpbmFsbHkoKCk9Pnt3aW5kb3cuX192ZVRheVJ1bm5pbmc9ZmFsc2U7fSk7Cn0pKCk7"
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
            or any(sorted(old.get(key, [])) != sorted(manifest.get(key, []))
                   for key in permissions
                   if not (key == "host_permissions" and ref is BUNDLED_EXTENSION
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


def open_chrome_flow(token, port):
    candidates = [Path(os.environ[key]) / "Google/Chrome/Application/chrome.exe"
                  for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA") if os.environ.get(key)]
    chrome = next((path for path in candidates if path.is_file()), None)
    if chrome is None:
        raise RuntimeError("Không tìm thấy Google Chrome trên máy.")
    project = "https://flow.google.com/#vetay=" + token + (f"-{port}" if port != 7877 else "")
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
                    result = {"job": state["id"], "index": index, "prompt": state["prompts"][index]}
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
                            key = "submitted" if progress.get("stage") == "submitted" else "generated"
                            state[key] = max(state.get(key, 0), count)
                        elif self.path == "/error" and size <= 8192:
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

        try:
            server = ExclusiveServer(("127.0.0.1", port), Handler)
        except OSError:
            if port != 7877:
                raise
            server = ExclusiveServer(("127.0.0.1", 0), Handler)
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def images(self, prompts, folder, report):
        state = {"id": uuid.uuid4().hex, "prompts": prompts, "folder": folder,
                 "paths": [], "event": threading.Event(), "error": None}
        with self.guard:
            self.token = uuid.uuid4().hex
            self.job = state
        try:
            open_chrome_flow(self.token, self.port)
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
                report(f"Đã gửi {state.get('submitted', 0)}/{len(prompts)} prompt. Flow đã tạo {state.get('generated', count)}/{len(prompts)} ảnh. Vẽ Tay đã nhận {count}/{len(prompts)} ảnh.")
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

    def run(script, voice, pictures, provider, ratio, highlights, render, output_folder=None, shared_prompt="", progress=gr.Progress()):
        nonlocal chrome_server

        def report(message):
            progress(None, desc=message)
        if not LOCK.acquire(blocking=False):
            raise gr.Error("Một tác vụ đang chạy.")
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
                rows = scene_rows(plan, ratio, highlights, shared_prompt)
                yield rows, None, f"Sẵn sàng: {len(rows)} cảnh và prompt."
                if not render:
                    return
                if provider == "chrome":
                    if chrome_server is None:
                        try:
                            chrome_server = bridge.serve()
                        except OSError as error:
                            raise RuntimeError(
                                f"Không mở được cổng nội bộ cho Chrome: {error}"
                            ) from error
                    yield rows, None, "Đang mở Google Flow và kết nối tự động với tiện ích Chrome…"
                    pictures = bridge.images([row[5] for row in rows], folder, report)

                if not pictures:
                    raise ValueError("Chọn ảnh theo thứ tự cảnh hoặc chọn Google Flow.")
                dest = DATA / f"VeTay-{uuid.uuid4().hex[:8]}.mp4"
                path = render_folder(wav, pictures, plan, dest, report, ratio, highlights)
                saved = save_folder / dest.name
                if saved != dest:
                    try:
                        with tempfile.TemporaryDirectory(prefix="vetay-export-", dir=save_folder) as staging:
                            copy = Path(staging) / dest.name
                            shutil.copyfile(dest, copy)
                            os.replace(copy, saved)
                    except OSError as error:
                        yield rows, path, f"Đã dựng MP4 tại {dest}. Không lưu được sang {save_folder}: {error}"
                        return
                yield rows, path, f"Đã lưu MP4 · {len(rows)} cảnh · {saved}"
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or b"").decode("utf-8", errors="replace").strip()
            yield rows, None, "Lỗi xử lý âm thanh hoặc video: " + detail[-1500:]
        except Exception as error:
            yield rows, None, "Tác vụ dừng: " + str(error)
        finally:
            LOCK.release()

    def analyze(script, voice, ratio, highlights, shared_prompt):
        yield from run(script, voice, None, None, ratio, highlights, False, shared_prompt=shared_prompt)

    def generate_video(script, voice, pictures, provider, ratio, highlights, output_folder, shared_prompt):
        yield from run(script, voice, pictures, provider, ratio, highlights, True, output_folder, shared_prompt)

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
                provider = gr.Radio([("Google Flow qua tiện ích Chrome", "chrome"), ("Ảnh có sẵn", "files")],
                                    value="chrome", label="Nguồn ảnh minh họa")

                pictures = gr.File(label="Ảnh có sẵn theo thứ tự cảnh", file_count="multiple",
                                   file_types=["image"], type="filepath")
        video = gr.Video(label="Xem trước video MP4", height=260, elem_id="vetay-preview")
        highlights = gr.Checkbox(value=True, label="Chữ nhấn theo cảnh")
        with gr.Row():
            analysis = gr.Button("Phân tích và chia cảnh")
            generate = gr.Button("Tự tạo video", variant="primary")
        table = gr.Dataframe(headers=["Cảnh", "Bắt đầu", "Kết thúc", "Thời lượng (giây)",
                                      "Lời đọc", "Prompt Google Flow", "Chữ nhấn"],
                             datatype=["number", "str", "str", "number", "str", "str", "str"],
                             label="Bảng phân cảnh theo lời đọc", wrap=True, interactive=False)
        task = gr.Textbox(label="Tiến độ dựng", interactive=False)
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
        generation = generate.click(
            generate_video, [script, voice, pictures, provider, ratio, highlights, output_folder, shared_prompt],
            [table, video, task],
        )
        generation.success(
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

