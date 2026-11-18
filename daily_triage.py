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
import io
import random
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
from PIL import Image
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# Importa as funções de OCR do outro arquivo
from captcha_ocr import solve_captcha, _prepare_image

try:
    import cv2
except ImportError:
    cv2 = None

load_dotenv()

# 🚨 MUDANÇA: URL base do 1SecMail
MAILTM    = "https://www.1secmail.com/api/v1/"
FORM_URL  = "https://www.testersgc.fun/plan4z4k/afi9egnzv2"
IBO_LOGIN = "https://iboplayer.com/dashboard"
IBO_EDIT  = os.getenv("IBO_PLAYLIST_EDIT_URL")

IBO_MAC    = os.getenv("IBO_MAC")
IBO_DEVKEY = os.getenv("IBO_DEVKEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

if not all([IBO_MAC, IBO_DEVKEY, IBO_EDIT]):
    print("[!] .env incompleto (Faltam IBO_MAC, IBO_DEVKEY ou IBO_EDIT)")
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

# 🚨 NOVA FUNÇÃO: make_inbox para o 1SecMail
async def make_inbox() -> tuple[str, str, str]:
    local = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    
    async with httpx.AsyncClient(timeout=20) as c:
        resp = await c.get(f"{MAILTM}?action=getDomainList")
        if resp.status_code != 200:
            raise Exception("Falha ao obter domínios do 1SecMail")
        
        domains = resp.json()
        if not domains:
            raise Exception("Nenhum domínio disponível no 1SecMail")
        
        domain = random.choice(domains)
        addr = f"{local}@{domain}"
        
        log.info(f"✅ E-mail temporário criado (1SecMail): {addr}")
        return addr, local, domain

# 🚨 NOVA FUNÇÃO: wait_for_email para o 1SecMail
async def wait_for_email(inbox_addr: str, timeout_s: int = 600) -> dict:
    login, domain = inbox_addr.split("@")
    
    async with httpx.AsyncClient(timeout=20) as c:
        for _ in range(timeout_s // 15): 
            await asyncio.sleep(15) 
            try:
                resp = await c.get(f"{MAILTM}?action=getMessages&login={login}&domain={domain}")
                
                if resp.status_code == 429:
                    log.warning("⚠️ 1SecMail bloqueou (429). Esperando 60 segundos...")
                    await asyncio.sleep(60)
                    continue
                
                if resp.status_code == 200:
                    msgs = resp.json()
                    if msgs: 
                        msg_id = msgs[0]["id"]
                        msg_resp = await c.get(f"{MAILTM}?action=readMessage&login={login}&domain={domain}&id={msg_id}")
                        if msg_resp.status_code == 200:
                            return msg_resp.json()
                else:
                    log.warning(f"Status inesperado do 1SecMail: {resp.status_code}")
                    
            except Exception as e:
                log.error(f"Erro ao checar e-mail (1SecMail): {e}")
                await asyncio.sleep(15)
                
    raise TimeoutError("sem email na janela")

def extract_m3u(body: str) -> str | None:
    m = re.search(r"https?://[^\s\"'<>]+/get\.php\?username=[^\s\"'<>]+&password=[^\s\"'<>]+&type=m3u[^\s\"'<>]*", body, re.I)
    if m: return m.group(0)
    m = re.search(r"https?://[^\s\"'<>]+\.m3u8?(?:\?[^\s\"'<>]*)?", body, re.I)
    return m.group(0) if m else None

async def submit_form(page, inbox_addr: str):
    first, last = rand_name()
    log.info("Acessando formulário testersgc...")
    await page.goto(FORM_URL, wait_until="networkidle")
    await page.wait_for_timeout(1500)

    for txt in ["Aceitar Todos", "Accept", "OK"]:
        try:
            await page.click(f'button:has-text("{txt}")', timeout=1500)
            await page.wait_for_timeout(500)
        except PWTimeout:
            pass

    try:
        await page.wait_for_selector('input[placeholder="Digite seu nome completo"]', timeout=60000)
    except PWTimeout:
        log.error("❌ O formulário não carregou a tempo.")
        await page.screenshot(path="erro_form.png")
        raise Exception("Formulário não encontrado")

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
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx_kwargs = dict(locale="pt-BR", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        if COOKIES.exists(): ctx_kwargs["storage_state"] = str(COOKIES)
        ctx = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()
        try:
            await iboplayer_login(page)
            await ctx.storage_state(path=str(COOKIES))
            
            log.info("Acessando pagina de edicao...")
            await page.goto(IBO_EDIT, wait_until="networkidle")
            await page.wait_for_timeout(2000)
            await _accept_modals(page)
            
            if "/edit/" not in page.url:
                log.info("Redirecionando novamente para a edicao...")
                await page.goto(IBO_EDIT, wait_until="networkidle")
                await page.wait_for_timeout(2000)
                
            await page.evaluate("() => { const modals = document.querySelectorAll('[class*=\"modal\"], [class*=\"terms\"]'); modals.forEach(m => m.remove()); }")
            await page.wait_for_timeout(1000)
            
            log.info("Esperando o campo da playlist (#playlist-url)...")
            await page.wait_for_selector("#playlist-url", timeout=60000, state="visible")
            
            url_antiga = await page.input_value("#playlist-url")
            log.info(f"URL antiga encontrada: {url_antiga}")
            
            if url_antiga == m3u_url:
                log.info("A URL ja e a mesma, nada a fazer.")
                return True
                
            log.info(f"Preenchendo nova URL: {m3u_url}")
            await page.fill("#playlist-url", m3u_url)
            await page.wait_for_timeout(300)
            
            log.info("Removendo pop-ups que possam bloquear o botão...")
            await page.evaluate("""() => { 
                const elements = document.querySelectorAll('div.fixed, div[class*="overlay"], div[class*="z-"]');
                for (let i = 0; i < elements.length; i++) { elements[i].remove(); }
            }""")
            await page.wait_for_timeout(1000)
            
            log.info("Clicando no botao SAVE via JS...")
            await page.evaluate("""() => { 
                const btn = Array.from(document.querySelectorAll('button')).find(b => b.innerText.trim().toUpperCase() === 'SAVE'); 
                if (btn) btn.click(); 
            }""")
            
            await page.wait_for_timeout(5000)
            await _accept_modals(page)
            
            if "/edit/" not in page.url:
                await page.goto(IBO_EDIT, wait_until="networkidle")
                await page.wait_for_timeout(2000)
                await page.wait_for_selector("#playlist-url", timeout=15000, state="visible")
                
            url_nova = await page.input_value("#playlist-url")
            log.info(f"URL nova confirmada: {url_nova}")
            
            return url_nova == m3u_url
            
        except Exception as e:
            log.error(f"ERRO NA ATUALIZACAO DA PLAYLIST: {e}")
            return False
        finally:
            await browser.close()

async def fetch_m3u() -> str | None:
    # 🚨 MUDANÇA: Agora recebe (endereço, login, domínio)
    inbox_addr, _, domain = await make_inbox() 
    log.info(f"inbox: {inbox_addr}")
    
    # Espera inicial para dar tempo do site enviar o e-mail
    log.info("⏳ Aguardando 30 segundos iniciais antes de checar a caixa de entrada...")
    await asyncio.sleep(30)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(locale="pt-BR", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        page = await ctx.new_page()
        try:
            await submit_form(page, inbox_addr)
        finally:
            await browser.close()
            
    # 🚨 MUDANÇA: Passa o endereço completo para a função de espera
    msg = await wait_for_email(inbox_addr, timeout_s=600)
    
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
            
            log.info("Iniciando atualizacao no ioplayer...")
            ibo_ok = await iboplayer_update(m3u)
            log.info(f"Resultado da atualizacao: {ibo_ok}")
            
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
