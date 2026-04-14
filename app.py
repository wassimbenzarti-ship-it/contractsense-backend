from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import anthropic
import json
import os
import io
import re
import zipfile
import datetime
import hashlib
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import base64
import uuid
import numpy as np
import voyageai
import requests
from docx import Document
try:
    import olefile as olefile_lib
    HAS_OLEFILE = True
except ImportError:
    HAS_OLEFILE = False
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

def _anthropic_error_msg(e):
    """Return a user-friendly French message for Anthropic API errors, or None."""
    msg = str(e)
    if "usage limits" in msg or "usage_limits" in msg or "You have reached your specified API usage limits" in msg:
        return ("Limite de budget API atteinte. Veuillez augmenter votre limite mensuelle "
                "sur console.anthropic.com → Billing → Usage limits.")
    if "rate_limit" in msg or "rate limit" in msg.lower() or "529" in msg or "overloaded" in msg.lower():
        return "L'API est temporairement surchargée. Veuillez réessayer dans quelques secondes."
    if "invalid_api_key" in msg or "authentication" in msg.lower():
        return "Clé API invalide ou absente. Vérifiez la variable ANTHROPIC_API_KEY."
    return None

app = Flask(__name__)

_CORS_ORIGINS = [
    "https://ai.westfieldavocats.com",
    "https://westfieldavocats.com",
    "https://www.westfieldavocats.com",
    "https://wassimbenzarti-ship-it.github.io",
    "https://contractsense.fr",
    "https://www.contractsense.fr",
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:5173",
    "null",
]
CORS(app, origins=_CORS_ORIGINS, supports_credentials=True)

@app.after_request
def _add_cors(response):
    """Safety net: ensure CORS headers are always present on every response."""
    origin = request.headers.get("Origin", "")
    if origin in _CORS_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization, X-Requested-With, apikey"
        )
    return response

def get_legal_framework(contract_type):
    """Return mandatory legal constraints per contract type"""
    frameworks = {
        "employment": (
            "DROIT DU TRAVAIL MAROCAIN — RÈGLES IMPÉRATIVES:\n"
            "- CDD (contrat de projet/durée déterminée): max 1 an, renouvelable UNE seule fois (Art. 16 CT)\n"
            "- Renouvellement abusif = requalification automatique en CDI\n"
            "- Préavis légaux: 8 jours (<1 an), 1 mois (1-5 ans), 2 mois (>5 ans) pour ouvriers\n"
            "- Préavis cadres: 1 mois (<1 an), 2 mois (1-5 ans), 3 mois (>5 ans)\n"
            "- Indemnité de licenciement: 96h/an pour les 3 premières années, 144h/an après\n"
            "- Licenciement abusif interdit — cause réelle et sérieuse obligatoire\n"
            "- Heures supplémentaires: majoration 25% (jour), 50% (nuit/vendredi), 100% (dimanche)\n"
            "- Congé annuel: 1,5 jour/mois travaillé (min 18 jours/an)\n"
            "- Toute clause moins favorable que la loi est NULLE de plein droit"
        ),
        "nda": (
            "DROIT MAROCAIN — CONFIDENTIALITÉ:\n"
            "- Durée maximale raisonnable: 3-5 ans post-contrat\n"
            "- Clause doit définir précisément les informations confidentielles\n"
            "- Pénalités doivent être proportionnées (Art. 264 DOC)"
        ),
        "service": (
            "DROIT MAROCAIN — PRESTATION DE SERVICES:\n"
            "- Délai de paiement: max 60 jours (Art. 78 loi 15-95)\n"
            "- Pénalités de retard légales: taux directeur BAM + 3 points\n"
            "- Clauses limitatives de responsabilité admises si non abusives\n"
            "- Clause de non-concurrence: limitée dans le temps et l'espace"
        ),
        "purchase": (
            "DROIT MAROCAIN — VENTE:\n"
            "- Garantie des vices cachés: 1 an (Art. 573 DOC)\n"
            "- Transfert de propriété: à la livraison sauf clause contraire\n"
            "- Réserve de propriété possible jusqu'au paiement complet"
        ),
    }
    return frameworks.get(contract_type, "Respecte le droit marocain applicable et les principes généraux du DOC.")

# ── Party label normalization ─────────────────────────────
CONTRACT_CATEGORIES = {
    "service": "Prestation de services",
    "saas": "SaaS / Logiciel",
    "nda": "Confidentialite (NDA)",
    "employment": "Contrat de travail",
    "purchase": "Achat / Vente",
    "partnership": "Partenariat",
    "collaboration": "Convention de collaboration",
    "generic": "Generique",
}

PARTY_KEYWORDS = [
    (["prestataire", "service provider", "fournisseur", "mandate"], "favorable prestataire"),
    (["client", "customer", "mandant", "donneur"], "favorable client"),
    (["employeur", "employer"], "favorable employeur"),
    (["employe", "employee", "salarie"], "favorable employe"),
    (["divulgateur", "disclosing"], "favorable divulgateur"),
    (["destinataire", "receiving"], "favorable destinataire"),
    (["vendeur", "seller"], "favorable vendeur"),
    (["acheteur", "buyer"], "favorable acheteur"),
]

def normalize_party_label(partie, contract_type=None):
    if not partie:
        return "neutre"
    p = partie.lower().strip()
    for keywords, label in PARTY_KEYWORDS:
        if any(k in p for k in keywords):
            return label
    # Derive from contract type
    defaults = {
        "service": "favorable prestataire",
        "saas": "favorable prestataire",
        "collaboration": "favorable prestataire",
        "employment": "favorable employe",
        "nda": "favorable divulgateur",
        "purchase": "favorable vendeur",
    }
    if contract_type in defaults:
        return defaults[contract_type]
    # Clean up — remove company names, keep first word
    first_word = p.split()[0] if p.split() else p
    return "favorable " + first_word

# ── Supabase client ──────────────────────────────────────
SUPA_URL = os.environ.get("SUPABASE_URL", "")
SUPA_KEY = os.environ.get("SUPABASE_KEY", "")
SUPA_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# ── Email (SMTP) ──────────────────────────────────────────────────────────────
SMTP_HOST     = os.environ.get("SMTP_HOST", "")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM     = os.environ.get("SMTP_FROM", SMTP_USER)

def send_email(to: str, subject: str, html: str):
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        print(f"[EMAIL] SMTP non configuré — email non envoyé à {to}", flush=True)
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_FROM
        msg["To"]      = to
        msg.attach(MIMEText(html, "html", "utf-8"))
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.sendmail(SMTP_FROM, [to], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.sendmail(SMTP_FROM, [to], msg.as_string())
        print(f"[EMAIL] Envoyé à {to} — {subject}", flush=True)
    except Exception as e:
        print(f"[EMAIL] Erreur envoi à {to}: {e}", flush=True)

# ── In-memory file cache ──────────────────────────────────────────────────────
# Stores original uploaded files (bytes) keyed by UUID so /export can retrieve
# them even when the client no longer has the file. Limited to 200 entries ~100 MB.
_FILE_CACHE: dict = {}
_FILE_CACHE_ORDER: list = []
_FILE_CACHE_MAX = 200

def _cache_store(key: str, data: bytes):
    _FILE_CACHE[key] = data
    _FILE_CACHE_ORDER.append(key)
    if len(_FILE_CACHE_ORDER) > _FILE_CACHE_MAX:
        old = _FILE_CACHE_ORDER.pop(0)
        _FILE_CACHE.pop(old, None)

def _cache_get(key):
    return _FILE_CACHE.get(key)

# CMI Payment config
CMI_CLIENT_ID   = os.environ.get("CMI_CLIENT_ID", "")
CMI_STORE_KEY   = os.environ.get("CMI_STORE_KEY", "")
CMI_PAYMENT_URL = os.environ.get("CMI_PAYMENT_URL", "https://testpayment.cmi.co.ma/fim/est3Dgate")
APP_URL         = os.environ.get("APP_URL", "https://westfieldavocats.com").strip().rstrip("/")

def supa_headers():
    return {
        "apikey": SUPA_KEY,
        "Authorization": "Bearer " + SUPA_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }

def supa_get(table, params=None):
    url = SUPA_URL + "/rest/v1/" + table
    r = requests.get(url, headers=supa_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def supa_update(table, record_id, updates):
    url = SUPA_URL + f"/rest/v1/{table}?id=eq.{record_id}"
    r = requests.patch(url, headers=supa_headers(), json=updates, timeout=10)
    if not r.content or r.status_code == 204:
        return {"_status": r.status_code}
    try:
        return r.json()
    except Exception:
        return {"_status": r.status_code}

def supa_insert(table, data):
    url = SUPA_URL + "/rest/v1/" + table
    r = requests.post(url, headers=supa_headers(), json=data, timeout=30)
    if not r.ok:
        print("supa_insert ERROR " + str(r.status_code) + ": " + r.text[:500])
    r.raise_for_status()
    return r

def supa_delete(table, filters):
    url = SUPA_URL + "/rest/v1/" + table
    r = requests.delete(url, headers=supa_headers(), params=filters, timeout=30)
    r.raise_for_status()
    return r

def supa_patch(table, updates, filter_str):
    """PATCH with a raw Supabase filter string, e.g. 'email=eq.foo@bar.com'"""
    url = SUPA_URL + f"/rest/v1/{table}?{filter_str}"
    r = requests.patch(url, headers=supa_headers(), json=updates, timeout=10)
    return r

def _storage_headers():
    key = SUPA_SERVICE_KEY or SUPA_KEY
    return {
        "apikey": key,
        "Authorization": "Bearer " + key,
    }

def supa_storage_ensure_bucket(bucket_name):
    """Create the storage bucket if it doesn't exist (idempotent)."""
    url = SUPA_URL + "/storage/v1/bucket"
    r = requests.post(url, headers={**_storage_headers(), "Content-Type": "application/json"},
                      json={"id": bucket_name, "name": bucket_name, "public": False}, timeout=10)
    return r

def supa_storage_upload(bucket, path, file_bytes, content_type="application/octet-stream"):
    """Upload a file to Supabase Storage, auto-creating the bucket if missing."""
    url = SUPA_URL + f"/storage/v1/object/{bucket}/{path}"
    headers = {**_storage_headers(), "Content-Type": content_type}
    r = requests.post(url, headers=headers, data=file_bytes, timeout=60)
    # Supabase returns 400 with "Bucket not found" when bucket doesn't exist
    bucket_missing = r.status_code in (400, 404) and "ucket" in r.text
    if bucket_missing:
        supa_storage_ensure_bucket(bucket)
        r = requests.post(url, headers=headers, data=file_bytes, timeout=60)
    return r

def supa_storage_download(bucket, path):
    """Download a file from Supabase Storage. Returns bytes or None."""
    url = SUPA_URL + f"/storage/v1/object/{bucket}/{path}"
    r = requests.get(url, headers=_storage_headers(), timeout=60)
    if r.ok:
        return r.content
    print(f"supa_storage_download failed {r.status_code}: {r.text[:200]}")
    return None

def parse_dt(s):
    """Parse ISO datetime string, strip timezone info for naive comparison."""
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(s)
        return dt.replace(tzinfo=None)  # make naive
    except Exception:
        return None

# ── RAG: Supabase REST storage ────────────────────────────
def load_rag(contract_type=None, limit=200, with_embeddings=False):
    """Load RAG docs — load a sample from each category for /rag/list endpoint only"""
    try:
        select_fields = "id,title,content,source,category,party_label"
        if with_embeddings:
            select_fields += ",embedding"
        docs = supa_get("rag_documents", {
            "select": select_fields,
            "limit": str(limit)
        })
        return {"documents": docs or []}
    except Exception as e:
        print("load_rag error: " + str(e))
        return {"documents": []}

def clean_text(text):
    """Remove null bytes and invalid unicode for Supabase"""
    if not isinstance(text, str):
        return text
    return text.replace("\x00", "").replace("\u0000", "")

def save_rag_doc(doc):
    try:
        doc_copy = dict(doc)
        # Clean all string fields
        for k, v in doc_copy.items():
            if isinstance(v, str):
                doc_copy[k] = clean_text(v)
        
        # Save embedding both as JSON (legacy) and as vector (pgvector)
        emb = doc_copy.get("embedding")
        if emb and isinstance(emb, list) and len(emb) == 1024:
            doc_copy["embedding_vector"] = emb  # pgvector column
            doc_copy["embedding"] = json.dumps(emb)  # legacy JSON column
            print("save_rag_doc: embedding 1024 dims OK")
        elif emb and isinstance(emb, list):
            doc_copy.pop("embedding_vector", None)  # skip pgvector for 512 dims
            doc_copy["embedding"] = json.dumps(emb)

        supa_insert("rag_documents", doc_copy)
        print("save_rag_doc OK: " + str(doc_copy.get("title","?"))[:50])
    except Exception as e:
        print("save_rag_doc ERROR: " + str(e))
        raise

def delete_rag_by_source(source):
    try:
        import re as _re
        docs = supa_get("rag_documents", {"select": "id,title", "limit": "1000"})
        count = 0
        for d in (docs or []):
            base = _re.sub(r" \(partie \d+\)$", "", d.get("title", ""))
            if base == source:
                supa_delete("rag_documents", {"id": "eq." + d["id"]})
                count += 1
        return count
    except Exception as e:
        print("delete_rag error: " + str(e))
        return 0

def cosine_similarity(a, b):
    a, b = np.array(a, dtype=float), np.array(b, dtype=float)
    # Skip if different dimensions
    if a.shape != b.shape:
        return 0.0
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))

