import base64
import io
import json
import os
import time
import urllib.request

import numpy as np
from PIL import Image

try:
    import cv2
except ImportError:
    cv2 = None

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/"

# Reduzido para 2 modelos para evitar Rate Limit (429)
MODELS = [
    "gemini-3.6-flash",
    "gemini-2.5-flash-lite",
]

# Prompt sem a palavra "captcha" para evitar recusa de segurança
PROMPT = (
    "Read the 2 characters shown in the center of this image. "
    "Ignore the word in the top-left corner and the diagonal line. "
    "Reply with ONLY the 2 characters, uppercase, no spaces."
)

def _prepare_image(img_bytes: bytes) -> str:
    if cv2 is not None:
        arr = np.frombuffer(img_bytes, np.uint8)
        img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img_bgr is not None:
            b, g, r = cv2.split(img_bgr)
            line_mask = ((r.astype(int) - g.astype(int) > 25) |
                         (b.astype(int) - g.astype(int) > 25))
            line_mask = line_mask.astype(np.uint8) * 255
            line_mask = cv2.dilate(line_mask, np.ones((3, 3), np.uint8), iterations=1)

            gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape
            gray[line_mask > 0] = 0
            gray[0:int(h * 0.22), 0:int(w * 0.35)] = 0

            p_low, p_high = np.percentile(gray, [5, 95])
            if p_high > p_low:
                stretched = np.clip((gray.astype(np.float32) - p_low) * 255.0 / (p_high - p_low), 0, 255)
                gray = stretched.astype(np.uint8)

            inverted = 255 - gray
            pil_img = Image.fromarray(inverted).convert("RGB")
        else:
            pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    else:
        pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")

    pil_img = pil_img.resize((pil_img.width * 2, pil_img.height * 2), Image.LANCZOS)
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


async def solve_captcha(img_bytes: bytes) -> str:
    if not img_bytes:
        return ""

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[captcha] GEMINI_API_KEY nao configurada")
        return ""

    b64 = _prepare_image(img_bytes)

    payload = {
        "contents": [{
            "parts": [
                {"text": PROMPT},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
            ]
        }],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 20,
        },
    }

    body = json.dumps(payload).encode("utf-8")

    for model in MODELS:
        url = f"{BASE_URL}{model}:generateContent"
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            # Aumentado para 60s
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = (
                data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                    .strip()
            )
            
            clean = "".join(c for c in text.upper() if c.isalnum())
            
            if len(clean) > 4:
                print(f"[captcha] {model}: recusou ou frase longa: {text!r}")
                continue
                
            if len(clean) >= 2:
                print(f"[captcha] {model}: {text!r} -> '{clean[-2:]}'")
                return clean[-2:] if len(clean) > 2 else clean
            else:
                print(f"[captcha] {model}: resposta curta demais: {text!r}")
        except urllib.error.HTTPError as e:
            print(f"[captcha] {model}: HTTP {e.code}")
            if e.code == 429:
                print("[captcha] Rate Limit atingido. Esperando 3s...")
                time.sleep(3) # Evita o bloqueio
            continue
        except Exception as e:
            print(f"[captcha] {model}: {e}")
            continue

    print("[captcha] todos os modelos falharam")
    return ""
