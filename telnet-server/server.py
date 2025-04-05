import asyncio
import re
import sys
import telnetlib3
import wikipedia
import textwrap
import os
import json
import uuid
import ssl
import websockets
import configparser

# ------------- REVISED CODE STARTS HERE ----------------

CONFIG_PATH = os.environ.get("SERVER_CONFIG_PATH", "server.cfg")

def load_config(path=CONFIG_PATH):
    """
    Load config values from an .ini file. If missing or incomplete,
    use safe defaults.
    """
    config = configparser.ConfigParser()
    config.read(path)

    # [general]
    debug_str = config.get("general", "debug", fallback="false").lower()
    debug_mode = (debug_str == "true" or debug_str == "1")

    port = config.getint("general", "port", fallback=8023)
    default_language = config.get("general", "default_language", fallback="en")
    ai_websocket_uri = config.get("general", "ai_websocket_uri", fallback="wss://127.0.0.1:50000/ai")

    # The welcome message (replace \n => \r\n at runtime)
    raw_welcome = config.get("general", "welcome_message", fallback="=== Wikipedia Telnet Gateway ===")
    welcome_msg = raw_welcome.replace("\n", "\r\n")

    # AI activation
    ai_activated_str = config.get("general", "ai_activated", fallback="true").lower()
    ai_activated = (ai_activated_str == "true" or ai_activated_str == "1")

    # [ollama]
    model = config.get("ollama", "model", fallback="smollm2:360m")

    return {
        "DEBUG": debug_mode,
        "PORT": port,
        "LANG": default_language,
        "AI_URI": ai_websocket_uri,
        "WELCOME_MSG": welcome_msg,
        "MODEL": model,
        "AI_ACTIVATED": ai_activated
    }

def telnet_debug_print(conf, *args, **kwargs):
    """
    Print debug info if conf["DEBUG"] is True.
    """
    if conf["DEBUG"]:
        print("[DEBUG]", *args, **kwargs)

# We'll store these config values globally after loading in main()
CONF = None

def get_welcome_logo():
    return CONF["WELCOME_MSG"]

def extract_toc_and_lines(content):
    lines = content.splitlines()
    toc = []
    for idx, line in enumerate(lines):
        m = re.match(r'^(={2,})([^=].*?)(={2,})\s*$', line.strip())
        if m:
            toc.append((m.group(2).strip(), idx))
    return toc, lines

def remove_wiki_markup(text):
    """
    Remove basic [[wikilink]] markup from raw text,
    e.g. [[Some Title]] => Some Title
         [[Title|Displayed]] => Displayed
    """
    return re.sub(
        r'\[\[([^|\]]+)(?:\|([^\]]+))?\]\]',
        lambda m: m.group(2) if m.group(2) else m.group(1),
        text
    )

def linkify_preserving_case(content, links):
    """
    Replaces each link in the text with [OriginalCase] ignoring case,
    but preserving the exact substring found in `content`.
    Then we insert placeholders which we re-inject later.
    """
    valid_links = [l for l in links if len(l) > 1]
    if not valid_links:
        return content, {}

    pattern = r'(' + r'|'.join(re.escape(lnk) for lnk in valid_links for lnk in [lnk]) + r')'
    placeholders = {}
    idx = 0

    def replacement(m):
        nonlocal idx
        matched = m.group(0)  # exact substring in content
        placeholder = f"{{PLCH{idx}}}"
        placeholders[placeholder] = f"[{matched}]"
        idx += 1
        return placeholder

    # ignore case but keep the matched substring
    new_content = re.sub(pattern, replacement, content, flags=re.IGNORECASE)
    return new_content, placeholders

def final_wrap_after_injection(lines, line_width):
    """
    Because the re-injected bracket text can lengthen lines,
    we do a second pass to ensure they never exceed line_width.
    """
    final_text = "\n".join(lines)
    wrapped2 = []
    paras = final_text.split("\n\n")
    for p in paras:
        p = p.strip()
        if p:
            wrapped_seg = textwrap.fill(p, width=line_width).splitlines()
            wrapped2.extend(wrapped_seg)
            wrapped2.append("")
    if wrapped2 and wrapped2[-1] == "":
        wrapped2.pop()
    return wrapped2

def wrap_content(content, line_width, links):
    """
    1) Convert link occurrences to placeholders (preserving case).
    2) remove leftover [\d+] references
    3) textwrap
    4) re-inject placeholders with bracket text
    5) do a second textwrap pass to ensure lines never exceed line_width
    """
    # Step 1: placeholders
    content, placeholders = linkify_preserving_case(content, links)
    # Step 2: remove e.g. [1], [2], etc.
    content = re.sub(r'\[\d+\]', '', content)

    # Step 3: initial wrap
    paras = content.split("\n\n") if "\n\n" in content else content.split("\n")
    lines = []
    for para in paras:
        para = para.strip()
        if para:
            wrapped = textwrap.fill(para, width=line_width).splitlines()
            lines.extend(wrapped)
            lines.append("")
    if lines and lines[-1] == "":
        lines.pop()

    # Step 4: re-inject bracket text
    for i in range(len(lines)):
        for placeholder, bracket_text in placeholders.items():
            lines[i] = lines[i].replace(placeholder, bracket_text)

    # Step 5: final re-wrap
    lines = final_wrap_after_injection(lines, line_width)
    return lines

