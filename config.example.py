"""
Sozlamalar namunasi.

Ishlatish:
    cp config.example.py config.py
va o'z ma'lumotlaringiz bilan to'ldiring. (config.py git'ga qo'shilmaydi.)
"""

# === PLATFORMA ===
# "hikconnect" — Hikvision / Hik-Connect hisoblari
# "ezviz"      — EZVIZ hisoblari
PLATFORM = "hikconnect"

# === HISOB MA'LUMOTLARI ===
EMAIL    = "your_email@example.com"
PASSWORD = "your_password"

# Region/domen. Odatda None qoldiring — login paytida avtomatik aniqlanadi.
# Kerak bo'lsa: hikconnect -> "api.hik-connect.com", ezviz -> "apiisgp.ezvizlife.com"
REGION   = None

# === STREAM SOZLAMALARI ===
PROXY_START_PORT = 8700     # har kameraga ketma-ket port
DISPLAY_WIDTH    = 1280     # ko'rsatish o'lchami (grid uchun kichraytiring: 854x480)
DISPLAY_HEIGHT   = 720
USE_GPU          = True     # NVIDIA NVDEC; ishlamasa avtomatik dasturiy dekodga o'tadi

# === FAYLLAR ===
TOKEN_FILE  = "token.json"
CAMKEY_FILE = "cam_keys.json"
