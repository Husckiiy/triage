# language: Python 3.11+, file: daily_triage.py
import asyncio
import json
import logging
import os
import re
import secrets
import string
import sys
import time
import urllib.request
import io  # <--- CORREÇÃO AQUI: Adicionado o import io
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
from PIL import Image
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# Importa as funções do seu outro arquivo para não duplicar código
from captcha_ocr import solve_captcha, _prepare_image

try:
    import cv2
except ImportError:
    cv2 = None

load_dotenv()

MAILTM    = "https://api.mail.tm"
FORM_URL  = "https://www.testersgc.fun/plan4z4k/afi9egnzv2"
IBO_LOGIN = "https://iboplayer.com/dashboard"
IBO_EDIT  = os.getenv("IBO_PLAYLIST_EDIT_URL")

IBO_MAC    = os.getenv("IBO_MAC")
IBO_DEVKEY = os.getenv("IBO_DEVKEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

if not all([IBO_MAC, IBO_DEVKEY, IBO_EDIT]):
    print("[!] .env incompleto")
    sys.exit(3)

BASE_DIR   = Path(os.getenv("DATA_DIR", str(Path(__file__).parent)))
LOCK_FILE  = Path("/tmp/daily_triage.lock")
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE   = BASE_DIR / "triage.log"
M3U_OUT    = BASE_DIR / "playlists"
COOKIES    = BASE_DIR / "iboplayer_state.json"

MAX_RETRIES   = 3
RETRY_BACKOFF = [30, 60, 120]
CAPTCHA_TRIES = 5

M3U_OUT.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("triage")

# ... (Mantenha as funções acquire_lock, release_lock, load_state, save_state, rand_phone_digits, rand_name, make_inbox, wait_for_email, extract_m3u, submit_form, _accept_modals EXATAMENTE como estavam) ...

async def iboplayer_login(page):
    await page.goto(IBO_LOGIN, wait_until="networkidle")
    await page.wait_for_timeout(2000)

    for attempt in range(CAPTCHA_TRIES):
        log.info(f"captcha tentativa {attempt + 1}/{CAPTCHA_TRIES}")

        await _accept_modals(page)

        if not await page.query_selector("#max-address"):
            log.info("sessao iboplayer ja ativa")
            return

        await page.fill("#max-address", IBO_MAC)
        await page.fill("#device-key", IBO_DEVKEY)

        svg = await page.query_selector('form svg')
        if not svg:
            log.warning("captcha svg nao encontrado")
            await page.reload(wait_until="networkidle")
            await page.wait_for_timeout(1500)
            continue

        await page.evaluate("""
            () => {
                const svg = document.querySelector('form svg');
                const bb = svg.getBBox();
                svg.setAttribute('viewBox', `${bb.x} ${bb.y} ${bb.width} ${bb.height}`);
                svg.setAttribute('width', bb.width * 4);
                svg.setAttribute('height', bb.height * 4);
            }
        """)
        await page.wait_for_timeout(300)

        svg = await page.query_selector('form svg')
        img_bytes = await svg.screenshot()
        
        # AQUI ESTÁ A CHAMADA PARA O GEMINI
        answer = await solve_captcha(img_bytes)
        log.info(f"captcha lido: '{answer}'")

        if len(answer) != 2:
            log.warning("resposta invalida, tentando de novo")
            await page.reload(wait_until="networkidle")
            await page.wait_for_timeout(1500)
            await asyncio.sleep(10)
            continue

        captcha_input = await page.query_selector('form input[type="text"]:not(#max-address)')
        await captcha_input.fill(answer)

        await _accept_modals(page)

        try:
            await page.click('button:has-text("LOGIN")', timeout=5000)
        except Exception:
            log.info("clique normal falhou, usando JS")
            await page.evaluate("""
                () => {
                    const btn = Array.from(document.querySelectorAll('button'))
                        .find(b => b.innerText.trim().toUpperCase() === 'LOGIN');
                    if (btn) btn.click();
                }
            """)

        await page.wait_for_timeout(2500)

        if not await page.query_selector('#max-address'):
            log.info("login ok")
            return

        log.warning("captcha errado ou modal bloqueou, tentando de novo")
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(1500)

    raise RuntimeError(f"captcha falhou apos {CAPTCHA_TRIES} tentativas")

# ... (Mantenha as funções iboplayer_update, fetch_m3u, run_daily, main EXATAMENTE como estavam) ...
