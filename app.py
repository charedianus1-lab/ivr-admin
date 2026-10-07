from datetime import timedelta
import os, re, io, json, time, wave, hmac, asyncio, hashlib, threading
import requests
import copy, uuid
from urllib.parse import urlparse, parse_qs, urlencode
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
LOCK = threading.RLock()
TYPES = {'playfile': 'השמעת קבצים מהתיקייה', 'submenu': 'תת-תפריט', 'songlist': 'רשימת שירים (קישורי יוטיוב)'}
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
    r.raise_for_status()
    if r.headers.get('content-type', '').startswith('application/json'):
        listing = ym_list(BASE)
        if listing.get('responseStatus') != 'OK': raise RuntimeError('לא ניתן לבדוק את הגיבוי בימות')
        entries = listing.get('files', [])
        if path == REMOTE_CFG and not any(x.get('name') == '_admin_config.ini' for x in entries): return None
        raise RuntimeError('לא ניתן לקרוא את הגיבוי בימות')
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
        if os.path.exists(DATA):
            with open(DATA, encoding='utf-8') as f: return json.load(f)
        if YM_SYSTEM and YM_PASS:
            t = ym_download_text(REMOTE_CFG)
            if t:
                cfg = json.loads(t)
                if not isinstance(cfg.get('items'), list): raise RuntimeError('גיבוי לא תקין')
                save(cfg, remote=False)
                return cfg
        return copy.deepcopy(DEFAULT)

def save(cfg, remote=True):
    # A successful save is durable, not just a /tmp write. Never hide a failed backup.
    with LOCK:
        if remote:
            if not (YM_SYSTEM and YM_PASS): raise RuntimeError('חסרים פרטי ימות לגיבוי')
            payload = json.dumps(cfg, ensure_ascii=False)
            ym_upload_text(payload, REMOTE_CFG)
            check = ym_download_text(REMOTE_CFG)
            if not check or json.loads(check) != cfg: raise RuntimeError('אימות הגיבוי לימות נכשל')
        tmp = DATA + '.new'
        with open(tmp, 'w', encoding='utf-8') as f: json.dump(cfg, f, ensure_ascii=False)
        os.replace(tmp, DATA)

def clean_song(song):
    u = urlparse(str(song.get('url', '')).strip())
    if u.scheme not in ('https', 'http'): raise ValueError('נדרש קישור יוטיוב מלא')
    host = (u.hostname or '').lower()
    if host == 'youtu.be': vid = u.path.strip('/').split('/')[0]
    elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
        vid = parse_qs(u.query).get('v', [''])[0]
        if u.path.startswith(('/shorts/', '/live/')): vid = u.path.split('/')[2]
    else: raise ValueError('רק קישורי יוטיוב מתקבלים')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid): raise ValueError('קישור שיר לא תקין; פלייליסט מיובא דרך כפתור היבוא')
    return {'url': 'https://www.youtube.com/watch?v=' + vid,
            'title': re.sub(r'\s+', ' ', str(song.get('title', ''))).strip()[:180] or vid}

def clean_items(items):
    if not isinstance(items, list) or len(items) > 10: raise ValueError('רשימת שלוחות לא תקינה')
    out, seen = [], set()
    for it in items:
        d = str(it.get('digit', '')).strip()
        name = re.sub(r'\s+', ' ', str(it.get('name', ''))).strip()[:60]
        if not re.fullmatch(r'[0-9]', d) or not name or d in seen: raise ValueError('נדרשים שם ומקש ייחודי לכל שלוחה')
        typ = it.get('type', 'playfile')
        if typ not in TYPES: raise ValueError('סוג שלוחה לא תקין')
        songs = it.get('songs', [])
        if not isinstance(songs, list) or len(songs) > 200: raise ValueError('עד 200 שירים בכל רשימה')
        seen.add(d); out.append({'digit': d, 'name': name, 'type': typ, 'songs': [clean_song(x) for x in songs]})
    return sorted(out, key=lambda x: x['digit'])

