from datetime import timedelta
import os, re, io, json, time, wave, hmac, asyncio, hashlib, threading
import requests
from flask import Flask, request, session, redirect, url_for, render_template_string, jsonify, Response

app = Flask(__name__)
YM_SYSTEM = os.environ.get('YM_SYSTEM', '')
YM_PASS = os.environ.get('YM_PASS', '')
YM_TOKEN = f'{YM_SYSTEM}:{YM_PASS}'
YM_API = 'https://www.call2all.co.il/ym/api'
BASE = os.environ.get('YM_BASE_EXT', '/5').rstrip('/')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
EDGE_VOICE = os.environ.get('EDGE_VOICE', 'he-IL-HilaNeural')
EDGE_RATE = os.environ.get('EDGE_RATE', '+25%')
DATA = os.environ.get('DATA_FILE', '/tmp/ivr_admin.json')
REMOTE_CFG = f'{BASE}/_admin_config.ini'
app.secret_key = os.environ.get('SECRET_KEY') or hashlib.sha256(('k' + ADMIN_PASSWORD + YM_PASS).encode()).hexdigest()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=bool(os.environ.get('COOKIE_SECURE', '1') == '1'),
                  PERMANENT_SESSION_LIFETIME=timedelta(days=3650))   # sign in once, stay signed in
LOCK = threading.Lock()
TYPES = {'playfile': 'השמעת קבצים מהתיקייה', 'submenu': 'תת-תפריט'}
DEFAULT = {'greeting_pre': 'ברוכים הבאים.', 'greeting_post': '', 'items': [], 'published': None, 'prev': None}

# ---------- Yemot ----------
def ym_p(p): return p if p.startswith('ivr2:') else 'ivr2:' + p

def ym_upload_text(text, path):
    r = requests.get(f'{YM_API}/UploadTextFile', params={'token': YM_TOKEN, 'what': ym_p(path), 'contents': text}, timeout=30)
    r.raise_for_status(); j = r.json()
    if j.get('responseStatus') != 'OK':
        raise RuntimeError('UploadTextFile: ' + str(j.get('message'))[:200])

def ym_upload(data, fname, path):
    r = requests.post(f'{YM_API}/UploadFile', data={'token': YM_TOKEN, 'path': ym_p(path)}, files={'file': (fname, data)}, timeout=90)
    r.raise_for_status(); j = r.json()
    if j.get('success') is False or j.get('responseStatus') not in (None, 'OK'):
        raise RuntimeError('UploadFile: ' + str(j.get('message'))[:200])

def ym_download_text(path):
    r = requests.get(f'{YM_API}/DownloadFile', params={'token': YM_TOKEN, 'path': ym_p(path)}, timeout=30)
    if r.status_code != 200 or r.headers.get('content-type', '').startswith('application/json'):
        return None
    return r.content.decode('utf-8', 'replace')

def ym_list(path):
    r = requests.get(f'{YM_API}/GetIVR2Dir', params={'token': YM_TOKEN, 'path': ym_p(path)}, timeout=30)
    return r.json()

