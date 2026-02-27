import requests, os, datetime, re, time, smtplib
from email.mime.text import MIMEText

FIREBASE = os.environ['FIREBASE_URL'].rstrip('/')
GEMINI_KEY = os.environ['GEMINI_KEY']
GMAIL_USER = os.environ.get('GMAIL_USER', '')
GMAIL_PASS = os.environ.get('GMAIL_PASS', '')
NOTIFY_EMAIL = os.environ.get('NOTIFY_EMAIL', GMAIL_USER)
MIN_PAY_ALERT = 15

# UPGRADED: gemini-2.0-flash = free, 1500 req/day, much better quality
GEMINI_MODEL = 'gemini-2.0-flash'
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"

FEEDS = [
    ('r/forhire', 'https://www.reddit.com/r/forhire/new/.rss?limit=50'),
    ('r/slavelabour', 'https://www.reddit.com/r/slavelabour/new/.rss?limit=50'),
]
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; RSS reader)',
    'Accept': 'application/rss+xml, application/xml, text/xml'
}

# ── TASK QUALITY FILTERS ──────────────────────────────────────────────────────

# Tasks AI can do well (90%+ success)
HIGH_SUCCESS = re.compile(
    r'\b(email|newsletter|blog|article|post|copy|content|letter|descri|summar|'
    r'translat|edit|proofread|caption|bio|proposal|cv|resume|cover.?letter|'
    r'product.?descri|social.?media|seo|rewrite|paraphras|'
    r'research|find|gather|compil|list|report|analyz|survey|'
    r'python|javascript|js|html|css|script|automat|bot|scrape|'
    r'wordpress|woocommerce|shopify)\b',
    re.I
)

# Tasks AI cannot do
UNDOABLE = re.compile(
    r'\b(video|photo|image|logo|design|illustrat|voiceover|voice.?over|audio|'
    r'podcast|record|film|animat|3d|photoshop|figma|draw|sketch|paint|'
    r'physical|deliver|print|call|phone|meet|zoom|in.?person|on.?site|'
    r'on site|driver|delivery|warehouse|construction|install)\b',
    re.I
)

# Job listings (people offering THEMSELVES, not hiring for a task)
JOB_LISTING = re.compile(
    r'\b(we are hiring|we\'re hiring|join our team|full.?time|part.?time|'
    r'per deal|commission|salary|per month|per year|annually|'
    r'outbound|sales rep|sales development|account exec|'
    r'send (your )?resume|send (your )?cv|apply (now|here|today)|'
    r'job opening|position available|looking to hire|recruiting)\b',
    re.I
)

# Minimum task — must have some actual deliverable
DELIVERABLE = re.compile(
    r'\b(write|create|build|make|develop|design|research|find|translate|'
    r'edit|fix|code|script|help|need|want|looking for|hire|require)\b',
    re.I
)

def is_good_task(title, desc):
    text = title + ' ' + desc
    # Reject job listings first
    if JOB_LISTING.search(text):
        return False, 'job_listing'
    # Reject undoable tasks
    if UNDOABLE.search(text):
        return False, 'undoable'
    # Must have a deliverable request
    if not DELIVERABLE.search(title):
        return False, 'no_deliverable'
    # Prefer tasks with clear AI-doable keywords
    if HIGH_SUCCESS.search(text):
        return True, 'high_success'
    # Default: accept and let AI try
    return True, 'general'

# ── CORE FUNCTIONS ────────────────────────────────────────────────────────────

def fb(path, method='GET', data=None):
    url = f"{FIREBASE}{path}.json"
    r = requests.request(method, url, json=data,
                         headers={'Content-Type': 'application/json'}, timeout=15)
    if r.status_code == 200:
        return r.json()
    print(f"Firebase error {r.status_code}: {r.text[:200]}")
    return None

def gemini(prompt, model=None):
    url = GEMINI_URL
    if model:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    r = requests.post(url, json=body, timeout=90)
    if r.status_code != 200:
        raise Exception(f"Gemini error {r.status_code}: {r.text[:200]}")
    return r.json()['candidates'][0]['content']['parts'][0]['text']

def detect_type(text):
    t = text.lower()
    if re.search(r'\b(code|script|python|javascript|js|html|css|api|bot|develop|bug|fix|app|program|automat|wordpress|shopify)\b', t):
        return 'coding'
    if re.search(r'\b(research|find|gather|data|list|report|analy|summariz|survey|compil|translat)\b', t):
        return 'research'
    return 'writing'

def extract_pay(text):
    # Exclude salary/commission patterns
    if re.search(r'per (month|year|deal|hour|hr)\b|\bsalar|\bcommission\b|\bannual', text, re.I):
        return None
    matches = re.findall(r'\$\s*(\d+(?:,\d+)?(?:\.\d+)?)', text)
    if not matches:
        return None
    vals = [float(m.replace(',', '')) for m in matches if 0 < float(m.replace(',', '')) < 5000]
    return f"${max(vals):.0f}" if vals else None

