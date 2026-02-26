import requests, json, os, datetime

FIREBASE = os.environ['FIREBASE_URL'].rstrip('/')
GEMINI_KEY = os.environ['GEMINI_KEY']
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"

def fb(path, method='GET', data=None):
    url = f"{FIREBASE}{path}.json"
    headers = {'Content-Type': 'application/json'}
    r = requests.request(method, url, json=data, headers=headers, timeout=15)
    if r.status_code == 200:
        return r.json()
    print(f"Firebase error {r.status_code}: {r.text}")
    return None

def gemini(prompt):
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    r = requests.post(GEMINI_URL, json=body, timeout=30)
    data = r.json()
    return data['candidates'][0]['content']['parts'][0]['text']

def run():
    print("Bot starting...")
    tasks = fb('/tasks') or {}
    approved = {k: v for k, v in tasks.items() if isinstance(v, dict) and v.get('status') == 'approved'}
    print(f"Found {len(approved)} approved tasks")

    for key, task in approved.items():
        print(f"Working on: {task.get('title','?')[:50]}")
        fb(f'/tasks/{key}', 'PATCH', {'status': 'executing'})
        try:
            prompts = {
                'coding': 'You are an expert developer. Write complete, working, well-commented code for this task. Deliver ready-to-use code:',
                'research': 'You are a professional researcher. Provide thorough, organized, accurate findings for this task:',
                'writing': 'You are an expert copywriter. Write compelling, professional content for this task. Deliver ready-to-submit work:'
            }
            t = task.get('type', 'writing')
            system = prompts.get(t, prompts['writing'])
            prompt = f"{system}\n\nTask: {task.get('title','')}\n\nDetails: {task.get('description','')}\n\nPay: {task.get('pay','negotiable')}"
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

run()
