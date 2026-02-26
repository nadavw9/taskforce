import requests, os, datetime, re, time, smtplib
from email.mime.text import MIMEText

FIREBASE = os.environ['FIREBASE_URL'].rstrip('/')
GEMINI_KEY = os.environ['GEMINI_KEY']
GMAIL_USER = os.environ.get('GMAIL_USER', '')
GMAIL_PASS = os.environ.get('GMAIL_PASS', '')
NOTIFY_EMAIL = os.environ.get('NOTIFY_EMAIL', GMAIL_USER)
MIN_PAY_ALERT = 15  # Email alert for tasks paying $15+

GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"

FEEDS = [
    ('r/forhire', 'https://www.reddit.com/r/forhire/new/.rss?limit=50'),
    ('r/slavelabour', 'https://www.reddit.com/r/slavelabour/new/.rss?limit=50'),
]
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; RSS reader)',
    'Accept': 'application/rss+xml, application/xml, text/xml'
}

DOABLE_PATTERNS = re.compile(
    r'\b(writ|blog|article|post|copy|content|email|letter|descri|summar|research|'
    r'find|gather|translat|edit|proofread|script|code|python|javascript|html|css|'
    r'automat|bot|scrap|data|analyz|report|review|rewrite|caption|bio|proposal|'
    r'cv|resume|cover.?letter|product.?descri|social.?media|tweet|reddit|seo)\b',
    re.I
)
UNDOABLE_PATTERNS = re.compile(
    r'\b(video|photo|image|logo|design|illustrat|voiceover|voice.?over|audio|'
    r'podcast|record|film|animat|3d|photoshop|figma|draw|sketch|paint|'
    r'physical|deliver|print|call|phone|meet|zoom|in.?person)\b',
    re.I
)

def fb(path, method='GET', data=None):
    url = f"{FIREBASE}{path}.json"
    r = requests.request(method, url, json=data,
                         headers={'Content-Type': 'application/json'}, timeout=15)
    if r.status_code == 200:
        return r.json()
    print(f"Firebase error {r.status_code}: {r.text[:200]}")
    return None

def gemini(prompt):
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    r = requests.post(GEMINI_URL, json=body, timeout=60)
    return r.json()['candidates'][0]['content']['parts'][0]['text']

def detect_type(text):
    t = text.lower()
    if re.search(r'\b(code|script|python|javascript|js|html|css|api|bot|develop|bug|fix|app|program|automat)\b', t):
        return 'coding'
    if re.search(r'\b(research|find|gather|data|list|report|analy|summariz|survey|compil|translat)\b', t):
        return 'research'
    return 'writing'

def extract_pay(text):
    matches = re.findall(r'\$\s*(\d+(?:,\d+)?(?:\.\d+)?)', text)
    if not matches:
        return None
    vals = [float(m.replace(',', '')) for m in matches if 0 < float(m.replace(',', '')) < 50000]
    return f"${max(vals):.0f}" if vals else None

def is_doable(title, desc):
    text = title + ' ' + desc
    if UNDOABLE_PATTERNS.search(text):
        return False
    if DOABLE_PATTERNS.search(text):
        return True
    return True

def send_email(subject, body):
    if not GMAIL_USER or not GMAIL_PASS:
        print("Email not configured — skipping")
        return
    try:
        msg = MIMEText(body, 'html')
        msg['Subject'] = subject
        msg['From'] = GMAIL_USER
        msg['To'] = NOTIFY_EMAIL
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.send_message(msg)
        print(f"Email sent: {subject}")
    except Exception as e:
        print(f"Email error: {e}")

def parse_rss(xml, source):
    tasks = []
    for item in re.split(r'<entry>|<item>', xml)[1:]:
        try:
            title_m = re.search(r'<title[^>]*><!\[CDATA\[(.*?)\]\]></title>', item, re.S) or \
                      re.search(r'<title[^>]*>(.*?)</title>', item, re.S)
            link_m = re.search(r'<link[^>]+href=["\']([^"\']+)["\']', item, re.S) or \
                     re.search(r'<link>(.*?)</link>', item, re.S) or \
                     re.search(r'<guid[^>]*>(.*?)</guid>', item, re.S)
            desc_m = re.search(r'<content[^>]*><!\[CDATA\[(.*?)\]\]></content>', item, re.S) or \
                     re.search(r'<description[^>]*><!\[CDATA\[(.*?)\]\]></description>', item, re.S) or \
                     re.search(r'<description[^>]*>(.*?)</description>', item, re.S)
            title = title_m.group(1).strip() if title_m else ''
            link = link_m.group(1).strip() if link_m else ''
            desc = re.sub(r'<[^>]+>', '', desc_m.group(1) if desc_m else '').strip()[:600]
            id_m = re.search(r'/comments/([a-z0-9]+)/', link)
            pid = id_m.group(1) if id_m else re.sub(r'[^a-z0-9]', '', title.lower())[:12]
            if not title or not pid:
                continue
            if 'forhire' in source and not re.search(r'\[h', title, re.I):
                continue
            if not is_doable(title, desc):
                continue
            tasks.append({
                'id': pid, 'redditId': pid,
                'title': re.sub(r'^\[(h|hiring|for hire)\]\s*', '', title, flags=re.I).strip(),
                'description': desc or 'No description.',
                'pay': extract_pay(title + ' ' + desc),
                'type': detect_type(title + ' ' + desc),
                'source': source, 'url': link,
                'createdAt': int(time.time()),
                'status': 'inbox',
                'fetchedAt': int(datetime.datetime.utcnow().timestamp() * 1000)
            })
        except Exception as e:
            print(f"Parse error: {e}")
    return tasks

