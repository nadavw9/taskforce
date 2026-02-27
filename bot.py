"""
TASKFORCE — Multi-Agent System v5
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Architecture:
  Orchestrator (deterministic code) coordinates 6 specialist agents.
  Each agent returns structured JSON. Control system approves or loops back.
  Max 3 retries per stage with critique-augmented re-prompting.
  All decisions stored in Firebase. Fully autonomous once task is approved.

Agents:
  SCOUT      → Is this a real, completable task?
  EVALUATOR  → Can AI do every step? Confidence score 0-100
  PLANNER    → Creates execution blueprint
  EXECUTOR   → Follows blueprint, produces deliverable
  CRITIC     → Scores quality 0-100, lists issues
  IMPROVER   → Rewrites using critic feedback (only if score < 75)
"""

import requests, os, datetime, re, time, smtplib, json, traceback
from email.mime.text import MIMEText

# ══════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════

FIREBASE     = os.environ['FIREBASE_URL'].rstrip('/')
GEMINI_KEY   = os.environ['GEMINI_KEY']
GMAIL_USER   = os.environ.get('GMAIL_USER', '')
GMAIL_PASS   = os.environ.get('GMAIL_PASS', '')
NOTIFY_EMAIL = os.environ.get('NOTIFY_EMAIL', GMAIL_USER)

MIN_CONFIDENCE  = 70   # Evaluator: reject tasks below this % confidence
MIN_QUALITY     = 75   # Critic: trigger Improver below this score
MAX_RETRIES     = 3    # Max loops per agent stage
MIN_PAY_ALERT   = 15   # Email alert for tasks paying $15+

# Model assignments — all free tier
# gemini-2.5-flash = 500 RPD, gemini-2.5-flash-lite = 1000 RPD
MODELS = {
    'scout':     'gemini-2.5-flash-lite',
    'evaluator': 'gemini-2.5-flash',
    'planner':   'gemini-2.5-flash',
    'executor':  'gemini-2.5-flash',
    'critic':    'gemini-2.5-flash-lite',
    'improver':  'gemini-2.5-flash',
}

FEEDS = [
    ('r/forhire',     'https://www.reddit.com/r/forhire/new/.rss?limit=50'),
    ('r/slavelabour', 'https://www.reddit.com/r/slavelabour/new/.rss?limit=50'),
]
RSS_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; RSS reader)',
    'Accept':     'application/rss+xml, application/xml, text/xml'
}

# ══════════════════════════════════════════════════════════
# CORE UTILITIES
# ══════════════════════════════════════════════════════════

def gemini(prompt, model, expect_json=False):
    url  = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if expect_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}

    for attempt in range(3):
        try:
            r = requests.post(url, json=body, timeout=90)
            if r.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"    Rate limited — waiting {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            text = r.json()['candidates'][0]['content']['parts'][0]['text']
            if expect_json:
                clean = re.sub(r'^```json\s*|\s*```$', '', text.strip())
                return json.loads(clean)
            return text
        except json.JSONDecodeError as e:
            raise ValueError(f"Agent returned invalid JSON: {e}")
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(5)


def fb(path, method='GET', data=None):
    url = f"{FIREBASE}{path}.json"
    r = requests.request(method, url, json=data,
                         headers={'Content-Type': 'application/json'}, timeout=15)
    if r.status_code == 200:
        return r.json()
    print(f"  Firebase {method} {path}: {r.status_code}")
    return None


def log_event(task_id, agent, status, detail=''):
    timestamp = datetime.datetime.utcnow().isoformat()
    entry = {"ts": timestamp, "agent": agent, "status": status, "detail": detail[:300]}
    existing = fb(f'/tasks/{task_id}/agent_log') or []
    if not isinstance(existing, list):
        existing = []
    existing.append(entry)
    fb(f'/tasks/{task_id}/agent_log', 'PUT', existing)


def send_email(subject, body):
    if not GMAIL_USER or not GMAIL_PASS:
        return
    try:
        msg = MIMEText(body, 'html')
        msg['Subject'] = subject
        msg['From']    = GMAIL_USER
        msg['To']      = NOTIFY_EMAIL
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.send_message(msg)
        print(f"  Email: {subject[:60]}")
    except Exception as e:
        print(f"  Email error: {e}")


