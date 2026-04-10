"""
Script d'upload de documents RAG avec juridiction OBLIGATOIRE.

Usage:
  python upload_rag.py --jurisdiction droit_marocain --folder C:/Omniscient/Maroc/
  python upload_rag.py --jurisdiction droit_francais --folder C:/Omniscient/France/
  python upload_rag.py --jurisdiction droit_marocain --file C:/Omniscient/CDI_type.docx
  python upload_rag.py --jurisdiction universel      --file C:/Omniscient/RGPD_general.pdf

Juridictions valides:
  droit_marocain  droit_francais  droit_algerien  droit_tunisien  universel

Formats supportés: .pdf .docx .txt .md

Variables d'environnement requises:
  SUPABASE_URL, SUPABASE_SERVICE_KEY (ou SUPABASE_KEY)
  VOYAGE_API_KEY ou OPENAI_API_KEY (pour les embeddings)
"""

import os, sys, json, hashlib, requests
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "") or os.environ.get("SUPABASE_KEY", "")
VOYAGE_KEY   = os.environ.get("VOYAGE_API_KEY", "")
OPENAI_KEY   = os.environ.get("OPENAI_API_KEY", "")
BACKEND_URL  = os.environ.get("BACKEND_URL", "")   # ex: https://ton-app.railway.app

VALID_JURISDICTIONS = ["droit_marocain", "droit_francais", "droit_algerien", "droit_tunisien", "universel"]
SUPPORTED_EXT = {".pdf", ".docx", ".txt", ".md"}
CHUNK_SIZE = 1500   # caractères par chunk

headers_sb = {
    "apikey": SUPABASE_KEY,
    "Authorization": "Bearer " + SUPABASE_KEY,
    "Content-Type": "application/json"
}

# ── Embeddings ───────────────────────────────────────────────────────────────
def get_embedding(text):
    """Génère un embedding via Voyage AI (préféré) ou OpenAI."""
    if VOYAGE_KEY:
        r = requests.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {VOYAGE_KEY}", "Content-Type": "application/json"},
            json={"model": "voyage-large-2", "input": [text[:4000]]}
        )
        if r.ok:
            return r.json()["data"][0]["embedding"]
        print(f"  Voyage error: {r.text[:100]}")
    if OPENAI_KEY:
        r = requests.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
            json={"model": "text-embedding-3-small", "input": text[:8000]}
        )
        if r.ok:
            return r.json()["data"][0]["embedding"]
        print(f"  OpenAI error: {r.text[:100]}")
    return None

# ── Extraction texte ─────────────────────────────────────────────────────────
def extract_text(filepath):
    ext = Path(filepath).suffix.lower()
    if ext == ".txt" or ext == ".md":
        return open(filepath, encoding="utf-8", errors="ignore").read()
    elif ext == ".docx":
        try:
            import docx
            doc = docx.Document(filepath)
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except ImportError:
            print("  pip install python-docx requis pour les .docx")
            return None
    elif ext == ".pdf":
        try:
            import pdfplumber
            with pdfplumber.open(filepath) as pdf:
                return "\n".join(p.extract_text() or "" for p in pdf.pages)
        except ImportError:
            try:
                import PyPDF2
                with open(filepath, "rb") as f:
                    reader = PyPDF2.PdfReader(f)
                    return "\n".join(p.extract_text() or "" for p in reader.pages)
            except ImportError:
                print("  pip install pdfplumber ou PyPDF2 requis pour les .pdf")
                return None
    return None

# ── Chunking ─────────────────────────────────────────────────────────────────
def chunk_text(text, chunk_size=CHUNK_SIZE, overlap=200):
    """Découpe le texte en chunks avec chevauchement."""
    chunks, start = [], 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start += chunk_size - overlap
    return chunks

# ── Upload vers Supabase ──────────────────────────────────────────────────────
def doc_exists(title, jurisdiction):
    """Vérifie si un doc avec ce titre et cette juridiction existe déjà."""
    r = requests.get(
        SUPABASE_URL + "/rest/v1/rag_documents",
        headers=headers_sb,
        params={"select": "id", "title": f"eq.{title}", "jurisdiction": f"eq.{jurisdiction}", "limit": "1"}
    )
    return bool(r.ok and r.json())