def fetch_reddit():
    existing = fb('/tasks') or {}
    existing_ids = set(existing.keys())
    new_tasks = {}
    high_pay_tasks = []
    for source, feed_url in FEEDS:
        try:
            r = requests.get(feed_url, headers=HEADERS, timeout=20)
            print(f"{source} RSS: {r.status_code}")
            if not r.ok:
                continue
            tasks = parse_rss(r.text, source)
            fresh = [t for t in tasks if t['id'] not in existing_ids]
            print(f"{source}: {len(fresh)} new doable tasks")
            for t in fresh:
                new_tasks[t['id']] = t
                pay_val = float(t['pay'].replace('$','').replace(',','')) if t.get('pay') else 0
                if pay_val >= MIN_PAY_ALERT:
                    high_pay_tasks.append(t)
        except Exception as e:
            print(f"Error {source}: {e}")
    if new_tasks:
        for pid, task in new_tasks.items():
            fb(f'/tasks/{pid}', 'PUT', task)
        print(f"Pushed {len(new_tasks)} tasks to Firebase")
    if high_pay_tasks:
        rows = ''.join([f'<tr><td style="padding:10px;color:#f0a500">{t["pay"]}</td><td style="padding:10px">{t["title"][:60]}</td><td style="padding:10px"><a href="{t["url"]}">View</a></td></tr>' for t in high_pay_tasks])
        send_email(
            f"⚡ TASKFORCE: {len(high_pay_tasks)} task(s) paying ${MIN_PAY_ALERT}+",
            f'<div style="background:#0a0b0d;color:#c8cdd8;padding:20px;font-family:monospace"><h2 style="color:#f0a500">⚡ High-Pay Tasks Available</h2><table style="width:100%">{rows}</table><p><a href="https://nadavw9.github.io/taskforce" style="color:#00e5a0">Open Dashboard →</a></p></div>'
        )

def execute_tasks():
    tasks = fb('/tasks') or {}
    approved = {k: v for k, v in tasks.items() if isinstance(v, dict) and v.get('status') == 'approved'}
    print(f"Approved tasks to execute: {len(approved)}")
    for key, task in approved.items():
        print(f"Executing: {task.get('title','?')[:50]}")
        fb(f'/tasks/{key}', 'PATCH', {'status': 'executing'})
        try:
            prompts = {
                'coding': 'You are an expert developer completing a paid freelance task. Write complete, working, well-commented code with usage instructions:',
                'research': 'You are a professional researcher completing a paid freelance task. Provide thorough, organized, accurate findings with a summary:',
                'writing': 'You are an expert copywriter completing a paid freelance task. Write compelling, professional, ready-to-submit content:'
            }
            t = task.get('type', 'writing')
            prompt = f"{prompts.get(t, prompts['writing'])}\n\nTask: {task.get('title','')}\n\nDetails: {task.get('description','')}\n\nPay: {task.get('pay','negotiable')}\n\nDeliver professional, complete work ready to send to the client."
            output = gemini(prompt)
            fb(f'/tasks/{key}', 'PATCH', {
                'status': 'done', 'output': output,
                'completedAt': datetime.datetime.utcnow().isoformat()
            })
            print(f"Done: {task.get('title','?')[:40]}")
            if GMAIL_USER and GMAIL_PASS:
                send_email(
                    f"✅ Task completed: {task.get('title','')[:50]}",
                    f'<div style="background:#0a0b0d;color:#c8cdd8;padding:20px;font-family:monospace"><h2 style="color:#00e5a0">✅ Task Done</h2><p style="color:#f0a500">{task.get("title","")}</p><p>Pay: {task.get("pay","?")}</p><a href="https://nadavw9.github.io/taskforce" style="color:#00e5a0">Open Dashboard to copy & send →</a></div>'
                )
        except Exception as e:
            fb(f'/tasks/{key}', 'PATCH', {'status': 'error', 'error': str(e)})
            print(f"Error: {e}")

print("=== TaskForce Bot Starting ===")
fetch_reddit()
execute_tasks()
print("=== Bot Done ===")