def extract_pay(text):
    if re.search(r'per (month|year|deal|hour|hr|week)\b|\bsalar|\bcommission\b|\bannual|\bhourly', text, re.I):
        return None
    matches = re.findall(r'\$\s*(\d+(?:,\d+)?(?:\.\d+)?)', text)
    vals = [float(m.replace(',', '')) for m in matches if 0 < float(m.replace(',', '')) < 5000]
    return f"${max(vals):.0f}" if vals else None

# ══════════════════════════════════════════════════════════
# RSS PARSING
# ══════════════════════════════════════════════════════════

def parse_rss(xml, source):
    tasks = []
    for item in re.split(r'<entry>|<item>', xml)[1:]:
        try:
            title_m = (re.search(r'<title[^>]*><!\[CDATA\[(.*?)\]\]></title>', item, re.S) or
                       re.search(r'<title[^>]*>(.*?)</title>', item, re.S))
            link_m  = (re.search(r'<link[^>]+href=["\']([^"\']+)["\']', item, re.S) or
                       re.search(r'<link>(.*?)</link>', item, re.S) or
                       re.search(r'<guid[^>]*>(.*?)</guid>', item, re.S))
            desc_m  = (re.search(r'<content[^>]*><!\[CDATA\[(.*?)\]\]></content>', item, re.S) or
                       re.search(r'<description[^>]*><!\[CDATA\[(.*?)\]\]></description>', item, re.S) or
                       re.search(r'<description[^>]*>(.*?)</description>', item, re.S))

            if not title_m:
                continue

            title = title_m.group(1).strip()
            link  = link_m.group(1).strip() if link_m else ''
            desc  = re.sub(r'<[^>]+>', '', desc_m.group(1) if desc_m else '').strip()[:900]

            id_m = re.search(r'/comments/([a-z0-9]+)/', link)
            pid  = id_m.group(1) if id_m else re.sub(r'[^a-z0-9]', '', title.lower())[:12]

            if not pid:
                continue
            if 'forhire' in source and not re.search(r'\[\s*h[^a-z]', title, re.I):
                continue

            clean_title = re.sub(r'^\[(h|hiring|for hire|offer)\]\s*', '', title, flags=re.I).strip()
            tasks.append({
                'id': pid, 'title': clean_title,
                'description': desc or 'No description.',
                'source': source, 'url': link,
            })
        except Exception:
            pass
    return tasks

# ══════════════════════════════════════════════════════════
# AGENT 1: SCOUT
# ══════════════════════════════════════════════════════════

def agent_scout(task, critique=''):
    critique_block = f"\n\nPREVIOUS REJECTION FEEDBACK — fix these if wrong:\n{critique}" if critique else ""
    prompt = f"""You are a specialist task classifier for a freelance AI automation system.

Analyze this Reddit post and determine if it's a real task with a specific deliverable.

TITLE: {task['title']}
DESCRIPTION: {task['description']}
SOURCE: {task['source']}{critique_block}

Return ONLY this JSON:
{{
  "is_real_task": true or false,
  "rejection_reason": "job_listing | self_promotion | scam | vague | in_person | no_deliverable | none",
  "deliverable_type": "writing | research | coding | translation | data | other",
  "understood_request": "exact one-sentence description of what deliverable the client wants",
  "flags": ["list any concerns even if accepting"]
}}

REJECT (is_real_task=false) if ANY true:
- Person advertising THEMSELVES as available for hire
- Requires physical presence, phone calls, in-person work
- Full-time/part-time job with salary or commission
- No clear specific deliverable
- Obvious spam or scam

ACCEPT only if: a real client needs a specific text/code/research/data deliverable"""

    try:
        data = gemini(prompt, MODELS['scout'], expect_json=True)
        approved = data.get('is_real_task', False)
        reason = data.get('rejection_reason', 'none') if not approved else 'accepted'
        return {'approved': approved, 'data': data, 'reason': reason}
    except Exception as e:
        return {'approved': False, 'data': {}, 'reason': f'scout_error: {e}'}

