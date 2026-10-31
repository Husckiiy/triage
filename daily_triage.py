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
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
from PIL import Image
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

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

def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            if os.name != "nt":
                os.kill(pid, 0)
            log.warning(f"lock ativo por pid {pid}")
            return False
        except (ProcessLookupError, ValueError):
            pass
    LOCK_FILE.write_text(str(os.getpid()))
    return True

def release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"runs": [], "last_success": None}

def save_state(s: dict):
    s["runs"] = s["runs"][-30:]
    STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))

def rand_phone_digits() -> str:
    ddd = secrets.choice([11, 21, 31, 41, 51, 61, 71, 81, 91])
    return f"{ddd}9{secrets.randbelow(10**8):08d}"

def rand_name() -> tuple[str, str]:
    first = ["Lucas", "Marina", "Rafael", "Beatriz", "Thiago", "Camila"]
    last  = ["Almeida", "Costa", "Ferreira", "Rocha", "Barbosa", "Mendes"]
    return secrets.choice(first), secrets.choice(last)

async def make_inbox() -> tuple[str, str, str]:
    async with httpx.AsyncClient(timeout=20) as c:
        domains = (await c.get(f"{MAILTM}/domains")).json()
        domain = domains["hydra:member"][0]["domain"]
        local = "t" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(11))
        addr = f"{local}@{domain}"
        pwd = secrets.token_urlsafe(12)
        await c.post(f"{MAILTM}/accounts", json={"address": addr, "password": pwd})
        tok = (await c.post(f"{MAILTM}/token", json={"address": addr, "password": pwd})).json()["token"]
        return addr, pwd, tok

async def wait_for_email(token: str, timeout_s: int = 300) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=20, headers=headers) as c:
        for _ in range(timeout_s // 5):
            await asyncio.sleep(5)
            msgs = (await c.get(f"{MAILTM}/messages")).json()
            if msgs.get("hydra:member"):
                mid = msgs["hydra:member"][0]["id"]
                return (await c.get(f"{MAILTM}/messages/{mid}")).json()
    raise TimeoutError("sem email na janela")

def extract_m3u(body: str) -> str | None:
    m = re.search(r"https?://[^\s\"'<>]+/get\.php\?username=[^\s\"'<>]+&password=[^\s\"'<>]+&type=m3u[^\s\"'<>]*", body, re.I)
    if m: return m.group(0)
    m = re.search(r"https?://[^\s\"'<>]+\.m3u8?(?:\?[^\s\"'<>]*)?", body, re.I)
    return m.group(0) if m else None

async def submit_form(page, inbox_addr: str):
    first, last = rand_name()
    await page.goto(FORM_URL, wait_until="networkidle")
    await page.wait_for_timeout(1500)

    for txt in ["Aceitar Todos", "Accept", "OK"]:
        try:
            await page.click(f'button:has-text("{txt}")', timeout=1500)
            await page.wait_for_timeout(500)
        except PWTimeout:
            pass

    await page.get_by_placeholder("Digite seu nome completo").fill(f"{first} {last}")
    await page.get_by_placeholder("seu@email.com").fill(inbox_addr)
    await page.get_by_placeholder("(11) 99999-9999").fill(rand_phone_digits())

    for cb in await page.query_selector_all('input[type="checkbox"]'):
        if not await cb.is_checked():
            await cb.check()
            await page.wait_for_timeout(150)

    await page.get_by_role("button", name="Ativar Teste Gratuito").click()
    await page.wait_for_load_state("networkidle")
    log.info("form testersgc submetido")

async def _accept_modals(page):
    for _ in range(3):
        modal_aceito = False
        for txt in ["Accept legal terms", "Aceitar Todos", "Accept"]:
            try:
                btn = await page.query_selector(f'button:has-text("{txt}")')
                if btn and await btn.is_visible():
                    await btn.click(timeout=3000)
                    log.info(f"modal aceito: {txt}")
                    await page.wait_for_timeout(1500)
                    modal_aceito = True
                    break
            except Exception:
                pass
        if not modal_aceito:
            break

def _prepare_image(img_bytes: bytes) -> str:
    import base64
    if cv2 is not None:
        arr = np.frombuffer(img_bytes, np.uint8)
        img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img_bgr is not None:
            b, g, r = cv2.split(img_bgr)
            line_mask = ((r.astype(int) - g.astype(int) > 25) | (b.astype(int) - g.astype(int) > 25))
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
    if not img_bytes: return ""
    api_key = GEMINI_KEY
    if not api_key: return ""
    b64 = _prepare_image(img_bytes)
    MODELS = ["gemini-flash-latest", "gemini-3.6-flash"]
    PROMPT = "Read the 2 characters shown in the center of this image. Ignore the word in the top-left corner and the diagonal line. Reply with ONLY the 2 characters, uppercase, no spaces."
    payload = {"contents": [{"parts": [{"text": PROMPT}, {"inline_data": {"mime_type": "image/jpeg", "data": b64}}]}], "generationConfig": {"temperature": 0.0, "maxOutputTokens": 20}}
    body = json.dumps(payload).encode("utf-8")
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/"
    for model in MODELS:
        url = f"{BASE_URL}{model}:generateContent"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "x-goog-api-key": api_key}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", [])
            if not candidates: continue
            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
            clean = "".join(c for c in text.upper() if c.isalnum())
            if len(clean) > 4: continue
            if len(clean) >= 2:
                print(f"[captcha] {model}: {text!r} -> '{clean[-2:]}'")
                return clean[-2:] if len(clean) > 2 else clean
        except Exception as e:
            print(f"[captcha] {model}: {e}")
            continue
    return ""

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

