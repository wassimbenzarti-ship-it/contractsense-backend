<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Omniscient</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0a0c12; color: #e2e5f0; font-family: system-ui, sans-serif; min-height: 100vh; }
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes fadeIn { from { opacity:0; transform:translateY(10px); } to { opacity:1; transform:translateY(0); } }
.fadein { animation: fadeIn 0.35s ease; }
.wrap { max-width: 820px; margin: 0 auto; padding: 36px 20px 80px; }
.logo { display: flex; align-items: center; gap: 9px; font-weight: 800; font-size: 19px; margin-bottom: 44px; }
.dot { width: 9px; height: 9px; border-radius: 50%; background: #5b7cfa; box-shadow: 0 0 14px #5b7cfa; }
.card { background: #111420; border: 1px solid #1f2537; border-radius: 14px; padding: 20px; margin-bottom: 20px; }
label { display: block; font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
input, select, textarea { width: 100%; background: #181c2a; border: 1px solid #1f2537; border-radius: 9px; padding: 10px 13px; color: #e2e5f0; font-size: 13px; font-family: inherit; }
input:focus, select:focus, textarea:focus { outline: none; border-color: #5b7cfa; }
textarea { min-height: 160px; resize: vertical; line-height: 1.6; }
.row { display: grid; grid-template-columns: 1fr auto; gap: 8px; }
.btn-blue { padding: 10px 18px; background: #5b7cfa; border: none; border-radius: 9px; color: #fff; font-size: 13px; font-weight: 600; cursor: pointer; }
.btn-main { width: 100%; padding: 15px; background: linear-gradient(135deg, #5b7cfa, #8b5cf6); border: none; border-radius: 11px; color: #fff; font-size: 15px; font-weight: 800; cursor: pointer; margin-top: 4px; }
.btn-main:disabled { opacity: 0.4; cursor: not-allowed; }
.tabs { display: flex; gap: 3px; background: #111420; border: 1px solid #1f2537; border-radius: 10px; padding: 3px; width: fit-content; margin-bottom: 16px; }
.tab { padding: 7px 16px; border-radius: 8px; border: none; background: none; color: #6b7280; font-size: 13px; cursor: pointer; border-bottom: 2px solid transparent; }
.tab.active { background: #181c2a; color: #e2e5f0; border-bottom-color: #5b7cfa; }
.drop { border: 1.5px dashed #1f2537; border-radius: 14px; padding: 38px 24px; text-align: center; cursor: pointer; background: #111420; margin-bottom: 14px; }
.drop:hover { border-color: #5b7cfa; background: rgba(91,124,250,0.05); }
.tags { display: flex; gap: 5px; justify-content: center; margin-top: 10px; }
.tag { padding: 2px 9px; border-radius: 20px; border: 1px solid #1f2537; color: #6b7280; font-size: 11px; }
.file-ok { display: flex; align-items: center; gap: 9px; background: rgba(16,185,129,0.08); border: 1px solid rgba(16,185,129,0.3); border-radius: 9px; padding: 11px 15px; margin-bottom: 14px; font-size: 13px; }
.opts { display: grid; grid-template-columns: 1fr 1fr; gap: 13px; margin-bottom: 16px; }
select { appearance: none; }
.error { background: rgba(239,68,68,0.08); border: 1px solid rgba(239,68,68,0.25); border-radius: 9px; padding: 11px 15px; color: #ef4444; font-size: 13px; margin-top: 12px; }
.loading { display: flex; flex-direction: column; align-items: center; gap: 18px; padding: 52px 0; }
.spinner { width: 36px; height: 36px; border: 2px solid #1f2537; border-top-color: #5b7cfa; border-radius: 50%; animation: spin 0.8s linear infinite; }
.steps { display: flex; flex-direction: column; gap: 8px; }
.step { font-size: 13px; color: #6b7280; transition: color 0.4s; }
.step.done { color: #10b981; }
.key-saved { display: flex; justify-content: space-between; align-items: center; background: rgba(16,185,129,0.08); border: 1px solid rgba(16,185,129,0.3); border-radius: 9px; padding: 11px 15px; margin-bottom: 20px; font-size: 13px; color: #10b981; }
h1 { font-size: clamp(28px, 5vw, 46px); font-weight: 800; letter-spacing: -1.5px; line-height: 1.05; margin-bottom: 10px; }
.accent { background: linear-gradient(135deg, #5b7cfa, #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.desc { color: #6b7280; font-size: 14px; line-height: 1.65; max-width: 420px; margin-bottom: 28px; }
.partie-btn { text-align: left; padding: 18px 20px; background: #111420; border: 1px solid #1f2537; border-radius: 13px; color: #e2e5f0; cursor: pointer; transition: all 0.2s; width: 100%; margin-bottom: 10px; }
.partie-btn:hover { border-color: #5b7cfa; background: rgba(91,124,250,0.08); }
.partie-btn .name { font-weight: 700; font-size: 15px; margin-bottom: 4px; }
.partie-btn .desc2 { color: #6b7280; font-size: 13px; }
.mod-card { background: #111420; border: 1px solid #1f2537; border-radius: 14px; margin-bottom: 16px; overflow: hidden; }
.mod-card.accepted { border-color: #10b981; }
.mod-card.rejected { border-color: #ef4444; opacity: 0.6; }
.mod-header { display: flex; justify-content: space-between; align-items: center; padding: 16px 20px; border-bottom: 1px solid #1f2537; flex-wrap: wrap; gap: 8px; }
.mod-name { font-weight: 700; font-size: 15px; }
.risk-pill { padding: 3px 10px; border-radius: 20px; font-size: 11px; white-space: nowrap; }
.mod-body { padding: 20px; }
.mod-label { font-size: 10px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; font-weight: 600; }
.mod-original { background: rgba(239,68,68,0.06); border: 1px solid rgba(239,68,68,0.2); border-radius: 9px; padding: 14px; font-size: 13px; line-height: 1.65; margin-bottom: 12px; }
.mod-proposed { background: rgba(16,185,129,0.06); border: 1px solid rgba(16,185,129,0.2); border-radius: 9px; padding: 14px; font-size: 13px; line-height: 1.65; margin-bottom: 16px; }
.mod-reason { color: #6b7280; font-size: 12px; line-height: 1.6; margin-bottom: 16px; }
.mod-actions { display: flex; gap: 10px; }
.btn-accept { flex: 1; padding: 10px; background: rgba(16,185,129,0.15); border: 1px solid rgba(16,185,129,0.4); border-radius: 9px; color: #10b981; font-size: 13px; font-weight: 700; cursor: pointer; }
.btn-accept.active { background: #10b981; color: #fff; }
.btn-reject { flex: 1; padding: 10px; background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.3); border-radius: 9px; color: #ef4444; font-size: 13px; font-weight: 700; cursor: pointer; }
.btn-reject.active { background: #ef4444; color: #fff; }
.mod-status { text-align: center; padding: 8px; font-size: 12px; font-weight: 600; margin-top: 10px; }
.review-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 28px; padding-bottom: 18px; border-bottom: 1px solid #1f2537; flex-wrap: wrap; gap: 12px; }
.review-stats { display: flex; gap: 10px; flex-wrap: wrap; }
.stat { padding: 4px 12px; border-radius: 20px; font-weight: 600; font-size: 12px; }
.stat-pending { background: rgba(91,124,250,0.15); color: #5b7cfa; }
.stat-accepted { background: rgba(16,185,129,0.15); color: #10b981; }
.stat-rejected { background: rgba(239,68,68,0.15); color: #ef4444; }
.export-section { background: #111420; border: 1px solid #1f2537; border-radius: 14px; padding: 24px; margin-top: 28px; text-align: center; }
.btn-export { display: inline-block; padding: 13px 28px; background: linear-gradient(135deg, #5b7cfa, #8b5cf6); border: none; border-radius: 10px; color: #fff; font-size: 14px; font-weight: 700; cursor: pointer; }
.btn-new { display: block; margin: 12px auto 0; padding: 10px 22px; background: none; border: 1px solid #1f2537; border-radius: 9px; color: #6b7280; font-size: 13px; cursor: pointer; }
@media (max-width: 580px) { .opts { grid-template-columns: 1fr; } .mod-actions { flex-direction: column; } }
</style>
</head>
<body>
<div class="wrap">
  <div class="logo"><div class="dot"></div>Omniscient</div>

  <!-- STEP 2: Upload form -->
  <div id="step-form">
    <h1 class="fadein">Révisez vos contrats<br/><span class="accent">avec l'IA.</span></h1>
    <p class="desc">Uploadez votre contrat. L'IA identifie les parties, vous choisissez laquelle protéger, puis propose des modifications avec Track Changes.</p>

    <div class="tabs">
      <button class="tab active" id="tab-upload">📄 Uploader</button>
      <button class="tab" id="tab-text">✏️ Coller du texte</button>
    </div>

    <div id="upload-zone">
      <div class="drop" id="drop-area">
        <div style="font-size:30px;margin-bottom:10px">📄</div>
        <div style="font-weight:700;font-size:15px;margin-bottom:5px">Déposez votre contrat ici</div>
        <div style="color:#6b7280;font-size:13px">ou cliquez pour sélectionner</div>
        <div class="tags"><span class="tag">PDF</span><span class="tag">DOCX</span><span class="tag">TXT</span></div>
      </div>
      <input type="file" id="file-input" accept=".pdf,.docx,.txt,.doc" style="display:none"/>
      <div id="file-ok" style="display:none" class="file-ok">
        <span>✅</span>
        <span id="file-name" style="color:#10b981;font-size:13px"></span>
        <span id="file-size" style="color:#6b7280;font-size:11px;margin-left:auto"></span>
      </div>
    </div>

    <div id="text-zone" style="display:none">
      <textarea id="contract-text" placeholder="Collez le texte de votre contrat ici…" style="margin-bottom:14px"></textarea>
    </div>

    <div class="opts">
      <div>
        <label>Type</label>
        <select id="type">
          <option value="generic">Générique</option>
          <option value="nda">NDA</option>
          <option value="saas">SaaS</option>
          <option value="purchase">Achat/Vente</option>
          <option value="employment">RH</option>
          <option value="partnership">Partenariat</option>
        </select>
      </div>
    </div>

    <button class="btn-main" id="btn-analyze">⚡ Analyser le contrat</button>
    <div id="usage-counter" style="text-align:center;font-size:12px;color:#6b7280;margin-top:10px"></div>
    <div id="form-error" style="display:none" class="error"></div>
  </div>

  <!-- STEP 3: Loading -->
  <div id="step-loading" style="display:none" class="loading">
    <div class="spinner"></div>
    <div class="steps">
      <div class="step" id="s0">→ Lecture du document…</div>
      <div class="step" id="s1">→ Identification des parties…</div>
      <div class="step" id="s2">→ Analyse des risques…</div>
      <div class="step" id="s3">→ Rédaction des modifications…</div>
    </div>
  </div>

  <!-- STEP 4: Choose partie -->
  <div id="step-parties" style="display:none" class="fadein">
    <h2 style="font-size:20px;font-weight:800;margin-bottom:8px">Quelle partie représentez-vous ?</h2>
    <p style="color:#6b7280;font-size:14px;margin-bottom:24px">L'IA adaptera ses modifications pour protéger vos intérêts.</p>
    <div id="parties-list"></div>
    <button id="btn-back" style="display:block;margin:16px auto 0;padding:9px 20px;background:none;border:1px solid #1f2537;border-radius:9px;color:#6b7280;font-size:13px;cursor:pointer">↩ Retour</button>
  </div>

  <!-- STEP 5: Review modifications -->
  <div id="step-review" style="display:none" class="fadein">
    <div class="review-header">
      <div style="font-size:20px;font-weight:800">Révision du contrat</div>
      <div class="review-stats" id="review-stats"></div>
    </div>
    <div id="mods-list"></div>
    <div class="export-section">
      <div style="font-size:16px;font-weight:700;margin-bottom:8px">Générer le document final</div>
      <div id="export-desc" style="color:#6b7280;font-size:13px;margin-bottom:20px;line-height:1.6"></div>
      <button class="btn-export" id="btn-export">⬇ Télécharger avec Track Changes</button>
      <button class="btn-new" id="btn-new">↩ Analyser un autre contrat</button>
    </div>
  </div>
</div>

<script>
var BACKEND = 'https://web-production-f96f7.up.railway.app';
var apiKey = '';
var curFile = null;
var curMode = 'upload';
var modifications = [];
var decisions = {};
var savedFormData = null;
var savedLang = "fr";
var savedType = "generic";
var savedText = "";

// ── Init ──────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
  // Show usage counter
  var usage = parseInt(localStorage.getItem('cs_usage') || '0');
  var remaining = 3 - usage;
  var counter = document.getElementById('usage-counter');
  if (counter) {
    if (remaining > 0) {
      counter.textContent = remaining + ' analyse' + (remaining > 1 ? 's' : '') + ' gratuite' + (remaining > 1 ? 's' : '') + ' restante' + (remaining > 1 ? 's' : '');
    } else {
      counter.textContent = '⚠ Limite atteinte — contactez westfieldavocats.com';
      counter.style.color = '#ef4444';
    }
  }




  // Tabs
  document.getElementById('tab-upload').addEventListener('click', function() {
    curMode = 'upload';
    this.classList.add('active');
    document.getElementById('tab-text').classList.remove('active');
    show('upload-zone');
    hide('text-zone');
  });
  document.getElementById('tab-text').addEventListener('click', function() {
    curMode = 'text';
    this.classList.add('active');
    document.getElementById('tab-upload').classList.remove('active');
    hide('upload-zone');
    show('text-zone');
  });

  // Drop zone
  var drop = document.getElementById('drop-area');
  drop.addEventListener('click', function() { document.getElementById('file-input').click(); });
  drop.addEventListener('dragover', function(e) { e.preventDefault(); drop.style.borderColor = '#5b7cfa'; });
  drop.addEventListener('dragleave', function() { drop.style.borderColor = '#1f2537'; });
  drop.addEventListener('drop', function(e) {
    e.preventDefault();
    drop.style.borderColor = '#1f2537';
    setFile(e.dataTransfer.files[0]);
  });
  document.getElementById('file-input').addEventListener('change', function() {
    var f = this.files[0];
    if (f && f.name.toLowerCase().endsWith('.doc') && !f.name.toLowerCase().endsWith('.docx')) {
      showErr("form-error", "⚠️ Format .doc detecte. Convertissez en .docx pour les Track Changes complets. L'analyse reste possible mais l'export sera limite.");
    } else {
      hideErr('form-error');
    }
    setFile(f);
  });

  // Analyze
  document.getElementById('btn-analyze').addEventListener('click', analyze);

  // Back
  document.getElementById('btn-back').addEventListener('click', function() {
    hide('step-parties');
    show('step-form');
  });

  // Export + New
  document.getElementById('btn-export').addEventListener('click', exportDoc);
  document.getElementById('btn-new').addEventListener('click', reset);
});

// ── Helpers ───────────────────────────────────────────────
function show(id) { document.getElementById(id).style.display = 'block'; }
function hide(id) { document.getElementById(id).style.display = 'none'; }
function showFlex(id) { document.getElementById(id).style.display = 'flex'; }
function showErr(id, msg) { var el = document.getElementById(id); el.textContent = msg; el.style.display = 'block'; }
function hideErr(id) { document.getElementById(id).style.display = 'none'; }

function showKeyBar() {
  hide('step-key');
  show('key-saved-bar');
}

function setFile(f) {
  if (!f) return;
  curFile = f;
  document.getElementById('file-name').textContent = f.name;
  document.getElementById('file-size').textContent = (f.size/1024).toFixed(0) + ' KB';
  show('file-ok');
}

function saveKey() {
  var k = document.getElementById('api-key').value.trim();
  if (!k || k.indexOf('sk-') !== 0) {
    showErr('key-error', '⚠ Clé invalide — doit commencer par sk-');
    return;
  }
  apiKey = k;
  localStorage.setItem('cs_key', k);
  hideErr('key-error');
  showKeyBar();
  show('step-form');
}

function stepDone(i) {
  var el = document.getElementById('s' + i);
  var labels = ['Lecture du document…','Identification des parties…','Analyse des risques…','Rédaction des modifications…'];
  if (el) { el.classList.add('done'); el.textContent = '✓ ' + labels[i]; }
}

function stepReset() {
  var labels = ['Lecture du document…','Identification des parties…','Analyse des risques…','Rédaction des modifications…'];
  for (var i = 0; i < 4; i++) {
    var el = document.getElementById('s' + i);
    if (el) { el.classList.remove('done'); el.textContent = '→ ' + labels[i]; }
  }
}

function rc(r) { return r === 'high' ? '#ef4444' : r === 'medium' ? '#f59e0b' : '#10b981'; }
function rl(r) { return r === 'high' ? 'Risque élevé' : r === 'medium' ? 'Risque modéré' : 'Faible risque'; }

function buildFormData() {
  var fd = new FormData();
  fd.append('lang', 'auto');
  fd.append('type', document.getElementById('type').value);
  fd.append('api_key', apiKey);
  if (curMode === 'upload' && curFile) {
    fd.append('file', curFile);
  } else {
    var txt = document.getElementById('contract-text').value || savedText;
    fd.append('file', new Blob([txt], {type:'text/plain'}), 'contract.txt');
  }
  return fd;
}

// ── Step 1: Analyze → identify parties ───────────────────
async function analyze() {
  hideErr('form-error');

  // Check free usage limit
  var usage = parseInt(localStorage.getItem('cs_usage') || '0');
  if (usage >= 3) {
    showErr('form-error', '⚠ Vous avez utilisé vos 3 analyses gratuites. Contactez-nous sur westfieldavocats.com pour un accès illimité.');
    return;
  }

  var txt = document.getElementById('contract-text').value;
  if (curMode === 'upload' && !curFile) { showErr('form-error', '⚠ Uploade un fichier.'); return; }
  if (curMode === 'text' && txt.trim().length < 50) { showErr('form-error', '⚠ Colle au moins 50 caractères.'); return; }

  hide('step-form');
  showFlex('step-loading');
  stepReset();
  stepDone(0);
  setTimeout(function() { stepDone(1); }, 800);

  // Save params for later reuse
  savedLang = 'auto';
  savedType = document.getElementById('type').value;
  savedText = document.getElementById('contract-text').value;

  try {
    var fd2 = buildFormData();
    var resp = await fetch(BACKEND + '/identify-parties', { method: 'POST', body: fd2 });
    var data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'Erreur serveur');

    hide('step-loading');
    renderParties(data.parties || []);
  } catch(e) {
    hide('step-loading');
    show('step-form');
    showErr('form-error', '⚠ ' + (e.message || 'Erreur inattendue.'));
  }
}

// ── Step 2: Show parties ──────────────────────────────────
function renderParties(parties) {
  var list = document.getElementById('parties-list');
  list.innerHTML = '';
  parties.forEach(function(p) {
    var btn = document.createElement('button');
    btn.className = 'partie-btn';
    // Show name + generic role
    var roleLabel = p.party_label || p.description || p.role || '';
    btn.innerHTML = 
      '<div class="name">' + p.name + '</div>' +
      '<div class="desc2">' + p.description + '</div>' +
      (roleLabel ? '<div style="margin-top:4px;font-size:11px;color:#5b7cfa;font-weight:600">' + roleLabel + '</div>' : '');
    // Pass party_label (generic role) not company name
    btn.addEventListener('click', function() { analyzeWithPartie(p.party_label || p.description || p.name); });
    list.appendChild(btn);
  });
  show('step-parties');
}

// ── Step 3: Analyze with selected partie ─────────────────
async function analyzeWithPartie(partieName) {
  selectedPartie = partieName;
  hide('step-parties');
  showFlex('step-loading');
  stepDone(2);
  setTimeout(function() { stepDone(3); }, 1400);

  try {
    var fd = buildFormData();
    fd.append('partie', partieName);

    var resp = await fetch(BACKEND + '/analyze', { method: 'POST', body: fd });
    var data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'Erreur serveur');

    modifications = data.modifications || [];
    decisions = {};
    modifications.forEach(function(m) { decisions[m.id] = 'pending'; });

    hide('step-loading');
    renderReview();
  } catch(e) {
    hide('step-loading');
    show('step-form');
    showErr('form-error', '⚠ ' + (e.message || 'Erreur inattendue.'));
  }
}

// ── Step 4: Review ────────────────────────────────────────
function onProposedEdit(id, el) {
  // Save user-edited version
  var mod = modifications.find(function(m) { return m.id === id; });
  if (mod) {
    mod.proposed_user = el.innerText.trim();
    // Visual feedback
    el.style.borderColor = '#f59e0b';
    el.style.background = 'rgba(245,158,11,0.06)';
  }
}

function decide(id, decision) {
  // If user edited the proposed text, save it back to modifications
  var mod = modifications.find(function(m) { return m.id === id; });
  if (mod && decision === 'accepted') {
    var editedEl = document.getElementById('proposed-' + id);
    if (editedEl) {
      var edited = editedEl.innerText.trim();
      if (edited && edited !== mod.proposed) {
        mod.proposed_edited = edited;
        mod.proposed = edited; // Use edited version for export
      }
    }
  }
  decisions[id] = decision;
  var card = document.getElementById('card-' + id);
  var btnA = document.getElementById('ba-' + id);
  var btnR = document.getElementById('br-' + id);
  var status = document.getElementById('st-' + id);
  card.classList.remove('accepted','rejected');
  btnA.classList.remove('active');
  btnR.classList.remove('active');
  if (decision === 'accepted') {
    card.classList.add('accepted');
    btnA.classList.add('active');
    status.textContent = '✅ Modification acceptée';
    status.style.color = '#10b981';
  } else {
    card.classList.add('rejected');
    btnR.classList.add('active');
    status.textContent = '❌ Texte original conservé';
    status.style.color = '#ef4444';
  }
  updateStats();
}

function updateStats() {
  var a = Object.values(decisions).filter(function(d){return d==='accepted';}).length;
  var r = Object.values(decisions).filter(function(d){return d==='rejected';}).length;
  var p = Object.values(decisions).filter(function(d){return d==='pending';}).length;
  document.getElementById('review-stats').innerHTML =
    '<span class="stat stat-pending">' + p + ' en attente</span>' +
    '<span class="stat stat-accepted">' + a + ' acceptées</span>' +
    '<span class="stat stat-rejected">' + r + ' refusées</span>';
}

function renderReview() {
  var list = document.getElementById('mods-list');
  list.innerHTML = '';
  modifications.forEach(function(m) {
    var color = rc(m.risk);
    var div = document.createElement('div');
    div.className = 'mod-card';
    div.id = 'card-' + m.id;
    div.innerHTML =
      '<div class="mod-header">' +
        '<div class="mod-name">' + m.clause_name + '</div>' +
        '<span class="risk-pill" style="background:' + color + '20;color:' + color + '">' + rl(m.risk) + '</span>' +
      '</div>' +
      '<div class="mod-body">' +
        (m.rag_source ? '<div style="background:rgba(91,124,250,0.08);border:1px solid rgba(91,124,250,0.2);border-radius:7px;padding:5px 10px;font-size:11px;color:#5b7cfa;margin-bottom:10px;">🧠 Basé sur: ' + m.rag_source + '</div>' : '') +
        '<div class="mod-reason">💡 ' + m.reason + '</div>' +
        '<div class="mod-label" style="color:#ef4444">📄 Texte original</div>' +
        '<div class="mod-original">' + m.original + '</div>' +
        '<div class="mod-label" style="color:#10b981">✏ Modification proposée <span style="color:#6b7280;font-size:10px;font-weight:400">(éditable)</span></div>' +
        '<div class="mod-proposed" id="proposed-' + m.id + '" contenteditable="true" spellcheck="true" style="cursor:text;outline:none;" oninput="onProposedEdit(' + m.id + ', this)">' + m.proposed + '</div>' +
        '<div class="mod-actions">' +
          '<button class="btn-accept" id="ba-' + m.id + '" onclick="decide(' + m.id + ',\'accepted\')">✅ Accepter</button>' +
          '<button class="btn-reject" id="br-' + m.id + '" onclick="decide(' + m.id + ',\'rejected\')">❌ Refuser</button>' +
        '</div>' +
        '<div class="mod-status" id="st-' + m.id + '"></div>' +
      '</div>';
    list.appendChild(div);
  });

  var isDocx = curFile && (curFile.name.endsWith('.docx') || curFile.name.endsWith('.doc'));
  document.getElementById('export-desc').innerHTML = isDocx
    ? 'Le fichier DOCX original sera téléchargé avec les <strong>vraies Track Changes Word</strong> — mise en forme préservée.'
    : 'Un document Word sera généré avec les modifications acceptées en Track Changes.';

  updateStats();
  show('step-review');
}

// ── Export ────────────────────────────────────────────────
async function exportDoc() {
  var btn = document.getElementById('btn-export');
  btn.textContent = '⏳ Génération en cours…';
  btn.disabled = true;
  try {
    var fd = buildFormData();
    fd.append('modifications', JSON.stringify(modifications));
    fd.append('decisions', JSON.stringify(decisions));
    var resp = await fetch(BACKEND + '/export', { method: 'POST', body: fd });
    if (!resp.ok) { var e = await resp.json(); throw new Error(e.error); }
    var blob = await resp.blob();
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'contrat-track-changes.docx';
    a.click();
    btn.textContent = '✅ Téléchargé !';
    btn.style.background = '#10b981';
    setTimeout(function() {
      btn.textContent = '⬇ Télécharger avec Track Changes';
      btn.style.background = '';
      btn.disabled = false;
    }, 3000);

    // Silent auto-contribute full contract to queue
    contributeToQueue();
  } catch(e) {
    alert('⚠ ' + e.message);
    btn.disabled = false;
    btn.textContent = '⬇ Télécharger avec Track Changes';
  }
}

// ── Reset ─────────────────────────────────────────────────
async function contributeToQueue() {
  try {
    console.log('contributeToQueue: start', {
      curFile: curFile ? curFile.name : null,
      savedText: savedText ? savedText.length + ' chars' : null,
      selectedPartie: selectedPartie,
      modifications: modifications.length,
      decisions: Object.keys(decisions).length
    });

    var fd = new FormData();
    fd.append('lang', 'auto');
    fd.append('type', savedType || 'generic');
    fd.append('partie', selectedPartie || '');
    fd.append('contract_type', savedType || 'generic');
    fd.append('modifications', JSON.stringify(modifications));
    fd.append('decisions', JSON.stringify(decisions));

    if (curFile) {
      fd.append('file', curFile);
      console.log('contributeToQueue: using curFile', curFile.name);
    } else if (savedText && savedText.length > 50) {
      fd.append('file', new Blob([savedText], {type:'text/plain'}), 'contract.txt');
      console.log('contributeToQueue: using savedText');
    } else {
      console.log('contributeToQueue: no file available, skipping');
      return;
    }

    var resp = await fetch(BACKEND + '/rag/contribute', { method: 'POST', body: fd });
    var data = await resp.json();
    console.log('contributeToQueue: response', resp.status, data);
  } catch(e) {
    console.log('contributeToQueue ERROR:', e.message);
  }
}

function reset() {
  curFile = null;
  modifications = [];
  decisions = {};
  savedFormData = null;
  hide('step-review');
  hide('step-parties');
  hide('step-loading');
  hide('file-ok');
  document.getElementById('contract-text').value = '';
  document.getElementById('file-input').value = '';
  hideErr('form-error');
  stepReset();
  show('step-form');
  window.scrollTo(0, 0);
}
</script>
</body>
</html>