async def read_line_custom(writer, reader):
    buffer = []
    while True:
        ch = await reader.read(1)
        if not ch:
            return "".join(buffer)
        if ch in ("\r", "\n"):
            writer.write("\r\n")
            await writer.drain()
            return "".join(buffer)
        elif ch in ("\x08", "\x7f"):
            if buffer:
                buffer.pop()
                writer.write("\b \b")
                await writer.drain()
        elif ch == "\x1b":
            _ = await reader.read(2)
        else:
            buffer.append(ch)
            writer.write(ch)
            await writer.drain()

def cursor_up(n=1):
    return f"\033[{n}A"

def cursor_down(n=1):
    return f"\033[{n}B"

def cursor_carriage_return():
    return "\r"

def clear_line():
    return "\033[K"

TOC_GO_TO_ARTICLE_START = -999
SPINNER_CHARS = ["|", "/", "-", "\\"]
SPIN_INTERVAL = 0.25

async def stream_ai_with_spinner_and_interrupts(
    conf,
    question, article_context, article_page,
    user_id, conversation_history, writer, reader, max_width=80
):
    """
    Connect to the AI server over websockets using conf["AI_URI"].
    """
    payload = {
        "user_id": user_id,
        "conversation": conversation_history,
        "context": article_context,
        "page_index": article_page,
        "new_question": question,
        "auth_token": "AAAAB3NzaC1yc2EAAAADAQABAAABAQDBg"  # example placeholder
    }

    partial_tokens = []
    current_line = ""
    stop_flag = False
    user_canceled = False
    user_cleared = False
    last_token_time = asyncio.get_event_loop().time()
    spinner_index = 0

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    uri = conf["AI_URI"]

    async def read_websocket():
        nonlocal stop_flag, last_token_time, current_line
        try:
            async with websockets.connect(uri, ping_interval=None, ssl=ssl_context) as ws:
                await ws.send(json.dumps(payload))
                writer.write("MULTIVAC> ")
                await writer.drain()
                while not stop_flag:
                    try:
                        chunk = await asyncio.wait_for(ws.recv(), timeout=SPIN_INTERVAL)
                        tokens = re.findall(r'\S+|\s+', chunk)
                        for token in tokens:
                            if stop_flag:
                                break
                            token_text = telnet_fix_newlines(token)
                            if "\n" in token_text:
                                parts = token_text.split("\n")
                                for i, part in enumerate(parts):
                                    if stop_flag:
                                        break
                                    if len(current_line) + len(part) > max_width:
                                        writer.write("\r\n")
                                        await writer.drain()
                                        current_line = ""
                                    current_line += part
                                    writer.write(part)
                                    await writer.drain()
                                    if i < len(parts) - 1:
                                        writer.write("\r\n")
                                        await writer.drain()
                                        current_line = ""
                                partial_tokens.append(token_text)
                            else:
                                if len(current_line) + len(token_text) > max_width:
                                    writer.write("\r\n")
                                    await writer.drain()
                                    current_line = ""
                                current_line += token_text
                                writer.write(token_text)
                                await writer.drain()
                                partial_tokens.append(token_text)
                        last_token_time = asyncio.get_event_loop().time()
                    except asyncio.TimeoutError:
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        break
        except Exception as e:
            telnet_debug_print(conf, "WebSocket AI error:", e)
        finally:
            writer.write("\r\n")
            await writer.drain()
            stop_flag = True

    async def read_keystrokes():
        nonlocal stop_flag, user_canceled, user_cleared
        while not stop_flag:
            ckey = await reader.read(1)
            if not ckey:
                continue
            if ckey.lower() == 'q':
                user_canceled = True
                stop_flag = True
            elif ckey.lower() == 'c':
                user_cleared = True
                user_canceled = True
                stop_flag = True

    async def spinner_task():
        nonlocal spinner_index, stop_flag
        while not stop_flag:
            now = asyncio.get_event_loop().time()
            if now - last_token_time >= SPIN_INTERVAL:
                spin_char = SPINNER_CHARS[spinner_index % len(SPINNER_CHARS)]
                spinner_index += 1
                try:
                    writer.write(f"{spin_char}\b")
                    await writer.drain()
                except:
                    stop_flag = True
                    break
            await asyncio.sleep(SPIN_INTERVAL)

    t_ws = asyncio.create_task(read_websocket())
    t_keys = asyncio.create_task(read_keystrokes())
    t_spin = asyncio.create_task(spinner_task())
    await asyncio.wait([t_ws, t_keys, t_spin], return_when=asyncio.FIRST_COMPLETED)
    stop_flag = True
    await asyncio.wait([t_ws, t_keys, t_spin], return_when=asyncio.ALL_COMPLETED)

    writer.write("\r\n")
    await writer.drain()

    final_text = "".join(partial_tokens)
    if user_cleared:
        final_text = ""
    return final_text, user_canceled

