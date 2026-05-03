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
        # Parse JSON string if caller passed json.dumps() instead of raw list
        if emb and isinstance(emb, str):
            try:
                emb = json.loads(emb)
                doc_copy["embedding"] = emb
            except Exception:
                emb = None
        if emb and isinstance(emb, list) and len(emb) == 1024:
            # pgvector requires string format "[x,y,...]" — NOT a JSON array
            doc_copy["embedding_vector"] = "[" + ",".join(str(x) for x in emb) + "]"
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
            if not isinstance(results, list):
                print(f"pgvector unexpected response (not a list): {str(results)[:200]}")
                return []
            print(f"pgvector search: {len(results)} results")
            return results
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
            results = r.json()
            if not isinstance(results, list):
                print(f"Hybrid unexpected response (not a list): {str(results)[:200]}")
            else:
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
        # Walk the body in document order: paragraphs AND table cells
        for element in doc.element.body:
            tag = element.tag.split('}')[-1] if '}' in element.tag else element.tag
            if tag == 'p':
                from docx.text.paragraph import Paragraph as _Para
                para = _Para(element, doc)
                if para.text.strip():
                    text.append(para.text)
            elif tag == 'tbl':
                from docx.table import Table as _Tbl
                table = _Tbl(element, doc)
                for row in table.rows:
                    row_texts = []
                    for cell in row.cells:
                        cell_text = " ".join(p.text.strip() for p in cell.paragraphs if p.text.strip())
                        if cell_text:
                            row_texts.append(cell_text)
                    if row_texts:
                        text.append(" | ".join(row_texts))
        if not text:
            text = [p.text for p in doc.paragraphs if p.text.strip()]
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

def extract_text_from_pdf(file_bytes):
    """Extract plain text from a PDF. Uses pypdf for text PDFs, falls back to
    Claude Vision (Haiku) for scanned PDFs where pypdf returns little/no text."""
    text = ""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = []
        for page in reader.pages:
            t = page.extract_text() or ""
            if t.strip():
                pages.append(t)
        text = "\n".join(pages)
    except Exception as e:
        print("pypdf error: " + str(e))

    # If pypdf extracted enough text, use it
    if len(text.strip()) > 200:
        return text

    # Scanned PDF fallback: send to Claude Vision via Anthropic API
    print("PDF semble scanné (pypdf < 200 chars) — fallback Claude Vision OCR")
    try:
        _ak = os.environ.get("ANTHROPIC_API_KEY", "")
        if not _ak:
            return text
        _client = anthropic.Anthropic(api_key=_ak)
        _pdf_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
        _resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8192,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": _pdf_b64
                        }
                    },
                    {
                        "type": "text",
                        "text": "Extrait tout le texte de ce document. Retourne uniquement le texte brut, sans commentaires ni formatage markdown."
                    }
                ]
            }]
        )
        ocr_text = _resp.content[0].text if _resp.content else ""
        print(f"Claude Vision OCR: {len(ocr_text)} chars extraits")
        return ocr_text
    except Exception as e:
        print("Claude Vision PDF OCR error: " + str(e))
        return text

def read_file(file):
    file_bytes = file.read()
    filename = file.filename.lower()
    if filename.endswith(".docx") or filename.endswith(".doc"):
        text = extract_text_from_docx(file_bytes)
    elif filename.endswith(".pdf"):
        text = extract_text_from_pdf(file_bytes)
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
    """Build a numbered paragraph index from DOCX for precise matching.
    Includes both regular paragraphs and table cells (Arabic contracts often use tables)."""
    try:
        if filename.endswith('.docx') or filename.endswith('.doc'):
            doc = Document(io.BytesIO(file_bytes))
            from docx.oxml.ns import qn as _qn
            # Walk document body in XML order to preserve reading sequence
            items = []
            body = doc.element.body
            idx = 0
            for child in body.iter():
                tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                if tag == 'p':
                    text = "".join(r.text or '' for r in child.iter() if r.tag.split('}')[-1] == 't').strip()
                    if text:
                        items.append({"idx": idx, "text": text})
                        idx += 1
            return items
    except:
        pass
    return []

def anonymize_contract(text):
    """Anonymise PII dans le texte avant envoi à Claude. Retourne (texte_anonymisé, mapping)."""
    mapping = {}
    counters = {}

    def _ph(val, cat):
        for ph, orig in mapping.items():
            if orig.lower() == val.lower():
                return ph
        n = counters.get(cat, 0) + 1
        counters[cat] = n
        ph = f"[{cat}_{n}]"
        mapping[ph] = val
        return ph

    def _sub(cat):
        return lambda m: _ph(m.group(0), cat)

    result = text

    # Emails
    result = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', _sub('EMAIL'), result)

    # Téléphones marocains et français
    result = re.sub(
        r'\b(?:\+212|00212)?[\s.-]?(?:0[5-7]\d{8}|[5-7]\d{8})\b',
        _sub('TEL'), result
    )
    result = re.sub(r'\b(?:\+33|0033)[\s.-]?[67]\d{8}\b', _sub('TEL'), result)

    # IBAN
    result = re.sub(r'\b[A-Z]{2}\d{2}[A-Z0-9 ]{11,30}\b', _sub('IBAN'), result)

    # ICE Maroc (15 chiffres après mot-clé ICE)
    result = re.sub(
        r'(ICE\s*[:/]?\s*)(\d{15})\b',
        lambda m: m.group(1) + _ph(m.group(2), 'ICE'), result
    )

    # RC Maroc
    result = re.sub(
        r'\bR\.?C\.?\s*(?:N°|n°|No\.?|:)?\s*(\d{3,10})\b',
        lambda m: m.group(0)[:m.group(0).rindex(m.group(1))] + _ph(m.group(1), 'RC'), result
    )

    # SIRET / SIREN (seulement après le mot-clé)
    result = re.sub(
        r'(SIRET|SIREN)\s*[:/]?\s*(\d{9}(?:[\s]?\d{5})?)\b',
        lambda m: m.group(1) + ' ' + _ph(m.group(2).replace(' ', ''), 'SIRET'), result
    )

    # CIN marocain (seulement après le mot-clé)
    result = re.sub(
        r'(CIN\s*[:/]?\s*)([A-Z]{1,2}\d{5,6})\b',
        lambda m: m.group(1) + _ph(m.group(2), 'CIN'), result
    )

    # Noms de personnes après civilités
    result = re.sub(
        r'\b(M\.|Mme\.?|Monsieur|Madame|Mr\.?)\s+([A-ZÀ-Ÿ][a-zà-ÿ]+(?:(?:\s+|-)[A-ZÀ-Ÿ][A-ZÀ-Ÿa-zà-ÿ-]+){0,3})\b',
        lambda m: m.group(1) + ' ' + _ph(m.group(2), 'PERSONNE'), result
    )

    # Sociétés après forme juridique
    result = re.sub(
        r'\b(SARL|SA|SAS|SASU|EURL|SCI|GIE|SNC)\s+(?:dénommée?\s+)?"?([A-ZÀ-Ÿ؀-ۿ][A-Za-zÀ-ÿ؀-ۿ\s&,.-]{2,40}?)"?(?=\s*,|\s*\(|\s+dont|\s+au\s+capital|\s+inscrite|\s+immatriculée|\s*$)',
        lambda m: m.group(1) + ' ' + _ph(m.group(2).strip(), 'SOCIETE'), result
    )

    return result, mapping


def deanonymize_text(text, mapping):
    """Restitue les valeurs originales dans le texte anonymisé."""
    if not mapping or not text:
        return text
    for placeholder, original in mapping.items():
        text = text.replace(placeholder, original)
    return text


