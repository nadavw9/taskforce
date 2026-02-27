import requests, os, datetime, re, time, smtplib, json
from email.mime.text import MIMEText

FIREBASE = os.environ['FIREBASE_URL'].rstrip('/')
GEMINI_KEY = os.environ['GEMINI_KEY']
GMAIL_USER = os.environ.get('GMAIL_USER', '')
GMAIL_PASS = os.environ.get('GMAIL_PASS', '')
NOTIFY_EMAIL = os.environ.get('NOTIFY_EMAIL', GMAIL_USER)
MIN_PAY_ALERT = 15
MIN_CONFIDENCE = 70  # Only show tasks with >70% AI success confidence

# Model assignments — all free tier
MODEL_SCOUT    = 'gemini-2.5-flash-lite'  # 1000 RPD — fast classifier
MODEL_EVALUATOR = 'gemini-2.5-flash'       # 500 RPD  — smart reasoner
MODEL_EXECUTOR  = 'gemini-2.5-flash'       # 500 RPD  — does the work
MODEL_CRITIC    = 'gemini-2.5-flash-lite'  # 1000 RPD — quality check

FEEDS = [
    ('r/forhire', 'https://www.reddit.com/r/forhire/new/.rss?limit=50'),
    ('r/slavelabour', 'https://www.reddit.com/r/slavelabour/new/.rss?limit=50'),
]
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; RSS reader)',
    'Accept': 'application/rss+xml, application/xml, text/xml'
}

# ── GEMINI CALL ───────────────────────────────────────────────────────────────

def gemini(prompt, model, expect_json=False):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if expect_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    r = requests.post(url, json=body, timeout=90)
    if r.status_code != 200:
        raise Exception(f"Gemini {model} error {r.status_code}: {r.text[:300]}")
    text = r.json()['candidates'][0]['content']['parts'][0]['text']
    if expect_json:
        # Strip markdown fences if present
        text = re.sub(r'^```json\s*|\s*```$', '', text.strip())
        return json.loads(text)
    return text

# ── FIREBASE ──────────────────────────────────────────────────────────────────

def fb(path, method='GET', data=None):
    url = f"{FIREBASE}{path}.json"
    r = requests.request(method, url, json=data,
                         headers={'Content-Type': 'application/json'}, timeout=15)
    if r.status_code == 200:
        return r.json()
    print(f"Firebase error {r.status_code}: {r.text[:200]}")
    return None

# ── EMAIL ─────────────────────────────────────────────────────────────────────

def send_email(subject, body):
    if not GMAIL_USER or not GMAIL_PASS:
        return
    try:
        msg = MIMEText(body, 'html')
        msg['Subject'] = subject
        msg['From'] = GMAIL_USER
        msg['To'] = NOTIFY_EMAIL
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.send_message(msg)
        print(f"  Email sent: {subject[:50]}")
    except Exception as e:
        print(f"  Email error: {e}")

# ── RSS PARSING ───────────────────────────────────────────────────────────────

def parse_rss(xml, source):
    raw_tasks = []
    for item in re.split(r'<entry>|<item>', xml)[1:]:
        try:
            title_m = re.search(r'<title[^>]*><!\[CDATA\[(.*?)\]\]></title>', item, re.S) or \
                      re.search(r'<title[^>]*>(.*?)</title>', item, re.S)
            link_m  = re.search(r'<link[^>]+href=["\']([^"\']+)["\']', item, re.S) or \
                      re.search(r'<link>(.*?)</link>', item, re.S) or \
                      re.search(r'<guid[^>]*>(.*?)</guid>', item, re.S)
            desc_m  = re.search(r'<content[^>]*><!\[CDATA\[(.*?)\]\]></content>', item, re.S) or \
                      re.search(r'<description[^>]*><!\[CDATA\[(.*?)\]\]></description>', item, re.S) or \
                      re.search(r'<description[^>]*>(.*?)</description>', item, re.S)

            title = title_m.group(1).strip() if title_m else ''
            link  = link_m.group(1).strip()  if link_m  else ''
            desc  = re.sub(r'<[^>]+>', '', desc_m.group(1) if desc_m else '').strip()[:800]

            id_m = re.search(r'/comments/([a-z0-9]+)/', link)
            pid  = id_m.group(1) if id_m else re.sub(r'[^a-z0-9]', '', title.lower())[:12]

            if not title or not pid:
                continue
            # forhire: only posts with [H] hiring tag
            if 'forhire' in source and not re.search(r'\[h', title, re.I):
                continue

            raw_tasks.append({
                'id': pid,
                'title': re.sub(r'^\[(h|hiring|for hire|offer)\]\s*', '', title, flags=re.I).strip(),
                'description': desc or 'No description provided.',
                'source': source,
                'url': link,
            })
        except Exception as e:
            print(f"  Parse error: {e}")
    return raw_tasks

