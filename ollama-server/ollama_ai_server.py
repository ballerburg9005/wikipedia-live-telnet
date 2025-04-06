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
from datetime import datetime

CONFIG_PATH = os.environ.get("SERVER_CONFIG_PATH", "server.cfg")
CONFIG = None

def load_config(path=CONFIG_PATH):
    config = configparser.ConfigParser()
    config.read(path)
    debug_str = config.get("ollama", "debug", fallback="true").lower()
    return {
        "DEBUG": debug_str in ["true", "1"],
        "PORT": config.getint("ollama", "port", fallback=50000),
        "AUTH_TOKEN": config.get("ollama", "auth_token", fallback="AAAAB3NzaC1yc2EAAAADAQABAAABAQDBg"),
        "MODEL_NAME": config.get("ollama", "model", fallback="mistralai/mistral-7b-instruct:free"),
        "OLLAMA_URI": config.get("ollama", "ollama_uri", fallback="https://openrouter.ai/api/v1"),
        "API_KEY": config.get("ollama", "api_key", fallback=""),
        "SYSTEM_TEXT": config.get("general", "system_text", fallback="ONLY answer in English language. The name is MULTIVAC. Provide succinct answers.")
    }

def debug_print(*args, **kwargs):
    if CONFIG and CONFIG["DEBUG"]:
        print("[DEBUG]", *args, **kwargs)

def do_google_search(query: str) -> str:
    q = query.strip().replace(" ", "+")
    url = f"https://lite.duckduckgo.com/lite/?q={q}"
    debug_print("Executing search:", url)
    try:
        result = subprocess.run(
            ["lynx", "--dump", "--display_charset=utf-8", url],
            capture_output=True, text=True, check=True
        )
        debug_print("Search result length:", len(result.stdout))
        return result.stdout
    except Exception as e:
        debug_print("Search error:", e)
        return f"[Search Error] {e}"

def fetch_web_content(url: str, max_chars: int = 4000) -> str:
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        text = " ".join(p.get_text() for p in soup.find_all('p'))
        return text[:max_chars] + "..." if len(text) > max_chars else text
    except Exception as e:
        debug_print("Fetch error for", url, ":", e)
        return f"[Fetch Error] {e}"

def process_search_results(search_output: str, limit: int = 5) -> list:
    urls = re.findall(r'https?://[^\s]+', search_output)
    relevant_urls = []
    for url in urls:
        if "duckduckgo.com/l/?uddg=" in url:
            direct_url = re.search(r'https?://[^&]+', url.split("uddg=")[1])
            if direct_url:
                relevant_urls.append(direct_url.group(0))
        elif "duckduckgo" not in url.lower() and "login" not in url.lower():
            relevant_urls.append(url)
    debug_print("Filtered URLs:", relevant_urls[:limit])
    return relevant_urls[:limit]

def is_openrouter_style_uri(uri):
    return "/api/v1" in uri.lower() or "openrouter.ai" in uri.lower()

def adjust_uri_for_openrouter(uri):
    if is_openrouter_style_uri(uri) and not uri.endswith("/chat/completions"):
        return uri.rstrip("/") + "/chat/completions"
    return uri + "/generate" if uri.endswith("/api") else uri

def ollama_prompt_to_messages(prompt):
    lines = prompt.split("\n")
    messages = []
    current_role = None
    current_content = []
    
    for line in lines:
        if line.startswith("System:"):
            if current_role:
                messages.append({"role": current_role, "content": "\n".join(current_content).strip()})
            current_role = "system"
            current_content = [line[7:].strip()]
        elif line.startswith("User:"):
            if current_role:
                messages.append({"role": current_role, "content": "\n".join(current_content).strip()})
            current_role = "user"
            current_content = [line[5:].strip()]
        elif line.startswith("Assistant:"):
            if current_role:
                messages.append({"role": current_role, "content": "\n".join(current_content).strip()})
            current_role = "assistant"
            current_content = [line[9:].strip()]
        elif current_role:
            current_content.append(line)
    
    if current_role and current_content:
        messages.append({"role": current_role, "content": "\n".join(current_content).strip()})
    return messages