def wrap_block_of_text(lines_list, width):
    out = []
    for line in lines_list:
        if not line.strip():
            out.append("")
            continue
        wrapped = textwrap.fill(line, width=width).splitlines()
        out.extend(wrapped)
    while out and not out[-1].strip():
        out.pop()
    return out

async def paginate(lines, writer, reader, page_size):
    total_lines = len(lines)
    if total_lines == 0:
        writer.write("No content.\r\n")
        await writer.drain()
        return
    total_pages = (total_lines + page_size - 1) // page_size
    page_index = total_pages - 1

    while True:
        writer.write("\033[2J\033[H")
        start = page_index * page_size
        end = start + page_size
        for line in lines[start:end]:
            writer.write(line + "\r\n")

        if page_index < total_pages - 1:
            writer.write(f"\r\n-- Page {page_index+1}/{total_pages} -- (Enter/l=next, h=prev, q=exit): ")
        else:
            writer.write(f"\r\n-- Page {page_index+1}/{total_pages} -- (Enter/l/q=exit, h=prev): ")

        await writer.drain()
        key = await reader.read(1)
        if not key:
            return
        if key == "\x1b":
            seq = await reader.read(2)
            if seq == "[C":
                key = "l"
            elif seq == "[D":
                key = "h"
            elif seq == "[A":
                key = "k"
            elif seq == "[B":
                key = "j"
            else:
                key = "??"

        if key in ("\r", "\n", "l"):
            if page_index < total_pages - 1:
                page_index += 1
            else:
                return
        elif key == "h":
            if page_index > 0:
                page_index -= 1
        elif key.lower() == 'q':
            return
        elif key in ("j", "k"):
            pass

async def show_ai_conversation_overlay(
    conf,
    writer, reader,
    article_text, article_page,
    user_id, line_width, page_size,
    is_top_level=False,
    initial_question=None
):
    conversation = []

    while True:
        lines_for_pagination = []
        for msg in conversation:
            if msg["speaker"] == "You":
                lines_for_pagination.append(f"You> {msg['text']}")
            elif msg["speaker"] == "AI":
                lines_for_pagination.append(f"MULTIVAC> {msg['text']}")
            elif msg["speaker"] == "Error":
                lines_for_pagination.append(f"[ERROR: {msg['text']}]")
            else:
                lines_for_pagination.append(f"{msg['speaker']}> {msg['text']}")
            lines_for_pagination.append("")

        wrapped_for_pagination = wrap_block_of_text(lines_for_pagination, line_width)
        await paginate(wrapped_for_pagination, writer, reader, page_size)

        writer.write("\033[2J\033[H")
        if is_top_level:
            writer.write("=== AI Assistant Shell Mode ===\r\n(Type your question, q=exit)\r\n\r\n")
        else:
            writer.write("=== AI Assistant Overlay ===\r\n(Type your question, q=exit)\r\n\r\n")
        await writer.drain()

        if initial_question:
            question = initial_question.strip()
            initial_question = None
            writer.write(f"You> {question}\r\n")
            await writer.drain()
        else:
            writer.write("You> ")
            await writer.drain()
            question = await read_line_custom(writer, reader)
            question = question.strip()

        if question.lower() == 'q':
            writer.write("[Exiting AI assistant overlay]\r\n")
            await writer.drain()
            return
        if not question:
            if not is_top_level:
                return
            continue

        conversation.append({"speaker": "You", "text": question})

        try:
            final_text, canceled = await stream_ai_with_spinner_and_interrupts(
                conf,
                question, article_text, article_page,
                user_id, conversation,
                writer, reader, line_width
            )
            if canceled:
                if final_text == "":
                    conversation.append({"speaker": "AI", "text": "[User cleared partial response]"})
                else:
                    conversation.append({"speaker": "AI", "text": final_text + " [User canceled]"})
            else:
                conversation.append({"speaker": "AI", "text": final_text})
        except Exception as e:
            conversation.append({"speaker": "Error", "text": f"AI assistant connection error: {e}\nSorry MULTIVAC is offline"})