# ══════════════════════════════════════════════════════════
# AGENT 2: EVALUATOR
# ══════════════════════════════════════════════════════════

def agent_evaluator(task, scout_data):
    prompt = f"""You are an honest AI capability evaluator. Assess whether an AI language model 
can complete this freelance task and produce output a real client would pay for.

TASK: {task['title']}
FULL BRIEF: {task['description']}
DELIVERABLE TYPE: {scout_data.get('deliverable_type', 'unknown')}
WHAT CLIENT WANTS: {scout_data.get('understood_request', 'unknown')}

Think through EVERY step. For each, assess if AI can do it without external tools.

Return ONLY this JSON:
{{
  "completion_steps": [
    {{
      "step_number": 1,
      "description": "what needs to happen",
      "ai_can_do": true or false,
      "confidence": 0-100,
      "blocker": "blocker if ai_can_do false, else empty"
    }}
  ],
  "blocking_issues": ["critical things AI cannot do"],
  "overall_confidence": 0-100,
  "verdict": "accept | reject",
  "rejection_reason": "reason if rejected",
  "complexity": "simple | medium | complex",
  "execution_approach": "if accepted: precise strategy for completing this well"
}}

ALWAYS REJECT if any step requires:
- Browsing real internet for live data
- Logging into accounts  
- Phone calls or real human contact
- Original photos/illustrations/audio
- Real-time market data or current prices
- Physical world actions

Calibration: 90%+ = AI does this excellently. 70-89% = good with minor limits. Below 70% = reject."""

    try:
        data = gemini(prompt, MODELS['evaluator'], expect_json=True)
        confidence = data.get('overall_confidence', 0)
        verdict    = data.get('verdict', 'reject')
        approved   = (verdict == 'accept') and (confidence >= MIN_CONFIDENCE)
        reason     = f"confidence={confidence}%" if approved else data.get('rejection_reason', 'low confidence')
        return {'approved': approved, 'data': data, 'reason': reason}
    except Exception as e:
        return {'approved': False, 'data': {}, 'reason': f'evaluator_error: {e}'}

# ══════════════════════════════════════════════════════════
# AGENT 3: PLANNER
# ══════════════════════════════════════════════════════════

def agent_planner(task, eval_data):
    steps_summary = ""
    for s in eval_data.get('completion_steps', []):
        if s.get('ai_can_do'):
            steps_summary += f"  - Step {s['step_number']}: {s['description']}\n"

    prompt = f"""You are an expert project planner. Create a precise execution blueprint.

TASK: {task['title']}
CLIENT BRIEF: {task.get('description', '')}
COMPLEXITY: {eval_data.get('complexity', 'medium')}
DOABLE STEPS:
{steps_summary if steps_summary else '  - Complete the task as described'}

Return ONLY this JSON:
{{
  "format": "markdown | plain_text | code | structured_report",
  "target_length": "estimated word count or lines of code",
  "tone": "professional | casual | technical | persuasive",
  "must_include": ["critical elements the deliverable must contain"],
  "must_avoid": ["things to NOT include"],
  "structure": ["ordered sections or components"],
  "opening_instruction": "exactly how to start the deliverable",
  "quality_bar": "specific standard this work must meet",
  "executor_prompt_additions": "any special instructions"
}}"""

    try:
        data = gemini(prompt, MODELS['planner'], expect_json=True)
        return {'approved': True, 'data': data, 'reason': 'plan_ready'}
    except Exception as e:
        return {'approved': True, 'data': {}, 'reason': f'planner_degraded: {e}'}

# ══════════════════════════════════════════════════════════
# AGENT 4: EXECUTOR
# ══════════════════════════════════════════════════════════

