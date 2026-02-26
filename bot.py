import requests, os, datetime, re, time

FIREBASE = os.environ['FIREBASE_URL'].rstrip('/')
GEMINI_KEY = os.environ['GEMINI_KEY']
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"

FEEDS = [
    ('r/forhire', 'https://www.reddit.com/r/forhire/new/.rss?limit=50'),
    ('r/slavelabour', 'https://www.reddit.com/r/slavelabour/new/.rss?limit=50'),
]
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; RSS reader)',
    'Accept': 'application/rss+xml, application/xml, text/xml'
}

def fb(path, method='GET', data=None):
    url = f"{FIREBASE}{path}.json"
    r = requests.request(method, url, json=data, headers={'Content-Type':'application/json'}, timeout=15)
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
    if re.search(r'\b(code|script|python|javascript|js|html|css|api|bot|develop|bug|fix|app|program)\b', t):
        return 'coding'
    if re.search(r'\b(research|find|gather|data|list|report|analy|summariz|survey|compil)\b', t):
        return 'research'
    return 'writing'

def extract_pay(text):
    matches = re.findall(r'\$\s*(\d+(?:,\d+)?(?:\.\d+)?)', text)
    if not matches:
        return None
    vals = [float(m.replace(',','')) for m in matches if 0 < float(m.replace(',','')) < 50000]
    return f"${max(vals):.0f}" if vals else None

def parse_rss(xml, source):
    tasks = []
    for item in re.split(r'<entry>|<item>', xml)[1:]:
        try:
            title_m = re.search(r'<title[^>]*><!\[CDATA\[(.*?)\]\]></title>', item, re.S) or re.search(r'<title[^>]*>(.*?)</title>', item, re.S)
            link_m = re.search(r'<link>(.*?)</link>', item, re.S) or re.search(r'<guid[^>]*>(.*?)</guid>', item, re.S)
            desc_m = re.search(r'<description[^>]*><!\[CDATA\[(.*?)\]\]></description>', item, re.S) or re.search(r'<description[^>]*>(.*?)</description>', item, re.S)
            title = title_m.group(1).strip() if title_m else ''
            link = link_m.group(1).strip() if link_m else ''
            desc = re.sub(r'<[^>]+>', '', desc_m.group(1) if desc_m else '').strip()[:600]
            id_m = re.search(r'/comments/([a-z0-9]+)/', link)
            pid = id_m.group(1) if id_m else re.sub(r'[^a-z0-9]', '', title.lower())[:12]
            if not title or not pid:
                continue
            if 'forhire' in source and not re.search(r'\[h|\bhiring\b', title, re.I):
                continue
            tasks.append({
                'id': pid, 'redditId': pid,
                'title': re.sub(r'^\[(h|hiring|for hire)\]\s*', '', title, flags=re.I).strip(),
                'description': desc or 'No description.',
                'pay': extract_pay(title+' '+desc),
                'type': detect_type(title+' '+desc),
                'source': source, 'url': link,
                'createdAt': int(time.time()),
                'status': 'inbox',
                'fetchedAt': int(datetime.datetime.utcnow().timestamp()*1000)
            })
        except Exception as e:
            print(f"Parse error: {e}")
    return tasks

def fetch_reddit():
    existing = fb('/tasks') or {}
    existing_ids = set(existing.keys())
    new_tasks = {}
    for source, feed_url in FEEDS:
        try:
            r = requests.get(feed_url, headers=HEADERS, timeout=20)
            print(f"{source} RSS: {r.status_code}")
            if not r.ok:
                continue
            tasks = parse_rss(r.text, source)
            fresh = [t for t in tasks if t['id'] not in existing_ids]
            print(f"{source}: {len(fresh)} new tasks")
            for t in fresh:
                new_tasks[t['id']] = t
        except Exception as e:
            print(f"Error {source}: {e}")
    if new_tasks:
        for pid, task in new_tasks.items():
            fb(f'/tasks/{pid}', 'PUT', task)
        print(f"Pushed {len(new_tasks)} tasks to Firebase")
    else:
        print("No new tasks")

def execute_tasks():
    tasks = fb('/tasks') or {}
    approved = {k: v for k, v in tasks.items() if isinstance(v, dict) and v.get('status') == 'approved'}
    print(f"Approved tasks to execute: {len(approved)}")
    for key, task in approved.items():
        fb(f'/tasks/{key}', 'PATCH', {'status': 'executing'})
        try:
            prompts = {
                'coding': 'You are an expert developer. Write complete working code:',
                'research': 'You are a researcher. Provide thorough accurate findings:',
                'writing': 'You are a copywriter. Write compelling ready-to-submit content:'
            }
            t = task.get('type','writing')
            prompt = f"{prompts.get(t,prompts['writing'])}\n\nTask: {task.get('title','')}\n\nDetails: {task.get('description','')}\n\nPay: {task.get('pay','negotiable')}"
            output = gemini(prompt)
            fb(f'/tasks/{key}', 'PATCH', {'status':'done','output':output,'completedAt':datetime.datetime.utcnow().isoformat()})
            print(f"Done: {task.get('title','?')[:40]}")
        except Exception as e:
            fb(f'/tasks/{key}', 'PATCH', {'status':'error','error':str(e)})
            print(f"Error: {e}")

print("=== TaskForce Bot Starting ===")
fetch_reddit()
execute_tasks()
print("=== Bot Done ===")