def bridge():
    text = ym_download_text('/2/ext.ini') or ''
    link = next((x.split('=',1)[1].strip() for x in text.splitlines() if x.startswith('api_link=')), '')
    u = urlparse(link)
    if u.scheme != 'https' or u.path != '/yemot-song': raise RuntimeError('חיבור השירים של הקו לא זמין')
    return u.scheme + '://' + u.netloc, parse_qs(u.query).get('secret', [''])[0]

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
    songlists = [it for it in items if it['type'] == 'songlist']
    host, secret = ('', '')
    if songlists:
        if any(not it['songs'] for it in songlists): raise RuntimeError('רשימת שירים ריקה')
        host, secret = bridge()
        r = requests.get(host + '/admin-song-links', headers={'X-Bridge-Secret': secret}, timeout=15)
        r.raise_for_status()
        if not r.json().get('enabled'): raise RuntimeError('חיבור הרשימות בקו עדיין לא הופעל; הטיוטה נשמרת')
    # Render first and save rollback + intended revision before touching the live tree.
    audio = tts_wav(greeting({**cfg, 'items': items}))
    old = copy.deepcopy(cfg.get('published'))
    cfg['prev'] = old
    save(cfg)
    version = uuid.uuid4().hex
    for it in items:
        path = f"{BASE}/{it['digit']}"
        if it['type'] == 'songlist':
            manifest = f'{BASE}/_songs_{version}_{it["digit"]}.ini'
            ym_upload_text(json.dumps({'songs': it['songs']}, ensure_ascii=False), manifest)
            if json.loads(ym_download_text(manifest) or '{}').get('songs') != it['songs']: raise RuntimeError('אימות רשימת השירים נכשל')
            query = urlencode({'secret': secret, 'config': manifest})
            text = f'type=api\napi_link={host}/yemot-link-list?{query}\napi_dir=/2\napi_url_post=no\n'
        else: text = f"type={'menu' if it['type']=='submenu' else 'playfile'}\n"
        ym_upload_text(text, path + '/ext.ini')
    ym_upload(audio, '000.wav', f'{BASE}/000.wav')
    ym_upload_text('type=menu\ntimeout=5\nattempts=3\nup=*\n', f'{BASE}/ext.ini')
    cfg['items'] = items
    cfg['published'] = {'items': copy.deepcopy(items), 'greeting_pre': cfg.get('greeting_pre', ''),
                        'greeting_post': cfg.get('greeting_post', ''), 'greeting': greeting(cfg), 'at': time.strftime('%d/%m/%Y %H:%M')}
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
    with LOCK:
        try:
            j = request.get_json(force=True)
            cfg = load()
            if j.get('revision') != cfg.get('revision'): return jsonify(ok=False, error='הטיוטה השתנתה; רענן לפני שמירה'), 409
            cfg['items'] = clean_items(j.get('items', []))
            cfg['greeting_pre'] = str(j.get('greeting_pre', ''))[:200]
            cfg['greeting_post'] = str(j.get('greeting_post', ''))[:200]
            cfg['revision'] = uuid.uuid4().hex
            save(cfg)
            return jsonify(ok=True, greeting=greeting(cfg), revision=cfg['revision'])
        except Exception as e: return jsonify(ok=False, error=str(e)[:200]), 502