async def select_option(options, writer, reader, page_size, prompt, previous_page=None):
    selected = 0
    digit_buffer = ""
    is_toc_prompt = ("chapter" in prompt.lower())
    if is_toc_prompt:
        display_options = ["[Start]"] + options
    else:
        display_options = options
    total = len(display_options)
    total_pages = (total + page_size - 1) // page_size

    def current_page(sel):
        return sel // page_size

    def print_full_page(page_idx, sel_idx, digits):
        writer.write("\033[2J\033[H")
        start = page_idx * page_size
        end = min(start + page_size, total)
        for i in range(start, end):
            arrow = "-> " if i == sel_idx else "   "
            writer.write(f"{i}. {arrow}{display_options[i]}\r\n")
        writer.write(f"\r\n-- Page {page_idx+1}/{total_pages} -- {prompt} {digits}\r\n")

    async def update_selection_inplace(old_sel, new_sel):
        page_start = cur_page * page_size
        page_end = min(page_start + page_size, total)
        lines_in_page = page_end - page_start
        old_offset = old_sel - page_start
        new_offset = new_sel - page_start
        lines_to_move_up = (lines_in_page + 2 - old_offset)
        writer.write(cursor_up(lines_to_move_up) + cursor_carriage_return() + clear_line())
        writer.write(f"{old_sel}.    {display_options[old_sel]}")
        diff = old_offset - new_offset
        if diff > 0:
            writer.write(cursor_up(diff))
        elif diff < 0:
            writer.write(cursor_down(-diff))
        writer.write(cursor_carriage_return() + clear_line())
        writer.write(f"{new_sel}. -> {display_options[new_sel]}")
        lines_back_down = (lines_in_page + 2 - new_offset)
        writer.write(cursor_down(lines_back_down) + cursor_carriage_return())
        await writer.drain()

    cur_page = current_page(selected)
    print_full_page(cur_page, selected, digit_buffer)
    await writer.drain()

    while True:
        key = await reader.read(1)
        if not key:
            return None
        if key.isdigit():
            digit_buffer += key
            writer.write(cursor_up(1) + cursor_carriage_return() + clear_line())
            writer.write(f"-- Page {cur_page+1}/{total_pages} -- {prompt} {digit_buffer}\r\n")
            await writer.drain()
            continue

        if key in ("\r", "\n"):
            if digit_buffer:
                try:
                    choice = int(digit_buffer)
                    if 0 <= choice <= total - 1:
                        if is_toc_prompt and choice == 0:
                            return TOC_GO_TO_ARTICLE_START
                        elif is_toc_prompt:
                            return choice - 1
                        return choice
                except:
                    pass
                return selected if not is_toc_prompt or selected > 0 else TOC_GO_TO_ARTICLE_START
            if is_toc_prompt and selected == 0:
                return TOC_GO_TO_ARTICLE_START
            elif is_toc_prompt:
                return selected - 1
            return selected

        if key.lower() == 'q':
            writer.write("\033[2J\033[H")
            writer.write("Ambiguous selection cancelled. Please be more specific.\r\n")
            await writer.drain()
            return None
        if key.lower() == 't' and is_toc_prompt:
            return previous_page if previous_page is not None else 0

        if key == "\x1b":
            seq = await reader.read(2)
            if seq == "[A":
                key = "k"
            elif seq == "[B":
                key = "j"
            elif seq == "[C":
                key = "l"
            elif seq == "[D":
                key = "h"
            else:
                key = "??"

        if key == "k":
            if selected > 0:
                old_sel = selected
                selected -= 1
                new_page = current_page(selected)
                if new_page != cur_page:
                    cur_page = new_page
                    print_full_page(cur_page, selected, digit_buffer)
                    await writer.drain()
                else:
                    await update_selection_inplace(old_sel, selected)
        elif key == "j":
            if selected < total - 1:
                old_sel = selected
                selected += 1
                new_page = current_page(selected)
                if new_page != cur_page:
                    cur_page = new_page
                    print_full_page(cur_page, selected, digit_buffer)
                    await writer.drain()
                else:
                    await update_selection_inplace(old_sel, selected)
        elif key == "l":
            if cur_page < total_pages - 1:
                cur_page += 1
                selected = cur_page * page_size
                if selected >= total:
                    selected = total - 1
                print_full_page(cur_page, selected, digit_buffer)
                await writer.drain()
            elif is_toc_prompt:
                return TOC_GO_TO_ARTICLE_START
        elif key == "h":
            if cur_page > 0:
                cur_page -= 1
                selected = cur_page * page_size
                print_full_page(cur_page, selected, digit_buffer)
                await writer.drain()

class ArticleSearchState:
    def __init__(self):
        self.term = None
        self.matches = []
        self.match_index = 0

def find_all_matches_in_wrapped(wrapped_lines, term):
    matches = []
    regex = re.compile(re.escape(term), re.IGNORECASE)
    for i, line in enumerate(wrapped_lines):
        for m in regex.finditer(line):
            matches.append((i, m.start(), m.end()))
    return matches