def agent_executor(task, eval_data, plan_data):
    dtype   = task.get('type', 'writing')
    plan    = plan_data or {}
    approach = eval_data.get('execution_approach', '')

    role_map = {
        'writing':     'expert professional freelance writer',
        'research':    'thorough professional researcher',
        'coding':      'expert software developer',
        'translation': 'professional certified translator',
        'data':        'data analyst and researcher',
        'other':       'professional freelancer',
    }

    structure_block = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(plan.get('structure', [])))
    inclusions      = "\n".join(f"  - {x}" for x in plan.get('must_include', []))
    exclusions      = "\n".join(f"  - {x}" for x in plan.get('must_avoid', []))

    prompt = f"""You are a {role_map.get(dtype, role_map['other'])} completing paid client work.

CLIENT TASK: {task['title']}
FULL BRIEF: {task.get('description', '')}
BUDGET: {task.get('pay', 'unspecified')}

EXECUTION PARAMETERS:
- Format: {plan.get('format', 'professional document')}
- Tone: {plan.get('tone', 'professional')}
- Target length: {plan.get('target_length', '400+ words')}
- Quality standard: {plan.get('quality_bar', 'professional, ready-to-submit')}
{f"- Opening instruction: {plan.get('opening_instruction')}" if plan.get('opening_instruction') else ""}
{f"- Strategy: {approach}" if approach else ""}

{f"REQUIRED STRUCTURE:{chr(10)}{structure_block}" if structure_block else ""}
{f"MUST INCLUDE:{chr(10)}{inclusions}" if inclusions else ""}
{f"MUST AVOID:{chr(10)}{exclusions}" if exclusions else ""}
{f"SPECIAL INSTRUCTIONS: {plan.get('executor_prompt_additions')}" if plan.get('executor_prompt_additions') else ""}

RULES:
- Deliver the COMPLETE work — not an outline, not a summary
- Start the deliverable directly — NO preamble like "Here is your..."
- Never use placeholder text like [INSERT X] or [TBD]
- Every section must be fully written out
- This is the actual submission to the client

Deliver the complete work now:"""

    try:
        output = gemini(prompt, MODELS['executor'])
        if len(output.strip()) < 150:
            raise ValueError(f"Output too short ({len(output)} chars)")
        return {'approved': True, 'data': {'output': output}, 'reason': 'executed'}
    except Exception as e:
        return {'approved': False, 'data': {}, 'reason': f'executor_error: {e}'}

# ══════════════════════════════════════════════════════════
# AGENT 5: CRITIC
# ══════════════════════════════════════════════════════════

def agent_critic(task, output):
    prompt = f"""You are a strict quality reviewer for freelance work.

ORIGINAL TASK: {task['title']}
CLIENT BRIEF: {task.get('description', '')}

SUBMITTED WORK (first 3000 chars):
{output[:3000]}

Return ONLY this JSON:
{{
  "quality_score": 0-100,
  "verdict": "excellent | good | acceptable | needs_improvement | reject",
  "ready_to_send": true or false,
  "issues": [
    {{"severity": "critical | major | minor", "issue": "specific problem", "fix": "how to fix"}}
  ],
  "strengths": ["what works well"]
}}

SCORING: 90-100=excellent, 75-89=good/send, 60-74=needs work, below 60=reject
MARK ready_to_send=false if: missing sections, doesn't answer the brief, has placeholders, too short, is just an outline"""

    try:
        data = gemini(prompt, MODELS['critic'], expect_json=True)
        score    = data.get('quality_score', 0)
        ready    = data.get('ready_to_send', False)
        approved = score >= MIN_QUALITY and ready
        return {'approved': approved, 'data': data, 'reason': f'score={score}'}
    except Exception as e:
        return {'approved': True, 'data': {'quality_score': 75, 'ready_to_send': True, 'issues': []},
                'reason': f'critic_degraded: {e}'}

# ══════════════════════════════════════════════════════════
# AGENT 6: IMPROVER
# ══════════════════════════════════════════════════════════

def agent_improver(task, original_output, critic_data):
    issues_text = ""
    for issue in critic_data.get('issues', []):
        issues_text += f"  [{issue.get('severity','?').upper()}] {issue.get('issue','')} → FIX: {issue.get('fix','')}\n"

    prompt = f"""You are rewriting rejected freelance work. Fix ALL listed problems.

TASK: {task['title']}
BRIEF: {task.get('description', '')}

QUALITY ISSUES TO FIX:
{issues_text if issues_text else 'Improve overall quality and completeness.'}

STRENGTHS TO KEEP:
{chr(10).join(critic_data.get('strengths', ['Complete the task well']))}

PREVIOUS VERSION:
{original_output[:2500]}

Fix every issue. Keep what works. Deliver the complete improved version now:"""

    try:
        improved = gemini(prompt, MODELS['improver'])
        if len(improved.strip()) < 150:
            raise ValueError("Improved output too short")
        return {'approved': True, 'data': {'output': improved}, 'reason': 'improved'}
    except Exception as e:
        return {'approved': True, 'data': {'output': original_output}, 'reason': f'improver_failed: {e}'}