def search_rag_keyword(query, contract_type=None, top_k=10):
    """Keyword-based RAG fallback — used when Voyage AI embeddings are unavailable.
    Scores documents by term overlap between query and title+content."""
    data = load_rag(with_embeddings=False)
    if not data["documents"]:
        return []
    query_words = set(re.findall(r'\w{3,}', query.lower()))
    scored = []
    for doc in data["documents"]:
        text = (doc.get("title","") + " " + doc.get("content","")).lower()
        doc_words = set(re.findall(r'\w{3,}', text))
        overlap = len(query_words & doc_words)
        if overlap == 0:
            continue
        score = overlap / (len(query_words) + 1)
        # Boost by contract_type match
        cat = (doc.get("category","") + " " + doc.get("source","")).lower()
        if contract_type and contract_type.lower() in cat:
            score *= 2.0
        if "validated_clause" in doc.get("source",""):
            score *= 1.5
        scored.append((score, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in scored[:top_k]]

def get_embedding(text, voyage_key=None):
    # Try Voyage AI for semantic embeddings
    if voyage_key:
        try:
            vo = voyageai.Client(api_key=voyage_key)
            result = vo.embed([text[:1000]], model="voyage-law-2", input_type="document")
            return result.embeddings[0]
        except Exception as e:
            print("Voyage AI error: " + str(e))
            pass
    # Fallback to TF-IDF hashing
    import hashlib
    words = re.findall(r'\w+', text.lower())
    vec = [0.0] * 512
    for word in words:
        h = int(hashlib.md5(word.encode()).hexdigest(), 16) % 512
        vec[h] += 1.0
    for i in range(len(words)-1):
        bigram = words[i] + '_' + words[i+1]
        h = int(hashlib.sha256(bigram.encode()).hexdigest(), 16) % 512
        vec[h] += 0.5
    norm = sum(v*v for v in vec) ** 0.5
    if norm > 0:
        vec = [v/norm for v in vec]
    return vec

def search_rag_pgvector(query_embedding, top_k=10, doc_type=None, user_id=None):
    """Search RAG using pgvector directly in Supabase — fast semantic search"""
    try:
        url = SUPA_URL + "/rest/v1/rpc/search_rag"
        # Convert embedding list to pgvector string format
        if isinstance(query_embedding, list):
            vec_str = "[" + ",".join(str(x) for x in query_embedding) + "]"
        else:
            vec_str = str(query_embedding)
        payload = {
            "query_embedding": vec_str,
            "match_count": top_k,
            "filter_type": doc_type
        }
        # If user_id provided, search only their models
        if user_id:
            payload["filter_user_id"] = user_id
        r = requests.post(url, headers=supa_headers(), json=payload, timeout=15)
        if r.ok:
            results = r.json()
            print(f"pgvector search: {len(results)} results")
            return results or []
        else:
            print("pgvector search error " + str(r.status_code) + ": " + r.text[:300])
            return []
    except Exception as e:
        print("pgvector search exception: " + str(e))
        return []

def search_rag_hybrid(query_text, query_embedding, top_k=15, jurisdiction=None):
    """Hybrid BM25 + vector search via search_rag_hybrid SQL RPC.
    Falls back to pgvector-only if the RPC isn't available."""
    try:
        url = SUPA_URL + "/rest/v1/rpc/search_rag_hybrid"
        vec_str = "[" + ",".join(str(x) for x in query_embedding) + "]" if isinstance(query_embedding, list) else str(query_embedding)
        payload = {"query_text": query_text[:500], "query_embedding": vec_str, "match_count": top_k}
        if jurisdiction and jurisdiction not in ("auto", "universel"):
            payload["p_jurisdiction"] = jurisdiction
        key = SUPA_SERVICE_KEY or SUPA_KEY
        headers = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        if r.ok:
            results = r.json() or []
            print(f"Hybrid BM25+vec: {len(results)} results")
            return results
        else:
            print(f"Hybrid search error {r.status_code} — fallback to pgvector")
    except Exception as e:
        print(f"Hybrid search exception: {e}")
    return search_rag_pgvector(query_embedding, top_k=top_k)

def extract_article_refs(content, title=""):
    """Extract article references (e.g. 'Art. 16 CT', 'Article 264 DOC') from RAG doc content."""
    refs = re.findall(r'\bArt(?:icle)?\.?\s*\d+[\w\-]*(?:\s+(?:CT|DOC|CC|CO|CSC|CPCM|CPC))?\b', content or "", re.IGNORECASE)
    return list(dict.fromkeys(refs))[:5]  # deduplicate, max 5

def search_rag(query, api_key, voyage_key=None, top_k=5, partie=None):
    data = load_rag()
    if not data["documents"]:
        return []
    query_vec = get_embedding(query, voyage_key)
    scored = []
    for doc in data["documents"]:
        emb = doc.get("embedding")
        if emb is None:
            continue
        # Parse embedding from JSON string if needed
        if isinstance(emb, str):
            try:
                emb = json.loads(emb)
            except:
                continue
        score = cosine_similarity(query_vec, emb)
        # Boost for matching party
        doc_label = doc.get("party_label") or ""
        if partie and doc_label and partie.lower() in doc_label.lower():
            score *= 1.3
        # Boost validated clauses
        if "validated_clause" in doc.get("source", ""):
            score *= 1.2
        scored.append((score, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in scored[:top_k]]

# ── Text extraction ───────────────────────────────────────
def extract_text_from_doc_ole(file_bytes):
    """Extract text from old .doc format using olefile"""
    if not HAS_OLEFILE:
        return None
    try:
        ole = olefile_lib.OleFileIO(io.BytesIO(file_bytes))
        if not ole.exists('WordDocument'):
            return None
        stream = ole.openstream('WordDocument').read()
        text = stream.decode('latin-1', errors='ignore')
        clean = re.sub(r'[^\x20-\x7E\x80-\xFF\n\r\t]', ' ', text)
        clean = re.sub(r' {3,}', ' ', clean)
        clean = re.sub(r'\n{3,}', '\n\n', clean)
        # Skip binary header — find first readable content
        for marker in ['CONTRAT', 'Contrat', 'CONTRACT', 'ACCORD', 'CONVENTION']:
            idx = clean.find(marker)
            if idx != -1 and idx < len(clean) // 2:
                return clean[idx:]
        # Fallback: skip first third
        return clean[len(clean)//4:]
    except Exception as e:
        print("OLE extract error: " + str(e))
        return None

def extract_text_from_docx(file_bytes):
    try:
        doc = Document(io.BytesIO(file_bytes))
        text = []
        for para in doc.paragraphs:
            if para.text.strip():
                text.append(para.text)
        return "\n".join(text)
    except Exception:
        # Try OLE for old .doc format
        ole_text = extract_text_from_doc_ole(file_bytes)
        if ole_text:
            return ole_text
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                if 'word/document.xml' in z.namelist():
                    doc_xml = z.read('word/document.xml').decode('utf-8', errors='ignore')
                    text = re.sub(r'<[^>]+>', ' ', doc_xml)
                    return re.sub(r'\s+', ' ', text).strip()
        except Exception as e2:
            raise ValueError("Impossible de lire le fichier Word: " + str(e2))

def read_file(file):
    file_bytes = file.read()
    filename = file.filename.lower()
    if filename.endswith(".docx") or filename.endswith(".doc"):
        text = extract_text_from_docx(file_bytes)
    else:
        text = file_bytes.decode("utf-8", errors="ignore")
    # Remove null bytes
    text = text.replace("\x00", "").replace("\u0000", "") if text else text
    return text, file_bytes, filename

# ── AI functions ──────────────────────────────────────────
def identify_parties(contract_text, lang, api_key):
    client = anthropic.Anthropic(api_key=api_key)
    system = f"""Tu es un juriste expert. Identifie les parties dans ce contrat.
Réponds UNIQUEMENT en {'anglais' if lang == 'en' else 'français'} avec ce JSON exact, sans markdown:
{{"parties":[{{"id":"partie_1","name":"Nom exact de la partie 1","description":"Role de cette partie"}},{{"id":"partie_2","name":"Nom exact de la partie 2","description":"Role de cette partie"}}]}}
- Utilise les vrais noms tels qu'ils apparaissent dans le contrat
- Maximum 3 parties, description max 10 mots"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": f"Contrat:\n\n{contract_text[:20000]}\n\nIdentifie les parties."}]
    )
    raw = message.content[0].text
    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        raise ValueError("Réponse invalide")
    return json.loads(match.group(0))

def build_numbered_paragraphs(file_bytes, filename):
    """Build a numbered paragraph index from DOCX for precise matching"""
    try:
        if filename.endswith('.docx') or filename.endswith('.doc'):
            doc = Document(io.BytesIO(file_bytes))
            paragraphs = []
            for i, para in enumerate(doc.paragraphs):
                text = para.text.strip()
                if text:
                    paragraphs.append({"idx": i, "text": text})
            return paragraphs
    except:
        pass
    return []

def analyze_contract(contract_text, lang, contract_type, api_key, partie="la partie bénéficiaire", file_bytes=None, filename="", progress_cb=None):
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("Clé API manquante")
    client = anthropic.Anthropic(api_key=api_key)

    # Build numbered paragraphs for precise matching
    paragraphs = build_numbered_paragraphs(file_bytes, filename) if file_bytes else []
    
    # Build numbered contract text for AI
    if paragraphs:
        numbered_text = "\n".join(("[P" + str(p["idx"]) + "] " + p["text"]) for p in paragraphs[:150])
    else:
        numbered_text = contract_text[:20000]

    # ── Structured RAG: separate model docs (protection) from legal docs (conformite) ──
    if progress_cb: progress_cb("\U0001f4da Consultation de la base légale...")
    model_context = ""
    legal_context = ""
    _rag_contract_count = 0
    _rag_legal_count = 0
    LEGAL_CATS = {"loi", "law", "doctrine", "jurisprudence", "legal", "legislation"}
    # Quick jurisdiction detection for boosting relevant docs
    def _detect_jur(text):
        s = text[:3000].lower()
        if any(k in s for k in ["code du travail marocain", "dahir", "droit marocain", "maroc"]):
            return "droit_marocain"
        if any(k in s for k in ["droit français", "loi française", "france", "code civil français"]):
            return "droit_francais"
        return "universel"
    _jurisdiction = _detect_jur(contract_text)
    try:
        voyage_key = os.environ.get("VOYAGE_API_KEY", "")
        search_query = contract_type + " " + partie + " " + contract_text[:500]
        query_vec = get_embedding(search_query, voyage_key)
        is_voyage = bool(voyage_key) and len(query_vec) == 1024
        all_docs = []

        # 1. Primary: hybrid BM25 + vector (best quality)
        if is_voyage:
            all_docs = search_rag_hybrid(search_query[:300], query_vec, top_k=25, jurisdiction=_jurisdiction)
            if not all_docs:
                all_docs = search_rag_pgvector(query_vec, top_k=25)
            print(f"Primary RAG: {len(all_docs)} docs (hybrid={bool(all_docs)})")

        # 2. Fallback: fetch all docs with embeddings + cosine similarity
        if not all_docs:
            print("RAG fallback: fetching docs with embeddings from Supabase")
            try:
                key = SUPA_SERVICE_KEY or SUPA_KEY
                raw_r = requests.get(
                    SUPA_URL + "/rest/v1/rag_documents",
                    headers={"apikey": key, "Authorization": "Bearer " + key},
                    params={"select": "id,title,content,source,category,party_label,jurisdiction,embedding,embedding_vector", "limit": "500"},
                    timeout=30
                )
                if raw_r.ok:
                    raw_docs = raw_r.json() or []
                    scored = []
                    for doc in raw_docs:
                        emb = None
                        raw_emb = doc.get("embedding")
                        if isinstance(raw_emb, str) and raw_emb.strip():
                            try: emb = json.loads(raw_emb)
                            except: pass
                        elif isinstance(raw_emb, list):
                            emb = raw_emb
                        if not emb:
                            raw_vec = doc.get("embedding_vector")
                            if isinstance(raw_vec, str) and raw_vec.strip().startswith("["):
                                try: emb = json.loads(raw_vec)
                                except: pass
                            elif isinstance(raw_vec, list):
                                emb = raw_vec
                        if emb and isinstance(emb, list) and len(emb) == len(query_vec):
                            scored.append((cosine_similarity(query_vec, emb), doc))
                    scored.sort(key=lambda x: x[0], reverse=True)
                    all_docs = [d for _, d in scored[:20]]
                    print(f"Fallback RAG: {len(all_docs)} docs from {len(raw_docs)} total")
            except Exception as fe:
                print("Fallback RAG error: " + str(fe))

        # 3. Last resort: keyword search
        if not all_docs:
            all_docs = search_rag_keyword(search_query, contract_type=contract_type, top_k=10)
            print(f"Keyword RAG fallback: {len(all_docs)} docs")

        # Jurisdiction boost
        all_docs.sort(key=lambda d: 0 if (d.get("jurisdiction") or "universel") in (_jurisdiction, "universel", "auto") else 1)

        # Separate contract models from legal references
        contract_docs = [d for d in all_docs if d.get("category", "").lower() not in LEGAL_CATS]
        legal_docs    = [d for d in all_docs if d.get("category", "").lower() in LEGAL_CATS]

        # Filter employment law docs for non-employment contracts
        _ct_lower = (contract_type or "").lower()
        _is_employment = _ct_lower in ("employment", "cdi", "cdd") or any(k in _ct_lower for k in ["travail", "emploi"])
        if not _is_employment:
            _emp_kw = ["code du travail", "loi 65-99", "licenciement", "preavis", "heures supplementaires", "conge annuel"]
            def _is_emp_doc(doc):
                txt = ((doc.get("title") or "") + " " + (doc.get("source") or "") + " " + (doc.get("content") or "")[:500]).lower()
                return any(k in txt for k in _emp_kw)
            _n_before = len(legal_docs)
            legal_docs = [d for d in legal_docs if not _is_emp_doc(d)]
            if len(legal_docs) < _n_before:
                print(f"Employment filter: removed {_n_before - len(legal_docs)} labor law docs")

        protected_kw = ["lexisnexis", "dalloz", "lamy", "mernissi", "traite-de-droit", "pdf-free", "lexis"]

        # Context 1: contract models → client protection
        if contract_docs:
            validated = [d for d in contract_docs if "validated_clause" in d.get("source", "")]
            reference = [d for d in contract_docs if "validated_clause" not in d.get("source", "")]
            model_context = "\n\n=== MODÈLES DE CONTRATS ET CLAUSES PROTECTRICES ===\n"
            for doc in (validated + reference)[:12]:
                title_doc = doc.get("title", "") or doc.get("source", "modele")
                content_doc = str(doc.get("content", ""))[:1400]
                is_prot = any(p in (title_doc + doc.get("source", "")).lower() for p in protected_kw)
                arts = extract_article_refs(content_doc, title_doc)
                model_context += "\n=== " + title_doc + " ===\n"
                if arts:
                    model_context += "→ Articles cités: " + ", ".join(arts) + "\n"
                model_context += content_doc + "\n"
                model_context += "→ rag_source: " + ("null (protege)" if is_prot else title_doc) + "\n"
                if doc.get("party_label"):
                    model_context += "[PARTIE PROTEGEE PAR CE MODELE: " + str(doc.get("party_label", "")) + "]\n"

        # Context 2: legal references → conformite
        if legal_docs:
            legal_context = "\n\n=== RÉFÉRENCES JURIDIQUES (LOIS / DOCTRINE / JURISPRUDENCE) ===\n"
            for doc in legal_docs[:12]:
                cat = doc.get("category", "reference").upper()
                title_doc = doc.get("title", "") or doc.get("source", "reference")
                content_doc = str(doc.get("content", ""))[:1400]
                arts = extract_article_refs(content_doc, title_doc)
                legal_context += "\n[" + cat + "] " + title_doc + "\n"
                if arts:
                    legal_context += "→ Articles disponibles: " + ", ".join(arts) + "\n"
                legal_context += content_doc + "\n"
                legal_context += "→ rag_source: " + title_doc + "\n"

        _rag_contract_count = len(contract_docs)
        _rag_legal_count = len(legal_docs)
        print(f"RAG final: {_rag_contract_count} contract docs, {_rag_legal_count} legal docs | model={len(model_context)}c legal={len(legal_context)}c")
    except Exception as e:
        print("RAG search error: " + str(e))
        import traceback; traceback.print_exc()
    rag_context = model_context  # used in prompt below

    # Detect contract language
    english_words = len([w for w in contract_text[:2000].lower().split() if w in ['the','and','of','to','in','for','is','this','agreement','shall','party','parties','contract','hereby','whereas','including','provided','subject','pursuant','accordance','obligation','represent','warrant','indemnify','liability','termination','governing','arbitration','confidential']])
    french_words = len([w for w in contract_text[:2000].lower().split() if w in ['le','la','les','de','du','des','en','et','est','que','qui','une','par','pour','sur','dans','avec','aux','au','contrat','société','article','présent','parties','prestataire','client','mandant','mandataire','clause','accord','convention','résiliation','responsabilité','confidentialité']])
    arabic_words = len([w for w in contract_text[:2000].split() if any(0x0600 <= ord(c) <= 0x06FF for c in w)])
    
    if arabic_words > 10:
        detected_lang = "AR (Arabic)"
    elif english_words > french_words:
        detected_lang = "EN (English)"
    else:
        detected_lang = "FR (French)"
    
    print(f"Detected language: {detected_lang} (en={english_words}, fr={french_words}, ar={arabic_words})")

    # Define what "favorable" means for each role
    role_objectives = {
        "employeur": "maximiser la flexibilité opérationnelle, minimiser les obligations et coûts, renforcer le pouvoir de direction et de contrôle, faciliter la résiliation, protéger les intérêts commerciaux",
        "employe": "garantir la stabilité de l'emploi, maximiser les protections et indemnités, limiter les obligations post-contrat, encadrer les heures et conditions de travail",
        "prestataire": "garantir le paiement, limiter la responsabilité, protéger la propriété intellectuelle, encadrer les modifications de scope",
        "client": "garantir la qualité et les délais, maximiser les pénalités, faciliter la résiliation, protéger les données",
        "acheteur": "garantir la conformité, maximiser les garanties, faciliter les recours",
        "vendeur": "garantir le paiement, limiter les garanties et responsabilités",
    }
    # Extract role from partie label
    role_key = "employeur"
    for key in role_objectives:
        if key in partie.lower():
            role_key = key
            break
    role_obj = role_objectives.get(role_key, "protéger ses intérêts")

    # Coalition detection: "A et B" means defend A and B together against third party
    _coalition_parties = [p.strip() for p in partie.split(" et ")] if " et " in partie else []
    _is_coalition = len(_coalition_parties) >= 2

    system = (
        "Tu es un avocat d'affaires senior avec 20 ans d'expérience en droit des contrats. Ta responsabilité professionnelle est engagée.\n"
        "MISSION CRITIQUE: Analyser EXHAUSTIVEMENT ce contrat. Tu n'as pas le droit à l'erreur — chaque clause désavantageuse non identifiée est une faute professionnelle.\n"
        "OBLIGATION D'EXHAUSTIVITÉ: Tu DOIS analyser CHAQUE clause du contrat, une par une. Ne saute AUCUN paragraphe.\n"
        "FAVORISER: " + partie + "\n\n"
        "LANGUE DU CONTRAT: " + detected_lang + "\n"
        "RÈGLE ABSOLUE: Tu DOIS répondre dans LA MÊME LANGUE QUE LE CONTRAT.\n"
        "- Contrat en ANGLAIS → tous les champs (reason, proposed, clause_name) en ANGLAIS UNIQUEMENT\n"
        "- Contrat en FRANÇAIS → tous les champs en FRANÇAIS UNIQUEMENT\n"
        "- Contrat en ARABE → tous les champs en ARABE UNIQUEMENT\n"
        "FAUTE PROFESSIONNELLE: répondre en français pour un contrat anglais est une erreur grave.\n"
        "INTERDICTION ABSOLUE de mélanger les langues ou répondre dans une autre langue.\n"
        "TYPE DE CONTRAT: " + contract_type + "\n"
        "PARTIE À PROTÉGER: " + partie + "\n"
        "OBJECTIFS CONCRETS pour " + partie + ": " + role_obj + "\n\n"
        + (_is_coalition and ("\nCOALITION " + partie + " — REGLES STRICTES:\n""Tu representes EXCLUSIVEMENT les interets de : " + " et ".join(_coalition_parties) + ".\n""Les AUTRES parties au contrat sont tes adversaires dans cette negociation.\n""OBLIGATIONS ABSOLUES pour chaque modification proposee :\n""  1. REDUIRE ou SUPPRIMER les obligations imposees sur " + " et ".join(_coalition_parties) + "\n""  2. AJOUTER des droits, protections ou recours POUR " + " et ".join(_coalition_parties) + "\n""  3. IMPOSER des obligations et contraintes sur les AUTRES parties (adversaires)\n""INTERDICTIONS ABSOLUES — toute violation est une FAUTE GRAVE :\n""  X NE JAMAIS ajouter de nouvelles obligations sur " + " ou ".join(_coalition_parties) + "\n""  X NE JAMAIS renforcer ou etendre les obligations existantes sur " + " ou ".join(_coalition_parties) + "\n""  X NE JAMAIS proposer de mecanismes de controle ou rapports QUI PESENT sur " + " ou ".join(_coalition_parties) + "\n""EXEMPLE CORRECT: obligation de 44 emplois → proposer une clause de force majeure liberant la Societe + contrepartie financiere de l'Etat\n""EXEMPLE INTERDIT: obligation de 44 emplois → ajouter un rapport semestriel (nouvelle charge sur la coalition)\n\n") or "")
        + "RÈGLES D'ANALYSE PROFESSIONNELLE:\n"
        "CLAUSES HAUTE PRIORITE - ANALYSE ABSOLUMENT OBLIGATOIRE:\n"
        "  RESILIATION/TERMINATION : Cherche les articles SPECIFIQUEMENT intitules 'Resiliation', 'Termination', 'Fin du contrat', 'Rupture', 'Resolution', 'Early termination' ou equivalent."
        " ATTENTION : la clause de DUREE ou DUREE DU CONTRAT n'est PAS une clause de resiliation — ne les confonds pas, meme si la clause de duree mentionne l'expiration."
        " Analyse OBLIGATOIREMENT : (1) qui peut resilier et dans quels cas (manquement, faillite, force majeure, convenance), (2) le preavis requis, (3) les consequences financieres (penalites, indemnites de resiliation, remboursements exiges),"
        " (4) si " + partie + " peut etre force de rembourser des sommes importantes."
        " Si aucun article de resiliation n'existe = RISQUE CRITIQUE, propose une nouvelle clause. Obligation de remboursement > 0 = RISQUE CRITIQUE (risk=high).\n"
        "  RESPONSABILITE/LIABILITY : Verifier systematiquement (1) plafonds de responsabilite, (2) exclusions de garantie, (3) indemnisation asymetrique. Si " + partie + " supporte une responsabilite illimitee ou superieure a celle de la partie adverse = RISQUE CRITIQUE.\n"
        "  REMBOURSEMENT/RESTITUTION : Verifier toute obligation de rembourser avances, subventions ou benefices fiscaux. Quantifier le montant max que " + partie + " pourrait devoir rembourser en cas de manquement ou resiliation.\n\n"
        "1. EXHAUSTIVITÉ TOTALE: Identifie TOUTES les clauses désavantageuses pour " + partie + " — même les clauses en apparence neutres\n"
        "2. CLAUSES À RISQUE: Cherche spécifiquement: limitation de responsabilité, résiliation unilatérale, pénalités asymétriques, clauses d'exclusivité abusives, délais de paiement défavorables, cessions de droits excessives, clauses de non-concurrence, force majeure restrictive, juridiction défavorable\n"
        "3. CLAUSES MANQUANTES OBLIGATOIRES: Tu DOIS proposer ENTRE 4 ET 5 nouvelles clauses (type=nouvelle_clause) — CECI EST OBLIGATOIRE SANS EXCEPTION (type=nouvelle_clause) pour les protections absentes du contrat. Cherche systématiquement: limitation de responsabilité, pénalités/clause pénale, confidentialité, force majeure, révision de prix, juridiction compétente, non-sollicitation, garantie, assurance, cession du contrat. Pour chaque clause manquante: (1) rédige-la complète dans proposed dans la même langue que le contrat, (2) numérote-la en suivant la numérotation existante, (3) indique insertion_after=para_idx du dernier article existant avant l'endroit logique d'insertion, (4) original=null.\n"
        "4. NIVEAU RÉDACTIONNEL: Style avocat d'affaires senior — précis, technique, sans ambiguïté\n"
        "5. RAG OBLIGATOIRE: Cite UNIQUEMENT les sources marquées === SOURCE dans le contexte. NE JAMAIS inventer. NE JAMAIS citer LexisNexis/ouvrages payants. Si source protégée ou absente du contexte → rag_source: null.\n"
        "6. LÉGALITÉ: Toutes les modifications doivent respecter le droit applicable — jamais de clauses illégales\n\n"
        "PROCESSUS D'ANALYSE:\n"
        "Étape 1: Lis tout le contrat\n"
        "Étape 2: Pour chaque paragraphe, demande-toi: Cette clause est-elle favorable, neutre ou défavorable à " + partie + " ?\n"
        "Étape 3: Pour chaque clause défavorable ou neutre améliorable → propose une modification\n"
        "Étape 4: Vérifie les protections manquantes → propose des clauses additionnelles\n"
        "Étape 5: Vérifie chaque modification contre le RAG pour citer les sources\n\n"
        + get_legal_framework(contract_type) +
        "\n\n"
        + rag_context +
        + legal_context +
        "\n\nATTENTION sur les clauses validées du RAG:\n"
        "- Utilise-les UNIQUEMENT si elles sont favorables à " + partie + "\n"
        "- Si une clause validée favorise l'autre partie, IGNORE-LA\n"
        "- Vérifie toujours que ta proposition avantage bien " + partie + "\n\n"
        "IMPORTANT: Le contrat est numéroté [P0], [P1], etc.\n\n"
        "Retourne UNIQUEMENT du JSON valide, sans markdown:\n"
        '{"modifications":[{"id":1,"para_idx":32,"clause_name":"nom court",'
        '"risk":"high|medium|low",'
        '"reason":"Pourquoi cette clause désavantage ' + partie + ' et comment la modification la protège",'
        '"type":"modification|nouvelle_clause",'
        '"original":"texte EXACT du paragraphe ou null pour nouvelle_clause",'
        '"proposed":"clause reformulée favorisant ' + partie + '",'
        '"type":"modification|nouvelle_clause",'
        '"insertion_after":"para_idx après lequel insérer ou null si modification",'
        '"rag_source":"titre EXACT de la source RAG du contexte, ou null si absente/protégée"}]}\n\n'
        "Règles:\n"
        "- MINIMUM 8 modifications obligatoires — un juriste qui en trouve moins de 8 n'a pas analysé exhaustivement\n"
        "- para_idx: numéro entier du paragraphe\n"
        "- original: copie EXACTE sans modification\n"
        "- proposed: clause juridique complète et professionnelle, rédigée en style contractuel soutenu\n"
        "- proposed: utilise le vocabulaire juridique approprié (nonobstant, en ce compris, à titre de, ci-après, sous réserve de...)\n"
        "- proposed: structure avec sujet + verbe + objet + conditions + exceptions si nécessaire\n"
        "- proposed: max 120 mots, mais suffisamment détaillé pour être opérationnel sans ambiguïté\n"
        "- proposed: jamais de blancs ou placeholders comme ___ ou [à compléter]\n"
        "- proposed: rédige comme un avocat d'affaires senior rédigeant pour un client exigeant\n"
        "- Vérifie chaque proposed: est-ce que ça avantage bien " + partie + " ? Si non, reformule."
    )

    # Limit text to avoid timeout
    truncated_text = numbered_text[:15000]
    if progress_cb: progress_cb("\U0001f916 Démarrage de l\'analyse IA...")
    raw = ""
    _clause_buf = ""
    _law_buf = ""
    import re as _re
    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": "Contrat:\n\n" + truncated_text + "\n\nRetourne le JSON."}]
    ) as _stream:
        for _tok in _stream.text_stream:
            raw += _tok
            if progress_cb:
                _clause_buf += _tok
                _law_buf += _tok
                # Detect clause name being analyzed
                _cm = _re.search(r'"clause_name"\s*:\s*"([^"]{4,70})"', _clause_buf)
                if _cm:
                    progress_cb("\U0001f50d Analyse : " + _cm.group(1))
                    _clause_buf = _clause_buf[_cm.end():]
                # Detect legal references
                _lm = _re.search(r'((?:Loi|loi)\s+n[\xb0\u00b0][\s\d-]+|(?:Article|art\.?)\s+\d+\s+(?:du\s+)?(?:DOC|Code|CCJA|Dahir)|Code\s+(?:du travail|des obligations))', _law_buf)
                if _lm:
                    progress_cb("\u2696\ufe0f " + _lm.group(1).strip())
                    _law_buf = ""
                if len(_clause_buf) > 2000: _clause_buf = _clause_buf[-500:]
                if len(_law_buf) > 2000: _law_buf = _law_buf[-500:]
    if progress_cb: progress_cb("\u2705 Traitement des résultats...")
    print("RAW FULL:", raw[:3000])

    # Strip markdown code blocks
    raw = re.sub(r'```(?:json)?\s*', '', raw)
    raw = raw.replace('```', '')

    # Extract modifications array directly - more robust than full JSON parsing
    # Find all modification objects
    mod_pattern = re.compile(
        r'\{\s*"id"\s*:\s*(\d+)[\s\S]*?"proposed"\s*:\s*"((?:[^"\\]|\\.)*)"',
        re.DOTALL
    )

    # First try standard JSON parsing
    match = re.search(r'\{[\s\S]*"modifications"[\s\S]*\}', raw)
    if match:
        json_str = match.group(0)
        # Fix double opening braces
        json_str = re.sub(r'\{\s*\{', '{', json_str)
        # Remove control characters
        json_str = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', ' ', json_str)
        # Remove trailing commas
        json_str = re.sub(r',\s*}', '}', json_str)
        json_str = re.sub(r',\s*]', ']', json_str)
        # Fix missing commas between keys (common Claude mistake)
        json_str = re.sub(r'("|}|\d|true|false|null)\s*\n\s*"', r'\1,\n"', json_str)
        try:
            result = json.loads(json_str)
        except:
            result = None
    else:
        result = None

    # Fallback: extract individual modification objects using brace tracking
    if not result or not result.get("modifications"):
        mods = []
        # Track braces to find complete objects
        depth = 0
        start = -1
        for i, c in enumerate(raw):
            if c == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    obj_str = raw[start:i+1]
                    if '"id"' in obj_str and '"proposed"' in obj_str:
                        try:
                            clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', ' ', obj_str)
                            clean = re.sub(r',\s*}', '}', clean)
                            clean = re.sub(r',\s*]', ']', clean)
                            obj = json.loads(clean)
                            if obj.get("proposed"):
                                mods.append(obj)
                        except:
                            pass
                    start = -1

        if not mods:
            # Last resort: regex extraction
            ids = re.findall(r'"id"\s*:\s*(\d+)', raw)
            names = re.findall(r'"clause_name"\s*:\s*"([^"]+)"', raw)
            risks = re.findall(r'"risk"\s*:\s*"([^"]+)"', raw)
            originals = re.findall(r'"original"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
            proposeds = re.findall(r'"proposed"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
            reasons = re.findall(r'"reason"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
            rag_sources = re.findall(r'"rag_source"\s*:\s*(?:"((?:[^"\\\\]|\\\\.)*?)"|null)', raw)
            types = re.findall(r'"type"\s*:\s*"([^"]+)"', raw)
            insertions = re.findall(r'"insertion_after"\s*:\s*(\d+|null)', raw)
            for i in range(min(len(ids), len(proposeds))):
                mods.append({
                    "id": int(ids[i]) if i < len(ids) else i+1,
                    "clause_name": names[i] if i < len(names) else "Clause",
                    "risk": risks[i] if i < len(risks) else "medium",
                    "type": types[i] if i < len(types) else "modification",
                    "reason": reasons[i] if i < len(reasons) else "",
                    "original": originals[i] if i < len(originals) else "",
                    "proposed": proposeds[i] if i < len(proposeds) else "",
                    "insertion_after": int(insertions[i]) if i < len(insertions) and insertions[i] != 'null' else None,
                    "rag_source": rag_sources[i] if i < len(rag_sources) and rag_sources[i] else None
                })

        if mods:
            result = {"modifications": mods}
        else:
            raise ValueError("Impossible d'extraire les modifications")

    # Add confidence score based on RAG usage
    mods = result.get("modifications", [])
    rag_backed = sum(1 for m in mods if m.get("rag_source"))
    result["_rag_coverage"] = str(rag_backed) + "/" + str(len(mods)) + " modifications basées sur le RAG"
    result["_paragraphs"] = paragraphs
    return result

def fuzzy_match(original, para_text, threshold=0.60):
    """Check if original text roughly matches para_text"""
    original_lower = original.lower().strip()
    para_lower = para_text.lower().strip()
    # Exact match
    if original_lower in para_lower:
        return True
    # Extract meaningful words (ignore short words)
    orig_words = [w for w in re.findall(r"[a-zA-ZÀ-ÿ]{3,}", original_lower)]
    para_words_set = set(re.findall(r"[a-zA-ZÀ-ÿ]{3,}", para_lower))
    orig_words_set = set(orig_words)
    if len(orig_words_set) < 4:
        return False
    overlap = len(orig_words_set & para_words_set) / len(orig_words_set)
    return overlap >= threshold

def create_docx_with_changes(contract_text, modifications, decisions):
    """Fallback DOCX: rapport professionnel avec texte original barre et proposition en vert."""
    from docx import Document as DocxDocument
    from docx.shared import RGBColor, Pt, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn as _qn
    from docx.oxml import OxmlElement as _OE

    doc = DocxDocument()
    for section in doc.sections:
        section.top_margin    = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin   = Cm(2.5)
        section.right_margin  = Cm(2.5)

    title = doc.add_heading("Rapport de modifications — ContractSense", 0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    dp = doc.add_paragraph()
    dp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    dr = dp.add_run("Genere le " + datetime.datetime.now().strftime("%d/%m/%Y a %H:%M"))
    dr.font.size = Pt(9)
    dr.font.color.rgb = RGBColor(0x70, 0x70, 0x70)

    accepted = [m for m in modifications if decisions.get(str(m["id"])) == "accepted"]
    if not accepted:
        doc.add_paragraph("Aucune modification acceptee.")
        out = io.BytesIO(); doc.save(out); out.seek(0); return out

    doc.add_paragraph()
    sp = doc.add_paragraph()
    sp.add_run(str(len(accepted)) + " clause(s) modifiee(s)").bold = True

    RISK_LABEL = {"high": "Risque eleve", "medium": "Risque modere", "low": "Risque faible"}
    RISK_COLOR = {"high": RGBColor(0xEF,0x44,0x44), "medium": RGBColor(0xF5,0x9E,0x0B), "low": RGBColor(0x10,0xB9,0x81)}

    for i, mod in enumerate(accepted):
        doc.add_paragraph()
        doc.add_heading(str(i+1) + ". " + mod.get("clause_name", "Clause"), level=2)

        risk = mod.get("risk", "")
        if risk:
            rp = doc.add_paragraph()
            rr = rp.add_run("[ " + RISK_LABEL.get(risk, risk) + " ]")
            rr.font.color.rgb = RISK_COLOR.get(risk, RGBColor(0x70,0x70,0x70))
            rr.font.size = Pt(9); rr.bold = True

        reason = mod.get("reason", "")
        if reason:
            rp2 = doc.add_paragraph()
            rr2 = rp2.add_run(reason)
            rr2.font.size = Pt(9)
            rr2.font.color.rgb = RGBColor(0x50,0x50,0x50)
            rr2.italic = True

        pl = doc.add_paragraph()
        rl = pl.add_run("TEXTE ORIGINAL :")
        rl.bold = True; rl.font.size = Pt(9)
        rl.font.color.rgb = RGBColor(0xCC,0x00,0x00)

        po = doc.add_paragraph()
        po.paragraph_format.left_indent = Cm(0.5)
        ro = po.add_run(mod.get("original", ""))
        ro.font.color.rgb = RGBColor(0xCC,0x00,0x00)
        ro.font.strike = True

        pa = doc.add_paragraph("Proposition de modification :")
        pa.runs[0].bold = True
        pa.runs[0].font.size = Pt(9)
        pa.runs[0].font.color.rgb = RGBColor(0x00,0x80,0x00)

        pp = doc.add_paragraph()
        pp.paragraph_format.left_indent = Cm(0.5)
        rp3 = pp.add_run(mod.get("proposed", ""))
        rp3.font.color.rgb = RGBColor(0x00,0x70,0x00)
        rp3.bold = True

        sep = doc.add_paragraph()
        pPr = sep._p.get_or_add_pPr()
        pBdr = _OE("w:pBdr")
        bottom = _OE("w:bottom")
        bottom.set(_qn("w:val"), "single")
        bottom.set(_qn("w:sz"), "4")
        bottom.set(_qn("w:space"), "1")
        bottom.set(_qn("w:color"), "CCCCCC")
        pBdr.append(bottom); pPr.append(pBdr)

    out = io.BytesIO(); doc.save(out); out.seek(0); return out


def apply_track_changes(file_bytes, modifications, decisions):
    doc = Document(io.BytesIO(file_bytes))
    author = "ContractSense"
    date = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    rev_id = 1

    accepted = [m for m in modifications if decisions.get(str(m["id"])) == "accepted"]
    applied = set()
    paragraphs = list(doc.paragraphs)

    for mod in accepted:
        mod_id = mod.get("id")
        proposed = mod.get("proposed", "").strip()
        if not proposed:
            continue

        para = None

        # Method 1: Use para_idx if available (precise)
        para_idx = mod.get("para_idx")
        if para_idx is not None and para_idx < len(paragraphs):
            candidate = paragraphs[para_idx]
            if candidate.text.strip():
                para = candidate

        # Method 2: Fuzzy match fallback
        if para is None:
            original = mod.get("original", "").strip()
            for p in paragraphs:
                if p.text.strip() and fuzzy_match(original, p.text.strip()):
                    para = p
                    break

        # Handle new clauses (type=nouvelle_clause) — insert as new paragraph
        if mod.get('type') == 'nouvelle_clause':
            insertion_after = mod.get('insertion_after')
            insert_para = None
            MIN_INSERT_IDX = 5

            # Find insertion point — use insertion_after directly
            if insertion_after is not None:
                safe_idx = max(int(insertion_after), MIN_INSERT_IDX)
                if safe_idx < len(paragraphs):
                    insert_para = paragraphs[safe_idx]

            # Fallback: insert before last paragraph
            if insert_para is None:
                for p in reversed(paragraphs):
                    if p.text.strip() and len(p.text.strip()) > 10:
                        insert_para = p
                        break

            if insert_para is not None:
                # Copy formatting from insert_para run
                ref_rpr = None
                if insert_para.runs:
                    ref_rpr = insert_para.runs[0]._r.find(qn('w:rPr'))

                # Build new paragraph with Track Changes ins
                new_p = OxmlElement('w:p')

                # Copy paragraph properties if available
                if insert_para._p.find(qn('w:pPr')) is not None:
                    import copy
                    new_ppr = copy.deepcopy(insert_para._p.find(qn('w:pPr')))
                    new_p.append(new_ppr)

                ins_elem = OxmlElement('w:ins')
                ins_elem.set(qn('w:id'), str(rev_id))
                ins_elem.set(qn('w:author'), author)
                ins_elem.set(qn('w:date'), date)
                rev_id += 1

                new_r = OxmlElement('w:r')
                # Copy run formatting
                if ref_rpr is not None:
                    import copy
                    new_r.append(copy.deepcopy(ref_rpr))
                new_t = OxmlElement('w:t')
                new_t.text = proposed
                new_t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                new_r.append(new_t)
                ins_elem.append(new_r)
                new_p.append(ins_elem)

                # Insert AFTER target paragraph
                # addnext inserts before in lxml — get next sibling and insert before it
                next_sib = insert_para._p.getnext()
                if next_sib is not None:
                    insert_para._p.getparent().insert(
                        list(insert_para._p.getparent()).index(next_sib),
                        new_p
                    )
                else:
                    insert_para._p.getparent().append(new_p)
                applied.add(mod_id)
                print(f"Inserted new clause '{mod.get('clause_name')}' after para {insertion_after}")
            else:
                print(f"Could not find insertion point for new clause: {mod.get('clause_name')}")
            continue

        if para is None:
            print(f"Could not find paragraph for mod {mod_id}: {mod.get('clause_name')}")
            continue

        para_text = para.text.strip()

        # Clear all runs
        for run in para.runs:
            run.text = ""
        p = para._p

        # Del element
        del_elem = OxmlElement('w:del')
        del_elem.set(qn('w:id'), str(rev_id))
        del_elem.set(qn('w:author'), author)
        del_elem.set(qn('w:date'), date)
        del_run = OxmlElement('w:r')
        del_rpr = OxmlElement('w:rPr')
        del_run.append(del_rpr)
        del_text = OxmlElement('w:delText')
        del_text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        del_text.text = para_text
        del_run.append(del_text)
        del_elem.append(del_run)
        p.append(del_elem)
        rev_id += 1

        # Ins element
        ins_elem = OxmlElement('w:ins')
        ins_elem.set(qn('w:id'), str(rev_id))
        ins_elem.set(qn('w:author'), author)
        ins_elem.set(qn('w:date'), date)
        ins_run = OxmlElement('w:r')
        ins_text_el = OxmlElement('w:t')
        ins_text_el.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        ins_text_el.text = proposed
        ins_run.append(ins_text_el)
        ins_elem.append(ins_run)
        p.append(ins_elem)
        rev_id += 1

        applied.add(mod_id)

    print(f"Track changes: {len(applied)}/{len(accepted)} applied")
    output = io.BytesIO()
    doc.save(output)
    output.seek(0)
    return output

# ── Routes ────────────────────────────────────────────────
@app.route("/debug-env", methods=["GET"])
def debug_env():
    try:
        test = supa_get("rag_documents", {"select": "id", "limit": "1"})
        supa_status = "OK - " + str(len(test)) + " docs"
    except Exception as e:
        supa_status = "ERROR: " + str(e)
    return jsonify({
        "supabase_url": SUPA_URL[:40],
        "supabase_key_set": bool(SUPA_KEY),
        "supabase_test": supa_status,
        "anthropic_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "voyage_key_set": bool(os.environ.get("VOYAGE_API_KEY"))
    })


@app.route("/queue/add", methods=["POST", "OPTIONS"])
def queue_add():
    """Ajoute une analyse à la queue admin — stocké en Supabase"""
    if request.method == "OPTIONS":
        return "", 204
    try:
        data = request.get_json() or {}
        doc = {
            "filename": data.get("filename", "Contrat"),
            "contract_type": data.get("contract_type", ""),
            "partie": data.get("partie", ""),
            "accepted_modifications": data.get("accepted_modifications", "[]"),
            "decisions": data.get("decisions", "{}"),
            "submitted_by": data.get("submitted_by", "user"),
            "score": data.get("score", 75),
            "status": "pending"
        }
        supa_insert("analyses_queue", doc)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500


@app.route("/rag/suggest", methods=["POST", "OPTIONS"])
def rag_suggest():
    if request.method == "OPTIONS":
        return "", 204
    try:
        filename = request.form.get("source") or request.form.get("filename") or ""
        file_obj = request.files.get("file")
        if not filename and file_obj:
            filename = file_obj.filename or "inconnu"
        if not filename:
            filename = "inconnu"
        category = request.form.get("category", "contract")
        suggested_by = request.form.get("suggested_by", "anonyme")
        file = request.files.get("file")
        content_text = ""
        if file:
            try:
                content_text = file.read().decode("utf-8", errors="ignore")[:50000]
            except:
                content_text = ""
        supa_insert("pending_suggestions", {
            "filename": filename,
            "content": content_text,
            "category": category,
            "suggested_by": suggested_by,
            "status": "pending"
        })
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500

@app.route("/suggestions/list", methods=["GET"])
def suggestions_list():
    try:
        url = SUPA_URL + "/rest/v1/pending_suggestions?order=submitted_at.desc&limit=100&select=id,filename,category,suggested_by,status,submitted_at"
        headers = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"}
        r = requests.get(url, headers=headers, timeout=10)
        return jsonify({"suggestions": r.json() if r.ok else []})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500

@app.route("/suggestions/preview/<suggestion_id>", methods=["GET", "OPTIONS"])
def suggestion_preview(suggestion_id):
    if request.method == "OPTIONS":
        return "", 204
    try:
        url = SUPA_URL + "/rest/v1/pending_suggestions?id=eq." + suggestion_id + "&select=filename,content,category,suggested_by"
        r = requests.get(url, headers=supa_headers(), timeout=15)
        data = r.json()
        if not data:
            return jsonify({"error": "Suggestion non trouvee"}), 404
        s = data[0]
        content = s.get("content", "") or ""
        filename = s.get("filename", "document") or "document"
        # Return as downloadable text
        from flask import Response
        resp = Response(content, mimetype="text/plain; charset=utf-8")
        resp.headers["Content-Disposition"] = "inline; filename=" + filename
        return resp
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500

@app.route("/suggestions/approve/<suggestion_id>", methods=["POST", "OPTIONS"])
def suggestion_approve(suggestion_id):
    if request.method == "OPTIONS":
        return "", 204
    try:
        url = SUPA_URL + f"/rest/v1/pending_suggestions?id=eq.{suggestion_id}&select=*"
        headers = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"}
        r = requests.get(url, headers=headers, timeout=10)
        docs = r.json()
        if not docs:
            return jsonify({"error": "Non trouve"}), 404
        doc = docs[0]
        voyage_key = os.environ.get("VOYAGE_API_KEY", "")
        emb = get_embedding((doc.get("content") or "")[:1000], voyage_key)
        rag_doc = {
            "source": doc["filename"],
            "title": doc["filename"],
            "content": doc.get("content", ""),
            "category": doc.get("category", "contract"),
        }
        if emb and len(emb) == 1024:
            rag_doc["embedding_vector"] = "[" + ",".join(str(x) for x in emb) + "]"
        supa_insert("rag_documents", rag_doc)
        supa_update("pending_suggestions", suggestion_id, {"status": "approved"})
        return jsonify({"status": "ok", "message": "Approuve et ajoute au RAG"})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500
@app.route("/suggestions/reject/<suggestion_id>", methods=["POST", "OPTIONS"])
def suggestion_reject(suggestion_id):
    if request.method == "OPTIONS": return "", 204
    try:
        supa_update("pending_suggestions", suggestion_id, {"status": "rejected"})
        return jsonify({"status": "ok", "message": "Suggestion rejetee"})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500

# ===== DIRECTOR SUGGESTIONS (juriste -> directeur -> admin) =====

@app.route("/rag/suggest-to-director", methods=["POST", "OPTIONS"])
def suggest_to_director():
    if request.method == "OPTIONS": return "", 204
    try:
        file_obj = request.files.get("file")
        filename = request.form.get("source", "") or (file_obj.filename if file_obj else "inconnu")
        if not filename or filename == "inconnu":
            filename = file_obj.filename if file_obj else "inconnu"
        category = request.form.get("category", "contract")
        suggested_by = request.form.get("suggested_by", "")
        target_email = request.form.get("target_email", "")
        content_text = ""
        if file_obj:
            try:
                raw = file_obj.read()
                try:
                    import zipfile as zf
                    from docx import Document
                    import io as sio
                    doc_obj = Document(sio.BytesIO(raw))
                    content_text = "\n".join([p.text for p in doc_obj.paragraphs])
                except:
                    content_text = raw.decode("utf-8", errors="replace")
            except: pass
        supa_insert("pending_suggestions_director", {
            "filename": filename,
            "content": content_text,
            "category": category,
            "suggested_by": suggested_by,
            "target_director_email": target_email,
            "status": "pending"
        })
        return jsonify({"status": "ok", "message": "Suggestion envoyee au directeur"})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500

@app.route("/suggestions/list-for-director", methods=["GET", "OPTIONS"])
def suggestions_list_for_director():
    if request.method == "OPTIONS": return "", 204
    try:
        director_email = request.args.get("director_email", "")
        if not director_email:
            return jsonify({"suggestions": []})
        suggestions = supa_get("pending_suggestions_director", {
            "target_director_email": "eq." + director_email,
            "status": "eq.pending",
            "order": "created_at.desc"
        })
        result = []
        for s in (suggestions or []):
            result.append({
                "id": s.get("id"),
                "filename": s.get("filename", "inconnu"),
                "category": s.get("category", ""),
                "suggested_by": s.get("suggested_by", ""),
                "status": s.get("status", "pending"),
                "submitted_at": s.get("created_at", "")
            })
        return jsonify({"suggestions": result})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500

@app.route("/suggestions/forward-to-admin/<suggestion_id>", methods=["POST", "OPTIONS"])
def forward_suggestion_to_admin(suggestion_id):
    if request.method == "OPTIONS": return "", 204
    try:
        # Get the director suggestion
        rows = supa_get("pending_suggestions_director", {"id": "eq." + suggestion_id})
        if not rows:
            return jsonify({"error": "Suggestion introuvable"}), 404
        s = rows[0]
        # Insert into main admin suggestions queue
        supa_insert("pending_suggestions", {
            "filename": s.get("filename", "inconnu"),
            "content": s.get("content", ""),
            "category": s.get("category", "contract"),
            "suggested_by": s.get("suggested_by", ""),
            "status": "pending"
        })
        # Mark director suggestion as forwarded
        supa_update("pending_suggestions_director", suggestion_id, {"status": "forwarded"})
        return jsonify({"status": "ok", "message": "Suggestion transmise a admin"})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500

@app.route("/suggestions/reject-director/<suggestion_id>", methods=["POST", "OPTIONS"])
def reject_director_suggestion(suggestion_id):
    if request.method == "OPTIONS": return "", 204
    try:
        supa_update("pending_suggestions_director", suggestion_id, {"status": "rejected"})
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500


@app.route("/analyses/request-revision/<analysis_id>", methods=["POST", "OPTIONS"])
def request_revision_by_director(analysis_id):
    if request.method == "OPTIONS": return "", 204
    try:
        data = request.get_json() or {}
        modifications = data.get("modifications", [])
        director_notes = (data.get("director_notes") or "").strip()
        modifications = [m for m in modifications if not (isinstance(m, dict) and m.get("_isDirectorNote"))]
        if director_notes:
            modifications = [{"_isDirectorNote": True, "note": director_notes}] + modifications
        patch = {
            "status": "revision_requested",
            "modifications": modifications,
            "director_email": data.get("director_email", "")
        }
        # Use return=representation to detect silent RLS failures (empty array = 0 rows updated)
        patch_url = SUPA_URL + f"/rest/v1/analyses?id=eq.{analysis_id}"
        patch_headers = {
            "apikey": SUPA_KEY,
            "Authorization": "Bearer " + SUPA_KEY,
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }
        r = requests.patch(patch_url, headers=patch_headers, json=patch, timeout=10)
        if not r.ok:
            err = r.json() if r.content else {}
            return jsonify({"error": err.get("message", f"Erreur Supabase {r.status_code}")}), 500
        rows = r.json() if r.content else []
        if not rows:
            return jsonify({"error": "Analyse introuvable ou droits insuffisants"}), 403
        return jsonify({"status": "ok", "updated": len(rows)})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500


@app.route("/analyses/validate-by-director/<analysis_id>", methods=["POST", "OPTIONS"])
def validate_analysis_by_director(analysis_id):
    if request.method == "OPTIONS": return "", 204
    try:
        data = request.get_json() or {}
        patch = {
            "status": "validated",
            "director_email": data.get("director_email", ""),
            "modifications": data.get("modifications", [])
        }
        patch_url = SUPA_URL + f"/rest/v1/analyses?id=eq.{analysis_id}"
        patch_headers = {
            "apikey": SUPA_KEY,
            "Authorization": "Bearer " + SUPA_KEY,
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }
        r = requests.patch(patch_url, headers=patch_headers, json=patch, timeout=10)
        if not r.ok:
            err = r.json() if r.content else {}
            return jsonify({"error": err.get("message", f"Erreur Supabase {r.status_code}")}), 500
        rows = r.json() if r.content else []
        if not rows:
            return jsonify({"error": "Analyse introuvable ou droits insuffisants"}), 403
        return jsonify({"status": "ok", "updated": len(rows)})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500

        return jsonify({"status": "ok", "message": "Suggestion rejetee par le directeur"})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500


# ===== ADMIN USER CREATION =====

@app.route("/admin/create-user", methods=["POST", "OPTIONS"])
def admin_create_user():
    if request.method == "OPTIONS": return "", 204
    try:
        data = request.get_json() or {}
        email = data.get("email", "").strip()
        password = data.get("password", "").strip()
        role = data.get("role", "directeur")
        parent_email = data.get("parent_email", "")
        if not email or not password:
            return jsonify({"error": "Email et mot de passe requis"}), 400
        if role == "juriste" and not parent_email:
            return jsonify({"error": "Un juriste doit être rattaché à un directeur (parent_email requis)"}), 400
        # Use service_role key to create Auth user
        service_key = SUPA_SERVICE_KEY
        free_reset = (datetime.datetime.now() + datetime.timedelta(days=7)).isoformat()
        if not service_key:
            # Fallback: only insert metadata, warn about Auth
            supa_insert("user_accounts", {
                "email": email, "role": role,
                "parent_email": parent_email if parent_email else None,
                "temp_password": password,
                "analyses_remaining": 3,
                "payment_status": "free",
                "subscription_end": free_reset
            })
            return jsonify({"status": "partial", "message": "Metadata enregistree. Configurez SUPABASE_SERVICE_KEY dans Railway pour creer automatiquement le compte Auth.", "auth_created": False})
        # Create Supabase Auth user via admin API
        auth_url = SUPA_URL.rstrip("/") + "/auth/v1/admin/users"
        auth_headers = {
            "apikey": service_key,
            "Authorization": "Bearer " + service_key,
            "Content-Type": "application/json"
        }
        auth_resp = requests.post(auth_url, headers=auth_headers, json={
            "email": email,
            "password": password,
            "email_confirm": True
        }, timeout=15)
        if not auth_resp.ok:
            err = auth_resp.json()
            return jsonify({"error": "Auth creation failed: " + err.get("message", str(err))}), 400
        auth_user = auth_resp.json()
        # Insert metadata into user_accounts
        supa_insert("user_accounts", {
            "email": email, "role": role,
            "parent_email": parent_email if parent_email else None,
            "temp_password": password,
            "analyses_remaining": 3,
            "payment_status": "free",
            "subscription_end": free_reset
        })
        return jsonify({"status": "ok", "message": "Compte cree avec succes", "auth_created": True, "user_id": auth_user.get("id")})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    rag = load_rag()
    return jsonify({"status": "ok", "rag_docs": len(rag["documents"])})

@app.route("/detect-jurisdiction", methods=["POST", "OPTIONS"])
def detect_jurisdiction():
    """Quick jurisdiction detection from contract file or text."""
    if request.method == "OPTIONS": return "", 204
    try:
        file = request.files.get("file")
        contract_text = ""
        if file:
            contract_text, _, _ = read_file(file)
        if not contract_text:
            contract_text = (request.form.get("text") or "").strip()
        if not contract_text or len(contract_text.strip()) < 20:
            return jsonify({"jurisdiction": "universel"})

        sample = contract_text[:3000].lower()
        # Rule-based heuristic detection
        if any(k in sample for k in ["code du travail marocain", "dahir", "droit marocain", "maroc", "tribunal de commerce de casablanca", "doc marocain", "droit marocain"]):
            return jsonify({"jurisdiction": "droit_marocain"})
        if any(k in sample for k in ["code du travail français", "droit français", "loi française", "tribunal de commerce de paris", "france", "code civil français"]):
            return jsonify({"jurisdiction": "droit_francais"})
        if any(k in sample for k in ["english law", "laws of england", "courts of england", "english courts", "governed by the laws of"]):
            return jsonify({"jurisdiction": "droit_anglais"})
        if any(k in sample for k in ["droit tunisien", "tunisie", "code des obligations et des contrats tunisien"]):
            return jsonify({"jurisdiction": "droit_tunisien"})
        if any(k in sample for k in ["droit algérien", "algérie", "code civil algérien"]):
            return jsonify({"jurisdiction": "droit_algerien"})
        if any(k in sample for k in ["droit belge", "belgique", "droit belge", "code civil belge"]):
            return jsonify({"jurisdiction": "droit_belge"})
        return jsonify({"jurisdiction": "universel"})
    except Exception as e:
        return jsonify({"jurisdiction": "universel"})

@app.route("/identify-parties", methods=["POST"])
def identify_parties_route():
    try:
        file = request.files.get("file")
        api_key = os.environ.get("ANTHROPIC_API_KEY") or request.form.get("api_key", "")
        lang = request.form.get("lang", "fr")
        if not file:
            return jsonify({"error": "Fichier manquant"}), 400
        contract_text, _, _ = read_file(file)
        if not contract_text or len(contract_text.strip()) < 50:
            return jsonify({"error": "Fichier vide ou illisible"}), 400
        result = identify_parties(contract_text, lang, api_key)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500


@app.route("/analyze/stream", methods=["POST", "OPTIONS"])
def analyze_stream():
    """SSE streaming endpoint: sends real-time progress events then the final result."""
    if request.method == "OPTIONS": return "", 204
    import threading, queue as _queue, json as _json

    # Read all form data before entering the generator (WSGI constraint)
    file     = request.files.get("file")
    lang     = request.form.get("lang", "fr")
    contract_type = request.form.get("type", "generic")
    api_key  = os.environ.get("ANTHROPIC_API_KEY") or request.form.get("api_key", "")
    partie   = request.form.get("partie", "la partie beneficiaire") or "la partie beneficiaire"
    user_email = request.form.get("user_email", "").strip()
    file_bytes = file.read() if file else None
    filename   = file.filename if file else ""

    q = _queue.Queue()

    def worker():
        def _cb(msg):
            q.put({"type": "progress", "message": msg})
        try:
            _cb("\U0001f4c4 Lecture du document...")
            # Auth / quota check
            if not user_email:
                q.put({"type": "error", "message": "Connexion requise."})
                return
            rows = supa_get("user_accounts", {"email": f"eq.{user_email}", "select": "analyses_remaining,is_admin", "limit": "1"})
            remaining = 9999
            if rows:
                acc = rows[0]
                remaining = 9999 if acc.get("is_admin") else (acc.get("analyses_remaining") or 0)
            if remaining <= 0:
                q.put({"type": "error", "message": "Quota epuise."})
                return
            if not file_bytes:
                q.put({"type": "error", "message": "Fichier manquant."})
                return
            import io as _io
            contract_text, _, _ = read_file(type("F", (), {"read": lambda s: file_bytes, "filename": filename, "seek": lambda s,x: None})())
            if not contract_text or len(contract_text.strip()) < 50:
                q.put({"type": "error", "message": "Fichier vide ou illisible."})
                return
            _cb(f"\U0001f4c4 Document lu ({len(contract_text.split())} mots)...")
            result = analyze_contract(contract_text, lang, contract_type, api_key, partie,
                                      file_bytes=file_bytes, filename=filename, progress_cb=_cb)
            # Quota decrement
            if remaining < 9999:
                supa_patch("user_accounts", {"analyses_remaining": remaining - 1}, f"email=eq.{user_email}")
            # Cache
            file_cache_id = str(uuid.uuid4())
            _cache_store(file_cache_id, file_bytes)
            result["file_cache_id"] = file_cache_id
            result["file_storage_path"] = None
            if file_bytes and SUPA_URL and (SUPA_SERVICE_KEY or SUPA_KEY):
                try:
                    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "docx"
                    if ext in ("docx", "pdf", "doc", "txt"):
                        storage_path = str(uuid.uuid4()) + "." + ext
                        ct_map = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                  "pdf": "application/pdf", "doc": "application/msword", "txt": "text/plain"}
                        upload_r = supa_storage_upload("contracts", storage_path, file_bytes, ct_map.get(ext, "application/octet-stream"))
                        if upload_r.ok:
                            result["file_storage_path"] = storage_path
                except Exception:
                    pass
            result["contract_text"] = contract_text[:80000]
            q.put({"type": "result", "data": result})
        except Exception as e:
            q.put({"type": "error", "message": _anthropic_error_msg(e) or str(e)})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        while True:
            try:
                item = q.get(timeout=180)
            except Exception:
                yield "data: " + _json.dumps({"type": "error", "message": "Timeout"}) + "\n\n"
                return
            if item is None:
                return
            yield "data: " + _json.dumps(item, ensure_ascii=False) + "\n\n"

    from flask import stream_with_context
    # NOTE: do NOT set Connection: keep-alive — it is forbidden in HTTP/2 (Railway uses HTTP/2)
    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        file = request.files.get("file")
        lang = request.form.get("lang", "fr")
        contract_type = request.form.get("type", "generic")
        api_key = os.environ.get("ANTHROPIC_API_KEY") or request.form.get("api_key", "")
        partie = request.form.get("partie", "la partie bénéficiaire") or "la partie bénéficiaire"
        user_email = request.form.get("user_email", "").strip()

        # Require login
        if not user_email:
            return jsonify({"error": "Connexion requise pour analyser un contrat."}), 401

        # Check analyses_remaining — upsert row if missing (3 free analyses by default)
        rows = supa_get("user_accounts", {"email": f"eq.{user_email}", "select": "analyses_remaining,is_admin", "limit": "1"})
        if not rows:
            # First time user — create free account with 3 analyses
            import datetime as _dt
            reset_date = (_dt.datetime.now() + _dt.timedelta(days=7)).isoformat()
            supa_insert("user_accounts", {
                "email": user_email, "role": "directeur",
                "analyses_remaining": 3, "payment_status": "free",
                "subscription_end": reset_date
            })
            remaining = 3
        else:
            acc = rows[0]
            if acc.get("is_admin"):
                remaining = 9999  # admin = unlimited
            else:
                remaining = acc.get("analyses_remaining", 0) or 0

        if remaining <= 0:
            return jsonify({"error": "Quota d'analyses épuisé. Veuillez renouveler votre abonnement."}), 403

        if not file:
            return jsonify({"error": "Fichier manquant"}), 400
        contract_text, file_bytes, filename = read_file(file)
        if not contract_text or len(contract_text.strip()) < 50:
            return jsonify({"error": "Fichier vide ou illisible"}), 400
        result = analyze_contract(contract_text, lang, contract_type, api_key, partie, file_bytes, filename)

        # Decrement analyses_remaining after successful analysis
        if user_email and remaining is not None:
            supa_patch("user_accounts", {"analyses_remaining": remaining - 1}, f"email=eq.{user_email}")

        # ── Cache en mémoire (toujours disponible dans la session serveur) ───
        file_cache_id = None
        if file_bytes:
            file_cache_id = str(uuid.uuid4())
            _cache_store(file_cache_id, file_bytes)
        result["file_cache_id"] = file_cache_id

        # ── Supabase Storage (persistance longue durée, optionnel) ───────────
        file_storage_path = None
        if file_bytes and SUPA_URL and (SUPA_SERVICE_KEY or SUPA_KEY):
            try:
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "docx"
                if ext in ("docx", "pdf", "doc", "txt"):
                    storage_path = str(uuid.uuid4()) + "." + ext
                    ct_map = {
                        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "pdf": "application/pdf",
                        "doc": "application/msword",
                        "txt": "text/plain",
                    }
                    upload_r = supa_storage_upload("contracts", storage_path, file_bytes, ct_map.get(ext, "application/octet-stream"))
                    if upload_r.ok:
                        file_storage_path = storage_path
                    else:
                        print(f"Storage upload failed {upload_r.status_code}: {upload_r.text[:200]}")
            except Exception as _e:
                print(f"Storage upload error: {_e}")
        result["file_storage_path"] = file_storage_path
        # Include extracted contract text so frontend can cache it for chatbot
        result["contract_text"] = contract_text[:80000]

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500


@app.route("/analyze-clause", methods=["POST", "OPTIONS"])
def analyze_clause():
    if request.method == "OPTIONS": return "", 204
    try:
        data = request.get_json() or {}
        clause_name = (data.get("clause_name") or "").strip()
        clause_text = (data.get("clause_text") or "").strip()
        contract_type = data.get("type", "general")
        partie = data.get("partie", "la partie bénéficiaire")
        if not clause_name:
            return jsonify({"error": "clause_name requis"}), 400

        # Limit to 8000 chars — enough for even the longest résiliation article
        clause_text_trunc = clause_text[:8000] if clause_text else ""
        if clause_text and len(clause_text) > 8000:
            clause_text_trunc += "\n[…texte tronqué]"

        prompt = f"""Tu es un juriste expert. Analyse la clause suivante extraite d'un contrat de type "{contract_type}".

Nom de la clause : {clause_name}
Texte de la clause :
{clause_text_trunc or "(texte non fourni — analyse sur la base du nom uniquement)"}

Réponds UNIQUEMENT avec un objet JSON valide (sans markdown, sans backticks).
IMPORTANT : les valeurs "original" et "proposed" doivent être des résumés concis (max 300 caractères chacun), PAS une reproduction intégrale du texte.
{{
  "original": "résumé concis de la clause originale (max 300 car.)",
  "proposed": "rédaction améliorée protégeant {partie} (max 300 car.)",
  "risk": "high|medium|low",
  "reason": "explication concise du risque et de la modification proposée (max 400 car.)"
}}"""

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        msg = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        # Nettoyer si markdown
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        result = json.loads(raw)
        return jsonify(result)
    except json.JSONDecodeError:
        return jsonify({"error": "Réponse IA invalide", "raw": raw[:200]}), 500
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500


@app.route("/export", methods=["POST"])
def export():
    try:
        file = request.files.get("file")
        file_storage_path = request.form.get("file_storage_path", "").strip()
        file_cache_id = request.form.get("file_cache_id", "").strip()
        modifications = json.loads(request.form.get("modifications", "[]"))
        decisions = json.loads(request.form.get("decisions", "{}"))

        # Strip internal metadata entries before processing
        modifications = [m for m in modifications if not m.get("_isClauseMeta") and not m.get("_isFileMeta")]

        file_bytes = None
        filename = ""

        # 1. Cache mémoire (priorité : même session serveur, 100% fiable)
        if file_cache_id:
            cached = _cache_get(file_cache_id)
            if cached:
                file_bytes = cached
                filename = "contrat.docx"

        # 2. Supabase Storage (persistance longue durée)
        if file_bytes is None and file_storage_path and SUPA_URL and (SUPA_SERVICE_KEY or SUPA_KEY):
            downloaded = supa_storage_download("contracts", file_storage_path)
            if downloaded:
                file_bytes = downloaded
                filename = file_storage_path.rsplit("/", 1)[-1].lower()

        # 3. Fallback : fichier uploadé directement dans la requête
        if file_bytes is None:
            if not file:
                return jsonify({"error": "Fichier manquant"}), 400
            file_bytes = file.read()
            filename = file.filename.lower()

        if filename.endswith(".docx"):
            try:
                output = apply_track_changes(file_bytes, modifications, decisions)
            except Exception as zip_err:
                # File is not a valid DOCX (e.g. text content with .docx extension)
                text_content = file_bytes.decode("utf-8", errors="ignore")
                output = create_docx_with_changes(text_content, modifications, decisions)
        elif filename.endswith(".doc"):
            # Old .doc format — extract text then create new DOCX
            doc_text = extract_text_from_docx(file_bytes) or ""
            output = create_docx_with_changes(doc_text, modifications, decisions)
        else:
            doc = Document()
            doc.add_heading('ContractSense - Modifications acceptées', 0)
            accepted = [m for m in modifications if decisions.get(str(m["id"])) == "accepted"]
            for i, m in enumerate(accepted):
                doc.add_heading(f"{i+1}. {m.get('clause_name', '')}", level=2)
                p_del = doc.add_paragraph()
                run_del = p_del.add_run(m.get("original", ""))
                rpr = run_del._r.get_or_add_rPr()
                strike = OxmlElement('w:strike')
                rpr.append(strike)
                color = OxmlElement('w:color')
                color.set(qn('w:val'), 'FF0000')
                rpr.append(color)
                p_ins = doc.add_paragraph()
                run_ins = p_ins.add_run(m.get("proposed", ""))
                rpr2 = run_ins._r.get_or_add_rPr()
                color2 = OxmlElement('w:color')
                color2.set(qn('w:val'), '008000')
                rpr2.append(color2)
            output = io.BytesIO()
            doc.save(output)
            output.seek(0)

        return send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True,
            download_name="contrat-track-changes.docx"
        )
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500

# ── Queue: Supabase REST storage ─────────────────────────
def load_queue():
    try:
        items = supa_get("queue_pending", {"select": "*", "order": "submitted_at", "limit": "200"})
        return {"pending": items or []}
    except Exception as e:
        print("load_queue error: " + str(e))
        return {"pending": []}

def save_queue_item(item):
    try:
        item_copy = dict(item)
        for field in ["key_clauses", "accepted_modifications"]:
            if field in item_copy and not isinstance(item_copy[field], str):
                item_copy[field] = json.dumps(item_copy.get(field, []))
        supa_insert("queue_pending", item_copy)
    except Exception as e:
        print("save_queue_item error: " + str(e))

def delete_queue_item(item_id):
    try:
        supa_delete("queue_pending", {"id": "eq." + item_id})
    except Exception as e:
        print("delete_queue_item error: " + str(e))

@app.route("/rag/contribute", methods=["POST"])
def rag_contribute():
    """Auto-queue full contract with AI scoring for admin validation"""
    try:
        file = request.files.get("file")
        modifications = json.loads(request.form.get("modifications", "[]"))
        decisions = json.loads(request.form.get("decisions", "{}"))
        partie = request.form.get("partie", "")
        contract_type = request.form.get("contract_type", "generic")
        api_key = os.environ.get("ANTHROPIC_API_KEY") or request.form.get("api_key", "")

        if not file:
            return jsonify({"error": "Fichier manquant"}), 400

        contract_text, _, filename = read_file(file)
        accepted = [m for m in modifications if decisions.get(str(m["id"])) == "accepted"]
        rejected = [m for m in modifications if decisions.get(str(m["id"])) == "rejected"]

        # Use user-edited version if available — higher quality for RAG
        for m in accepted:
            if m.get("proposed_edited"):
                m["proposed"] = m["proposed_edited"]
                m["user_refined"] = True

        if rejected:
            print("Rejected clauses (" + str(len(rejected)) + "): " + ", ".join([m.get("clause_name","?") for m in rejected]))

        # AI scoring of contract quality for RAG
        client = anthropic.Anthropic(api_key=api_key)
        scoring_prompt = """Evalue ce contrat pour une base de connaissances juridiques.
Reponds UNIQUEMENT en JSON valide, sans markdown:
{
  "score": 0-100,
  "category": "nda|saas|purchase|employment|partnership|service|collaboration|generic",
  "party_label": "favorable """ + (partie if partie else "neutre") + """",
  "quality_reason": "1 phrase expliquant le score",
  "key_clauses": ["clause1", "clause2", "clause3"]
}
Regles:
- category: deduis du CONTENU du contrat, pas du type selectionne par l utilisateur
  * service = contrat de prestation de services, collaboration, mission
  * nda = confidentialite
  * employment = travail, salarie
  * partnership = association, joint-venture
  * purchase = achat, vente
  * saas = logiciel, abonnement
- party_label: utilise un label GENERIQUE selon le role de la partie dans CE contrat
  * service/prestation/collaboration/mission → "favorable client" ou "favorable prestataire"
  * travail/salarie → "favorable employeur" ou "favorable employe"
  * nda/confidentialite → "favorable divulgateur" ou "favorable destinataire"
  * achat/vente → "favorable acheteur" ou "favorable vendeur"
  * partenariat/association → "favorable partenaire A" ou "favorable partenaire B"
  NE JAMAIS utiliser le nom d une societe ou d une personne dans party_label.
  La partie favorisee dans ce contrat est: """ + (partie if partie else "neutre") + """
- score: 0-100 selon la qualite et completude du contrat
Score eleve = contrat complet avec clauses interessantes a reutiliser."""

        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=scoring_prompt,
            messages=[{"role": "user", "content": "Contrat:\n\n" + contract_text[:5000]}]
        )
        raw = message.content[0].text
        match = re.search(r'\{[\s\S]*\}', raw)
        scoring = json.loads(match.group(0)) if match else {"score": 50, "category": contract_type, "party_label": f"favorable {partie}", "quality_reason": "Scoring indisponible", "key_clauses": []}

        import uuid
        import uuid as _uuid
        save_queue_item({
            "id": str(_uuid.uuid4()),
            "contract_text": contract_text[:50000],
            "filename": filename,
            "partie": partie,
            "party_label": normalize_party_label(scoring.get("party_label", partie), contract_type),
            "contract_type": contract_type,
            "score": scoring.get("score", 50),
            "category": scoring.get("category", contract_type),
            "quality_reason": scoring.get("quality_reason", ""),
            "key_clauses": scoring.get("key_clauses", []),
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
            "accepted_modifications": accepted,
            "submitted_at": datetime.datetime.now().isoformat()
        })
        return jsonify({"success": True, "score": scoring.get("score", 50)})

    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500


@app.route("/queue/list", methods=["GET"])
def queue_list():
    """Liste les analyses en attente de validation admin"""
    try:
        # Try analyses_queue table first
        docs = supa_get("analyses_queue", {
            "select": "id,filename,contract_type,partie,submitted_by,score,status,accepted_modifications,decisions,created_at",
            "status": "eq.pending",
            "order": "created_at.desc",
            "limit": "100"
        })
        if docs is None:
            docs = []
        # Parse modifications
        result = []
        for d in docs:
            try:
                mods = json.loads(d.get("accepted_modifications") or "[]")
            except:
                mods = []
            # Count accepted/rejected
            accepted = [m for m in mods if not isinstance(m, dict) or m.get("decision") != "rejected"]
            rejected_mods = [m for m in mods if isinstance(m, dict) and m.get("decision") == "rejected"]
            result.append({
                "id": d.get("id"),
                "filename": d.get("filename", "Contrat"),
                "contract_type": d.get("contract_type", ""),
                "category": d.get("contract_type", "contract"),
                "partie": d.get("partie", ""),
                "party_label": d.get("partie", ""),
                "submitted_by": d.get("submitted_by", ""),
                "score": d.get("score", 75),
                "quality_reason": d.get("quality_reason", "Analyse automatique"),
                "status": d.get("status", "pending"),
                "accepted_modifications": mods,
                "key_clauses": mods,
                "accepted_count": len(mods),
                "rejected_count": 0,
                "decisions": json.loads(d.get("decisions") or "{}"),
                "submitted_at": d.get("created_at", ""),
                "created_at": d.get("created_at", "")
            })
        return jsonify({"pending": result, "total": len(result)})
    except Exception as e:
        print(f"queue_list error: {e}")
        return jsonify({"pending": [], "total": 0, "error": str(e)})

@app.route("/queue/validate", methods=["POST"])
def queue_validate():
    """Admin validates contract — indexes full text into RAG"""
    try:
        body = request.get_json()
        contract_id = body.get("id")
        admin_category = body.get("category", "")
        admin_party_label = body.get("party_label", "")
        voyage_key = os.environ.get("VOYAGE_API_KEY", "")

        queue = load_queue()
        pending = queue.get("pending", [])
        contract = next((c for c in pending if c["id"] == contract_id), None)
        if not contract:
            return jsonify({"error": "Contrat introuvable"}), 404

        contract_text = contract.get("contract_text", "")
        category = admin_category or contract.get("category", "generic")
        party_label = admin_party_label or contract.get("party_label", "")

        # Use admin-edited modifications if provided
        edited_mods = body.get("edited_modifications", [])
        if edited_mods:
            # Merge edited mods back into contract
            edited_map = {m.get("id"): m for m in edited_mods if m.get("proposed")}
            accepted_mods = contract.get("accepted_modifications", [])
            if isinstance(accepted_mods, str):
                accepted_mods = json.loads(accepted_mods)
            for mod in accepted_mods:
                if mod.get("id") in edited_map:
                    mod.update(edited_map[mod["id"]])
            contract["accepted_modifications"] = accepted_mods
        title_base = f"[{category.upper()}] {party_label}"

        # Split contract into chunks and index
        import uuid
        words = contract_text.split()
        chunk_size = 400
        chunks = []
        for i in range(0, len(words), chunk_size):
            chunks.append(" ".join(words[i:i+chunk_size]))

        data = load_rag()
        for i, chunk in enumerate(chunks):
            embedding = get_embedding(chunk, voyage_key)
            title = f"{title_base} (partie {i+1})" if len(chunks) > 1 else title_base
            data["documents"].append({
                "id": str(uuid.uuid4()),
                "title": title,
                "category": category,
                "party_label": party_label,
                "partie": contract.get("partie", ""),
                "contract_type": category,
                "content": chunk,
                "embedding": embedding,
                "source": title_base,
                "key_clauses": contract.get("key_clauses", []),
                "score": contract.get("score", 50),
                "validated_at": datetime.datetime.now().isoformat()
            })

        # Also index accepted modifications as separate entries
        accepted_mods = contract.get("accepted_modifications", [])
        if isinstance(accepted_mods, str):
            accepted_mods = json.loads(accepted_mods)
        for mod in accepted_mods:
            mod_text = "CLAUSE VALIDEE [" + party_label + "]: " + mod.get('clause_name','') + "\n" + mod.get('proposed','')
            embedding = get_embedding(mod_text, voyage_key)
            normalized_label = normalize_party_label(party_label, category)
            save_rag_doc({
                "id": str(uuid.uuid4()),
                "title": "[" + CONTRACT_CATEGORIES.get(category, category.upper()) + "] " + mod.get("clause_name","") + " — " + normalized_label,
                "category": "validated_clause",
                "party_label": normalized_label,
                "partie": contract.get("partie", ""),
                "contract_type": category,
                "content": mod_text,
                "embedding": json.dumps(embedding),
                "source": "admin_validated_clause",
                "validated_at": datetime.datetime.now().isoformat()
            })

        delete_queue_item(contract_id)

        return jsonify({"success": True, "chunks_indexed": len(chunks)})

    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500


@app.route("/queue/reject", methods=["POST"])
def queue_reject():
    """Admin rejects contract — removes from queue"""
    try:
        body = request.get_json()
        contract_id = body.get("id")
        delete_queue_item(contract_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500


@app.route("/rag/upload", methods=["POST"])
def rag_upload():
    try:
        file = request.files.get("file")
        title = request.form.get("source_name") or request.form.get("title") or (file.filename.rsplit(".",1)[0] if file else "Document")
        category = request.form.get("doc_type") or request.form.get("category", "general")
        title_base = title  # Use as source key
        api_key = os.environ.get("ANTHROPIC_API_KEY") or request.form.get("api_key", "")
        if not file:
            return jsonify({"error": "Fichier manquant"}), 400

        file_bytes = file.read()
        filename = file.filename.lower()
        if filename.endswith(".docx") or filename.endswith(".doc"):
            content = extract_text_from_docx(file_bytes)
        else:
            content = file_bytes.decode("utf-8", errors="ignore")

        if not content or len(content.strip()) < 50:
            return jsonify({"error": "Document vide ou illisible"}), 400

        # Limit content size for large documents
        if len(content) > 200000:
            content = content[:200000]

        # Split into chunks of ~400 words
        words = content.split()
        chunk_size = 400
        max_chunks = 50  # Max 50 chunks per upload to avoid timeout
        chunks = []
        for i in range(0, min(len(words), chunk_size * max_chunks), chunk_size):
            chunk = " ".join(words[i:i+chunk_size])
            chunks.append(chunk)

        import uuid
        voyage_key = os.environ.get("VOYAGE_API_KEY") or request.form.get("voyage_key", "")
        for i, chunk in enumerate(chunks):
            embedding = get_embedding(chunk, voyage_key)
            chunk_title = (title + " (partie " + str(i+1) + ")") if len(chunks) > 1 else title
            save_rag_doc({
                "id": str(uuid.uuid4()),
                "title": chunk_title,
                "category": category,
                "content": chunk,
                "embedding": json.dumps(embedding),
                "source": title,
                "validated_at": datetime.datetime.now().isoformat()
            })

        total = load_rag()
        return jsonify({"success": True, "chunks": len(chunks), "source": title, "total_docs": len(total["documents"])})

    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500

@app.route("/rag/diag", methods=["GET", "POST"])
def rag_diag():
    """Diagnostic endpoint: check voyage AI, pgvector, embedding dimensions, doc count"""
    import traceback
    diag = {}
    try:
        # 1. Check env vars
        voyage_key = os.environ.get("VOYAGE_API_KEY", "")
        diag["voyage_key_present"] = bool(voyage_key)
        diag["anthropic_key_present"] = bool(os.environ.get("ANTHROPIC_API_KEY",""))

        # 2. Test Voyage AI embedding
        diag["voyage_test"] = "skipped (no key)"
        if voyage_key:
            try:
                vec = get_embedding("contrat de travail CDI Maroc", voyage_key)
                diag["voyage_test"] = "ok"
                diag["voyage_dims"] = len(vec) if vec else 0
            except Exception as e:
                diag["voyage_test"] = "error: " + str(e)

        # 3. Count docs in RAG + check embedding_vector coverage
        try:
            all_docs = supa_get("rag_documents", {"select": "id,source,category", "limit": "2000"})
            diag["total_docs"] = len(all_docs or [])
            # Check a few for embedding_vector
            sample = supa_get("rag_documents", {
                "select": "id,embedding_vector",
                "limit": "10",
                "embedding_vector": "not.is.null"
            })
            diag["docs_with_embedding_vector"] = len(sample or [])
        except Exception as e:
            diag["doc_count_error"] = str(e)

        # 4. Test pgvector search
        diag["pgvector_test"] = "skipped"
        if voyage_key:
            try:
                vec = get_embedding("contrat de travail CDI licenciement préavis Maroc", voyage_key)
                if vec and len(vec) == 1024:
                    results = search_rag_pgvector(vec, top_k=5)
                    diag["pgvector_test"] = "ok"
                    diag["pgvector_results"] = len(results)
                    diag["pgvector_titles"] = [r.get("title","?") for r in results[:3]]
                else:
                    diag["pgvector_test"] = f"wrong dims: {len(vec) if vec else 0}"
            except Exception as e:
                diag["pgvector_test"] = "error: " + str(e)

        # 5. Test keyword fallback
        try:
            kw_results = search_rag_keyword("contrat de travail CDI licenciement Maroc", contract_type="employment", top_k=5)
            diag["keyword_fallback_results"] = len(kw_results)
            diag["keyword_fallback_titles"] = [r.get("title","?") for r in kw_results[:3]]
        except Exception as e:
            diag["keyword_fallback_error"] = str(e)

    except Exception as e:
        diag["fatal_error"] = str(e)
        diag["traceback"] = traceback.format_exc()

    return jsonify(diag)


@app.route("/rag/list", methods=["GET"])
def rag_list():
    try:
        # Load ALL docs from Supabase with pagination
        all_docs = []
        offset = 0
        while True:
            batch = supa_get("rag_documents", {
                "select": "id,source,category,party_label",
                "limit": "1000",
                "offset": str(offset)
            })
            if not batch:
                break
            all_docs.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000

        grouped = {}
        for doc in all_docs:
            src = re.sub(r" \(partie \d+/\d+\)$", "", doc.get("source",""))
            src = re.sub(r" — partie \d+/\d+$", "", src)
            if src not in grouped:
                grouped[src] = {
                    "source": src,
                    "chunks": 0,
                    "type": doc.get("category",""),
                    "party_label": doc.get("party_label",""),
                    "warning": False
                }
            grouped[src]["chunks"] += 1

        for src, d in grouped.items():
            if d["chunks"] < 5:
                d["warning"] = True
                d["warning_msg"] = "Trop peu de chunks"

        result = sorted(grouped.values(), key=lambda x: (x.get("type",""), x.get("source","")))
        return jsonify({
            "documents": result,
            "total": sum(d["chunks"] for d in result),
            "total_docs": len(result)
        })
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500


@app.route("/rag/delete/<doc_id>", methods=["DELETE"])
def rag_delete_by_id(doc_id):
    try:
        sb = get_supabase()
        sb.table("rag_documents").delete().eq("id", doc_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500

@app.route("/rag/delete", methods=["POST", "DELETE", "OPTIONS"])
def rag_delete():
    if request.method == "OPTIONS":
        return "", 204
    try:
        body = request.get_json() or {}
        source = body.get("source", "")
        count = delete_rag_by_source(source)
        return jsonify({"success": True, "deleted": count})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500

# ── Account info + free tier weekly reset ────────────────────────────────────

@app.route("/account/info", methods=["POST", "OPTIONS"])
def account_info():
    if request.method == "OPTIONS": return "", 204
    data = request.get_json() or {}
    email = data.get("email", "").strip()
    if not email:
        return jsonify({"error": "email requis"}), 400
    rows = supa_get("user_accounts", {"email": f"eq.{email}", "limit": "1"})
    if not rows:
        return jsonify({"error": "compte introuvable"}), 404
    acc = rows[0]

    # Admin → toujours illimité
    if acc.get("is_admin"):
        return jsonify({**acc, "analyses_remaining": -1, "can_analyze": True})

    # Juriste → couvert uniquement par son directeur, pas de free tier
    if acc.get("role") == "juriste":
        parent_email = acc.get("parent_email")
        if not parent_email:
            return jsonify({**acc, "can_analyze": False,
                            "blocked_reason": "no_director",
                            "message": "Votre compte n'est rattaché à aucun directeur."})
        parent = supa_get("user_accounts", {"email": f"eq.{parent_email}", "limit": "1"})
        if not parent:
            return jsonify({**acc, "can_analyze": False,
                            "blocked_reason": "director_not_found"})
        p = parent[0]
        if p.get("payment_status") != "active":
            return jsonify({**acc, "can_analyze": False,
                            "blocked_reason": "director_inactive",
                            "message": "Votre directeur n'a pas d'abonnement actif."})
        sub_end = p.get("subscription_end")
        if sub_end and parse_dt(sub_end) < datetime.datetime.now():
            return jsonify({**acc, "can_analyze": False,
                            "blocked_reason": "director_expired",
                            "message": "L'abonnement de votre directeur a expiré."})
        return jsonify({**acc, "can_analyze": True, "payment_status": "active"})

    # Directeur (solo ou équipe) — abonnement actif → vérifier expiration
    if acc.get("payment_status") == "active":
        sub_end = acc.get("subscription_end")
        if sub_end and parse_dt(sub_end) < datetime.datetime.now():
            reset = (datetime.datetime.now() + datetime.timedelta(days=7)).isoformat()
            supa_patch("user_accounts",
                       {"payment_status": "free", "analyses_remaining": 3, "subscription_end": reset},
                       f"email=eq.{email}")
            acc["payment_status"] = "free"
            acc["analyses_remaining"] = 3
            acc["subscription_end"] = reset
        return jsonify({**acc, "can_analyze": acc.get("analyses_remaining", 0) > 0})

    # Directeur free → reset hebdomadaire auto
    sub_end = acc.get("subscription_end")
    if sub_end and parse_dt(sub_end) < datetime.datetime.now():
        reset = (datetime.datetime.now() + datetime.timedelta(days=7)).isoformat()
        supa_patch("user_accounts",
                   {"analyses_remaining": 3, "subscription_end": reset},
                   f"email=eq.{email}")
        acc["analyses_remaining"] = 3
        acc["subscription_end"] = reset

    rem = acc.get("analyses_remaining", 0) or 0
    return jsonify({**acc, "can_analyze": rem > 0})

# ── CMI Payment ──────────────────────────────────────────────────────────────

def cmi_hash(params, store_key):
    excluded = {"HASH", "encoding"}
    sorted_keys = sorted([k for k in params if k not in excluded], key=lambda x: x.lower())
    s = "|".join(str(params[k]) for k in sorted_keys) + "|" + store_key
    print(f"[CMI DEBUG] fields_order: {sorted_keys}", flush=True)
    for k in sorted_keys:
        print(f"[CMI DEBUG]   {k} = {params[k]}", flush=True)
    print(f"[CMI DEBUG] storekey_len={len(store_key)} storekey_start={store_key[:4]}...", flush=True)
    result = base64.b64encode(hashlib.sha512(s.encode("utf-8")).digest()).decode()
    print(f"[CMI DEBUG] HASH: {result}", flush=True)
    return result

@app.route("/payment/initiate", methods=["POST", "OPTIONS"])
def payment_initiate():
    if request.method == "OPTIONS": return "", 204
    data = request.get_json() or {}
    director_email = data.get("director_email", "")
    nb_users = int(data.get("nb_users", 1))
    role = data.get("role", "directeur")  # "juriste" = 950 DH solo, "directeur" = 850 DH/user
    price = 950 if role == "juriste" else 850
    total = nb_users * price
    order_id = f"WF-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8].upper()}"

    supa_insert("payments", {
        "director_email": director_email, "order_id": order_id,
        "amount": total, "nb_users": nb_users, "status": "pending"
    })

    params = {
        "clientid":      CMI_CLIENT_ID,
        "storetype":     "3D_PAY_HOSTING",
        "trantype":      "PreAuth",
        "amount":        f"{total:.2f}",
        "currency":      "504",
        "oid":           order_id,
        "okUrl":         f"{APP_URL}/app-v2.html?payment=success",
        "failUrl":       f"{APP_URL}/app-v2.html?payment=failed",
        "shopurl":       APP_URL,
        "callbackUrl":   "https://web-production-f96f7.up.railway.app/payment/callback",
        "lang":          "fr",
        "rnd":           datetime.datetime.now().strftime("%Y%m%d%H%M%S"),
        "hashAlgorithm": "ver3",
        "encoding":      "UTF-8",
        "email":         director_email,
    }
    params["HASH"] = cmi_hash(params, CMI_STORE_KEY)
    return jsonify({"form_url": CMI_PAYMENT_URL, "params": params, "total": total})

@app.route("/payment/callback", methods=["POST"])
def payment_callback():
    data = request.form.to_dict()
    order_id = data.get("oid", "")
    if data.get("ProcReturnCode") == "00":
        supa_patch("payments", {"status": "success", "paid_at": datetime.datetime.now().isoformat()},
                   f"order_id=eq.{order_id}")
        payments = supa_get("payments", {"order_id": f"eq.{order_id}", "limit": "1"})
        if payments:
            p = payments[0]
            sub_end = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
            nb_users = p.get("nb_users", 1)
            nb_juristes_max = max(0, nb_users - 1)  # nb_users includes director
            upd_dir = {
                "payment_status": "active", "analyses_remaining": 20,
                "subscription_end": sub_end, "nb_juristes_max": nb_juristes_max
            }
            upd_jur = {"payment_status": "active", "analyses_remaining": 20, "subscription_end": sub_end}
            supa_patch("user_accounts", upd_dir, f"email=eq.{p['director_email']}")
            juristes = supa_get("user_accounts", {"parent_email": f"eq.{p['director_email']}", "select": "email"}) or []
            for j in juristes:
                supa_patch("user_accounts", upd_jur, f"email=eq.{j['email']}")
        # Répondre ACTION=POSTAUTH pour capturer le paiement (PreAuth → capture)
        return "ACTION=POSTAUTH", 200
    else:
        supa_patch("payments", {"status": "failed"}, f"order_id=eq.{order_id}")
        return "APPROVED", 200


@app.route("/director/create-juriste", methods=["POST", "OPTIONS"])
def director_create_juriste():
    if request.method == "OPTIONS": return "", 204
    data = request.get_json() or {}
    director_email = data.get("director_email", "").strip()
    juriste_email  = data.get("juriste_email", "").strip()
    juriste_password = data.get("juriste_password", "").strip()

    if not director_email or not juriste_email or not juriste_password:
        return jsonify({"error": "Champs requis manquants"}), 400

    # Check director exists and has slots available
    rows = supa_get("user_accounts", {"email": f"eq.{director_email}", "limit": "1"})
    if not rows:
        return jsonify({"error": "Directeur introuvable"}), 404
    director = rows[0]
    is_admin = director.get("role") == "admin"

    if not is_admin and director.get("payment_status") != "active":
        return jsonify({"error": "Abonnement inactif — souscrivez d'abord un abonnement"}), 403

    if not is_admin:
        nb_juristes_max = director.get("nb_juristes_max", 0) or 0
        existing = supa_get("user_accounts", {"parent_email": f"eq.{director_email}", "select": "id"}) or []
        if len(existing) >= nb_juristes_max:
            return jsonify({
                "error": f"Quota atteint : votre abonnement inclut {nb_juristes_max} juriste(s). Modifiez votre abonnement pour en ajouter."
            }), 403

    # Create Supabase auth user via admin API
    # Si l'utilisateur existe déjà dans Auth, on met juste à jour son mot de passe
    try:
        r = requests.post(
            SUPA_URL + "/auth/v1/admin/users",
            headers={"apikey": SUPA_SERVICE_KEY, "Authorization": f"Bearer {SUPA_SERVICE_KEY}", "Content-Type": "application/json"},
            json={"email": juriste_email, "password": juriste_password, "email_confirm": True},
            timeout=15
        )
        if not r.ok:
            err_text = r.text.lower()
            if any(x in err_text for x in ["already registered", "already exists", "user already", "email_exists"]):
                # Trouver l'UUID et mettre à jour le mot de passe
                list_r = requests.get(
                    SUPA_URL + "/auth/v1/admin/users",
                    headers={"apikey": SUPA_SERVICE_KEY, "Authorization": f"Bearer {SUPA_SERVICE_KEY}"},
                    params={"filter": f"email=={juriste_email}", "per_page": "1000"},
                    timeout=15
                )
                if list_r.ok:
                    for u in (list_r.json().get("users") or []):
                        if u.get("email") == juriste_email:
                            requests.put(
                                SUPA_URL + f"/auth/v1/admin/users/{u['id']}",
                                headers={"apikey": SUPA_SERVICE_KEY, "Authorization": f"Bearer {SUPA_SERVICE_KEY}", "Content-Type": "application/json"},
                                json={"password": juriste_password},
                                timeout=15
                            )
                            break
            else:
                return jsonify({"error": "Erreur création compte auth: " + r.text[:200]}), 500
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500

    # Upsert user_accounts row (supprimer l'ancienne si elle existe, puis réinsérer)
    existing_row = supa_get("user_accounts", {"email": f"eq.{juriste_email}", "limit": "1"})
    if existing_row:
        supa_patch("user_accounts", {
            "role": "juriste", "parent_email": director_email,
            "payment_status": "active", "analyses_remaining": 20,
            "subscription_end": director.get("subscription_end", "")
        }, f"email=eq.{juriste_email}")
    else:
        supa_insert("user_accounts", {
            "email": juriste_email, "role": "juriste",
            "parent_email": director_email,
            "payment_status": "active",
            "analyses_remaining": 20,
            "subscription_end": director.get("subscription_end", "")
        })

    # Envoyer email de bienvenue avec identifiants
    app_url = os.environ.get("APP_URL", "https://contractsense.fr")
    send_email(
        to=juriste_email,
        subject="Votre accès ContractSense",
        html=f"""
<div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;padding:32px;background:#f9fafb;border-radius:12px">
  <h2 style="color:#1e293b;margin-bottom:8px">Bienvenue sur ContractSense</h2>
  <p style="color:#475569">Votre directeur vous a ajouté à son équipe. Voici vos identifiants de connexion :</p>
  <div style="background:#fff;border-radius:8px;padding:20px;margin:20px 0;border:1px solid #e2e8f0">
    <p style="margin:0 0 8px 0"><strong>Email :</strong> {juriste_email}</p>
    <p style="margin:0"><strong>Mot de passe :</strong> {juriste_password}</p>
  </div>
  <a href="{app_url}" style="display:inline-block;background:linear-gradient(135deg,#5b7cfa,#8b5cf6);color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:700">
    Accéder à ContractSense
  </a>
  <p style="color:#94a3b8;font-size:12px;margin-top:24px">Pensez à changer votre mot de passe après votre première connexion.</p>
</div>
"""
    )

    return jsonify({"status": "ok", "message": f"Compte juriste {juriste_email} créé avec succès"})


@app.route("/director/delete-juriste", methods=["POST", "OPTIONS"])
def director_delete_juriste():
    if request.method == "OPTIONS": return "", 204
    data = request.get_json() or {}
    director_email = data.get("director_email", "").strip()
    juriste_email  = data.get("juriste_email", "").strip()
    if not director_email or not juriste_email:
        return jsonify({"error": "Champs requis manquants"}), 400

    # Vérifier que le juriste appartient bien à ce directeur
    rows = supa_get("user_accounts", {"email": f"eq.{juriste_email}", "limit": "1"})
    if not rows:
        return jsonify({"error": "Juriste introuvable"}), 404
    juriste = rows[0]
    if juriste.get("parent_email") != director_email:
        return jsonify({"error": "Ce juriste n'appartient pas à votre équipe"}), 403

    # Supprimer de Supabase Auth — chercher dans toutes les pages
    auth_headers = {"apikey": SUPA_SERVICE_KEY, "Authorization": f"Bearer {SUPA_SERVICE_KEY}"}
    deleted_auth = False
    for page in range(1, 20):
        list_r = requests.get(
            SUPA_URL + "/auth/v1/admin/users",
            headers=auth_headers,
            params={"page": page, "per_page": "1000"},
            timeout=15
        )
        if not list_r.ok:
            break
        users = list_r.json().get("users") or []
        for u in users:
            if u.get("email") == juriste_email:
                requests.delete(
                    SUPA_URL + f"/auth/v1/admin/users/{u['id']}",
                    headers=auth_headers, timeout=15
                )
                deleted_auth = True
                break
        if deleted_auth or len(users) < 1000:
            break

    # Supprimer de user_accounts
    requests.delete(
        SUPA_URL + f"/rest/v1/user_accounts?email=eq.{juriste_email}",
        headers={**supa_headers(), "apikey": SUPA_SERVICE_KEY, "Authorization": f"Bearer {SUPA_SERVICE_KEY}"},
        timeout=10
    )

    return jsonify({"status": "ok", "message": f"Juriste {juriste_email} supprimé"})


@app.route("/chat", methods=["POST", "OPTIONS"])
def chat():
    if request.method == "OPTIONS": return "", 204
    try:
        data = request.get_json() or {}
        message        = (data.get("message") or "").strip()
        history        = data.get("history") or []
        contract_text  = (data.get("contract_text") or "").strip()
        modifications  = data.get("modifications") or []
        decisions      = data.get("decisions") or {}
        partie         = (data.get("partie") or "").strip()
        jurisdiction   = (data.get("jurisdiction") or "universel").strip()
        file_cache_id  = (data.get("file_cache_id") or "").strip()
        file_storage_path = (data.get("file_storage_path") or "").strip()

        if not message:
            return jsonify({"error": "message requis"}), 400

        # Try to retrieve contract text from cache / storage if not provided
        if not contract_text and file_cache_id:
            cached = _cache_get(file_cache_id)
            if cached:
                try:
                    contract_text = cached.decode("utf-8", errors="replace")[:80000]
                except Exception:
                    pass
        if not contract_text and file_storage_path and SUPA_URL and (SUPA_SERVICE_KEY or SUPA_KEY):
            downloaded = supa_storage_download("contracts", file_storage_path)
            if downloaded:
                fname = file_storage_path.rsplit("/", 1)[-1].lower()
                try:
                    if fname.endswith(".docx"):
                        import io as _io
                        doc = Document(_io.BytesIO(downloaded))
                        contract_text = "\n".join(p.text for p in doc.paragraphs)[:80000]
                    else:
                        contract_text = downloaded.decode("utf-8", errors="replace")[:80000]
                except Exception:
                    pass

        # Build accepted modifications summary
        accepted_mods = [m for m in modifications if decisions.get(str(m.get("id") or "")) == "accepted"]
        mods_summary = ""
        if accepted_mods:
            lines = []
            for m in accepted_mods[:10]:
                lines.append(f"- {m.get('clause_name','?')}: {(m.get('proposed') or '')[:120]}")
            mods_summary = "\nMODIFICATIONS DÉJÀ ACCEPTÉES PAR LE CLIENT:\n" + "\n".join(lines)

        # Full contract sent every time — prompt caching makes it cheap after 1st call
        contract_excerpt = contract_text[:80000] if contract_text else ""

        # System prompt
        system_prompt = (
            "Tu es un assistant juridique expert en droit des contrats. "
            "Tu aides un avocat à analyser et améliorer un contrat. "
            "Réponds toujours en français, de manière professionnelle.\n"
            + (f"Partie représentée : {partie}. Tu défends UNIQUEMENT les intérêts de cette partie.\n" if partie else "")
            + (f"Juridiction : {jurisdiction}.\n" if jurisdiction and jurisdiction != "universel" else "")
            + (f"\nCONTRAT COMPLET:\n{contract_excerpt}\n" if contract_excerpt else "")
            + mods_summary
            + """

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LOI ABSOLUE — BLOCS <modification> OBLIGATOIRES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CHAQUE FOIS que tu mentionnes, analyses, commentes ou révises une clause du contrat,
tu DOIS impérativement terminer ta réponse avec un ou plusieurs blocs <modification>.

C'est INTERDIT de répondre sur une clause sans produire ce bloc.
C'est INTERDIT d'écrire uniquement du texte narratif quand une clause est discutée.

FORMAT EXACT (respecter à la lettre) :
<modification>
{"clause_name":"[Article X.X – Titre de la clause]","original":"[COPIE MOT POUR MOT du texte original depuis le contrat, sans résumer, sans couper]","proposed":"[RÉDACTION COMPLÈTE de la nouvelle version, texte intégral de la clause révisée]"}
</modification>

RÈGLES :
• "original" = copie intégrale exacte depuis le contrat (même ponctuation, même casse)
• "proposed" = texte complet et rédigé, pas un résumé, pas des pointillés
• Plusieurs clauses discutées = plusieurs blocs séparés
• SEULE exception : question purement théorique sans mention d'une clause précise du contrat

EXEMPLE :
Utilisateur : "Peux-tu revoir la clause de confidentialité ?"
Ta réponse :
J'ai analysé l'Article 15.1. Voici mes recommandations : [analyse textuelle]
<modification>
{"clause_name":"Article 15.1 – Confidentialité","original":"Les parties s'engagent à garder confidentielles toutes les informations échangées.","proposed":"Les parties s'engagent mutuellement et irrévocablement à maintenir strictement confidentielles toutes informations, documents et données échangés dans le cadre du présent accord, pour une durée de cinq (5) ans suivant son expiration, sous peine de dommages et intérêts."}
</modification>
"""
        )

        # Build messages list for Claude
        messages = []
        for h in (history or [])[-8:]:
            role = h.get("role") or "user"
            content = h.get("content") or ""
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        # Ensure last message is the current user message
        if not messages or messages[-1].get("content") != message:
            messages.append({"role": "user", "content": message})

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        # Use prompt caching: contract text cached after 1st call (~90% cost reduction on cache hits)
        system_blocks = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=system_blocks,
            messages=messages,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"}
        )
        reply_raw = response.content[0].text.strip()

        # Extract all <modification>...</modification> blocks
        mod_blocks = re.findall(r'<modification>(.*?)</modification>', reply_raw, re.DOTALL)
        mod_list = []
        for block in mod_blocks:
            block = block.strip()
            try:
                obj = json.loads(block)
                if obj.get("clause_name") and obj.get("proposed"):
                    mod_list.append(obj)
            except Exception:
                pass

        # Clean reply text — remove modification blocks
        reply_text = re.sub(r'<modification>.*?</modification>', '', reply_raw, flags=re.DOTALL).strip()

        result = {"reply": reply_text}
        if mod_list:
            result["modifications"] = mod_list
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500


# ── Static frontend ──────────────────────────────────────────────────────────
@app.route("/app-v2.html", methods=["GET"])
@app.route("/app-v2", methods=["GET"])
@app.route("/", methods=["GET"])
@app.route("/index.html", methods=["GET"])
def serve_frontend():
    resp = send_file(os.path.join(os.path.dirname(__file__), "static", "app-v2.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/westfield-ghost.png", methods=["GET"])
def serve_logo():
    resp = send_file(os.path.join(os.path.dirname(__file__), "static", "westfield-ghost.png"))
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


def _init_storage():
    """Crée le bucket Supabase Storage au démarrage si inexistant."""
    if not SUPA_URL or not (SUPA_SERVICE_KEY or SUPA_KEY):
        return
    try:
        r = supa_storage_ensure_bucket("contracts")
        if r.ok:
            print("Storage bucket 'contracts' pret.")
        elif "already exists" in r.text.lower() or r.status_code == 409:
            print("Storage bucket 'contracts' deja existant.")
        else:
            print(f"Storage bucket init: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"Storage bucket init error: {e}")

_init_storage()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, timeout=120)