def highlight_line(line, term):
    regex = re.compile(re.escape(term), re.IGNORECASE)
    def replacer(m):
        return f"█{m.group(0)}█"
    return regex.sub(replacer, line)

def highlight_page_lines(wrapped_lines, page_start, page_end, search_term):
    out = []
    for line in wrapped_lines[page_start:page_end]:
        out.append(highlight_line(line, search_term))
    return out

async def do_article_search(writer, reader, article_search_state, wrapped_lines):
    writer.write("\r\n=== Internal Article Search ===\r\nSearch for: ")
    await writer.drain()
    srch = await read_line_custom(writer, reader)
    srch = srch.strip()
    if not srch:
        writer.write("No search term given.\r\n")
        await writer.drain()
        return
    article_search_state.term = srch
    article_search_state.matches = find_all_matches_in_wrapped(wrapped_lines, srch)
    article_search_state.match_index = 0
    if not article_search_state.matches:
        writer.write("No matches found.\r\n")
        await writer.drain()
        article_search_state.term = None
    else:
        writer.write(f"Found {len(article_search_state.matches)} matches.\r\n")
        await writer.drain()

async def jump_to_next_match(article_search_state, total_pages, page_size, current_page):
    if not article_search_state.term or not article_search_state.matches:
        return None
    total_matches = len(article_search_state.matches)
    for _ in range(total_matches):
        article_search_state.match_index = (article_search_state.match_index + 1) % total_matches
        line_idx, _, _ = article_search_state.matches[article_search_state.match_index]
        page_idx = line_idx // page_size
        if page_idx > current_page:
            return page_idx
    line_idx, _, _ = article_search_state.matches[0]
    return line_idx // page_size

async def loading_dots(writer):
    dots = ""
    try:
        while True:
            dots += "."
            writer.write(f"\rLoading{dots}\r")
            await writer.drain()
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        pass

