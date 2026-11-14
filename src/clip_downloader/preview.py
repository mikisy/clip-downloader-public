"""Loopback-only video preview and range selection for the embedded player."""
import html
import json
import queue
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from clip_downloader.local_clip import LocalClipRequest

PAGE = r'''<!doctype html><html lang="ja"><meta charset="utf-8"><title>範囲クリップ</title>
<style>
body{background:#141923;color:#eef;font:16px sans-serif;max-width:1050px;margin:24px auto;padding:0 20px}
video{display:block;width:100%;max-height:65vh;background:black}
button,input{font:inherit;border-radius:6px;box-sizing:border-box}
button{cursor:pointer;padding:8px 10px;white-space:nowrap}
input{width:110px;padding:8px 6px;font-variant-numeric:tabular-nums}
.control-row,.time-fields,.time-field,.transport,.actions{display:flex;align-items:center;gap:8px}
.control-row{justify-content:space-between;flex-wrap:wrap;gap:12px;margin-top:12px;font-size:14px}
.time-fields{flex-wrap:wrap;gap:12px}.time-field{white-space:nowrap}
.transport{margin-left:auto;justify-content:flex-end;flex-wrap:wrap}
#clock{font-variant-numeric:tabular-nums;white-space:nowrap}
#toggle{min-width:60px}.actions{justify-content:flex-end;margin-top:12px}
#status{min-height:2em}h1{font-size:20px}
</style>
<h1>__TITLE__</h1><video id="video" controls preload="metadata" src="media"></video>
<div class="control-row">
 <div class="time-fields">
  <div class="time-field"><label for="start">開始</label><input id="start" value="00:00:00.0"><button id="markStart">ここから</button></div>
  <div class="time-field"><label for="end">終了</label><input id="end" value="00:00:30.0"><button id="markEnd">ここまで</button></div>
 </div>
 <div class="transport"><span>現在位置</span><strong id="clock">00:00:00.0</strong><button id="back">−5秒</button><button id="forward">＋5秒</button><button id="toggle">再生</button></div>
</div>
<div class="actions"><button id="range">選択範囲を再生</button><button id="export">切り出し開始</button></div>
<div class="actions"><button id="addRange">区間を追加</button><button id="updateRange" disabled>選択区間を更新</button></div>
<p id="summary">登録区間なし：開始・終了の1区間を出力します。</p><ol id="ranges"></ol>
<p id="status" role="status"></p><p>時刻はこのファイルの先頭から。元動画は残ります。切り出しの進捗・停止はアプリのキューで確認できます。</p>
<script>
const v=document.getElementById('video'),s=document.getElementById('start'),e=document.getElementById('end'),status=document.getElementById('status'),go=document.getElementById('export');
let stopAt=null, ranges=[], selected=-1;
function renderRanges(){
 const list=document.getElementById('ranges');list.replaceChildren();
 ranges.forEach(([a,b],i)=>{
  const row=document.createElement('li');row.style.marginBottom='8px';
  const label=document.createElement('button');label.textContent=`${i+1}. ${fmt(a)} ～ ${fmt(b)} (${fmt(b-a)})${i===selected?' 選択中':''}`;
  label.onclick=()=>{selected=i;s.value=fmt(a);e.value=fmt(b);stopAt=null;v.currentTime=a;renderRanges()};row.append(label);
  for(const [text,action,disabled] of [
   ['↑',()=>moveRange(i,-1),i===0],['↓',()=>moveRange(i,1),i===ranges.length-1],
   ['削除',()=>{ranges.splice(i,1);selected=-1;renderRanges()},false]]){
   const button=document.createElement('button');button.textContent=text;button.disabled=disabled;button.onclick=action;row.append(button);
  }list.append(row);
 });
 document.getElementById('updateRange').disabled=selected<0;
 document.getElementById('summary').textContent=ranges.length?`${ranges.length}区間 / 合計 ${fmt(ranges.reduce((t,[a,b])=>t+b-a,0))}：上から順に結合します。未登録の入力値は出力に含みません。`:'登録区間なし：開始・終了の1区間を出力します。';
 go.textContent=ranges.length?'登録区間を結合して切り出し':'切り出し開始';
}
function moveRange(i,d){[ranges[i],ranges[i+d]]=[ranges[i+d],ranges[i]];selected=i+d; s.value=fmt(ranges[selected][0]);e.value=fmt(ranges[selected][1]);renderRanges()}
document.getElementById('addRange').onclick=()=>{try{if(ranges.length>=100)throw Error('区間は100件以内にしてください。');ranges.push(bounds());selected=ranges.length-1;renderRanges();status.textContent='区間を追加しました。'}catch(err){status.textContent=err.message}};
document.getElementById('updateRange').onclick=()=>{try{if(selected<0)return;ranges[selected]=bounds();renderRanges();status.textContent='区間を更新しました。'}catch(err){status.textContent=err.message}};
function fmt(t){let n=Math.round(t*10);return `${String(Math.floor(n/36000)).padStart(2,'0')}:${String(Math.floor(n/600)%60).padStart(2,'0')}:${String(Math.floor(n/10)%60).padStart(2,'0')}.${n%10}`}
function parse(t){if(!/^\d+(?::\d{1,2}){0,2}(?:\.\d+)?$/.test(t.trim()))throw Error('時刻は時:分:秒または秒数で入力してください。');let a=t.trim().split(':').map(Number);if(a.slice(1).some(x=>x>=60))throw Error('分・秒は60未満にしてください。');return a.reduce((x,y)=>x*60+y,0)}
function bounds(){let a=parse(s.value),b=parse(e.value);if(!Number.isFinite(v.duration)||a<0||b<=a||b>v.duration+.05)throw Error('開始・終了が動画の範囲内か確認してください。');return [a,b]}
v.addEventListener('loadedmetadata',()=>{e.value=fmt(Math.min(30,v.duration))});
v.addEventListener('timeupdate',()=>{document.getElementById('clock').textContent=fmt(v.currentTime);if(stopAt!==null&&v.currentTime>=stopAt){v.pause();stopAt=null}});
v.addEventListener('error',()=>{status.textContent='この形式を内蔵プレーヤーで再生できません。一覧の右クリックから「時刻入力でクリップ」をお試しください。'});
const toggle=document.getElementById('toggle');
function syncToggle(){toggle.textContent=v.paused?'再生':'停止'}
v.addEventListener('play',syncToggle);v.addEventListener('pause',syncToggle);v.addEventListener('ended',syncToggle);
toggle.onclick=async()=>{try{if(v.paused){await v.play()}else{v.pause()}}catch(err){status.textContent=err.message}};
document.getElementById('back').onclick=()=>{stopAt=null;v.currentTime=Math.max(0,v.currentTime-5)};
document.getElementById('forward').onclick=()=>{stopAt=null;v.currentTime=Math.min(v.duration,v.currentTime+5)};
document.getElementById('markStart').onclick=()=>{s.value=fmt(v.currentTime)};
document.getElementById('markEnd').onclick=()=>{e.value=fmt(v.currentTime)};
document.getElementById('range').onclick=async()=>{try{let [a,b]=bounds();stopAt=b;v.currentTime=a;await v.play()}catch(err){status.textContent=err.message}};
go.onclick=async()=>{try{let intervals=ranges.length?ranges.map(pair=>[...pair]):[bounds()];go.disabled=true;status.textContent='キューへ追加中…';let r=await fetch('clip',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ranges:intervals})});let data=await r.json();if(!r.ok)throw Error(data.error);for(let n=0;n<30;n++){await new Promise(r=>setTimeout(r,500));let response=await fetch('result/'+data.id);if(!response.ok)throw Error('アプリとの接続を確認してください。');let result=await response.json();if(result.error)throw Error(result.error);if(result.done){status.textContent='切り出しを開始しました。アプリのキューで進捗を確認できます。';return}}throw Error('受付結果を確認できません。再実行前にアプリのキューを確認してください。')}catch(err){status.textContent=err.message}finally{go.disabled=false}};
</script></html>'''