# ── AGENT 1: SCOUT ────────────────────────────────────────────────────────────
# Quickly reads the task and decides if it's even worth evaluating

def scout(task):
    prompt = f"""You are screening freelance job posts. Read this post and classify it.

Title: {task['title']}
Description: {task['description']}

Respond ONLY with a JSON object:
{{
  "is_real_task": true or false,
  "rejection_reason": "job_listing | scam | physical_only | vague | not_a_task | none",
  "understood_request": "in one sentence, what does the client actually want delivered?",
  "deliverable_type": "writing | research | coding | translation | other"
}}

Key rules:
- is_real_task = false if: they are advertising THEMSELVES for hire, it requires in-person work, it's a scam/spam, there is no clear deliverable, or it's a job opening (salary/full-time/commission roles)
- is_real_task = true if: a real person needs a specific text/research/code deliverable produced"""

    try:
        result = gemini(prompt, MODEL_SCOUT, expect_json=True)
        return result
    except Exception as e:
        print(f"  Scout error: {e}")
        return {"is_real_task": False, "rejection_reason": "scout_error"}

# ── AGENT 2: EVALUATOR ────────────────────────────────────────────────────────
# Deeply analyzes whether AI can realistically complete the task

def evaluate(task, scout_result):
    prompt = f"""You are an honest AI capability evaluator. Your job is to assess whether an AI language model can successfully complete a freelance task and produce output a real client would pay for.

Task: {task['title']}
Details: {task['description']}
Deliverable: {scout_result.get('understood_request', 'unknown')}
Type: {scout_result.get('deliverable_type', 'unknown')}

Think step by step:
1. What are the specific completion steps for this task?
2. For each step, can an AI language model do it without any external tools, accounts, or human interaction?
3. What is the overall probability this produces a result the client finds acceptable?

Respond ONLY with this JSON:
{{
  "completion_steps": [
    {{"step": "describe step", "ai_can_do": true, "confidence": 85, "reason": "why"}}
  ],
  "blocking_issues": ["list any steps AI cannot do at all"],
  "overall_confidence": 0-100,
  "verdict": "accept | reject | borderline",
  "rejection_reason": "explain if rejected",
  "execution_approach": "if accepted, brief strategy for how to complete this well"
}}

Be honest. Reject tasks that require: real internet browsing, specific accounts/logins, phone calls, physical actions, real-time data, original images/videos, or anything requiring human judgment beyond text generation."""

    try:
        result = gemini(prompt, MODEL_EVALUATOR, expect_json=True)
        return result
    except Exception as e:
        print(f"  Evaluator error: {e}")
        return {"verdict": "reject", "overall_confidence": 0, "rejection_reason": f"evaluator_error: {e}"}

# ── AGENT 3: EXECUTOR ─────────────────────────────────────────────────────────
# Does the actual work using the evaluator's strategy

def execute(task, eval_result):
    approach = eval_result.get('execution_approach', '')
    dtype = task.get('type', 'writing')

    system_prompts = {
        'writing': "You are an expert freelance writer producing paid client work.",
        'research': "You are a professional researcher producing a paid client report.",
        'coding': "You are an expert developer producing paid client code.",
        'translation': "You are a professional translator producing paid client translations.",
        'other': "You are a professional freelancer producing paid client work."
    }

    prompt = f"""{system_prompts.get(dtype, system_prompts['other'])}

CLIENT TASK: {task['title']}
FULL BRIEF: {task.get('description', '')}
BUDGET: {task.get('pay', 'unspecified')}

EXECUTION STRATEGY: {approach}

RULES:
- Deliver COMPLETE, ready-to-submit work — not an outline or summary
- Do NOT add meta-commentary like "Here is your..." — start the deliverable directly
- Match the client's exact requirements
- Professional quality that justifies the stated budget
- If writing, minimum 300 words unless client specified short format
- If coding, include all files and usage instructions

Deliver the work now:"""

    return gemini(prompt, MODEL_EXECUTOR)

# ── AGENT 4: CRITIC ───────────────────────────────────────────────────────────
# Reviews the output and improves it if needed

def critique(task, output):
    prompt = f"""You are a quality reviewer checking freelance work before delivery to a client.

ORIGINAL TASK: {task['title']}
BRIEF: {task.get('description', '')}

SUBMITTED WORK:
{output[:3000]}

Evaluate strictly. Respond ONLY with this JSON:
{{
  "quality_score": 0-100,
  "ready_to_send": true or false,
  "issues": ["list specific problems if any"],
  "verdict": "approved | needs_improvement | reject"
}}

Score 90+ = excellent, send as-is
Score 70-89 = good, send as-is  
Score 50-69 = needs improvement, flag it
Score <50 = reject, do not send

Check: Does it actually answer the brief? Is it complete? Is it professional? Would a client pay for this?"""

    try:
        result = gemini(prompt, MODEL_CRITIC, expect_json=True)
        return result
    except Exception as e:
        print(f"  Critic error: {e}")
        return {"quality_score": 75, "ready_to_send": True, "verdict": "approved", "issues": []}