async def paginate_article(
    conf,
    wrapped_lines, writer, reader,
    page_size, line_width,
    raw_lines, toc,
    initial_page=0,
    page_title=None
):
    user_id = str(uuid.uuid4())
    total_lines = len(wrapped_lines)
    if total_lines == 0:
        writer.write("Article is empty.\r\n")
        await writer.drain()
        return
    total_pages = (total_lines + page_size - 1) // page_size
    page_index = initial_page

    search_state = ArticleSearchState()
    need_reprint = True
    keep_going = True

    page = None
    if page_title:
        wikipedia.set_lang(conf["LANG"])
        try:
            page = wikipedia.page(title=page_title, auto_suggest=False)
        except:
            pass

    links = [l for l in (page.links if page else []) if len(l) > 1]
    link_positions = []

    def find_links_in_lines():
        nonlocal link_positions
        link_positions.clear()
        full_text = "\n".join(wrapped_lines)
        for link in links:
            rgx = re.compile(r'\[' + re.escape(link) + r'\]', re.IGNORECASE)
            for m in rgx.finditer(full_text):
                start_line = full_text[:m.start()].count("\n")
                line_start = full_text.rfind("\n", 0, m.start()) + 1 if start_line > 0 else 0
                link_positions.append((start_line,
                                       m.start() - line_start,
                                       m.end() - line_start,
                                       link))
        link_positions.sort()

    find_links_in_lines()

    def get_page_links(page_idx):
        start = page_idx * page_size
        end = min(start + page_size, total_lines)
        return [lp for lp in link_positions if start <= lp[0] < end]

    def highlight_lines_with_links(page_lines, page_idx, sel_link_idx):
        page_links = get_page_links(page_idx)
        out_lines = page_lines[:]
        for i, (line_idx, start, end, link) in enumerate(page_links):
            local_idx = line_idx - (page_idx * page_size)
            if 0 <= local_idx < len(out_lines):
                line = out_lines[local_idx]
                if i == sel_link_idx:
                    out_lines[local_idx] = line[:start] + "<" + line[start+1:end-1] + ">" + line[end:]
                else:
                    out_lines[local_idx] = line
        if search_state.term:
            for i, line in enumerate(out_lines):
                out_lines[i] = highlight_line(line, search_state.term)
        return out_lines

    async def update_link_selection(old_idx, new_idx, page_idx):
        page_links = get_page_links(page_idx)
        total_lines_on_page = min(page_size, total_lines - page_idx * page_size)
        if old_idx is not None and 0 <= old_idx < len(page_links):
            old_pos = page_links[old_idx]
            old_line = wrapped_lines[old_pos[0]]
            writer.write(cursor_up(total_lines_on_page + 1 - (old_pos[0] - page_idx * page_size)) + "\r")
            writer.write(old_line)
            writer.write(cursor_down(total_lines_on_page + 1 - (old_pos[0] - page_idx * page_size)) + "\r")
        if new_idx is not None and 0 <= new_idx < len(page_links):
            new_pos = page_links[new_idx]
            new_line = wrapped_lines[new_pos[0]]
            writer.write(cursor_up(total_lines_on_page + 1 - (new_pos[0] - page_idx * page_size)) + "\r")
            line_before = new_line[:(new_pos[1])]
            line_mid = new_line[new_pos[1]:(new_pos[2])]
            line_after = new_line[new_pos[2]:]
            line_mid = "<" + line_mid[1:-1] + ">"
            writer.write(line_before + line_mid + line_after)
            writer.write(cursor_down(total_lines_on_page + 1 - (new_pos[0] - page_idx * page_size)) + "\r")
        await writer.drain()

    selected_link = None

    while keep_going:
        if need_reprint:
            writer.write("\033[2J\033[H")
            start = page_index * page_size
            end = min(start + page_size, total_lines)
            page_lines = wrapped_lines[start:end]
            page_lines = highlight_lines_with_links(page_lines, page_index, selected_link)

            for line in page_lines:
                writer.write(line + "\r\n")

            # If AI is not activated, omit 'a=AI' from the prompt
            if conf["AI_ACTIVATED"]:
                writer.write(
                    f"\r\n-- Page {page_index+1}/{total_pages} -- "
                    f"(l=next, h=prev, t=TOC, q=exit, j/k=links, s/d=search, a=AI): "
                )
            else:
                writer.write(
                    f"\r\n-- Page {page_index+1}/{total_pages} -- "
                    f"(l=next, h=prev, t=TOC, q=exit, j/k=links, s/d=search): "
                )
            await writer.drain()
            need_reprint = False

        key = await reader.read(1)
        if not key:
            return

        if key == "\x1b":
            seq = await reader.read(2)
            if seq == "[A":
                key = "k"
            elif seq == "[B":
                key = "j"
            elif seq == "[C":
                key = "l"
            elif seq == "[D":
                key = "h"
            else:
                key = "??"

        page_links = get_page_links(page_index)

        if key in ("\r", "\n"):
            if selected_link is not None and page:
                # Open the link
                link_title = page_links[selected_link][3]
                writer.write("\033[2J\033[HLoading\r")
                await writer.drain()
                loading_task = asyncio.create_task(loading_dots(writer))
    
                try:
                    new_page = await asyncio.to_thread(wikipedia.page, title=link_title, auto_suggest=False)
                except:
                    loading_task.cancel()
                    try:
                        await loading_task
                    except asyncio.CancelledError:
                        pass
                    writer.write("\r" + clear_line() + "Failed to load link.\r\n")
                    await writer.drain()
                    need_reprint = True
                    continue

                new_content = remove_wiki_markup(new_page.content)
                new_content = re.sub(r'\n\s+', '\n', new_content)
                sub_links = [l for l in new_page.links if len(l) > 1]
                new_wrapped = wrap_content(new_content, line_width, sub_links)

                new_toc, new_raw = extract_toc_and_lines(new_content)

                loading_task.cancel()
                try:
                    await loading_task
                except asyncio.CancelledError:
                    pass
                writer.write("\r" + clear_line())
                await writer.drain()

                await paginate_article(
                    conf,
                    new_wrapped, writer, reader,
                    page_size, line_width,
                    new_raw, new_toc,
                    page_title=link_title
                )

                need_reprint = True

            elif page_index < total_pages - 1:
                page_index += 1
                selected_link = None
                need_reprint = True
            else:
                keep_going = False

        elif key == "l":
            if page_index < total_pages - 1:
                page_index += 1
                selected_link = None
                need_reprint = True
            else:
                keep_going = False

        elif key == "h":
            if page_index > 0:
                page_index -= 1
                selected_link = None
                need_reprint = True

        elif key == "j":
            if page_links:
                old_sel = selected_link
                if selected_link is None:
                    selected_link = 0
                elif selected_link < len(page_links) - 1:
                    selected_link += 1
                else:
                    selected_link = None
                await update_link_selection(old_sel, selected_link, page_index)

        elif key == "k":
            if page_links:
                old_sel = selected_link
                if selected_link is None:
                    selected_link = len(page_links) - 1
                elif selected_link > 0:
                    selected_link -= 1
                await update_link_selection(old_sel, selected_link, page_index)

        elif key == "t":
            if toc:
                toc_opts = [header for header, _ in toc]
                sel = await select_option(
                    toc_opts, writer, reader, page_size,
                    prompt="(j=down, k=up, t=back, Enter/number=select chapter, q=cancel): ",
                    previous_page=page_index
                )
                if sel == TOC_GO_TO_ARTICLE_START:
                    page_index = 0
                elif sel is not None and isinstance(sel, int):
                    chapter_raw = toc[sel][1]
                    preceding_text = "\n".join(raw_lines[:chapter_raw])
                    preceding_text = remove_wiki_markup(preceding_text)
                    preceding_wrapped = wrap_content(preceding_text, line_width, links)
                    new_page_idx = len(preceding_wrapped) // page_size
                    if new_page_idx >= total_pages:
                        new_page_idx = total_pages - 1
                    page_index = new_page_idx
                selected_link = None
                need_reprint = True

        elif key.lower() == "q":
            keep_going = False

        elif key.lower() == "s":
            search_state.term = None
            search_state.matches = []
            search_state.match_index = 0
            await do_article_search(writer, reader, search_state, wrapped_lines)
            need_reprint = True

        elif key.lower() == "d":
            if not search_state.term:
                await do_article_search(writer, reader, search_state, wrapped_lines)
            else:
                new_pg = await jump_to_next_match(search_state, total_pages, page_size, page_index)
                if new_pg is not None:
                    page_index = new_pg
            selected_link = None
            need_reprint = True

        elif key.lower() == "a":
            # Only proceed if AI is activated
            if conf["AI_ACTIVATED"]:
                article_text = "\n".join(wrapped_lines)
                await show_ai_conversation_overlay(
                    conf,
                    writer, reader,
                    article_text, page_index,
                    user_id, line_width, page_size,
                    is_top_level=False
                )
                need_reprint = True

