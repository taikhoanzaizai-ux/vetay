# V? Tay

?ng d?ng Windows t?o video v? tay d?c t? k?ch b?n ti?ng Vi?t ho?c b?n thu. B? d?ng g?i tr?c ti?p Whiteboard Animation VI tr�n CPU. ?ng d?ng m? giao di?n di?u khi?n n?i b? t?i c?ng 7871.

## C�i d?t tr�n Windows

1. C�i Git v� FFmpeg. D?m b?o l?nh `ffmpeg` ch?y du?c ho?c c?u h�nh bi?n `FFMPEG_BINARY`.
2. C�i Whiteboard Animation VI c�ng c�c thu vi?n theo hu?ng d?n upstream. Du?ng d?n m?c d?nh l� `%USERPROFILE%\Downloads\whiteboard-animation-vi-main`. N?u v? tr� kh�c, d?t bi?n m�i tru?ng `VETAY_ENGINE`.
3. Clone repo b?ng `git clone https://github.com/taikhoanzaizai-ux/vetay.git`. B?n Download ZIP thi?u l?ch s? Git d�ng d? c?p nh?t.
4. B?m d�p `Cai_Ve_Tay.bat`. L?n sau m? b?ng `Mo_Ve_Tay.bat`.

T�y ch?n t� nh�n v?t tru?c du?c d�ng n?u trong engine c� `assets/v181d/model.safetensors`.

## S? d?ng

- Nh?p k?ch b?n ho?c dua file voice. N?u c� c? hai, video d�ng l?i d?c trong voice. Edge TTS c?n m?ng d? t?o gi?ng d?c. Whisper t?i model ? l?n nh?n d?ng d?u ti�n.
- Ch? d? **?nh c� s?n** nh?n danh s�ch ?nh theo th? t? l?i d?c. S? c?nh tr�n giao di?n l� gi?i h?n t?i da.
- Ch? d? **Google Flow (th? nghi?m)** d�ng Microsoft Edge v?i h? so ri�ng. Dang nh?p tr?c ti?p v?i Google, m? d? �n, ch?n ch? d? ?nh v� d�ng model da ch?n trong ?ng d?ng. Lu?ng th? d?a v�o c�c n�t hi?n th? c?a Flow. C� th? d?ng khi b? c?c ho?c quy?n truy c?p thay d?i. Chua ki?m th? t?o ?nh v?i m?t t�i kho?n Flow dang dang nh?p.
- Video du?c luu trong `%LOCALAPPDATA%\VeTay`. Giao di?n cho xem v� t?i MP4. B?n d?u chua t? dang video l�n Canva.

## C?p nh?t qua GitHub

N�t **C?p nh?t qua GitHub** l?y nh�nh `main` c?a repo g?c v� ch? c?p nh?t fast-forward tr�n b?n clone s?ch. Khi c� thay d?i, app tho�t d? `Mo_Ve_Tay.bat` m? l?i. Ch?y l?i file c�i n?u m?t b?n c?p nh?t y�u c?u ph? thu?c m?i.

Whiteboard Animation VI du?c c�i ri�ng theo [repo upstream](https://github.com/letuandat131/whiteboard-animation-vi).