def analyze_contract(contract_text, lang, contract_type, api_key, partie="la partie bénéficiaire", file_bytes=None, filename="", progress_cb=None, director_email="", user_models_extra=None):
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("Clé API manquante")
    client = anthropic.Anthropic(api_key=api_key)

    # Build numbered paragraphs for precise matching
    paragraphs = build_numbered_paragraphs(file_bytes, filename) if file_bytes else []
    
    # Build numbered contract text for AI — 300 paragraphs / 40k chars for long contracts (Art. 19+)
    if paragraphs:
        numbered_text = "\n".join(("[P" + str(p["idx"]) + "] " + p["text"]) for p in paragraphs[:300])
    else:
        numbered_text = contract_text[:40000]

    # Anonymise PII avant envoi à Claude (emails, tél, IBAN, CIN, noms, sociétés)
    numbered_text, _anon_mapping = anonymize_contract(numbered_text)
    print(f"[ANON] {len(_anon_mapping)} valeurs anonymisées: {list(_anon_mapping.keys())}", flush=True)

    # ── Structured RAG: separate model docs (protection) from legal docs (conformite) ──
    if progress_cb: progress_cb("\U0001f4da Consultation de la base légale...")
    model_context = ""
    legal_context = ""
    _rag_contract_count = 0
    _rag_legal_count = 0
    _rag_debug_contract = []
    _rag_debug_legal = []
    LEGAL_CATS = {"loi", "law", "law_employment", "law_commercial", "law_civil",
                  "doctrine", "jurisprudence", "legal", "legislation"}
    # Quick jurisdiction detection for boosting relevant docs
    # Returns (jurisdiction_tag, is_foreign_unsupported)
    def _detect_jur(text):
        s = text[:3000].lower()
        _arabic_morocco = ["المملكة المغربية", "الظهير الملكي", "القانون المغربي",
                           "المحاكم المغربية", "المحاكم المختصة بالمملكة", "دفاتر الشروط الإدارية",
                           "ccag-t", "المغرب", "درهم مغربي", "الدراهم المغربية"]
        if any(k in text[:3000] for k in _arabic_morocco):
            return "droit_marocain", False
        if any(k in s for k in ["code du travail marocain", "dahir", "droit marocain", "royaume du maroc", "maroc"]):
            return "droit_marocain", False
        if any(k in s for k in ["droit français", "loi française", "france", "code civil français", "droit de la france"]):
            return "droit_francais", False
        # Detect foreign jurisdictions we have no RAG for — don't inject wrong-law docs
        _foreign_kw = [
            "estonian law", "droit estonien", "estonia", "estonie",
            "german law", "droit allemand", "deutsches recht",
            "english law", "droit anglais", "laws of england",
            "us law", "droit américain", "new york law", "delaware law",
            "dutch law", "droit néerlandais", "netherlands",
            "swiss law", "droit suisse", "switzerland",
            "belgian law", "droit belge", "belgique",
            "luxembourg law", "droit luxembourgeois",
            "spanish law", "droit espagnol",
            "italian law", "droit italien",
            "portuguese law", "droit portugais",
            "polish law", "droit polonais",
            "czech law", "droit tchèque",
            "hungarian law", "droit hongrois",
            "romanian law", "droit roumain",
            "bulgarian law", "droit bulgare",
            "greek law", "droit grec",
            "swedish law", "droit suédois",
            "danish law", "droit danois",
            "finnish law", "droit finlandais",
            "norwegian law", "droit norvégien",
        ]
        if any(k in s for k in _foreign_kw):
            return "foreign_unsupported", True
        return "universel", False
    _jurisdiction, _is_foreign_jurisdiction = _detect_jur(contract_text)
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

        # 4. Supplement: user-uploaded docs (non-ailovecontracts) via source/title ilike
        # Prevents protected-source dominance from burying user docs in hybrid BM25 ranking
        try:
            _protected_pfx = ["ailovecontracts", "lexisnexis", "dalloz", "lamy", "mernissi", "traite-de-droit"]
            _kw_terms = list(dict.fromkeys(
                w for w in re.findall(r'[a-zA-ZÀ-ÿ]{5,}', (contract_type + " " + contract_text[:300]).lower())
                if w not in {"cette","avec","dans","pour","les","des","une","par","sur","que","qui","leur","leurs","dont","mais","aussi","entre","comme","plus","sans","tout","tous","toute","toutes","selon","vers","sous"}
            ))[:4]
            _existing_ids = {d.get("id") for d in all_docs}
            _suppl_key = SUPA_SERVICE_KEY or SUPA_KEY
            _added = 0
            for _kw in _kw_terms:
                _sr = requests.get(
                    SUPA_URL + "/rest/v1/rag_documents",
                    headers={"apikey": _suppl_key, "Authorization": "Bearer " + _suppl_key},
                    params={"select": "id,title,content,source,category,party_label,jurisdiction",
                            "or": f"(source.ilike.*{_kw}*,title.ilike.*{_kw}*)",
                            "limit": "30"},
                    timeout=8
                )
                if _sr.ok:
                    for _d in (_sr.json() or []):
                        if _d.get("id") not in _existing_ids and not any(p in (_d.get("source","")).lower() for p in _protected_pfx):
                            all_docs.insert(0, _d)  # prepend — higher priority than protected sources
                            _existing_ids.add(_d.get("id"))
                            _added += 1
            if _added:
                print(f"Supplementary user-doc search: +{_added} non-protected docs added")
        except Exception as _se:
            print("Supplementary search error: " + str(_se))

        # Jurisdiction boost
        all_docs.sort(key=lambda d: 0 if (d.get("jurisdiction") or "universel") in (_jurisdiction, "universel", "auto") else 1)

        # Director priority boost: docs tagged with this director's org come first (2x priority)
        if director_email:
            _dir_prefix = "org:" + director_email + "§"
            _dir_docs   = [d for d in all_docs if (d.get("source") or "").startswith(_dir_prefix)]
            _other_docs = [d for d in all_docs if not (d.get("source") or "").startswith(_dir_prefix)]
            all_docs = _dir_docs + _other_docs
            if _dir_docs:
                print(f"Director RAG boost: {len(_dir_docs)} docs prioritized for {director_email}")

        # Separate contract models from legal references
        contract_docs = [d for d in all_docs if d.get("category", "").lower() not in LEGAL_CATS]
        legal_docs    = [d for d in all_docs if d.get("category", "").lower() in LEGAL_CATS]

        # Domain filter: keep only contract models relevant to this contract type
        # Docs tagged contract_{type} are domain-specific; docs tagged "contract" are generic (allowed for all)
        # This prevents NDA clauses from appearing in employment contracts, etc.
        _ct_lower = (contract_type or "").lower()
        _CT_DOMAINS = {
            "employment": "contract_employment",
            "cdi": "contract_employment", "cdd": "contract_employment",
            "nda": "contract_nda",
            "service": "contract_service",
            "purchase": "contract_purchase",
            "saas": "contract_saas",
            "partnership": "contract_partnership",
        }
        _domain_cat = _CT_DOMAINS.get(_ct_lower)
        if _domain_cat:
            def _domain_ok(doc):
                cat = (doc.get("category") or "").lower()
                # Accept: exact domain match, generic "contract", or untagged
                return cat in ("contract", _domain_cat) or not cat.startswith("contract_")
            _n_before_domain = len(contract_docs)
            contract_docs = [d for d in contract_docs if _domain_ok(d)]
            print(f"Domain filter ({_ct_lower}→{_domain_cat}): {_n_before_domain} → {len(contract_docs)} contract docs")

        # Filter legal docs for foreign/unsupported jurisdictions — avoid injecting wrong-law doctrine
        if _is_foreign_jurisdiction:
            _n_before_jur = len(legal_docs)
            legal_docs = []  # No legal RAG for this jurisdiction — let AI use its own knowledge
            print(f"Foreign jurisdiction detected: suppressed {_n_before_jur} legal docs to avoid wrong-law injection")

        # Filter by jurisdiction match for supported jurisdictions
        elif _jurisdiction not in ("universel", "auto"):
            _n_before_jur = len(legal_docs)
            legal_docs = [d for d in legal_docs
                          if (d.get("jurisdiction") or "universel") in (_jurisdiction, "universel", "auto", None)]
            if len(legal_docs) < _n_before_jur:
                print(f"Jurisdiction filter ({_jurisdiction}): {_n_before_jur} → {len(legal_docs)} legal docs")

        # Filter employment law docs for non-employment contracts
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

        protected_kw = ["lexisnexis", "dalloz", "lamy", "mernissi", "traite-de-droit", "pdf-free", "lexis",
                        "ailovecontracts"]  # modèles de clauses tierces — inspiration silencieuse, jamais citées

        # Build debug info — tracks which docs the model actually receives
        _rag_debug_contract = []
        _rag_debug_legal = []
        model_context = ""
        legal_context = ""

        # Context 0: Director's own cabinet models — always injected, highest priority, citeable
        if user_models_extra:
            model_context = "\n\n=== MODÈLES CABINET (RÉFÉRENCE PRIORITAIRE — TOUJOURS CITER) ===\n"
            for um in user_models_extra[:8]:
                title_um = um.get("filename", "Modele cabinet")
                content_um = str(um.get("content", ""))[:6000]
                if content_um.strip():
                    model_context += "\n=== " + title_um + " ===\n"
                    model_context += content_um + "\n"
                    model_context += "→ rag_source: " + title_um + "\n"
                    _rag_debug_contract.insert(0, {
                        "title": title_um,
                        "source": "cabinet-model",
                        "category": "modele",
                        "protected": False
                    })

        # Context 1: contract models → split between director policy (NIVEAU 1) and generic protective (NIVEAU 2)
        # Rule: admin_validated_clause = NIVEAU 1 for everyone
        #       modele/director_model = NIVEAU 1 only for the originating director's org; NIVEAU 2 for others
        if contract_docs:
            _dir_org_prefix = ("org:" + director_email + "§") if director_email else None
            def _is_director_model(doc):
                source = doc.get("source", "")
                cat = doc.get("category", "")
                if "validated_clause" in source:
                    return True  # admin_validated_clause → NIVEAU 1 for all
                if "cabinet-model" in source:
                    return True  # user_models injected as Context 0 → always NIVEAU 1
                if cat in ("modele", "director_model"):
                    # NIVEAU 1 only for the originating director's org
                    if _dir_org_prefix and source.startswith(_dir_org_prefix):
                        return True
                    if not source.startswith("org:"):
                        return True  # No org prefix (admin direct upload) → NIVEAU 1
                    return False  # Different director's model → NIVEAU 2 for this user
                return False
            validated = [d for d in contract_docs if _is_director_model(d)]
            reference = [d for d in contract_docs if not _is_director_model(d)]

            def _fmt_doc(doc, ctx):
                title_doc = doc.get("title", "") or doc.get("source", "modele")
                content_doc = str(doc.get("content", ""))[:1400]
                is_prot = any(p in (title_doc + doc.get("source", "")).lower() for p in protected_kw)
                arts = extract_article_refs(content_doc, title_doc)
                ctx += "\n--- " + title_doc + " ---\n"
                if arts:
                    ctx += "→ Articles cités: " + ", ".join(arts) + "\n"
                ctx += content_doc + "\n"
                ctx += "→ rag_source: " + ("null (protege)" if is_prot else title_doc) + "\n"
                if doc.get("party_label"):
                    ctx += "[PARTIE PROTEGEE: " + str(doc.get("party_label", "")) + "]\n"
                _rag_debug_contract.append({
                    "title": title_doc,
                    "source": doc.get("source", ""),
                    "category": doc.get("category", ""),
                    "protected": is_prot
                })
                return ctx

            if validated:
                model_context += "\n\n=== POLITIQUE JURIDIQUE DU DIRECTEUR (CLAUSES VALIDÉES) ===\n"
                for doc in validated[:8]:
                    model_context = _fmt_doc(doc, model_context)
            if reference:
                model_context += "\n\n=== CLAUSES PROTECTRICES DE RÉFÉRENCE ===\n"
                for doc in reference[:6]:
                    model_context = _fmt_doc(doc, model_context)

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
                _rag_debug_legal.append({
                    "title": title_doc,
                    "source": doc.get("source", ""),
                    "category": doc.get("category", "")
                })

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
        "client": "garantir la qualité et les délais de livraison, maintenir les pénalités contractuelles, faciliter la résiliation pour faute, protéger les données, maintenir le prix forfaitaire ferme",
        "maitre": "maintenir le prix forfaitaire et éviter toute révision au profit du prestataire, renforcer les pénalités de retard, faciliter la résiliation et le retrait du marché, imposer des garanties bancaires et assurances, protéger les délais de réception et la qualité des ouvrages",
        "commanditaire": "maintenir le prix forfaitaire et éviter toute révision au profit du prestataire, renforcer les pénalités de retard, faciliter la résiliation et le retrait du marché, imposer des garanties bancaires et assurances, protéger les délais de réception et la qualité des ouvrages",
        "acheteur": "garantir la conformité, maximiser les garanties, faciliter les recours",
        "vendeur": "garantir le paiement, limiter les garanties et responsabilités",
        "الطرف الأول": "الحفاظ على السعر الجزافي النهائي ومنع أي مطالبة بزيادة من الطرف الثاني، تعزيز غرامات التأخير، تسهيل سحب العمل وفسخ العقد، فرض ضمانات بنكية وتأمينات كافية، حماية آجال الاستلام وجودة الأشغال",
        "الطرف الثاني": "ضمان الأداء في الآجال المتفق عليها، تحديد نطاق الأشغال بدقة، تأمين حماية من القوة القاهرة، ضمان آليات تعديل السعر عند الاقتضاء",
    }
    # Extract role from partie label — check Arabic keys first, then French
    role_key = "employeur"
    partie_lower = partie.lower()
    for key in role_objectives:
        if key in partie_lower or key in partie:
            role_key = key
            break
    # Construction/BTP fallback: maître d'ouvrage keywords
    if role_key == "employeur":
        _btp_kw = ["maître", "maitre", "maitrise", "maîtrise", "btp", "construction",
                   "donneur", "commanditaire", "مكتب", "مالك", "صاحب", "الأول", "أول"]
        if any(k in partie_lower or k in partie for k in _btp_kw):
            role_key = "maitre"
    role_obj = role_objectives.get(role_key, "protéger ses intérêts")

    # Coalition detection: "A et B" means defend A and B together against third party
    _coalition_parties = [p.strip() for p in partie.split(" et ")] if " et " in partie else []
    _is_coalition = len(_coalition_parties) >= 2

    system = (
        "Tu es un avocat d'affaires senior avec 20 ans d'expérience en droit des contrats. Ta responsabilité professionnelle est engagée.\n"
        "MISSION CRITIQUE: Analyser EXHAUSTIVEMENT ce contrat. Tu n'as pas le droit à l'erreur — chaque clause désavantageuse non identifiée est une faute professionnelle.\n"
        "OBLIGATION D'EXHAUSTIVITÉ: Tu DOIS analyser CHAQUE clause du contrat, une par une. Ne saute AUCUN paragraphe.\n"
        "FAVORISER: " + partie + "\n\n"
        "RÈGLE FONDAMENTALE — UNILATÉRALITÉ STRICTE:\n"
        "Tu représentes EXCLUSIVEMENT " + partie + ". Toute modification doit AVANTAGER " + partie + " et seulement " + partie + ".\n"
        "INTERDIT ABSOLU: Ne propose JAMAIS une modification qui réduit les obligations de la CONTREPARTIE envers " + partie + ".\n"
        "- Une clause qui IMPOSE des obligations à la CONTREPARTIE est FAVORABLE pour " + partie + " → NE PAS l'affaiblir.\n"
        "- Une clause qui IMPOSE des obligations ou risques à " + partie + " est DÉFAVORABLE → la modifier en faveur de " + partie + ".\n"
        "EXEMPLES CONCRETS (contrat de construction, Partie 1 = maître d'ouvrage):\n"
        "  → Prix forfaitaire fixe imposé à l'entrepreneur = FAVORABLE pour Partie 1 → NE PAS proposer de révision de prix pour l'entrepreneur\n"
        "  → Pénalités de retard imposées à l'entrepreneur = FAVORABLE pour Partie 1 → NE PAS les atténuer ni créer des exceptions\n"
        "  → Délais stricts imposés à l'entrepreneur = FAVORABLE pour Partie 1 → NE PAS réduire la responsabilité de l'entrepreneur\n"
        "  → Garantie bancaire imposée à l'entrepreneur = FAVORABLE pour Partie 1 → NE PAS la supprimer ni la réduire\n"
        "Cette règle s'applique à TOUS les types de contrats dans les deux langues.\n\n"
        "RÈGLE ABSOLUE — NE JAMAIS MODIFIER LES VALEURS NUMÉRIQUES DU CONTRAT:\n"
        "Les chiffres, pourcentages, montants, délais et seuils déjà définis dans le contrat sont INTANGIBLES.\n"
        "INTERDIT ABSOLU de changer: taux de pénalité (ex: 3%/jour → garder 3%/jour), plafond de pénalité (ex: 10% → garder 10%), "
        "montants forfaitaires, délais en jours/mois, taux d'intérêt, seuils de déclenchement.\n"
        "Les modifications proposées doivent être de nature JURIDIQUE uniquement: ajouter des protections manquantes, "
        "clarifier des mécanismes contractuels ambigus, corriger des asymétries de droits, ajouter des recours légaux, "
        "renforcer des garanties, préciser des conditions de résiliation — jamais renégocier les valeurs chiffrées.\n"
        "EXEMPLE: pénalité de retard de 18 200 DH/jour → NE PAS proposer 20 000 DH/jour. Proposer à la place: "
        "préciser le mécanisme de déclenchement, ajouter une clause de mise en demeure, clarifier les cas d'exonération.\n\n"
        "RÈGLE ABSOLUE — RESPECTER LES CLAUSES D'EXCLUSION EXPLICITES:\n"
        "Si le contrat contient une formule d'exclusion absolue telle que 'en aucun cas' / 'بأي حال من الأحوال' / 'under no circumstances' / "
        "'quelles que soient les circonstances' — NE JAMAIS proposer d'exceptions, de dérogations ou de cas particuliers à cette exclusion.\n"
        "Ces formules sont des protections intentionnelles et maximales. Les affaiblir en ajoutant des exceptions (a), (b), (c)... "
        "constitue une modification DÉFAVORABLE pour le client.\n"
        "CORRECT: renforcer le mécanisme d'application, ajouter des sanctions en cas de violation, préciser la mise en demeure.\n"
        "INTERDIT: ajouter des exceptions qui contredisent la protection absolue expressément choisie par les parties.\n\n"
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
        " CRITERES AUTOMATIQUES DE RISQUE ELEVE (risk=high) :\n"
        "   → Résiliation UNILATÉRALE possible UNIQUEMENT par la contrepartie (asymétrie totale) = RISQUE ÉLEVÉ OBLIGATOIRE\n"
        "   → Restitution INTÉGRALE des avantages, subventions ou bénéfices fiscaux sans proratisation = RISQUE ÉLEVÉ\n"
        "   → Aucun droit de résiliation de " + partie + " pour manquement grave de la contrepartie ou changement législatif = RISQUE ÉLEVÉ\n"
        "   → Renonciation à indemnité ou dommages-intérêts en cas de résiliation abusive = RISQUE ÉLEVÉ\n"
        "   → Délais de remédiation trop courts (< 3 mois) pour des manquements opérationnels complexes = RISQUE ÉLEVÉ\n"
        "   → Obligation de remboursement > 0 = RISQUE CRITIQUE (risk=high)\n"
        " Si aucun article de resiliation n'existe = RISQUE CRITIQUE, propose une nouvelle clause.\n"
        " RÈGLE ABSOLUE — CONSÉQUENCES DE RÉSILIATION SÉPARÉES OBLIGATOIRES :\n"
        "   Si le contrat contient un article traitant des CONSÉQUENCES de la résiliation"
        " (ex: restitution intégrale des avantages/subventions/bénéfices fiscaux, remboursements, absence d'indemnité),"
        " tu DOIS créer UNE modification séparée portant UNIQUEMENT sur ces conséquences — PAS regroupée avec la durée ou la procédure.\n"
        "   Cette modification DOIT avoir risk=high si : restitution intégrale exigée, ou aucune indemnité pour " + partie + ", ou obligation de remboursement non proratisée.\n"
        "   INTERDICTION ABSOLUE : Ne jamais regrouper les conséquences de résiliation avec la clause de durée.\n"
        "  RESPONSABILITE/LIABILITY : Verifier systematiquement (1) plafonds de responsabilite, (2) exclusions de garantie, (3) indemnisation asymetrique. Si " + partie + " supporte une responsabilite illimitee ou superieure a celle de la partie adverse = RISQUE CRITIQUE.\n"
        "  REMBOURSEMENT/RESTITUTION : Verifier toute obligation de rembourser avances, subventions ou benefices fiscaux. Quantifier le montant max que " + partie + " pourrait devoir rembourser en cas de manquement ou resiliation.\n\n"
        "1. EXHAUSTIVITÉ TOTALE: Identifie TOUTES les clauses désavantageuses pour " + partie + " — même les clauses en apparence neutres\n"
        "2. CHECKLIST THÉMATIQUE OBLIGATOIRE — tu DOIS vérifier CHACUN de ces points, sans exception:\n"
        "   [PAIEMENT] Conditions de paiement, délais, intérêts de retard, retenues de garantie / retenues à la source — asymétrie ?\n"
        "   [DÉLAIS] Délais d'exécution, jalons, pénalités de retard — unilatéraux ? plafonnés ? mécanisme de prorogation ?\n"
        "   [RÉCEPTION] Réception provisoire, réception définitive, levée de réserves — droits et délais de " + partie + " ?\n"
        "   [GARANTIES] Garantie de parfait achèvement, décennale, vices cachés, garantie de fonctionnement — durée et portée ?\n"
        "   [TRAVAUX SUPPLÉMENTAIRES] Avenants, ordres de service, travaux hors contrat — qui décide ? rémunération ?\n"
        "   [SUSPENSION] Droits de suspension, prolongation de délai, indemnisation pendant suspension — prévu pour " + partie + " ?\n"
        "   [RÉSILIATION] Résiliation pour faute, pour convenance, pour insolvabilité — symétrique ? conséquences ?\n"
        "   [RESPONSABILITÉ] Plafond, exclusions, responsabilité solidaire, indemnisation — équilibré ?\n"
        "   [ASSURANCES] Assurances obligatoires (RC, décennale, tous risques chantier) — qui les souscrit ? preuve ?\n"
        "   [FORCE MAJEURE] Définition précise ? effets sur délais et prix ? obligation de notification ?\n"
        "   [SOUS-TRAITANCE] Droits de sous-traiter, agrément, responsabilité du sous-traitant — contrôle de " + partie + " ?\n"
        "   [RÉVISION DE PRIX] Clause de révision ou d'indexation — absente dans un contrat long = risque pour " + partie + " ?\n"
        "   [CONFIDENTIALITÉ] Données techniques, financières, commerciales — protégées après fin du contrat ?\n"
        "   [CESSION] Cession du contrat ou des droits à un tiers — accord requis ?\n"
        "   [RÈGLEMENT DES LITIGES] Médiation, arbitrage, juridiction compétente — favorable à " + partie + " ?\n"
        "   [DOCUMENTS CONTRACTUELS] Hiérarchie des pièces, conflits entre documents — favorable à " + partie + " ?\n"
        "   [NOTIFICATIONS] Formes et délais des mises en demeure et notifications — contraintes excessives ?\n"
        "   [PROPRIÉTÉ INTELLECTUELLE] Plans, études, brevets — qui en garde la propriété ?\n"
        "3. CLAUSES MANQUANTES OBLIGATOIRES: Identifie les protections RÉELLEMENT absentes du contrat — une clause est manquante uniquement si ni le contrat ni aucun document expressément annexé ou référencé ne la couvre. Si le contrat couvre un sujet par renvoi explicite à une annexe, un cahier des charges ou un devis, ce sujet N'EST PAS manquant. Propose AU MOINS 2 nouvelles clauses (type=nouvelle_clause), sans limite maximum — autant qu'il en faut pour protéger exhaustivement " + partie + ". Pour chaque clause manquante: (1) rédige-la complète dans proposed dans la même langue que le contrat, (2) numérote-la en suivant la numérotation existante, (3) indique insertion_after=para_idx du dernier article existant avant l'endroit logique d'insertion, (4) original=null.\n"
        "4. NIVEAU RÉDACTIONNEL: Style avocat d'affaires senior — précis, technique, sans ambiguïté\n"
        "   RÈGLE TYPOGRAPHIQUE ABSOLUE: INTERDIT d'utiliser des symboles mathématiques (+, -, ×, /) dans le texte juridique rédigé.\n"
        "   Écrire en toutes lettres: 'augmenté de' au lieu de '+', 'diminué de' au lieu de '-', 'ainsi que' au lieu de '+', etc.\n"
        "   En arabe: 'إضافة إلى' ou 'بالإضافة إلى' au lieu de '+', jamais '25/08/2026 + أيام' mais 'يوم 25/08/2026 مضافاً إليه عدد أيام'.\n"
        "5. RAG — RÈGLE DE CITATION STRICTE: Cite une source dans rag_source UNIQUEMENT si elle fonde DIRECTEMENT la modification proposée — c'est-à-dire si elle traite précisément du même aspect juridique que la clause originale modifiée."
        " INTERDIT: citer un article qui parle d'un aspect connexe mais différent (ex: Art.255 traite des conséquences du non-respect du préavis → NE PAS le citer pour une clause qui définit la durée du préavis)."
        " Si la source est seulement thématiquement proche mais ne fonde pas directement le proposed → rag_source: null."
        " NE JAMAIS inventer de source. NE JAMAIS citer LexisNexis/ouvrages payants. Sources marquées 'null (protege)' → rag_source: null.\n"
        "6. LÉGALITÉ: Toutes les modifications doivent respecter le droit applicable — jamais de clauses illégales\n\n"
        "PROCESSUS D'ANALYSE EN 6 ÉTAPES OBLIGATOIRES:\n"
        "Étape 1: Lis l'intégralité du contrat sans rien sauter\n"
        "Étape 2: Pour CHAQUE article numéroté, détermine: Favorable / Neutre / Défavorable à " + partie + " — note chaque article\n"
        "Étape 3: Pour chaque article Défavorable ou Neutre améliorable → crée une modification\n"
        "Étape 4: Parcours la CHECKLIST THÉMATIQUE ci-dessus point par point — pour chaque thème non couvert ou mal couvert → crée une modification ou une nouvelle clause\n"
        "Étape 5: Vérifie chaque modification contre le RAG pour citer les sources pertinentes\n"
        "Étape 6: VÉRIFICATION FINALE — relis ta liste de modifications et demande-toi: 'Ai-je laissé passer une clause qui désavantage " + partie + " ?' Si oui → ajoute-la maintenant\n\n"
        + get_legal_framework(contract_type) +
        "\n\n"
        + rag_context
        + legal_context
        + "\n\n=== HIÉRARCHIE DES SOURCES — RÈGLE D'UTILISATION ===\n"
        "NIVEAU 0 — MODÈLES CABINET (section MODÈLES CABINET ci-dessus) — PRIORITÉ ABSOLUE :\n"
        "   Pour chaque modification, vérifie D'ABORD si un modèle cabinet traite du même sujet.\n"
        "   Si oui : reprendre ses clauses, termes et chiffres EXACTS. Exemples obligatoires :\n"
        "   · Modèle prévoit préavis 12 mois → proposed dit 12 mois (pas 180 jours)\n"
        "   · Modèle prévoit CIRDI à Paris → proposed cite CIRDI à Paris (pas CNUDCI)\n"
        "   · Modèle prévoit indemnité 24 mois → proposed dit 24 mois\n"
        "   · Modèle prévoit renégociation 30j / résolution 90j → proposed reprend ces délais\n"
        "   INTERDIT d'inventer quand le modèle cabinet traite du même sujet.\n"
        "   Cite rag_source avec le nom du fichier modèle cabinet.\n\n"
        "NIVEAU 1 — POLITIQUE JURIDIQUE DU DIRECTEUR (section POLITIQUE JURIDIQUE DU DIRECTEUR ci-dessus) — AUTORITÉ ABSOLUE :\n"
        "   Ce sont les clauses officielles du cabinet. PROCESSUS OBLIGATOIRE pour chaque modification :\n"
        "   → Lis d'abord la section POLITIQUE JURIDIQUE DU DIRECTEUR.\n"
        "   → Si une clause y couvre le même sujet ET est favorable à " + partie + " :\n"
        "      (1) proposed = reprend ses termes EXACTS (délais, montants, mécanismes)\n"
        "      (2) rag_source = titre exact de cette clause du directeur\n"
        "      (3) INTERDIT de regarder NIVEAU 2 pour ce sujet — le directeur a tranché\n"
        "   → Si la clause du directeur est défavorable à " + partie + " → ignore-la, passe au NIVEAU 2.\n\n"
        "NIVEAU 2 — CLAUSES PROTECTRICES DE RÉFÉRENCE (section ci-dessus) — UNIQUEMENT SI NIVEAU 1 NE COUVRE PAS :\n"
        "   N'utiliser que si aucune clause du directeur ne traite du même sujet.\n"
        "   Reprends leur logique — sans obligation de reproduire mot pour mot.\n\n"
        "NIVEAU 3 — RÉFÉRENCES JURIDIQUES (lois / doctrine / jurisprudence) — CONFORMITÉ :\n"
        "   Sert à argumenter les risques dans reason. Cite les articles pertinents.\n"
        "   N'impose pas la rédaction du proposed.\n\n"
        "IMPORTANT: Le contrat est numéroté [P0], [P1], etc.\n\n"
        "Retourne UNIQUEMENT du JSON valide, sans markdown:\n"
        '{"modifications":[{"id":1,"para_idx":32,"clause_name":"nom court",'
        '"risk":"high|medium|low",'
        '"reason":"Pourquoi cette clause désavantage ' + partie + ' et comment la modification la protège",'
        '"type":"modification|nouvelle_clause",'
        '"original":"texte EXACT du paragraphe ou null pour nouvelle_clause",'
        '"proposed":"clause reformulée favorisant ' + partie + '",'
        '"insertion_after":"para_idx après lequel insérer ou null si modification",'
        '"rag_source":"titre EXACT de la source RAG du contexte, ou null si absente/protégée"}]}\n\n'
        "Règles:\n"
        "- MINIMUM 10 modifications obligatoires — un juriste qui en trouve moins de 10 n'a pas analysé exhaustivement\n"
        "- OBLIGATION SUPPLÉMENTAIRE : Si le contrat a un article 'Résiliation' avec des CONSÉQUENCES (restitution, remboursement, pas d'indemnité) → cet article doit être une modification SÉPARÉE avec risk=high\n"
        "- para_idx: numéro entier du paragraphe\n"
        "- original: copie EXACTE sans modification\n"
        "- RÈGLE UN ORIGINAL = UNE MODIFICATION: Chaque texte original ne peut apparaître QUE DANS UNE SEULE modification."
        " Si tu identifies plusieurs problèmes dans la même clause (même original), CONSOLIDE-LES tous dans un seul proposed."
        " Si tu veux traiter un aspect supplémentaire de la même clause → type=nouvelle_clause avec original=null et insertion_after=para_idx de cette clause.\n"
        "- RÈGLE ABSOLUE COHÉRENCE SUJET: Pour type=modification, le proposed DOIT traiter du MÊME sujet juridique que l'original."
        " INTERDIT: prendre une clause de préavis/démission et proposer à la place une clause de restitution, confidentialité, responsabilité ou tout autre sujet différent."
        " Si tu veux ajouter un sujet nouveau non couvert par l'original → type=nouvelle_clause avec original=null."
        " TEST OBLIGATOIRE: Lis l'original. Lis le proposed. Traitent-ils du même sujet ? Si non → nouvelle_clause.\n"
        "- RÈGLE CRITIQUE — original pour nouvelle_clause: TOUJOURS null (ou chaîne vide). JAMAIS de texte de contrat dans original pour une nouvelle_clause."
        " Si tu copies un texte de contrat dans original d'une nouvelle_clause, tu crées un décalage en cascade qui corrompt TOUTES les modifications suivantes — elles utilisent toutes le mauvais texte original.\n"
        "- RÈGLE CRITIQUE — CORRESPONDANCE reason/original: Le [P_N] cité dans reason DOIT correspondre exactement au texte dans original."
        " Ex: si reason dit 'La clause [P43]…' alors original = texte verbatim du paragraphe 43. INTERDIT d'utiliser le texte d'un paragraphe différent.\n"
        "- VALIDATION FINALE OBLIGATOIRE: Avant d'écrire le JSON, lis chaque entrée et vérifie: (1) pour nouvelle_clause → original est null/vide, (2) pour modification → original = paragraphe cité dans reason, (3) proposed traite du MÊME sujet que original.\n"
        "- proposed: clause juridique complète et professionnelle, rédigée en style contractuel soutenu\n"
        "- proposed: utilise le vocabulaire juridique approprié (nonobstant, en ce compris, à titre de, ci-après, sous réserve de...)\n"
        "- proposed: structure avec sujet + verbe + objet + conditions + exceptions si nécessaire\n"
        "- proposed: max 120 mots, mais suffisamment détaillé pour être opérationnel sans ambiguïté\n"
        "- proposed: jamais de blancs ou placeholders comme ___ ou [à compléter]\n"
        "- proposed: rédige comme un avocat d'affaires senior rédigeant pour un client exigeant\n"
        "- Vérifie chaque proposed: est-ce que ça avantage bien " + partie + " ? Si non, reformule."
    )

    # Limit text — 40k chars ensures late articles (Art. 19+) are included
    truncated_text = numbered_text[:40000]
    if progress_cb: progress_cb("\U0001f916 Démarrage de l\'analyse IA...")
    raw = ""
    _clause_buf = ""
    _law_buf = ""
    import re as _re
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=16000,
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
            originals_raw = re.findall(r'"original"\s*:\s*(null|"(?:[^"\\]|\\.)*")', raw)
            originals = [None if o == 'null' else o[1:-1] for o in originals_raw]
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
                    "original": originals[i] if i < len(originals) else None,
                    "proposed": proposeds[i] if i < len(proposeds) else "",
                    "insertion_after": int(insertions[i]) if i < len(insertions) and insertions[i] != 'null' else None,
                    "rag_source": rag_sources[i] if i < len(rag_sources) and rag_sources[i] else None
                })

        if mods:
            result = {"modifications": mods}
        else:
            raise ValueError("Impossible d'extraire les modifications")

    # Enforce: nouvelle_clause must have original=null (Claude sometimes copies paragraph
    # text into original for nouvelle_clause, which cascades and shifts all originals after it)
    mods = result.get("modifications", [])
    for mod in mods:
        if mod.get("type") == "nouvelle_clause" and mod.get("original"):
            mod["original"] = None

    # Restitue les valeurs PII dans les champs texte des modifications
    if _anon_mapping:
        for mod in mods:
            for field in ("original", "proposed", "reason", "clause_name"):
                if mod.get(field):
                    mod[field] = deanonymize_text(mod[field], _anon_mapping)

    # Add confidence score based on RAG usage
    rag_backed = sum(1 for m in mods if m.get("rag_source"))
    result["_rag_coverage"] = str(rag_backed) + "/" + str(len(mods)) + " modifications basées sur le RAG"
    result["_paragraphs"] = paragraphs
    # Debug: what the RAG pipeline actually found and sent to the model
    _citeable = [d["title"] for d in _rag_debug_contract if not d["protected"]]
    _protected = [d["title"] for d in _rag_debug_contract if d["protected"]]
    result["_rag_debug"] = {
        "contract_docs_in_context": _rag_debug_contract,
        "legal_docs_in_context": _rag_debug_legal,
        "citeable_sources": _citeable,
        "protected_sources_silenced": _protected,
        "total_found": _rag_contract_count + _rag_legal_count
    }
    return result

