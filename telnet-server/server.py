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
import sqlite3
from datetime import datetime

# ------------- CONFIG AND UTILS ----------------

CONFIG_PATH = os.environ.get("SERVER_CONFIG_PATH", "server.cfg")

def load_config(path=CONFIG_PATH):
    config = configparser.ConfigParser()
    config.read(path)
    debug_str = config.get("general", "debug", fallback="false").lower()
    debug_mode = (debug_str == "true" or debug_str == "1")
    port = config.getint("general", "port", fallback=8023)
    default_language = config.get("general", "default_language", fallback="en")
    ai_websocket_uri = config.get("general", "ai_websocket_uri", fallback="wss://127.0.0.1:50000/ai")
    raw_welcome = config.get("general", "welcome_message", fallback="=== Wikipedia Telnet Gateway ===").replace("\\n", "\r\n").replace("\\r\\n", "\r\n")
    ai_activated_str = config.get("general", "ai_activated", fallback="true").lower()
    ai_activated = (ai_activated_str == "true" or ai_activated_str == "1")
    captcha_disabled_str = config.get("general", "captcha_disabled", fallback="false").lower()
    captcha_disabled = (captcha_disabled_str == "true" or captcha_disabled_str == "1")
    model = config.get("ollama", "model", fallback="smollm2:360m")
    auth_token = config.get("ollama", "auth_token", fallback="AAAAB3NzaC1yc2EAAAADAQABAAABAQDBg")
    return {
        "DEBUG": debug_mode,
        "PORT": port,
        "LANG": default_language,
        "AI_URI": ai_websocket_uri,
        "WELCOME_MSG": raw_welcome,
        "MODEL": model,
        "AI_ACTIVATED": ai_activated,
        "CAPTCHA_DISABLED": captcha_disabled,
        "AUTH_TOKEN": auth_token
    }

def telnet_debug_print(conf, *args, **kwargs):
    if conf["DEBUG"]:
        print("[DEBUG]", *args, **kwargs)

CONF = None

def get_welcome_logo():
    return CONF["WELCOME_MSG"]

def telnet_fix_newlines(text):
    return re.sub(r'(?<!\r)\n', '\r\n', text)

# --------------------- I/O HELPER FUNCTIONS ---------------------

async def read_line_custom(writer, reader):
    if not writer or writer.is_closing():
        return ""
    buffer = []
    while True:
        try:
            ch = await reader.read(1)
        except (BrokenPipeError, ConnectionError):
            return "".join(buffer)
        if not ch:
            return "".join(buffer)
        if ch in ("\r", "\n"):
            try:
                writer.write("\r\n")
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return "".join(buffer)
            return "".join(buffer)
        elif ch in ("\x08", "\x7f"):
            if buffer:
                buffer.pop()
                try:
                    writer.write("\b \b")
                    await writer.drain()
                except (BrokenPipeError, ConnectionError):
                    return "".join(buffer)
        elif ch == "\x1b":
            _ = await reader.read(2)
        else:
            buffer.append(ch)
            try:
                writer.write(ch)
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return "".join(buffer)

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

