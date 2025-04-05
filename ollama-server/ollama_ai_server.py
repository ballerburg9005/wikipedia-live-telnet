#!/usr/bin/env python3
import asyncio
import json
import subprocess
import re
import requests
import os
import ssl
import websockets
from bs4 import BeautifulSoup
import configparser

# Global configuration will be loaded from server.cfg.
CONFIG_PATH = os.environ.get("SERVER_CONFIG_PATH", "server.cfg")
CONFIG = None

def load_config(path=CONFIG_PATH):
    config = configparser.ConfigParser()
    config.read(path)
    debug_str = config.get("ollama", "debug", fallback="true").lower()
    debug_mode = debug_str in ["true", "1"]
    port = config.getint("ollama", "port", fallback=50000)
    auth_token = config.get("ollama", "auth_token", fallback="AAAAB3NzaC1yc2EAAAADAQABAAABAQDBg")
    model = config.get("ollama", "model", fallback="smollm2:360m")
    ollama_uri = config.get("ollama", "ollama_uri", fallback="http://localhost:11434/api")
    return {
        "DEBUG": debug_mode,
        "PORT": port,
        "AUTH_TOKEN": auth_token,
        "MODEL_NAME": model,
        "OLLAMA_URI": ollama_uri
    }

def debug_print(*args, **kwargs):
    if CONFIG and CONFIG["DEBUG"]:
        print("[DEBUG]", *args, **kwargs)

def do_google_search(query: str) -> str:
    q = query.strip().replace(" ", "+")
    url = f"https://lite.duckduckgo.com/lite/?q={q}"
    debug_print("Executing command:", " ".join(["lynx", "--dump", "--display_charset=utf-8", url]))
    try:
        result = subprocess.run(
            ["lynx", "--dump", "--display_charset=utf-8", url],
            capture_output=True, text=True, check=True
        )
        debug_print("Search result received, length:", len(result.stdout))
        return result.stdout
    except Exception as e:
        debug_print("Search error:", e)
        return f"[Google Error] {e}"

def browse_and_extract(url: str, query: str) -> str:
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        keywords = query.lower().split()
        relevant_text = []
        for p in soup.find_all('p'):
            text = p.get_text().lower()
            if any(kw in text for kw in keywords):
                relevant_text.append(p.get_text().strip())
        if relevant_text:
            distilled = " ".join(relevant_text)[:200].strip() + "..."
            debug_print("Extracted and distilled from", url, ":", distilled)
            return distilled
        else:
            return "MULTIVAC: No relevant data found on this page."
    except Exception as e:
        debug_print("Browse error:", e)
        return f"[Browse Error] {e}"

def process_search_results(search_output: str, query: str) -> str:
    urls = re.findall(r'https?://[^\s]+', search_output)
    debug_print("Found URLs:", urls)
    relevant_urls = [url for url in urls[:2] if "duckduckgo" not in url.lower() and "login" not in url.lower()]
    if not relevant_urls:
        return "MULTIVAC: No suitable search results to browse."
    extracted = []
    for url in relevant_urls[:2]:
        result = browse_and_extract(url, query)
        extracted.append(f"From {url}: {result}")
    return "\n".join(extracted)

