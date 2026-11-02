import base64
import io
import json
import os
import time
import urllib.request
from PIL import Image

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/"

# Modelos válidos
MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite", # Substituiu o 2.5
]

# Prompt direto e focado
PROMPT = (
    "Read the 2 characters shown in this image. "
    "Ignore the word 'Captcha' and the colored diagonal line. "
    "Reply with ONLY the 2 characters, uppercase, no spaces."
)

def _prepare_image(img_bytes: bytes) -> str:
    # Vamos simplificar: apenas abrir a imagem e aumentar um pouco a resolução para a IA enxergar melhor
    pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    
    # Aumenta a imagem em 3x para o Gemini ler com mais facilidade
    pil_img = pil_img.resize((pil_img.width * 3, pil_img.height * 3), Image.LANCZOS)
    
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=95)
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
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            
            candidates = data.get("candidates", [])
            if not candidates:
                continue
                
            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
            
            # Limpa a resposta, mantendo apenas letras e números
            clean = "".join(c for c in text.upper() if c.isalnum())
            
            if len(clean) >= 2:
                print(f"[captcha] {model}: {text!r} -> '{clean[:2]}'")
                return clean[:2] # Pega os dois primeiros caracteres
            else:
                print(f"[captcha] {model}: resposta inválida: {text!r}")
                
        except urllib.error.HTTPError as e:
            print(f"[captcha] {model}: HTTP {e.code}")
            continue
        except Exception as e:
            print(f"[captcha] {model}: {e}")
            continue

    print("[captcha] todos os modelos falharam")
    return ""