async def configure_terminal(writer, reader, conf):
    global CONF
    writer.write("\r\n==Configure your terminal==\r\n\r\n")
    writer.write("Select encoding scheme:\r\n1. ASCII\r\n2. Latin-1\r\n3. CP437\r\n4. UTF-8\r\n")
    writer.write("Enter choice [1-4] (default 1): ")
    await writer.drain()
    enc_choice = await read_line_custom(writer, reader)
    if enc_choice == "2":
        enc = "latin-1"
    elif enc_choice == "3":
        enc = "cp437"
    elif enc_choice == "4":
        enc = "utf-8"
    else:
        enc = "ascii"
    if hasattr(reader, 'encoding'):
        reader.encoding = enc
    writer.encoding = enc
    writer.write(f"\r\nEncoding set to: {enc}\r\n\r\n")

    writer.write("Enter desired line width (default 80): ")
    await writer.drain()
    lw_input = await read_line_custom(writer, reader)
    writer.write("\r\n")
    try:
        lw = int(lw_input.strip()) if lw_input.strip() else 80
    except:
        lw = 80
    if lw < 5:
        lw = 5
    writer.write(f"Line width set to: {lw}\r\n\r\n")

    writer.write("Enter desired page size (default 24): ")
    await writer.drain()
    ps_input = await read_line_custom(writer, reader)
    writer.write("\r\n")
    try:
        ps = int(ps_input.strip()) if ps_input.strip() else 23
    except:
        ps = 23
    if ps < 1:
        ps = 1
    writer.write(f"Page size set to: {ps+1}\r\n\r\n")
    await writer.drain()

    real_lw = lw - 2 if lw > 2 else 1
#    writer.write(f"Article wrapping set to {real_lw} (2 less than line width)\r\n\r\n")
    await writer.drain()
    return enc, real_lw, ps

async def top_level_wiki_search(conf, writer, reader, query, line_width, page_size):
    writer.write(f"Searching for '{query}'...\r\n")
    await writer.drain()
    wikipedia.set_lang(conf["LANG"])
    results = wikipedia.search(query)
    if not results:
        writer.write("No results found.\r\n\r\n")
        await writer.drain()
        return

    page_title = results[0]
    writer.write(f"\r\nRetrieving page: {page_title}\r\n")
    await writer.drain()
    try:
        try:
            page = wikipedia.page(title=page_title, auto_suggest=False, preload=False)
        except wikipedia.DisambiguationError as e:
            opts = [opt.strip() for opt in e.options]
            sel = await select_option(
                opts, writer, reader, page_size,
                prompt="(j=down, k=up, Enter/number=select, q=cancel): "
            )
            if sel is None:
                writer.write("\r\nCancelled.\r\n")
                await writer.drain()
                return
            page_title = opts[sel]
            writer.write(f"\r\nRetrieving page: {page_title}\r\n")
            await writer.drain()
            page = wikipedia.page(title=page_title, auto_suggest=False, preload=False)

        content = remove_wiki_markup(page.content)
        content = re.sub(r'\n\s+', '\n', content)
        safe_links = [l for l in page.links if len(l) > 1]

        toc, raw_lines = extract_toc_and_lines(content)
        wrapped = wrap_content(content, line_width, safe_links)

        init_page = 0
        if toc:
            toc_opts = [header for header, _ in toc]
            sel = await select_option(
                toc_opts, writer, reader, page_size,
                prompt="(j=down, k=up, t=back, Enter/number=select chapter, q=cancel): "
            )
            if sel == TOC_GO_TO_ARTICLE_START:
                init_page = 0
            elif sel is not None and isinstance(sel, int):
                chapter_raw = toc[sel][1]
                preceding_text = "\n".join(raw_lines[:chapter_raw])
                preceding_text = remove_wiki_markup(preceding_text)
                preceding_wrapped = wrap_content(preceding_text, line_width, safe_links)
                init_page = len(preceding_wrapped) // page_size

        await paginate_article(
            conf,
            wrapped, writer, reader,
            page_size, line_width,
            raw_lines, toc,
            initial_page=init_page,
            page_title=page_title
        )
        writer.write("\r\n--- End of Article ---\r\n")
        await writer.drain()
    except Exception as e:
        writer.write(f"Error retrieving article: {e}\r\n\r\n")
        await writer.drain()