async def iboplayer_update(m3u_url: str) -> bool:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        ctx_kwargs = dict(locale="pt-BR", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        if COOKIES.exists(): ctx_kwargs["storage_state"] = str(COOKIES)
        ctx = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()
        try:
            await iboplayer_login(page)
            await ctx.storage_state(path=str(COOKIES))
            await page.goto(IBO_EDIT, wait_until="networkidle")
            await page.wait_for_timeout(2000)
            await _accept_modals(page)
            if "/edit/" not in page.url:
                await page.goto(IBO_EDIT, wait_until="networkidle")
                await page.wait_for_timeout(2000)
            await page.evaluate("() => { const modals = document.querySelectorAll('[class*=\"modal\"], [class*=\"terms\"]'); modals.forEach(m => m.remove()); }")
            await page.wait_for_timeout(1000)
            await page.wait_for_selector("#playlist-url", timeout=60000, state="visible")
            url_antiga = await page.input_value("#playlist-url")
            if url_antiga == m3u_url: return True
            await page.fill("#playlist-url", m3u_url)
            await page.wait_for_timeout(300)
            try:
                async with page.expect_navigation(timeout=10000, wait_until="networkidle"):
                    await page.click('button:has-text("SAVE")', timeout=5000)
            except Exception:
                pass
            await _accept_modals(page)
            if "/edit/" not in page.url:
                await page.goto(IBO_EDIT, wait_until="networkidle")
                await page.wait_for_timeout(2000)
                await page.wait_for_selector("#playlist-url", timeout=15000, state="visible")
            url_nova = await page.input_value("#playlist-url")
            return url_nova == m3u_url
        finally:
            await browser.close()

async def fetch_m3u() -> str | None:
    inbox_addr, _, token = await make_inbox()
    log.info(f"inbox: {inbox_addr}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(locale="pt-BR", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        page = await ctx.new_page()
        try:
            await submit_form(page, inbox_addr)
        finally:
            await browser.close()
    msg = await wait_for_email(token)
    body = msg.get("text") or " ".join(msg.get("html", []))
    return extract_m3u(body)

async def run_daily() -> bool:
    state = load_state()
    run_id = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for i in range(MAX_RETRIES):
        try:
            m3u = await fetch_m3u()
            if not m3u:
                if i < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF[i])
                    continue
                return False
            fname = M3U_OUT / f"{datetime.now().strftime('%Y-%m-%d')}.m3u"
            fname.write_text(m3u, encoding="utf-8")
            ibo_ok = await iboplayer_update(m3u)
            state["runs"].append({"id": run_id, "ok": ibo_ok, "file": str(fname)})
            if ibo_ok: state["last_success"] = run_id
            save_state(state)
            return ibo_ok
        except Exception as e:
            log.error(f"erro: {e}")
            if i < MAX_RETRIES - 1: await asyncio.sleep(RETRY_BACKOFF[i])
    return False

async def main():
    if not acquire_lock(): sys.exit(2)
    try:
        ok = await run_daily()
        sys.exit(0 if ok else 1)
    finally:
        release_lock()

if __name__ == "__main__":
    asyncio.run(main())
