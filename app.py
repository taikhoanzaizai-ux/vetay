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
VERSION = "0.2.41"
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
    "version": "1.3.20",
    "commit": "1f873c3c8357f238b4b4a2207cc0cfe348d1e929",
    "files": {
        "flow.js": "71b24121ede7b55288abb1dc2f21edc372175384",
        "popup.html": "d1f3fe3d89500eb7788fc312842525a9c22193c4",
        "manifest.json": "04436f6ebccb5a502c3eeff2d382a03332d6b7b4",
        "worker.js": "5d7f7eb0d19a2c7a4591619b1e26267adbc79386"
    }
}
BUNDLED_EXTENSION_FILES = {
    "flow.js": "KCgpID0+IHsKICBjb25zdCBwYWlyaW5nID0gL14jdmV0YXk9KFthLWYwLTldezMyfSkoPzotKFswLTldezQsNX0pKT8kLy5leGVjKGxvY2F0aW9uLmhhc2gpOwogIGlmICh3aW5kb3cuX192ZVRheVJ1bm5pbmcpIHJldHVybjsKICB3aW5kb3cuX192ZVRheVJ1bm5pbmcgPSB0cnVlOwogIGNvbnN0IHNsZWVwID0gKG1zKSA9PiBuZXcgUHJvbWlzZShyZXNvbHZlID0+IHNldFRpbWVvdXQocmVzb2x2ZSxtcykpOwogIGNvbnN0IHZpc2libGUgPSBub2RlID0+IG5vZGUuZ2V0Q2xpZW50UmVjdHMoKS5sZW5ndGggJiYgZ2V0Q29tcHV0ZWRTdHlsZShub2RlKS52aXNpYmlsaXR5ICE9PSAiaGlkZGVuIjsKICBjb25zdCBzZW5kID0gYXN5bmMgKG1lc3NhZ2UpID0+IHsKICAgIGNvbnN0IHJlc3BvbnNlID0gYXdhaXQgY2hyb21lLnJ1bnRpbWUuc2VuZE1lc3NhZ2UobWVzc2FnZSk7CiAgICBpZiAoIXJlc3BvbnNlPy5vaykgdGhyb3cgRXJyb3IocmVzcG9uc2U/LmVycm9yIHx8ICJN4bqldCBr4bq/dCBu4buRaSB24bubaSBW4bq9IFRheS4iKTsKICAgIHJldHVybiByZXNwb25zZS5yZXN1bHQ7CiAgfTsKICBmdW5jdGlvbiByZXN1bHRJbWFnZXMoY2FyZCkgewogICAgcmV0dXJuIFsuLi5jYXJkLnF1ZXJ5U2VsZWN0b3JBbGwoImltZyIpXS5maWx0ZXIoaW1nID0+IHsKICAgICAgaWYgKCFpbWcuY29tcGxldGUgfHwgIWltZy5uYXR1cmFsV2lkdGggfHwgIWltZy5uYXR1cmFsSGVpZ2h0IHx8ICF2aXNpYmxlKGltZykpIHJldHVybiBmYWxzZTsKICAgICAgY29uc3Qgc3JjID0gaW1nLmN1cnJlbnRTcmMgfHwgaW1nLnNyYzsKICAgICAgY29uc3QgdXJsID0gbmV3IFVSTChzcmMsIGxvY2F0aW9uLmhyZWYpOwogICAgICBjb25zdCBtZWRpYSA9IC9eKMOUIGhp4buDbiB0aOG7iyBow6xuaCDhuqNuaCBj4bunYSBuZ8aw4budaSBkw7luZ3xVc2VyIGltYWdlIHRpbGUpJC9pdS50ZXN0KGltZy5hbHQudHJpbSgpKTsKICAgICAgY29uc3Qgb3JpZ2luYWwgPSAvXihibG9iOnxodHRwczpcL1wvW14vXSpcLmdvb2dsZXVzZXJjb250ZW50XC5jb21cLykvaS50ZXN0KHNyYyk7CiAgICAgIHJldHVybiBvcmlnaW5hbCB8fCAobWVkaWEgJiYgWyJodHRwczoiLCAiZGF0YToiLCAiYmxvYjoiXS5pbmNsdWRlcyh1cmwucHJvdG9jb2wpKTsKICAgIH0pOwogIH0KICBjb25zdCBjYXB0aW9uID0gYnV0dG9uID0+IChidXR0b24uZ2V0QXR0cmlidXRlKCJhcmlhLWxhYmVsIikgfHwgYnV0dG9uLmdldEF0dHJpYnV0ZSgidGl0bGUiKSB8fCBidXR0b24udGV4dENvbnRlbnQgfHwgIiIpLnRyaW0oKTsKICBmdW5jdGlvbiBwcm9tcHRGaWVsZCgpIHsKICAgIGNvbnN0IGZpZWxkcyA9IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCd0ZXh0YXJlYSxbY29udGVudGVkaXRhYmxlPSJ0cnVlIl0sW2NvbnRlbnRlZGl0YWJsZT0icGxhaW50ZXh0LW9ubHkiXScpXQogICAgICAuZmlsdGVyKGZpZWxkID0+IHZpc2libGUoZmllbGQpICYmICFmaWVsZC5jbG9zZXN0KCdbaW5lcnRdLFthcmlhLWhpZGRlbj0idHJ1ZSJdLFtyb2xlPSJzZWFyY2giXScpCiAgICAgICAgJiYgIWZpZWxkLmRpc2FibGVkICYmICFmaWVsZC5yZWFkT25seSAmJiBmaWVsZC5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSAhPT0gInRydWUiKTsKICAgIGNvbnN0IGxhYmVscyA9IGZpZWxkID0+IFsiYXJpYS1sYWJlbCIsICJwbGFjZWhvbGRlciIsICJkYXRhLXBsYWNlaG9sZGVyIiwgInRpdGxlIl0KICAgICAgLm1hcChuYW1lID0+IGZpZWxkLmdldEF0dHJpYnV0ZShuYW1lKSB8fCAiIikuam9pbigiICIpOwogICAgY29uc3QgZWxpZ2libGUgPSBmaWVsZHMuZmlsdGVyKGZpZWxkID0+ICEvc2VhcmNofHTDrG0ga2nhur9tfHJlbmFtZXzEkeG7lWkgdMOqbnxwcm9qZWN0IG5hbWV8dMOqbiBk4buxIMOhbi9pLnRlc3QobGFiZWxzKGZpZWxkKSkKICAgICAgJiYgIWZpZWxkcy5zb21lKHBhcmVudCA9PiBwYXJlbnQgIT09IGZpZWxkICYmIHBhcmVudC5jb250YWlucyhmaWVsZCkpKTsKICAgIGNvbnN0IG1hdGNoZXMgPSBlbGlnaWJsZS5maWx0ZXIoZmllbGQgPT4gL3Byb21wdHx3aGF0ICg/OmRvfHdvdWxkKSB5b3V8ZGVzY3JpYmV8bcO0IHThuqN8YuG6oW4gbXXhu5FufHThuqFvIG7hu5lpIGR1bmcvaS50ZXN0KGxhYmVscyhmaWVsZCkpKTsKICAgIGlmIChtYXRjaGVzLmxlbmd0aCA9PT0gMSkgcmV0dXJuIG1hdGNoZXNbMF07CiAgICByZXR1cm4gZWxpZ2libGUubGVuZ3RoID09PSAxID8gZWxpZ2libGVbMF0gOiBudWxsOwogIH0KICBjb25zdCByYXRpb0xhYmVsID0gcmF0aW8gPT4gbmV3IFJlZ0V4cCgiKD86XnxbXjAtOV0pIiArIHJhdGlvLnJlcGxhY2UoIjoiLCAiXFxzKls64oi2XVxccyoiKSArICIoPyFbMC05XSkiLCAidSIpOwogIGFzeW5jIGZ1bmN0aW9uIHNlbGVjdFJhdGlvKHJhdGlvKSB7CiAgICBjb25zdCBzZWxlY3RlZCA9IG5vZGUgPT4gWyJhcmlhLWNoZWNrZWQiLCAiYXJpYS1zZWxlY3RlZCIsICJhcmlhLXByZXNzZWQiXS5zb21lKGtleSA9PiBub2RlLmdldEF0dHJpYnV0ZShrZXkpID09PSAidHJ1ZSIpCiAgICAgIHx8IG5vZGUuZ2V0QXR0cmlidXRlKCJkYXRhLXN0YXRlIikgPT09ICJjaGVja2VkIiB8fCBub2RlLmdldEF0dHJpYnV0ZSgiZGF0YS1zdGF0ZSIpID09PSAiYWN0aXZlIjsKICAgIGNvbnN0IGNvbnRyb2xzID0gKCkgPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoImJ1dHRvbixbcm9sZT1idXR0b25dLFtyb2xlPXJhZGlvXSxbcm9sZT1vcHRpb25dLFtyb2xlPW1lbnVpdGVtcmFkaW9dLFtyb2xlPXRhYl0iKV0KICAgICAgLmZpbHRlcihub2RlID0+IHZpc2libGUobm9kZSkgJiYgIW5vZGUuZGlzYWJsZWQgJiYgbm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSAhPT0gInRydWUiKTsKICAgIGNvbnN0IGxhYmVscyA9IG5vZGUgPT4gW2NhcHRpb24obm9kZSksIG5vZGUuZ2V0QXR0cmlidXRlKCJkYXRhLXZhbHVlIikgfHwgIiIsIG5vZGUuaW5uZXJUZXh0IHx8ICIiXS5qb2luKCIgIik7CiAgICBjb25zdCBleGFjdCA9IHJhdGlvTGFiZWwocmF0aW8pOwogICAgY29uc3QgaWNvbk5hbWUgPSAiY3JvcF8iICsgcmF0aW8ucmVwbGFjZSgiOiIsICJfIik7CiAgICBjb25zdCBtYXRjaGVzUmF0aW8gPSBub2RlID0+IGV4YWN0LnRlc3QobGFiZWxzKG5vZGUpKSB8fCBbLi4ubm9kZS5xdWVyeVNlbGVjdG9yQWxsKCJtYXQtaWNvbixbcm9sZT1pbWddLHN2ZyIpXQogICAgICAuc29tZShpY29uID0+IFtpY29uLnRleHRDb250ZW50LCBpY29uLmdldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIpLCBpY29uLmdldEF0dHJpYnV0ZSgiZGF0YS1pY29uIildCiAgICAgICAgLnNvbWUobGFiZWwgPT4gbGFiZWw/LnRyaW0oKSA9PT0gaWNvbk5hbWUpKTsKICAgIGNvbnN0IGhhc1JhdGlvID0gcmF0aW9MYWJlbCgiWzAtOV0rOlswLTldKyIpOwogICAgY29uc3QgaXNPcHRpb24gPSBub2RlID0+IFsicmFkaW8iLCAib3B0aW9uIiwgIm1lbnVpdGVtcmFkaW8iXS5pbmNsdWRlcyhub2RlLmdldEF0dHJpYnV0ZSgicm9sZSIpKTsKICAgIGNvbnN0IHRyaWdnZXIgPSBjb250cm9scygpLmZpbHRlcihub2RlID0+ICFpc09wdGlvbihub2RlKSAmJiAobm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtaGFzcG9wdXAiKQogICAgICA/IC9hc3BlY3QgcmF0aW98dOG7tyBs4buHfHThu4kgbOG7hy9pdS50ZXN0KGxhYmVscyhub2RlKSkgfHwgaGFzUmF0aW8udGVzdChsYWJlbHMobm9kZSkpCiAgICAgIDogL14oPzphc3BlY3QgcmF0aW98dOG7tyBs4buHKD86IGtodW5nIGjDrG5oKT98dOG7iSBs4buHKD86IGtodW5nIGjDrG5oKT8pJC9pdS50ZXN0KGNhcHRpb24obm9kZSkpKSk7CiAgICBjb25zdCBvcHRpb25zID0gKCkgPT4gY29udHJvbHMoKS5maWx0ZXIobm9kZSA9PiAhdHJpZ2dlci5pbmNsdWRlcyhub2RlKSAmJiBub2RlICE9PSBzZXR0aW5ncyAmJiBtYXRjaGVzUmF0aW8obm9kZSkpOwogICAgY29uc3QgbW9kZWxTZXR0aW5ncyA9IGNvbnRyb2xzKCkuZmlsdGVyKG5vZGUgPT4gL05hbm8gQmFuYW5hfEltYWdlbi9pdS50ZXN0KGxhYmVscyhub2RlKSkgJiYgL1t4w5ddWzEtNF1cYi9pdS50ZXN0KGxhYmVscyhub2RlKSkpOwogICAgY29uc3Qgc2V0dGluZ3MgPSBjb250cm9scygpLmZpbmQobm9kZSA9PiAvXijEkGnhu4F1IGtp4buHbiBrw61jaCBob+G6oXQgY8OgaSDEkeG6t3R8U2V0dGluZ3MgdHJpZ2dlcnxHZW5lcmF0aW9uIHNldHRpbmdzKSQvaXUudGVzdChjYXB0aW9uKG5vZGUpKSkKICAgICAgfHwgKG1vZGVsU2V0dGluZ3MubGVuZ3RoID09PSAxID8gbW9kZWxTZXR0aW5nc1swXSA6IG51bGwpOwogICAgbGV0IG9wZW5lZCA9IGZhbHNlOwogICAgaWYgKCFvcHRpb25zKCkubGVuZ3RoICYmIHNldHRpbmdzKSB7CiAgICAgIHNldHRpbmdzLmNsaWNrKCk7CiAgICAgIG9wZW5lZCA9IHRydWU7CiAgICAgIGNvbnN0IGltYWdlID0gYXdhaXQgd2FpdEZvcigoKSA9PiBjb250cm9scygpLmZpbmQobm9kZSA9PiBub2RlLmdldEF0dHJpYnV0ZSgicm9sZSIpID09PSAicmFkaW8iICYmIC9eKEltYWdlfEjDrG5oIOG6o25ofOG6om5oKSQvaXUudGVzdChjYXB0aW9uKG5vZGUpKSksIDMpOwogICAgICBpZiAoaW1hZ2UgJiYgIXNlbGVjdGVkKGltYWdlKSkgaW1hZ2UuY2xpY2soKTsKICAgIH0KICAgIGlmICghb3B0aW9ucygpLmxlbmd0aCkgYXdhaXQgd2FpdEZvcigoKSA9PiBvcHRpb25zKCkubGVuZ3RoLCAzKTsKICAgIGlmICghb3B0aW9ucygpLmxlbmd0aCAmJiB0cmlnZ2VyLmxlbmd0aCA9PT0gMSkgdHJpZ2dlclswXS5jbGljaygpOwogICAgY29uc3Qgb3B0aW9uID0gYXdhaXQgd2FpdEZvcigoKSA9PiB7CiAgICAgIGlmICh0cmlnZ2VyLmxlbmd0aCA9PT0gMSAmJiBleGFjdC50ZXN0KGxhYmVscyh0cmlnZ2VyWzBdKSkgJiYgIW9wdGlvbnMoKS5sZW5ndGgpIHJldHVybiB0cmlnZ2VyWzBdOwogICAgICBjb25zdCBtYXRjaGVzID0gb3B0aW9ucygpOwogICAgICBjb25zdCByYWRpb3MgPSBtYXRjaGVzLmZpbHRlcihpc09wdGlvbik7CiAgICAgIHJldHVybiByYWRpb3MubGVuZ3RoID09PSAxID8gcmFkaW9zWzBdIDogbWF0Y2hlcy5sZW5ndGggPT09IDEgPyBtYXRjaGVzWzBdIDogbnVsbDsKICAgIH0sIDEyKTsKICAgIGlmICghb3B0aW9uKSB0aHJvdyBFcnJvcigiS2jDtG5nIGNo4buNbiDEkcaw4bujYyB04bu3IGzhu4cgIiArIHJhdGlvICsgIiB0cm9uZyBGbG93ICgiICsgKHNldHRpbmdzID8gIsSRw6MgdGjhuqV5IG7DunQgY8OgaSDEkeG6t3Qg4bqjbmgiIDogImNoxrBhIHRo4bqleSBuw7p0IGPDoGkgxJHhurd0IOG6o25oIikgKyAiKS4iKTsKICAgIGlmICghdHJpZ2dlci5pbmNsdWRlcyhvcHRpb24pICYmICFzZWxlY3RlZChvcHRpb24pKSBvcHRpb24uY2xpY2soKTsKICAgIGNvbnN0IHNpbmdsZSA9IGNvbnRyb2xzKCkuZmlsdGVyKG5vZGUgPT4gL15beMOXXVxzKjEkL2l1LnRlc3QoY2FwdGlvbihub2RlKSkpOwogICAgaWYgKHNpbmdsZS5sZW5ndGggPT09IDEgJiYgIXNlbGVjdGVkKHNpbmdsZVswXSkpIHNpbmdsZVswXS5jbGljaygpOwogICAgaWYgKG9wZW5lZCAmJiBvcHRpb25zKCkubGVuZ3RoICYmIHNldHRpbmdzLmlzQ29ubmVjdGVkKSBzZXR0aW5ncy5jbGljaygpOwogIH0KICBhc3luYyBmdW5jdGlvbiBydW4oKSB7CiAgICBsZXQgYXBwbGllZFJhdGlvOwogICAgY29uc3QgYmF0Y2ggPSBhd2FpdCBzZW5kKHtvcDoiYmF0Y2gifSk7CiAgICBpZiAoYmF0Y2guZG9uZSkgcmV0dXJuOwogICAgY29uc3QgcXVldWVkID0gW107CiAgICBjb25zdCByZXN1bHRzID0gW107CiAgICBjb25zdCBjYXJkcyA9ICgpID0+IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbcm9sZT0iZ3JvdXAiXVthcmlhLWxhYmVsXScpXTsKICAgIGNvbnN0IG5vcm1hbGl6ZSA9IHRleHQgPT4gKHRleHQgfHwgIiIpLnJlcGxhY2UoL1xzKy9ndSwgIiAiKS50cmltKCk7CiAgICBjb25zdCBpbml0aWFsQ2FyZHMgPSBjYXJkcygpOwogICAgbGV0IG5ld2VzdEZpcnN0ID0gdHJ1ZTsKICAgIGZvciAoY29uc3QgW29mZnNldCwgcHJvbXB0XSBvZiBiYXRjaC5wcm9tcHRzLmVudHJpZXMoKSkgewogICAgICBjb25zdCBqb2IgPSB7am9iOmJhdGNoLmpvYiwgaW5kZXg6YmF0Y2guaW5kZXggKyBvZmZzZXQsIHByb21wdH07CiAgICAgIGNvbnN0IGFnZW50ID0gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tyb2xlPSJjaGVja2JveCJdLGlucHV0W3R5cGU9ImNoZWNrYm94Il0nKV0uZmluZChub2RlID0+IHZpc2libGUobm9kZSkKICAgICAgICAmJiAvXihUw6FjIG5ow6JufEFnZW50KSQvaXUudGVzdCgobm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtbGFiZWwiKSB8fCBub2RlLnRleHRDb250ZW50IHx8ICIiKS50cmltKCkpCiAgICAgICAgJiYgKG5vZGUuY2hlY2tlZCB8fCBub2RlLmdldEF0dHJpYnV0ZSgiYXJpYS1jaGVja2VkIikgPT09ICJ0cnVlIikpOwogICAgICBpZiAoYWdlbnQpIHsgYWdlbnQuY2xpY2soKTsgYXdhaXQgc2xlZXAoMzAwKTsgfQogICAgICBsZXQgZmllbGQgPSBhd2FpdCB3YWl0Rm9yKHByb21wdEZpZWxkLCAyNSk7CiAgICAgIGlmICghZmllbGQpIHRocm93IEVycm9yKCJLaMO0bmcgeMOhYyDEkeG7i25oIMSRxrDhu6NjIMO0IHByb21wdCBkdXkgbmjhuqV0LiBN4bufIHRyYW5nIHThuqFvIOG6o25oIHRyb25nIGThu7Egw6FuIEZsb3cuIik7CiAgICAgIGF3YWl0IHNlbmQoe29wOiJhY3RpdmF0ZSJ9KTsKICAgICAgY29uc3QgcmF0aW8gPSAvXlThuqFvIG3hu5l0IOG6o25oIG1pbmggaOG7jWEgdOG7tyBs4buHIChcZCs6XGQrKSAvdS5leGVjKGpvYi5wcm9tcHQpPy5bMV07CiAgICAgIGlmIChyYXRpbyAmJiByYXRpbyAhPT0gYXBwbGllZFJhdGlvKSB7CiAgICAgICAgYXdhaXQgc2VsZWN0UmF0aW8ocmF0aW8pOwogICAgICAgIGFwcGxpZWRSYXRpbyA9IHJhdGlvOwogICAgICAgIGZpZWxkID0gYXdhaXQgd2FpdEZvcihwcm9tcHRGaWVsZCwgMTApOwogICAgICAgIGlmICghZmllbGQpIHRocm93IEVycm9yKCJLaMO0bmcgdGjhuqV5IMO0IHByb21wdCBzYXUga2hpIGNo4buNbiB04bu3IGzhu4cuIik7CiAgICAgIH0KICAgICAgZmllbGQuZm9jdXMoKTsKICAgICAgaWYgKGZpZWxkIGluc3RhbmNlb2YgSFRNTFRleHRBcmVhRWxlbWVudCkgewogICAgICAgIE9iamVjdC5nZXRPd25Qcm9wZXJ0eURlc2NyaXB0b3IoSFRNTFRleHRBcmVhRWxlbWVudC5wcm90b3R5cGUsInZhbHVlIikuc2V0LmNhbGwoZmllbGQsam9iLnByb21wdCk7CiAgICAgICAgZmllbGQuZGlzcGF0Y2hFdmVudChuZXcgSW5wdXRFdmVudCgiaW5wdXQiLHtidWJibGVzOnRydWUsZGF0YTpqb2IucHJvbXB0LGlucHV0VHlwZToiaW5zZXJ0VGV4dCJ9KSk7CiAgICAgIH0gZWxzZSB7CiAgICAgICAgY29uc3Qgc2VsZWN0aW9uID0gd2luZG93LmdldFNlbGVjdGlvbigpOwogICAgICAgIGNvbnN0IHJhbmdlID0gZG9jdW1lbnQuY3JlYXRlUmFuZ2UoKTsKICAgICAgICByYW5nZS5zZWxlY3ROb2RlQ29udGVudHMoZmllbGQpOwogICAgICAgIHNlbGVjdGlvbi5yZW1vdmVBbGxSYW5nZXMoKTsKICAgICAgICBzZWxlY3Rpb24uYWRkUmFuZ2UocmFuZ2UpOwogICAgICAgIGlmICghZG9jdW1lbnQuZXhlY0NvbW1hbmQoImluc2VydFRleHQiLGZhbHNlLGpvYi5wcm9tcHQpKSB0aHJvdyBFcnJvcigiS2jDtG5nIMSRaeG7gW4gxJHGsOG7o2MgcHJvbXB0IHRyb25nIEZsb3cuIik7CiAgICAgIH0KICAgICAgYXdhaXQgc2xlZXAoNTAwKTsKICAgICAgY29uc3QgYnV0dG9ucyA9IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCJidXR0b24sW3JvbGU9YnV0dG9uXSIpXS5maWx0ZXIoYj0+dmlzaWJsZShiKSYmIWIuZGlzYWJsZWQmJmIuZ2V0QXR0cmlidXRlKCJhcmlhLWRpc2FibGVkIikhPT0idHJ1ZSIpOwogICAgICBjb25zdCBzdGFydCA9IGJ1dHRvbnMuZmlsdGVyKGJ1dHRvbiA9PiAvXihC4bqvdCDEkeG6p3UgdOG6oW98U3RhcnQgZ2VuZXJhdGluZ3xTdGFydCBjcmVhdGluZykkL2l1LnRlc3QoY2FwdGlvbihidXR0b24pKSk7CiAgICAgIGxldCBnZW5lcmF0ZSA9IHN0YXJ0Lmxlbmd0aCA9PT0gMSA/IHN0YXJ0IDogYnV0dG9ucy5maWx0ZXIoYj0+L14oR2VuZXJhdGUgSW1hZ2V8Q3JlYXRlIEltYWdlfFThuqFvIGjDrG5oIOG6o25ofFThuqFvIOG6o25oKSQvaS50ZXN0KGNhcHRpb24oYikpKTsKICAgICAgaWYgKGdlbmVyYXRlLmxlbmd0aCA9PT0gMCAmJiBtYXRjaGluZygvTmFubyBCYW5hbmF8SW1hZ2VuL2kpLmxlbmd0aCA9PT0gMSkKICAgICAgICBnZW5lcmF0ZSA9IGJ1dHRvbnMuZmlsdGVyKGI9Pi9eKEdlbmVyYXRlfFThuqFvKSQvaS50ZXN0KGNhcHRpb24oYikpKTsKICAgICAgaWYgKGdlbmVyYXRlLmxlbmd0aCA9PT0gMCkgewogICAgICAgIGNvbnN0IGVkaXRvciA9IGZpZWxkLmNsb3Nlc3QoImZvcm0iKSB8fCBmaWVsZC5wYXJlbnRFbGVtZW50OwogICAgICAgIGdlbmVyYXRlID0gYnV0dG9ucy5maWx0ZXIoYnV0dG9uID0+IC9eKGFycm93X2ZvcndhcmR8c2VuZHxn4butaXxi4bqvdCDEkeG6p3UgdOG6oW98c3RhcnQgZ2VuZXJhdGluZ3xzdGFydCBjcmVhdGluZykkL2l1LnRlc3QoY2FwdGlvbihidXR0b24pKQogICAgICAgICAgJiYgKGVkaXRvcj8uY29udGFpbnMoYnV0dG9uKSB8fCBmaWVsZC5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKS5ib3R0b20gPD0gYnV0dG9uLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpLmJvdHRvbQogICAgICAgICAgICAmJiBNYXRoLmFicyhmaWVsZC5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKS5yaWdodCAtIGJ1dHRvbi5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKS5yaWdodCkgPCAxMDApKTsKICAgICAgfQogICAgICBpZiAoZ2VuZXJhdGUubGVuZ3RoICE9PSAxKSB0aHJvdyBFcnJvcigiS2jDtG5nIG5o4bqtbiBkaeG7h24gxJHGsOG7o2MgbsO6dCB04bqhbyDhuqNuaC4gS2nhu4NtIHRyYSBjaOG6vyDEkeG7mSDhuqJuaCwgbW9kZWwgdsOgIGjhuqFuIG3hu6ljIEZsb3cuIik7CiAgICAgIGNvbnN0IHByaW9yID0gbmV3IFNldChjYXJkcygpKTsKICAgICAgY29uc3QgZWRpdG9yQm94ID0gZmllbGQuZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCk7CiAgICAgIGNvbnN0IG5lYXJieUFycm93cyA9IGJ1dHRvbnMuZmlsdGVyKGJ1dHRvbiA9PiB7CiAgICAgICAgY29uc3QgYm94ID0gYnV0dG9uLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgICAgIHJldHVybiBib3gudG9wID49IGVkaXRvckJveC5ib3R0b20gLSA4ICYmIGJveC50b3AgPD0gZWRpdG9yQm94LmJvdHRvbSArIDEwMAogICAgICAgICAgJiYgTWF0aC5hYnMoYm94LnJpZ2h0IC0gZWRpdG9yQm94LnJpZ2h0KSA8IDEwMAogICAgICAgICAgJiYgKFsuLi5idXR0b24ucXVlcnlTZWxlY3RvckFsbCgibWF0LWljb24sW2RhdGEtaWNvbl0sW2FyaWEtaGlkZGVuPXRydWVdLGltZyIpXS5zb21lKGljb24gPT4KICAgICAgICAgICAgL14oYXJyb3dfZm9yd2FyZHxzZW5kKSQvaS50ZXN0KChpY29uLmdldEF0dHJpYnV0ZSgiZGF0YS1pY29uIikgfHwgaWNvbi5nZXRBdHRyaWJ1dGUoImFsdCIpIHx8IGljb24udGV4dENvbnRlbnQgfHwgIiIpLnRyaW0oKSkpCiAgICAgICAgICAgIHx8IC9eKELhuq90IMSR4bqndSB04bqhb3xTdGFydCBnZW5lcmF0aW5nfFN0YXJ0IGNyZWF0aW5nKSQvaXUudGVzdChjYXB0aW9uKGJ1dHRvbikpKTsKICAgICAgfSk7CiAgICAgIGNvbnN0IHN1Ym1pdCA9IG5lYXJieUFycm93cy5sZW5ndGggPT09IDEgPyBuZWFyYnlBcnJvd3NbMF0gOiBnZW5lcmF0ZVswXTsKICAgICAgc3VibWl0LnNjcm9sbEludG9WaWV3KHtibG9jazoibmVhcmVzdCIsIGlubGluZToibmVhcmVzdCJ9KTsKICAgICAgY29uc3QgYm94ID0gc3VibWl0LmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgICBjb25zdCB4ID0gYm94LmxlZnQgKyBib3gud2lkdGggLyAyOwogICAgICBjb25zdCB5ID0gYm94LnRvcCArIGJveC5oZWlnaHQgLyAyOwogICAgICBpZiAoIXN1Ym1pdC5jb250YWlucyhkb2N1bWVudC5lbGVtZW50RnJvbVBvaW50KHgseSkpKSB0aHJvdyBFcnJvcigiTsO6dCB04bqhbyDhuqNuaCDEkWFuZyBi4buLIGNoZS4iKTsKICAgICAgYXdhaXQgc2VuZCh7b3A6InN1Ym1pdCIsIGpvYjpqb2Iuam9iLCB4LCB5fSk7CiAgICAgIGNvbnN0IGZpbmRDYXJkID0gKCkgPT4gewogICAgICAgIGNvbnN0IG1hdGNoZXMgPSBjYXJkcygpLmZpbHRlcihjYXJkID0+ICFwcmlvci5oYXMoY2FyZCkgJiYKICAgICAgICAgIChub3JtYWxpemUoY2FyZC5nZXRBdHRyaWJ1dGUoImFyaWEtbGFiZWwiKSkgPT09IG5vcm1hbGl6ZShwcm9tcHQpCiAgICAgICAgICAgfHwgbm9ybWFsaXplKGNhcmQudGV4dENvbnRlbnQpLmluY2x1ZGVzKG5vcm1hbGl6ZShwcm9tcHQpKSkpOwogICAgICAgIHJldHVybiBtYXRjaGVzLmxlbmd0aCA9PT0gMSA/IG1hdGNoZXNbMF0gOiBudWxsOwogICAgICB9OwogICAgICBjb25zdCBjYXJkID0gYXdhaXQgd2FpdEZvcihmaW5kQ2FyZCwgMjApOwogICAgICBpZiAoIWNhcmQpIHRocm93IEVycm9yKCJGbG93IGNoxrBhIHjDoWMgbmjhuq1uIMSRw6Mgbmjhuq1uIHByb21wdCBj4bqjbmggIiArIChqb2IuaW5kZXggKyAxKSArICIuIik7CiAgICAgIGNvbnN0IGN1cnJlbnRDYXJkcyA9IGNhcmRzKCk7CiAgICAgIG5ld2VzdEZpcnN0ICYmPSBjdXJyZW50Q2FyZHMubGVuZ3RoID09PSBpbml0aWFsQ2FyZHMubGVuZ3RoICsgcXVldWVkLmxlbmd0aCArIDEKICAgICAgICAmJiBjdXJyZW50Q2FyZHNbMF0gPT09IGNhcmQ7CiAgICAgIHF1ZXVlZC5wdXNoKHtqb2IsIGNhcmQsIGZpbmRDYXJkfSk7CiAgICAgIGF3YWl0IHNlbmQoe29wOiJwcm9ncmVzcyIsIGNvdW50OnF1ZXVlZC5sZW5ndGgsIHN0YWdlOiJzdWJtaXR0ZWQifSk7CiAgICB9CiAgICBjb25zdCBjbGFpbWVkID0gbmV3IFNldCgpOwogICAgZm9yIChjb25zdCB7am9iLCBjYXJkLCBmaW5kQ2FyZH0gb2YgcXVldWVkKSB7CiAgICAgIGNvbnN0IHJlc3VsdCA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gewogICAgICAgIGNvbnN0IGdyaWQgPSBjYXJkcygpOwogICAgICAgIGNvbnN0IHJlcGxhY2VtZW50ID0gbmV3ZXN0Rmlyc3QgJiYgaW5pdGlhbENhcmRzLmxlbmd0aCA9PT0gMCAmJiBncmlkLmxlbmd0aCA9PT0gcXVldWVkLmxlbmd0aAogICAgICAgICAgJiYgZ3JpZC5ldmVyeShpdGVtID0+IHJlc3VsdEltYWdlcyhpdGVtKS5sZW5ndGggPT09IDEpCiAgICAgICAgICA/IGdyaWRbcXVldWVkLmxlbmd0aCAtIDEgLSBxdWV1ZWQuZmluZEluZGV4KGVudHJ5ID0+IGVudHJ5LmNhcmQgPT09IGNhcmQpXSA6IG51bGw7CiAgICAgICAgY29uc3QgY3VycmVudCA9IChjYXJkLmlzQ29ubmVjdGVkICYmIHJlc3VsdEltYWdlcyhjYXJkKS5sZW5ndGggPyBjYXJkIDogZmluZENhcmQoKSkgfHwgcmVwbGFjZW1lbnQ7CiAgICAgICAgaWYgKCFjdXJyZW50IHx8IGNsYWltZWQuaGFzKGN1cnJlbnQpKSByZXR1cm4gbnVsbDsKICAgICAgICBjb25zdCB1cmxzID0gWy4uLm5ldyBTZXQocmVzdWx0SW1hZ2VzKGN1cnJlbnQpLm1hcChpbWFnZSA9PiBpbWFnZS5jdXJyZW50U3JjIHx8IGltYWdlLnNyYykpXTsKICAgICAgICBpZiAodXJscy5sZW5ndGggIT09IDEpIHJldHVybiBudWxsOwogICAgICAgIGNsYWltZWQuYWRkKGN1cnJlbnQpOwogICAgICAgIHJldHVybiBjdXJyZW50OwogICAgICB9LCAxNzUpOwogICAgICBpZiAoIXJlc3VsdCkgdGhyb3cgRXJyb3IoIktow7RuZyB0w6xtIHRo4bqleSDhuqNuaCBr4bq/dCBxdeG6oyBj4bunYSBj4bqjbmggIiArIChqb2IuaW5kZXggKyAxKSArICIuIik7CiAgICAgIHJlc3VsdHMucHVzaCh7am9iLCByZXN1bHR9KTsKICAgICAgYXdhaXQgc2VuZCh7b3A6InByb2dyZXNzIiwgY291bnQ6cmVzdWx0cy5sZW5ndGh9KTsKICAgIH0KICAgIGZvciAoY29uc3Qge2pvYiwgcmVzdWx0OmNhcmR9IG9mIHJlc3VsdHMpIHsKICAgICAgaWYgKCFjYXJkLmlzQ29ubmVjdGVkKSB0aHJvdyBFcnJvcigiVGjhursg4bqjbmggY+G6o25oICIgKyAoam9iLmluZGV4ICsgMSkgKyAiIMSRw6MgdGhheSDEkeG7lWkuIik7CiAgICAgIGNvbnN0IGFjdGl2YXRlQ29udHJvbCA9IGFzeW5jIChub2RlLCBjbGlja3M9MSkgPT4gewogICAgICAgIG5vZGUuc2Nyb2xsSW50b1ZpZXcoe2Jsb2NrOiJjZW50ZXIiLCBpbmxpbmU6Im5lYXJlc3QifSk7CiAgICAgICAgY29uc3QgYm94ID0gbm9kZS5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKTsKICAgICAgICBjb25zdCB4ID0gYm94LmxlZnQgKyBib3gud2lkdGggLyAyLCB5ID0gYm94LnRvcCArIGJveC5oZWlnaHQgLyAyOwogICAgICAgIGlmICghbm9kZS5jb250YWlucyhkb2N1bWVudC5lbGVtZW50RnJvbVBvaW50KHgseSkpKSB0aHJvdyBFcnJvcigiTsO6dCB04bqjaSDhuqNuaCBGbG93IMSRYW5nIGLhu4sgY2hlLiIpOwogICAgICAgIGF3YWl0IHNlbmQoe29wOiJzdWJtaXQiLCBqb2I6am9iLmpvYiwgeCwgeSwgY2xpY2tzfSk7CiAgICAgIH07CiAgICAgIGNvbnN0IHRpbGUgPSByZXN1bHRJbWFnZXMoY2FyZClbMF07CiAgICAgIGF3YWl0IGFjdGl2YXRlQ29udHJvbCh0aWxlLCAyKTsKICAgICAgY29uc3QgZG93bmxvYWQgPSBhd2FpdCB3YWl0Rm9yKCgpID0+IG1hdGNoaW5nKC9eKFThuqNpIG7hu5lpIGR1bmcgbmdoZSBuaMOsbiB4deG7kW5nfERvd25sb2FkIG1lZGlhfERvd25sb2FkKSQvaXUpWzBdLCAxMik7CiAgICAgIGlmICghZG93bmxvYWQpIHRocm93IEVycm9yKCJLaMO0bmcgdGjhuqV5IG7DunQgdOG6o2kg4bqjbmggZ+G7kWMgY+G7p2EgY+G6o25oICIgKyAoam9iLmluZGV4ICsgMSkgKyAiLiIpOwogICAgICBhd2FpdCBhY3RpdmF0ZUNvbnRyb2woZG93bmxvYWQpOwogICAgICBjb25zdCBvcHRpb24gPSBhd2FpdCB3YWl0Rm9yKCgpID0+IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbcm9sZT0ibWVudWl0ZW0iXSxidXR0b24nKV0KICAgICAgICAuZmluZChpdGVtID0+IHZpc2libGUoaXRlbSkgJiYgL14ySyg/OlxzfCQpL2l1LnRlc3QoY2FwdGlvbihpdGVtKSkgJiYgIWl0ZW0uZGlzYWJsZWQgJiYgaXRlbS5nZXRBdHRyaWJ1dGUoImFyaWEtZGlzYWJsZWQiKSAhPT0gInRydWUiKSwgOCk7CiAgICAgIGlmICghb3B0aW9uKSB0aHJvdyBFcnJvcigiRmxvdyBraMO0bmcgY8OzIHTDuXkgY2jhu41uIHThuqNpIDJLIGNobyBj4bqjbmggIiArIChqb2IuaW5kZXggKyAxKSArICIuIik7CiAgICAgIGF3YWl0IHNlbmQoe29wOiJhcm0yayIsam9iOmpvYi5qb2IsaW5kZXg6am9iLmluZGV4fSk7CiAgICAgIGF3YWl0IGFjdGl2YXRlQ29udHJvbChvcHRpb24pOwogICAgICBjb25zdCBkZWFkbGluZSA9IERhdGUubm93KCkgKyAxNTAwMDA7CiAgICAgIGxldCByZWNlaXZlZCA9IGZhbHNlOwogICAgICB3aGlsZSAoIXJlY2VpdmVkICYmIERhdGUubm93KCkgPCBkZWFkbGluZSkgewogICAgICAgIHJlY2VpdmVkID0gKGF3YWl0IHNlbmQoe29wOiJ0cmFuc2ZlcjJrIixqb2I6am9iLmpvYixpbmRleDpqb2IuaW5kZXh9KSkucmVjZWl2ZWQ7CiAgICAgICAgaWYgKCFyZWNlaXZlZCkgYXdhaXQgc2xlZXAoMTAwMCk7CiAgICAgIH0KICAgICAgaWYgKCFyZWNlaXZlZCkgdGhyb3cgRXJyb3IoIkNocm9tZSBjaMawYSB04bqjaSB4b25nIOG6o25oIDJLIGNobyBj4bqjbmggIiArIChqb2IuaW5kZXggKyAxKSArICIuIik7CiAgICAgIGlmIChqb2IuaW5kZXggIT09IGJhdGNoLmluZGV4ICsgYmF0Y2gucHJvbXB0cy5sZW5ndGggLSAxKSB7CiAgICAgICAgY29uc3QgYmFjayA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gbWF0Y2hpbmcoL14oTsO6dCBxdWF5IGzhuqFpIMSR4buDIHF1YXkgduG7gSB0cmFuZyB0csaw4bubY3xHbyBiYWNrfEJhY2spJC9pdSlbMF0sIDgpOwogICAgICAgIGlmICghYmFjaykgdGhyb3cgRXJyb3IoIktow7RuZyB0aOG6pXkgbsO6dCB0cuG7nyBs4bqhaSBsxrDhu5tpIOG6o25oIEZsb3cuIik7CiAgICAgICAgYXdhaXQgYWN0aXZhdGVDb250cm9sKGJhY2spOwogICAgICAgIGF3YWl0IHdhaXRGb3IoKCkgPT4gY2FyZHMoKS5sZW5ndGggPiAwLCAxMik7CiAgICAgICAgZm9yIChjb25zdCBlbnRyeSBvZiByZXN1bHRzKSB7CiAgICAgICAgICBpZiAoZW50cnkucmVzdWx0LmlzQ29ubmVjdGVkKSBjb250aW51ZTsKICAgICAgICAgIGNvbnN0IHJlcGxhY2VtZW50ID0gbmV3ZXN0Rmlyc3QgJiYgaW5pdGlhbENhcmRzLmxlbmd0aCA9PT0gMCAmJiBjYXJkcygpLmxlbmd0aCA9PT0gcXVldWVkLmxlbmd0aAogICAgICAgICAgICA/IGNhcmRzKClbcXVldWVkLmxlbmd0aCAtIDEgLSAoZW50cnkuam9iLmluZGV4IC0gYmF0Y2guaW5kZXgpXSA6IG51bGw7CiAgICAgICAgICBpZiAocmVwbGFjZW1lbnQpIGVudHJ5LnJlc3VsdCA9IHJlcGxhY2VtZW50OwogICAgICAgIH0KICAgICAgfQogICAgfQogIH0KICBhc3luYyBmdW5jdGlvbiB3YWl0Rm9yKGZpbmQsIHNlY29uZHMpIHsKICAgIGZvciAobGV0IGk9MDtpPHNlY29uZHMqMjtpKyspIHsKICAgICAgY29uc3QgdmFsdWU9ZmluZCgpOwogICAgICBpZiAodmFsdWUpIHJldHVybiB2YWx1ZTsKICAgICAgYXdhaXQgc2xlZXAoNTAwKTsKICAgIH0KICAgIHJldHVybiBudWxsOwogIH0KICBjb25zdCBwcm9qZWN0T3BlbiA9ICgpID0+IC9cL3Byb2plY3RzP1wvW14vXSsvLnRlc3QobG9jYXRpb24ucGF0aG5hbWUpOwogIGNvbnN0IG1hdGNoaW5nID0gKHBhdHRlcm4pID0+IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCJidXR0b24sW3JvbGU9YnV0dG9uXSxbcm9sZT10YWJdLGEiKV0KICAgIC5maWx0ZXIoaXRlbT0+dmlzaWJsZShpdGVtKSYmIWl0ZW0uZGlzYWJsZWQmJml0ZW0uZ2V0QXR0cmlidXRlKCJhcmlhLWRpc2FibGVkIikhPT0idHJ1ZSIKICAgICAgJiYgKHBhdHRlcm4udGVzdChjYXB0aW9uKGl0ZW0pKSB8fCBwYXR0ZXJuLnRlc3QoKGl0ZW0uaW5uZXJUZXh0IHx8IGl0ZW0udGV4dENvbnRlbnQgfHwgIiIpLnJlcGxhY2UoL1xzKy9ndSwgIiAiKS50cmltKCkpKSk7CiAgYXN5bmMgZnVuY3Rpb24gY29ubmVjdCgpIHsKICAgIGNvbnN0IHN0YXRlID0gYXdhaXQgc2VuZChwYWlyaW5nID8ge29wOiJwYWlyIix0b2tlbjpwYWlyaW5nWzFdLHBvcnQ6TnVtYmVyKHBhaXJpbmdbMl0gfHwgNzg3Nyl9IDoge29wOiJyZXN1bWUifSk7CiAgICBpZiAoc3RhdGUuZG9uZSkgcmV0dXJuOwogICAgaWYgKHBhaXJpbmcpIGhpc3RvcnkucmVwbGFjZVN0YXRlKGhpc3Rvcnkuc3RhdGUsICIiLCBsb2NhdGlvbi5wYXRobmFtZSArIGxvY2F0aW9uLnNlYXJjaCk7IAogICAgaWYgKGRvY3VtZW50LnJlYWR5U3RhdGUgPT09ICJsb2FkaW5nIikgYXdhaXQgbmV3IFByb21pc2UocmVzb2x2ZT0+ZG9jdW1lbnQuYWRkRXZlbnRMaXN0ZW5lcigiRE9NQ29udGVudExvYWRlZCIscmVzb2x2ZSx7b25jZTp0cnVlfSkpOwogICAgaWYgKCFwcm9qZWN0T3BlbigpKSB7CiAgICAgIGlmICghcGFpcmluZykgdGhyb3cgRXJyb3IoIkZsb3cgduG6q24g4bufIHRyYW5nIGNo4bunIHNhdSBraGkgdOG6oW8gZOG7sSDDoW4uIEtp4buDbSB0cmEgxJHEg25nIG5o4bqtcCBy4buTaSBjaOG6oXkgbOG6oWkuIik7CiAgICAgIGNvbnN0IGNyZWF0ZSA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gewogICAgICAgIGNvbnN0IGNob2ljZXMgPSBtYXRjaGluZygvXig/Oig/OmFkZCg/Ol9jaXJjbGV8X2JveCk/fHBsdXN8XCspXHMqKT8oPzpOZXcgcHJvamVjdHxDcmVhdGUgcHJvamVjdHxDcmVhdGUgbmV3IHByb2plY3R8ROG7sSDDoW4gbeG7m2l8VOG6oW8gZOG7sSDDoW58VOG6oW8gZOG7sSDDoW4gbeG7m2kpJC9pdSk7CiAgICAgICAgY29uc3QgY29udHJvbHMgPSBjaG9pY2VzLmZpbHRlcihpdGVtID0+ICFjaG9pY2VzLnNvbWUob3RoZXIgPT4gb3RoZXIgIT09IGl0ZW0gJiYgaXRlbS5jb250YWlucyhvdGhlcikpKTsKICAgICAgICByZXR1cm4gY29udHJvbHNbMF0gfHwgbnVsbDsKICAgICAgfSwzNSk7CiAgICAgIGlmICghY3JlYXRlKSB0aHJvdyBFcnJvcigiS2jDtG5nIHTDrG0gdGjhuqV5IG7DunQgdOG6oW8gZOG7sSDDoW4gRmxvdy4gS2nhu4NtIHRyYSBDaHJvbWUgxJHDoyDEkcSDbmcgbmjhuq1wIHbDoCB4ZW0gdHJhbmcgRmxvdyBjw7MgdGjDtG5nIGLDoW8gZ8OsLiIpOwogICAgICBjcmVhdGUuY2xpY2soKTsKICAgICAgaWYgKCFhd2FpdCB3YWl0Rm9yKHByb2plY3RPcGVuLDQ1KSkgdGhyb3cgRXJyb3IoIkZsb3cgY2jGsGEgbeG7nyBk4buxIMOhbiBt4bubaSBzYXUga2hpIGLhuqVtIHThuqFvLiIpOwogICAgfQogICAgaWYgKCFhd2FpdCB3YWl0Rm9yKCgpPT5kb2N1bWVudC5xdWVyeVNlbGVjdG9yKCd0ZXh0YXJlYSxbY29udGVudGVkaXRhYmxlPSJ0cnVlIl0nKSw0NSkpCiAgICAgIHRocm93IEVycm9yKCJE4buxIMOhbiBt4bubaSBjaMawYSBoaeG7h24gw7QgcHJvbXB0LiIpOwogICAgCiAgICBhd2FpdCBydW4oKTsKICB9CiAgY29ubmVjdCgpLmNhdGNoKGFzeW5jIGVycm9yID0+IHsKICAgIHRyeSB7YXdhaXQgc2VuZCh7b3A6ImVycm9yIixtZXNzYWdlOmVycm9yLm1lc3NhZ2V9KTt9IGNhdGNoIHsgY29uc29sZS53YXJuKCJW4bq9IFRheTogZ2jDqXAgbuG7kWkgaG/hurdjIGfhu61pIHRy4bqhbmcgdGjDoWkgdGjhuqV0IGLhuqFpLiIsIGVycm9yLm1lc3NhZ2UpOyB9CiAgfSkuZmluYWxseSgoKT0+e3dpbmRvdy5fX3ZlVGF5UnVubmluZz1mYWxzZTt9KTsKfSkoKTs=",
    "popup.html": "PCFkb2N0eXBlIGh0bWw+PGh0bWwgbGFuZz0idmkiPjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij48c3R5bGU+Ym9keXtmb250OjE0cHggc3lzdGVtLXVpO3dpZHRoOjI3MHB4O3BhZGRpbmc6MTRweH08L3N0eWxlPjxiPlbhur0gVGF5IMK3IEdvb2dsZSBGbG93PC9iPjxwPlRyb25nIFbhur0gVGF5LCBi4bqlbSBU4buxIHThuqFvIHZpZGVvIMSR4buDIOG7qW5nIGThu6VuZyBt4bufIHRhYiBGbG93IHbDoCB04buxIGvhur90IG7hu5FpLjwvcD48L2h0bWw+Cg==",
    "manifest.json": "ewogICJtYW5pZmVzdF92ZXJzaW9uIjogMywKICAibmFtZSI6ICJW4bq9IFRheSDCtyBHb29nbGUgRmxvdyIsCiAgInZlcnNpb24iOiAiMS4zLjIwIiwKICAiYWN0aW9uIjogewogICAgImRlZmF1bHRfcG9wdXAiOiAicG9wdXAuaHRtbCIKICB9LAogICJiYWNrZ3JvdW5kIjogewogICAgInNlcnZpY2Vfd29ya2VyIjogIndvcmtlci5qcyIKICB9LAogICJwZXJtaXNzaW9ucyI6IFsKICAgICJzdG9yYWdlIiwKICAgICJhbGFybXMiLAogICAgImRlYnVnZ2VyIiwKICAgICJkb3dubG9hZHMiCiAgXSwKICAiaG9zdF9wZXJtaXNzaW9ucyI6IFsKICAgICJodHRwOi8vMTI3LjAuMC4xLyoiLAogICAgImh0dHBzOi8vKi5nb29nbGV1c2VyY29udGVudC5jb20vKiIsCiAgICAiaHR0cHM6Ly9mbG93Lmdvb2dsZS5jb20vKiIsCiAgICAiaHR0cHM6Ly9sYWJzLmdvb2dsZS8qIgogIF0sCiAgImNvbnRlbnRfc2NyaXB0cyI6IFsKICAgIHsKICAgICAgIm1hdGNoZXMiOiBbCiAgICAgICAgImh0dHBzOi8vZmxvdy5nb29nbGUuY29tLyoiLAogICAgICAgICJodHRwczovL2xhYnMuZ29vZ2xlLyoiCiAgICAgIF0sCiAgICAgICJqcyI6IFsKICAgICAgICAiZmxvdy5qcyIKICAgICAgXSwKICAgICAgInJ1bl9hdCI6ICJkb2N1bWVudF9zdGFydCIKICAgIH0KICBdCn0K",
    "worker.js": "Y29uc3QgYmFzZSA9ICJodHRwOi8vMTI3LjAuMC4xOjc4NzciOwpmdW5jdGlvbiBmbG93VGFiKHVybCkgeyByZXR1cm4gL15odHRwczpcL1wvKGZsb3dcLmdvb2dsZVwuY29tfGxhYnNcLmdvb2dsZSlcLy8udGVzdCh1cmwgfHwgIiIpOyB9CmFzeW5jIGZ1bmN0aW9uIGFwaSh7dG9rZW4sIHBvcnR9LCBwYXRoLCBib2R5KSB7CiAgY29uc3QgciA9IGF3YWl0IGZldGNoKCJodHRwOi8vMTI3LjAuMC4xOiIgKyBwb3J0ICsgcGF0aCwgewogICAgaGVhZGVyczogeyJYLVZlVGF5LVRva2VuIjp0b2tlbiwgLi4uKGJvZHkgPyB7IkNvbnRlbnQtVHlwZSI6Ym9keS50eXBlIHx8ICJhcHBsaWNhdGlvbi9qc29uIn0gOiB7fSl9LAogICAgbWV0aG9kOiBib2R5ID8gIlBPU1QiIDogIkdFVCIsIGJvZHkKICB9KTsKICBpZiAoIXIub2spIHRocm93IEVycm9yKChhd2FpdCByLnRleHQoKSkuc2xpY2UoMCwyNTApKTsKICByZXR1cm4gci5qc29uKCk7Cn0KY29uc3QgcGVuZGluZ0Rvd25sb2FkcyA9IG5ldyBNYXAoKTsKY2hyb21lLmRvd25sb2Fkcy5vbkRldGVybWluaW5nRmlsZW5hbWUuYWRkTGlzdGVuZXIoKGl0ZW0sIHN1Z2dlc3QpID0+IHsKICBjb25zdCBwZW5kaW5nID0gWy4uLnBlbmRpbmdEb3dubG9hZHMudmFsdWVzKCldLmZpbHRlcihlbnRyeSA9PiAhZW50cnkuaWQgJiYgRGF0ZS5ub3coKSA8IGVudHJ5LmRlYWRsaW5lKTsKICBjb25zdCBmcm9tRmxvdyA9IFtpdGVtLnJlZmVycmVyLCBpdGVtLnVybF0uc29tZSh1cmwgPT4KICAgIC9eaHR0cHM6XC9cL2Zsb3dcLmdvb2dsZVwuY29tXC98XmJsb2I6aHR0cHM6XC9cL2Zsb3dcLmdvb2dsZVwuY29tXC8vaS50ZXN0KHVybCB8fCAiIikpOwogIGlmICghZnJvbUZsb3cgfHwgcGVuZGluZy5sZW5ndGggIT09IDEpIHJldHVybjsKICBjb25zdCBlbnRyeSA9IHBlbmRpbmdbMF07CiAgY29uc3QgZXh0ZW5zaW9uID0gL1wuKHBuZ3xqcGU/Z3x3ZWJwKSQvaS5leGVjKGl0ZW0uZmlsZW5hbWUgfHwgIiIpPy5bMV0gfHwgInBuZyI7CiAgZW50cnkuaWQgPSBpdGVtLmlkOwogIHN1Z2dlc3Qoe2ZpbGVuYW1lOiJWZVRheS0ySy8iICsgZW50cnkuc3RlbSArICIuIiArIGV4dGVuc2lvbiwgY29uZmxpY3RBY3Rpb246Im92ZXJ3cml0ZSJ9KTsKfSk7CmNocm9tZS5ydW50aW1lLm9uTWVzc2FnZS5hZGRMaXN0ZW5lcigobSxzZW5kZXIscmVwbHkpID0+IHsKICAoYXN5bmMoKT0+ewogICAgaWYgKCFmbG93VGFiKHNlbmRlci50YWI/LnVybCkpIHRocm93IEVycm9yKCJDaOG7iSBuaOG6rW4gecOqdSBj4bqndSB04burIHRhYiBGbG93LiIpOwogICAgY29uc3Qga2V5ID0gInBhaXJpbmc6IiArIHNlbmRlci50YWIuaWQ7CiAgICBpZiAobS5vcCA9PT0gInBhaXIiKSB7CiAgICAgIGlmICghL15bYS1mMC05XXszMn0kLy50ZXN0KG0udG9rZW4gfHwgIiIpKSB0aHJvdyBFcnJvcigiTcOjIGvhur90IG7hu5FpIGtow7RuZyBo4bujcCBs4buHLiIpOwogICAgICBjb25zdCBwb3J0ID0gbS5wb3J0ID8/IDc4Nzc7CiAgICAgIGlmICghTnVtYmVyLmlzSW50ZWdlcihwb3J0KSB8fCBwb3J0IDwgMTAyNCB8fCBwb3J0ID4gNjU1MzUpIHRocm93IEVycm9yKCJD4buVbmcgVuG6vSBUYXkga2jDtG5nIGjhu6NwIGzhu4cuIik7CiAgICAgIGNvbnN0IGNvbm5lY3Rpb24gPSB7dG9rZW46bS50b2tlbiwgcG9ydH07CiAgICAgIGNvbnN0IGpvYiA9IGF3YWl0IGFwaShjb25uZWN0aW9uLCAiL25leHQiKTsKICAgICAgaWYgKGpvYi5kb25lKSB0aHJvdyBFcnJvcigiVuG6vSBUYXkgY2jGsGEgY8OzIGPhuqNuaCBjaOG7nS4iKTsKICAgICAgYXdhaXQgY2hyb21lLnN0b3JhZ2Uuc2Vzc2lvbi5zZXQoe1trZXldOmNvbm5lY3Rpb259KTsKICAgICAgcmV0dXJuIGpvYjsKICAgIH0KICAgIGNvbnN0IHNhdmVkID0gYXdhaXQgY2hyb21lLnN0b3JhZ2Uuc2Vzc2lvbi5nZXQoa2V5KTsKICAgIGNvbnN0IHBhaXJpbmcgPSB0eXBlb2Ygc2F2ZWRba2V5XSA9PT0gInN0cmluZyIgPyB7dG9rZW46c2F2ZWRba2V5XSxwb3J0Ojc4Nzd9IDogc2F2ZWRba2V5XTsKICAgIGlmIChtLm9wID09PSAicmVzdW1lIikgcmV0dXJuIHBhaXJpbmcgPyBhcGkocGFpcmluZywiL25leHQiKSA6IHtkb25lOnRydWV9OwogICAgaWYgKCFwYWlyaW5nKSB0aHJvdyBFcnJvcigiVGFiIGNoxrBhIMSRxrDhu6NjIFbhur0gVGF5IGvhur90IG7hu5FpLiIpOwogICAgaWYgKG0ub3AgPT09ICJhcm0yayIgfHwgbS5vcCA9PT0gInRyYW5zZmVyMmsiKSB7CiAgICAgIGNvbnN0IGpvYiA9IGF3YWl0IGFwaShwYWlyaW5nLCIvbmV4dCIpOwogICAgICBpZiAoam9iLmRvbmUgfHwgam9iLmpvYiAhPT0gbS5qb2IgfHwgam9iLmluZGV4ICE9PSBtLmluZGV4KSB0aHJvdyBFcnJvcigiVMOhYyB24bulIOG6o25oIDJLIMSRw6MgxJHhu5VpLiIpOwogICAgICBpZiAobS5vcCA9PT0gImFybTJrIikgewogICAgICAgIHBlbmRpbmdEb3dubG9hZHMuc2V0KGtleSx7c3RlbTpqb2Iuam9iICsgIi0iICsgam9iLmluZGV4LCBkZWFkbGluZTpEYXRlLm5vdygpKzE1MDAwMH0pOwogICAgICAgIHJldHVybiB7YXJtZWQ6dHJ1ZX07CiAgICAgIH0KICAgICAgY29uc3QgZW50cnkgPSBwZW5kaW5nRG93bmxvYWRzLmdldChrZXkpOwogICAgICBpZiAoIWVudHJ5Py5pZCkgcmV0dXJuIHtyZWNlaXZlZDpmYWxzZX07CiAgICAgIGNvbnN0IFtpdGVtXSA9IGF3YWl0IGNocm9tZS5kb3dubG9hZHMuc2VhcmNoKHtpZDplbnRyeS5pZH0pOwogICAgICBpZiAoaXRlbT8uc3RhdGUgPT09ICJpbnRlcnJ1cHRlZCIpIHRocm93IEVycm9yKCJDaHJvbWUgxJHDoyBuZ+G6r3QgbMaw4bujdCB04bqjaSDhuqNuaCAySy4iKTsKICAgICAgaWYgKGl0ZW0/LnN0YXRlICE9PSAiY29tcGxldGUiKSByZXR1cm4ge3JlY2VpdmVkOmZhbHNlfTsKICAgICAgY29uc3QgcmVzdWx0ID0gYXdhaXQgYXBpKHBhaXJpbmcsIi9kb3dubG9hZDJrLyIgKyBqb2Iuam9iICsgIi8iICsgam9iLmluZGV4LEpTT04uc3RyaW5naWZ5KHtwYXRoOml0ZW0uZmlsZW5hbWV9KSk7CiAgICAgIHBlbmRpbmdEb3dubG9hZHMuZGVsZXRlKGtleSk7CiAgICAgIHJldHVybiByZXN1bHQ7CiAgICB9CiAgICBpZiAobS5vcCA9PT0gInN1Ym1pdCIpIHsKICAgICAgY29uc3QgdGFiID0gYXdhaXQgY2hyb21lLnRhYnMuZ2V0KHNlbmRlci50YWIuaWQpOwogICAgICBpZiAoIXRhYi5hY3RpdmUgfHwgc2VuZGVyLmZyYW1lSWQgIT09IDAgfHwgbmV3IFVSTCh0YWIudXJsKS5vcmlnaW4gIT09ICJodHRwczovL2Zsb3cuZ29vZ2xlLmNvbSIKICAgICAgICB8fCAhL15cL3Byb2plY3RcL1teL10rKD86XC8uKik/JC8udGVzdChuZXcgVVJMKHRhYi51cmwpLnBhdGhuYW1lKQogICAgICAgIHx8ICFbbS54LG0ueV0uZXZlcnkoTnVtYmVyLmlzRmluaXRlKSB8fCBtLnggPCAwIHx8IG0ueSA8IDAgfHwgbS54ID4gMTAwMDAgfHwgbS55ID4gMTAwMDApCiAgICAgICAgdGhyb3cgRXJyb3IoIlRhYiBk4buxIMOhbiBGbG93IGtow7RuZyBjw7JuIOG7nyB0cuG6oW5nIHRow6FpIHPhurVuIHPDoG5nLiIpOwogICAgICBjb25zdCBqb2IgPSBhd2FpdCBhcGkocGFpcmluZywiL25leHQiKTsKICAgICAgaWYgKGpvYi5kb25lIHx8IGpvYi5qb2IgIT09IG0uam9iKSB0aHJvdyBFcnJvcigiVMOhYyB24bulIFbhur0gVGF5IMSRw6MgxJHhu5VpLiIpOwogICAgICBjb25zdCB0YXJnZXQgPSB7dGFiSWQ6dGFiLmlkfTsKICAgICAgYXdhaXQgY2hyb21lLmRlYnVnZ2VyLmF0dGFjaCh0YXJnZXQsIjEuMyIpOwogICAgICB0cnkgewogICAgICAgIGNvbnN0IGlucHV0ID0gKHR5cGUsYnV0dG9ucykgPT4gY2hyb21lLmRlYnVnZ2VyLnNlbmRDb21tYW5kKHRhcmdldCwiSW5wdXQuZGlzcGF0Y2hNb3VzZUV2ZW50IiwKICAgICAgICAgIHt0eXBlLHg6bS54LHk6bS55LGJ1dHRvbjoibGVmdCIsYnV0dG9ucyxjbGlja0NvdW50OjF9KTsKICAgICAgICBhd2FpdCBpbnB1dCgibW91c2VQcmVzc2VkIiwxKTsKICAgICAgICBhd2FpdCBpbnB1dCgibW91c2VSZWxlYXNlZCIsMCk7CiAgICAgICAgaWYgKG0uY2xpY2tzID09PSAyKSB7CiAgICAgICAgICBjb25zdCBzZWNvbmQgPSAodHlwZSxidXR0b25zKSA9PiBjaHJvbWUuZGVidWdnZXIuc2VuZENvbW1hbmQodGFyZ2V0LCJJbnB1dC5kaXNwYXRjaE1vdXNlRXZlbnQiLAogICAgICAgICAgICB7dHlwZSx4Om0ueCx5Om0ueSxidXR0b246ImxlZnQiLGJ1dHRvbnMsY2xpY2tDb3VudDoyfSk7CiAgICAgICAgICBhd2FpdCBzZWNvbmQoIm1vdXNlUHJlc3NlZCIsMSk7CiAgICAgICAgICBhd2FpdCBzZWNvbmQoIm1vdXNlUmVsZWFzZWQiLDApOwogICAgICAgIH0KICAgICAgfSBmaW5hbGx5IHsKICAgICAgICBhd2FpdCBjaHJvbWUuZGVidWdnZXIuZGV0YWNoKHRhcmdldCk7CiAgICAgIH0KICAgICAgcmV0dXJuIHtjbGlja2VkOnRydWV9OwogICAgfQogICAgaWYgKG0ub3AgPT09ICJhY3RpdmF0ZSIpIHsKICAgICAgYXdhaXQgY2hyb21lLnRhYnMudXBkYXRlKHNlbmRlci50YWIuaWQsIHthY3RpdmU6dHJ1ZX0pOwogICAgICBhd2FpdCBjaHJvbWUud2luZG93cy51cGRhdGUoc2VuZGVyLnRhYi53aW5kb3dJZCwge2ZvY3VzZWQ6dHJ1ZX0pOwogICAgICByZXR1cm4ge2FjdGl2ZTp0cnVlfTsKICAgIH0KICAgIGlmIChtLm9wID09PSAibmV4dCIpIHJldHVybiBhcGkocGFpcmluZywiL25leHQiKTsKICAgIGlmIChtLm9wID09PSAiYmF0Y2giKSByZXR1cm4gYXBpKHBhaXJpbmcsIi9iYXRjaCIpOwogICAgaWYgKG0ub3AgPT09ICJwcm9ncmVzcyIpIHJldHVybiBhcGkocGFpcmluZywiL3Byb2dyZXNzIixKU09OLnN0cmluZ2lmeSh7Y291bnQ6bS5jb3VudCxzdGFnZTptLnN0YWdlfSkpOwogICAgaWYgKG0ub3AgPT09ICJlcnJvciIpIHJldHVybiBhcGkocGFpcmluZywiL2Vycm9yIixKU09OLnN0cmluZ2lmeSh7bWVzc2FnZTptLm1lc3NhZ2V9KSk7CiAgICBpZiAobS5vcCA9PT0gImltYWdlIiAmJiAvXlthLWYwLTldezMyfSQvLnRlc3QobS5qb2IpICYmIE51bWJlci5pc0ludGVnZXIobS5pbmRleCkpIHsKICAgICAgbGV0IGltYWdlOwogICAgICBpZiAobS5kYXRhICYmIC9eZGF0YTppbWFnZVwvKHBuZ3x3ZWJwfGpwZWcpO2Jhc2U2NCwvLnRlc3QobS5kYXRhKSkgewogICAgICAgIGltYWdlID0gYXdhaXQgKGF3YWl0IGZldGNoKG0uZGF0YSkpLmJsb2IoKTsKICAgICAgfSBlbHNlIHsKICAgICAgICBjb25zdCB1ID0gbmV3IFVSTChtLnVybCk7CiAgICAgICAgaWYgKHUucHJvdG9jb2wgIT09ICJodHRwczoiIHx8ICEvKF58XC4pZ29vZ2xldXNlcmNvbnRlbnRcLmNvbSQvLnRlc3QodS5ob3N0bmFtZSkpIHRocm93IEVycm9yKCJLaMO0bmcgeMOhYyDEkeG7i25oIMSRxrDhu6NjIMSR4buLYSBjaOG7iSDhuqNuaCBr4bq/dCBxdeG6oy4iKTsKICAgICAgICBjb25zdCByID0gYXdhaXQgZmV0Y2godS5ocmVmLHtjcmVkZW50aWFsczoib21pdCIscmVkaXJlY3Q6ImVycm9yIn0pOwogICAgICAgIGlmICghci5vaykgdGhyb3cgRXJyb3IoIktow7RuZyB04bqjaSDEkcaw4bujYyDhuqNuaCBr4bq/dCBxdeG6oy4iKTsKICAgICAgICBpbWFnZSA9IGF3YWl0IHIuYmxvYigpOwogICAgICB9CiAgICAgIGlmIChpbWFnZS5zaXplID4gMjQqMTAyNCoxMDI0IHx8ICFpbWFnZS50eXBlLnN0YXJ0c1dpdGgoImltYWdlLyIpKSB0aHJvdyBFcnJvcigi4bqibmgga+G6v3QgcXXhuqMga2jDtG5nIGjhu6NwIGzhu4cuIik7CiAgICAgIHJldHVybiBhcGkocGFpcmluZywiL2ltYWdlLyIrbS5qb2IrIi8iK20uaW5kZXgsaW1hZ2UpOwogICAgfQogICAgdGhyb3cgRXJyb3IoIlnDqnUgY+G6p3Uga2jDtG5nIGjhu6NwIGzhu4cuIik7CiAgfSkoKS50aGVuKHJlc3VsdD0+cmVwbHkoe29rOnRydWUscmVzdWx0fSksZXJyb3I9PnJlcGx5KHtvazpmYWxzZSxlcnJvcjplcnJvci5tZXNzYWdlfSkpOwogIHJldHVybiB0cnVlOwp9KTsKY2hyb21lLnRhYnMub25SZW1vdmVkLmFkZExpc3RlbmVyKHRhYklkID0+IGNocm9tZS5zdG9yYWdlLnNlc3Npb24ucmVtb3ZlKCJwYWlyaW5nOiIgKyB0YWJJZCkpOwoKY29uc3QgZXh0ZW5zaW9uQ2hlY2sgPSAidmV0YXktY2hlY2stbG9jYWwtdXBkYXRlIjsKZnVuY3Rpb24gcmVnaXN0ZXJDaGVjaygpIHsKICBjaHJvbWUuYWxhcm1zLmNyZWF0ZShleHRlbnNpb25DaGVjaywge3BlcmlvZEluTWludXRlczoxfSk7Cn0KY2hyb21lLnJ1bnRpbWUub25JbnN0YWxsZWQuYWRkTGlzdGVuZXIocmVnaXN0ZXJDaGVjayk7CmNocm9tZS5ydW50aW1lLm9uU3RhcnR1cC5hZGRMaXN0ZW5lcihyZWdpc3RlckNoZWNrKTsKY2hyb21lLmFsYXJtcy5vbkFsYXJtLmFkZExpc3RlbmVyKGFzeW5jIGFsYXJtID0+IHsKICBpZiAoYWxhcm0ubmFtZSAhPT0gZXh0ZW5zaW9uQ2hlY2spIHJldHVybjsKICB0cnkgewogICAgY29uc3QgY3VycmVudCA9IGNocm9tZS5ydW50aW1lLmdldE1hbmlmZXN0KCk7CiAgICBjb25zdCByZXNwb25zZSA9IGF3YWl0IGZldGNoKGNocm9tZS5ydW50aW1lLmdldFVSTCgibWFuaWZlc3QuanNvbiIpICsgIj90PSIgKyBEYXRlLm5vdygpLCB7Y2FjaGU6Im5vLXN0b3JlIn0pOwogICAgaWYgKCFyZXNwb25zZS5vaykgcmV0dXJuOwogICAgY29uc3QgZGlzayA9IGF3YWl0IHJlc3BvbnNlLmpzb24oKTsKICAgIGlmICghL15cZCtcLlxkK1wuXGQrJC8udGVzdChkaXNrLnZlcnNpb24pIHx8IGRpc2submFtZSAhPT0gY3VycmVudC5uYW1lIHx8CiAgICAgICAgZGlzay5tYW5pZmVzdF92ZXJzaW9uICE9PSBjdXJyZW50Lm1hbmlmZXN0X3ZlcnNpb24pIHJldHVybjsKICAgIGNvbnN0IG9sZCA9IGN1cnJlbnQudmVyc2lvbi5zcGxpdCgiLiIpLm1hcChOdW1iZXIpOwogICAgY29uc3QgbmV4dCA9IGRpc2sudmVyc2lvbi5zcGxpdCgiLiIpLm1hcChOdW1iZXIpOwogICAgY29uc3QgY2hhbmdlZCA9IG5leHQuZmluZEluZGV4KCh2YWx1ZSxpKT0+dmFsdWUhPT1vbGRbaV0pOwogICAgaWYgKGNoYW5nZWQgPCAwIHx8IG5leHRbY2hhbmdlZF0gPCBvbGRbY2hhbmdlZF0pIHJldHVybjsKICAgIGZvcihjb25zdCBrZXkgb2YgWyJwZXJtaXNzaW9ucyIsImhvc3RfcGVybWlzc2lvbnMiLCJvcHRpb25hbF9wZXJtaXNzaW9ucyIsIm9wdGlvbmFsX2hvc3RfcGVybWlzc2lvbnMiXSkgewogICAgICBpZihKU09OLnN0cmluZ2lmeShbLi4uKGN1cnJlbnRba2V5XXx8W10pXS5zb3J0KCkpIT09SlNPTi5zdHJpbmdpZnkoWy4uLihkaXNrW2tleV18fFtdKV0uc29ydCgpKSkgcmV0dXJuOwogICAgfQogICAgY29uc3QgbWF0Y2hlcyA9IHZhbHVlID0+ICh2YWx1ZS5jb250ZW50X3NjcmlwdHMgfHwgW10pLmZsYXRNYXAoc2NyaXB0PT5zY3JpcHQubWF0Y2hlcyB8fCBbXSkuc29ydCgpOwogICAgaWYoSlNPTi5zdHJpbmdpZnkobWF0Y2hlcyhjdXJyZW50KSkhPT1KU09OLnN0cmluZ2lmeShtYXRjaGVzKGRpc2spKSkgcmV0dXJuOwogICAgY29uc3QgaWRsZSA9IGF3YWl0IGZldGNoKGJhc2UrIi9leHRlbnNpb24taWRsZSIse2NhY2hlOiJuby1zdG9yZSJ9KTsKICAgIGlmKGlkbGUub2sgJiYgKGF3YWl0IGlkbGUuanNvbigpKS5pZGxlID09PSB0cnVlKSBjaHJvbWUucnVudGltZS5yZWxvYWQoKTsKICB9IGNhdGNoIHt9Cn0pOwo="
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
                        elif index < len(state["prompts"]) and self.path in (
                            f"/image/{state['id']}/{index}", f"/download2k/{state['id']}/{index}"
                        ):
                            payload = self.rfile.read(size)
                            if len(payload) != size:
                                raise ValueError("Ảnh chưa tải đủ.")
                            if self.path.startswith("/download2k/"):
                                if size > 8192:
                                    raise ValueError("Đường dẫn ảnh 2K không hợp lệ.")
                                downloaded = Path(json.loads(payload)["path"])
                                expected = f"{state['id']}-{index}"
                                if (not downloaded.is_absolute() or downloaded.stem != expected
                                        or downloaded.parent.name != "VeTay-2K" or downloaded.is_symlink()
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

    def run(script, voice, pictures, provider, ratio, highlights, render, output_folder=None, shared_prompt="", full_resolution=True, progress=gr.Progress()):
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
                path = render_folder(wav, pictures, plan, dest, report, ratio, highlights, full_resolution)
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

    def generate_video(script, voice, pictures, provider, ratio, highlights, output_folder, shared_prompt, full_resolution):
        yield from run(script, voice, pictures, provider, ratio, highlights, True, output_folder, shared_prompt, full_resolution)

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
        full_resolution = gr.Checkbox(value=True, label="Dựng ở độ phân giải đầy đủ")
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
            generate_video, [script, voice, pictures, provider, ratio, highlights, output_folder, shared_prompt, full_resolution],
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