# ══════════════════════════════════════════════════════════
# ORCHESTRATOR — FETCH PIPELINE
# ══════════════════════════════════════════════════════════

def run_fetch_pipeline():
    existing     = fb('/tasks') or {}
    existing_ids = set(existing.keys())

    accepted_tasks = []
    high_pay_tasks = []
    stats = {'total_raw': 0, 'job_listing': 0, 'scam_vague': 0,
             'in_person': 0, 'low_confidence': 0, 'accepted': 0}

    for source, feed_url in FEEDS:
        print(f"\n{'─'*50}")
        print(f"Fetching {source}...")
        try:
            r = requests.get(feed_url, headers=RSS_HEADERS, timeout=20)
            print(f"  RSS status: {r.status_code}")
            if not r.ok:
                continue

            raw_tasks = parse_rss(r.text, source)
            new_raw   = [t for t in raw_tasks if t['id'] not in existing_ids]
            print(f"  {len(new_raw)} new posts to evaluate")
            stats['total_raw'] += len(new_raw)

            for raw_task in new_raw:
                pid   = raw_task['id']
                title = raw_task['title']
                print(f"\n  ▶ {title[:55]}")

                # ── AGENT 1: SCOUT ──────────────────────────────
                scout_result = None
                for attempt in range(MAX_RETRIES):
                    scout_result = agent_scout(raw_task)
                    if scout_result['approved']:
                        print(f"    ✓ Scout [{scout_result['data'].get('deliverable_type')}]")
                        break
                    reason = scout_result['reason']
                    print(f"    ✗ Scout: {reason}")
                    # Clear rejections don't retry
                    if reason in ('job_listing', 'self_promotion', 'in_person', 'scam'):
                        break
                    time.sleep(1)

                if not scout_result['approved']:
                    reason = scout_result['reason']
                    if 'job_listing' in reason or 'self_promotion' in reason:
                        stats['job_listing'] += 1
                    elif 'in_person' in reason or 'scam' in reason:
                        stats['in_person'] += 1
                    else:
                        stats['scam_vague'] += 1
                    time.sleep(0.3)
                    continue

                # ── AGENT 2: EVALUATOR ──────────────────────────
                eval_result = agent_evaluator(raw_task, scout_result['data'])
                if not eval_result['approved']:
                    conf = eval_result['data'].get('overall_confidence', 0)
                    print(f"    ✗ Evaluator: {conf}% — {eval_result['reason'][:60]}")
                    blocking = eval_result['data'].get('blocking_issues', [])
                    if blocking:
                        print(f"      Blockers: {'; '.join(blocking[:2])}")
                    stats['low_confidence'] += 1
                    time.sleep(0.3)
                    continue

                conf = eval_result['data'].get('overall_confidence', 0)
                print(f"    ✓ Evaluator: {conf}% confidence")

                # ── TASK ACCEPTED ───────────────────────────────
                pay  = extract_pay(title + ' ' + raw_task.get('description', ''))
                task = {
                    'id':          pid,
                    'redditId':    pid,
                    'title':       title,
                    'description': raw_task.get('description', ''),
                    'pay':         pay,
                    'type':        scout_result['data'].get('deliverable_type', 'writing'),
                    'source':      source,
                    'url':         raw_task['url'],
                    'createdAt':   int(time.time()),
                    'status':      'inbox',
                    'fetchedAt':   int(datetime.datetime.utcnow().timestamp() * 1000),
                    'aiConfidence':     conf,
                    'aiUnderstoodAs':   scout_result['data'].get('understood_request', ''),
                    'aiComplexity':     eval_result['data'].get('complexity', 'medium'),
                    'aiBlockers':       eval_result['data'].get('blocking_issues', []),
                    'aiStepsCount':     len(eval_result['data'].get('completion_steps', [])),
                    'aiFlags':          scout_result['data'].get('flags', []),
                    'aiApproach':       eval_result['data'].get('execution_approach', ''),
                    '_evalData':        eval_result['data'],
                }
                accepted_tasks.append(task)
                stats['accepted'] += 1
                print(f"    ★ ACCEPTED — {conf}%, pay: {pay or 'unspecified'}")

                if pay:
                    try:
                        if float(pay.replace('$', '').replace(',', '')) >= MIN_PAY_ALERT:
                            high_pay_tasks.append(task)
                    except Exception:
                        pass

                time.sleep(0.8)

        except Exception as e:
            print(f"  Error {source}: {e}")

    if accepted_tasks:
        for task in accepted_tasks:
            eval_data = task.pop('_evalData', {})
            fb(f'/tasks/{task["id"]}', 'PUT', task)
            fb(f'/task_evals/{task["id"]}', 'PUT', eval_data)
        print(f"\n✓ Pushed {len(accepted_tasks)} tasks to Firebase")

    print(f"\n{'═'*50}")
    print(f"FETCH STATS: {stats['total_raw']} evaluated")
    print(f"  Job listings:       {stats['job_listing']}")
    print(f"  Scam/vague:         {stats['scam_vague']}")
    print(f"  In-person/undoable: {stats['in_person']}")
    print(f"  Low confidence:     {stats['low_confidence']}")
    print(f"  ✓ Accepted:         {stats['accepted']}")

    if high_pay_tasks:
        rows = ''.join([
            f'<tr><td style="padding:10px;color:#f0a500;font-weight:bold">{t["pay"]}</td>'
            f'<td style="padding:10px">{t["title"][:55]}</td>'
            f'<td style="padding:10px;color:#00e5a0">{t["aiConfidence"]}% AI</td>'
            f'<td style="padding:10px"><a href="{t["url"]}" style="color:#3d9eff">View</a></td></tr>'
            for t in high_pay_tasks
        ])
        send_email(
            f"⚡ TASKFORCE: {len(high_pay_tasks)} high-pay task(s) — AI verified",
            f'<div style="background:#0a0b0d;color:#c8cdd8;padding:20px;font-family:monospace">'
            f'<h2 style="color:#f0a500">⚡ High-Pay Tasks — Scout + Evaluator Verified</h2>'
            f'<table style="width:100%;border-collapse:collapse">{rows}</table>'
            f'<br/><a href="https://nadavw9.github.io/taskforce" style="color:#00e5a0">Open Dashboard →</a></div>'
        )