async def paginate(lines, writer, reader, page_size, is_guestbook=False):
    if not writer or writer.is_closing():
        return False
    total_lines = len(lines)
    if total_lines == 0:
        try:
            writer.write("No content.\r\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionError):
            return False
        return False
    total_pages = (total_lines + page_size - 1) // page_size
    page_index = 0
    digit_buffer = ""
    need_reprint = True
    while True:
        if need_reprint:
            try:
                writer.write("\033[2J\033[H")
                start = page_index * page_size
                end = min(start + page_size, total_lines)
                for line in lines[start:end]:
                    writer.write(line + "\r\n")
                prompt = (
                    f"\r\n-- Page {page_index+1}/{total_pages} -- "
                    f"({'Enter=input, ' if is_guestbook else ''}h/l=prev/next, q(w)=exit): {digit_buffer}"
                )
                writer.write(prompt)
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return False
            need_reprint = False
        key = await reader.read(1)
        if not key:
            return False
        if key.isdigit():
            digit_buffer += key
            try:
                writer.write(f"\r{clear_line()}-- Page {page_index+1}/{total_pages} -- "
                            f"({'Enter=input, ' if is_guestbook else ''}h/l=prev/next, q(w)=exit): {digit_buffer}")
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return False
            continue
        if key in ("\r", "\n"):
            if digit_buffer:
                try:
                    new_page = int(digit_buffer) - 1
                    if 0 <= new_page < total_pages:
                        page_index = new_page
                        digit_buffer = ""
                        need_reprint = True
                    else:
                        digit_buffer = ""
                except ValueError:
                    digit_buffer = ""
            elif is_guestbook:
                return "input"
            else:
                return "query"
            continue
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
        if key in ("l", "j"):
            if total_pages > 1:
                page_index = (page_index + 1) % total_pages
                digit_buffer = ""
                need_reprint = True
        elif key in ("h", "k"):
            if total_pages > 1:
                page_index = (page_index - 1) % total_pages
                digit_buffer = ""
                need_reprint = True
        elif key == "q":
            return False
        elif key == "w":
            return True

# --------------------- WIKI HELPER FUNCTIONS ---------------------

def extract_toc_and_lines(content):
    lines = content.splitlines()
    toc = []
    for idx, line in enumerate(lines):
        m = re.match(r'^(={2,})([^=].*?)(={2,})\s*$', line.strip())
        if m:
            toc.append((m.group(2).strip(), idx))
    return toc, lines

def remove_wiki_markup(text):
    return re.sub(
        r'\[\[([^|\]]+)(?:\|([^\]]+))?\]\]',
        lambda m: m.group(2) if m.group(2) else m.group(1),
        text
    )

def linkify_preserving_case(content, links):
    valid_links = [l for l in links if len(l) > 1]
    if not valid_links:
        return content, {}
    pattern = r'(' + r'|'.join(re.escape(lnk) for lnk in valid_links) + r')'
    placeholders = {}
    idx = 0
    def replacement(m):
        nonlocal idx
        matched = m.group(0)
        placeholder = f"{{PLCH{idx}}}"
        placeholders[placeholder] = f"[{matched}]"
        idx += 1
        return placeholder
    new_content = re.sub(pattern, replacement, content, flags=re.IGNORECASE)
    return new_content, placeholders

def final_wrap_after_injection(lines, line_width):
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
    content, placeholders = linkify_preserving_case(content, links)
    content = re.sub(r'\[\d+\]', '', content)
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
    for i in range(len(lines)):
        for placeholder, bracket_text in placeholders.items():
            lines[i] = lines[i].replace(placeholder, bracket_text)
    lines = final_wrap_after_injection(lines, line_width)
    return lines

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

# --------------------- CONFIGURE TERMINAL ---------------------

async def configure_terminal(writer, reader, conf):
    if not writer or writer.is_closing():
        return "ascii", 80, 23
    try:
        writer.write(f"{get_welcome_logo()}\r\n")
        if conf["AI_ACTIVATED"]:
            writer.write(f"AI model: {conf['MODEL']}\r\n")
        writer.write("Software wikipedia-live-telnet:\r\nhttps://github.com/ballerburg9005/wikipedia-live-telnet\r\n\r\n")
        writer.write("========Configure your terminal========\r\n")
        writer.write("Terminal size (cols x rows) [80x24]: ")
        await writer.drain()
    except (BrokenPipeError, ConnectionError):
        return "ascii", 80, 23
    size_input = await read_line_custom(writer, reader)
    if not size_input.strip():
        size_input = "80x24"
        try:
            writer.write("80x24")
            await writer.drain()
        except (BrokenPipeError, ConnectionError):
            return "ascii", 80, 23
    try:
        cols, rows = map(int, size_input.lower().replace("x", " ").split())
        if cols < 5 or rows < 1:
            raise ValueError
    except ValueError:
        try:
            writer.write("\r\nInvalid size, use 'cols x rows'. Defaulting to 80x24.\r\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionError):
            return "ascii", 80, 23
        cols, rows = 80, 24
    try:
        writer.write("\r\nTerminal type [dumb]: ")
        await writer.drain()
    except (BrokenPipeError, ConnectionError):
        return "ascii", cols, rows - 2
    term_type = await read_line_custom(writer, reader)
    if not term_type.strip():
        term_type = "dumb"
        try:
            writer.write("dumb")
            await writer.drain()
        except (BrokenPipeError, ConnectionError):
            return "ascii", cols, rows - 2
    if term_type.lower() != "dumb":
        try:
            writer.write("\r\nValid option is: dumb\r\nTerminal type [dumb]: ")
            await writer.drain()
            term_type = await read_line_custom(writer, reader)
            if not term_type.strip():
                term_type = "dumb"
                writer.write("dumb")
                await writer.drain()
            elif term_type.lower() != "dumb":
                writer.write("\r\nDefaulting to dumb.\r\n")
                await writer.drain()
                term_type = "dumb"
        except (BrokenPipeError, ConnectionError):
            return "ascii", cols, rows - 2
    try:
        writer.write("\r\nCharacter set [ASCII]: ")
        await writer.drain()
    except (BrokenPipeError, ConnectionError):
        return "ascii", cols, rows - 2
    enc_choice = await read_line_custom(writer, reader)
    if not enc_choice.strip():
        enc_choice = "ASCII"
        try:
            writer.write("ASCII")
            await writer.drain()
        except (BrokenPipeError, ConnectionError):
            return "ascii", cols, rows - 2
    valid_encodings = {"ascii": "ascii", "latin-1": "latin-1", "cp437": "cp437", "utf-8": "utf-8"}
    enc = valid_encodings.get(enc_choice.lower(), None)
    if not enc:
        try:
            writer.write("\r\nValid options are: ASCII, Latin-1, CP437, UTF-8\r\nCharacter set [ASCII]: ")
            await writer.drain()
            enc_choice = await read_line_custom(writer, reader)
            if not enc_choice.strip():
                enc_choice = "ASCII"
                writer.write("ASCII")
                await writer.drain()
            enc = valid_encodings.get(enc_choice.lower(), "ascii")
            if not enc:
                writer.write("\r\nDefaulting to ASCII.\r\n")
                await writer.drain()
                enc = "ascii"
        except (BrokenPipeError, ConnectionError):
            return "ascii", cols, rows - 2
    if hasattr(reader, 'encoding'):
        reader.encoding = enc
    writer.encoding = enc
    try:
        writer.write(f"\r\n\r\nCommands: :ai, :wiki, :guestbook, :help, :quit.\r\n")
        writer.write(f"Article wrapping: {cols-2 if cols > 2 else 1}, page_size: {rows}\r\n\r\n")
        await writer.drain()
    except (BrokenPipeError, ConnectionError):
        return enc, cols - 2 if cols > 2 else 1, rows - 2
    return enc, cols - 2 if cols > 2 else 1, rows - 2

# --------------------- ARTICLE SEARCH FUNCTIONS ---------------------

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

async def do_article_search(writer, reader, search_state, wrapped_lines):
    if not writer or writer.is_closing():
        return
    try:
        writer.write("\r\n=== Internal Article Search ===\r\nSearch for: ")
        await writer.drain()
    except (BrokenPipeError, ConnectionError):
        return
    srch = await read_line_custom(writer, reader)
    srch = srch.strip()
    if not srch:
        try:
            writer.write("No search term given.\r\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionError):
            return
        return
    search_state.term = srch
    search_state.matches = find_all_matches_in_wrapped(wrapped_lines, srch)
    search_state.match_index = 0
    if not search_state.matches:
        try:
            writer.write("No matches found.\r\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionError):
            return
        search_state.term = None
    else:
        try:
            writer.write(f"Found {len(search_state.matches)} matches.\r\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionError):
            return

async def jump_to_next_match(search_state, total_pages, page_size, current_page):
    if not search_state.term or not search_state.matches:
        return None
    total_matches = len(search_state.matches)
    current_line = current_page * page_size
    start_idx = 0
    for i, (line_idx, _, _) in enumerate(search_state.matches):
        if line_idx >= current_line:
            start_idx = i
            break
    search_state.match_index = start_idx - 1
    for _ in range(total_matches):
        search_state.match_index = (search_state.match_index + 1) % total_matches
        line_idx, _, _ = search_state.matches[search_state.match_index]
        page_idx = line_idx // page_size
        if page_idx != current_page:
            return page_idx
    return None

async def jump_to_prev_match(search_state, total_pages, page_size, current_page):
    if not search_state.term or not search_state.matches:
        return None
    total_matches = len(search_state.matches)
    current_line = current_page * page_size
    start_idx = total_matches - 1
    for i, (line_idx, _, _) in enumerate(reversed(search_state.matches)):
        if line_idx < current_line:
            start_idx = total_matches - 1 - i
            break
    search_state.match_index = start_idx + 1
    for _ in range(total_matches):
        search_state.match_index = (search_state.match_index - 1) % total_matches
        line_idx, _, _ = search_state.matches[search_state.match_index]
        page_idx = line_idx // page_size
        if page_idx != current_page:
            return page_idx
    return None

async def loading_dots(writer):
    if not writer or writer.is_closing():
        return
    dots = ""
    try:
        while True:
            try:
                dots += "."
                writer.write(f"\rLoading{dots}\r")
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return
            await asyncio.sleep(0.3)
    except asyncio.CancelledError:
        pass

# --------------------- PAGINATE ARTICLE ---------------------

async def paginate_article(
    conf,
    wrapped_lines, writer, reader,
    page_size, line_width,
    raw_lines, toc,
    initial_page=0,
    page_title=None
):
    if not writer or writer.is_closing():
        return False
    user_id = str(uuid.uuid4())
    total_lines = len(wrapped_lines)
    if total_lines == 0:
        try:
            writer.write("Article is empty.\r\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionError):
            return False
        return False
    total_pages = (total_lines + page_size - 1) // page_size
    page_index = initial_page
    search_state = ArticleSearchState()
    need_reprint = True
    keep_going = True
    digit_buffer = ""

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
                link_positions.append((start_line, m.start() - line_start, m.end() - line_start, link))
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
        try:
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
        except (BrokenPipeError, ConnectionError):
            return

    selected_link = None

    while keep_going:
        if need_reprint:
            try:
                writer.write("\033[2J\033[H")
                start = page_index * page_size
                end = min(start + page_size, total_lines)
                page_lines = wrapped_lines[start:end]
                page_lines = highlight_lines_with_links(page_lines, page_index, selected_link)
                for line in page_lines:
                    writer.write(line + "\r\n")
                prompt = (
                    f"\r\n-- Page {page_index+1}/{total_pages} -- "
                    f"(hjkl=nav, t=TOC, q(w)=exit, s/d/f=search"
                    f"{', a=AI' if conf['AI_ACTIVATED'] else ''}): {digit_buffer}"
                )
                writer.write(prompt)
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return False
            need_reprint = False

        key = await reader.read(1)
        if not key:
            return False

        if key.isdigit():
            digit_buffer += key
            try:
                writer.write(f"\r{clear_line()}-- Page {page_index+1}/{total_pages} -- "
                            f"(hjkl=nav, t=TOC, q(w)=exit, s/d/f=search"
                            f"{', a=AI' if conf['AI_ACTIVATED'] else ''}): {digit_buffer}")
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return False
            continue

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

        page_links = get_page_links(page_index)

        if key in ("\r", "\n"):
            if digit_buffer:
                try:
                    new_page = int(digit_buffer) - 1
                    if 0 <= new_page < total_pages:
                        page_index = new_page
                        digit_buffer = ""
                        need_reprint = True
                    else:
                        digit_buffer = ""
                except ValueError:
                    digit_buffer = ""
            elif selected_link is not None and page:
                link_title = page_links[selected_link][3]
                try:
                    writer.write("\033[2J\033[HLoading\r")
                    await writer.drain()
                except (BrokenPipeError, ConnectionError):
                    return False
                loading_task = asyncio.create_task(loading_dots(writer))
                try:
                    new_page = await asyncio.to_thread(wikipedia.page, title=link_title, auto_suggest=False)
                except:
                    loading_task.cancel()
                    try:
                        await loading_task
                    except asyncio.CancelledError:
                        pass
                    try:
                        writer.write(f"\r{clear_line()}Failed to load link.\r\n")
                        await writer.drain()
                    except (BrokenPipeError, ConnectionError):
                        return False
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
                try:
                    writer.write(clear_line())
                    await writer.drain()
                except (BrokenPipeError, ConnectionError):
                    return False
                if await paginate_article(conf, new_wrapped, writer, reader, page_size, line_width, new_raw, new_toc, page_title=link_title):
                    return True
                need_reprint = True
            elif total_pages > 1:
                page_index = (page_index + 1) % total_pages
                need_reprint = True
            continue

        elif key == "l":
            if total_pages > 1:
                page_index = (page_index + 1) % total_pages
                selected_link = None
                need_reprint = True
                digit_buffer = ""

        elif key == "h":
            if total_pages > 1:
                page_index = (page_index - 1) % total_pages
                selected_link = None
                need_reprint = True
                digit_buffer = ""

        elif key == "j" and page_links:
            old_sel = selected_link
            if selected_link is None:
                selected_link = 0
            elif selected_link < len(page_links) - 1:
                selected_link += 1
            else:
                selected_link = None
            if old_sel != selected_link:
                await update_link_selection(old_sel, selected_link, page_index)

        elif key == "k" and page_links:
            old_sel = selected_link
            if selected_link is None:
                selected_link = len(page_links) - 1
            elif selected_link > 0:
                selected_link -= 1
            else:
                selected_link = None
            if old_sel != selected_link:
                await update_link_selection(old_sel, selected_link, page_index)

        elif key == "t" and toc:
            toc_opts = [header for header, _ in toc]
            sel = await select_option(toc_opts, writer, reader, page_size, "(hjkl=nav, t=exit-TOC, q(w)=exit): ", page_index, is_toc=True)
            if sel == "superquit":
                return True
            if sel == TOC_GO_TO_ARTICLE_START:
                page_index = 0
                need_reprint = True
            elif sel is not None and isinstance(sel, int):
                chapter_raw = toc[sel][1]
                preceding_text = "\n".join(raw_lines[:chapter_raw])
                preceding_text = remove_wiki_markup(preceding_text)
                preceding_wrapped = wrap_content(preceding_text, line_width, links)
                new_page_idx = len(preceding_wrapped) // page_size
                if new_page_idx >= total_pages:
                    new_page_idx = total_pages - 1
                page_index = new_page_idx
                need_reprint = True
            try:
                writer.write("\033[2J\033[H")
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return False
            selected_link = None
            digit_buffer = ""

        elif key == "q":
            keep_going = False

        elif key == "w":
            return True

        elif key == "s":
            search_state.term = None
            search_state.matches = []
            search_state.match_index = 0
            await do_article_search(writer, reader, search_state, wrapped_lines)
            need_reprint = True
            selected_link = None

        elif key == "d" and search_state.term:
            new_pg = await jump_to_next_match(search_state, total_pages, page_size, page_index)
            if new_pg is not None:
                page_index = new_pg
                need_reprint = True
            selected_link = None

        elif key == "f" and search_state.term:
            new_pg = await jump_to_prev_match(search_state, total_pages, page_size, page_index)
            if new_pg is not None:
                page_index = new_pg
                need_reprint = True
            selected_link = None

        elif key == "a" and conf["AI_ACTIVATED"]:
            article_text = "\n".join(wrapped_lines)
            try:
                writer.write("\033[2J\033[H=== AI Assistant Overlay ===\r\n(Type your question, Enter=query, q(w)=exit)\r\n\r\nYou> ")
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return False
            if await show_ai_conversation_overlay(conf, writer, reader, article_text, page_index, user_id, line_width, page_size, previous_mode="wiki"):
                return True
            need_reprint = True

    return False

# --------------------- AI CONVERSATION OVERLAY ---------------------

async def stream_ai_with_spinner_and_interrupts(
    conf,
    question, article_context, article_page,
    user_id, conversation_history, writer, reader, max_width=80
):
    if not writer or writer.is_closing():
        return "", True, False
    payload = {
        "user_id": user_id,
        "conversation": [(ts, speaker, text) for ts, speaker, text in conversation_history],
        "context": article_context,
        "page_index": article_page,
        "new_question": question,
        "auth_token": conf["AUTH_TOKEN"]
    }
    partial_tokens = []
    current_line = ""
    stop_flag = False
    user_canceled = False
    user_cleared = False
    superquit = False
    last_token_time = asyncio.get_event_loop().time()
    spinner_index = 0

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    uri = conf["AI_URI"]

    async def read_websocket():
        nonlocal stop_flag, last_token_time, current_line, superquit
        try:
            async with websockets.connect(uri, ping_interval=None, ssl=ssl_context) as ws:
                await ws.send(json.dumps(payload))
                try:
                    writer.write("MULTIVAC>\r\n")
                    await writer.drain()
                except (BrokenPipeError, ConnectionError):
                    stop_flag = True
                    return
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
                                        try:
                                            writer.write("\r\n")
                                            await writer.drain()
                                        except (BrokenPipeError, ConnectionError):
                                            stop_flag = True
                                            return
                                        current_line = ""
                                    current_line += part
                                    try:
                                        writer.write(part)
                                        await writer.drain()
                                    except (BrokenPipeError, ConnectionError):
                                        stop_flag = True
                                        return
                                    if i < len(parts) - 1:
                                        try:
                                            writer.write("\r\n")
                                            await writer.drain()
                                        except (BrokenPipeError, ConnectionError):
                                            stop_flag = True
                                            return
                                        current_line = ""
                                partial_tokens.append(token_text)
                            else:
                                if len(current_line) + len(token_text) > max_width:
                                    try:
                                        writer.write("\r\n")
                                        await writer.drain()
                                    except (BrokenPipeError, ConnectionError):
                                        stop_flag = True
                                        return
                                    current_line = ""
                                current_line += token_text
                                try:
                                    writer.write(token_text)
                                    await writer.drain()
                                except (BrokenPipeError, ConnectionError):
                                    stop_flag = True
                                    return
                                partial_tokens.append(token_text)
                        last_token_time = asyncio.get_event_loop().time()
                    except asyncio.TimeoutError:
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        break
        except Exception as e:
            telnet_debug_print(conf, "WebSocket AI error:", e)
        finally:
            try:
                writer.write("\r\n")
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                pass
            stop_flag = True

    async def read_keystrokes():
        nonlocal stop_flag, user_canceled, user_cleared, superquit
        while not stop_flag:
            ckey = await reader.read(1)
            if not ckey:
                continue
            if ckey == 'q':
                user_canceled = True
                stop_flag = True
            elif ckey == 'w':
                user_canceled = True
                superquit = True
                stop_flag = True
            elif ckey == 'c':
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
                except (BrokenPipeError, ConnectionError):
                    stop_flag = True
                    break
            await asyncio.sleep(SPIN_INTERVAL)

    t_ws = asyncio.create_task(read_websocket())
    t_keys = asyncio.create_task(read_keystrokes())
    t_spin = asyncio.create_task(spinner_task())
    await asyncio.wait([t_ws, t_keys, t_spin], return_when=asyncio.FIRST_COMPLETED)
    stop_flag = True
    for task in [t_ws, t_keys, t_spin]:
        task.cancel()
    try:
        await asyncio.gather(t_ws, t_keys, t_spin, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    final_text = "".join(partial_tokens)
    if user_cleared:
        final_text = ""
    return final_text, user_canceled, superquit

async def show_ai_conversation_overlay(
    conf,
    writer, reader,
    article_text, article_page,
    user_id, line_width, page_size,
    is_top_level=False,
    initial_question=None,
    previous_mode="wiki"
):
    if not writer or writer.is_closing():
        return False
    conversation = []
    while True:
        if initial_question or not is_top_level:
            question = initial_question.strip() if initial_question else await read_line_custom(writer, reader)
            initial_question = None
            if not question:
                if not is_top_level:
                    lines_for_pagination = []
                    for ts, speaker, text in sorted(conversation, key=lambda x: x[0], reverse=True):
                        if speaker == "You":
                            lines_for_pagination.append(f"You> {text}")
                        elif speaker == "AI":
                            lines_for_pagination.append(f"MULTIVAC> {text}")
                        elif speaker == "Error":
                            lines_for_pagination.append(f"[ERROR: {text}]")
                        lines_for_pagination.append("")
                    wrapped_for_pagination = wrap_block_of_text(lines_for_pagination, line_width)
                    result = await paginate(wrapped_for_pagination, writer, reader, page_size)
                    if result == "query":
                        try:
                            writer.write("=== AI Assistant Overlay ===\r\n(Type your question, Enter=query, q(w)=exit)\r\n\r\nYou> ")
                            await writer.drain()
                        except (BrokenPipeError, ConnectionError):
                            return False
                        continue
                    elif result:
                        return True
                try:
                    writer.write("\r\n")
                    await writer.drain()
                except (BrokenPipeError, ConnectionError):
                    return False
                return False
            if question == "q":
                try:
                    writer.write("[Exiting AI assistant overlay]\r\n")
                    await writer.drain()
                except (BrokenPipeError, ConnectionError):
                    return False
                return False
            if question == "w":
                return True
            conversation.append((datetime.now().timestamp(), "You", question))
            try:
                final_text, canceled, superquit = await stream_ai_with_spinner_and_interrupts(
                    conf, question, article_text, article_page, user_id, conversation, writer, reader, line_width
                )
                if superquit:
                    return True
                if canceled:
                    if final_text == "":
                        conversation.append((datetime.now().timestamp(), "AI", "[User cleared partial response]"))
                    else:
                        conversation.append((datetime.now().timestamp(), "AI", final_text + " [User canceled]"))
                else:
                    conversation.append((datetime.now().timestamp(), "AI", final_text))
            except Exception as e:
                conversation.append((datetime.now().timestamp(), "Error", f"AI assistant connection error: {e}\nSorry MULTIVAC is offline"))
            lines_for_pagination = []
            for ts, speaker, text in sorted(conversation, key=lambda x: x[0], reverse=True):
                if speaker == "You":
                    lines_for_pagination.append(f"You> {text}")
                elif speaker == "AI":
                    lines_for_pagination.append(f"MULTIVAC> {text}")
                elif speaker == "Error":
                    lines_for_pagination.append(f"[ERROR: {text}]")
                lines_for_pagination.append("")
            wrapped_for_pagination = wrap_block_of_text(lines_for_pagination, line_width)
            result = await paginate(wrapped_for_pagination, writer, reader, page_size)
            if result == "query":
                try:
                    writer.write("=== AI Assistant Overlay ===\r\n(Type your question, Enter=query, q(w)=exit)\r\n\r\nYou> ")
                    await writer.drain()
                except (BrokenPipeError, ConnectionError):
                    return False
                continue
            elif result:
                return True
            try:
                writer.write("\r\n")
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return False
            return False
        else:
            try:
                writer.write("\033[2J\033[H=== AI Assistant Shell Mode ===\r\n(Type your question, Enter=query, q(w)=exit)\r\n\r\nYou> ")
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return False
            question = await read_line_custom(writer, reader)
            question = question.strip()
            if not question:
                try:
                    writer.write("=== AI Assistant Shell Mode ===\r\n(Type your question, Enter=query, q(w)=exit)\r\n\r\nYou> ")
                    await writer.drain()
                except (BrokenPipeError, ConnectionError):
                    return False
                continue
            if question == "q":
                try:
                    writer.write("[Exiting AI assistant shell]\r\n")
                    await writer.drain()
                except (BrokenPipeError, ConnectionError):
                    return False
                return False
            if question == "w":
                try:
                    writer.write("\r\n")
                    await writer.drain()
                except (BrokenPipeError, ConnectionError):
                    return False
                return True
            conversation.append((datetime.now().timestamp(), "You", question))
            try:
                final_text, canceled, superquit = await stream_ai_with_spinner_and_interrupts(
                    conf, question, article_text, article_page, user_id, conversation, writer, reader, line_width
                )
                if superquit:
                    try:
                        writer.write("\r\n")
                        await writer.drain()
                    except (BrokenPipeError, ConnectionError):
                        return False
                    return True
                if canceled:
                    if final_text == "":
                        conversation.append((datetime.now().timestamp(), "AI", "[User cleared partial response]"))
                    else:
                        conversation.append((datetime.now().timestamp(), "AI", final_text + " [User canceled]"))
                else:
                    conversation.append((datetime.now().timestamp(), "AI", final_text))
            except Exception as e:
                conversation.append((datetime.now().timestamp(), "Error", f"AI assistant connection error: {e}\nSorry MULTIVAC is offline"))
            lines_for_pagination = []
            for ts, speaker, text in sorted(conversation, key=lambda x: x[0], reverse=True):
                if speaker == "You":
                    lines_for_pagination.append(f"You> {text}")
                elif speaker == "AI":
                    lines_for_pagination.append(f"MULTIVAC> {text}")
                elif speaker == "Error":
                    lines_for_pagination.append(f"[ERROR: {text}]")
                lines_for_pagination.append("")
            wrapped_for_pagination = wrap_block_of_text(lines_for_pagination, line_width)
            result = await paginate(wrapped_for_pagination, writer, reader, page_size)
            if result == "query":
                try:
                    writer.write("=== AI Assistant Shell Mode ===\r\n(Type your question, Enter=query, q(w)=exit)\r\n\r\nYou> ")
                    await writer.drain()
                except (BrokenPipeError, ConnectionError):
                    return False
                continue
            elif result:
                try:
                    writer.write("\r\n")
                    await writer.drain()
                except (BrokenPipeError, ConnectionError):
                    return False
                return True

# --------------------- GUESTBOOK FUNCTIONS ---------------------

def init_guestbook_db():
    try:
        conn = sqlite3.connect("guestbook.db")
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            name TEXT NOT NULL,
            comment TEXT NOT NULL
        )''')
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        print(f"[ERROR] Guestbook DB init failed: {e}")

def add_guestbook_entry(name, comment):
    try:
        conn = sqlite3.connect("guestbook.db")
        c = conn.cursor()
        date = datetime.now().strftime("%Y-%m-%d %H:%M")
        c.execute("INSERT INTO entries (date, name, comment) VALUES (?, ?, ?)", (date, name, comment))
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        print(f"[ERROR] Guestbook entry add failed: {e}")

def get_guestbook_entries():
    try:
        conn = sqlite3.connect("guestbook.db")
        c = conn.cursor()
        c.execute("SELECT date, name, comment FROM entries ORDER BY id DESC")
        entries = c.fetchall()
        conn.close()
        return entries
    except sqlite3.Error as e:
        print(f"[ERROR] Guestbook fetch failed: {e}")
        return []

async def show_guestbook(writer, reader, line_width, page_size):
    if not writer or writer.is_closing():
        return False
    init_guestbook_db()
    entries = get_guestbook_entries()
    max_comment_len = line_width * (page_size * 2 // 3)
    lines_for_pagination = []
    for date, name, comment in entries:
        lines_for_pagination.append(f"{date} | {name}")
        wrapped_comment = textwrap.fill(comment, width=line_width).splitlines()
        lines_for_pagination.extend(wrapped_comment)
        lines_for_pagination.append("")
    total_lines = len(lines_for_pagination)
    total_pages = (total_lines + page_size - 1) // page_size if lines_for_pagination else 1
    page_index = 0
    digit_buffer = ""
    need_reprint = True
    while True:
        if need_reprint:
            try:
                writer.write("\033[2J\033[H")
                start = page_index * page_size
                end = min(start + page_size, total_lines)
                for line in lines_for_pagination[start:end]:
                    writer.write(line + "\r\n")
                prompt = f"\r\n-- Page {page_index+1}/{total_pages} -- (Enter=input, h/l=prev/next, q(w)=exit): {digit_buffer}"
                writer.write(prompt)
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return False
            need_reprint = False
        key = await reader.read(1)
        if not key:
            return False
        if key.isdigit():
            digit_buffer += key
            try:
                writer.write(f"\r{clear_line()}-- Page {page_index+1}/{total_pages} -- (Enter=input, h/l=prev/next, q(w)=exit): {digit_buffer}")
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return False
            continue
        if key in ("\r", "\n"):
            if digit_buffer:
                try:
                    new_page = int(digit_buffer) - 1
                    if 0 <= new_page < total_pages:
                        page_index = new_page
                        digit_buffer = ""
                        need_reprint = True
                    else:
                        digit_buffer = ""
                except ValueError:
                    digit_buffer = ""
            else:
                while True:
                    try:
                        writer.write("\033[2J\033[H=== New Guestbook Entry ===\r\nName (max 20 chars): ")
                        await writer.drain()
                    except (BrokenPipeError, ConnectionError):
                        return False
                    name = await read_line_custom(writer, reader)
                    name = name.strip()
                    if len(name) > 20:
                        try:
                            writer.write("Error: Name too long. Try again.\r\n")
                            await writer.drain()
                        except (BrokenPipeError, ConnectionError):
                            return False
                        await asyncio.sleep(1)
                        continue
                    try:
                        writer.write(f"Comment (max {max_comment_len} chars):\r\n")
                        await writer.drain()
                    except (BrokenPipeError, ConnectionError):
                        return False
                    comment = await read_line_custom(writer, reader)
                    comment = comment.strip()[:max_comment_len]
                    if name and comment:
                        add_guestbook_entry(name, comment)
                        try:
                            writer.write("Entry added!\r\n")
                            await writer.drain()
                        except (BrokenPipeError, ConnectionError):
                            return False
                        entries = get_guestbook_entries()
                        lines_for_pagination = []
                        for date, name, comment in entries:
                            lines_for_pagination.append(f"{date} | {name}")
                            wrapped_comment = textwrap.fill(comment, width=line_width).splitlines()
                            lines_for_pagination.extend(wrapped_comment)
                            lines_for_pagination.append("")
                        total_lines = len(lines_for_pagination)
                        total_pages = (total_lines + page_size - 1) // page_size
                        page_index = 0
                    else:
                        try:
                            writer.write("Entry discarded (empty name or comment).\r\n")
                            await writer.drain()
                        except (BrokenPipeError, ConnectionError):
                            return False
                    await asyncio.sleep(1)
                    need_reprint = True
                    break
            continue
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
        if key in ("l", "j"):
            if total_pages > 1:
                page_index = (page_index + 1) % total_pages
                digit_buffer = ""
                need_reprint = True
        elif key in ("h", "k"):
            if total_pages > 1:
                page_index = (page_index - 1) % total_pages
                digit_buffer = ""
                need_reprint = True
        elif key == "q":
            try:
                writer.write("\r\n")
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return False
            return False
        elif key == "w":
            try:
                writer.write("\r\n")
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return False
            return True

# --------------------- TOP LEVEL WIKI SEARCH ---------------------

async def top_level_wiki_search(conf, writer, reader, query, line_width, page_size):
    if not writer or writer.is_closing():
        return False
    try:
        writer.write(f"Searching for '{query}'...\r\n")
        await writer.drain()
    except (BrokenPipeError, ConnectionError):
        return False
    wikipedia.set_lang(conf["LANG"])
    results = wikipedia.search(query)
    if not results:
        try:
            writer.write("No results found.\r\n\r\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionError):
            return False
        return False
    page_title = results[0]
    try:
        writer.write(f"\r\nRetrieving page: {page_title}\r\n")
        await writer.drain()
    except (BrokenPipeError, ConnectionError):
        return False
    try:
        try:
            page = wikipedia.page(title=page_title, auto_suggest=False, preload=False)
        except wikipedia.DisambiguationError as e:
            opts = [opt.strip() for opt in e.options]
            sel = await select_option(opts, writer, reader, page_size, "(h/l=prev/next, j/k=chapter, q(w)=exit): ", is_toc=False)
            if sel == "superquit":
                return True
            if sel is None:
                try:
                    writer.write("\r\nCancelled.\r\n")
                    await writer.drain()
                except (BrokenPipeError, ConnectionError):
                    return False
                return False
            page_title = opts[sel]
            try:
                writer.write(f"\r\nRetrieving page: {page_title}\r\n")
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return False
            page = wikipedia.page(title=page_title, auto_suggest=False, preload=False)
        content = remove_wiki_markup(page.content)
        content = re.sub(r'\n\s+', '\n', content)
        safe_links = [l for l in page.links if len(l) > 1]
        toc, raw_lines = extract_toc_and_lines(content)
        wrapped = wrap_content(content, line_width, safe_links)
        init_page = 0
        if toc:
            toc_opts = [header for header, _ in toc]
            sel = await select_option(toc_opts, writer, reader, page_size, "(hjkl=nav, t=exit-TOC, q(w)=exit): ", init_page, is_toc=True)
            if sel == "superquit":
                return True
            if sel == TOC_GO_TO_ARTICLE_START:
                init_page = 0
            elif sel is not None and isinstance(sel, int):
                chapter_raw = toc[sel][1]
                preceding_text = "\n".join(raw_lines[:chapter_raw])
                preceding_text = remove_wiki_markup(preceding_text)
                preceding_wrapped = wrap_content(preceding_text, line_width, safe_links)
                init_page = len(preceding_wrapped) // page_size
        if await paginate_article(conf, wrapped, writer, reader, page_size, line_width, raw_lines, toc, init_page, page_title):
            return True
        try:
            writer.write("\r\n--- End of Article ---\r\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionError):
            return False
    except Exception as e:
        try:
            writer.write(f"Error retrieving article: {e}\r\n\r\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionError):
            return False
    return False

# --------------------- SELECT OPTION ---------------------

async def select_option(options, writer, reader, page_size, prompt, previous_page=None, is_toc=False):
    if not writer or writer.is_closing():
        return None
    selected = 0
    digit_buffer = ""
    display_options = ["[Start]"] + options if is_toc else options
    total = len(display_options)
    total_pages = (total + page_size - 1) // page_size
    page_index = 0
    need_reprint = True

    async def print_full_page(page_idx, sel_idx, digits):
        try:
            writer.write("\033[2J\033[H")
            start = page_idx * page_size
            end = min(start + page_size, total)
            for i in range(start, end):
                arrow = "-> " if i == sel_idx else "   "
                writer.write(f"{i}. {arrow}{display_options[i]}\r\n")
            writer.write(f"\r\n-- Page {page_idx+1}/{total_pages} -- {prompt} {digits}")
            await writer.drain()
        except (BrokenPipeError, ConnectionError):
            return False
        return True

    async def update_selection_inplace(old_sel, new_sel):
        page_start = page_index * page_size
        page_end = min(page_start + page_size, total)
        lines_in_page = page_end - page_start
        old_offset = old_sel - page_start
        new_offset = new_sel - page_start
        lines_to_move_up = (lines_in_page + 2 - old_offset)
        try:
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
        except (BrokenPipeError, ConnectionError):
            return False
        return True

    if not await print_full_page(page_index, selected, digit_buffer):
        return None

    while True:
        key = await reader.read(1)
        if not key:
            return None
        if key.isdigit():
            digit_buffer += key
            try:
                writer.write(cursor_up(1) + cursor_carriage_return() + clear_line())
                writer.write(f"\r\n-- Page {page_index+1}/{total_pages} -- {prompt} {digit_buffer}")
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return None
            continue
        if key in ("\r", "\n"):
            if digit_buffer:
                try:
                    choice = int(digit_buffer)
                    if 0 <= choice <= total - 1:
                        return TOC_GO_TO_ARTICLE_START if is_toc and choice == 0 else (choice - 1 if is_toc else choice)
                    digit_buffer = ""
                except ValueError:
                    digit_buffer = ""
            else:
                return TOC_GO_TO_ARTICLE_START if is_toc and selected == 0 else (selected - 1 if is_toc else selected)
        if key == "q":
            try:
                writer.write("\033[2J\033[HAmbiguous selection cancelled. Please be more specific.\r\n")
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return None
            return None
        if key == "w":
            return "superquit"
        if key == "t" and is_toc:
            return TOC_GO_TO_ARTICLE_START
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
        if key == "k":
            old_sel = selected
            selected = (selected - 1) % total
            new_page = selected // page_size
            if new_page != page_index:
                page_index = new_page
                if not await print_full_page(page_index, selected, digit_buffer):
                    return None
            else:
                if not await update_selection_inplace(old_sel, selected):
                    return None
        elif key == "j":
            old_sel = selected
            selected = (selected + 1) % total
            new_page = selected // page_size
            if new_page != page_index:
                page_index = new_page
                if not await print_full_page(page_index, selected, digit_buffer):
                    return None
            else:
                if not await update_selection_inplace(old_sel, selected):
                    return None
        elif key == "l" and total_pages > 1:
            page_index = (page_index + 1) % total_pages
            selected = page_index * page_size
            if selected >= total:
                selected = total - 1
            if not await print_full_page(page_index, selected, digit_buffer):
                return None
        elif key == "h" and total_pages > 1:
            page_index = (page_index - 1) % total_pages
            selected = page_index * page_size
            if selected >= total:
                selected = total - 1
            if not await print_full_page(page_index, selected, digit_buffer):
                return None

# --------------------- SHELL ---------------------

async def shell(reader, writer):
    global CONF
    if hasattr(writer, 'set_echo'):
        writer.set_echo(False)
    enc, article_width, page_size = await configure_terminal(writer, reader, CONF)
    if not writer or writer.is_closing():
        return

    if not CONF["CAPTCHA_DISABLED"]:
        try:
            writer.write("Captcha: Repeat the first spacecraft to land on another planet three times.\r\nAnswer: ")
            await writer.drain()
        except (BrokenPipeError, ConnectionError):
            return
        captcha_input = await read_line_custom(writer, reader)
        if captcha_input.lower().count("venera") != 3:
            try:
                writer.write("Access denied. Invalid response.\r\n")
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                pass
            return

    shell_mode = "wiki"

    try:
        while True:
            prompt = "Wiki> " if shell_mode == "wiki" else "AI> " if shell_mode == "ai" else "Guestbook> "
            if not writer or writer.is_closing():
                return
            try:
                writer.write(prompt)
                await writer.drain()
            except (BrokenPipeError, ConnectionError):
                return
            line = await read_line_custom(writer, reader)
            if line is None:
                return
            cmd = line.strip()
            if cmd == "":
                try:
                    if shell_mode == "wiki":
                        writer.write("(In Wiki mode, type text => search. :quit => exit)\r\n")
                        if CONF["AI_ACTIVATED"]:
                            writer.write("During article reading, press 'a' => AI assistant overlay w/ context.\r\n")
                        writer.write("Use 's', 'd', or 'f' for internal search, 't' for TOC, q=exit, w=superquit.\r\n")
                    elif shell_mode == "ai":
                        if CONF["AI_ACTIVATED"]:
                            writer.write("(In AI mode, type text => conversation. :quit => exit)\r\n")
                        else:
                            writer.write("AI is disabled.\r\n")
                    else:
                        writer.write("(In Guestbook mode, view/add entries. Enter=new, q=exit, w=superquit)\r\n")
                    await writer.drain()
                except (BrokenPipeError, ConnectionError):
                    print("broken pipe?")
                    return
                continue
            if cmd.startswith(":"):
                parts = cmd.split()
                c = parts[0].lower()
                if c == ":quit":
                    try:
                        writer.write("Goodbye!\r\n")
                        await writer.drain()
                    except (BrokenPipeError, ConnectionError):
                        pass
                    break
                elif c == ":ai":
                    if not CONF["AI_ACTIVATED"]:
                        try:
                            writer.write("[AI not available]\r\n")
                            await writer.drain()
                        except (BrokenPipeError, ConnectionError):
                            return
                    else:
                        shell_mode = "ai"
                        try:
                            writer.write("[Switched to AI mode]\r\n")
                            await writer.drain()
                        except (BrokenPipeError, ConnectionError):
                            return
                elif c == ":wiki":
                    shell_mode = "wiki"
                    try:
                        writer.write("[Switched to Wiki mode]\r\n")
                        await writer.drain()
                    except (BrokenPipeError, ConnectionError):
                        return
                elif c == ":guestbook":
                    if await show_guestbook(writer, reader, article_width, page_size):
                        continue
                    shell_mode = "wiki"
                elif c == ":help":
                    try:
                        if shell_mode == "wiki":
                            writer.write("(In Wiki mode, type text => search. :quit => exit)\r\n")
                            if CONF["AI_ACTIVATED"]:
                                writer.write("During article reading, press 'a' => AI assistant overlay w/ context.\r\n")
                            writer.write("Use 's', 'd', or 'f' for internal search, 't' for TOC, q=exit, w=superquit.\r\n")
                        elif shell_mode == "ai":
                            if CONF["AI_ACTIVATED"]:
                                writer.write("(In AI mode, type text => conversation. :quit => exit)\r\n")
                            else:
                                writer.write("AI is disabled.\r\n")
                        else:
                            writer.write("(In Guestbook mode, view/add entries. Enter=new, q=exit, w=superquit)\r\n")
                        await writer.drain()
                    except (BrokenPipeError, ConnectionError):
                        return
                else:
                    try:
                        writer.write("[Unknown command]\r\n")
                        await writer.drain()
                    except (BrokenPipeError, ConnectionError):
                        return
                continue
            if shell_mode == "wiki":
                if await top_level_wiki_search(CONF, writer, reader, cmd, article_width, page_size):
                    break
            elif shell_mode == "ai" and CONF["AI_ACTIVATED"]:
                user_id = "top-level-ai-user"
                if await show_ai_conversation_overlay(CONF, writer, reader, "", 0, user_id, article_width, page_size, is_top_level=True, initial_question=cmd, previous_mode=shell_mode):
                    shell_mode = "wiki"
                    continue
    finally:
        writer.close()

# --------------------- MAIN ---------------------

def main():
    global CONF
    CONF = load_config()
    if CONF["DEBUG"]:
        print("[DEBUG] Loaded config:", CONF)
    port = CONF["PORT"]
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    server = telnetlib3.create_server(port=port, shell=shell, encoding='utf8', timeout=600)
    try:
        loop.run_until_complete(server)
        print(f"Telnet server running on port {port}")
        loop.run_forever()
    except KeyboardInterrupt:
        print("Server shutting down.")
    except Exception as e:
        print(f"Server error: {e}")
    finally:
        for task in asyncio.all_tasks(loop):
            task.cancel()
        try:
            loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(loop), return_exceptions=True))
        except asyncio.CancelledError:
            pass
        server.close()
        loop.close()

if __name__ == '__main__':
    main()
