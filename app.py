from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import anthropic
import json
import os
import io
import re
import zipfile
import datetime
import numpy as np
import voyageai
from supabase import create_client
from docx import Document
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

app = Flask(__name__)
CORS(app)

# ── Supabase client ──────────────────────────────────────
def get_supabase():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        raise ValueError("SUPABASE_URL et SUPABASE_KEY requis")
    return create_client(url, key)

# ── RAG: Supabase storage ─────────────────────────────────
def load_rag():
    try:
        sb = get_supabase()
        result = sb.table("rag_documents").select("*").execute()
        return {"documents": result.data or []}
    except Exception as e:
        print(f"load_rag error: {e}")
        return {"documents": []}

def save_rag_doc(doc):
    try:
        sb = get_supabase()
        sb.table("rag_documents").upsert(doc).execute()
    except Exception as e:
        print(f"save_rag_doc error: {e}")

def delete_rag_by_source(source):
    try:
        sb = get_supabase()
        import re as _re
        # Get all docs and filter by source title
        result = sb.table("rag_documents").select("id, title").execute()
        ids = [d["id"] for d in (result.data or []) 
               if _re.sub(r" \(partie \d+\)$", "", d.get("title", "")) == source]
        for doc_id in ids:
            sb.table("rag_documents").delete().eq("id", doc_id).execute()
        return len(ids)
    except Exception as e:
        print(f"delete_rag error: {e}")
        return 0

def cosine_similarity(a, b):
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))

def get_embedding(text, voyage_key=None):
    # Try Voyage AI for semantic embeddings
    if voyage_key:
        try:
            vo = voyageai.Client(api_key=voyage_key)
            result = vo.embed([text[:2000]], model="voyage-law-2", input_type="document")
            return result.embeddings[0]
        except:
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
        if partie and doc.get("party_label", "").lower() and partie.lower() in doc.get("party_label", "").lower():
            score *= 1.3
        # Boost validated clauses
        if "validated_clause" in doc.get("source", ""):
            score *= 1.2
        scored.append((score, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in scored[:top_k]]

# ── Text extraction ───────────────────────────────────────
def extract_text_from_docx(file_bytes):
    try:
        doc = Document(io.BytesIO(file_bytes))
        text = []
        for para in doc.paragraphs:
            if para.text.strip():
                text.append(para.text)
        return "\n".join(text)
    except Exception:
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                if 'word/document.xml' in z.namelist():
                    doc_xml = z.read('word/document.xml').decode('utf-8', errors='ignore')
                    text = re.sub(r'<[^>]+>', ' ', doc_xml)
                    return re.sub(r'\s+', ' ', text).strip()
        except Exception as e2:
            raise ValueError(f"Impossible de lire le fichier Word: {str(e2)}")

def read_file(file):
    file_bytes = file.read()
    filename = file.filename.lower()
    if filename.endswith(".docx") or filename.endswith(".doc"):
        return extract_text_from_docx(file_bytes), file_bytes, filename
    else:
        return file_bytes.decode("utf-8", errors="ignore"), file_bytes, filename

# ── AI functions ──────────────────────────────────────────
# … Toutes tes fonctions pour identify_parties, analyze_contract, apply_track_changes, etc. …
# … Routes /identify-parties, /analyze, /export, /queue/*, /rag/* …
# … Comme dans ton code original partagé précédemment …

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