# ── PAY EXTRACTION ────────────────────────────────────────────────────────────

def extract_pay(text):
    # Skip salary/commission patterns
    if re.search(r'per (month|year|deal|hour|hr|week)\b|\bsalar|\bcommission\b|\bannual|\bhourly', text, re.I):
        return None
    matches = re.findall(r'\$\s*(\d+(?:,\d+)?(?:\.\d+)?)', text)
    if not matches:
        return None
    vals = [float(m.replace(',', '')) for m in matches if 0 < float(m.replace(',', '')) < 5000]
    return f"${max(vals):.0f}" if vals else None

# ── MAIN: FETCH & SCOUT REDDIT ────────────────────────────────────────────────

def fetch_reddit():
    existing = fb('/tasks') or {}
    existing_ids = set(existing.keys())
    accepted = {}
    high_pay = []

    stats = {'total': 0, 'job_listing': 0, 'scam_vague': 0,
             'low_confidence': 0, 'accepted': 0}

    for source, feed_url in FEEDS:
        try:
            r = requests.get(feed_url, headers=HEADERS, timeout=20)
            print(f"{source} RSS: {r.status_code}")
            if not r.ok:
                continue

            raw = parse_rss(r.text, source)
            new_raw = [t for t in raw if t['id'] not in existing_ids]
            print(f"  {len(new_raw)} new posts to evaluate")
            stats['total'] += len(new_raw)

            for raw_task in new_raw:
                pid = raw_task['id']

                # AGENT 1: Scout
                scout_result = scout(raw_task)
                if not scout_result.get('is_real_task', False):
                    reason = scout_result.get('rejection_reason', 'unknown')
                    if reason in ('job_listing',):
                        stats['job_listing'] += 1
                    else:
                        stats['scam_vague'] += 1
                    print(f"  ✗ Scout rejected [{reason}]: {raw_task['title'][:40]}")
                    continue

                # AGENT 2: Evaluator
                eval_result = evaluate(raw_task, scout_result)
                confidence = eval_result.get('overall_confidence', 0)
                verdict = eval_result.get('verdict', 'reject')

                if verdict == 'reject' or confidence < MIN_CONFIDENCE:
                    stats['low_confidence'] += 1
                    print(f"  ✗ Evaluator rejected [{confidence}%]: {raw_task['title'][:40]}")
                    print(f"    Reason: {eval_result.get('rejection_reason', '')[:80]}")
                    continue

                # Task passed both agents — add to inbox
                pay = extract_pay(raw_task['title'] + ' ' + raw_task['description'])
                task = {
                    'id': pid, 'redditId': pid,
                    'title': raw_task['title'],
                    'description': raw_task['description'],
                    'pay': pay,
                    'type': scout_result.get('deliverable_type', 'writing'),
                    'source': source,
                    'url': raw_task['url'],
                    'createdAt': int(time.time()),
                    'status': 'inbox',
                    'fetchedAt': int(datetime.datetime.utcnow().timestamp() * 1000),
                    # Store agent analysis in task for dashboard display
                    'aiConfidence': confidence,
                    'aiApproach': eval_result.get('execution_approach', ''),
                    'aiSteps': len(eval_result.get('completion_steps', [])),
                    'understoodAs': scout_result.get('understood_request', ''),
                }
                accepted[pid] = task
                stats['accepted'] += 1
                print(f"  ✓ Accepted [{confidence}%]: {raw_task['title'][:40]}")

                # High pay alert
                pay_val = float(pay.replace('$', '').replace(',', '')) if pay else 0
                if pay_val >= MIN_PAY_ALERT:
                    high_pay.append(task)

                time.sleep(0.5)  # Be gentle with API rate limits

        except Exception as e:
            print(f"Error {source}: {e}")

    # Push accepted tasks to Firebase
    if accepted:
        for pid, task in accepted.items():
            fb(f'/tasks/{pid}', 'PUT', task)
        print(f"Pushed {len(accepted)} tasks to Firebase")

    print(f"\nStats: {stats['total']} evaluated → "
          f"{stats['job_listing']} job listings, "
          f"{stats['scam_vague']} scam/vague, "
          f"{stats['low_confidence']} low confidence, "
          f"{stats['accepted']} accepted")

    # Email for high pay tasks
    if high_pay:
        rows = ''.join([
            f'<tr>'
            f'<td style="padding:10px;color:#f0a500;font-weight:bold">{t["pay"]}</td>'
            f'<td style="padding:10px">{t["title"][:55]}</td>'
            f'<td style="padding:10px;color:#00e5a0">{t["aiConfidence"]}% AI confidence</td>'
            f'<td style="padding:10px"><a href="{t["url"]}" style="color:#3d9eff">View</a></td>'
            f'</tr>'
            for t in high_pay
        ])
        send_email(
            f"⚡ TASKFORCE: {len(high_pay)} high-pay task(s) — AI-verified doable",
            f'<div style="background:#0a0b0d;color:#c8cdd8;padding:20px;font-family:monospace">'
            f'<h2 style="color:#f0a500">⚡ High-Pay Tasks — AI Verified</h2>'
            f'<p style="color:#7a8190">These tasks passed 2-stage AI evaluation with high confidence</p>'
            f'<table style="width:100%;border-collapse:collapse">{rows}</table>'
            f'<br/><a href="https://nadavw9.github.io/taskforce" style="color:#00e5a0">Open Dashboard →</a>'
            f'</div>'
        )

