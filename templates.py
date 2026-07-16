"""Interface web DevLLMA -- HTML/CSS/JS de la page unique.

Extrait de webui.py (ex-lignes 566-1612) pour la lisibilite : c etait 42 pourcent
du fichier (1049 lignes) sans aucune logique metier, juste la page rendue au
navigateur. webui.py fait `from templates import HTML` et sert cette
constante telle quelle sur GET /.
"""

HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>DevLLMA</title>
<link rel="manifest" href="/static/manifest.json">
<meta name="theme-color" content="#0d1117">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="DevLLMA">
<link rel="apple-touch-icon" href="/static/icon-192.png">
<link rel="icon" href="/static/icon-512.png">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117;--sf:#161b22;--sf2:#1c2128;--bd:#30363d;
  --bl:#58a6ff;--gn:#3fb950;--pu:#bc8cff;--or:#f97316;--rd:#ef4444;
  --tx:#e6edf3;--mu:#8b949e
}
body{background:var(--bg);color:var(--tx);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}
/* Header */
header{padding:8px 16px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:8px;flex-shrink:0;flex-wrap:wrap}
.logo{font-family:monospace;font-size:.95rem;font-weight:700;color:var(--bl);letter-spacing:-.02em}
.chip{font-size:.6rem;font-family:monospace;padding:2px 7px;border-radius:3px;font-weight:700;letter-spacing:.03em}
.c-g{background:#22c55e18;color:#22c55e;border:1px solid #22c55e40}
.c-b{background:#58a6ff18;color:#58a6ff;border:1px solid #58a6ff40}
.c-p{background:#bc8cff18;color:#bc8cff;border:1px solid #bc8cff40}
.c-o{background:#f9731618;color:#fb923c;border:1px solid #f9731640}
.dot{width:7px;height:7px;border-radius:50%;background:var(--gn);animation:pu 2s infinite;flex-shrink:0}
@keyframes pu{0%,100%{box-shadow:0 0 3px var(--gn)}50%{box-shadow:0 0 8px var(--gn)}}
select{margin-left:auto;background:var(--sf);border:1px solid var(--bd);color:var(--tx);padding:3px 8px;border-radius:5px;font-size:.75rem;cursor:pointer}
#newSessionBtn{margin-left:6px;background:var(--bl);color:#fff;border:none;border-radius:5px;padding:4px 10px;font-size:.72rem;font-weight:600;cursor:pointer;white-space:nowrap}
#newSessionBtn:hover{filter:brightness(1.1)}
/* Barre ressources */
#statusbar{flex-shrink:0;display:flex;align-items:center;gap:18px;padding:4px 16px;border-top:1px solid var(--bd);background:var(--sf);font-family:monospace;font-size:.66rem;color:var(--mu)}
#statusbar .stat{display:flex;align-items:center;gap:6px}
#statusbar .stat-l{color:var(--mu);white-space:nowrap}
#statusbar .stat b{color:var(--tx);min-width:42px;text-align:right;font-weight:700}
#statusbar .stat-bar{width:72px;height:7px;background:var(--bg);border:1px solid var(--bd);border-radius:4px;overflow:hidden}
#statusbar .stat-bar i{display:block;height:100%;width:0%;background:var(--gn);transition:width .5s ease,background .5s}
#stat-host{margin-left:auto;color:var(--mu)}
/* Layout */
.layout{display:flex;flex:1;overflow:hidden}
/* Sidebar projets */
#sidebar{width:190px;flex-shrink:0;border-right:1px solid var(--bd);display:flex;flex-direction:column;overflow:hidden;background:var(--sf)}
.sb-head{padding:8px 10px;font-size:.62rem;font-family:monospace;color:var(--mu);text-transform:uppercase;letter-spacing:.07em;border-bottom:1px solid var(--bd);flex-shrink:0}
#proj-list{flex:1;overflow-y:auto;padding:4px}
#session-list{max-height:180px;overflow-y:auto;padding:4px;flex-shrink:0;border-bottom:1px solid var(--bd)}
.sess-item{padding:4px 6px;border-radius:5px;font-size:.71rem;font-family:monospace;display:flex;align-items:center;gap:4px;transition:background .15s}
.sess-item:hover{background:var(--sf2)}
.sess-item.active{background:#58a6ff18;color:var(--bl)}
.sess-label{flex:1;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sess-del{background:none;border:none;color:var(--mu);cursor:pointer;font-size:.85rem;line-height:1;padding:0 3px;flex-shrink:0;opacity:.55}
.sess-del:hover{opacity:1;color:var(--rd)}
.proj-item{padding:5px 8px;border-radius:5px;cursor:pointer;font-size:.73rem;font-family:monospace;display:flex;align-items:center;gap:5px;transition:background .15s}
.proj-item:hover{background:var(--sf2)}
.proj-item.active{background:#58a6ff18;color:var(--bl)}
.proj-dot{width:5px;height:5px;border-radius:50%;background:var(--gn);flex-shrink:0}
/* Todos sidebar */
#todos{border-top:1px solid var(--bd);padding:6px 10px;flex-shrink:0}
.todo-head{font-size:.6rem;font-family:monospace;color:var(--mu);text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px}
.todo-item{display:flex;align-items:flex-start;gap:5px;font-size:.7rem;color:var(--mu);line-height:1.4;padding:1px 0}
.todo-item.done{color:var(--gn);text-decoration:line-through}
.todo-item.active{color:var(--tx)}
.todo-cb{flex-shrink:0;width:11px;height:11px;border:1px solid var(--bd);border-radius:2px;margin-top:1px;display:flex;align-items:center;justify-content:center;font-size:.6rem}
.todo-cb.done{background:var(--gn);border-color:var(--gn);color:#000}
/* Chat */
#chat-area{flex:1;display:flex;flex-direction:column;overflow:hidden;position:relative}
#chat{flex:1;overflow-y:auto;padding:14px 18px;display:flex;flex-direction:column;gap:10px}
.msg{display:flex;flex-direction:column;gap:4px;max-width:92%}
.msg.user{align-self:flex-end;align-items:flex-end}
.msg.agent{align-self:flex-start;align-items:flex-start;width:100%;max-width:100%}
.atag{font-size:.58rem;font-family:monospace;padding:2px 6px;border-radius:3px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;display:inline-block;margin-bottom:2px}
.bbl{padding:9px 13px;border-radius:10px;font-size:.83rem;line-height:1.7;white-space:pre-wrap;word-break:break-word}
.msg.user .bbl{background:#1f6feb;color:#fff;border-bottom-right-radius:3px}
.msg.agent .bbl{background:var(--sf);border:1px solid var(--bd);border-bottom-left-radius:3px;width:100%}
.bbl code{font-family:monospace;background:#0d1117;padding:1px 4px;border-radius:3px;font-size:.77rem;color:#79c0ff}
.bbl pre{background:#0d1117;border:1px solid var(--bd);border-radius:6px;padding:9px 11px;overflow-x:auto;margin:6px 0;font-size:.73rem;font-family:monospace;line-height:1.5}
/* Think block */
.think{border-left:3px solid var(--pu);border-radius:0 6px 6px 0;background:#bc8cff08;margin:3px 0}
.think-h{display:flex;align-items:center;gap:6px;padding:6px 10px;cursor:pointer;font-size:.68rem;font-family:monospace;color:var(--pu);font-weight:700;user-select:none}
.think-h:hover{background:#ffffff05}
.arr{transition:transform .2s;font-size:.6rem}
.think.open .arr{transform:rotate(90deg)}
.think-b{padding:8px 11px 10px;font-size:.73rem;color:var(--mu);white-space:pre-wrap;font-family:monospace;line-height:1.55;border-top:1px solid var(--bd);display:none}
.think.open .think-b{display:block}
/* File card */
.fcard{display:flex;align-items:center;gap:7px;background:#0d1117;border-left:2px solid var(--gn);border-radius:0 5px 5px 0;padding:5px 10px;margin:2px 0;font-size:.72rem;font-family:monospace}
.fcard.err{border-color:var(--rd)}
.fcard .fn{color:var(--gn);font-weight:700}.fcard.err .fn{color:var(--rd)}
.fcard .fi{color:var(--mu);font-size:.64rem}
.fcard .fbtn{margin-left:auto;background:none;border:1px solid var(--bd);color:var(--mu);border-radius:4px;font-size:.6rem;padding:2px 6px;cursor:pointer;font-family:inherit}
.fcard .fbtn:hover{color:var(--tx);border-color:var(--bl)}
/* Arborescence workspace */
.tree-toggle{background:none;border:none;color:var(--mu);cursor:pointer;padding:0 2px;font-size:.65rem}
.tree-node{padding-left:16px}
.tree-file{display:flex;align-items:center;gap:5px;padding:2px 6px;font-size:.7rem;color:var(--tx);cursor:pointer;border-radius:4px}
.tree-file:hover{background:var(--sf2)}
.tree-dir{font-size:.7rem;color:var(--mu);padding:2px 6px;display:flex;align-items:center;gap:4px;cursor:pointer}
.tree-dir:hover{color:var(--tx)}
/* Modale de previsualisation de fichier */
#fileModal .modal-box{width:760px}
#fileModal pre{max-height:60vh;overflow:auto}
/* Run result */
.run-ok,.run-err{border-radius:6px;padding:8px 12px;margin:4px 0;font-size:.72rem;font-family:monospace}
.run-ok{background:#0f1f10;border:1px solid #22c55e33}
.run-err{background:#1f0e0e;border:1px solid #ef444433}
.run-label{font-size:.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px}
.run-ok .run-label{color:var(--gn)}.run-err .run-label{color:var(--rd)}
.run-out{color:var(--tx);white-space:pre-wrap}
/* Project done box */
.pbox{background:var(--sf);border:1px solid #58a6ff25;border-radius:7px;padding:9px 13px;margin:4px 0}
.ptitle{font-size:.67rem;font-family:monospace;color:var(--bl);font-weight:700;margin-bottom:4px;text-transform:uppercase;letter-spacing:.05em}
.ppath{font-family:monospace;font-size:.67rem;color:var(--mu);margin-bottom:6px}
.chips{display:flex;flex-wrap:wrap;gap:3px}
.fchip{background:#58a6ff12;border:1px solid #58a6ff28;color:#79c0ff;font-family:monospace;font-size:.65rem;padding:2px 5px;border-radius:3px}
/* Iteration */
.iter{font-size:.63rem;font-family:monospace;color:#f59e0b;padding:3px 8px;background:#f59e0b15;border:1px solid #f59e0b30;border-radius:4px;margin:3px 0;display:inline-block}
/* Snapshot badge */
.snap{font-size:.62rem;font-family:monospace;color:var(--mu);padding:3px 8px;background:var(--sf2);border:1px solid var(--bd);border-radius:4px;margin:3px 0;display:inline-flex;align-items:center;gap:8px}
.snap b{color:var(--bl)}
.snap button{background:none;border:1px solid var(--bd);color:var(--bl);border-radius:3px;font-size:.6rem;font-family:monospace;padding:1px 6px;cursor:pointer}
.snap button:hover{border-color:var(--bl)}
/* Memoire semantique (RAG) */
.mem{font-size:.62rem;font-family:monospace;color:var(--pu);padding:3px 8px;background:#bc8cff0f;border:1px solid #bc8cff30;border-radius:4px;margin:3px 0;display:inline-flex;flex-wrap:wrap;align-items:center;gap:6px}
.mem b{color:var(--pu)}
.mem .mitem{color:var(--mu);background:#00000030;border-radius:3px;padding:1px 5px}
/* Bouton copier code */
.pre-wrap{position:relative}
.copy-btn{position:absolute;top:5px;right:5px;background:var(--sf2);border:1px solid var(--bd);color:var(--mu);border-radius:4px;font-size:.62rem;font-family:monospace;padding:2px 7px;cursor:pointer;opacity:.75}
.copy-btn:hover{opacity:1;color:var(--tx);border-color:var(--bl)}
/* Coloration syntaxique legere */
.tok-kw{color:#ff7b72}.tok-str{color:#a5d6ff}.tok-com{color:#8b949e;font-style:italic}.tok-num{color:#79c0ff}.tok-fn{color:#d2a8ff}
/* Vitesse generation */
#speed-chip{font-size:.6rem;font-family:monospace;color:var(--gn);padding:2px 7px;border-radius:3px;background:#22c55e12;border:1px solid #22c55e30;display:none}
#colab-chip{font-size:.6rem;font-family:monospace;padding:2px 7px;border-radius:3px;border:1px solid;display:none;cursor:default}
#colab-chip.up{color:var(--gn);background:#22c55e12;border-color:#22c55e30}
#colab-chip.down{color:var(--rd);background:#ef444412;border-color:#ef444430}
/* Stop / regenerer */
#stopBtn{background:var(--rd);color:#fff;border:none;border-radius:8px;padding:0 13px;font-size:.86rem;cursor:pointer;font-weight:600;height:40px;white-space:nowrap;display:none}
#stopBtn:hover{filter:brightness(1.1)}
.regen-btn{background:none;border:1px solid var(--bd);color:var(--mu);border-radius:4px;font-size:.62rem;font-family:monospace;padding:2px 7px;cursor:pointer;margin-top:3px;align-self:flex-start}
.regen-btn:hover{color:var(--tx);border-color:var(--bl)}
/* Theme toggle + export */
#themeBtn,#exportBtn,#regenBtn,#modelsBtn{background:var(--sf);border:1px solid var(--bd);color:var(--tx);border-radius:5px;padding:3px 8px;font-size:.72rem;cursor:pointer}
#themeBtn:hover,#exportBtn:hover,#regenBtn:hover,#modelsBtn:hover{border-color:var(--bl)}
/* Modale gestion des modeles */
.modal-overlay{position:fixed;inset:0;background:#000a;display:flex;align-items:center;justify-content:center;z-index:200}
.modal-box{background:var(--sf);border:1px solid var(--bd);border-radius:10px;width:620px;max-width:92vw;max-height:85vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 12px 40px #000a}
.modal-head{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--bd);font-family:monospace;font-size:.85rem;font-weight:700}
.modal-body{padding:6px 14px 14px;overflow-y:auto;flex:1}
.modal-section-title{font-size:.65rem;font-family:monospace;color:var(--mu);text-transform:uppercase;letter-spacing:.06em;margin:14px 0 6px;border-bottom:1px solid var(--bd);padding-bottom:4px}
.model-row{display:flex;align-items:center;gap:8px;padding:4px 2px;font-size:.75rem;font-family:monospace;border-bottom:1px solid var(--bd)}
.model-row .mn{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.model-row .mn.mstar{color:var(--gn)}
.model-row .ms{color:var(--mu);font-size:.68rem;flex-shrink:0}
#pullRow{display:flex;gap:6px}
#pullInput{flex:1;background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:6px 9px;color:var(--tx);font-family:monospace;font-size:.8rem}
#pullBtn{background:var(--bl);color:#fff;border:none;border-radius:6px;padding:0 14px;font-size:.8rem;cursor:pointer}
#pullBtn:disabled{opacity:.5;cursor:not-allowed}
#pull-log{font-family:monospace;font-size:.66rem;color:var(--mu);white-space:pre-wrap;max-height:120px;overflow-y:auto;background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:6px;margin-top:6px;display:none}
.bench-row{display:flex;gap:8px;padding:3px 2px;font-size:.7rem;font-family:monospace;border-bottom:1px solid var(--bd)}
#memTestRow{display:flex;gap:6px}
#memTestRow input{flex:1;background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:6px 9px;color:var(--tx);font-family:monospace;font-size:.8rem}
#memTestRow button{background:var(--bl);color:#fff;border:none;border-radius:6px;padding:0 14px;font-size:.8rem;cursor:pointer}
.mem-row{display:flex;align-items:flex-start;gap:8px;padding:6px 2px;font-size:.7rem;border-bottom:1px solid var(--bd)}
.mem-row .mk{font-family:monospace;font-size:.6rem;padding:1px 6px;border-radius:3px;flex-shrink:0;text-transform:uppercase}
.mem-row .mc{flex:1;color:var(--tx);word-break:break-word}
.mem-row .mref{color:var(--mu);font-size:.62rem;display:block;margin-bottom:2px}
.mem-row .mdel{background:none;border:none;color:var(--mu);cursor:pointer;flex-shrink:0}
.mem-row .mdel:hover{color:var(--rd)}
.mem-score{color:var(--gn);font-family:monospace;font-size:.65rem;margin-left:6px}
.bench-row .bn{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* Scroll to bottom */
#scrollBtn{position:absolute;right:16px;bottom:66px;background:var(--bl);color:#fff;border:none;border-radius:50%;width:32px;height:32px;font-size:.9rem;cursor:pointer;display:none;box-shadow:0 2px 8px #0006;z-index:10}
/* Horodatage message */
.msg-time{font-size:.58rem;color:var(--mu);font-family:monospace;margin-top:1px}
/* Theme clair */
body.light{--bg:#f6f8fa;--sf:#ffffff;--sf2:#eef1f4;--bd:#d0d7de;--tx:#1f2328;--mu:#57606a}
body.light .bbl code{background:#eef1f4;color:#0550ae}
body.light .bbl pre{background:#f6f8fa}
/* Sécurité */
.secbox{background:var(--sf);border:1px solid #f9731633;border-radius:7px;padding:9px 13px;margin:4px 0}
.sec-title{font-size:.67rem;font-family:monospace;color:#fb923c;font-weight:700;margin-bottom:6px;text-transform:uppercase;letter-spacing:.05em}
.sec-item{display:flex;align-items:flex-start;gap:7px;font-size:.71rem;font-family:monospace;padding:3px 0;border-top:1px solid var(--bd)}
.sec-sev{flex-shrink:0;font-size:.58rem;font-weight:700;padding:1px 5px;border-radius:3px}
.sev-HAUTE{background:#ef444422;color:#f87171}
.sev-MOYENNE{background:#f59e0b22;color:#fbbf24}
.sev-BASSE{background:#58a6ff22;color:#79c0ff}
.sec-msg{color:var(--tx)}.sec-loc{color:var(--mu);font-size:.64rem}
/* Bloqué */
.blocked{background:#1f0d0d;border:1px solid var(--rd);border-radius:7px;padding:9px 13px;margin:4px 0;font-size:.74rem}
.blocked-t{color:var(--rd);font-weight:700;font-family:monospace;font-size:.67rem;text-transform:uppercase;margin-bottom:5px}
.blocked-r{color:var(--tx);font-family:monospace;font-size:.71rem;padding:1px 0}
/* Typing */
#typing{align-self:flex-start}
.dots{display:flex;gap:4px;padding:10px 13px;background:var(--sf);border:1px solid var(--bd);border-radius:10px;border-bottom-left-radius:3px}
.dots span{width:5px;height:5px;border-radius:50%;background:var(--mu);animation:bl 1.2s infinite}
.dots span:nth-child(2){animation-delay:.2s}.dots span:nth-child(3){animation-delay:.4s}
@keyframes bl{0%,80%,100%{opacity:.2}40%{opacity:1}}
/* Footer */
footer{padding:9px 16px;border-top:1px solid var(--bd);display:flex;gap:7px;flex-shrink:0}
#inp{flex:1;background:var(--sf);border:1px solid var(--bd);border-radius:8px;padding:8px 12px;color:var(--tx);font-size:.86rem;resize:none;height:40px;max-height:140px;overflow-y:auto;outline:none;font-family:inherit;transition:border-color .2s}
#inp:focus{border-color:var(--bl)}
#btn{background:var(--bl);color:#fff;border:none;border-radius:8px;padding:0 15px;font-size:.86rem;cursor:pointer;font-weight:600;height:40px;white-space:nowrap}
#btn:disabled{opacity:.4;cursor:not-allowed}
#clipBtn{background:var(--sf);color:var(--tx);border:1px solid var(--bd);border-radius:8px;width:40px;height:40px;font-size:1.05rem;cursor:pointer;flex-shrink:0}
#clipBtn:hover{border-color:var(--bl)}
body.dragging{outline:3px dashed var(--bl);outline-offset:-6px}
.s-btn{background:none;border:none;color:var(--mu);cursor:pointer;font-size:.8rem;padding:0 3px;line-height:1}
.s-btn:hover{color:var(--tx)}
/* Timeline des etapes d'outils de l'agent */
.tstep{align-self:flex-start;background:var(--sf);border:1px solid var(--bd);border-left:3px solid var(--bl);border-radius:7px;padding:6px 11px;margin:2px 0;font-size:.74rem;max-width:80%}
.tstep .tst-head{display:flex;align-items:center;gap:7px;font-family:monospace}
.tstep .tst-label{color:var(--tx);font-weight:600}
.tstep .tst-arg{color:var(--mu);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:340px}
.tstep .tst-status{margin-left:auto;font-weight:700}
.tstep.ok{border-left-color:var(--gn)}
.tstep.err{border-left-color:var(--rd)}
.tstep .tst-ms{color:var(--mu);font-size:.64rem}
.tstep details{margin-top:4px}
.tstep details summary{cursor:pointer;color:var(--mu);font-size:.64rem}
.tstep pre{white-space:pre-wrap;word-break:break-all;font-size:.66rem;color:var(--mu);margin-top:3px;max-height:180px;overflow-y:auto}
.tspin{width:10px;height:10px;border:2px solid var(--bd);border-top-color:var(--bl);border-radius:50%;animation:tsp .8s linear infinite;flex-shrink:0}
@keyframes tsp{to{transform:rotate(360deg)}}
/* Tableau resultats SQL */
.sqlwrap{align-self:flex-start;max-width:88%;overflow-x:auto;background:var(--sf);border:1px solid var(--bd);border-radius:7px;margin:3px 0}
.sqlwrap table{border-collapse:collapse;font-size:.72rem;font-family:monospace;min-width:200px}
.sqlwrap th{background:var(--sf2);color:var(--bl);padding:5px 10px;text-align:left;cursor:pointer;user-select:none;white-space:nowrap;border-bottom:1px solid var(--bd)}
.sqlwrap th:hover{color:var(--tx)}
.sqlwrap td{padding:4px 10px;color:var(--tx);border-bottom:1px solid var(--bd)}
.sqlwrap tr:last-child td{border-bottom:none}
.sql-meta{display:flex;align-items:center;gap:8px;padding:4px 10px;font-size:.64rem;color:var(--mu);border-bottom:1px solid var(--bd)}
.sql-copy{background:none;border:1px solid var(--bd);color:var(--mu);border-radius:5px;font-size:.62rem;padding:2px 8px;cursor:pointer;margin-left:auto}
.sql-copy:hover{color:var(--tx);border-color:var(--bl)}
/* Chip piece jointe document */
#attach-bar{display:none;padding:4px 16px 0;flex-shrink:0}
.attach-chip{display:inline-flex;align-items:center;gap:7px;background:var(--sf);border:1px solid var(--bl);border-radius:15px;padding:4px 12px;font-size:.72rem;color:var(--tx)}
.attach-chip .ac-x{cursor:pointer;color:var(--mu);font-weight:700}
.attach-chip .ac-x:hover{color:var(--rd)}
/* Recherche sessions */
#searchBox{width:calc(100% - 16px);margin:4px 8px;background:var(--sf);border:1px solid var(--bd);border-radius:6px;padding:5px 9px;color:var(--tx);font-size:.72rem;outline:none}
#searchBox:focus{border-color:var(--bl)}
.snip mark{background:#f59e0b44;color:var(--tx);border-radius:2px;padding:0 1px}
.snip{font-size:.62rem;color:var(--mu);margin-top:2px;line-height:1.35}
/* Menu slash (bibliotheque de prompts) */
#slashMenu{display:none;position:absolute;bottom:62px;left:16px;right:16px;max-width:560px;background:var(--sf);border:1px solid var(--bd);border-radius:9px;box-shadow:0 8px 24px #0008;z-index:60;max-height:260px;overflow-y:auto}
.slash-item{padding:8px 13px;font-size:.76rem;cursor:pointer;border-bottom:1px solid var(--bd)}
.slash-item:last-child{border-bottom:none}
.slash-item.selected,.slash-item:hover{background:var(--sf2)}
.slash-item b{color:var(--bl);font-family:monospace;font-size:.7rem}
.slash-item span{color:var(--mu);display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#saveTplBtn{background:var(--sf);color:var(--mu);border:1px solid var(--bd);border-radius:8px;width:40px;height:40px;font-size:.95rem;cursor:pointer;flex-shrink:0}
#saveTplBtn:hover{border-color:var(--bl);color:var(--tx)}

/* ── Responsive : tablette et mobile, avec detection du support ── */
#sidebarToggle{display:none;background:none;border:none;color:var(--tx);font-size:1.15rem;padding:2px 6px;cursor:pointer;flex-shrink:0}
#sidebarOverlay{display:none;position:fixed;inset:0;background:#000a;z-index:99}
.btn-label{display:inline}

/* Tablette (<=900px) et mobile : sidebar devient un tiroir escamotable */
@media (max-width:900px){
  #sidebarToggle{display:block}
  .layout{position:relative}
  #sidebar{position:fixed;top:0;left:0;bottom:0;width:min(78vw,300px);z-index:100;
    transform:translateX(-100%);transition:transform .22s ease;box-shadow:4px 0 24px #0008}
  body.sidebar-open #sidebar{transform:translateX(0)}
  body.sidebar-open #sidebarOverlay{display:block}
  header{gap:6px}
  .btn-label{display:none}
  #newSessionBtn,#exportBtn,#regenBtn,#modelsBtn{padding:6px 9px}
  select{max-width:38vw}
}

/* Mobile (<=600px) : layout une colonne, cibles tactiles agrandies */
@media (max-width:600px){
  .chip.hide-compact{display:none}
  .logo{font-size:.78rem}
  header{padding:6px 6px;gap:4px}
  select{max-width:24vw;font-size:.65rem;padding:3px 4px}
  #newSessionBtn,#exportBtn,#regenBtn,#modelsBtn,#themeBtn{padding:5px 7px}
  #chat{padding:10px 10px}
  .msg{max-width:96%!important}
  footer{padding:8px 10px;flex-wrap:wrap}
  #inp{font-size:16px} /* >=16px evite le zoom automatique de Safari/Chrome mobile au focus */
  #statusbar{overflow-x:auto;gap:12px;white-space:nowrap}
  #statusbar .stat-bar{width:46px}
  .modal-box{width:96vw!important;max-height:92vh}
  #fileModal .modal-box{width:96vw}
  .sqlwrap,.tstep{max-width:100%}
}

/* Ecrans tactiles (tablette ou mobile, quelle que soit la resolution) :
   cibles agrandies pour le doigt plutot que la souris. */
@media (pointer:coarse){
  button,.s-btn,.fbtn,.tree-toggle,.sess-del,select{min-height:34px}
  #btn,#stopBtn,#clipBtn,#saveTplBtn{min-width:40px}
  .slash-item,.mem-row,.tree-file,.tree-dir{padding-top:9px;padding-bottom:9px}
}
</style>
</head>
<body>
<header>
  <button id="sidebarToggle" onclick="toggleSidebar()" title="Sessions et projets" aria-label="Menu">&#9776;</button>
  <div class="dot"></div>
  <span class="logo">&gt;_ DevLLMA</span>
  <span class="chip c-p hide-compact">&#129504; Brain actif</span>
  <span class="chip c-g hide-compact">&#9889; Exécution</span>
  <span class="chip c-b hide-compact">&#128196; Lecture/Écriture</span>
  <span class="chip c-o hide-compact">&#128260; Auto-correction</span>
  <span id="colab-chip" title="État du worker GPU Colab">&#9889; Colab</span>
  <select id="modelSel" onchange="chgM(this.value)" title="Choisir le modèle IA">
    <option>chargement…</option>
  </select>
  <button id="newSessionBtn" onclick="newSession()" title="Démarrer une nouvelle session">&#10133; <span class="btn-label">Nouvelle session</span></button>
  <button id="exportBtn" onclick="exportSession()" title="Exporter la session en markdown">&#11015; <span class="btn-label">Export</span></button>
  <button id="regenBtn" onclick="regenLast()" title="Regenerer la derniere reponse">&#8635; <span class="btn-label">Regenerer</span></button>
  <button id="modelsBtn" onclick="openModelsModal()" title="Gerer les modeles Ollama">&#128230; <span class="btn-label">Modeles</span></button>
  <button id="themeBtn" onclick="toggleTheme()" title="Theme clair/sombre">&#127768;</button>
</header>
<div class="layout">
  <div id="sidebarOverlay" onclick="closeSidebar()"></div>
  <div id="sidebar">
    <div class="sb-head">&#128172; Sessions</div>
    <input id="searchBox" placeholder="&#128269; Rechercher dans les sessions..." autocomplete="off">
    <div id="session-list"><div style="padding:8px;font-size:.7rem;color:var(--mu)">Chargement...</div></div>
    <div class="sb-head">&#128193; Workspace</div>
    <div id="proj-list"><div style="padding:8px;font-size:.7rem;color:var(--mu)">Chargement...</div></div>
    <div id="todos">
      <div class="todo-head">&#9745; Tâches en cours</div>
      <div id="todo-list"><div style="font-size:.7rem;color:var(--mu)">En attente...</div></div>
    </div>
  </div>
  <div id="chat-area">
    <div id="chat">
      <div class="msg agent">
        <div class="atag" style="background:#f59e0b22;color:#f59e0b">&#9679; brain</div>
        <div class="bbl">Prêt. Mon cerveau est chargé avec la mémoire du workspace.<br><br>
&#8226; <code>Crée une API FastAPI avec SQLite et authentification</code><br>
&#8226; <code>Fais un tableau de bord HTML avec graphiques</code><br>
&#8226; <code>Développe un scraper Python avec export CSV</code><br>
&#8226; <code>Cree un dossier test sur le bureau</code><br>
&#8226; <code>Quelle est la derniere version de Python ?</code> (recherche web)</div>
      </div>
    </div>
    <div id="attach-bar"></div>
    <footer style="position:relative">
      <div id="slashMenu"></div>
      <input type="file" id="imgInput" accept=".png,.jpg,.jpeg,.webp,.bmp,.docx,.xlsx,.pdf,image/*" style="display:none" onchange="handleFile(this.files[0]);this.value=''">
      <button id="clipBtn" onclick="document.getElementById('imgInput').click()" title="Joindre une image (OCR) ou un document Word/Excel/PDF — ou colle/dépose le fichier">&#128206;</button>
      <textarea id="inp" placeholder="Décris ta tâche… (/ pour les modèles de prompts, colle/dépose une image ou un document)" onkeydown="onK(event)" oninput="onInp()"></textarea>
      <button id="saveTplBtn" onclick="saveTemplate()" title="Sauver le texte comme modèle de prompt">&#128190;</button>
      <button id="btn" onclick="send()">Envoyer &#9654;</button>
      <button id="stopBtn" onclick="stopGen()">&#9209; Stop</button>
    </footer>
    <button id="scrollBtn" onclick="chat.scrollTop=chat.scrollHeight" title="Aller en bas">&#8595;</button>
  </div>
</div>
<!-- Panneau de gestion des modeles Ollama -->
<div id="modelsModal" class="modal-overlay" style="display:none" onclick="if(event.target===this)closeModelsModal()">
  <div class="modal-box">
    <div class="modal-head"><span>&#128230; Gestion des modeles</span>
      <button class="s-btn" onclick="closeModelsModal()" title="Fermer">&#10005;</button>
    </div>
    <div class="modal-body">
      <div class="modal-section-title">Modeles installes</div>
      <div id="models-table"><div style="color:var(--mu);font-size:.72rem">Chargement...</div></div>
      <div class="modal-section-title">Telecharger un nouveau modele</div>
      <div id="pullRow">
        <input id="pullInput" placeholder="ex: qwen3-coder:30b (voir ollama.com/library)" onkeydown="if(event.key==='Enter')pullModel()">
        <button id="pullBtn" onclick="pullModel()">Telecharger</button>
      </div>
      <div id="pull-log"></div>
      <div class="modal-section-title">Derniers resultats de benchmark</div>
      <div id="bench-table"><div style="color:var(--mu);font-size:.72rem">Chargement...</div></div>
    </div>
  </div>
</div>
<!-- Previsualisation d'un fichier du workspace -->
<div id="fileModal" class="modal-overlay" style="display:none" onclick="if(event.target===this)closeFileModal()">
  <div class="modal-box">
    <div class="modal-head"><span id="fileModalTitle">&#128196; Fichier</span>
      <span style="margin-left:auto"></span>
      <button class="s-btn" onclick="downloadCurrentFile()" title="Telecharger">&#11015;</button>
      <button class="s-btn" onclick="closeFileModal()" title="Fermer">&#10005;</button>
    </div>
    <div class="modal-body"><div id="fileModalBody" style="color:var(--mu);font-size:.72rem">Chargement...</div></div>
  </div>
</div>
<!-- Memoire semantique -->
<div id="memModal" class="modal-overlay" style="display:none" onclick="if(event.target===this)closeMemModal()">
  <div class="modal-box">
    <div class="modal-head"><span>&#129504; Memoire semantique</span>
      <button class="s-btn" onclick="closeMemModal()" title="Fermer">&#10005;</button>
    </div>
    <div class="modal-body">
      <div class="modal-section-title">Tester ce dont l'agent se souviendrait</div>
      <div id="memTestRow">
        <input id="memTestInput" placeholder="Tape une demande pour voir les souvenirs associes..." onkeydown="if(event.key==='Enter')testMemory()">
        <button onclick="testMemory()">Tester</button>
      </div>
      <div id="memTestResult"></div>
      <div class="modal-section-title">Souvenirs enregistres <button class="s-btn" style="margin-left:8px;border:1px solid var(--rd);color:var(--rd);border-radius:4px;padding:1px 6px" onclick="purgeMemories()">Tout purger</button></div>
      <div id="mem-table"><div style="color:var(--mu);font-size:.72rem">Chargement...</div></div>
    </div>
  </div>
</div>
<!-- Barre d'usage ressources temps réel -->
<div id="statusbar">
  <span class="stat"><span class="stat-l">CPU</span><span class="stat-bar"><i id="cpu-fill"></i></span><b id="cpu-v">--</b></span>
  <span class="stat"><span class="stat-l">RAM</span><span class="stat-bar"><i id="ram-fill"></i></span><b id="ram-v">--</b></span>
  <span class="stat"><span class="stat-l">&#127777; Temp</span><b id="temp-v">--</b></span>
  <span id="speed-chip">-- tok/s</span>
  <span id="mem-count" style="font-size:.62rem;color:var(--mu);cursor:pointer" title="Cliquer pour gerer la memoire" onclick="openMemModal()">&#129504; -- souvenirs</span>
  <span id="stat-host">192.168.1.30</span>
</div>
<script>
// ── Detection du support (tablette/mobile/desktop, tactile ou non) ──
// Ajuste des classes sur <body> pour que le CSS ET le JS puissent reagir sans
// dupliquer les seuils : la largeur seule ne suffit pas (un ordinateur portable
// en fenetre reduite n'est pas une tablette; une tablette en mode paysage peut
// depasser 900px mais reste tactile).
function detectDevice(){
  const w=window.innerWidth;
  const touch=matchMedia("(pointer:coarse)").matches;
  document.body.classList.toggle("device-mobile",w<=600);
  document.body.classList.toggle("device-tablet",w>600&&w<=900);
  document.body.classList.toggle("device-desktop",w>900);
  document.body.classList.toggle("is-touch",touch);
  if(w>900)document.body.classList.remove("sidebar-open");
}
detectDevice();
window.addEventListener("resize",detectDevice);
function isCompactLayout(){return document.body.classList.contains("device-mobile")||document.body.classList.contains("device-tablet");}
function toggleSidebar(){document.body.classList.toggle("sidebar-open");}
function closeSidebar(){document.body.classList.remove("sidebar-open");}
function closeSidebarIfCompact(){if(isCompactLayout())closeSidebar();}

const chat=document.getElementById("chat"),inp=document.getElementById("inp"),btn=document.getElementById("btn");
let ws,curB=null,curR="",curW=null,todos=[],projList=[];
const AC={brain:"#f59e0b",coder:"#3b82f6",architect:"#8b5cf6",debugger:"#ef4444",reviewer:"#10b981",
          tester:"#06b6d4",devops:"#f97316",database:"#6366f1",frontend:"#ec4899",
          backend:"#14b8a6",security:"#dc2626",systeme:"#22c55e",researcher:"#0ea5e9",agent:"#a3e635"};

// ── Sessions ──
const INITIAL_SESSION = new URLSearchParams(location.search).get("session") || "";
let currentSid = null;
const WELCOME = '<div class="msg agent"><div class="atag" style="background:#f59e0b22;color:#f59e0b">&#9679; brain</div>'
  + '<div class="bbl">Pr&#234;t. Mon cerveau est charg&#233; avec la m&#233;moire du workspace.<br><br>'
  + '&#8226; <code>Cr&#233;e une API FastAPI avec SQLite</code><br>'
  + '&#8226; <code>Fais un tableau de bord HTML avec graphiques</code><br>'
  + '&#8226; <code>D&#233;veloppe un scraper Python avec export CSV</code><br>'
  + '&#8226; <code>Quelle est la derni&#232;re version de Python ?</code> (recherche web)</div></div>';
function clearChat(){chat.innerHTML=WELCOME;curB=null;curR="";curW=null;todos=[];renderTodos();}
function newSession(){
  if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:"new_session"}));
  closeSidebarIfCompact();
}
function openSession(id){
  if(!id)return;
  window.open(location.pathname+"?session="+encodeURIComponent(id),"_blank");
}
function deleteSession(id,evt){
  if(evt)evt.stopPropagation();
  if(!confirm("Supprimer la session #"+id+" ? Cette action est irreversible."))return;
  fetch("/sessions/"+id,{method:"DELETE"}).then(r=>r.json()).then(()=>{loadSessions();}).catch(()=>{});
}
function loadSessions(){
  fetch("/sessions").then(r=>r.json()).then(d=>{
    const el=document.getElementById("session-list");
    const list=d.sessions||[];
    if(!list.length){el.innerHTML='<div style="padding:8px;font-size:.7rem;color:var(--mu)">Aucune session</div>';return;}
    el.innerHTML=list.map(s=>{
      const dt=(s.created_at||"").substring(5,16);
      const active=(String(s.id)===String(currentSid))?" active":"";
      return '<div class="sess-item'+active+'">'
        +'<span class="sess-label" onclick="openSession('+s.id+')" title="Ouvrir dans un nouvel onglet">#'+s.id+' '+esc(dt)+'</span>'
        +'<button class="sess-del" onclick="deleteSession('+s.id+',event)" title="Supprimer">&times;</button></div>';
    }).join("");
  }).catch(()=>{});
}
// ── Recherche dans l'historique des sessions ──
let searchTimer=null;
document.getElementById("searchBox").addEventListener("input",function(){
  clearTimeout(searchTimer);
  const q=this.value.trim();
  if(!q){loadSessions();return;}
  searchTimer=setTimeout(()=>{
    fetch("/search?q="+encodeURIComponent(q)).then(r=>r.json()).then(d=>{
      const el=document.getElementById("session-list");
      const res=d.results||[];
      if(!res.length){el.innerHTML='<div style="padding:8px;font-size:.7rem;color:var(--mu)">Aucun r&#233;sultat</div>';return;}
      const rxSafe=q.replace(/[.*+?^${}()|[\]\\]/g,"\\$&");
      el.innerHTML=res.map(s=>{
        // esc() D'ABORD, puis surlignage du terme echappe (jamais l'inverse -> injection)
        let snip=esc(s.snippet);
        snip=snip.replace(new RegExp("("+esc(rxSafe)+")","gi"),"<mark>$1</mark>");
        return '<div class="sess-item">'
          +'<span class="sess-label" onclick="openSession('+s.session_id+')">#'+s.session_id
          +' '+esc((s.ts||"").substring(5,16))+'<div class="snip">'+snip+'</div></span></div>';
      }).join("");
    }).catch(()=>{});
  },300);
});

function connect(){
  ws=new WebSocket("ws://"+location.host+"/ws");
  ws.onopen=()=>{
    ws.send(JSON.stringify({type:"init",session:INITIAL_SESSION}));
    ws.send(JSON.stringify({type:"get_projects"}));
    loadSessions();
  };
  ws.onmessage=e=>{
    const d=JSON.parse(e.data);
    if(d.type==="token"){curR+=d.text;if(curB)curB.innerHTML=fmt(curR);sc();}
    else if(d.type==="agent_start"){
      rmT();const c=AC[d.agent]||"#8b949e";setGenerating(true);
      curW=mk("div","msg agent");
      curW.innerHTML='<div class="atag" style="background:'+c+'22;color:'+c+'">&#9679; '+d.agent+'</div><div class="bbl"></div><div class="msg-time">'+nowStr()+'</div>';
      chat.appendChild(curW);curB=curW.querySelector(".bbl");curR="";sc();
    }
    else if(d.type==="speed"){
      const sp=document.getElementById("speed-chip");sp.style.display="inline-block";sp.textContent=d.tps+" tok/s";
    }
    else if(d.type==="colab_status"){
      const cc=document.getElementById("colab-chip");
      if(!d.configured){cc.style.display="none";}
      else{
        cc.style.display="inline-block";
        cc.className=d.up?"up":"down";
        cc.innerHTML=d.up?"&#9889; Colab":"&#9889; Colab hors ligne";
        cc.title=d.up?"Worker GPU Colab joignable — les taches lourdes partent sur le GPU"
                     :"Worker GPU Colab injoignable — generation en local (relance le notebook Colab, Executer tout)";
      }
    }
    else if(d.type==="memory"){
      const el=mk("div","mem");
      el.innerHTML='&#129504; <b>Memoire</b>'+d.items.map(m=>'<span class="mitem">'+esc(m.ref)+' ('+m.score+')</span>').join("");
      (curW||chat).appendChild(el);sc();
    }
    else if(d.type==="tool_step"){
      if(d.phase==="start"){
        rmT();
        const el=mk("div","tstep");el.dataset.stepId=d.id;
        el.innerHTML='<div class="tst-head"><div class="tspin"></div><span class="tst-label">'+esc(d.label)+'</span>'
          +'<span class="tst-arg">'+esc(d.args_preview||"")+'</span><span class="tst-status"></span></div>'
          +'<details><summary>d&#233;tails</summary><pre>'+esc(d.args_full||"")+'</pre></details>';
        chat.appendChild(el);sc();
      }else{
        const el=chat.querySelector('.tstep[data-step-id="'+d.id+'"]');
        if(el){
          el.classList.add(d.ok?"ok":"err");
          const spin=el.querySelector(".tspin");if(spin)spin.remove();
          const st=el.querySelector(".tst-status");
          if(st)st.innerHTML=(d.ok?'<span style="color:var(--gn)">&#10003;</span>':'<span style="color:var(--rd)">&#10007;</span>')
            +(d.ms!=null?' <span class="tst-ms">'+(d.ms>=1000?(d.ms/1000).toFixed(1)+"s":d.ms+"ms")+'</span>':'');
          const pre=el.querySelector("pre");
          if(pre&&d.result_preview)pre.textContent+="\n→ "+d.result_preview;
        }
        sc();
      }
    }
    else if(d.type==="sql_result"){
      const wrap=mk("div","sqlwrap");
      wrap._rows=d.rows;wrap._cols=d.columns;wrap._sortCol=-1;wrap._sortAsc=true;
      let html='<div class="sql-meta">&#128202; '+d.rows.length+' ligne(s)'+(d.truncated?' (tronqu&#233;)':'')
        +'<button class="sql-copy">Copier CSV</button></div><table><thead><tr>';
      html+=d.columns.map((c,i)=>'<th data-col="'+i+'">'+esc(c)+'</th>').join("")+'</tr></thead><tbody>';
      html+=d.rows.map(r=>'<tr>'+r.map(c=>'<td>'+esc(c==null?"":c)+'</td>').join("")+'</tr>').join("");
      html+='</tbody></table>';
      wrap.innerHTML=html;
      wrap.addEventListener("click",ev=>{
        const th=ev.target.closest("th");
        if(th){
          const col=parseInt(th.dataset.col,10);
          wrap._sortAsc=(wrap._sortCol===col)?!wrap._sortAsc:true;wrap._sortCol=col;
          const sorted=[...wrap._rows].sort((a,b)=>{
            const x=a[col],y=b[col];
            const nx=parseFloat(x),ny=parseFloat(y);
            const cmp=(!isNaN(nx)&&!isNaN(ny))?nx-ny:String(x??"").localeCompare(String(y??""));
            return wrap._sortAsc?cmp:-cmp;
          });
          wrap.querySelector("tbody").innerHTML=sorted.map(r=>'<tr>'+r.map(c=>'<td>'+esc(c==null?"":c)+'</td>').join("")+'</tr>').join("");
        }
        const cp=ev.target.closest(".sql-copy");
        if(cp){
          const q=v=>'"'+String(v==null?"":v).replace(/"/g,'""')+'"';
          const csv=[wrap._cols.map(q).join(";")].concat(wrap._rows.map(r=>r.map(q).join(";"))).join("\n");
          navigator.clipboard.writeText(csv).then(()=>{const o=cp.textContent;cp.textContent="✓ Copié";setTimeout(()=>cp.textContent=o,1200);}).catch(()=>{});
        }
      });
      chat.appendChild(wrap);sc();
    }
    else if(d.type==="stopped"){
      setGenerating(false);
      const el=mk("div","mem");el.style.color="var(--or)";el.style.background="#f973160f";el.style.borderColor="#f9731630";
      el.innerHTML="&#9209; Generation arretee par l'utilisateur";
      (curW||chat).appendChild(el);sc();
    }
    else if(d.type==="pull_progress"){
      const log=document.getElementById("pull-log");
      if(log){log.style.display="block";log.textContent+=d.line+"\n";log.scrollTop=log.scrollHeight;}
    }
    else if(d.type==="pull_done"){
      const log=document.getElementById("pull-log");
      if(log)log.textContent+=(d.ok?"\n✔ Téléchargement terminé.\n":"\n✘ Échec : "+(d.error||"")+"\n");
      const btn2=document.getElementById("pullBtn"),inp2=document.getElementById("pullInput");
      if(btn2){btn2.disabled=false;inp2.disabled=false;}
      loadModelsDetail();loadModels();
    }
    else if(d.type==="brain_think"){
      const tb=mk("div","think");
      tb.innerHTML='<div class="think-h" onclick="this.parentElement.classList.toggle(\'open\')"><span class="arr">&#9658;</span><span>&#129504; Réflexion Brain</span><span style="margin-left:auto;color:var(--mu);font-weight:400;font-size:.6rem">clic</span></div><div class="think-b">'+esc(d.text)+'</div>';
      (curW||chat).appendChild(tb);sc();
    }
    else if(d.type==="todos"){
      todos=d.items;renderTodos();
    }
    else if(d.type==="todo_done"){
      todos=todos.map((t,i)=>i===d.index?{...t,done:true}:t);renderTodos();
    }
    else if(d.type==="file_created"){
      const e2=mk("div","fcard");
      e2.innerHTML='<span>&#128196;</span><span class="fn">'+esc(d.name)+'</span><span class="fi">'+esc(d.size)+'</span>'
        +(d.path?'<button class="fbtn" onclick="openFilePreview(\''+esc(d.path).replace(/'/g,"\\'")+'\')">Aper&#231;u</button>'
                +'<a class="fbtn" style="text-decoration:none" href="/dl?p='+encodeURIComponent(d.path)+'">&#11015;</a>':'');
      (curW||chat).appendChild(e2);sc();
    }
    else if(d.type==="project_done"){
      const b=mk("div","pbox");
      b.innerHTML='<div class="ptitle">&#9989; Projet créé — '+d.count+' fichier(s)</div><div class="ppath">&#128193; '+esc(d.path)+'</div><div class="chips">'+d.files.map(f=>'<span class="fchip">'+esc(f)+'</span>').join("")+'</div>';
      (curW||chat).appendChild(b);
      addProj(d.project_name,d.path);sc();
    }
    else if(d.type==="run_result"){
      const ok=d.ok,el=mk("div",ok?"run-ok":"run-err");
      el.innerHTML='<div class="run-label">'+(ok?"&#9654; Exécuté — OK":"&#9888; Erreur d\'exécution")+(d.entry?" ("+esc(d.entry)+")":"")+'</div><div class="run-out">'+esc(d.output||"(aucune sortie)")+'</div>';
      (curW||chat).appendChild(el);sc();
    }
    else if(d.type==="iter_start"){
      const it=mk("div","iter");
      it.textContent="&#128260; Auto-correction — tentative "+d.n+"/3";
      (curW||chat).appendChild(it);sc();
    }
    else if(d.type==="exec_result"){
      const ok=d.status==="ok",el=mk("div","fcard"+(ok?"":" err"));
      el.innerHTML='<span>'+(ok?"&#10003;":"&#10007;")+'</span><span class="fn">'+esc(d.output||"OK")+'</span>';
      (curW||chat).appendChild(el);sc();
    }
    else if(d.type==="snapshot"){
      const el=mk("div","snap");
      el.innerHTML='&#128190; Sauvegarde créée &#8212; <b>#'+d.id+'</b> ('+d.files+' fichiers)'
        +'<button onclick="restoreSnap('+d.id+')">&#9100; Restaurer</button>';
      (curW||chat).appendChild(el);sc();
    }
    else if(d.type==="security"){
      const b=mk("div","secbox");
      let h='<div class="sec-title">&#128737; Sécurité &#8212; '+d.findings.length+' point(s) détecté(s)</div>';
      h+=d.findings.map(f=>'<div class="sec-item"><span class="sec-sev sev-'+f.severity+'">'+f.severity+'</span><div><div class="sec-msg">'+esc(f.message)+'</div><div class="sec-loc">'+esc(f.file)+':'+f.line+'</div></div></div>').join("");
      b.innerHTML=h;(curW||chat).appendChild(b);sc();
    }
    else if(d.type==="blocked"){
      const b=mk("div","blocked");
      b.innerHTML='<div class="blocked-t">&#9940; Exécution bloquée &#8212; code potentiellement destructeur</div>'+d.reasons.map(r=>'<div class="blocked-r">&#8226; '+esc(r)+'</div>').join("");
      (curW||chat).appendChild(b);sc();
    }
    else if(d.type==="projects"){
      projList=d.items;renderProjs();
    }
    else if(d.type==="done"){curB=null;curR="";curW=null;setGenerating(false);inp.focus();}
    else if(d.type==="session_set"){currentSid=d.sid;}
    else if(d.type==="session_new"){
      currentSid=d.sid;clearChat();loadSessions();
      const n=mk("div","msg agent");
      n.innerHTML='<div class="atag" style="background:#22c55e22;color:#22c55e">&#9679; systeme</div><div class="bbl">Nouvelle session #'+d.sid+' d&#233;marr&#233;e.</div>';
      chat.appendChild(n);sc();
    }
    else if(d.type==="session_history"){
      currentSid=d.sid;chat.innerHTML="";
      const banner=mk("div","msg agent");
      banner.innerHTML='<div class="atag" style="background:#58a6ff22;color:#58a6ff">&#9679; session</div><div class="bbl">Reprise de la session #'+d.sid+' ('+(d.messages?d.messages.length:0)+' messages).</div>';
      chat.appendChild(banner);
      (d.messages||[]).forEach(m=>{
        if(m.role==="user"){
          const w=mk("div","msg user");w.innerHTML='<div class="bbl">'+esc(m.content)+'</div>';chat.appendChild(w);
        }else{
          const c=AC[m.agent]||"#8b949e";const w=mk("div","msg agent");
          w.innerHTML='<div class="atag" style="background:'+c+'22;color:'+c+'">&#9679; '+esc(m.agent)+'</div><div class="bbl">'+fmt(m.content)+'</div>';
          chat.appendChild(w);
        }
      });
      loadSessions();sc();
    }
    else if(d.type==="thinking"){addT();}
  };
  ws.onclose=()=>setTimeout(connect,1500);
}

function renderTodos(){
  const el=document.getElementById("todo-list");
  if(!todos.length){el.innerHTML='<div style="font-size:.7rem;color:var(--mu)">En attente...</div>';return;}
  el.innerHTML=todos.map((t,i)=>'<div class="todo-item'+(t.done?" done":t.active?" active":"")+'"><div class="todo-cb'+(t.done?" done":"")+'">'+( t.done?"&#10003;":"")+'</div><span>'+esc(t.text)+'</span></div>').join("");
}
function renderProjs(){
  const el=document.getElementById("proj-list");
  if(!projList.length){el.innerHTML='<div style="padding:8px;font-size:.7rem;color:var(--mu)">Aucun projet</div>';return;}
  el.innerHTML=projList.map(p=>'<div class="proj-item" title="Clic: reprendre ce projet" data-p="'+esc(p)+'">'
    +'<span class="tree-toggle" onclick="event.stopPropagation();toggleTree(this,'+"'"+esc(p).replace(/'/g,"\\'")+"'"+')">&#9656;</span>'
    +'<span onclick="resumeProject(this.parentElement.dataset.p)" style="flex:1;cursor:pointer"><div class="proj-dot" style="display:inline-block"></div>'+esc(p)+'</span>'
    +'</div><div class="tree-holder" data-holder="'+esc(p)+'"></div>').join("");
}
function resumeProject(name){
  // Pre-remplit l'entree pour reprendre/continuer le projet (DevLLMA relit les fichiers existants)
  inp.value="Reprends le projet « "+name+" » : lis les fichiers existants dans son dossier et continue-le. ";
  inp.style.height="40px";inp.style.height=Math.min(inp.scrollHeight,140)+"px";
  inp.focus();
  closeSidebarIfCompact();
}
function addProj(name,path){
  if(!projList.includes(name))projList=[...projList,name];
  renderProjs();
}
// ── Arborescence workspace + visionneuse de fichiers ──
function renderTreeNodes(nodes,projName){
  return nodes.map(n=>{
    const p=projName+"/"+n.name;
    if(n.type==="dir"){
      return '<div class="tree-dir" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display===\'none\'?\'block\':\'none\'">&#128193; '+esc(n.name)+'</div>'
        +'<div class="tree-node" style="display:none">'+renderTreeNodes(n.children||[],p)+'</div>';
    }
    return '<div class="tree-file" onclick="openFilePreview(\''+p.replace(/'/g,"\\'")+'\')">&#128196; '+esc(n.name)
      +'<span style="margin-left:auto;color:var(--mu);font-size:.6rem">'+(n.size?Math.round(n.size/1024)+" Ko":"")+'</span></div>';
  }).join("");
}
function toggleTree(btn,projName){
  const holder=document.querySelector('.tree-holder[data-holder="'+CSS.escape(projName)+'"]');
  if(!holder)return;
  if(holder.dataset.loaded==="1"){
    const open=holder.style.display!=="none";
    holder.style.display=open?"none":"block";
    btn.innerHTML=open?"&#9656;":"&#9662;";
    return;
  }
  fetch("/tree/"+encodeURIComponent(projName)).then(r=>r.json()).then(d=>{
    holder.innerHTML='<div class="tree-node">'+(renderTreeNodes(d.children||[],projName)||'<div style="color:var(--mu);font-size:.65rem;padding:2px 6px">(vide)</div>')+'</div>';
    holder.dataset.loaded="1";holder.style.display="block";
    btn.innerHTML="&#9662;";
  }).catch(()=>{holder.innerHTML='<div style="color:var(--rd);font-size:.65rem;padding:2px 6px">Erreur de chargement</div>';});
}
let currentPreviewPath=null;
function openFilePreview(path){
  currentPreviewPath=path;
  document.getElementById("fileModal").style.display="flex";
  document.getElementById("fileModalTitle").textContent="📄 "+path.split("/").pop();
  const body=document.getElementById("fileModalBody");
  body.innerHTML="Chargement...";
  fetch("/file?p="+encodeURIComponent(path)).then(r=>r.json()).then(d=>{
    if(d.kind==="document"){body.innerHTML='<pre style="white-space:pre-wrap">'+esc(d.content)+'</pre>';}
    else{body.innerHTML='<pre>'+highlight(d.content)+'</pre>';}
  }).catch(()=>{body.innerHTML='<span style="color:var(--rd)">Erreur de chargement</span>';});
}
function closeFileModal(){document.getElementById("fileModal").style.display="none";currentPreviewPath=null;}
function downloadCurrentFile(){if(currentPreviewPath)window.open("/dl?p="+encodeURIComponent(currentPreviewPath),"_blank");}

const KW=/\b(def|class|import|from|as|return|if|elif|else|for|while|try|except|finally|with|pass|break|continue|lambda|yield|async|await|True|False|None|self|function|const|let|var|new|export|default|extends|public|private|static|void|null|undefined|this|of|in|switch|case)\b/g;
const TOKEN_RE=/(#.*$|\/\/.*$)|("(?:[^"\\n]|\.)*"|'(?:[^'\\n]|\.)*')|\b(\d+\.?\d*)\b|\b(def|class|import|from|as|return|if|elif|else|for|while|try|except|finally|with|pass|break|continue|lambda|yield|async|await|True|False|None|self|function|const|let|var|new|export|default|extends|public|private|static|void|null|undefined|this|of|in|switch|case)\b/gm;
function highlight(raw){
  const escaped=esc(raw);
  return escaped.replace(TOKEN_RE,function(match,com,str,num,kw){
    if(com!==undefined)return '<span class="tok-com">'+com+'</span>';
    if(str!==undefined)return '<span class="tok-str">'+str+'</span>';
    if(num!==undefined)return '<span class="tok-num">'+num+'</span>';
    if(kw!==undefined)return '<span class="tok-kw">'+kw+'</span>';
    return match;
  });
}
let preCounter=0;
function fmt(t){
  return t.replace(/```[\w]*\n?([\s\S]*?)```/g,(_,c)=>{
        const id="pre"+(preCounter++);
        return '<div class="pre-wrap"><button class="copy-btn" data-target="'+id+'">&#128203; Copier</button><pre id="'+id+'">'+highlight(c.trim())+'</pre></div>';
      })
      .replace(/`([^`\n]+)`/g,(_,c)=>"<code>"+esc(c)+"</code>")
      .replace(/\*\*(.+?)\*\*/g,"<strong>$1</strong>")
      .replace(/^#{1,3}\s(.+)$/gm,"<strong>$1</strong>")
      .replace(/\n/g,"<br>");
}
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function nowStr(){const d=new Date();return String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0");}
chat.addEventListener("click",e=>{
  const b=e.target.closest(".copy-btn");
  if(!b)return;
  const pre=document.getElementById(b.dataset.target);
  if(!pre)return;
  navigator.clipboard.writeText(pre.innerText).then(()=>{
    const old=b.innerHTML;b.innerHTML="&#10003; Copie";setTimeout(()=>b.innerHTML=old,1200);
  }).catch(()=>{});
});
chat.addEventListener("scroll",()=>{
  const nearBottom=chat.scrollHeight-chat.scrollTop-chat.clientHeight<60;
  document.getElementById("scrollBtn").style.display=nearBottom?"none":"block";
});
function mk(t,c){const e=document.createElement(t);if(c)e.className=c;return e;}
function addT(){rmT();const e=mk("div");e.id="typing";e.innerHTML='<div class="dots"><span></span><span></span><span></span></div>';chat.appendChild(e);sc();}
function rmT(){const t=document.getElementById("typing");if(t)t.remove();}
function sc(){chat.scrollTop=chat.scrollHeight;}
function send(){
  const t=inp.value.trim();if(!t||!ws||ws.readyState!==1)return;
  if(pendingDoc&&pendingDoc.loading){alert("Le document est encore en cours de lecture, patiente une seconde.");return;}
  let full=t;
  let shown=esc(t);
  if(pendingDoc){
    full="Contenu du document « "+pendingDoc.name+" » :\n"+pendingDoc.text+"\n\n"+t;
    shown='<span class="attach-chip" style="margin-bottom:4px">&#128206; '+esc(pendingDoc.name)+'</span><br>'+esc(t);
    pendingDoc=null;renderAttach();
  }
  const w=mk("div","msg user");w.innerHTML='<div class="bbl">'+shown+'</div><div class="msg-time">'+nowStr()+'</div>';chat.appendChild(w);sc();
  ws.send(JSON.stringify({type:"message",text:full}));
  inp.value="";inp.style.height="40px";
  // L'input reste ACTIF: on peut continuer a ecrire/envoyer pendant qu'une tache tourne (messages mis en file)
  todos=[];renderTodos();inp.focus();
}
function chgM(m){if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:"model",model:m}));}
let generating=false;
function setGenerating(v){
  generating=v;
  document.getElementById("stopBtn").style.display=v?"inline-block":"none";
  if(!v)document.getElementById("speed-chip").style.display="none";
}
function stopGen(){if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:"stop"}));}
function regenLast(){if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:"regenerate"}));}
function exportSession(){if(currentSid)window.open("/export/"+currentSid,"_blank");else alert("Aucune session active");}
function toggleTheme(){
  document.body.classList.toggle("light");
  localStorage.setItem("devllma_theme",document.body.classList.contains("light")?"light":"dark");
}
function restoreSnap(id){
  if(!confirm("Restaurer ce snapshot ? Les fichiers actuels du projet seront ecrases."))return;
  fetch("/restore/"+id,{method:"POST"}).then(r=>r.json()).then(d=>{alert(d.msg||(d.ok?"Restaure":"Echec"));})
    .catch(()=>alert("Erreur reseau"));
}
if(localStorage.getItem("devllma_theme")==="light")document.body.classList.add("light");
function loadModels(){
  fetch("/models").then(r=>r.json()).then(d=>{
    const sel=document.getElementById("modelSel");
    if(!d.models||!d.models.length){sel.innerHTML='<option>aucun modèle</option>';return;}
    sel.innerHTML=d.models.map(m=>'<option value="'+m.name+'"'+(m.active?' selected':'')+'>'+esc(m.label)+'</option>').join("");
  }).catch(()=>{});
}
loadModels();
// ── Panneau de gestion des modeles ──
function openModelsModal(){
  document.getElementById("modelsModal").style.display="flex";
  loadModelsDetail();loadBenchResults();
}
function closeModelsModal(){document.getElementById("modelsModal").style.display="none";}
function loadModelsDetail(){
  fetch("/models_detail").then(r=>r.json()).then(d=>{
    const el=document.getElementById("models-table");
    const list=d.models||[];
    el.innerHTML=list.length?list.map(m=>
      '<div class="model-row"><span class="mn'+(m.active?" mstar":"")+'">'+(m.active?"&#9733; ":"")+esc(m.label)+'</span><span class="ms">'+m.size_gb+' Go</span></div>'
    ).join(""):'<div style="color:var(--mu);font-size:.72rem">Aucun modele installe</div>';
  }).catch(()=>{});
}
function loadBenchResults(){
  fetch("/bench_results").then(r=>r.json()).then(d=>{
    const el=document.getElementById("bench-table");
    const rows=[];
    const results=d.results||{};
    for(const model in results){
      for(const task in results[model]){
        const res=results[model][task];
        if(res.error){rows.push('<div class="bench-row"><span class="bn">'+esc(model)+' / '+esc(task)+'</span><span>erreur</span></div>');continue;}
        rows.push('<div class="bench-row"><span class="bn">'+esc(model)+' / '+esc(task)+'</span><span>'+(res.ok?"&#9989;":"&#10060;")+' '+res.decode_tok_s+' tok/s</span></div>');
      }
    }
    el.innerHTML=rows.length?rows.join(""):'<div style="color:var(--mu);font-size:.72rem">Aucun benchmark enregistre</div>';
  }).catch(()=>{});
}
// ── Panneau memoire semantique ──
const MEM_COLORS={qa:"#0ea5e9",project:"#3fb950",lesson:"#ef4444",system:"#8b5cf6",knowledge:"#f59e0b",note:"#8b949e"};
function openMemModal(){
  document.getElementById("memModal").style.display="flex";
  document.getElementById("memTestInput").value="";
  document.getElementById("memTestResult").innerHTML="";
  loadMemTable();
}
function closeMemModal(){document.getElementById("memModal").style.display="none";}
function renderMemRow(m,searchMode){
  const c=MEM_COLORS[m.kind]||"#8b949e";
  return '<div class="mem-row"><span class="mk" style="background:'+c+'22;color:'+c+'">'+esc(m.kind)+'</span>'
    +'<div class="mc"><span class="mref">'+esc(m.ref_name||m.ref||"")+(searchMode?'<span class="mem-score">score '+m.score+'</span>':'')+'</span>'+esc(m.chunk)+'</div>'
    +(searchMode?'':'<button class="mdel" title="Supprimer" onclick="deleteMemory('+m.id+')">&times;</button>')+'</div>';
}
function loadMemTable(){
  const el=document.getElementById("mem-table");
  fetch("/memories").then(r=>r.json()).then(d=>{
    const items=d.items||[];
    el.innerHTML=items.length?items.map(m=>renderMemRow(m,false)).join("")
      :'<div style="color:var(--mu);font-size:.72rem">Aucun souvenir enregistre</div>';
  }).catch(()=>{el.innerHTML='<div style="color:var(--rd);font-size:.72rem">Erreur de chargement</div>';});
}
function testMemory(){
  const q=document.getElementById("memTestInput").value.trim();
  const el=document.getElementById("memTestResult");
  if(!q){el.innerHTML="";return;}
  el.innerHTML='<div style="color:var(--mu);font-size:.72rem">Recherche...</div>';
  fetch("/memories?q="+encodeURIComponent(q)).then(r=>r.json()).then(d=>{
    const items=d.items||[];
    el.innerHTML=items.length?items.map(m=>renderMemRow(m,true)).join("")
      :'<div style="color:var(--mu);font-size:.72rem">Aucun souvenir associe (l\'agent repondrait sans rappel memoire)</div>';
  }).catch(()=>{el.innerHTML='<div style="color:var(--rd);font-size:.72rem">Erreur reseau</div>';});
}
function deleteMemory(id){
  fetch("/memories/"+id,{method:"DELETE"}).then(()=>loadMemTable()).catch(()=>{});
}
function purgeMemories(){
  if(!confirm("Supprimer TOUS les souvenirs enregistres ? Cette action est irreversible."))return;
  fetch("/memories",{method:"DELETE"}).then(()=>loadMemTable()).catch(()=>{});
}
function pullModel(){
  const inp2=document.getElementById("pullInput"),btn2=document.getElementById("pullBtn");
  const name=inp2.value.trim();
  if(!name||!ws||ws.readyState!==1)return;
  const log=document.getElementById("pull-log");
  log.style.display="block";log.textContent="Démarrage du téléchargement de "+name+"...\n";
  btn2.disabled=true;inp2.disabled=true;
  ws.send(JSON.stringify({type:"pull_model",model:name}));
}
// ── Bibliotheque de prompts (menu slash) ──
const DEFAULT_TEMPLATES=[
  {name:"resume-doc",text:"Résume ce document en 10 points clés : "},
  {name:"explique-erreur",text:"Explique cette erreur et propose la correction : "},
  {name:"cree-projet",text:"Crée une application complète qui "},
  {name:"verifie-excel",text:"Analyse ce tableau Excel et signale les anomalies : "},
  {name:"traduis",text:"Traduis ce texte en anglais professionnel : "},
  {name:"recherche",text:"Fais une recherche web et donne-moi une synthèse sourcée sur : "}
];
function getTemplates(){
  try{const t=JSON.parse(localStorage.getItem("devllma_templates"));if(Array.isArray(t)&&t.length)return t;}catch(_){}
  return DEFAULT_TEMPLATES.slice();
}
let slashSel=0;
function renderSlash(filter){
  const menu=document.getElementById("slashMenu");
  const items=getTemplates().filter(t=>!filter||t.name.toLowerCase().includes(filter)||t.text.toLowerCase().includes(filter));
  if(!items.length){menu.style.display="none";return;}
  slashSel=Math.min(slashSel,items.length-1);
  menu.innerHTML=items.map((t,i)=>'<div class="slash-item'+(i===slashSel?" selected":"")+'" data-i="'+i+'"><b>/'+esc(t.name)+'</b><span>'+esc(t.text)+'</span></div>').join("");
  menu.style.display="block";
  menu._items=items;
  menu.querySelectorAll(".slash-item").forEach(el=>{
    el.onclick=()=>applyTemplate(items[parseInt(el.dataset.i,10)]);
  });
}
function applyTemplate(t){
  if(!t)return;
  inp.value=t.text;
  document.getElementById("slashMenu").style.display="none";
  inp.focus();inp.setSelectionRange(inp.value.length,inp.value.length);
  inp.style.height="40px";inp.style.height=Math.min(inp.scrollHeight,140)+"px";
}
function saveTemplate(){
  const t=inp.value.trim();
  if(!t){alert("Écris d'abord le texte du modèle dans la zone de saisie.");return;}
  const name=window.prompt("Nom du modèle (sans espace) :");
  if(!name)return;
  const list=getTemplates();
  list.push({name:name.replace(/\s+/g,"-").toLowerCase(),text:t});
  localStorage.setItem("devllma_templates",JSON.stringify(list));
  alert("Modèle « /"+name+" » enregistré. Tape / pour le retrouver.");
}
function onInp(){
  const v=inp.value;
  if(v.startsWith("/")&&!v.includes("\n")){slashSel=0;renderSlash(v.slice(1).toLowerCase());}
  else document.getElementById("slashMenu").style.display="none";
}
function onK(e){
  const menu=document.getElementById("slashMenu");
  if(menu.style.display==="block"&&menu._items){
    if(e.key==="ArrowDown"){e.preventDefault();slashSel=Math.min(slashSel+1,menu._items.length-1);renderSlash(inp.value.slice(1).toLowerCase());return;}
    if(e.key==="ArrowUp"){e.preventDefault();slashSel=Math.max(slashSel-1,0);renderSlash(inp.value.slice(1).toLowerCase());return;}
    if(e.key==="Enter"){e.preventDefault();applyTemplate(menu._items[slashSel]);return;}
    if(e.key==="Escape"){menu.style.display="none";return;}
  }
  if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send();}
  setTimeout(()=>{inp.style.height="40px";inp.style.height=Math.min(inp.scrollHeight,140)+"px";},0);
}
// ── Usage ressources temps réel ──
function colorFor(v){return v<60?"var(--gn)":(v<85?"#f59e0b":"var(--rd)");}
function pollStats(){
  fetch("/stats",{cache:"no-store"}).then(r=>r.json()).then(d=>{
    if(d.cpu!=null){const f=document.getElementById("cpu-fill");f.style.width=d.cpu+"%";f.style.background=colorFor(d.cpu);document.getElementById("cpu-v").textContent=d.cpu+"%";}
    if(d.ram_pct!=null){const f=document.getElementById("ram-fill");f.style.width=d.ram_pct+"%";f.style.background=colorFor(d.ram_pct);document.getElementById("ram-v").textContent=d.ram_pct+"% ("+d.ram_used+"/"+d.ram_total+"G)";}
    const tv=document.getElementById("temp-v");
    if(d.temp!=null){tv.textContent=d.temp+"°C";tv.style.color=d.temp<60?"var(--gn)":(d.temp<80?"#f59e0b":"var(--rd)");}
    else{tv.textContent="N/A";}
    if(d.memories!=null){document.getElementById("mem-count").innerHTML="&#129504; "+d.memories+" souvenirs";}
  }).catch(()=>{});
}
setInterval(pollStats,2000);pollStats();
// ── Pieces jointes : images (OCR) et documents Word/Excel/PDF ──
let pendingDoc=null; // {name, text}
function renderAttach(){
  const bar=document.getElementById("attach-bar");
  if(!pendingDoc){bar.style.display="none";bar.innerHTML="";return;}
  bar.style.display="block";
  bar.innerHTML='<span class="attach-chip">&#128206; '+esc(pendingDoc.name)
    +' <span style="color:var(--mu)">('+Math.round(pendingDoc.text.length/1000)+' k car.'
    +(pendingDoc.truncated?", tronqu&#233;":"")+')</span>'
    +' <span class="ac-x" onclick="pendingDoc=null;renderAttach()" title="Retirer">&times;</span></span>';
}
function ocrFile(f){
  if(!f)return;
  const fd=new FormData();fd.append("file",f);
  const prev=inp.value;inp.value="(lecture de l'image en cours…)";
  fetch("/ocr",{method:"POST",body:fd}).then(r=>r.json()).then(d=>{
    inp.value="Analyse ce retour / cette erreur (texte lu dans l'image) :\n"+(d.text||"(rien détecté)")+"\n\n";
    inp.style.height="40px";inp.style.height=Math.min(inp.scrollHeight,140)+"px";inp.focus();
  }).catch(()=>{inp.value=prev;});
}
function docFile(f){
  const fd=new FormData();fd.append("file",f);
  pendingDoc={name:f.name,text:"",truncated:false,loading:true};
  const bar=document.getElementById("attach-bar");
  bar.style.display="block";
  bar.innerHTML='<span class="attach-chip">&#8987; lecture de '+esc(f.name)+'…</span>';
  fetch("/upload_doc",{method:"POST",body:fd}).then(r=>r.json()).then(d=>{
    if(d.error){pendingDoc=null;renderAttach();alert("Document illisible : "+d.error);return;}
    pendingDoc={name:d.name,text:d.text,truncated:d.truncated};
    renderAttach();inp.focus();
  }).catch(()=>{pendingDoc=null;renderAttach();alert("Erreur réseau pendant la lecture du document");});
}
function handleFile(f){
  if(!f)return;
  const n=(f.name||"").toLowerCase();
  if(n.endsWith(".docx")||n.endsWith(".xlsx")||n.endsWith(".pdf")){docFile(f);}
  else if(f.type&&f.type.indexOf("image")===0){ocrFile(f);}
  else alert("Format non pris en charge : joins une image ou un document .docx/.xlsx/.pdf");
}
document.addEventListener("paste",e=>{
  const items=e.clipboardData&&e.clipboardData.items;if(!items)return;
  for(const it of items){if(it.type&&it.type.indexOf("image")===0){ocrFile(it.getAsFile());e.preventDefault();return;}}
});
document.addEventListener("dragover",e=>{e.preventDefault();document.body.classList.add("dragging");});
document.addEventListener("dragleave",e=>{document.body.classList.remove("dragging");});
document.addEventListener("drop",e=>{
  document.body.classList.remove("dragging");
  const f=e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files[0];
  if(f){handleFile(f);e.preventDefault();}
});
connect();
// ── PWA : enregistrement du service worker (installabilite sur Android/iOS) ──
if("serviceWorker" in navigator){
  window.addEventListener("load",()=>{navigator.serviceWorker.register("/static/sw.js").catch(()=>{});});
}
</script>
</body>
</html>"""