async def stream_ollama_response(prompt: str, model: str, tools: list = None):
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": {
            "num_predict": 256,
            "num_ctx": 1024,
            "max_tokens": 256,
            "temperature": 0.0,
            "top_p": 0.8,
            "top_k": 40,
            "repeat_penalty": 1.3,
        },
    }
    if tools:
        payload["tools"] = tools
    debug_print("Payload for Ollama:", json.dumps(payload, indent=2))
    curl_cmd = [
        "curl", "-sS", "-X", "POST",
        "http://localhost:11434/api/generate",
        "-H", "Content-Type: application/json",
        "-d", json.dumps(payload)
    ]
    debug_print("Running curl command:", " ".join(curl_cmd))
    process = await asyncio.create_subprocess_exec(
        *curl_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    if not process.stdout:
        debug_print("No STDOUT from process!")
        yield "[Error] No STDOUT from LLM"
        return
    try:
        async for line_bytes in process.stdout:
            line = line_bytes.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            debug_print("Received raw line from curl:", line)
            try:
                data = json.loads(line)
                debug_print("Parsed JSON chunk:", data)
            except json.JSONDecodeError as je:
                debug_print("JSON decode error for line:", line, "Error:", je)
                yield f"[Unparseable chunk] {line}"
                continue
            if "response" in data:
                token_chunk = data["response"]
                parts = re.findall(r'\S+|\s+', token_chunk)
                for part in parts:
                    debug_print("Yielding individual token:", repr(part))
                    yield part
                    await asyncio.sleep(0.05)
            if data.get("done"):
                debug_print("LLM indicated done.")
                break
        await process.wait()
        debug_print("Curl process finished with return code:", process.returncode)
    except asyncio.CancelledError:
        process.terminate()
        await process.wait()
        debug_print("Process cancelled.")
        raise

async def handle_ai_connection(websocket):
    try:
        msg = await websocket.recv()
        debug_print("Received WebSocket message:", msg)
        data = json.loads(msg)
        token = data.get("auth_token", "")
        if token != CONFIG["AUTH_TOKEN"]:
            await websocket.send("[Error] Invalid or missing auth token.")
            return
        user_id = data.get("user_id", "anonymous")
        conversation = data.get("conversation", [])
        context = data.get("context", "")
        page_index = data.get("page_index", 0)
        new_question = data.get("new_question", "")
        tools = [{
            "type": "function",
            "function": {
                "name": "do_web_search",
                "description": "Perform a web search for a given query and browse results.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query."
                        }
                    },
                    "required": ["query"]
                }
            }
        }]
        system_text = (
            "ONLY answer in English language. NEVER repeat the system message or the user. "
            "Tone: technical, professional, 80s corporate mainframe. Your name is MULTIVAC. "
            "Primary goal: Answer from provided context (e.g., Wikipedia article) if possible. "
            "If context lacks sufficient info or is absent, immediately trigger a web search with <search>query</search> "
            "and browse the first two relevant results to extract data. For real-time queries (e.g., weather), "
            "always use <search>query</search>. Answer concisely using only context or search results—NO speculation."
        )
        prompt_lines = [f"System: {system_text}"]
        if context:
            prompt_lines.append(f"Article Context (Page {page_index}):\n{context}")
        for msg_item in conversation:
            role = msg_item.get("speaker", "User")
            text = msg_item.get("text", "")
            prompt_lines.append(f"{role}: {text}")
        prompt_lines.append(f"User: {new_question}")
        prompt_lines.append("Assistant:")
        full_prompt = "\n".join(prompt_lines)
        debug_print("Full prompt constructed:\n", full_prompt)
        model_name = CONFIG["MODEL_NAME"]
        response_buffer = ""
        async for token_chunk in stream_ollama_response(full_prompt, model_name, tools):
            response_buffer += token_chunk
            await websocket.send(token_chunk)
        if (not response_buffer.strip()) or (len(response_buffer) < 20) or ("<search>" in response_buffer.lower()):
            debug_print("Response too weak or requests search:", response_buffer)
            if "<search>" in response_buffer.lower():
                match = re.search(r'<search>(.*?)</search>', response_buffer.lower())
                search_query = match.group(1) if match else new_question
            else:
                search_query = new_question
            search_result = do_google_search(search_query)
            distilled_info = process_search_results(search_result, new_question)
            await websocket.send(f"\nMULTIVAC: Insufficient data in context/model. Retrieved from web:\n{distilled_info}")
        else:
            debug_print("Response deemed sufficient:", response_buffer)
    except websockets.exceptions.ConnectionClosed:
        debug_print("Connection closed by client")
    except Exception as e:
        err_msg = f"[AI Error] {type(e).__name__}: {e}"
        debug_print("Exception in handle_ai_connection:", err_msg)
        await websocket.send(err_msg)

def create_self_signed_cert(certfile="server.crt", keyfile="server.key"):
    debug_print("Generating a self-signed certificate...")
    subj = "/C=US/ST=Denial/L=Springfield/O=Dis/CN=localhost"
    cmd = [
        "openssl", "req",
        "-x509", "-nodes", "-days", "365",
        "-newkey", "rsa:2048",
        "-subj", subj,
        "-keyout", keyfile,
        "-out", certfile
    ]
    subprocess.run(cmd, check=True)
    debug_print("Self-signed certificate generated:", certfile, keyfile)

async def main():
    global CONFIG
    CONFIG = load_config()
    debug_print("Loaded config for Ollama server:", CONFIG)
    # Start ollama in background if needed is now handled by entrypoint.sh.
    port = CONFIG["PORT"]
    debug_print(f"Starting AI WebSocket server on wss://0.0.0.0:{port}/ai")
    certfile = "server.crt"
    keyfile = "server.key"
    if not (os.path.exists(certfile) and os.path.exists(keyfile)):
        create_self_signed_cert(certfile, keyfile)
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile, keyfile)
    async with websockets.serve(handle_ai_connection, "0.0.0.0", port, ssl=ssl_context):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())

