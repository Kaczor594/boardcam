"""Write a labelling page for a label set's unlabelled photos, and merge its export back.

    python -m scripts.label_page data/labels/green-mat --split holdout
    open data/labels/green-mat/label-holdout.html
    python -m scripts.label_page data/labels/green-mat --merge ~/Downloads/labels-holdout.json

The page shows each photo beside an empty board in White's view (a8 top left).
Set the position with the palette, or paste a FEN from the lichess board editor.
Nothing is pre-filled: hold-out labels are entered from a blank board so they
are not anchored on a reader's guess. A photo that cannot be labelled (board
cut off, pieces hidden) is marked excluded with a reason, and stays out of the
score. Progress lives in the browser's localStorage; Export downloads JSON for
``--merge``. The page lives next to the photos, under data/, and is never
committed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import chess

PAGE = r"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BoardCam labels</title>
<style>
:root{--bg:#f4f2ec;--fg:#222;--light:#efe9da;--dark:#7f9c80;--line:#888;--accent:#b0392b}
@media (prefers-color-scheme:dark){:root{--bg:#1c1d1b;--fg:#e8e6e0;--light:#c9c2b0;--dark:#4f6b52;--line:#666;--accent:#e2735f}}
body{font:15px/1.45 system-ui;margin:16px;background:var(--bg);color:var(--fg)}
.wrap{display:flex;gap:20px;align-items:flex-start;flex-wrap:wrap}
img{max-height:88vh;max-width:min(55vw,100%);border:1px solid var(--line)}
table{border-collapse:collapse}td{width:48px;height:48px;text-align:center;font-size:34px;cursor:pointer;border:1px solid var(--line);user-select:none;color:#111}
td.l{background:var(--light)}td.d{background:var(--dark)}th{font:12px system-ui;opacity:.7;padding:2px 5px}
.pal button{font-size:26px;width:44px;height:44px;margin:2px;cursor:pointer;background:#fff;color:#111;border:1px solid var(--line)}
.pal button.on{outline:3px solid var(--accent)}
button{font-size:14px;padding:6px 11px;margin:3px 3px 3px 0}
input[type=text]{width:100%;font:13px monospace;padding:4px;box-sizing:border-box}
.ex{color:var(--accent)}
</style>
<h3 id="title"></h3>
<div class="wrap"><img id="photo"><div style="max-width:440px">
<p>Set the position as it stands on the board, White at the bottom. Ignore captured pieces off the board.</p>
<div class="pal" id="pal"></div><table id="board"></table>
<p><label>FEN (paste from <a href="https://lichess.org/editor" target="_blank">lichess.org/editor</a>)
<input type="text" id="fen"></label></p>
<p><label><input type="checkbox" id="done"> this board is correct</label><br>
<label><input type="checkbox" id="exc"> exclude this photo — why: <input type="text" id="why" style="width:60%;font:inherit"></label></p>
<div><button id="prev">← Previous</button><button id="next">Next →</button><span id="prog"></span></div>
<div><button id="flip">Rotate board 180°</button><button id="clr">Clear</button><button id="exp">Export</button></div>
</div></div>
<script>
const NAMES=__NAMES__, KEY=__KEY__, OUT=__OUT__;
const G={K:'♔',Q:'♕',R:'♖',B:'♗',N:'♘',P:'♙',k:'♚',q:'♛',r:'♜',b:'♝',n:'♞',p:'♟','.':''};
let S={};try{S=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){}
const EMPTY=()=>Array(8).fill('........');
for(const n of NAMES)if(!S[n])S[n]={rows:EMPTY(),done:false,excluded:null};
let i=0,cur='.';
const save=()=>{try{localStorage.setItem(KEY,JSON.stringify(S))}catch(e){}};
const fenOf=rows=>rows.map(r=>r.replace(/\.+/g,m=>m.length)).join('/');
const rowsOf=f=>{const rs=f.trim().split(' ')[0].split('/');if(rs.length!==8)return null;
 const o=rs.map(r=>r.replace(/[1-8]/g,d=>'.'.repeat(+d)));return o.every(r=>/^[KQRBNPkqrbnp.]{8}$/.test(r))?o:null};
const pal=document.getElementById('pal');
for(const k of 'KQRBNPkqrbnp.'){const b=document.createElement('button');b.textContent=G[k]||'✕';b.title=k;
 b.onclick=()=>{cur=k;[...pal.children].forEach(x=>x.classList.toggle('on',x===b))};pal.appendChild(b);if(k==='.')b.classList.add('on')}
function draw(){const n=NAMES[i],s=S[n];
 document.getElementById('title').textContent=`Photo ${n} (${i+1} of ${NAMES.length})`;
 document.getElementById('photo').src=`photos/${n}.jpg`;
 document.getElementById('done').checked=s.done;document.getElementById('exc').checked=!!s.excluded;
 document.getElementById('why').value=s.excluded||'';document.getElementById('fen').value=fenOf(s.rows);
 const t=document.getElementById('board');t.innerHTML='<tr><th></th>'+'abcdefgh'.split('').map(c=>`<th>${c}</th>`).join('')+'</tr>';
 for(let r=0;r<8;r++){const tr=document.createElement('tr');tr.innerHTML=`<th>${8-r}</th>`;
  for(let c=0;c<8;c++){const td=document.createElement('td');td.className=(r+c)%2?'d':'l';td.textContent=G[s.rows[r][c]];
   td.onclick=()=>{const a=s.rows[r].split('');a[c]=(a[c]===cur?'.':cur);s.rows[r]=a.join('');s.done=false;save();draw()};tr.appendChild(td)}
  t.appendChild(tr)}
 const d=NAMES.filter(x=>S[x].done).length,e=NAMES.filter(x=>S[x].excluded).length;
 document.getElementById('prog').textContent=` ${d} done, ${e} excluded, ${NAMES.length-d-e} to go`}
document.getElementById('fen').onchange=e=>{const r=rowsOf(e.target.value);if(r){S[NAMES[i]].rows=r;save()}draw()};
document.getElementById('done').onchange=e=>{S[NAMES[i]].done=e.target.checked;save();draw()};
const setExc=()=>{const on=document.getElementById('exc').checked,w=document.getElementById('why').value.trim();
 S[NAMES[i]].excluded=on?(w||'excluded'):null;save();draw()};
document.getElementById('exc').onchange=setExc;document.getElementById('why').onchange=setExc;
document.getElementById('prev').onclick=()=>{i=(i+NAMES.length-1)%NAMES.length;draw()};
document.getElementById('next').onclick=()=>{i=(i+1)%NAMES.length;draw()};
document.getElementById('flip').onclick=()=>{const s=S[NAMES[i]];s.rows=s.rows.slice().reverse().map(r=>r.split('').reverse().join(''));save();draw()};
document.getElementById('clr').onclick=()=>{S[NAMES[i]].rows=EMPTY();S[NAMES[i]].done=false;save();draw()};
document.getElementById('exp').onclick=()=>{const o={};for(const n of NAMES)o[n]={fen:fenOf(S[n].rows),done:S[n].done,excluded:S[n].excluded};
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(o,null,1)],{type:'application/json'}));a.download=OUT;a.click()};
draw();
</script>"""