class PreviewServer:
    def __init__(self):
        self.sources = {}
        self.results = {}
        self.pending = queue.SimpleQueue()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def source(self):
                if self.headers.get('Host') != f'127.0.0.1:{owner.server.server_port}':
                    return None, None
                parts = self.path.split('/')
                return owner.sources.get(parts[1]) if len(parts) >= 3 else None, '/'.join(parts[2:])

            def do_GET(self):
                record, action = self.source()
                if record is None:
                    self.send_error(404)
                    return
                if action.startswith('result/'):
                    self.reply(200, owner.results.get(action[7:], {'error': '受付が見つかりません。'}))
                    return
                if not action:
                    body = PAGE.replace('__TITLE__', html.escape(record['title'])).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('Referrer-Policy', 'no-referrer')
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if action != 'media':
                    self.send_error(404)
                    return
                try:
                    with Path(record['path']).open('rb') as file:
                        size = file.seek(0, 2)
                        start, end = 0, size-1
                        range_header = self.headers.get('Range')
                        if range_header:
                            match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header)
                            if not match or not any(match.groups()):
                                self.send_error(416)
                                return
                            left, right = match.groups()
                            start = int(left) if left else max(0, size-int(right))
                            end = min(size-1, int(right)) if left and right else size-1
                            if start > end or start >= size:
                                self.send_error(416)
                                return
                        self.send_response(206 if range_header else 200)
                        self.send_header('Content-Type', 'video/mp4')
                        self.send_header('Accept-Ranges', 'bytes')
                        self.send_header('Content-Length', str(end-start+1))
                        if range_header:
                            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                        self.end_headers()
                        file.seek(start)
                        remaining = end-start+1
                        while remaining:
                            data = file.read(min(256*1024, remaining))
                            if not data:
                                break
                            self.wfile.write(data)
                            remaining -= len(data)
                except (ConnectionError, OSError):
                    return

            def do_POST(self):
                record, action = self.source()
                origin = self.headers.get('Origin')
                if record is None or action != 'clip' or origin not in (None, owner.origin):
                    self.send_error(403)
                    return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 16384 or self.headers.get('Content-Type') != 'application/json':
                        raise ValueError('不正なリクエストです。')
                    data = json.loads(self.rfile.read(size))
                    pairs = data.get('ranges', [[data.get('start'), data.get('end')]])
                    if not isinstance(pairs, list) or not 1 <= len(pairs) <= 100:
                        raise ValueError('区間は1〜100件で指定してください。')
                    if any(not isinstance(pair, list) or len(pair) != 2 for pair in pairs):
                        raise ValueError('各区間の開始・終了を指定してください。')
                    ranges = tuple((float(a), float(b)) for a, b in pairs)
                    request = LocalClipRequest(record['path'], record['directory'], *ranges[0],
                                               record['title'], record['source_url'], ranges)
                    request.validate()
                except (ValueError, TypeError, KeyError, AttributeError, OSError) as error:
                    self.reply(400, {'error': str(error)})
                    return
                key = secrets.token_urlsafe(18)
                owner.results[key] = {'done': False}
                owner.pending.put((key, request))
                self.reply(202, {'id': key})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, path, directory, record):
        key = secrets.token_urlsafe(24)
        self.sources[key] = {'path': str(path), 'directory': directory,
                             'title': str(record.get('title') or path.stem),
                             'source_url': record.get('source_url', '')}
        return f'{self.origin}/{key}/'

    def poll(self, submit):
        while True:
            try:
                key, request = self.pending.get_nowait()
            except queue.Empty:
                return
            try:
                submit(request)
                self.results[key] = {'done': True}
            except (ValueError, OSError) as error:
                self.results[key] = {'error': str(error)}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
