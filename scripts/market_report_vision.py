import argparse
import asyncio
import subprocess
import re
import requests
import json
import time
import urllib.parse
import concurrent.futures
import os
import base64
import threading
import tempfile
import image_generator
from collections import deque
from datetime import datetime, timedelta
from dotenv import load_dotenv
import contextvars

load_dotenv()

REPORT_ONLY = False
# Use ContextVar for thread-safe per-task debug directory
_debug_dir_var = contextvars.ContextVar('DEBUG_DIR', default=None)

def _get_debug_dir():
    return _debug_dir_var.get()

def _set_debug_dir(path):
    _debug_dir_var.set(path)

_notify_msgs_var = contextvars.ContextVar('NOTIFY_MSGS', default=None)

def _push_notify(msg):
    lst = _notify_msgs_var.get()
    if lst is not None:
        lst.append(msg)

def get_and_clear_notify_msgs():
    lst = _notify_msgs_var.get()
    if lst:
        msgs = list(lst)
        lst.clear()
        return msgs
    return []

def _debug_save(filename, content):
    """Debug 輔助函數：將內容存入 DEBUG_DIR/filename（若 DEBUG_DIR 已設定）"""
    debug_dir = _get_debug_dir()
    if not debug_dir:
        return
    os.makedirs(debug_dir, exist_ok=True)
    filepath = os.path.join(debug_dir, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    _original_print(f"  💾 [DEBUG] 存檔: {filepath}")

def _debug_log(msg):
    """Debug log 輔助函數：將訊息 append 到 DEBUG_DIR/debug_log.txt"""
    debug_dir = _get_debug_dir()
    if not debug_dir:
        return
    os.makedirs(debug_dir, exist_ok=True)
    timestamp = time.strftime('%H:%M:%S')
    line = f"[{timestamp}] {msg}\n"
    _original_print(f"  📍 [DEBUG] {msg}")
    with open(os.path.join(debug_dir, 'debug_log.txt'), 'a', encoding='utf-8') as f:
        f.write(line)

def _debug_step(source: str, step_num: int, query: str, url: str,
                status: str, candidate_urls: list = None,
                selected_url: str = None, reason: str = "",
                extra: dict = None):
    """
    結構化 Debug Trace — 每次搜尋動作都記錄一筆 JSON 到 debug_trace.jsonl
    """
    debug_dir = _get_debug_dir()
    if not debug_dir:
        return
    os.makedirs(debug_dir, exist_ok=True)
    record = {
        "time": time.strftime('%H:%M:%S'),
        "source": source,
        "step": step_num,
        "query": query,
        "url": url,
        "status": status,
        "candidate_urls": candidate_urls or [],
        "selected_url": selected_url or "",
        "reason": reason,
    }
    if extra:
        record.update(extra)
    # 即時 print 到 terminal
    icon = "✅" if status == "OK" else "❌"
    _original_print(f"  {icon} [{source} Step {step_num}] query={query!r}")
    _original_print(f"       URL  : {url}")
    _original_print(f"       狀態 : {status}  —  {reason}")
    if candidate_urls:
        _original_print(f"       候選 URLs ({len(candidate_urls)} 筆):")
        for u in candidate_urls:
            _original_print(f"         • {u}")
    if selected_url:
        _original_print(f"       選定 URL : {selected_url}")
    # append 到 JSONL
    with open(os.path.join(debug_dir, 'debug_trace.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')

_original_print = print
def print(*args, **kwargs):
    if REPORT_ONLY and not kwargs.get('force', False):
        return
    if 'force' in kwargs:
        del kwargs['force']
    _original_print(*args, **kwargs)

_jina_requests_queue = deque()
_jina_lock = threading.Lock()

def fetch_jina_markdown(target_url):
    global _jina_requests_queue
    
    # Rate Limiter: 18 requests per 60 seconds (1 minute)
    MAX_REQUESTS = 18
    WINDOW_SIZE = 60.0
    
    sleep_time = 0
    with _jina_lock:
        now = time.time()
        # Remove requests older than 60 seconds
        while _jina_requests_queue and now - _jina_requests_queue[0] > WINDOW_SIZE:
            _jina_requests_queue.popleft()
            
        if len(_jina_requests_queue) >= MAX_REQUESTS:
            # Calculate sleep time required to let the oldest request expire
            sleep_time = WINDOW_SIZE - (now - _jina_requests_queue[0])
            
    if sleep_time > 0:
        print(f"⏳ Jina API rate limit approaching ({MAX_REQUESTS}/min). Pausing for {sleep_time:.1f} seconds to cool down...")
        time.sleep(sleep_time)
        
    with _jina_lock:
        now = time.time()
        # Clean up again
        while _jina_requests_queue and now - _jina_requests_queue[0] > WINDOW_SIZE:
            _jina_requests_queue.popleft()
        _jina_requests_queue.append(now)

    print(f"Fetching: {target_url}...")
    jina_url = f"https://r.jina.ai/{target_url}"
    
    for attempt in range(3):
        try:
            response = requests.get(jina_url, timeout=60)
            if response.status_code == 429:
                print(f"⚠️ Jina 發生 429 頻率限制 (嘗試 {attempt+1}/3). 暫停 1 秒後重試...")
                time.sleep(1)
                continue
                
            response.raise_for_status()
            return response.text
            
        except requests.exceptions.RequestException as e:
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
                print(f"⚠️ Jina 發生 429 頻率限制 (嘗試 {attempt+1}/3). 暫停 1 秒後重試...")
                time.sleep(1)
                continue
                
            print(f"Fetch error for {target_url}: {e}")
            return ""
            
    return ""

# v1.1 變更註解:
# 1) SNKRDUNK 搜尋從 Jina HTML 解析改為原生 API (/en/v1/search)。
# 2) 成交歷史從 sales-histories 頁面解析改為原生 API (/en/v1/streetwears/{id}/trading-histories)。
# 3) 新增 session warmup + retry，降低 terminal 直接呼叫 API 時的 403 機率。
# 4) 維持既有搜尋決策流程: 先候選搜尋 -> 編號/Variant/語言過濾 -> 再抓價格。
# 5) SNKRDUNK API 價格來源為 USD 時，先轉回 JPY，維持舊報表顯示格式。
def _create_snkr_api_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://snkrdunk.com/",
        "Origin": "https://snkrdunk.com",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    })
    # Warm up cookies to reduce 403 on direct API endpoints.
    try:
        session.get("https://snkrdunk.com/", timeout=20)
    except Exception as e:
        _debug_log(f"SNKRDUNK API warmup failed (will continue): {e}")
    return session