def write_page(label_dir: Path, split: str) -> Path:
    truth = json.loads((label_dir / "truth.json").read_text())
    names = sorted(n for n, e in truth.items() if e.get("split") == split)
    key = f"boardcam-labels-{label_dir.name}-{split}"
    out = label_dir / f"label-{split}.html"
    out.write_text(PAGE.replace("__NAMES__", json.dumps(names))
                   .replace("__KEY__", json.dumps(key))
                   .replace("__OUT__", json.dumps(f"labels-{split}.json")))
    return out


def merge(label_dir: Path, exported: Path) -> dict:
    path = label_dir / "truth.json"
    truth = json.loads(path.read_text())
    got = json.loads(exported.read_text())
    counts = {"labelled": 0, "excluded": 0, "pending": 0}
    for name, e in got.items():
        if name not in truth:
            raise SystemExit(f"{name} is not in {path}")
        if e.get("excluded"):
            truth[name].update(excluded=e["excluded"], done=False)
            counts["excluded"] += 1
        elif e.get("done"):
            chess.Board(None).set_board_fen(e["fen"])          # raises on a bad FEN
            truth[name].update(fen=e["fen"], done=True, excluded=None)
            counts["labelled"] += 1
        else:
            counts["pending"] += 1
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(truth, indent=1))
    tmp.replace(path)
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("label_dir", type=Path)
    ap.add_argument("--split", default="holdout")
    ap.add_argument("--merge", type=Path, help="exported JSON to fold into truth.json")
    a = ap.parse_args(argv)
    if a.merge:
        print(merge(a.label_dir, a.merge))
    else:
        print(write_page(a.label_dir, a.split))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