def tts_wav(text):
    import edge_tts, miniaudio
    text = re.sub(r'\s+', ' ', text or '').strip()
    mp3 = f'/tmp/tts-{time.time_ns()}.mp3'
    try:
        async def gen(): await edge_tts.Communicate(text, EDGE_VOICE, rate=EDGE_RATE).save(mp3)
        asyncio.run(gen())
        snd = miniaudio.decode_file(mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=8000)
    finally:
        try: os.remove(mp3)
        except OSError: pass
    buf = io.BytesIO(); w = wave.open(buf, 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
    w.writeframes(bytes(snd.samples)); w.close()
    return buf.getvalue()

# ---------- Config ----------
def load():
    with LOCK:
        try:
            return json.load(open(DATA, encoding='utf-8'))
        except Exception:
            pass
    cfg = None
    if YM_SYSTEM and YM_PASS:
        try:
            t = ym_download_text(REMOTE_CFG)
            if t: cfg = json.loads(t)
        except Exception:
            cfg = None
    cfg = cfg or json.loads(json.dumps(DEFAULT))
    save(cfg, remote=False)
    return cfg

def save(cfg, remote=True):
    with LOCK:
        json.dump(cfg, open(DATA, 'w', encoding='utf-8'), ensure_ascii=False)
    if remote and YM_SYSTEM and YM_PASS:
        try: ym_upload_text(json.dumps(cfg, ensure_ascii=False), REMOTE_CFG)
        except Exception as e: return str(e)[:150] or 'upload failed'
    return None

def clean_items(items):
    out, seen = [], set()
    for it in items:
        d = re.sub(r'\D', '', str(it.get('digit', '')))[:1]
        name = re.sub(r'\s+', ' ', str(it.get('name', ''))).strip()[:60]
        typ = it.get('type') if it.get('type') in TYPES else 'playfile'
        if not d or not name or d in seen: continue
        seen.add(d); out.append({'digit': d, 'name': name, 'type': typ})
    return sorted(out, key=lambda x: x['digit'])

def greeting(cfg):
    parts = []
    if cfg.get('greeting_pre'): parts.append(cfg['greeting_pre'].strip())
    for it in cfg['items']:
        parts.append(f"ל{it['name']}, הקישו {it['digit']}.")
    if cfg.get('greeting_post'): parts.append(cfg['greeting_post'].strip())
    return ' '.join(parts)

def publish(cfg):
    items = clean_items(cfg['items'])
    if not items: raise RuntimeError('אין שלוחות לפרסום')
    if not (YM_SYSTEM and YM_PASS): raise RuntimeError('חסרים פרטי ימות')
    cfg['prev'] = cfg.get('published')
    ym_upload_text('type=menu\ntimeout=5\nattempts=3\nup=*\n', f'{BASE}/ext.ini')
    for it in items:
        ym_upload_text(f"type={'menu' if it['type']=='submenu' else 'playfile'}\n", f"{BASE}/{it['digit']}/ext.ini")
    ym_upload(tts_wav(greeting({**cfg, 'items': items})), '000.wav', f'{BASE}/000.wav')
    cfg['items'] = items
    cfg['published'] = {'items': items, 'greeting': greeting({**cfg, 'items': items}), 'at': time.strftime('%d/%m/%Y %H:%M')}
    save(cfg)

# ---------- Auth ----------
FAILS = {}
def authed(): return session.get('ok') is True
def need_auth(f):
    from functools import wraps
    @wraps(f)
    def w(*a, **k):
        if not authed(): return redirect(url_for('login'))
        return f(*a, **k)
    return w
def csrf_ok():
    return hmac.compare_digest((request.headers.get('X-CSRF', '') or request.form.get('csrf', '')).encode(), session.get('csrf', 'x').encode())

@app.route('/login', methods=['GET', 'POST'])
def login():
    msg = ''
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    n, t = FAILS.get(ip, (0, 0))
    if n >= 5 and time.time() - t < 900:
        return render_template_string(PAGE_LOGIN, msg='יותר מדי ניסיונות, נסה שוב בעוד 15 דקות'), 429
    if request.method == 'POST':
        if ADMIN_PASSWORD and hmac.compare_digest(request.form.get('password', '').encode(), ADMIN_PASSWORD.encode()):
            session.permanent = True; session['ok'] = True; session['csrf'] = hashlib.sha256(os.urandom(16)).hexdigest()
            FAILS.pop(ip, None); return redirect('/')
        FAILS[ip] = (n + 1, time.time()); msg = 'סיסמה שגויה'
    return render_template_string(PAGE_LOGIN, msg=msg)

@app.route('/logout')
def logout():
    session.clear(); return redirect('/login')

@app.route('/healthz')
def healthz(): return 'ok'

# ---------- Pages / API ----------
@app.route('/')
@need_auth
def index():
    cfg = load()
    return render_template_string(PAGE_MAIN, cfg=cfg, types=TYPES, greeting=greeting(cfg), csrf=session['csrf'],
                                  ym_ok=bool(YM_SYSTEM and YM_PASS), base=BASE)

@app.route('/api/save', methods=['POST'])
@need_auth
def api_save():
    if not csrf_ok(): return jsonify(ok=False, error='csrf'), 403
    j = request.get_json(force=True)
    cfg = load()
    cfg['items'] = clean_items(j.get('items', []))
    cfg['greeting_pre'] = str(j.get('greeting_pre', ''))[:200]
    cfg['greeting_post'] = str(j.get('greeting_post', ''))[:200]
    warn = save(cfg)
    return jsonify(ok=True, greeting=greeting(cfg), warn=warn)

@app.route('/api/preview')
@need_auth
def api_preview():
    cfg = load()
    try:
        return Response(tts_wav(greeting(cfg)), mimetype='audio/wav')
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 502

@app.route('/api/publish', methods=['POST'])
@need_auth
def api_publish():
    if not csrf_ok(): return jsonify(ok=False, error='csrf'), 403
    cfg = load()
    try:
        publish(cfg)
        return jsonify(ok=True, at=cfg['published']['at'])
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 502

@app.route('/api/rollback', methods=['POST'])
@need_auth
def api_rollback():
    if not csrf_ok(): return jsonify(ok=False, error='csrf'), 403
    cfg = load()
    if not cfg.get('prev'): return jsonify(ok=False, error='אין גרסה קודמת'), 400
    cfg['items'] = cfg['prev']['items']
    try:
        publish(cfg); return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 502

PAGE_LOGIN = '''<!doctype html><html dir="rtl" lang="he"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>כניסה</title><style>body{font-family:system-ui;background:#0f172a;color:#e2e8f0;display:grid;place-items:center;height:100vh;margin:0}
form{background:#1e293b;padding:28px;border-radius:14px;width:300px}input,button{width:100%;padding:12px;margin-top:12px;border-radius:8px;border:0;font-size:16px;box-sizing:border-box}
button{background:#3b82f6;color:#fff;cursor:pointer}.m{color:#f87171;margin-top:10px}</style>
<form method="post"><h2>ניהול שלוחה 5</h2><input type="password" name="password" placeholder="סיסמה" autofocus><button>כניסה</button><div class="m">{{msg}}</div></form></html>'''

PAGE_MAIN = '''<!doctype html><html dir="rtl" lang="he"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ניהול שלוחה 5</title><style>
body{font-family:system-ui;background:#0f172a;color:#e2e8f0;margin:0;padding:16px;max-width:760px;margin:auto}
.card{background:#1e293b;border-radius:14px;padding:16px;margin-bottom:14px}h1{font-size:22px}h3{margin-top:0}
input,select,textarea,button{padding:10px;border-radius:8px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:15px}
button{background:#3b82f6;border:0;color:#fff;cursor:pointer}button.s{background:#475569}button.d{background:#b91c1c}button.g{background:#15803d}
.row{display:flex;gap:8px;margin-bottom:8px;align-items:center}.row input.n{flex:1}.row input.dg{width:56px;text-align:center}
#msg{min-height:22px}.ok{color:#4ade80}.er{color:#f87171}.gr{background:#0f172a;padding:12px;border-radius:8px;line-height:1.7}small{color:#94a3b8}
</style><h1>ניהול שלוחה {{base}} <a href="/logout" style="font-size:13px;color:#94a3b8">יציאה</a></h1>
{% if not ym_ok %}<div class="card er">פרטי ימות לא הוגדרו - פרסום לא יעבוד</div>{% endif %}
<div class="card"><h3>שלוחות</h3><div id="rows"></div>
<button class="s" onclick="addRow()">+ הוסף שלוחה</button></div>
<div class="card"><h3>הקראת התפריט (נבנית אוטומטית מהשמות)</h3>
<div class="row"><input id="pre" style="flex:1" placeholder="פתיחה"></div>
<div class="gr" id="gr"></div>
<div class="row" style="margin-top:8px"><input id="post" style="flex:1" placeholder="סיום (לא חובה)"></div>
<button class="s" onclick="prev()">שמע תצוגה מקדימה</button> <audio id="au" controls style="vertical-align:middle;display:none"></audio></div>
<div class="card"><button onclick="saveAll()">שמור טיוטה</button> <button class="g" onclick="pub()">פרסם לקו</button>
<button class="d" onclick="rb()">חזור לגרסה הקודמת</button><div id="msg"></div>
<small>{% if cfg.published %}פורסם לאחרונה: {{cfg.published.at}}{% else %}עדיין לא פורסם{% endif %}</small></div>
<script>
const CSRF="{{csrf}}", TYPES={{types|tojson}}; let items={{cfg['items']|tojson}};
document.getElementById('pre').value={{cfg.greeting_pre|tojson}}; document.getElementById('post').value={{cfg.greeting_post|tojson}};
function esc(s){return String(s).replace(/"/g,'&quot;').replace(/</g,'&lt;')}
function render(){document.getElementById('rows').innerHTML=items.map((it,i)=>`<div class="row"><input class="dg" value="${esc(it.digit)}" maxlength=1 oninput="items[${i}].digit=this.value;gr()"><input class="n" value="${esc(it.name)}" placeholder="שם השלוחה" oninput="items[${i}].name=this.value;gr()"><select onchange="items[${i}].type=this.value">${Object.entries(TYPES).map(([k,v])=>`<option value="${k}" ${k==it.type?'selected':''}>${v}</option>`).join('')}</select><button class="s" onclick="mv(${i},-1)">↑</button><button class="s" onclick="mv(${i},1)">↓</button><button class="d" onclick="items.splice(${i},1);render()">✕</button></div>`).join('');gr()}
function addRow(){const used=items.map(x=>x.digit);let d='1';for(let k=1;k<=9;k++){if(!used.includes(String(k))){d=String(k);break}}items.push({digit:d,name:'',type:'playfile'});render()}
function mv(i,s){const j=i+s;if(j<0||j>=items.length)return;[items[i],items[j]]=[items[j],items[i]];render()}
function gr(){const p=document.getElementById('pre').value,q=document.getElementById('post').value;document.getElementById('gr').textContent=[p].concat(items.filter(x=>x.name&&x.digit).sort((a,b)=>a.digit>b.digit?1:-1).map(x=>`ל${x.name}, הקישו ${x.digit}.`)).concat([q]).join(' ').trim()}
document.getElementById('pre').oninput=gr;document.getElementById('post').oninput=gr;
function say(t,ok){const m=document.getElementById('msg');m.textContent=t;m.className=ok?'ok':'er'}
async function post(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF':CSRF},body:JSON.stringify(b||{})});return r.json()}
async function saveAll(){const j=await post('/api/save',{items,greeting_pre:document.getElementById('pre').value,greeting_post:document.getElementById('post').value});say(j.ok?(j.warn?'נשמר באתר, אבל הגיבוי לימות נכשל: '+j.warn:'נשמר'):'שגיאה',j.ok&&!j.warn);return j.ok}
async function prev(){if(!await saveAll())return;const a=document.getElementById('au');a.style.display='inline';a.src='/api/preview?'+Date.now();a.play()}
async function pub(){if(!await saveAll())return;if(!confirm('לפרסם את השלוחות וההקראה לקו החי?'))return;say('מפרסם...',true);const j=await post('/api/publish');say(j.ok?'פורסם '+j.at:'שגיאה: '+j.error,j.ok)}
async function rb(){if(!confirm('לחזור לגרסה הקודמת ולפרסם אותה?'))return;const j=await post('/api/rollback');say(j.ok?'הוחזר':'שגיאה: '+j.error,j.ok);if(j.ok)setTimeout(()=>location.reload(),800)}
render();
</script></html>'''

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