def fuzzy_match(original, para_text, threshold=0.60):
    """Check if original text roughly matches para_text (Latin + Arabic)"""
    original_lower = original.lower().strip()
    para_lower = para_text.lower().strip()
    if original_lower in para_lower:
        return True
    # Extract meaningful words — Latin and Arabic scripts
    _word_re = r"[؀-ۿ]{2,}|[a-zA-ZÀ-ÿ]{3,}"
    orig_words_set = set(re.findall(_word_re, original_lower))
    para_words_set = set(re.findall(_word_re, para_lower))
    if len(orig_words_set) < 3:
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

    # Walk full XML body (includes table cells) — same index as build_numbered_paragraphs
    # Use raw XML elements + iter-based text to handle <w:sdt>, hyperlinks, table cells, etc.
    _WNS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    _wt_tag = _WNS + 't'
    _wr_tag = _WNS + 'r'

    def _p_text(elem):
        return "".join(t.text or '' for t in elem.iter(_wt_tag)).strip()

    _body = doc.element.body
    paragraphs = []  # raw lxml <w:p> elements
    for _child in _body.iter():
        _ctag = _child.tag.split('}')[-1] if '}' in _child.tag else _child.tag
        if _ctag == 'p' and _p_text(_child):
            paragraphs.append(_child)

    for mod in accepted:
        mod_id = mod.get("id")
        proposed = mod.get("proposed", "").strip()
        if not proposed:
            continue

        para = None

        # Method 1: Use para_idx if available (precise), with length sanity check.
        # In Arabic table contracts, Claude sometimes assigns para_idx to a short
        # header cell (e.g. "الطرف الثاني") instead of the adjacent clause cell.
        # If the candidate is much shorter than original, fall through to fuzzy match.
        original = mod.get("original", "").strip()
        para_idx = mod.get("para_idx")
        if para_idx is not None and para_idx < len(paragraphs):
            candidate = paragraphs[para_idx]
            cand_text = _p_text(candidate)
            if cand_text:
                if original and len(original) > 60 and len(cand_text) < len(original) * 0.25:
                    pass  # candidate is likely a header cell; fall through to Method 2
                else:
                    para = candidate

        # Method 2: Fuzzy match fallback
        if para is None:
            for _pe in paragraphs:
                _pt = _p_text(_pe)
                if _pt and fuzzy_match(original, _pt):
                    para = _pe
                    break

        # Handle new clauses (type=nouvelle_clause) — insert as new paragraph
        if mod.get('type') == 'nouvelle_clause':
            insertion_after = mod.get('insertion_after')
            insert_para = None
            MIN_INSERT_IDX = 5

            if insertion_after is not None:
                safe_idx = max(int(insertion_after), MIN_INSERT_IDX)
                if safe_idx < len(paragraphs):
                    insert_para = paragraphs[safe_idx]

            if insert_para is None:
                for _pe in reversed(paragraphs):
                    _pt = _p_text(_pe)
                    if _pt and len(_pt) > 10:
                        insert_para = _pe
                        break

            if insert_para is not None:
                _direct_runs = [r for r in insert_para if r.tag == _wr_tag]
                ref_rpr = _direct_runs[0].find(qn('w:rPr')) if _direct_runs else None

                new_p = OxmlElement('w:p')

                _ppr = insert_para.find(qn('w:pPr'))
                if _ppr is not None:
                    import copy
                    new_p.append(copy.deepcopy(_ppr))

                ins_elem = OxmlElement('w:ins')
                ins_elem.set(qn('w:id'), str(rev_id))
                ins_elem.set(qn('w:author'), author)
                ins_elem.set(qn('w:date'), date)
                rev_id += 1

                new_r = OxmlElement('w:r')
                if ref_rpr is not None:
                    import copy
                    new_r.append(copy.deepcopy(ref_rpr))
                new_t = OxmlElement('w:t')
                new_t.text = proposed
                new_t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                new_r.append(new_t)
                ins_elem.append(new_r)
                new_p.append(ins_elem)

                next_sib = insert_para.getnext()
                if next_sib is not None:
                    insert_para.getparent().insert(
                        list(insert_para.getparent()).index(next_sib),
                        new_p
                    )
                else:
                    insert_para.getparent().append(new_p)
                applied.add(mod_id)
                print(f"Inserted new clause '{mod.get('clause_name')}' after para {insertion_after}")
            else:
                print(f"Could not find insertion point for new clause: {mod.get('clause_name')}")
            continue

        if para is None:
            print(f"Could not find paragraph for mod {mod_id}: {mod.get('clause_name')}")
            continue

        para_text = _p_text(para)

        # Clear text in all <w:t> descendants of direct <w:r> children
        for _r in list(para):
            if _r.tag == _wr_tag:
                for _t in _r.iter(_wt_tag):
                    _t.text = ''
        p = para

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