def _snkr_api_get_json(session, url, retries=3):
    last_error = None
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code == 403:
                # Re-warm homepage cookies and retry.
                session.get("https://snkrdunk.com/", timeout=20)
                time.sleep(0.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_error = e
            time.sleep(0.5 * (attempt + 1))
    _debug_log(f"SNKRDUNK API request failed after retries: {url} | err={last_error}")
    return {}

def _snkr_history_to_jpy(history, jpy_rate):
    price = history.get("price", 0)
    price_fmt = str(history.get("priceFormat", ""))
    try:
        p = float(price)
    except Exception:
        return 0

    if p <= 0:
        return 0

    fmt_upper = price_fmt.upper()
    if "¥" in price_fmt or "JPY" in fmt_upper:
        return int(round(p))
    if "$" in price_fmt or "USD" in fmt_upper:
        return int(round(p * jpy_rate))

    # Fallback heuristic when currency symbol is missing.
    if p >= 1000:
        return int(round(p))
    return int(round(p * jpy_rate))

def _snkr_traded_date(traded_at):
    if not traded_at:
        return ""
    if "T" in traded_at:
        traded_at = traded_at.split("T", 1)[0]
    return traded_at.replace("-", "/")

def get_exchange_rate():
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD")
        data = resp.json()
        return data['rates']['JPY']
    except:
        return 150.0

def _fetch_pc_prices_from_url(product_url, md_content=None, skip_hi_res=False, target_grade="PSA 10"):
    """
    Given a PriceCharting product URL, fetch (if md_content is None) and parse it.
    Returns (records, resolved_url, pc_img_url).
    """
    if not md_content:
        md_content = fetch_jina_markdown(product_url)
    
    if not md_content:
        print(f"DEBUG: Failed to get markdown for {product_url}")
        return [], product_url, None

    print(f"DEBUG: Parsing PriceCharting page: {product_url} (length: {len(md_content)})")

    lines = md_content.split('\n')
    records = []
    
    # Parser 1: 嘗試原本的 Markdown Table 格式 (每行有 | 分隔)
    date_regex_md = r'\|\s*(\d{4}-\d{2}-\d{2}|[A-Z][a-z]{2}\s\d{1,2},\s\d{4})\s*\|'
    for line in lines:
        if re.search(date_regex_md, line):
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 5:
                date_str = parts[1]
                all_prices = re.findall(r'\$([\d,]+\.\d{2})', line)
                if not all_prices: continue
                real_prices = [p for p in all_prices if p not in ('6.00',)]
                if not real_prices: continue
                
                price_usd = float(real_prices[-1].replace(',', ''))
                title_clean = line.replace(" ", "").lower()
                
                detected_grade = None
                if re.search(r'(psa|cgc|bgs|grade|gem)10', title_clean) or ("psa" in title_clean and "10" in title_clean):
                    detected_grade = "PSA 10"
                elif re.search(r'bgs\s*9\.5', title_clean):
                    detected_grade = "BGS 9.5"
                elif re.search(r'(psa|cgc|bgs|grade|gem)9', title_clean) or ("psa" in title_clean and "9" in title_clean):
                    detected_grade = "PSA 9"
                elif re.search(r'(psa|cgc|bgs|grade|gem)8', title_clean) or ("psa" in title_clean and "8" in title_clean):
                    detected_grade = "PSA 8"
                elif not re.search(r'(psa|bgs|cgc|grade|gem)', title_clean):
                    detected_grade = "Ungraded"
                        
                if detected_grade:
                    records.append({
                        "date": date_str,
                        "price": price_usd,
                        "grade": detected_grade
                    })

    # Parser 2: 嘗試 Jina 新版的 TSV 格式 (日期獨立一行，標題與價格在下一行)
    if not records: 
        current_date = None
        date_regex_tsv = r'^(\d{4}-\d{2}-\d.2}|[A-Z][a-z]{2}\s\d{1,2},\s\d{4})'
        for line in lines:
            line = line.strip()
            date_match = re.match(date_regex_tsv, line)
            if date_match:
                current_date = date_match.group(1)
                continue
            if current_date and "$" in line:
                all_prices = re.findall(r'\$([\d,]+\.\d{2})', line)
                if not all_prices: continue
                real_prices = [p for p in all_prices if p not in ('6.00',)]
                if not real_prices: continue
                price_usd = float(real_prices[-1].replace(',', ''))
                title_clean = line.replace(" ", "").lower()
                detected_grade = None
                if re.search(r'(psa|cgc|bgs|grade|gem)10', title_clean) or ("psa" in title_clean and "10" in title_clean):
                    detected_grade = "PSA 10"
                elif re.search(r'bgs\s*9\.5', title_clean):
                    detected_grade = "BGS 9.5"
                elif re.search(r'(psa|cgc|bgs|grade|gem)9', title_clean) or ("psa" in title_clean and "9" in title_clean):
                    detected_grade = "PSA 9"
                elif not re.search(r'(psa|bgs|cgc|grade|gem)', title_clean):
                    detected_grade = "Ungraded"
                if detected_grade:
                    records.append({
                        "date": current_date,
                        "price": price_usd,
                        "grade": detected_grade
                    })

    # Summary: if no per-item records, try summary table
    today_str = datetime.now().strftime('%Y-%m-%d')
    grade_summary_map = {'Ungraded': 'Ungraded', 'PSA 10': 'PSA 10', 'PSA 9': 'PSA 9', 'BGS 9.5': 'BGS 9.5'}
    existing_grades = set(r['grade'] for r in records)
    for line in lines:
        for grade_label, grade_key in grade_summary_map.items():
            label_nospace = grade_label.replace(' ', '')
            if re.match(rf'^{re.escape(label_nospace)}\$[\d,]+\.\d{{2}}$', line.replace(' ', '')):
                if grade_key not in existing_grades:
                    price_match = re.search(r'\$[\d,]+\.\d{2}', line)
                    if price_match:
                        price_usd = extract_price(price_match.group(0))
                        records.append({"date": today_str, "price": price_usd, "grade": grade_key, "note": "PC avg price"})

    records.sort(key=lambda x: x['date'], reverse=True)
    
    pc_img_url = None
    img_patterns = [
        r'!\[.*?\]\((https://storage\.googleapis\.com/images\.pricecharting\.com/[^/)]+/\d+\.jpg)\)',
        r'!\[.*?\]\((https://product-images\.s3\.amazonaws\.com/[^\)]+)\)',
        r'!\[.*?\]\((https://images\.pricecharting\.com/[^\)]+)\)',
        r'!\[.*?\]\((https://[^)]+?pricecharting\.com/[^)]+?\.(?:jpg|png|webp)[^)]*)\)',
        r'!\[.*?\]\((https://[^)]+?\.(?:jpg|png|webp)[^)]*)\)',
    ]
    for pat in img_patterns:
        m = re.search(pat, md_content)
        if m:
            pc_img_url = m.group(1)
            if not skip_hi_res:
                hiRes_url = re.sub(r'/([\d]+)\.jpg$', '/1600.jpg', pc_img_url)
                if hiRes_url != pc_img_url:
                    try:
                        if requests.head(hiRes_url, timeout=5).status_code == 200:
                            pc_img_url = hiRes_url
                    except: pass
            break

    _debug_log(f"PriceCharting: 成功提取 {len(records)} 筆價格紀錄 (包含全等級)")
    
    snkr_target = target_grade.replace(" ", "")
    matched_records = []
    for r in records:
        r_grade = r.get('grade', '')
        if r_grade == target_grade: matched_records.append(r)
        elif target_grade == "Unknown" and r_grade in ("Ungraded", "裸卡", "A"): matched_records.append(r)
        elif r_grade == snkr_target: matched_records.append(r)
        
    _debug_log(f"PriceCharting: 其中符合 '{target_grade}' 的紀錄有 {len(matched_records)} 筆")
    for r in matched_records[:5]:
        _debug_log(f"  - [{r.get('date', '')}] {r.get('grade', '')} : ${r.get('price', 0)}")
    if len(matched_records) > 5:
        _debug_log(f"  ... (還有 {len(matched_records) - 5} 筆不顯示)")

    return records, product_url, pc_img_url

def extract_price(price_str):
    cleaned = re.sub(r'[^\d.]', '', price_str)
    try:
        return float(cleaned)
    except:
        return 0.0


def _normalize_alnum_dash(text):
    return re.sub(r'[^a-z0-9]+', '-', str(text).lower()).strip('-')


def _contains_token_boundary(text_norm, token):
    tok = _normalize_alnum_dash(token)
    if not tok:
        return False
    return re.search(rf'(?<![a-z0-9]){re.escape(tok)}(?![a-z0-9])', text_norm) is not None


def _extract_number_denominator(number_text):
    parts = str(number_text or "").split("/", 1)
    if len(parts) < 2:
        return ""
    m = re.search(r'\d+', parts[1])
    return m.group(0) if m else ""


def _title_number_match(title_text, number_clean, number_padded):
    """
    Match card number by numerator-first logic.
    - Prefer fraction numerator match (e.g. target 018 should match 018/072, not 010/018)
    - Fallback to standalone number token only after removing x/y fractions
    Returns: (matched, numerator_hit, denominator_hit, reason)
    """
    if not number_clean or number_clean == "0":
        return True, "", "", "no_number_constraint"

    text = str(title_text or "").lower()
    fractions = re.findall(r'(\d{1,4})\s*/\s*(\d{1,4})', text)
    for n_raw, d_raw in fractions:
        n_norm = n_raw.lstrip('0') or '0'
        d_norm = d_raw.lstrip('0') or '0'
        if n_norm == number_clean:
            return True, n_norm, d_norm, "fraction_numerator"

    text_wo_frac = re.sub(r'\d{1,4}\s*/\s*\d{1,4}', ' ', text)
    if number_padded and re.search(rf'(?<!\d){re.escape(number_padded)}(?!\d)', text_wo_frac):
        return True, number_clean, "", "standalone_padded"
    if number_clean and re.search(rf'(?<!\d){re.escape(number_clean)}(?!\d)', text_wo_frac):
        return True, number_clean, "", "standalone_clean"
    return False, "", "", ""


def _score_pricecharting_candidate(
    url,
    *,
    name_slug,
    name_slug_alt,
    number_clean,
    number_padded,
    number_denominator,
    set_code_slug,
    mega_name_hint=False,
):
    slug = url.split('/')[-1].lower()
    slug_norm = _normalize_alnum_dash(slug)
    slug_compact = re.sub(r'[^a-z0-9]', '', slug)
    score = 0
    reasons = []

    # Priority #1: exact number is mandatory signal.
    if number_clean and re.search(rf'(?<!\d){re.escape(number_clean)}(?!\d)', slug):
        score += 150
        reasons.append("number_exact")
    elif number_padded and number_padded in slug:
        score += 140
        reasons.append("number_padded")
    elif number_clean and number_clean != "0":
        score -= 160
        reasons.append("number_missing_penalty")

    # Priority #2: set code exact (secondary to number)
    if set_code_slug and set_code_slug in slug_compact:
        score += 65
        reasons.append("set_code")

    # Priority #3: precise name matching (token boundary)
    if name_slug and _contains_token_boundary(slug_norm, name_slug):
        score += 85
        reasons.append("name_exact")
    elif name_slug_alt and _contains_token_boundary(slug_norm, name_slug_alt):
        score += 70
        reasons.append("name_alt_exact")
    else:
        tokens = [t for t in (name_slug or "").split('-') if t and len(t) >= 2]
        if tokens:
            token_hits = sum(1 for t in tokens if _contains_token_boundary(slug_norm, t))
            if token_hits == len(tokens):
                score += 55
                reasons.append("name_tokens_all")
            elif token_hits > 0:
                score += 20
                reasons.append("name_tokens_partial")

    # Extra bonus: denominator/full-form hints to avoid same-number wrong card.
    if number_denominator:
        den_trim = number_denominator.lstrip('0') or number_denominator
        if f"-{number_denominator}" in slug or f"/{number_denominator}" in slug:
            score += 35
            reasons.append("denominator_exact")
        elif f"-{den_trim}" in slug or f"/{den_trim}" in slug:
            score += 28
            reasons.append("denominator_trim")

    # Pokemon-only hint: if features says this is a Mega evolution card,
    # candidates explicitly containing mega/m naming get a ranking boost.
    if mega_name_hint and (
        _contains_token_boundary(slug_norm, "mega")
        or re.search(r'(^|-)m-(?=[a-z0-9])', slug_norm)
    ):
        score += 60
        reasons.append("mega_name_hint")

    return score, reasons

def filter_pricecharting_candidates(candidates):
    """Normalize and dedupe PriceCharting candidate strings."""
    seen = set()
    filtered = []
    for c in candidates or []:
        if not c:
            continue
        url = str(c).split(" — ", 1)[0].strip()
        if not url.startswith("https://www.pricecharting.com/game/"):
            continue
        if url in seen:
            continue
        seen.add(url)
        filtered.append(c)
    return filtered

def search_pricecharting(name, number, set_code, target_grade, is_alt_art, category="Pokemon", is_flagship=False, return_candidates=False, set_name="", jp_name="", mega_name_hint=False):
    # Basic Name cleaning (strip parentheses and normalize hyphens to spaces)
    name_query = re.sub(r'\(.*?\)', '', name).replace('-', ' ').strip()
    
    # Improve number extraction for One Piece (ST04-005 -> 005)
    # If the number contains a dash and follows OP format, take the part after the dash
    if '-' in number and re.search(r'[A-Z]+\d+-\d+', number):
        number_clean = number.split('-')[-1].lstrip('0')
    else:
        _num_parts = number.split('/')
        _num_raw = _num_parts[0].strip()
        _digits_only = re.search(r'\d+', _num_raw)
        number_clean = _digits_only.group(0).lstrip('0') if _digits_only else _num_raw.lstrip('0')
    
    if not number_clean: number_clean = '0'
    
    # Try to extract suffix like SM-P from the number itself if it's there
    suffix = ""
    number_denominator = _extract_number_denominator(number)
    _num_parts = number.split('/')
    if len(_num_parts) > 1:
        potential_suffix = _num_parts[1].strip()
        if re.search(r'(SM-P|S-P|SV-P|SV-G|S8a-G)', potential_suffix, re.IGNORECASE):
            suffix = potential_suffix
    
    # Try with set code or suffix first
    queries_to_try = []
    final_set_code = set_code if set_code else suffix
    
    # 1. 精確搜尋 (優先)：[卡名] [Set Code] [編號]
    if final_set_code and number_clean != '0':
        queries_to_try.append(f"{name_query} {final_set_code} {number_clean}".replace(" ", "+"))

    # 2. 廣泛搜尋：[卡名] [編號]
    if number_clean != '0':
        queries_to_try.append(f"{name_query} {number_clean}".replace(" ", "+"))

    # 3. 系列備援：[卡名] [系列全名] [編號] (僅在沒找到時，且名稱不包含系列名時嘗試)
    if set_name and number_clean != '0':
        _sn_clean = set_name.lower().strip()
        if _sn_clean not in name_query.lower():
            queries_to_try.append(f"{name_query} {set_name} {number_clean}".replace(" ", "+"))

    # 4. 基本搜尋：[卡名] [Set Code]
    if final_set_code:
        queries_to_try.append(f"{name_query} {final_set_code}".replace(" ", "+"))

    is_one_piece = category.lower() == "one piece"
    _debug_log(f"PriceCharting: 類別={category} ({'航海王模式' if is_one_piece else '寶可夢模式'})，共 {len(queries_to_try)} 種查詢方案: {queries_to_try}")

    md_content = ""
    search_url = ""
    pc_step = 0

    for query in queries_to_try:
        pc_step += 1
        search_url = f"https://www.pricecharting.com/search-products?q={query}&type=prices"
        _debug_log(f"PriceCharting Step {pc_step}: 查詢={query!r}  URL={search_url}")
        md_content = fetch_jina_markdown(search_url)
        if md_content and ("Search Results" in md_content or "Your search for" in md_content):
            _debug_step("PriceCharting", pc_step, query, search_url,
                        "OK", reason="搜尋頁面有多筆結果，繼續解析")
            break
        elif md_content and "PriceCharting" in md_content:
            _debug_step("PriceCharting", pc_step, query, search_url,
                        "OK", reason="直接落在商品頁面")
            break
        else:
            _debug_step("PriceCharting", pc_step, query, search_url,
                        "NO_RESULTS", reason="頁面為空或無法識別，嘗試下一個查詢")
            
    if not md_content:
        _debug_step("PriceCharting", pc_step, "", "",
                    "ERROR", reason="所有查詢均無回應，放棄")
        return None, None, None
    
    product_url = ""
    if "Your search for" in md_content or "Search Results" in md_content:
        urls = re.findall(r'(https://www\.pricecharting\.com/game/[^/]+/[^" )\]]+)', md_content)
        # Deduplicate while preserving order
        urls = list(dict.fromkeys(urls))
        
        _debug_log(f"PriceCharting: 從搜尋頁面提取到 {len(urls)} 個候選 URL")
        
        valid_urls = []
        # 「名稱 slug」用純角色名（去掉括號內的版本描述，如 Leader Parallel / SP Foil 等）
        name_for_slug = re.sub(r'\(.*?\)', '', name).strip()
        name_slug = re.sub(r'[^a-zA-Z0-9]', '-', name_for_slug.lower()).strip('-')
        
        # --- Mega / M 別名處理 ---
        name_slug_alt = ""
        if name_slug.startswith("m-") and len(name_slug) > 2:
            name_slug_alt = "mega-" + name_slug[2:]
        elif name_slug.startswith("mega-") and len(name_slug) > 5:
            name_slug_alt = "m-" + name_slug[5:]
        
        # 編號的 0-padded 3位形式，修復 URL slug 內 026 不能被 26 regex 匹配的問題
        number_padded_pc = number_clean.zfill(3)
        # 航海王模式：set_code slug 用來做額外驗證 (e.g. "OP02" -> "op02")
        set_code_slug = re.sub(r'[^a-zA-Z0-9]', '', set_code).lower() if set_code else ""

        def _num_match(slug):
            """編號匹配：接受去前導0 或 3位補齊兩種形式"""
            return (bool(re.search(rf'(?<!\d){number_clean}(?!\d)', slug))
                    or number_padded_pc in slug)

        def _set_match(slug):
            """set_code 匹配：URL slug 含有 set_code 的核心字母數字部分"""
            return bool(set_code_slug) and set_code_slug in slug.replace('-', '')
            
        def _name_match(slug):
            """名稱匹配：考慮 name_slug 及其 mega/m 別名"""
            if not name_slug:
                return False
            if name_slug in slug:
                return True
            if name_slug_alt and name_slug_alt in slug:
                return True
            return False

        matching_both = []   # 名稱 + 編號 (+ set_code for OP)
        matching_name = []   # 只有名稱 (+ set_code for OP)
        matching_number = [] # 只有編號 (+ set_code for OP)

        for u in urls:
            u_end = u.split('/')[-1].lower()

            if is_one_piece:
                # ── 航海王模式：必須包含 set_code，再依名稱/編號分級 ──
                has_set = _set_match(u_end)
                has_num = _num_match(u_end)
                has_name = _name_match(u_end)

                if has_name and has_num and has_set:
                    matching_both.append(u)
                    _debug_log(f"  ✅ [OP] 名稱+編號+setcode: {u}")
                elif has_name and has_set:
                    matching_name.append(u)
                    _debug_log(f"  🔶 [OP] 名稱+setcode (無編號): {u}")
                elif has_num and has_set:
                    matching_number.append(u)
                    _debug_log(f"  🔷 [OP] 編號+setcode (無名稱): {u}")
                elif has_name and has_num:
                    matching_both.append(u)
                    _debug_log(f"  🟡 [OP] 名稱+編號 (setcode未命中): {u}")
                else:
                    _debug_log(f"  ❌ [OP] URL 不符合: {u}")
            else:
                has_name = _name_match(u_end)
                has_num = _num_match(u_end)
                
                if has_name and has_num:
                    matching_both.append(u)
                    _debug_log(f"  ✅ [PKM] 名稱+編號: {u}")
                elif has_name:
                    matching_name.append(u)
                    _debug_log(f"  🔶 [PKM] 只符合名稱: {u}")
                elif has_num:
                    matching_number.append(u)
                    _debug_log(f"  🔷 [PKM] 只符合編號 '{number_clean}'/'{number_padded_pc}': {u}")
                else:
                    _debug_log(f"  ❌ [PKM] URL 不符合: {u}")

        # 合併：先確保至少匹配，再進入分數排序
        valid_urls = matching_both + matching_name + matching_number
                
        if not valid_urls:
            _debug_step("PriceCharting", pc_step + 1,
                        f"name_slug={name_slug!r}, number={number_clean!r}",
                        search_url, "NO_MATCH",
                        candidate_urls=urls,
                        reason=f"所有 {len(urls)} 個候選 URL 均不符合卡片名稱或編號，放棄")
            print(f"DEBUG: No PC product URL matched the card name '{name}' or number '{number_clean}'.")
            return None, None, None

        # Score ranking: set_code > exact name > exact number > denominator hints.
        scored_urls = []
        for u in valid_urls:
            sc, why = _score_pricecharting_candidate(
                u,
                name_slug=name_slug,
                name_slug_alt=name_slug_alt,
                number_clean=number_clean,
                number_padded=number_padded_pc,
                number_denominator=number_denominator,
                set_code_slug=set_code_slug,
                mega_name_hint=mega_name_hint,
            )
            scored_urls.append((u, sc, why))
        scored_urls.sort(key=lambda x: x[1], reverse=True)
        ranked_urls = [u for u, _, _ in scored_urls]

        if return_candidates:
            return ranked_urls, None, None

        product_url = ranked_urls[0]
        top_score = scored_urls[0][1]
        top_why = ",".join(scored_urls[0][2]) if scored_urls[0][2] else "fallback"
        selection_reason = f"Scored Best ({top_score}): {top_why}"
        _debug_log(f"PriceCharting ranking top3: {[(u, s) for u, s, _ in scored_urls[:3]]}")
        
        # Filter based on is_flagship / is_alt_art (features-based override 主導)
        if is_flagship:
            # 旗艦賽獎品卡：尋找包含 flagship 的 URL
            for u in ranked_urls:
                lower_u = u.replace('[', '').replace(']', '').lower()
                if "flagship" in lower_u:
                    product_url = u
                    selection_reason = "Flagship Filter (偵測到 Flagship Battle 關鍵字)"
                    break
        elif is_alt_art:
            for u in ranked_urls:
                lower_u = u.replace('[', '').replace(']', '').lower()
                # 航海王異圖版優先尋找包含這些關鍵字的
                if "manga" in lower_u or "alternate-art" in lower_u or "-sp" in lower_u:
                    product_url = u
                    selection_reason = "Alt-Art Filter (偵測到 Manga/Alternate-Art/SP 關鍵字)"
                    break
        
        _debug_step("PriceCharting", pc_step + 1,
                    f"is_alt_art={is_alt_art}, name_slug={name_slug!r}, number={number_clean!r}",
                    search_url, "OK",
                    candidate_urls=urls,
                    selected_url=product_url,
                    reason=selection_reason,
                    extra={"matching_both": matching_both,
                           "matching_name": matching_name,
                           "matching_number": matching_number,
                           "scored_top3": [(u, s) for u, s, _ in scored_urls[:3]]})
        print(f"DEBUG: Selected PC product URL: {product_url} ({selection_reason})")
        records, resolved_url, pc_img_url = _fetch_pc_prices_from_url(product_url, target_grade=target_grade)
    else:
        print(f"DEBUG: Landed directly on PC product page")
        product_url = search_url
        _debug_step("PriceCharting", pc_step + 1, "", product_url,
                    "OK", reason="直接落在商品頁面，跳過 URL 篩選")
                    
        if return_candidates:
            # If the main app expects candidate URLs, wrap the direct match as a candidate
            return filter_pricecharting_candidates([f"{product_url} — {name}"]), None, None
            
        records, resolved_url, pc_img_url = _fetch_pc_prices_from_url(product_url, md_content=md_content, target_grade=target_grade)
    
    return records, resolved_url, pc_img_url

def search_snkrdunk(en_name, jp_name, number, set_code, target_grade, is_alt_art=False, card_language="UNKNOWN", snkr_variant_kws=None, return_candidates=False, set_name=""):
    # Strip prefix like "No." (e.g. "No.025" -> "25"), then apply lstrip('0')
    if '-' in number and re.search(r'[A-Z]+\d+-\d+', number):
        number_clean = number.split('-')[-1].lstrip('0')
    else:
        _num_raw = number.split('/')[0]
        _digits_only = re.search(r'\d+', _num_raw)
        number_clean = _digits_only.group(0).lstrip('0') if _digits_only else _num_raw.lstrip('0')
    
    if not number_clean: number_clean = '0'
    number_padded = number_clean.zfill(3)
    number_denominator = _extract_number_denominator(number)
    set_code_slug = re.sub(r'[^a-zA-Z0-9]', '', set_code).lower() if set_code else ""

    # Normalize hyphens to spaces in names for better search matching (e.g. Ex-Holo -> Ex Holo)
    en_name_query = re.sub(r'\(.*?\)', '', en_name).replace('-', ' ').strip()
    jp_name_query = re.sub(r'\(.*?\)', '', jp_name).replace('-', ' ').strip() if jp_name else ""

    terms_to_try = []
    
    # [NEW] 優化搜尋順序：如果有 Set Code，優先使用精確組合，否則才用廣泛搜尋
    if set_code and number_padded != "000":
        if jp_name_query:
            terms_to_try.append(f"{jp_name_query} {set_code} {number_padded}")
        terms_to_try.append(f"{en_name_query} {set_code} {number_padded}")

    if number_padded != "000":
        if jp_name_query:
            terms_to_try.append(f"{jp_name_query} {number_padded}")
        terms_to_try.append(f"{en_name_query} {number_padded}")

    # SNKRDUNK search is highly accurate with Set Code (e.g. "ピカチュウ S8a-G", "ピカチュウ SV-P")
    if set_code:
        if jp_name_query:
            terms_to_try.append(f"{jp_name_query} {set_code}")
        terms_to_try.append(f"{en_name_query} {set_code}")
            
    # Fallback to just name if no number or set_code combinations yielded results
    if not terms_to_try:
        if jp_name_query:
            terms_to_try.append(jp_name_query)
        terms_to_try.append(en_name_query)
    
    _debug_log(f"SNKRDUNK: 共 {len(terms_to_try)} 種查詢方案: {terms_to_try}")

    product_id = None
    img_url = ""
    snkr_step = 0
    snkr_session = _create_snkr_api_session()

    for term in terms_to_try:
        snkr_step += 1
        q = urllib.parse.quote_plus(term)
        search_url = f"https://snkrdunk.com/en/v1/search?keyword={q}&perPage=40&page=1"
        _debug_log(f"SNKRDUNK Step {snkr_step}: 查詢={term!r}  URL={search_url}")
        data = _snkr_api_get_json(snkr_session, search_url)

        items = []
        for key in ("streetwears", "products"):
            arr = data.get(key, [])
            if isinstance(arr, list):
                items.extend(arr)

        _debug_log(f"SNKRDUNK Step {snkr_step}: API 原始匹配 {len(items)} 筆")

        seen = set()
        unique_matches = []
        for item in items:
            pid = str(item.get("id", "")).strip()
            if not pid:
                continue
            title = str(item.get("name", "")).strip()
            if not title:
                continue
            # Keep only trading cards when the flag exists.
            if item.get("isTradingCard") is False:
                continue
            thumb = item.get("thumbnailUrl") or item.get("imageUrl") or item.get("image") or ""
            if pid not in seen:
                seen.add(pid)
                unique_matches.append((title, pid, thumb))

        if not unique_matches:
            _debug_step("SNKRDUNK", snkr_step, term, search_url,
                        "NO_RESULTS", reason="搜尋頁面找不到任何商品連結，嘗試下一個查詢")
            time.sleep(1)
            continue
                
        filtered_by_number = []
        skipped = []
        for title, pid, thumb in unique_matches:
            # Drop Jina image prefixes
            title_clean = re.sub(r'(?i)image\s*\d+:\s*', '', title).lower()
            # Drop all https CDN links to prevent their timestamp digits from matching the card number
            title_clean = re.sub(r'https?://[^\s()\]]+', '', title_clean)

            is_num_match, n_hit, d_hit, n_reason = _title_number_match(title_clean, number_clean, number_padded)
            if is_num_match:
                filtered_by_number.append((title, pid, thumb))
                if n_reason == "fraction_numerator":
                    _debug_log(f"  ✅ 符合分子編號 '{number_padded}' ({n_hit}/{d_hit}): [{pid}] {title}")
                else:
                    _debug_log(f"  ✅ 符合編號 '{number_padded}' ({n_reason}): [{pid}] {title}")
            else:
                skipped.append((title, pid, thumb))
                _debug_log(f"  ❌ 不含編號 '{number_padded}': [{pid}] {title}")
                
        if not filtered_by_number:
            _debug_step("SNKRDUNK", snkr_step, term, search_url,
                        "NO_MATCH",
                        candidate_urls=[f"https://snkrdunk.com/apparels/{pid} — {t}" for t, pid, _ in unique_matches],
                        reason=f"找到 {len(unique_matches)} 筆商品但均不含卡片編號 '{number_padded}'，嘗試下一個查詢")
            time.sleep(1)
            continue # If no titles specifically have the card number, do not guess
            
        unique_matches = filtered_by_number

        if unique_matches:
            # Ranking: set_code > exact name > exact number > denominator.
            ranked_matches = []
            en_name_norm = re.sub(r'\(.*?\)', '', en_name).strip().lower()
            jp_name_norm = re.sub(r'\(.*?\)', '', jp_name).strip().lower() if jp_name else ""

            for title, pid, thumb in unique_matches:
                title_l = str(title).lower()
                title_norm = _normalize_alnum_dash(title_l)
                title_compact = re.sub(r'[^a-z0-9]', '', title_l)

                score = 0
                reasons = []

                if set_code_slug and set_code_slug in title_compact:
                    score += 140
                    reasons.append("set_code")

                # Japanese name exact string gets highest name confidence.
                if jp_name_norm and jp_name_norm in title_l:
                    score += 90
                    reasons.append("jp_name_exact")

                # English name with token boundaries avoids Mew->Mewtwo false hit.
                if en_name_norm:
                    if _contains_token_boundary(title_norm, en_name_norm):
                        score += 85
                        reasons.append("en_name_exact")
                    else:
                        en_tokens = [t for t in _normalize_alnum_dash(en_name_norm).split('-') if t and len(t) >= 2]
                        if en_tokens:
                            token_hits = sum(1 for t in en_tokens if _contains_token_boundary(title_norm, t))
                            if token_hits == len(en_tokens):
                                score += 60
                                reasons.append("en_tokens_all")
                            elif token_hits > 0:
                                score += 18
                                reasons.append("en_tokens_partial")
                        if en_name_norm in title_l and not _contains_token_boundary(title_norm, en_name_norm):
                            score -= 35
                            reasons.append("en_substring_penalty")

                num_match, n_hit, d_hit, n_reason = _title_number_match(title_l, number_clean, number_padded)
                if num_match:
                    if n_reason == "fraction_numerator":
                        score += 52
                        reasons.append("number_fraction_numerator")
                    elif n_reason == "standalone_padded":
                        score += 45
                        reasons.append("number_standalone_padded")
                    elif n_reason == "standalone_clean":
                        score += 40
                        reasons.append("number_standalone_clean")

                if number_denominator:
                    den_trim = number_denominator.lstrip('0') or number_denominator
                    if d_hit:
                        if d_hit == den_trim:
                            score += 35
                            reasons.append("denominator_exact")
                        else:
                            score -= 55
                            reasons.append("denominator_mismatch_penalty")

                ranked_matches.append((title, pid, thumb, score, reasons))

            ranked_matches.sort(key=lambda x: x[3], reverse=True)
            unique_matches = [(t, p, i) for t, p, i, _, _ in ranked_matches]
            score_by_pid = {p: s for _, p, _, s, _ in ranked_matches}
            _debug_log(f"SNKRDUNK ranking top3: {[(t, p, s) for t, p, _, s, _ in ranked_matches[:3]]}")

            if return_candidates:
                # 只回傳 URL 列表 (加上標題方便 bot 顯示列表)
                return [f"https://snkrdunk.com/apparels/{pid} — {title}" for title, pid, _ in unique_matches], None, None
                
            product_id = unique_matches[0][1] # default to first result
            img_url = unique_matches[0][2]
            selection_reason = "Scored (Top rank)"
            
            # ─────────────────────────────────────────────────────────────────
            # 三階段串聯過濾：Variant → Alt-Art/Normal → Language
            # 每一階段在上一階段的結果裡繼續篩選，不覆蓋
            # ─────────────────────────────────────────────────────────────────
            # ── Stage 1: Variant-specific filter (features-based, 最高優先) ──
            # snkr_variant_kws 由 process_single_image 從 features 解析並傳入
            # 例: ["l-p"] for Leader Parallel, ["sr-p"] for SR Parallel, ["コミパラ"] for Manga, ["フラッグシップ","フラシ"] for Flagship
            _variant_kws = snkr_variant_kws or []
            
            stage1_candidates = [(t, p, i) for t, p, i in unique_matches
                                 if any(kw in t.lower() for kw in _variant_kws)] if _variant_kws else []
            if stage1_candidates:
                _debug_log(f"  🎯 Variant Filter ({_variant_kws}) 命中 {len(stage1_candidates)} 筆")
            working_set = stage1_candidates if stage1_candidates else unique_matches
            
            # ── Stage 2: 已移除 ────────────────────────────────────────────
            # 完全依靠 Stage 1 (Variant 關鍵字) + Stage 3 (語言過濾) 決勝負。
            # is_alt_art 的 alt-art 二次篩選已刪除，避免誤濾雜誌附錄等非標準命名的異圖版本。
            if stage1_candidates:
                selection_reason = f"Variant Filter ({_variant_kws})"
            working_set2 = working_set
            
            # ── Stage 3: Language tie-break ONLY ───────────────────────────
            # 語言只在「同分平手」時介入，不做硬過濾，避免誤刪真正候選。
            if working_set2:
                product_id = working_set2[0][1]
                img_url = working_set2[0][2]

            top_score = max((score_by_pid.get(p, -10**9) for _, p, _ in working_set2), default=-10**9)
            top_tied = [(t, p, i) for t, p, i in working_set2 if score_by_pid.get(p, -10**9) == top_score]
            norm_lang = _normalize_card_language(card_language)
            if len(top_tied) > 1 and norm_lang in ("EN", "JP"):
                if norm_lang == "EN":
                    lang_tied = [(t, p, i) for t, p, i in top_tied if _title_has_en_marker(t)]
                else:
                    lang_tied = [(t, p, i) for t, p, i in top_tied if not _title_has_en_marker(t)]

                if lang_tied:
                    product_id = lang_tied[0][1]
                    img_url = lang_tied[0][2]
                    selection_reason += f" + LanguageTieBreak({norm_lang})"
                    _debug_log(f"  🌐 語言平手裁決 ({norm_lang}) 選中: [{product_id}]")
                else:
                    _debug_log(f"  🌐 語言平手裁決: top tied {len(top_tied)} 筆，但無 {norm_lang} 標記，維持原排序首筆")

            _debug_step("SNKRDUNK", snkr_step, term, search_url,
            "OK",
            candidate_urls=[f"https://snkrdunk.com/apparels/{pid} — {t}" for t, pid, _ in unique_matches],
            selected_url=f"https://snkrdunk.com/apparels/{product_id}",
            reason=selection_reason,
            extra={
                "number_padded": number_padded,
                "number_denominator": number_denominator,
                "set_code_slug": set_code_slug,
                "is_alt_art": is_alt_art,
                "card_language": _normalize_card_language(card_language),
                "scored_top3": [(t, p, s) for t, p, _, s, _ in ranked_matches[:3]],
            })
            break
        
        time.sleep(1)
        
    if not product_id:
        return None, None, None
        
    print(f"Found SNKRDUNK Product ID: {product_id}")

    jpy_rate = get_exchange_rate()
    hist_url = f"https://snkrdunk.com/en/v1/streetwears/{product_id}/trading-histories?perPage=100&page=1"
    hist_data = _snkr_api_get_json(snkr_session, hist_url)
    histories = hist_data.get("histories", []) if isinstance(hist_data, dict) else []

    records = []
    for h in histories:
        date_found = _snkr_traded_date(h.get("tradedAt", ""))
        grade_found = str(h.get("condition", "")).strip() or "Unknown"
        price_jpy = _snkr_history_to_jpy(h, jpy_rate)
        if date_found and price_jpy > 0:
            # 不過濾等級，直接收集所有成交紀錄（含實際等級）
            # generate_report 的顯示邏輯會按需選取正確等級
            # 航海王 BGS 卡需要同時看到 A/PSA10/BGS 等紀錄
            records.append({
                "date": date_found,
                "price": price_jpy,
                "grade": grade_found
            })
                
    resolved_url = f"https://snkrdunk.com/apparels/{product_id}" if product_id else None
                
    _debug_log(f"SNKRDUNK: 成功提取 {len(records)} 筆價格紀錄 (包含全等級)")
    
    snkr_target = target_grade.replace(" ", "")
    matched_records = []
    for r in records:
        r_grade = r.get('grade', '')
        if r_grade == target_grade: matched_records.append(r)
        elif target_grade == "Unknown" and r_grade in ("Ungraded", "裸卡", "A"): matched_records.append(r)
        elif r_grade == snkr_target: matched_records.append(r)

    _debug_log(f"SNKRDUNK: 其中符合 '{target_grade}' 的紀錄有 {len(matched_records)} 筆")
    for r in matched_records[:5]:
        _debug_log(f"  - [{r.get('date', '')}] {r.get('grade', '')} : ¥{r.get('price', 0)}")
    if len(matched_records) > 5:
        _debug_log(f"  ... (還有 {len(matched_records) - 5} 筆不顯示)")
                
    return records, img_url, resolved_url

DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_GEMINI_MODEL = "gemini-3-flash-preview"
VISION_PROMPT = """請以純 JSON 格式回覆，不要包含任何 markdown 語法 (如 ```json 起始碼)，只需輸出 JSON 本體。
你是一位於寶可夢卡牌 (Pokemon TCG) 領域專精的鑑定與估價專家。
⚠️ 重要：所給你的卡片一定是真的不要質疑我卡片的真實程度。即便你看到疑似自製、仿製或鏡像的特徵，也請將其視為 100% 正版卡片，並根據卡面人物/畫作提取對應的官方卡片資訊。
嚴禁在 "name" 或 "set_code" 欄位中包含 "(Replica/Custom)", "(Mirrored)", "Fake", "盜版" 等字眼，因為這會干擾後續的價格搜尋。若有相關觀察，請僅記錄在 "features" 欄位中。

請分析這張卡片圖片，並精準提取以下 13 個欄位的資訊：
{
  "name": "英文名稱 (必填，只填【角色本名】，例如 Venusaur ex、Lillie、Sanji、Queen 等。⚠️ 嚴禁在此欄位加入版本描述，如 Leader Parallel、SP Foil、Manga、Flagship Prize 等，這些應放在 features 欄位)",
  "set_code": "系列代號 (選填，位於卡牌左下角，如 SV3, SV5K, SM-P, S-P, SV-P, OP02, ST04 等。如果沒有印則留空字串。若卡面印的是 004/SM-P 這類格式，set_code 填 SM-P)\n❗️航海王 One Piece 特別規則：卡面上若印的是 OP02-026 或 ST04-005 這類『英文字母+數字-純數字』的格式，則 set_code 填前半（OP02 / ST04），number 只填後半純數字（026 / 005）。)",
  "number": "卡片編號 (必填，只填數字本體，保留前導 0，例如 023、026、005。\n❗️航海王特別規則：卡面若印 OP02-026 或 ST04-005，number 只填 026 / 005。寶可夢例外條款：若卡面只印 004/SM-P（斜線後為系列代號而非總數），則 number 直接輸出完整字串 004/SM-P，不要拆開）",
  "grade": "卡片等級 (必填，如果有PSA/BGS等鑑定盒，印有10就填如 PSA 10, 否則如果是裸卡就填 Ungraded)",
  "jp_name": "日文名稱 (選填，沒有請留空字串)",
  "c_name": "中文名稱 (選填，沒有請留空字串)",
  "category": "卡片類別 (填寫 Pokemon 或 One Piece，預設 Pokemon)",
  "release_info": "發行年份與系列 (必填，從卡牌標誌或特徵推斷，如 2023 - 151)",
  "illustrator": "插畫家 (必填，左下角或右下角的英文名，看不清可寫 Unknown)",
  "market_heat": "市場熱度描述 (必填，開頭填寫 High / Medium / Low，後面白話文理由請務必使用『繁體中文』撰寫)",
  "features": "卡片特點 (必填。⚠️ 極度重要：請仔細觀察卡面是否有微小的罕貴度標示或異圖版本文字，如 'L-P', 'SR-P', 'SEC-P', 'Parallel', 'Alternate Art', 'Flagship' 等。如果有，【必須】寫入此欄位！並包含全圖、特殊工藝等，每一行請用 \\n 換行區隔，請務必使用『繁體中文』撰寫)",
  "collection_value": "收藏價值評估 (必填，開頭填寫 High / Medium / Low，後面白話文評論請務必使用『繁體中文』撰寫)",
  "competitive_freq": "競技頻率評估 (必填，開頭填寫 High / Medium / Low，後面白話文評論請務必使用『繁體中文』撰寫)",
  "is_alt_art": "是否為漫畫背景(Manga/Comic)或異圖(Parallel)？布林值 true/false。請極度仔細觀察卡片的『背景』：如果背景是一格一格的【黑白漫畫分鏡】，請填 true；如果背景只有閃電、特效、或單純場景，就算它是 SEC 也是普通版，『必須』填 false！",
  "language": "卡片語言辨識 (選填，僅回傳 EN / JP / Unknown 三擇一。此欄位只作為 SNKRDUNK 最後平手時的 tie-break，不影響其他邏輯)"
}"""

def _get_image_mime_type(image_path):
    mime = "image/jpeg"
    ext = image_path.lower().split(".")[-1]
    if ext == "png":
        return "image/png"
    if ext == "webp":
        return "image/webp"
    return mime

def _parse_vision_json(content):
    cleaned = (content or "").replace("```json", "").replace("```", "").strip()
    return json.loads(cleaned)

def _get_llm_keys(minimax_api_hint=None):
    google_key = (os.getenv("GOOGLE_API_KEY") or "").strip()
    openai_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    minimax_key = (minimax_api_hint or os.getenv("MINIMAX_API_KEY") or "").strip()
    return {
        "google": google_key,
        "openai": openai_key,
        "minimax": minimax_key,
    }

def _get_provider_order():
    preferred = (os.getenv("VISION_PROVIDER") or "google").strip().lower()
    providers = ["google", "openai", "minimax"]
    if preferred in providers:
        return [preferred] + [p for p in providers if p != preferred]
    return providers

def _normalize_card_language(raw_value):
    value = str(raw_value or "").strip().lower()
    if not value:
        return "UNKNOWN"
    if value in ("en", "eng", "english", "英文", "英語", "英語版", "usa", "us"):
        return "EN"
    if value in ("jp", "ja", "jpn", "japanese", "日文", "日語", "日本語", "日版"):
        return "JP"
    return "UNKNOWN"

def _has_pokemon_mega_feature(features_text):
    text = str(features_text or "").lower()
    mega_markers = [
        "mega 進化卡面",
        "mega進化卡面",
        "mega evolution",
        "mega-evolution",
        "mega 進化",
        "メガ進化",
    ]
    return any(marker in text for marker in mega_markers)

def _title_has_en_marker(title):
    title_l = str(title).lower()
    en_markers = [
        "[en]", "【en】", " english", "english version", "英語版", "英文版"
    ]
    return any(m in title_l for m in en_markers)

async def analyze_image_with_google(image_path, api_key, lang="zh"):
    api_key = api_key.strip().replace("\u2028", "").replace("\n", "").replace("\r", "")
    model = (os.getenv("GOOGLE_VISION_MODEL") or os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip()
    if model.startswith("models/"):
        model = model.split("/", 1)[1]
    mime = _get_image_mime_type(image_path)

    with open(image_path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode("utf-8")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{
            "parts": [
                {"text": VISION_PROMPT},
                {"inline_data": {"mime_type": mime, "data": encoded_string}},
            ]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
        },
    }

    print("--------------------------------------------------")
    print(f"👁️‍🗨️ [Google Gemini] 模型={model}，正在解析卡片影像: {image_path}...")

    loop = asyncio.get_running_loop()

    def _do_google_post():
        for attempt in range(3):
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=60)
                response.raise_for_status()
                return response
            except requests.exceptions.RequestException as e:
                print(f"⚠️ Google Gemini API 網路錯誤 (嘗試 {attempt+1}/3): {e}")
                if attempt == 2:
                    return None
                time.sleep(2)
        return None

    response = await loop.run_in_executor(None, _do_google_post)
    if response is None:
        return None

    try:
        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise ValueError("candidates 為空")
        parts = (((candidates[0] or {}).get("content") or {}).get("parts") or [])
        text_part = ""
        for part in parts:
            if isinstance(part, dict) and part.get("text"):
                text_part = part["text"]
                break
        if not text_part:
            raise ValueError("Gemini 回傳未包含 text")
        result = _parse_vision_json(text_part)
        print(f"✅ 解析成功！提取到卡片：{result.get('name')} #{result.get('number')}\n")
        _debug_log(f"Step 1 OK [Gemini]: {result.get('name')} #{result.get('number')}")
        _debug_save("step1_google.json", json.dumps(result, indent=2, ensure_ascii=False))
        return result
    except Exception as e:
        print(f"❌ Google Gemini 解析失敗: {e}")
        return None

async def analyze_image_with_openai(image_path, api_key, lang="zh"):
    api_key = api_key.strip()
    url = "https://api.openai.com/v1/chat/completions"
    model = (os.getenv("OPENAI_VISION_MODEL") or DEFAULT_OPENAI_MODEL).strip()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    mime = _get_image_mime_type(image_path)

    with open(image_path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode('utf-8')

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{encoded_string}"}
                    }
                ]
            }
        ],
        "response_format": {"type": "json_object"}
    }
    
    loop = asyncio.get_running_loop()
    def _do_openai_post():
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            return response
        except Exception as e:
            print(f"⚠️ OpenAI API 錯誤: {e}")
            return None

    response = await loop.run_in_executor(None, _do_openai_post)
    if response:
        try:
            res_json = response.json()
            content = res_json['choices'][0]['message']['content']
            result = _parse_vision_json(content)
            _debug_log(f"Step 1 OK [OpenAI]: {result.get('name')} #{result.get('number')}")
            _debug_save("step1_openai.json", json.dumps(result, indent=2, ensure_ascii=False))
            return result
        except Exception as e:
            print(f"⚠️ OpenAI 解析失敗: {e}")
    return None