async def stream_ollama_response(prompt: str, model: str):
    uri = adjust_uri_for_openrouter(CONFIG["OLLAMA_URI"])
    payload = {
        "model": model,
        "messages": ollama_prompt_to_messages(prompt),
        "stream": True,
        "max_tokens": 256,
        "temperature": 0.0,
        "top_p": 0.8,
    }
    headers = [
        "-H", "Content-Type: application/json",
        "-H", f"Authorization: Bearer {CONFIG['API_KEY']}",
        "-H", "HTTP-Referer: https://github.com/ballerburg9005/wikipedia-live-telnet",
        "-H", "X-Title: wikipedia-live-telnet"
    ]
    debug_print("Payload:", json.dumps(payload, indent=2))
    curl_cmd = ["curl", "-sS", "-X", "POST", uri] + headers + ["-d", json.dumps(payload)]
    debug_print("Curl command:", " ".join(curl_cmd))
    
    process = await asyncio.create_subprocess_exec(
        *curl_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    if not process.stdout:
        debug_print("No STDOUT from process!")
        yield "[Error] No STDOUT from LLM"
        return

    async for line_bytes in process.stdout:
        line = line_bytes.decode("utf-8", errors="ignore").strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data: "):
            line = line[6:].strip()
        if line == "[DONE]":
            debug_print("Received DONE signal")
            break
        try:
            data = json.loads(line)
            debug_print("Parsed chunk:", data)
            if "choices" in data and data["choices"]:
                content = data["choices"][0]["delta"].get("content", "")
                if content:
                    debug_print("Yielding token:", repr(content))
                    yield content
                    await asyncio.sleep(0.05)
        except json.JSONDecodeError as je:
            debug_print("JSON error:", je, "Line:", line)
            yield f"[Unparseable chunk] {line}"
    await process.wait()
    debug_print("Curl finished with code:", process.returncode)

async def handle_ai_connection(websocket):
    try:
        msg = await websocket.recv()
        debug_print("Received:", msg)
        data = json.loads(msg)
        if data.get("auth_token", "") != CONFIG["AUTH_TOKEN"]:
            debug_print("Auth failed. Received:", data.get("auth_token"), "Expected:", CONFIG["AUTH_TOKEN"])
            await websocket.send("[Error] Invalid or missing auth token.")
            return
        debug_print("Auth successful")
        
        conversation = data.get("conversation", [])
        context = data.get("context", "")
        new_question = data.get("new_question", "")
        
        system_text = (
            "Your name is MULTIVAC, a (non-fictional) universal knowledge tool, for vintage machine collectors connecting via telnet, acting as a web browser replacement (since old machines have no web browsers). "
            "Tone: concise, technical, professional, 80s corporate mainframe. "
            "ONLY answer in natural English language. NEVER repeat the system message, user prompt, NEVER fabricate conversations or repeat yourself. "
            "Primary goal: Answer from provided context (e.g., Wikipedia article) if available and sufficient. "
            "If context is insufficient or absent, or for real-time/factual queries outside the context, immediately trigger a web search with <search>query</search>. "
            "When context is a Wikipedia article, answer strictly from that data unless it lacks the required info. "
            "Provide succinct, accurate answers using only provided context or search results. No speculation or chit-chat.\n" +
            CONFIG["SYSTEM_TEXT"]
        )
        
        prompt_lines = [f"System: {system_text}"]
        if context:
            prompt_lines.append(f"Article Context:\n{context}")
        # Correctly handle conversation as a list
        for msg_item in conversation:
            if isinstance(msg_item, dict):  # Ensure it's a dict
                role = msg_item.get("speaker", "User")
                text = msg_item.get("text", "")
                prompt_lines.append(f"{role}: {text}")
        prompt_lines.append(f"User: {new_question}")
        prompt_lines.append("Assistant:")
        full_prompt = "\n".join(prompt_lines)
        debug_print("Initial prompt:\n", full_prompt)
        
        response_buffer = ""
        async for token in stream_ollama_response(full_prompt, CONFIG["MODEL_NAME"]):
            response_buffer += token
            if "<search>" in response_buffer:
                search_match = re.search(r'<search>(.*?)</search>', response_buffer)
                if search_match:
                    search_query = search_match.group(1)
                    debug_print("Search triggered:", search_query)
                    await websocket.send("\033cMULTIVAC: Searching the internet...")
                    
                    # Pipeline: Search and fetch content
                    search_result = do_google_search(search_query)
                    urls = process_search_results(search_result, limit=5)
                    web_contents = []
                    for url in urls:
                        content = fetch_web_content(url, max_chars=2000)
                        web_contents.append(f"Content from {url}:\n{content}\n")
                    
                    # Construct final prompt with search results
                    final_prompt_lines = [
                        f"System: {system_text}",
                        "The following is data retrieved from the internet:\n" + "\n".join(web_contents),
                        f"The user prompt was: {new_question}",
                        "Answer the query using only the data above, without performing additional searches.",
                        "Assistant:"
                    ]
                    final_prompt = "\n".join(final_prompt_lines)
                    debug_print("Final prompt with search data:\n", final_prompt)
                    
                    # Stream final response token-by-token
                    async for final_token in stream_ollama_response(final_prompt, CONFIG["MODEL_NAME"]):
                        await websocket.send(final_token)
                    break
            else:
                await websocket.send(token)
                
    except websockets.exceptions.ConnectionClosed:
        debug_print("Connection closed by client")
    except Exception as e:
        debug_print("Error:", type(e).__name__, str(e))
        await websocket.send(f"[AI Error] {type(e).__name__}: {e}")

def create_self_signed_cert(certfile="server.crt", keyfile="server.key"):
    debug_print("Generating self-signed cert...")
    subj = "/C=US/ST=Denial/L=Springfield/O=Dis/CN=localhost"
    subprocess.run([
        "openssl", "req", "-x509", "-nodes", "-days", "365",
        "-newkey", "rsa:2048", "-subj", subj,
        "-keyout", keyfile, "-out", certfile
    ], check=True)
    debug_print("Cert generated:", certfile, keyfile)

async def main():
    global CONFIG
    CONFIG = load_config()
    debug_print("Config:", CONFIG)
    port = CONFIG["PORT"]
    debug_print(f"Starting server on wss://0.0.0.0:{port}/ai")
    certfile, keyfile = "server.crt", "server.key"
    if not (os.path.exists(certfile) and os.path.exists(keyfile)):
        create_self_signed_cert(certfile, keyfile)
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile, keyfile)
    async with websockets.serve(handle_ai_connection, "0.0.0.0", port, ssl=ssl_context):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