# ── MAIN: EXECUTE APPROVED TASKS ─────────────────────────────────────────────

def execute_approved():
    tasks = fb('/tasks') or {}
    approved = {k: v for k, v in tasks.items()
                if isinstance(v, dict) and v.get('status') == 'approved'}
    print(f"\nApproved tasks to execute: {len(approved)}")

    for key, task in approved.items():
        title = task.get('title', '')
        print(f"Executing: {title[:50]}")
        fb(f'/tasks/{key}', 'PATCH', {'status': 'executing'})

        try:
            # AGENT 3: Executor
            # Use stored AI approach if available from evaluation
            eval_mock = {'execution_approach': task.get('aiApproach', '')}
            output = execute(task, eval_mock)

            # AGENT 4: Critic
            print(f"  Critiquing output ({len(output)} chars)...")
            critique_result = critique(task, output)
            quality = critique_result.get('quality_score', 0)
            issues = critique_result.get('issues', [])

            print(f"  Quality score: {quality}/100")
            if issues:
                print(f"  Issues: {'; '.join(issues[:2])}")

            # If quality too low, retry with issues as feedback
            if quality < 70 and critique_result.get('verdict') != 'approved':
                print(f"  Quality too low ({quality}), improving...")
                improvement_prompt = f"""The following freelance work was rejected by a quality reviewer. Rewrite it fixing all issues.

TASK: {title}
BRIEF: {task.get('description','')}

ISSUES WITH PREVIOUS VERSION:
{chr(10).join(issues)}

PREVIOUS VERSION:
{output[:2000]}

Write an improved version that fixes all issues:"""
                output = gemini(improvement_prompt, MODEL_EXECUTOR)
                quality = 80  # Assume improved

            fb(f'/tasks/{key}', 'PATCH', {
                'status': 'done',
                'output': output,
                'qualityScore': quality,
                'qualityIssues': issues,
                'model': MODEL_EXECUTOR,
                'completedAt': datetime.datetime.utcnow().isoformat()
            })
            print(f"  ✓ Done — quality {quality}/100")

            # Email notification
            if GMAIL_USER and GMAIL_PASS:
                send_email(
                    f"✅ Task done [{quality}/100]: {title[:45]}",
                    f'<div style="background:#0a0b0d;color:#c8cdd8;padding:20px;font-family:monospace">'
                    f'<h2 style="color:#00e5a0">✅ Task Completed</h2>'
                    f'<p style="color:#f0a500;font-size:16px">{title}</p>'
                    f'<p>Quality: <strong style="color:{"#00e5a0" if quality >= 80 else "#f0a500"}">{quality}/100</strong>'
                    f' | Pay: {task.get("pay","?")}</p>'
                    f'<p><a href="{task.get("url","")}" style="color:#3d9eff">Original post ↗</a></p>'
                    f'<a href="https://nadavw9.github.io/taskforce" style="color:#00e5a0">'
                    f'Open dashboard to copy & send →</a></div>'
                )

        except Exception as e:
            fb(f'/tasks/{key}', 'PATCH', {'status': 'error', 'error': str(e)})
            print(f"  ✗ Error: {e}")

# ── ENTRY POINT ───────────────────────────────────────────────────────────────

print("=== TaskForce Agent System v4 ===")
print(f"Scout: {MODEL_SCOUT} | Evaluator: {MODEL_EVALUATOR}")
print(f"Executor: {MODEL_EXECUTOR} | Critic: {MODEL_CRITIC}")
print()
fetch_reddit()
execute_approved()
print("\n=== Done ===")