@app.route("/admin/users", methods=["GET", "OPTIONS"])
def admin_list_users():
    if request.method == "OPTIONS": return "", 204
    try:
        caller_email = request.args.get("caller_email", "").strip()
        if not caller_email:
            return jsonify({"error": "caller_email requis"}), 400
        rows = supa_get("user_accounts", {"email": f"eq.{caller_email}", "limit": "1"})
        if not rows or not (rows[0].get("is_admin") or rows[0].get("role") == "admin"):
            return jsonify({"error": "Acces reserve aux administrateurs"}), 403
        users = supa_get("user_accounts", {
            "select": "id,email,role,is_admin,parent_email,created_at,payment_status,analyses_remaining,subscription_end,nb_juristes_max",
            "order": "created_at.desc",
            "limit": "1000"
        })
        return jsonify({"users": users or []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/sync-payments", methods=["POST", "OPTIONS"])
def admin_sync_payments():
    if request.method == "OPTIONS": return "", 204
    try:
        data = request.get_json() or {}
        caller_email = data.get("caller_email", "").strip()
        if not caller_email:
            return jsonify({"error": "caller_email requis"}), 400
        rows = supa_get("user_accounts", {"email": f"eq.{caller_email}", "limit": "1"})
        if not rows or not (rows[0].get("is_admin") or rows[0].get("role") == "admin"):
            return jsonify({"error": "Acces reserve aux administrateurs"}), 403
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        expired_users = supa_get("user_accounts", {
            "payment_status": "eq.active",
            "subscription_end": f"lt.{now}",
            "select": "email"
        }) or []
        updated = []
        for u in expired_users:
            supa_patch("user_accounts", {"payment_status": "expired"}, f"email=eq.{u['email']}&payment_status=eq.active")
            updated.append(u["email"])
        return jsonify({"status": "ok", "created_or_updated": updated, "message": f"{len(updated)} abonnement(s) expire(s) mis a jour."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/activate-user", methods=["POST", "OPTIONS"])
def admin_activate_user():
    if request.method == "OPTIONS": return "", 204
    try:
        data = request.get_json() or {}
        caller_email = data.get("caller_email", "").strip()
        target_email = data.get("target_email", "").strip()
        role = data.get("role", "directeur")
        analyses_remaining = int(data.get("analyses_remaining", 20))
        nb_juristes_max = int(data.get("nb_juristes_max", 5))
        if not caller_email or not target_email:
            return jsonify({"error": "caller_email et target_email requis"}), 400
        caller_rows = supa_get("user_accounts", {"email": f"eq.{caller_email}", "limit": "1"})
        if not caller_rows or not (caller_rows[0].get("is_admin") or caller_rows[0].get("role") == "admin"):
            return jsonify({"error": "Acces reserve aux administrateurs"}), 403
        sub_end = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
        existing = supa_get("user_accounts", {"email": f"eq.{target_email}", "limit": "1"})
        if existing:
            supa_patch("user_accounts", {
                "payment_status": "active",
                "analyses_remaining": analyses_remaining,
                "subscription_end": sub_end,
                "role": role,
                "nb_juristes_max": nb_juristes_max
            }, f"email=eq.{target_email}")
            return jsonify({"status": "ok", "message": f"Abonnement active pour {target_email} (30 jours)."})
        else:
            supa_insert("user_accounts", {
                "email": target_email,
                "role": role,
                "payment_status": "active",
                "analyses_remaining": analyses_remaining,
                "subscription_end": sub_end,
                "nb_juristes_max": nb_juristes_max
            })
            return jsonify({"status": "ok", "message": f"Compte cree et active pour {target_email}."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/analyses/save-director/<analysis_id>", methods=["POST", "OPTIONS"])
def save_director_analysis(analysis_id):
    if request.method == "OPTIONS": return "", 204
    try:
        data = request.get_json() or {}
        modifications = data.get("modifications", [])
        director_notes = (data.get("director_notes") or "").strip()
        patch = {
            "modifications": modifications,
            "director_notes": director_notes,
            "status": "director_review"
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
        # Arabic-script Moroccan keywords (case-insensitive irrelevant for Arabic)
        _arabic_morocco = ["المملكة المغربية", "الظهير الملكي", "القانون المغربي",
                           "المحاكم المغربية", "المحاكم المختصة بالمملكة", "دفاتر الشروط الإدارية",
                           "ccag-t", "المغرب", "درهم مغربي", "الدراهم المغربية"]
        if any(k in contract_text[:3000] for k in _arabic_morocco):
            return jsonify({"jurisdiction": "droit_marocain"})
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
            rows = supa_get("user_accounts", {"email": f"eq.{user_email}", "select": "analyses_remaining,is_admin,parent_email,role", "limit": "1"})
            remaining = 9999
            _dir_email_stream = ""
            if rows:
                acc = rows[0]
                remaining = 9999 if acc.get("is_admin") else (acc.get("analyses_remaining") or 0)
                _parent_s = (acc.get("parent_email") or "").strip()
                _dir_email_stream = _parent_s if _parent_s else (user_email if (acc.get("role") or "") == "directeur" else "")
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
            # Fetch director's personal cabinet models (used as priority RAG context)
            _user_models_extra = []
            _models_email = _dir_email_stream or user_email
            if _models_email:
                try:
                    _sk = SUPA_SERVICE_KEY or SUPA_KEY
                    _um_r = requests.get(
                        SUPA_URL + "/rest/v1/user_models",
                        headers={"apikey": _sk, "Authorization": "Bearer " + _sk},
                        params={"user_email": f"eq.{_models_email}",
                                "select": "id,filename,content",
                                "content": "not.is.null",
                                "limit": "10"},
                        timeout=5
                    )
                    if _um_r.ok:
                        _user_models_extra = [m for m in (_um_r.json() or [])
                                              if m.get("content") and len(m.get("content","").strip()) > 50]
                        if _user_models_extra:
                            print(f"Cabinet models injected: {len(_user_models_extra)} for {_models_email}")
                except Exception as _ume:
                    print("user_models fetch error: " + str(_ume))
            result = analyze_contract(contract_text, lang, contract_type, api_key, partie,
                                      file_bytes=file_bytes, filename=filename, progress_cb=_cb,
                                      director_email=_dir_email_stream,
                                      user_models_extra=_user_models_extra or None)
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
        rows = supa_get("user_accounts", {"email": f"eq.{user_email}", "select": "analyses_remaining,is_admin,parent_email,role", "limit": "1"})
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
            director_email = user_email  # new user = directeur by default
        else:
            acc = rows[0]
            if acc.get("is_admin"):
                remaining = 9999  # admin = unlimited
            else:
                remaining = acc.get("analyses_remaining", 0) or 0
            # Director email: juriste → parent_email; directeur → own email
            _parent = (acc.get("parent_email") or "").strip()
            director_email = _parent if _parent else (user_email if (acc.get("role") or "") == "directeur" else "")

        if remaining <= 0:
            return jsonify({"error": "Quota d'analyses épuisé. Veuillez renouveler votre abonnement."}), 403

        if not file:
            return jsonify({"error": "Fichier manquant"}), 400
        contract_text, file_bytes, filename = read_file(file)
        if not contract_text or len(contract_text.strip()) < 50:
            return jsonify({"error": "Fichier vide ou illisible"}), 400
        # Fetch director's personal cabinet models (priority RAG context)
        _user_models_extra = []
        _models_email_a = director_email or user_email
        if _models_email_a:
            try:
                _sk_a = SUPA_SERVICE_KEY or SUPA_KEY
                _um_a = requests.get(
                    SUPA_URL + "/rest/v1/user_models",
                    headers={"apikey": _sk_a, "Authorization": "Bearer " + _sk_a},
                    params={"user_email": f"eq.{_models_email_a}",
                            "select": "id,filename,content",
                            "limit": "10"},
                    timeout=5
                )
                if _um_a.ok:
                    _user_models_extra = [m for m in (_um_a.json() or [])
                                          if m.get("content") and len(m.get("content", "").strip()) > 50]
                    if _user_models_extra:
                        print(f"[/analyze] Cabinet models injected: {len(_user_models_extra)} for {_models_email_a}")
            except Exception as _ume_a:
                print("user_models fetch error (analyze): " + str(_ume_a))
        result = analyze_contract(contract_text, lang, contract_type, api_key, partie, file_bytes, filename,
                                  director_email=director_email,
                                  user_models_extra=_user_models_extra or None)

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
            model="claude-sonnet-4-6",
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

@app.route("/export-translation", methods=["POST", "OPTIONS"])
def export_translation():
    if request.method == "OPTIONS": return "", 204
    try:
        data          = request.get_json() or {}
        contract_text = (data.get("contract_text") or "").strip()
        target_lang   = (data.get("target_lang") or "en").strip()
        filename      = (data.get("filename") or "contrat").strip()
        mods          = data.get("modifications") or []
        decisions     = data.get("decisions") or {}

        if not contract_text or len(contract_text) < 20:
            return jsonify({"error": "contract_text manquant ou trop court"}), 400

        # Split contract into sections — prefer [Px] markers (always present after analysis),
        # then blank-line splitting. If that yields ≤3 sections (table DOCX where rows are
        # separated by single \n only), fall back to line-by-line splitting.
        if re.search(r'\[P\d+\]', contract_text):
            sections = [s.strip() for s in re.split(r'\[P\d+\]\s*', contract_text) if s.strip()]
        else:
            sections = [s.strip() for s in re.split(r'\n{2,}', contract_text) if s.strip()]
        if len(sections) <= 3:
            sections = [l.strip() for l in contract_text.split('\n') if l.strip()]

        def _strip_px(t):
            """Remove [Px] paragraph markers Claude includes in original copies."""
            return re.sub(r'^\[P\d+\]\s*', '', t.strip())

        def _match_score(query, section):
            """Word-overlap ratio between query and section (works for any script)."""
            q_words = set(w for w in query.split() if len(w) >= 3)
            s_words = set(w for w in section.split() if len(w) >= 3)
            if len(q_words) < 3:
                return 0.0
            return len(q_words & s_words) / len(q_words)

        # Apply non-rejected modifications using fuzzy section matching
        modified_sections = list(sections)
        section_mods = {}  # section_idx -> {'original': str, 'proposed': str}
        applied_count = 0
        for i, mod in enumerate(mods):
            mod_id = str(mod.get("id") if mod.get("id") is not None else i)
            decision = decisions.get(mod_id, "proposed")
            if decision == "rejected":
                continue
            orig = _strip_px((mod.get("original") or "").strip())
            prop = (mod.get("proposed") or "").strip()
            if not orig or not prop:
                continue
            matched_idx = None
            best_score = 0.0
            for j, sec in enumerate(modified_sections):
                if j in section_mods:
                    continue
                if orig in sec or sec in orig:
                    matched_idx = j
                    break
                score = _match_score(orig, sec)
                if score > best_score and score >= 0.40:
                    best_score = score
                    matched_idx = j
            if matched_idx is not None:
                section_mods[matched_idx] = {'original': sections[matched_idx], 'proposed': prop}
                modified_sections[matched_idx] = prop
                applied_count += 1

        lang_label = {"en": "English", "fr": "Français", "ar": "العربية"}.get(target_lang, target_lang)
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        client = anthropic.Anthropic(api_key=api_key)

        # Ask Claude to translate section by section using numbered markers
        numbered_orig = "\n\n".join(f"[§{i+1}]\n{s}" for i, s in enumerate(modified_sections[:150]))
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=32000,
            messages=[{
                "role": "user",
                "content": (
                    f"Translate each numbered section [§N] into {lang_label}. "
                    "Output ONLY the translated sections in the exact same numbered format — "
                    "no commentary, no preamble, no extra text.\n\n"
                    "FORMAT:\n[§1]\ntranslation of section 1\n\n[§2]\ntranslation of section 2\n\n...\n\n"
                    "CONTRACT TO TRANSLATE:\n" + numbered_orig
                )
            }]
        )
        translated_raw = response.content[0].text

        # Parse translated sections
        trans_map = {}
        for m in re.finditer(r'\[§(\d+)\]\n(.*?)(?=\n\[§|\Z)', translated_raw, re.DOTALL):
            trans_map[int(m.group(1))] = m.group(2).strip()

        # Build side-by-side DOCX with a two-column table
        from docx import Document as _Doc
        from docx.shared import Pt, Cm, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml.ns import qn as _qn
        from docx.oxml import OxmlElement as _OE

        doc = _Doc()
        section = doc.sections[0]
        section.page_width  = Cm(29.7)  # A4 landscape
        section.page_height = Cm(21.0)
        section.left_margin = section.right_margin = Cm(1.5)
        section.top_margin  = section.bottom_margin = Cm(1.5)

        style = doc.styles['Normal']
        style.font.name = 'Calibri'
        style.font.size = Pt(10)

        left_col_label = "CONTRAT MODIFIÉ" if applied_count > 0 else "ORIGINAL"
        title = doc.add_heading(f"{filename}  —  {left_col_label} | {lang_label}", level=1)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER

        if applied_count > 0:
            info_p = doc.add_paragraph(f"Modifications appliquées : {applied_count} / {len(mods)}")
            info_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            info_p.runs[0].bold = True
            info_p.runs[0].font.color.rgb = RGBColor(0x1F, 0x38, 0x64)

        # Header row
        tbl = doc.add_table(rows=1, cols=2)
        tbl.style = 'Table Grid'
        hdr = tbl.rows[0].cells
        for cell, txt, color in [(hdr[0], left_col_label, "1F3864"), (hdr[1], lang_label.upper(), "1F3864")]:
            cell.text = txt
            run = cell.paragraphs[0].runs[0]
            run.bold = True
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            tc = cell._tc
            tcPr = tc.get_or_add_tcPr()
            shd = _OE('w:shd')
            shd.set(_qn('w:fill'), color)
            shd.set(_qn('w:color'), 'auto')
            shd.set(_qn('w:val'), 'clear')
            tcPr.append(shd)

        # Content rows — show strikethrough+green markup for modified sections
        for i, sec in enumerate(modified_sections[:150]):
            if not sec.strip() or len(sec.strip()) < 5:
                continue  # skip empty/trivial sections (no table row)
            trans = trans_map.get(i + 1, "")
            is_arabic = any(0x0600 <= ord(c) <= 0x06FF for c in sec[:50])
            row = tbl.add_row()
            left_cell = row.cells[0]

            if i in section_mods:
                mod_info = section_mods[i]
                left_cell.text = ""
                lp = left_cell.paragraphs[0]
                run_del = lp.add_run(mod_info['original'])
                run_del.font.strike = True
                run_del.font.color.rgb = RGBColor(0x99, 0x99, 0x99)
                lp.add_run("\n")
                run_ins = lp.add_run(mod_info['proposed'])
                run_ins.font.color.rgb = RGBColor(0x00, 0x7F, 0x00)
                run_ins.bold = True
            else:
                left_cell.text = sec

            lp = left_cell.paragraphs[0]
            if is_arabic:
                lp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                pPr = lp._p.get_or_add_pPr()
                bidi = _OE('w:bidi')
                pPr.append(bidi)
            else:
                lp.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

            row.cells[1].text = trans
            row.cells[1].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        dl_name = f"{filename}_translation_{target_lang}.docx"
        return send_file(
            buf,
            as_attachment=True,
            download_name=dl_name,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
    except Exception as e:
        print(f"[/export-translation] error: {e}")
        return jsonify({"error": str(e)}), 500

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

        # Resolve director_email from submitted_by → parent_email (for RAG priority tagging)
        _submitted_by = contract.get("submitted_by", "")
        _dir_email = ""
        if _submitted_by:
            _u = supa_get("user_accounts", {"email": f"eq.{_submitted_by}", "select": "parent_email,role", "limit": "1"})
            if _u:
                _dir_email = (_u[0].get("parent_email") or "").strip()
                if not _dir_email and (_u[0].get("role") or "") == "directeur":
                    _dir_email = _submitted_by
        _source_prefix = ("org:" + _dir_email + "§") if _dir_email else ""

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
                "source": _source_prefix + title_base,
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
                "source": _source_prefix + "admin_validated_clause",
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
        jurisdiction = request.form.get("jurisdiction", "").strip() or None
        title_base = title  # Use as source key
        api_key = os.environ.get("ANTHROPIC_API_KEY") or request.form.get("api_key", "")
        if not file:
            return jsonify({"error": "Fichier manquant"}), 400

        file_bytes = file.read()
        filename = file.filename.lower()
        if filename.endswith(".docx") or filename.endswith(".doc"):
            content = extract_text_from_docx(file_bytes)
        elif filename.endswith(".pdf"):
            content = extract_text_from_pdf(file_bytes)
        else:
            content = file_bytes.decode("utf-8", errors="ignore")

        if not content or len(content.strip()) < 50:
            return jsonify({"error": "Document vide ou illisible"}), 400

        # Limit content size for large documents
        if len(content) > 200000:
            content = content[:200000]

        # Smart chunking: split at article/clause boundaries first, fallback to 400-word chunks
        def _split_into_clauses(text, max_chunks=500):
            import re as _re
            # Strip table-of-contents lines (e.g. "Article 2 ........ 8") before splitting
            toc_line = _re.compile(r'(?m)^[^\n]*\.{5,}[^\n]*\d+\s*$\n?')
            text_clean = toc_line.sub('', text).strip()
            # If stripping TOC removed too much content, use original
            if len(text_clean) < 200:
                text_clean = text

            # Split at article/clause headers that are followed by real content
            article_pat = _re.compile(
                r'(?=\n\s*(?:Article|Art\.?|ARTICLE|§|Section|Clause|CLAUSE)\s+\d+[\.\-\s])',
                _re.IGNORECASE
            )
            parts = article_pat.split(text_clean)
            # Filter out TOC-like fragments: must have real content (no chunk that's just a reference line)
            parts = [p.strip() for p in parts
                     if p.strip() and len(p.strip()) > 80
                     and not _re.match(r'^(Article|Art\.?|§)\s+\d+[^\n]{0,5}$', p.strip(), _re.IGNORECASE)]
            if len(parts) >= 3:
                merged = []
                buf = ""
                for p in parts:
                    buf = (buf + "\n" + p).strip() if buf else p
                    if len(buf) >= 100:
                        merged.append(buf)
                        buf = ""
                if buf:
                    merged.append(buf)
                result = []
                for part in merged[:max_chunks]:
                    if len(part) <= 1200:
                        result.append(part)
                    else:
                        words_p = part.split()
                        for j in range(0, len(words_p), 200):
                            result.append(" ".join(words_p[j:j+200]))
                            if len(result) >= max_chunks:
                                break
                    if len(result) >= max_chunks:
                        break
                if result:
                    return result
            # Fallback: 400-word chunks
            words = text_clean.split()
            chunk_size = 400
            return [" ".join(words[i:i+chunk_size])
                    for i in range(0, min(len(words), chunk_size * max_chunks), chunk_size)]

        chunks = _split_into_clauses(content)

        import uuid
        voyage_key = os.environ.get("VOYAGE_API_KEY") or request.form.get("voyage_key", "")

        # Upsert: delete existing docs with same source name to avoid duplicates
        deleted = delete_rag_by_source(title)
        if deleted:
            print(f"rag_upload: supprimé {deleted} ancien(s) chunk(s) pour source='{title}'")

        for i, chunk in enumerate(chunks):
            embedding = get_embedding(chunk, voyage_key)
            chunk_title = (title + " (partie " + str(i+1) + ")") if len(chunks) > 1 else title
            save_rag_doc({
                "id": str(uuid.uuid4()),
                "title": chunk_title,
                "category": category,
                "content": chunk,
                "embedding": embedding,  # raw list — save_rag_doc handles JSON + pgvector
                "source": title,
                "jurisdiction": jurisdiction,
                "validated_at": datetime.datetime.now().isoformat()
            })

        total = load_rag()
        return jsonify({"success": True, "chunks": len(chunks), "source": title,
                        "replaced": deleted, "total_docs": len(total["documents"])})

    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500

@app.route("/models/upload", methods=["POST", "OPTIONS"])
def models_upload():
    """Upload a personal cabinet model for a director — no admin validation needed.
    Stored in user_models with full text content; injected as priority RAG context
    into every analysis run by that director."""
    if request.method == "OPTIONS": return "", 204
    try:
        user_email = request.form.get("user_email", "").strip()
        user_id = request.form.get("user_id", "").strip() or None
        file = request.files.get("file")
        if not file or not user_email:
            return jsonify({"error": "Fichier et user_email requis"}), 400
        file_bytes = file.read()
        fname = file.filename.lower()
        if fname.endswith(".docx") or fname.endswith(".doc"):
            content = extract_text_from_docx(file_bytes)
        elif fname.endswith(".pdf"):
            content = extract_text_from_pdf(file_bytes)
        else:
            content = file_bytes.decode("utf-8", errors="ignore")
        content = (content or "").replace("\x00", "")
        if len(content.strip()) < 20:
            return jsonify({"error": "Document vide ou illisible"}), 400
        # Delete any existing record with same user_email + filename — use service key to bypass RLS
        _sk = SUPA_SERVICE_KEY or SUPA_KEY
        try:
            requests.delete(
                SUPA_URL + "/rest/v1/user_models",
                headers={"apikey": _sk, "Authorization": "Bearer " + _sk, "Content-Type": "application/json"},
                params={"user_email": f"eq.{user_email}", "filename": f"eq.{file.filename}"},
                timeout=10
            )
        except Exception:
            pass
        doc_id = str(uuid.uuid4())
        row = {"id": doc_id, "user_email": user_email, "filename": file.filename,
               "content": content, "category": "modele", "is_public": False}
        if user_id:
            row["user_id"] = user_id
        supa_insert("user_models", row)
        print(f"models_upload: {file.filename} ({len(content)} chars) → user_models for {user_email}")
        return jsonify({"success": True, "id": doc_id, "filename": file.filename, "chars": len(content)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

        # 3. Count docs in RAG using Prefer: count=exact (bypasses 1000-row limit)
        try:
            key = SUPA_SERVICE_KEY or SUPA_KEY
            cnt_r = requests.get(
                SUPA_URL + "/rest/v1/rag_documents",
                headers={"apikey": key, "Authorization": "Bearer " + key,
                         "Prefer": "count=exact"},
                params={"select": "id,source,category,jurisdiction,embedding_vector",
                        "limit": "1"},
                timeout=15
            )
            total_count = int(cnt_r.headers.get("content-range", "0/0").split("/")[-1] or 0)
            diag["total_docs"] = total_count
            diag["total_docs_note"] = "count exact via Prefer:count=exact"
            # Sample 1000 for category/missing-vector stats
            all_docs = supa_get("rag_documents", {"select": "id,source,category,jurisdiction,embedding_vector", "limit": "1000"})
            with_vec = [d for d in (all_docs or []) if d.get("embedding_vector")]
            diag["sample_size"] = len(all_docs or [])
            diag["docs_with_embedding_vector_in_sample"] = len(with_vec)
            from collections import Counter
            diag["categories"] = dict(Counter(d.get("category","?") for d in (all_docs or [])))
            diag["jurisdictions"] = dict(Counter(d.get("jurisdiction") or "null" for d in (all_docs or [])))
            diag["sample_sources"] = list(set(d.get("source","?") for d in (all_docs or [])))[:10]
        except Exception as e:
            diag["doc_count_error"] = str(e)

        # 4. Test pgvector search
        diag["pgvector_test"] = "skipped"
        if voyage_key:
            try:
                vec = get_embedding("contrat de travail CDI licenciement préavis Maroc", voyage_key)
                if vec and len(vec) == 1024:
                    results = search_rag_pgvector(vec, top_k=5)
                    results = results if isinstance(results, list) else []
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
                "select": "id,source,category,party_label,jurisdiction",
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
                    "jurisdiction": doc.get("jurisdiction",""),
                    "warning": False
                }
            grouped[src]["chunks"] += 1

        for src, d in grouped.items():
            if d["chunks"] < 5:
                d["warning"] = True
                d["warning_msg"] = "Trop peu de chunks"

        result = sorted(grouped.values(), key=lambda x: (x.get("type",""), x.get("source","")))

        # Optional keyword filter: /rag/list?q=investissement
        q = (request.args.get("q") or "").strip().lower()
        if q:
            result = [d for d in result if q in d.get("source","").lower()]

        return jsonify({
            "documents": result,
            "total": sum(d["chunks"] for d in result),
            "total_docs": len(result)
        })
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500


@app.route("/rag/find", methods=["GET"])
def rag_find():
    """Search RAG documents by keyword in source/title. GET /rag/find?q=investissement"""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "Paramètre q requis"}), 400
    try:
        # Use Supabase server-side ilike filter — bypasses the 1000-row client limit
        key = SUPA_SERVICE_KEY or SUPA_KEY
        headers = {"apikey": key, "Authorization": "Bearer " + key}
        r = requests.get(
            SUPA_URL + "/rest/v1/rag_documents",
            headers=headers,
            params={
                "select": "id,title,source,category,jurisdiction",
                "or": f"(source.ilike.*{q}*,title.ilike.*{q}*)",
                "limit": "2000"
            },
            timeout=15
        )
        docs = r.json() if r.ok else []
        sources = {}
        for d in (docs or []):
            src = d.get("source","?")
            sources[src] = sources.get(src, 0) + 1
        return jsonify({
            "query": q,
            "matching_chunks": len(docs or []),
            "sources": [{"source": s, "chunks": n} for s, n in sorted(sources.items())]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/rag/retag", methods=["POST", "OPTIONS"])
def rag_retag():
    """Update the category of all RAG documents with a given source name."""
    if request.method == "OPTIONS":
        return "", 204
    try:
        body = request.get_json(force=True) or {}
        source = body.get("source", "").strip()
        new_category = body.get("category", "").strip()
        new_jurisdiction = body.get("jurisdiction", "").strip()
        if not source or (not new_category and not new_jurisdiction):
            return jsonify({"error": "source et au moins category ou jurisdiction requis"}), 400
        docs = supa_get("rag_documents", {"select": "id", "source": f"eq.{source}", "limit": "500"})
        if not docs:
            return jsonify({"error": "Aucun document trouvé pour cette source"}), 404
        updates = {}
        if new_category: updates["category"] = new_category
        if new_jurisdiction: updates["jurisdiction"] = new_jurisdiction
        updated = 0
        for doc in docs:
            supa_patch("rag_documents", updates, f"id=eq.{doc['id']}")
            updated += 1
        return jsonify({"success": True, "updated": updated, "source": source, **updates})
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500


@app.route("/rag/reindex", methods=["POST", "OPTIONS"])
def rag_reindex():
    """Re-embed all RAG docs that are missing embedding_vector (pgvector column).
    Called after the rag_upload bug-fix to backfill existing documents."""
    if request.method == "OPTIONS":
        return "", 204
    try:
        voyage_key = os.environ.get("VOYAGE_API_KEY", "")
        if not voyage_key:
            return jsonify({"error": "VOYAGE_API_KEY manquante — impossible de ré-indexer"}), 400

        # Fetch docs without embedding_vector, in batches
        fixed = 0
        skipped = 0
        errors = 0
        offset = 0
        batch_size = 100
        while True:
            # Fetch docs missing embedding_vector
            docs = supa_get("rag_documents", {
                "select": "id,content,embedding",
                "embedding_vector": "is.null",
                "limit": str(batch_size),
                "offset": str(offset)
            })
            if not docs:
                break
            for doc in docs:
                try:
                    content = doc.get("content") or ""
                    if not content.strip():
                        skipped += 1
                        continue
                    # Re-compute embedding
                    emb = get_embedding(content[:1000], voyage_key)
                    if not emb or len(emb) != 1024:
                        skipped += 1
                        continue
                    # Update embedding_vector and embedding JSON
                    vec_str = "[" + ",".join(str(x) for x in emb) + "]"
                    patch_r = supa_patch("rag_documents",
                                         {"embedding_vector": vec_str, "embedding": json.dumps(emb)},
                                         "id=eq." + doc["id"])
                    if patch_r.ok or patch_r.status_code == 204:
                        fixed += 1
                    else:
                        print(f"reindex PATCH failed {patch_r.status_code}: {patch_r.text[:300]}")
                        errors += 1
                except Exception as de:
                    print("reindex doc error: " + str(de))
                    errors += 1
            if len(docs) < batch_size:
                break
            offset += batch_size

        return jsonify({
            "success": True,
            "fixed": fixed,
            "skipped": skipped,
            "errors": errors,
            "message": f"{fixed} documents ré-indexés avec embedding_vector pgvector"
        })
    except Exception as e:
        return jsonify({"error": _anthropic_error_msg(e) or str(e)}), 500


@app.route("/rag/reindex-all", methods=["POST", "OPTIONS"])
def rag_reindex_all():
    """Force re-embed RAG docs in paginated batches. Use ?offset=0&limit=200 per call."""
    if request.method == "OPTIONS":
        return "", 204
    try:
        voyage_key = os.environ.get("VOYAGE_API_KEY", "")
        if not voyage_key:
            return jsonify({"error": "VOYAGE_API_KEY manquante"}), 400
        # Paginated: process only `limit` docs starting at `offset`
        offset = int(request.args.get("offset", 0))
        limit = min(int(request.args.get("limit", 200)), 300)
        fixed = skipped = errors = 0
        docs = supa_get("rag_documents", {
            "select": "id,content",
            "limit": str(limit),
            "offset": str(offset)
        })
        for doc in (docs or []):
            try:
                content = (doc.get("content") or "").strip()
                if not content:
                    skipped += 1
                    continue
                emb = get_embedding(content[:1000], voyage_key)
                if not emb or len(emb) != 1024:
                    skipped += 1
                    continue
                vec_str = "[" + ",".join(str(x) for x in emb) + "]"
                patch_r = supa_patch("rag_documents",
                                    {"embedding_vector": vec_str, "embedding": json.dumps(emb)},
                                    "id=eq." + doc["id"])
                if patch_r.ok or patch_r.status_code == 204:
                    fixed += 1
                else:
                    errors += 1
            except Exception as de:
                errors += 1
        next_offset = offset + limit
        return jsonify({
            "success": True, "fixed": fixed, "skipped": skipped, "errors": errors,
            "offset": offset, "limit": limit,
            "next_call": f"?offset={next_offset}&limit={limit}",
            "done": len(docs or []) < limit
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        user_email_chat = (data.get("user_email") or "").strip()

        if not message:
            return jsonify({"error": "message requis"}), 400

        # Chat quota — 15 messages per user (requires chat_remaining column in user_accounts)
        _chat_is_admin = False
        _chat_remaining = None
        if user_email_chat:
            try:
                _chat_acc = supa_get("user_accounts", {"email": f"eq.{user_email_chat}", "select": "is_admin,chat_remaining", "limit": "1"})
                if _chat_acc:
                    _chat_is_admin = bool(_chat_acc[0].get("is_admin"))
                    if not _chat_is_admin:
                        _chat_remaining = _chat_acc[0].get("chat_remaining")
                        if _chat_remaining is None:
                            supa_patch("user_accounts", {"chat_remaining": 15}, f"email=eq.{user_email_chat}")
                            _chat_remaining = 15
                        if _chat_remaining <= 0:
                            return jsonify({"error": "Quota de messages épuisé (0/15). Veuillez renouveler votre abonnement.", "chat_remaining": 0}), 403
            except Exception as _qe:
                print(f"[/chat] quota check error: {_qe}")

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

        # Build modifications summary — toutes les modifications (proposées ET acceptées)
        # Le statut de chaque modification est indiqué pour que le chatbot sache où en est la révision
        mods_summary = ""
        if modifications:
            lines = []
            for m in modifications[:20]:
                cname    = m.get('clause_name') or '?'
                original = (m.get('original') or '').strip()
                proposed = (m.get('proposed') or '').strip()
                mid      = str(m.get('id') or '')
                statut   = decisions.get(mid, 'en attente')
                lines.append(f"### {cname} [{statut}]\nORIGINAL: {original[:600]}\nPROPOSÉ: {proposed[:600]}")
            mods_summary = "\nCLAUSES DU CONTRAT (modifications proposées par l'analyse):\n" + "\n\n".join(lines)

        # Full contract sent every time — prompt caching makes it cheap after 1st call
        contract_excerpt = contract_text[:80000] if contract_text else ""

        # Search RAG for legal context relevant to the user's question
        _legal_rag_ctx = ""
        try:
            _vkey = os.environ.get("VOYAGE_API_KEY")
            _qemb = get_embedding(message[:500], voyage_key=_vkey)
            _rdocs = search_rag_hybrid(message[:300], _qemb, top_k=15)
            if not _rdocs:
                _rdocs = search_rag_pgvector(_qemb, top_k=15)
            if not _rdocs:
                _rdocs = search_rag_keyword(message, top_k=15)
            if _rdocs:
                _legal_rag_ctx = "\n\n=== BASE LÉGALE ET DOCUMENTAIRE (extraits pertinents) ===\n"
                for _rd in _rdocs[:10]:
                    _rt = _rd.get("title") or _rd.get("source") or "Document"
                    _rc = str(_rd.get("content", ""))[:3000]
                    if _rc.strip():
                        _legal_rag_ctx += f"\n[{_rt}]\n{_rc}\n"
                _legal_rag_ctx += "\n=== FIN BASE LÉGALE ===\n"
                _legal_rag_ctx += "Cite précisément ces sources légales quand tu t'y réfères dans ta réponse.\n"
                print(f"[/chat] RAG: {len(_rdocs)} docs for: {message[:60]}")
        except Exception as _re:
            print(f"[/chat] RAG error: {_re}")

        # System prompt
        system_prompt = (
            "Tu es un assistant juridique expert en droit des contrats. "
            "Tu aides un avocat à analyser et améliorer un contrat. "
            "Réponds toujours en français, de manière professionnelle.\n"
            "RÈGLE ABSOLUE SUR LES SOURCES LÉGALES : tu ne dois JAMAIS prétendre avoir accès "
            "à une loi ou un document juridique sauf si son contenu figure EXPLICITEMENT dans la section "
            "BASE LÉGALE ET DOCUMENTAIRE ci-dessous. Si cette section est absente ou ne contient pas "
            "la loi demandée, réponds : 'Cette loi ne figure pas dans ma base documentaire pour cette session. "
            "Voici ce que je sais de mémoire générale :' — puis réponds depuis tes connaissances générales "
            "en précisant que ce n'est pas un extrait de la base. "
            "INTERDIT : inventer une liste de lois 'disponibles', citer des articles sans les avoir dans le contexte.\n"
            "CAPACITÉ TRADUCTION WORD : si l'utilisateur demande une traduction du contrat en Word/DOCX "
            "(en anglais, français, arabe, etc.), ou confirme vouloir générer le fichier, "
            "réponds avec UNE SEULE phrase de confirmation puis place IMMÉDIATEMENT le marqueur — "
            "INTERDIT de demander 'voulez-vous que je génère ?' ou toute confirmation supplémentaire. "
            "Marqueur à placer seul sur la dernière ligne : "
            "[EXPORT_TRANSLATION:en] pour anglais, [EXPORT_TRANSLATION:fr] pour français, "
            "[EXPORT_TRANSLATION:ar] pour arabe. "
            "Ce marqueur déclenchera automatiquement la génération et le téléchargement du fichier Word.\n"
            + (f"Partie représentée : {partie}. Tu défends UNIQUEMENT les intérêts de cette partie.\n" if partie else "")
            + (f"Juridiction : {jurisdiction}.\n" if jurisdiction and jurisdiction != "universel" else "")
            + _legal_rag_ctx
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
• "proposed" = texte COMPLET et rédigé intégralement — incorpore TOUTES les améliorations dans une seule rédaction
• NE JAMAIS produire une liste de bullets à la place du bloc — les bullets sont INTERDITS comme réponse finale
• Si tu proposes 5 améliorations sur une clause → rédige UNE clause complète qui intègre les 5 améliorations
• Plusieurs clauses DIFFÉRENTES discutées = plusieurs blocs séparés (un par clause)
• SEULE exception : question strictement théorique sans référence à une clause du contrat présent

ANTI-PATTERN INTERDIT :
❌ "Voici mes recommandations :
   - Allonger les délais à 6 mois
   - Ajouter un droit de résiliation
   - ..."
→ C'est INTERDIT. Ces bullets doivent être transformées en rédaction intégrée dans le bloc <modification>.

PATTERN CORRECT :
✓ "J'ai analysé l'Article X. [courte explication textuelle]"
<modification>
{"clause_name":"Article X – Titre","original":"texte original exact","proposed":"Article X refondu intégrant TOUTES les améliorations : [texte complet rédigé]"}
</modification>

EXEMPLE :
Utilisateur : "Peux-tu revoir la clause de confidentialité ?"
Ta réponse :
J'ai analysé l'Article 15.1. Je propose une rédaction renforcée qui : allonge la durée, précise les sanctions et ajoute un droit de remédiation.
<modification>
{"clause_name":"Article 15.1 – Confidentialité","original":"Les parties s'engagent à garder confidentielles toutes les informations échangées.","proposed":"Les parties s'engagent mutuellement et irrévocablement à maintenir strictement confidentielles toutes informations, documents et données échangés dans le cadre du présent accord, pour une durée de cinq (5) ans suivant son expiration. En cas de divulgation non autorisée, la partie fautive dispose d'un délai de trente (30) jours pour remédier au manquement avant que des dommages et intérêts ne soient exigibles."}
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
        # Decrement chat quota and return remaining count
        if user_email_chat and _chat_remaining is not None and not _chat_is_admin:
            try:
                new_remaining = max(0, _chat_remaining - 1)
                supa_patch("user_accounts", {"chat_remaining": new_remaining}, f"email=eq.{user_email_chat}")
                result["chat_remaining"] = new_remaining
            except Exception:
                pass
        elif _chat_is_admin:
            result["chat_remaining"] = 9999
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