async def shell(reader, writer):
    global CONF
    if hasattr(writer, 'set_echo'):
        writer.set_echo(False)

    writer.write("\033[2J\033[H")
    writer.write(get_welcome_logo() + "\r\n\r\n")

    # Only display AI model if AI is activated
    if CONF["AI_ACTIVATED"]:
        writer.write(f"Using AI model: {CONF['MODEL']}\r\n\r\n")

    # CAPTCHA before anything else
    writer.write("Captcha: Repeat the first spacecraft to land on another planet three times.\r\nAnswer: ")
    await writer.drain()
    captcha_input = await read_line_custom(writer, reader)
    if captcha_input.lower().count("venera") != 3:
        writer.write("Access denied. Invalid response.\r\n")
        await writer.drain()
        writer.close()
        return

    enc, article_width, page_size = await configure_terminal(writer, reader, CONF)

    # If AI is activated, show both commands, otherwise only wiki
    if CONF["AI_ACTIVATED"]:
        writer.write("Commands: :ai, :wiki, :help, :quit.\r\n")
    else:
        writer.write("Commands: :wiki, :help, :quit.\r\n")

    writer.write(f"Article wrapping: {article_width}, page_size: {page_size+1}\r\n\r\n")
    await writer.drain()

    # Default shell mode
    shell_mode = "wiki"

    while True:
        prompt = "Wiki> " if shell_mode == "wiki" else "AI> "
        writer.write(prompt)
        await writer.drain()

        line = await read_line_custom(writer, reader)
        if not line:
            continue
        cmd = line.strip()
        if not cmd:
            continue

        if cmd.startswith(":"):
            parts = cmd.split()
            c = parts[0].lower()

            if c == ":quit":
                writer.write("Goodbye!\r\n")
                await writer.drain()
                break

            elif c == ":ai":
                if not CONF["AI_ACTIVATED"]:
                    writer.write("[AI not available]\r\n")
                    await writer.drain()
                else:
                    shell_mode = "ai"
                    writer.write("[Switched to AI mode]\r\n")
                    await writer.drain()

            elif c == ":wiki":
                shell_mode = "wiki"
                writer.write("[Switched to Wiki mode]\r\n")
                await writer.drain()

            elif c == ":help":
                if shell_mode == "wiki":
                    writer.write("(In Wiki mode, type text => search. :quit => exit)\r\n")
                    if CONF["AI_ACTIVATED"]:
                        writer.write("During article reading, press 'a' => AI assistant overlay w/ context.\r\n")
                    writer.write("Use 's' or 'd' for internal search, 't' for TOC, etc.\r\n")
                else:
                    # If AI is activated, explain usage. Otherwise, just say it's disabled.
                    if CONF["AI_ACTIVATED"]:
                        writer.write("(In AI mode, type text => conversation. :quit => exit)\r\n")
                    else:
                        writer.write("AI is disabled.\r\n")
                await writer.drain()

            else:
                writer.write("[Unknown command]\r\n")
                await writer.drain()

            continue

        if shell_mode == "wiki":
            await top_level_wiki_search(CONF, writer, reader, cmd, article_width, page_size)
        else:
            # Only proceed if AI is actually activated
            if CONF["AI_ACTIVATED"]:
                user_id = "top-level-ai-user"
                await show_ai_conversation_overlay(
                    CONF,
                    writer, reader,
                    article_text="",
                    article_page=0,
                    user_id=user_id,
                    line_width=article_width,
                    page_size=page_size,
                    is_top_level=True,
                    initial_question=cmd
                )
            else:
                writer.write("[AI not available]\r\n")
                await writer.drain()

    writer.close()

def telnet_fix_newlines(text):
    return re.sub(r'(?<!\r)\n', '\r\n', text)

def main():
    global CONF
    CONF = load_config()
    if CONF["DEBUG"]:
        print("[DEBUG] Loaded config:", CONF)

    port = CONF["PORT"]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    server = telnetlib3.create_server(port=port, shell=shell, encoding='utf8')
    loop.run_until_complete(server)
    print(f"Telnet server running on port {port}")
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print("Server shutting down.")

if __name__ == '__main__':
    main()