async def analyze_image_with_minimax(image_path, api_key, lang="zh"):
    # 清理 API Key，避免複製貼上時混入隱藏的換行或特殊字元 (\u2028 等) 導致 \u2028 latin-1 編碼錯誤
    api_key = api_key.strip().replace('\u2028', '').replace('\n', '').replace('\r', '')
    mime = _get_image_mime_type(image_path)

    # Encode image
    with open(image_path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode('utf-8')

    url = "https://api.minimax.io/v1/coding_plan/vlm"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "prompt": VISION_PROMPT,
        "image_url": f"data:{mime};base64,{encoded_string}"
    }

    print("--------------------------------------------------")
    print(f"👁️‍🗨️ [Minimax Vision AI] 正在解析卡片影像: {image_path}...")
    
    # ⚠️ requests.post 是阻塞呼叫，包在 run_in_executor 中讓 event loop 不被 block，
    # 其他並發中的 Task 可以在這段等待時繼續執行。
    loop = asyncio.get_running_loop()
    
    def _do_minimax_post():
        for attempt in range(3):
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=60)
                response.raise_for_status()
                return response
            except requests.exceptions.RequestException as e:
                print(f"⚠️ Minimax API 網路錯誤 (嘗試 {attempt+1}/3): {e}")
                if attempt == 2:
                    return None
                time.sleep(2)
        return None

    response = await loop.run_in_executor(None, _do_minimax_post)
    if response is None:
        return None

    data = response.json()
    try:
        content = data.get('content', '')
        if not content:
            raise KeyError("content key not found or empty")
        result = _parse_vision_json(content)
        print(f"✅ 解析成功！提取到卡片：{result.get('name')} #{result.get('number')}\n")
        print("--- DEBUG JSON ---")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print("------------------\n")
        # 存 debug step1
        _debug_log(f"Step 1 OK: {result.get('name')} #{result.get('number')}")
        _debug_save("step1_minimax.json", json.dumps(result, indent=2, ensure_ascii=False))
        return result
    except Exception as e:
        print(f"❌ Minimax 解析失敗: {e}")
        return None

