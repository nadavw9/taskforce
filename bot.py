import requests, os, datetime, re

FIREBASE = os.environ['FIREBASE_URL'].rstrip('/')
GEMINI_KEY = os.environ['GEMINI_KEY']
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"
HEADERS = {'User-Agent': 'TaskForceBot/1.0 (task automation)'}

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
    data = r.json()
    return data['candidates'][0]['content']['parts'][0]['text']

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

def fetch_reddit():
    tasks = {}
    existing = fb('/tasks') or {}
    existing_ids = set(existing.keys())

    for sub in ['forhire', 'slavelabour']:
        try:
            url = f"https://www.reddit.com/r/{sub}/new.json?limit=50&raw_json=1"
            r = requests.get(url, headers=HEADERS, timeout=15)
            if not r.ok:
                print(f"Reddit {sub} error: {r.status_code}")
                continue
            posts = r.json().get('data', {}).get('children', [])
            count = 0
            for p in posts:
                d = p['data']
                pid = d['id']
                if pid in existing_ids or d.get('over_18'):
                    continue
                title = d.get('title', '')
                if sub == 'forhire' and not re.search(r'\[h', title, re.I):
                    continue
                body = d.get('selftext', '')
                text = title + ' ' + body
                task = {
                    'id': pid,
                    'redditId': pid,
                    'title': re.sub(r'^\[(h|hiring|for hire)\]\s*', '', title, flags=re.I).strip(),
                    'description': body[:600] or 'No description.',
                    'pay': extract_pay(text),
                    'type': detect_type(text),
                    'source': f'r/{sub}',
                    'url': f"https://reddit.com{d.get('permalink','')}",
                    'createdAt': d.get('created_utc', 0),
                    'status': 'inbox',
                    'fetchedAt': int(datetime.datetime.utcnow().timestamp() * 1000)
                }
                tasks[pid] = task
                count += 1
            print(f"r/{sub}: {count} new tasks")
        except Exception as e:
            print(f"Reddit fetch error r/{sub}: {e}")

    if tasks:
        for pid, task in tasks.items():
            fb(f'/tasks/{pid}', 'PUT', task)
        print(f"Pushed {len(tasks)} new tasks to Firebase")
    else:
        print("No new tasks found on Reddit")

def execute_tasks():
    tasks = fb('/tasks') or {}
    approved = {k: v for k, v in tasks.items() if isinstance(v, dict) and v.get('status') == 'approved'}
    print(f"Found {len(approved)} approved tasks to execute")

    for key, task in approved.items():
        print(f"Executing: {task.get('title','?')[:50]}")
        fb(f'/tasks/{key}', 'PATCH', {'status': 'executing'})
        try:
            prompts = {
                'coding': 'You are an expert developer. Write complete, working, well-commented code:',
                'research': 'You are a professional researcher. Provide thorough, accurate findings:',
                'writing': 'You are an expert copywriter. Write compelling, ready-to-submit content:'
            }
            t = task.get('type', 'writing')
            prompt = f"{prompts.get(t, prompts['writing'])}\n\nTask: {task.get('title','')}\n\nDetails: {task.get('description','')}\n\nPay: {task.get('pay','negotiable')}"
            output = gemini(prompt)
            fb(f'/tasks/{key}', 'PATCH', {
                'status': 'done',
                'output': output,
                'completedAt': datetime.datetime.utcnow().isoformat()
            })
            print(f"Done: {task.get('title','?')[:40]}")
        except Exception as e:
            fb(f'/tasks/{key}', 'PATCH', {'status': 'error', 'error': str(e)})
            print(f"Error: {e}")

print("=== TaskForce Bot Starting ===")
fetch_reddit()
execute_tasks()
print("=== Bot Done ===")