def send_email(subject, body):
    if not GMAIL_USER or not GMAIL_PASS:
        print("Email not configured")
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
    rejected = {'job_listing': 0, 'undoable': 0, 'no_deliverable': 0}

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

            doable, reason = is_good_task(title, desc)
            if not doable:
                if reason in rejected:
                    rejected[reason] += 1
                continue

            pay = extract_pay(title + ' ' + desc)
            tasks.append({
                'id': pid, 'redditId': pid,
                'title': re.sub(r'^\[(h|hiring|for hire|offer)\]\s*', '', title, flags=re.I).strip(),
                'description': desc or 'No description.',
                'pay': pay,
                'type': detect_type(title + ' ' + desc),
                'source': source, 'url': link,
                'createdAt': int(time.time()),
                'status': 'inbox',
                'fetchedAt': int(datetime.datetime.utcnow().timestamp() * 1000)
            })
        except Exception as e:
            print(f"Parse error: {e}")

    print(f"  Rejected: {rejected['job_listing']} job listings, {rejected['undoable']} undoable, {rejected['no_deliverable']} no deliverable")
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
            print(f"  {len(fresh)} new tasks accepted")
            for t in fresh:
                new_tasks[t['id']] = t
                pay_val = float(t['pay'].replace('$', '').replace(',', '')) if t.get('pay') else 0
                if pay_val >= MIN_PAY_ALERT:
                    high_pay_tasks.append(t)
        except Exception as e:
            print(f"Error {source}: {e}")

    if new_tasks:
        for pid, task in new_tasks.items():
            fb(f'/tasks/{pid}', 'PUT', task)
        print(f"Pushed {len(new_tasks)} tasks to Firebase")

    if high_pay_tasks:
        rows = ''.join([
            f'<tr><td style="padding:10px;color:#f0a500;font-weight:bold">{t["pay"]}</td>'
            f'<td style="padding:10px">{t["title"][:60]}</td>'
            f'<td style="padding:10px"><a href="{t["url"]}" style="color:#3d9eff">View</a></td></tr>'
            for t in high_pay_tasks
        ])
        send_email(
            f"⚡ TASKFORCE: {len(high_pay_tasks)} task(s) paying ${MIN_PAY_ALERT}+",
            f'<div style="background:#0a0b0d;color:#c8cdd8;padding:20px;font-family:monospace">'
            f'<h2 style="color:#f0a500">⚡ High-Pay Tasks Available</h2>'
            f'<table style="width:100%">{rows}</table>'
            f'<p><a href="https://nadavw9.github.io/taskforce" style="color:#00e5a0">Open Dashboard →</a></p></div>'
        )

# ── PROMPTS BY TASK TYPE ──────────────────────────────────────────────────────
PROMPTS = {
    'writing': """You are a professional freelance writer completing a paid client task.
Write compelling, ready-to-submit content that matches the client's exact needs.
Rules:
- Match the tone and style the client asked for
- Be specific and detailed, not generic
- Deliver complete, polished work — not an outline
- Do NOT add meta-commentary like "Here is your content:" — just deliver the content

Task: {title}
Details: {desc}
Budget: {pay}""",

    'research': """You are a professional researcher completing a paid client task.
Deliver thorough, accurate, well-organized findings.
Rules:
- Use clear headings and structure
- Be specific with facts — avoid vague generalities
- Cite sources by name when referencing known data
- Deliver a complete report, not a summary of what you would research

Task: {title}
Details: {desc}
Budget: {pay}""",

    'coding': """You are an expert developer completing a paid client task.
Write complete, working, well-commented code.
Rules:
- Provide the full implementation — no placeholders or TODOs
- Include clear usage instructions at the top
- Add comments explaining key sections
- If multiple files are needed, clearly separate them

Task: {title}
Details: {desc}
Budget: {pay}"""
}

def execute_tasks():
    tasks = fb('/tasks') or {}
    approved = {k: v for k, v in tasks.items()
                if isinstance(v, dict) and v.get('status') == 'approved'}
    print(f"Approved tasks to execute: {len(approved)}")

    for key, task in approved.items():
        title = task.get('title', '')
        desc = task.get('description', '')
        pay = task.get('pay', 'negotiable')
        task_type = task.get('type', 'writing')

        print(f"Executing [{task_type}]: {title[:50]}")
        fb(f'/tasks/{key}', 'PATCH', {'status': 'executing'})

        try:
            prompt = PROMPTS.get(task_type, PROMPTS['writing']).format(
                title=title, desc=desc, pay=pay
            )
            output = gemini(prompt)

            # Basic quality check — if output is too short, retry with more detail
            if len(output.strip()) < 200:
                print(f"  Output too short ({len(output)} chars), retrying...")
                output = gemini(prompt + "\n\nIMPORTANT: Provide a detailed, complete response of at least 400 words.")

            fb(f'/tasks/{key}', 'PATCH', {
                'status': 'done',
                'output': output,
                'model': GEMINI_MODEL,
                'completedAt': datetime.datetime.utcnow().isoformat()
            })
            print(f"  Done ({len(output)} chars)")

            # Email notification
            if GMAIL_USER and GMAIL_PASS:
                send_email(
                    f"✅ Task done: {title[:50]}",
                    f'<div style="background:#0a0b0d;color:#c8cdd8;padding:20px;font-family:monospace">'
                    f'<h2 style="color:#00e5a0">✅ Task Completed</h2>'
                    f'<p style="color:#f0a500;font-size:16px">{title}</p>'
                    f'<p>Pay: <strong style="color:#00e5a0">{pay}</strong> | Type: {task_type}</p>'
                    f'<p><a href="{task.get("url","")}" style="color:#3d9eff">Original post ↗</a></p>'
                    f'<a href="https://nadavw9.github.io/taskforce" style="color:#00e5a0">Open dashboard to copy & send →</a></div>'
                )

        except Exception as e:
            fb(f'/tasks/{key}', 'PATCH', {'status': 'error', 'error': str(e)})
            print(f"  Error: {e}")

print("=== TaskForce Bot v3 Starting ===")
print(f"Model: {GEMINI_MODEL}")
fetch_reddit()
execute_tasks()
print("=== Bot Done ===")