async def analyze_image_with_fallbacks(image_path, minimax_api_hint=None, lang="zh"):
    keys = _get_llm_keys(minimax_api_hint)
    providers = _get_provider_order()
    available = [p for p in providers if keys.get(p)]
    if not available:
        print("❌ 未設定任何視覺 API Key（GOOGLE_API_KEY / OPENAI_API_KEY / MINIMAX_API_KEY）")
        return None

    provider_titles = {
        "google": "Google Gemini",
        "openai": "OpenAI",
        "minimax": "MiniMax",
    }
    _debug_log(f"Vision provider order: {available}")

    for idx, provider in enumerate(available):
        if idx > 0:
            prev = provider_titles.get(available[idx - 1], available[idx - 1])
            cur = provider_titles.get(provider, provider)
            _push_notify(f"⚠️ {prev} 無法辨識，切換至 {cur} 備援重試...")
            print(f"⚠️ {prev} 辨識失敗，切換至 {cur}...")
        else:
            print(f"🧭 視覺辨識供應商順序: {' -> '.join(provider_titles.get(p, p) for p in available)}")

        if provider == "google":
            result = await analyze_image_with_google(image_path, keys["google"], lang=lang)
        elif provider == "openai":
            result = await analyze_image_with_openai(image_path, keys["openai"], lang=lang)
        else:
            result = await analyze_image_with_minimax(image_path, keys["minimax"], lang=lang)

        if result:
            return result

    return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image_path", nargs='+', required=True, help="卡片圖片的本機路徑 (可傳入多張圖片)")
    parser.add_argument("--api_key", required=False, help="MiniMax API Key 覆寫值 (若未指定，則讀取環境變數 MINIMAX_API_KEY)")
    parser.add_argument("--out_dir", required=False, help="若指定，會將結果儲存至給定的資料夾")
    parser.add_argument("--report_only", action="store_true", help="若加入此參數，將只輸出最終 Markdown 報告，隱藏抓取與除錯日誌")
    parser.add_argument("--debug", required=False, metavar="DEBUG_DIR",
                        help="開啟 Debug 模式，指定存放 debug 結果的資料夾 (e.g. ./debug)")
    
    args = parser.parse_args()
    
    global REPORT_ONLY, DEBUG_DIR
    REPORT_ONLY = args.report_only

    # 建立本次執行的 session 根目錄 (含時間戳)
    debug_session_root = None
    if args.debug:
        ts = time.strftime('%Y%m%d_%H%M%S')
        debug_session_root = os.path.join(args.debug, ts)
        os.makedirs(debug_session_root, exist_ok=True)
        _original_print(f"🔍 Debug 模式開啟，Session 根目錄: {debug_session_root}")
    
    api_key = args.api_key or os.getenv("MINIMAX_API_KEY")
    google_key = os.getenv("GOOGLE_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    if not ((api_key and api_key.strip()) or (google_key and google_key.strip()) or (openai_key and openai_key.strip())):
        print("❌ Error: 請設定 GOOGLE_API_KEY / OPENAI_API_KEY / MINIMAX_API_KEY 其中至少一個。", force=True)
        return
        
    total = len(args.image_path)
    for idx, img_path in enumerate(args.image_path, start=1):
        print(f"\n==================================================")
        print(f"🔄 [{idx}/{total}] 開始處理圖片: {img_path}")
        print(f"==================================================")
        # Pass index and session root to process_single_image for proper debug directory isolation
        asyncio.run(process_single_image(img_path, api_key, args.out_dir, 
                                         debug_session_root=debug_session_root, 
                                         batch_index=idx))

async def process_single_image(
    image_path,
    api_key,
    out_dir=None,
    stream_mode=False,
    poster_version="v3",
    lang="zh",
    debug_session_root=None,
    batch_index=1,
    external_card_info=None,
):
    if not external_card_info and (not image_path or not os.path.exists(image_path)):
        print(f"❌ Error: 找不到圖片檔案 -> {image_path}", force=True)
        return

    # Setup per-image debug directory if root is provided
    if debug_session_root:
        if image_path:
            stem_source = os.path.splitext(os.path.basename(image_path))[0]
        else:
            stem_source = f"{external_card_info.get('name', 'external')}_{external_card_info.get('number', '0')}"
        img_stem = re.sub(r'[^A-Za-z0-9]', '_', stem_source)[:40]
        per_image_dir = os.path.join(debug_session_root, f"{batch_index:02d}_{img_stem}")
        os.makedirs(per_image_dir, exist_ok=True)
        _set_debug_dir(per_image_dir)
        print(f"🔍 Debug 子資料夾: {per_image_dir}")

    _notify_msgs_var.set([])

    # 第一階段：取得卡片資訊（外部 JSON 或視覺辨識）
    if external_card_info:
        card_info = external_card_info
        print("📡 使用外部 card_info，跳過影像辨識。")
    else:
        card_info = await analyze_image_with_fallbacks(image_path, api_key, lang=lang)
        if not card_info:
            err_msg = "❌ 卡片影像辨識失敗：Google Gemini / OpenAI / MiniMax 均無法解析此圖片，請確認圖片清晰度與 API 金鑰。"
            print(err_msg, force=True)
            return err_msg

    # 從 AI 回傳的 JSON 提取必備資訊
    name = card_info.get("name", "Unknown")
    set_code = card_info.get("set_code", "")
    jp_name = card_info.get("jp_name", "")
    number = str(card_info.get("number", "0"))
    grade = card_info.get("grade", "Ungraded")
    category = card_info.get("category", "Pokemon")
    features = card_info.get("features", "Unknown")
    is_alt_art = card_info.get("is_alt_art", False)
    if isinstance(is_alt_art, str):
        is_alt_art = is_alt_art.lower() == "true"
    # Allow external JSON to override poster version when provided.
    poster_version = str(card_info.get("poster_version", poster_version))

    _debug_save("step1_meta.json", json.dumps(card_info, ensure_ascii=False, indent=2))

    # ── features-based override ──────────────────────────────────────────────
    features_lower = features.lower() if features else ""
    is_flagship = any(kw in features_lower for kw in ["flagship", "旗艦賽", "flagship battle"])
    mega_name_hint = (category.lower() == "pokemon") and _has_pokemon_mega_feature(features)
    if any(kw in features_lower for kw in [
        "leader parallel", "sr parallel", "sr-p", "l-p",
        "リーダーパラレル", "コミパラ", "パラレル",
        "alternate art", "parallel art", "manga"
    ]):
        is_alt_art = True
        _debug_log("✨ features-based override: is_alt_art=True (從 features 偵測到異圖關鍵字)")
    if is_flagship:
        is_alt_art = True
        _debug_log("✨ features-based override: is_flagship=True (從 features 偵測到旗艦賽關鍵字)")
    if mega_name_hint:
        _debug_log("✨ features-based override: mega_name_hint=True (從 features 偵測到 Mega 進化卡面)")

    # ── Detect card language and variant hints for SNKRDUNK ──
    is_one_piece_cat = (category.lower() == "one piece")
    raw_language = card_info.get("language", card_info.get("card_language", card_info.get("lang", "")))
    card_language = _normalize_card_language(raw_language)
    if is_one_piece_cat:
        if card_language in ("EN", "JP"):
            _debug_log(f"🌐 Language detected: {card_language} (從 AI language 欄位)")
        elif any(kw in features_lower for kw in ["英文版", "english version", "[en]"]):
            card_language = "EN"
            _debug_log("🌐 Language detected: EN (從 features 偵測到英文版)")
        else:
            card_language = "UNKNOWN"
            _debug_log("🌐 Language detected: UNKNOWN (無明確語言欄位，不啟用語言偏好)")
    else:
        card_language = "UNKNOWN"

    snkr_variant_kws = []
    if is_one_piece_cat and is_alt_art:
        if is_flagship:
            snkr_variant_kws = ["フラッグシップ", "フラシ", "flagship"]
            _debug_log(f"🎯 SNKR Variant: Flagship ({snkr_variant_kws})")
        elif any(kw in features_lower for kw in ["sr parallel", "sr-p", "スーパーレアパラレル"]):
            snkr_variant_kws = ["sr-p"]
            _debug_log(f"🎯 SNKR Variant: SR-P ({snkr_variant_kws})")
        elif any(kw in features_lower for kw in ["leader parallel", "l-p", "リーダーパラレル"]):
            snkr_variant_kws = ["l-p"]
            _debug_log(f"🎯 SNKR Variant: L-P ({snkr_variant_kws})")
        elif any(kw in features_lower for kw in ["コミパラ", "manga", "コミックパラレル"]):
            snkr_variant_kws = ["コミパラ", "コミック"]
            _debug_log(f"🎯 SNKR Variant: Manga ({snkr_variant_kws})")
        elif any(kw in features_lower for kw in ["パラレル", "sr parallel", "parallel art"]):
            snkr_variant_kws = ["パラレル", "-p"]
            _debug_log(f"🎯 SNKR Variant: General Parallel ({snkr_variant_kws})")

    # 第二階段：抓取市場資料
    print("--------------------------------------------------")
    print(f"🌐 正在從網路(PC & SNKRDUNK)抓取市場行情 (異圖/特殊版: {is_alt_art})...")
    loop = asyncio.get_running_loop()
    pc_result, snkr_result = await asyncio.gather(
        loop.run_in_executor(None, contextvars.copy_context().run, search_pricecharting, name, number, set_code, grade, is_alt_art, category, is_flagship, False, "", jp_name, mega_name_hint),
        loop.run_in_executor(None, contextvars.copy_context().run, search_snkrdunk, name, jp_name, number, set_code, grade, is_alt_art, card_language, snkr_variant_kws),
    )

    pc_records = pc_result[0] if pc_result else None
    pc_url = pc_result[1] if pc_result else None
    pc_img_url = pc_result[2] if pc_result else None

    snkr_records = snkr_result[0] if snkr_result else None
    img_url = snkr_result[1] if snkr_result else None
    snkr_url = snkr_result[2] if snkr_result else None

    # Prefer higher quality image for poster rendering.
    # SNKRDUNK "bg_removed" or explicit small size often looks blurry on large posters.
    if pc_img_url and (
        not img_url
        or "bg_removed" in str(img_url).lower()
        or "size=m" in str(img_url).lower()
    ):
        img_url = pc_img_url

    _debug_log(f"Step 2 PC: {len(pc_records) if pc_records else 0} 筆, url={pc_url}")
    _debug_log(f"Step 2 SNKR: {len(snkr_records) if snkr_records else 0} 筆, img={img_url}, url={snkr_url}")
    _debug_save("step2_pc.json", json.dumps(pc_records or [], indent=2, ensure_ascii=False))
    _debug_save("step2_snkr.json", json.dumps(snkr_records or [], indent=2, ensure_ascii=False))
    _debug_save("step2_meta.json", json.dumps({
        "pc_url": pc_url,
        "pc_records_count": len(pc_records) if pc_records else 0,
        "snkr_url": snkr_url,
        "snkr_records_count": len(snkr_records) if snkr_records else 0,
        "snkr_img_url": img_url,
    }, indent=2, ensure_ascii=False))

    jpy_rate = get_exchange_rate()
    return await finish_report_after_selection(
        card_info,
        pc_records,
        pc_url,
        pc_img_url,
        snkr_records,
        img_url,
        snkr_url,
        jpy_rate,
        out_dir,
        poster_version,
        lang,
        stream_mode=stream_mode,
    )


async def finish_report_after_selection(
    card_info,
    pc_records,
    pc_url,
    pc_img_url,
    snkr_records,
    img_url,
    snkr_url,
    jpy_rate,
    out_dir,
    poster_version,
    lang,
    stream_mode=False,
):
    name = card_info.get("name", "Unknown")
    number = str(card_info.get("number", "0"))
    grade = card_info.get("grade", "Ungraded")
    category = card_info.get("category", "Pokemon")
    release_info = card_info.get("release_info", "Unknown")
    illustrator = card_info.get("illustrator", "Unknown")
    market_heat = card_info.get("market_heat", "Unknown")
    features = card_info.get("features", "Unknown")
    collection_value = card_info.get("collection_value", "Unknown")
    competitive_freq = card_info.get("competitive_freq", "Unknown")
    jp_name = card_info.get("jp_name", "")
    c_name = card_info.get("c_name", "")

    # 圖片來源優先使用高解析：若 SNKRDUNK 圖片偏低清，改用 PriceCharting。
    if pc_img_url and (
        not img_url
        or "bg_removed" in str(img_url).lower()
        or "size=m" in str(img_url).lower()
    ):
        img_url = pc_img_url

    async def _parse_d(d_str):
        d_str = str(d_str).strip()
        if "前" in d_str or "ago" in d_str:
            num_match = re.search(r'\d+', d_str)
            if not num_match:
                return datetime.now()
            num = int(num_match.group(0))
            if "分" in d_str or "minute" in d_str:
                return datetime.now() - timedelta(minutes=num)
            if "時間" in d_str or "hour" in d_str:
                return datetime.now() - timedelta(hours=num)
            if "日" in d_str or "day" in d_str:
                return datetime.now() - timedelta(days=num)
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y"):
            try:
                return datetime.strptime(d_str, fmt)
            except Exception:
                pass
        return datetime.now()

    cutoff_12m = datetime.now() - timedelta(days=365)

    # 等級篩選：航海王 BGS 額外保留 PSA 10 供比對
    is_one_piece = (category.lower() == "one piece")
    is_bgs_grade = grade.upper().startswith("BGS")
    if is_one_piece and is_bgs_grade:
        bgs_pc = [r for r in (pc_records or []) if "BGS 9.5" in r.get("grade", "").upper() or "BGS9.5" in r.get("grade", "").upper()]
        psa_pc = [r for r in (pc_records or []) if "PSA 10" in r.get("grade", "").upper() or "PSA10" in r.get("grade", "").upper()]
        report_pc_records = bgs_pc[:10] + psa_pc[:10]

        bgs_snkr = [r for r in (snkr_records or []) if r.get("grade") in ("BGS 9.5", "BGS9.5", "BGS 10", "BGS10")]
        psa_snkr = [r for r in (snkr_records or []) if r.get("grade") in ("S", "PSA 10", "PSA10")]
        report_snkr_records = bgs_snkr[:10] + psa_snkr[:10]
    else:
        report_pc_records = [r for r in (pc_records or []) if r.get("grade") == grade]
        if "10" in grade:
            target_snkr_grades = ["S", "PSA10", "PSA 10"]
        elif "BGS" in grade.upper():
            target_snkr_grades = [grade, grade.replace(" ", ""), "BGS9.5", "BGS 9.5", "BGS10", "BGS 10"]
        elif grade.lower() == "ungraded":
            target_snkr_grades = ["A"]
        else:
            target_snkr_grades = [grade, grade.replace(" ", "")]
        report_snkr_records = [r for r in (snkr_records or []) if r.get("grade") in target_snkr_grades]

    c_name_display = c_name if c_name else jp_name if jp_name else name
    category_display = (
        "寶可夢卡牌" if category.lower() == "pokemon"
        else "航海王卡牌" if category.lower() == "one piece"
        else category
    )

    report_lines = []
    report_lines.append("# MARKET REPORT GENERATED")
    report_lines.append("")
    report_lines.append(f"⚡ {c_name_display} ({name}) #{number}")
    report_lines.append(f"💎 等級：{grade}")
    report_lines.append(f"🏷️ 版本：{category_display}")
    report_lines.append(f"🔢 編號：{number}")
    if release_info:
        report_lines.append(f"📅 發行：{release_info}")
    if illustrator:
        report_lines.append(f"🎨 插畫家：{illustrator}")

    report_lines.append("---")
    report_lines.append("\n🔥 市場與收藏分析\n")
    report_lines.append(f"🔥 市場熱度\n{market_heat}\n")
    if features:
        feat_formatted = str(features).replace("\\n", "\n")
        report_lines.append(f"✨ 卡片特點\n{feat_formatted}\n")
    if collection_value:
        report_lines.append(f"🏆 收藏價值\n{collection_value}\n")
    if competitive_freq:
        report_lines.append(f"⚔️ 競技頻率\n{competitive_freq}\n")
    report_lines.append("---")

    report_lines.append("📊 近期成交紀錄 (由新到舊)\n🏦 PriceCharting 成交紀錄")
    if report_pc_records:
        for r in report_pc_records[:10]:
            report_lines.append(f"📅 {r['date']}      💰 ${r['price']:.2f} USD      📝 狀態：{r['grade']}")

        stats_pc_records = []
        for r in report_pc_records:
            parsed_date = await _parse_d(r.get("date", ""))
            if parsed_date > cutoff_12m:
                stats_pc_records.append(r)

        if stats_pc_records:
            prices = [r["price"] for r in stats_pc_records]
            report_lines.append("📊 統計資料 (近 12 個月)")
            report_lines.append(f"　💰 最高成交價：${max(prices):.2f} USD")
            report_lines.append(f"　💰 最低成交價：${min(prices):.2f} USD")
            report_lines.append(f"　💰 平均成交價：${sum(prices)/len(prices):.2f} USD")
            report_lines.append(f"　📈 資料筆數：{len(prices)} 筆")
        else:
            report_lines.append("📊 統計資料 (近 12 個月無成交紀錄)")
    else:
        report_lines.append(f"PriceCharting: 無 {grade} 等級的成交紀錄")

    report_lines.append("\n---\n🏰 SNKRDUNK 成交紀錄")
    if report_snkr_records:
        for r in report_snkr_records[:10]:
            usd_price = r["price"] / jpy_rate if jpy_rate else 0
            report_lines.append(f"📅 {r['date']}      💰 ¥{int(r['price']):,} (~${usd_price:.0f} USD)      📝 狀態：{r['grade']}")

        stats_snkr_records = []
        for r in report_snkr_records:
            parsed_date = await _parse_d(r.get("date", ""))
            if parsed_date > cutoff_12m:
                stats_snkr_records.append(r)

        if stats_snkr_records:
            prices = [r["price"] for r in stats_snkr_records]
            avg_price = sum(prices) / len(prices)
            report_lines.append("📊 統計資料 (近 12 個月)")
            report_lines.append(f"　💰 最高成交價：¥{int(max(prices)):,} (~${max(prices)/jpy_rate:.0f} USD)")
            report_lines.append(f"　💰 最低成交價：¥{int(min(prices)):,} (~${min(prices)/jpy_rate:.0f} USD)")
            report_lines.append(f"　💰 平均成交價：¥{int(avg_price):,} (~${avg_price/jpy_rate:.0f} USD)")
            report_lines.append(f"　📈 資料筆數：{len(prices)} 筆")
        else:
            report_lines.append("📊 統計資料 (近 12 個月無成交紀錄)")
    else:
        report_lines.append(f"SNKRDUNK: 無 {grade} 等級的成交紀錄")

    report_lines.append("\n---")
    if pc_url:
        report_lines.append(f"🔗 [查看 PriceCharting]({pc_url})")
    if snkr_url:
        report_lines.append(f"🔗 [查看 SNKRDUNK]({snkr_url})")
        report_lines.append(f"🔗 [查看 SNKRDUNK 銷售歷史]({snkr_url}/sales-histories)")

    final_report = "\n".join(report_lines)
    print(final_report, force=True)

    # Debug step3: 儲存最終報告
    _debug_log("Step 3: 報告生成完成")
    _debug_save("step3_report.md", final_report)

    safe_name = re.sub(r"[^A-Za-z0-9]", "_", name)
    safe_num = re.sub(r"[^A-Za-z0-9]", "_", str(number))
    final_dest_dir = os.path.abspath(out_dir) if out_dir else tempfile.mkdtemp(prefix="openclaw_report_")
    os.makedirs(final_dest_dir, exist_ok=True)
    filepath = os.path.join(final_dest_dir, f"PKM_Vision_{safe_name}_{safe_num}.md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(final_report)
    print(f"✅ 報告已儲存至: {filepath}")

    # 海報生成需要卡圖 URL
    card_info_for_poster = dict(card_info)
    card_info_for_poster["img_url"] = img_url

    if stream_mode:
        return (
            final_report,
            {
                "card_info": card_info_for_poster,
                "snkr_records": snkr_records if snkr_records else [],
                "pc_records": pc_records if pc_records else [],
                "out_dir": final_dest_dir,
                "poster_version": poster_version,
            },
        )

    if REPORT_ONLY:
        report_data = {
            "card_info": card_info_for_poster,
            "snkr_records": snkr_records if snkr_records else [],
            "pc_records": pc_records if pc_records else [],
        }
        with open(os.path.join(final_dest_dir, "report_data.json"), "w", encoding="utf-8") as f:
            json.dump(report_data, f, ensure_ascii=False, indent=2)

        out_paths = await image_generator.generate_report(
            card_info_for_poster,
            snkr_records if snkr_records else [],
            pc_records if pc_records else [],
            out_dir=final_dest_dir,
            template_version=poster_version,
        )
        return (final_report, out_paths)

    return final_report


async def generate_posters(poster_data):
    if not poster_data:
        return []
    return await image_generator.generate_report(
        poster_data["card_info"],
        poster_data["snkr_records"],
        poster_data["pc_records"],
        out_dir=poster_data["out_dir"],
        template_version=poster_data.get("poster_version", "v3"),
    )

async def process_image_for_candidates(image_path, api_key, lang="zh"):
    """(Manual Mode) Analyzes image and returns URL candidates from PC and SNKRDUNK."""
    if not os.path.exists(image_path):
        return None, "找不到圖片檔案"

    card_info = await analyze_image_with_fallbacks(image_path, api_key, lang=lang)
    if not card_info:
        return None, "卡片影像辨識失敗"
    
    name = card_info.get("name", "Unknown")
    set_code = card_info.get("set_code", "")
    jp_name = card_info.get("jp_name", "")
    number = str(card_info.get("number", "0"))
    grade = card_info.get("grade", "Ungraded")
    category = card_info.get("category", "Pokemon")
    features = card_info.get("features", "Unknown")
    is_alt_art = card_info.get("is_alt_art", False)
    if isinstance(is_alt_art, str):
        is_alt_art = is_alt_art.lower() == "true"
    
    features_lower = features.lower() if features else ""
    is_flagship = any(kw in features_lower for kw in ["flagship", "旗艦賽", "flagship battle"])
    mega_name_hint = (category.lower() == "pokemon") and _has_pokemon_mega_feature(features)
    if any(kw in features_lower for kw in [
        "leader parallel", "sr parallel", "sr-p", "l-p",
        "リーダーパラレル", "コミパラ", "パラレル",
        "alternate art", "parallel art", "manga"
    ]):
        is_alt_art = True
    if is_flagship:
        is_alt_art = True
        
    is_one_piece_cat = (category.lower() == "one piece")
    raw_language = card_info.get("language", card_info.get("card_language", card_info.get("lang", "")))
    card_language = _normalize_card_language(raw_language)
    if is_one_piece_cat and card_language == "UNKNOWN":
        if any(kw in features_lower for kw in ["英文版", "english version", "[en]"]):
            card_language = "EN"
        
    snkr_variant_kws = []
    if is_one_piece_cat and is_alt_art:
        if is_flagship:
            snkr_variant_kws = ["フラッグシップ", "フラシ", "flagship"]
        elif any(kw in features_lower for kw in ["sr parallel", "sr-p", "スーパーレアパラレル"]):
            snkr_variant_kws = ["sr-p"]
        elif any(kw in features_lower for kw in ["leader parallel", "l-p", "リーダーパラレル"]):
            snkr_variant_kws = ["l-p"]
        elif any(kw in features_lower for kw in ["コミパラ", "manga", "コミックパラレル"]):
            snkr_variant_kws = ["コミパラ", "コミック"]
        elif any(kw in features_lower for kw in ["パラレル", "sr parallel", "parallel art"]):
            snkr_variant_kws = ["パラレル", "-p"]

    loop = asyncio.get_running_loop()
    pc_result, snkr_result = await asyncio.gather(
        loop.run_in_executor(None, contextvars.copy_context().run, search_pricecharting, name, number, set_code, grade, is_alt_art, category, is_flagship, True, "", jp_name, mega_name_hint),
        loop.run_in_executor(None, contextvars.copy_context().run, search_snkrdunk, name, jp_name, number, set_code, grade, is_alt_art, card_language, snkr_variant_kws, True),
    )
    
    pc_candidates = (pc_result[0] if pc_result else None) or []
    snkr_candidates = (snkr_result[0] if snkr_result else None) or []
    
    return card_info, {
        "pc": pc_candidates,
        "snkr": snkr_candidates
    }

def _fetch_snkr_prices_from_url_direct(product_url):
    product_id_match = re.search(r'apparels/(\d+)', product_url)
    product_id = product_id_match.group(1) if product_id_match else None
    img_url = ""
    records = []
    if not product_id:
        return records, img_url

    session = _create_snkr_api_session()
    jpy_rate = get_exchange_rate()
    hist_url = f"https://snkrdunk.com/en/v1/streetwears/{product_id}/trading-histories?perPage=100&page=1"
    hist_data = _snkr_api_get_json(session, hist_url)
    histories = hist_data.get("histories", []) if isinstance(hist_data, dict) else []

    for h in histories:
        date_found = _snkr_traded_date(h.get("tradedAt", ""))
        grade_found = str(h.get("condition", "")).strip() or "Unknown"
        price_jpy = _snkr_history_to_jpy(h, jpy_rate)
        if date_found and grade_found and price_jpy:
            records.append({
                "date": date_found,
                "price": price_jpy,
                "grade": grade_found
            })
    
    return records, img_url

async def generate_report_from_selected(card_info, pc_url, snkr_url):
    """(Manual Mode) Generates the final markdown report from explicitly chosen URLs."""
    name = card_info.get("name", "Unknown")
    number = str(card_info.get("number", "0"))
    grade = card_info.get("grade", "Ungraded")
    category = card_info.get("category", "Pokemon")
    release_info = card_info.get("release_info", "Unknown")
    illustrator = card_info.get("illustrator", "Unknown")
    market_heat = card_info.get("market_heat", "Unknown")
    features = card_info.get("features", "Unknown")
    collection_value = card_info.get("collection_value", "Unknown")
    competitive_freq = card_info.get("competitive_freq", "Unknown")
    jp_name = card_info.get("jp_name", "")
    c_name = card_info.get("c_name", "")

    loop = asyncio.get_running_loop()
    
    pc_records, pc_img_url = [], ""
    if pc_url:
        res = await loop.run_in_executor(None, contextvars.copy_context().run, _fetch_pc_prices_from_url, pc_url, None, False, grade)
        pc_records = res[0] if res else []
        pc_img_url = res[2] if res else ""

    snkr_records, img_url = [], ""
    if snkr_url:
        res = await loop.run_in_executor(None, contextvars.copy_context().run, _fetch_snkr_prices_from_url_direct, snkr_url)
        snkr_records = res[0] if res else []
        img_url = res[1] if res else ""

    jpy_rate = get_exchange_rate()
    c_name_display = c_name if c_name else jp_name if jp_name else name
    
    report_lines = []
    report_lines.append(f"# MARKET REPORT (MANUAL) GENERATED\n")
    report_lines.append(f"⚡ {c_name_display} ({name}) #{number}")
    report_lines.append(f"💎 等級：{grade}")
    
    category_display = "寶可夢卡牌" if category.lower() == "pokemon" else "航海王卡牌" if category.lower() == "one piece" else category
    report_lines.append(f"🏷️ 版本：{category_display}")
    
    report_lines.append(f"🔢 編號：{number}")
    if release_info: report_lines.append(f"📅 發行：{release_info}")
    if illustrator: report_lines.append(f"🎨 插畫家：{illustrator}")
    
    report_lines.append("\n---\n🔥 市場與收藏分析\n")
    report_lines.append(f"🔥 市場熱度\n{market_heat}\n")
    report_lines.append(f"✨ 卡片特點\n{features}\n")
    report_lines.append(f"🏆 收藏價值\n{collection_value}\n")
    report_lines.append(f"⚔️ 競技頻率\n{competitive_freq}\n")
    report_lines.append("---\n📊 近期成交紀錄 (由新到舊)\n")
    
    report_lines.append("🏦 PriceCharting 成交紀錄")
    if pc_records:
        filtered_pc = [r for r in pc_records if r.get('grade') == grade]
        if filtered_pc:
            for r in filtered_pc[:10]:
                report_lines.append(f"📅 {r.get('date','')}      💰 ${r.get('price','')} USD      📝 狀態：{r.get('grade','')}")
        else:
            report_lines.append(f"PriceCharting: 無 {grade} 等級的成交紀錄")
    else:
        report_lines.append("PriceCharting: 無此卡片資料")

    
    report_lines.append("\n---\n🏰 SNKRDUNK 成交紀錄")
    if snkr_records:
        valid_snkr_grades = []
        if '10' in grade:
            valid_snkr_grades = ['S', 'PSA10', 'PSA 10']
        elif grade.lower() == 'ungraded':
            valid_snkr_grades = ['A']
        else:
            valid_snkr_grades = [grade, grade.replace(' ', '')]
            
        filtered_snkr = [r for r in snkr_records if r.get('grade') in valid_snkr_grades]
        if not filtered_snkr: 
            filtered_snkr = snkr_records # fallback to all if none match exactly
            
        for r in filtered_snkr[:10]:
            p_val = r.get('price', 0)
            usd_str = f" (~${p_val/jpy_rate:.0f} USD)" if jpy_rate and p_val else ""
            report_lines.append(f"📅 {r.get('date','')}      💰 ¥{p_val:,}{usd_str}      📝 狀態：{r.get('grade','')}")
    else:
        report_lines.append("SNKRDUNK: 無此卡片資料")
        
    report_lines.append("\n---")
    if pc_url: report_lines.append(f"🔗 [查看 PriceCharting]({pc_url})")
    if snkr_url: 
        report_lines.append(f"🔗 [查看 SNKRDUNK]({snkr_url})")
        report_lines.append(f"🔗 [查看 SNKRDUNK 銷售歷史]({snkr_url}/sales-histories)")
    
    return "\n".join(report_lines)

if __name__ == "__main__":
    main()