@app.route('/api/song-links', methods=['POST'])
@need_auth
def api_song_links():
    if not csrf_ok(): return jsonify(ok=False, error='csrf'), 403
    try:
        j = request.get_json(force=True)
        host, secret = bridge()
        r = requests.post(host + '/admin-song-links', json={'query': str(j.get('query', ''))[:300], 'mode': j.get('mode', 'search')},
                          headers={'X-Bridge-Secret': secret}, timeout=100)
        r.raise_for_status()
        data = r.json()
        if not data.get('ok'): raise RuntimeError(data.get('error', 'החיפוש נכשל'))
        return jsonify(ok=True, songs=[clean_song(x) for x in data['songs']])
    except Exception as e: return jsonify(ok=False, error=str(e)[:200]), 502

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
    target = copy.deepcopy(cfg['prev'])
    cfg['items'] = target['items']
    cfg['greeting_pre'] = target.get('greeting_pre', cfg.get('greeting_pre', ''))
    cfg['greeting_post'] = target.get('greeting_post', cfg.get('greeting_post', ''))
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
.row{display:flex;gap:8px;margin-bottom:8px;align-items:center}.row{flex-wrap:wrap}.row input.n{flex:1;min-width:120px}.songs{padding:12px;border:1px solid #334155;border-radius:10px;margin-bottom:18px}.song{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.song input{min-width:0;flex:1}.song .url{direction:ltr}.songs textarea{width:100%;box-sizing:border-box;min-height:80px}@media(max-width:500px){body{padding:8px}.song input{flex-basis:100%}.row input.n{width:130px}.card{padding:12px}}.row input.dg{width:56px;text-align:center}
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
const CSRF="{{csrf}}", TYPES={{types|tojson}}; let revision={{cfg.get("revision")|tojson}}; let items={{cfg['items']|tojson}};
document.getElementById('pre').value={{cfg.greeting_pre|tojson}}; document.getElementById('post').value={{cfg.greeting_post|tojson}};
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;')}
function render(){document.getElementById('rows').innerHTML=items.map((it,i)=>`<div class="row"><input class="dg" value="${esc(it.digit)}" maxlength=1 oninput="items[${i}].digit=this.value;gr()"><input class="n" value="${esc(it.name)}" placeholder="שם השלוחה" oninput="items[${i}].name=this.value;gr()"><select onchange="items[${i}].type=this.value;render()">${Object.entries(TYPES).map(([k,v])=>`<option value="${k}" ${k==it.type?'selected':''}>${v}</option>`).join('')}</select><button class="s" onclick="mv(${i},-1)">↑</button><button class="s" onclick="mv(${i},1)">↓</button><button class="d" onclick="items.splice(${i},1);render()">✕</button></div>${it.type==='songlist'?songPanel(it,i):''}`).join('');gr()}
function songPanel(it,i){it.songs=it.songs||[];return `<div class="songs"><small>נשמרים רק קישורים ושמות. השמעה דרך הקו לפי הסדר, בלי ספריית אודיו.</small>${it.songs.map((s,k)=>`<div class="song"><input value="${esc(s.title)}" placeholder="שם השיר" oninput="items[${i}].songs[${k}].title=this.value"><input class="url" value="${esc(s.url)}" placeholder="קישור יוטיוב" oninput="items[${i}].songs[${k}].url=this.value"><button class="s" onclick="smv(${i},${k},-1)">↑</button><button class="s" onclick="smv(${i},${k},1)">↓</button><button class="d" onclick="items[${i}].songs.splice(${k},1);render()">✕</button></div>`).join('')}<button class="s" onclick="items[${i}].songs.push({title:'',url:''});render()">+ שיר</button><p><textarea id="bulk${i}" placeholder="הדבקת קישורים, קישור אחד בכל שורה"></textarea><button class="s" onclick="bulk(${i})">הוסף קישורים</button></p><div class="row"><input id="query${i}" placeholder="חיפוש שיר או קישור לערוץ / פלייליסט"><button class="s" onclick="lookup(${i},'search')">חפש</button><button class="s" onclick="lookup(${i},'import')">יבא קישורים</button></div><div id="results${i}"></div><small>יבוא מוגבל ל-200 קישורים בכל פעם. אפשר לבדוק ולהסיר שירים לפני הפרסום.</small></div>`}
function smv(i,k,d){let a=items[i].songs,j=k+d;if(j<0||j>=a.length)return;[a[k],a[j]]=[a[j],a[k]];render()}
function bulk(i){let a=document.getElementById('bulk'+i).value.split(/\\n/).map(x=>x.trim()).filter(Boolean);a.forEach(url=>items[i].songs.push({title:'',url}));render()}
let results={};
async function lookup(i,mode){say(mode==='search'?'מחפש...':'מייבא קישורים...',true);try{let j=await post('/api/song-links',{query:document.getElementById('query'+i).value,mode});if(!j.ok){say(j.error,false);return}results[i]=j.songs;document.getElementById('results'+i).innerHTML=j.songs.map((s,k)=>`<div class="song"><span>${esc(s.title)}</span><button class="s" onclick="pick(${i},${k})">הוסף</button></div>`).join('')+(j.songs.length?`<button onclick="pickAll(${i})">הוסף את כל התוצאות</button>`:'אין תוצאות');say('נמצאו '+j.songs.length+' קישורים',true)}catch(e){say('לא ניתן להתחבר לקו',false)}}
function pick(i,k){let s=results[i][k];if(!items[i].songs.some(x=>x.url===s.url))items[i].songs.push({...s});render()}
function pickAll(i){for(let s of results[i]||[])if(!items[i].songs.some(x=>x.url===s.url))items[i].songs.push({...s});render()}
function addRow(){const used=items.map(x=>x.digit);let d='1';for(let k=1;k<=9;k++){if(!used.includes(String(k))){d=String(k);break}}items.push({digit:d,name:'',type:'playfile'});render()}
function mv(i,s){const j=i+s;if(j<0||j>=items.length)return;[items[i],items[j]]=[items[j],items[i]];render()}
function gr(){const p=document.getElementById('pre').value,q=document.getElementById('post').value;document.getElementById('gr').textContent=[p].concat(items.filter(x=>x.name&&x.digit).sort((a,b)=>a.digit>b.digit?1:-1).map(x=>`ל${x.name}, הקישו ${x.digit}.`)).concat([q]).join(' ').trim()}
document.getElementById('pre').oninput=gr;document.getElementById('post').oninput=gr;
function say(t,ok){const m=document.getElementById('msg');m.textContent=t;m.className=ok?'ok':'er'}
async function post(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF':CSRF},body:JSON.stringify(b||{})});return r.json()}
async function saveAll(){try{const j=await post('/api/save',{items,revision,greeting_pre:document.getElementById('pre').value,greeting_post:document.getElementById('post').value});if(j.ok)revision=j.revision;say(j.ok?'נשמר וגובה לימות':'השמירה נכשלה: '+j.error,j.ok);return j.ok}catch(e){say('השמירה נכשלה; אין אישור גיבוי',false);return false}}
async function prev(){if(!await saveAll())return;const a=document.getElementById('au');a.style.display='inline';a.src='/api/preview?'+Date.now();a.play()}
async function pub(){if(!await saveAll())return;if(!confirm('לפרסם את השלוחות וההקראה לקו החי?'))return;say('מפרסם...',true);const j=await post('/api/publish');say(j.ok?'פורסם '+j.at:'שגיאה: '+j.error,j.ok)}
async function rb(){if(!confirm('לחזור לגרסה הקודמת ולפרסם אותה?'))return;const j=await post('/api/rollback');say(j.ok?'הוחזר':'שגיאה: '+j.error,j.ok);if(j.ok)setTimeout(()=>location.reload(),800)}
render();
</script></html>'''

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