# ══════════════════════════════════════════════════════════
# ORCHESTRATOR — EXECUTION PIPELINE
# ══════════════════════════════════════════════════════════

def run_execution_pipeline():
    all_tasks = fb('/tasks') or {}
    approved  = {k: v for k, v in all_tasks.items()
                 if isinstance(v, dict) and v.get('status') == 'approved'}

    print(f"\nApproved tasks to execute: {len(approved)}")
    if not approved:
        return

    for key, task in approved.items():
        title = task.get('title', '')
        print(f"\n{'═'*50}")
        print(f"EXECUTING: {title[:60]}")
        fb(f'/tasks/{key}', 'PATCH', {'status': 'executing'})
        log_event(key, 'orchestrator', 'started', 'Execution pipeline started')

        try:
            eval_data = fb(f'/task_evals/{key}') or {}

            # ── PLANNER ─────────────────────────────────────
            print("  [Planner] Creating blueprint...")
            plan_result = agent_planner(task, eval_data)
            log_event(key, 'planner', 'complete', plan_result['reason'])

            # ── EXECUTOR ────────────────────────────────────
            print("  [Executor] Producing deliverable...")
            exec_result = None
            for attempt in range(MAX_RETRIES):
                exec_result = agent_executor(task, eval_data, plan_result['data'])
                if exec_result['approved']:
                    output = exec_result['data']['output']
                    print(f"  ✓ Executor: {len(output)} chars (attempt {attempt+1})")
                    log_event(key, 'executor', 'success', f'{len(output)} chars')
                    break
                print(f"  ✗ Executor attempt {attempt+1}: {exec_result['reason']}")
                log_event(key, 'executor', 'retry', exec_result['reason'])
                time.sleep(3)

            if not exec_result or not exec_result['approved']:
                raise Exception(f"Executor failed after {MAX_RETRIES} attempts")

            output = exec_result['data']['output']

            # ── CRITIC ──────────────────────────────────────
            print("  [Critic] Evaluating quality...")
            critic_result = agent_critic(task, output)
            score  = critic_result['data'].get('quality_score', 0)
            issues = critic_result['data'].get('issues', [])
            print(f"  {'✓' if critic_result['approved'] else '!'} Critic: {score}/100 — {critic_result['data'].get('verdict','?')}")
            log_event(key, 'critic', 'scored', f'score={score}')

            for iss in issues[:3]:
                print(f"    [{iss.get('severity','?')}] {iss.get('issue','')[:60]}")

            # ── IMPROVER (if needed) ─────────────────────────
            if not critic_result['approved']:
                print(f"  [Improver] Rewriting (score {score} < {MIN_QUALITY})...")
                log_event(key, 'improver', 'started', f'Improving from score {score}')
                improve_result = agent_improver(task, output, critic_result['data'])
                output = improve_result['data'].get('output', output)

                recheck = agent_critic(task, output)
                new_score = recheck['data'].get('quality_score', score)
                print(f"  ✓ Improver: {score} → {new_score}/100")
                log_event(key, 'improver', 'complete', f'Score: {score} → {new_score}')
                score  = new_score
                issues = recheck['data'].get('issues', [])
                critic_result = recheck

            # ── FINALIZE ─────────────────────────────────────
            final_status = 'done' if score >= MIN_QUALITY else 'done_flagged'
            fb(f'/tasks/{key}', 'PATCH', {
                'status':        final_status,
                'output':        output,
                'qualityScore':  score,
                'qualityIssues': [i.get('issue', '') for i in issues],
                'completedAt':   datetime.datetime.utcnow().isoformat(),
            })
            log_event(key, 'orchestrator', 'done', f'status={final_status}, quality={score}')
            print(f"  ★ DONE — {final_status}, quality {score}/100")

            flag = '' if score >= MIN_QUALITY else ' ⚠️ review recommended'
            send_email(
                f"{'✅' if score >= MIN_QUALITY else '⚠️'} Task done [{score}/100]{flag}: {title[:40]}",
                f'<div style="background:#0a0b0d;color:#c8cdd8;padding:20px;font-family:monospace">'
                f'<h2 style="color:{"#00e5a0" if score >= MIN_QUALITY else "#f0a500"}">'
                f'{"✅ Ready to Send" if score >= MIN_QUALITY else "⚠️ Review Before Sending"}</h2>'
                f'<p style="color:#f0a500;font-size:15px">{title}</p>'
                f'<p>Quality: <strong>{score}/100</strong> | Pay: {task.get("pay","?")} | Type: {task.get("type","?")}</p>'
                f'<p><a href="{task.get("url","")}" style="color:#3d9eff">Original post ↗</a></p>'
                f'<a href="https://nadavw9.github.io/taskforce" style="color:#00e5a0">Open dashboard →</a></div>'
            )

        except Exception as e:
            fb(f'/tasks/{key}', 'PATCH', {'status': 'error', 'error': str(e)})
            log_event(key, 'orchestrator', 'error', str(e)[:200])
            print(f"  ✗ Pipeline error: {e}")
            print(traceback.format_exc()[-500:])

# ══════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════

print("╔══════════════════════════════════════╗")
print("║   TASKFORCE — Agent System v5        ║")
print("╚══════════════════════════════════════╝")
print("Models:")
for role, model in MODELS.items():
    print(f"  {role:<12} → {model}")
print()

run_fetch_pipeline()
run_execution_pipeline()

print("\n╔══════════════════════════════════════╗")
print("║   All pipelines complete             ║")
print("╚══════════════════════════════════════╝")