def upload_doc(title, content, jurisdiction, category="contrat", chunk_index=0, total_chunks=1):
    embedding = get_embedding(content)
    doc = {
        "title": title,
        "content": content,
        "source": f"upload_manual/{Path(title).stem}",
        "jurisdiction": jurisdiction,
        "category": category,
        "embedding": json.dumps(embedding) if embedding else None,
    }
    r = requests.post(
        SUPABASE_URL + "/rest/v1/rag_documents",
        headers={**headers_sb, "Prefer": "return=minimal"},
        json=doc
    )
    return r.status_code in (200, 201)

# ── Logique principale ────────────────────────────────────────────────────────
def process_file(filepath, jurisdiction, dry_run=False):
    filepath = Path(filepath)
    if filepath.suffix.lower() not in SUPPORTED_EXT:
        print(f"  Ignoré (format non supporté): {filepath.name}")
        return 0, 0

    print(f"\n  Traitement: {filepath.name}")
    text = extract_text(filepath)
    if not text or len(text.strip()) < 50:
        print(f"  Ignoré (texte vide ou trop court)")
        return 0, 0

    chunks = chunk_text(text)
    title_base = filepath.stem.replace("_", " ").replace("-", " ")
    ok, skip = 0, 0

    for i, chunk in enumerate(chunks):
        title = title_base if len(chunks) == 1 else f"{title_base} (partie {i+1}/{len(chunks)})"
        if doc_exists(title, jurisdiction):
            print(f"    [SKIP] '{title}' existe déjà")
            skip += 1
            continue
        if dry_run:
            print(f"    [DRY] Uploadrait: '{title}' ({len(chunk)}c)")
            ok += 1
        else:
            success = upload_doc(title, chunk, jurisdiction, chunk_index=i, total_chunks=len(chunks))
            if success:
                ok += 1
                emb_status = "✓ embed" if VOYAGE_KEY or OPENAI_KEY else "⚠ sans embed"
                print(f"    [OK] '{title}' ({emb_status})")
            else:
                print(f"    [ECHEC] '{title}'")

    return ok, skip

def main():
    args = sys.argv[1:]
    if not args or "--help" in args or "-h" in args:
        print(__doc__)
        return

    jurisdiction, folder, file_path, dry_run = None, None, None, False

    i = 0
    while i < len(args):
        if args[i] == "--jurisdiction" and i+1 < len(args):
            jurisdiction = args[i+1]; i += 2
        elif args[i] == "--folder" and i+1 < len(args):
            folder = args[i+1]; i += 2
        elif args[i] == "--file" and i+1 < len(args):
            file_path = args[i+1]; i += 2
        elif args[i] == "--dry-run":
            dry_run = True; i += 1
        else:
            i += 1

    if not jurisdiction:
        print("ERREUR: --jurisdiction requis")
        print(f"Valides: {', '.join(VALID_JURISDICTIONS)}")
        sys.exit(1)
    if jurisdiction not in VALID_JURISDICTIONS:
        print(f"Juridiction invalide: '{jurisdiction}'")
        print(f"Valides: {', '.join(VALID_JURISDICTIONS)}")
        sys.exit(1)
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERREUR: SUPABASE_URL + SUPABASE_SERVICE_KEY requis")
        sys.exit(1)

    print(f"=== UPLOAD RAG ===")
    print(f"Juridiction : {jurisdiction}")
    print(f"Embeddings  : {'Voyage AI' if VOYAGE_KEY else 'OpenAI' if OPENAI_KEY else 'AUCUN (texte seul)'}")
    print(f"Mode        : {'DRY RUN' if dry_run else 'UPLOAD RÉEL'}\n")

    files = []
    if file_path:
        files = [Path(file_path)]
    elif folder:
        files = [f for f in Path(folder).rglob("*") if f.suffix.lower() in SUPPORTED_EXT]
        print(f"Fichiers trouvés dans {folder}: {len(files)}")

    if not files:
        print("Aucun fichier à traiter.")
        return

    total_ok, total_skip = 0, 0
    for f in files:
        ok, skip = process_file(f, jurisdiction, dry_run=dry_run)
        total_ok += ok
        total_skip += skip

    print(f"\n=== RÉSUMÉ ===")
    print(f"Uploadés  : {total_ok}")
    print(f"Ignorés   : {total_skip} (déjà existants)")
    print(f"Total     : {len(files)} fichiers traités")

if __name__ == "__main__":
    main()
