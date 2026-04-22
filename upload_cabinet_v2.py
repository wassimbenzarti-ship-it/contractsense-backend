"""
Script pour uploader les documents dans Omniscient RAG
Suit exactement la structure de dossiers de C:\Omniscient\contrats

Structure detectee automatiquement:
  contrats/
    04_Contrats/
      MA_Droit_Marocain/     -> jurisdiction=maroc
        Droit_Commercial/    -> category=droit_commercial
        Droit_des_Societes/
          SA/                -> category=societes_SA
          SARL/              -> category=societes_SARL
          SAS/               -> category=societes_SAS
        Droit_du_Travail/    -> category=droit_travail
      FR_Droit_Francais/     -> jurisdiction=france
        ...

Usage:
  python upload_cabinet.py <dossier> <type>
  python upload_cabinet.py <dossier> <type> --overwrite
  python upload_cabinet.py <dossier> <type> --jurisdiction droit_marocain
"""

import requests
import sys
import os
import time
import json
import unicodedata

BACKEND = "https://web-production-f96f7.up.railway.app"
PAUSE = 3
SUPPORTED = ['.pdf', '.docx', '.doc', '.txt']

# Juridictions valides
VALID_JURISDICTIONS = [
    "droit_marocain", "droit_francais", "droit_algerien",
    "droit_tunisien", "droit_anglais", "universel"
]

# Detection juridiction par nom de dossier
JURISDICTION_KEYWORDS = {
    'droit_marocain': ['ma_', '_ma_', 'marocain', 'maroc'],
    'droit_francais': ['fr_', '_fr_', 'francais', 'français', 'france', 'francis', 'lefebre', 'lefebvre'],
    'universel':      ['international', 'ohada', 'anglais', 'english'],
}

# Detection categorie par nom de dossier
CATEGORY_KEYWORDS = {
    'droit_commercial': ['commercial', 'commerce'],
    'droit_travail':    ['travail', 'social'],
    'societes_SA':      ['/sa/', '\\sa\\'],
    'societes_SARL':    ['sarl'],
    'societes_SAS':     ['sas'],
    'droit_societes':   ['societ', 'société'],
}


def _readable(text, sample=2000):
    """Retourne False si le texte ressemble à du binaire."""
    s = text[:sample]
    ratio = sum(1 for c in s if c.isprintable() or c in '\n\r\t') / max(len(s), 1)
    return ratio >= 0.70


def _unlock_pdf(raw_bytes, filepath):
    """Déverrouille un PDF protégé avec pikepdf. Retourne les bytes (déverrouillés ou originaux)."""
    try:
        import pikepdf, io
        with pikepdf.open(io.BytesIO(raw_bytes), password="") as pdf:
            buf = io.BytesIO()
            pdf.save(buf)
            unlocked = buf.getvalue()
            print(f"    🔓 PDF déverrouillé automatiquement")
            return unlocked
    except Exception:
        return raw_bytes


def normalize(s):
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return s.lower()


def detect_jurisdiction(path_parts):
    path_str = normalize('/'.join(path_parts))
    for jur, keywords in JURISDICTION_KEYWORDS.items():
        for kw in keywords:
            if kw in path_str:
                return jur
    return None


def detect_category(path_parts):
    last = normalize(path_parts[-1]) if path_parts else ''
    if last == 'sa':      return 'societes_SA'
    if last == 'sarl':    return 'societes_SARL'
    if last == 'sas':     return 'societes_SAS'
    if 'travail' in last: return 'droit_travail'
    if 'commercial' in last or 'commerce' in last: return 'droit_commercial'
    if 'societ' in last:  return 'droit_societes'
    clean = last.replace('droit_', '').strip('_').strip()
    return clean if clean else 'general'


def extract_text(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.txt':
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    elif ext in ['.docx', '.doc']:
        try:
            from docx import Document
            doc = Document(filepath)
            return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
        except Exception as e:
            print(f"    Erreur DOCX: {e}")
            return None
    elif ext == '.pdf':
        raw_bytes = open(filepath, 'rb').read()
        pdf_bytes = _unlock_pdf(raw_bytes, filepath)
        import io
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            if len(text.strip()) > 100 and _readable(text):
                return text
        except: pass
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
            text = "\n".join(p.extract_text() or "" for p in reader.pages)
            if len(text.strip()) > 100 and _readable(text):
                return text
        except: pass
        print(f"    PDF scanné ou illisible - OCR requis (ilovepdf.com)")
        return None
    return None


def already_exists(source_name):
    """Verifie si le document existe deja dans le RAG"""
    try:
        resp = requests.get(f"{BACKEND}/rag/list", timeout=10)
        if resp.ok:
            data = resp.json()
            docs = data.get('documents', data) if isinstance(data, dict) else data
            existing = [normalize(d.get('source', '')) for d in docs if isinstance(d, dict)]
            src_norm = normalize(source_name)
            for e in existing:
                if src_norm in e or e in src_norm:
                    return True
        return False
    except:
        return False


def upload_file(filepath, doc_type, category, jurisdiction, overwrite, jurisdiction_override):
    filename = os.path.basename(filepath)
    source_name = os.path.splitext(filename)[0]

    # Juridiction finale: override CLI > detection dossier > auto-backend
    final_jurisdiction = jurisdiction_override or jurisdiction

    jur_label = f" | {final_jurisdiction}" if final_jurisdiction else " | auto"
    print(f"\n  [{doc_type} > {category}{jur_label}] {filename}")

    # Skip si deja present (sauf si overwrite)
    if not overwrite and already_exists(source_name):
        print(f"    DEJA DANS LE RAG - ignore (utiliser --overwrite pour forcer)")
        return False

    text = extract_text(filepath)
    if not text or len(text.strip()) < 100:
        print(f"    Texte vide ou trop court - ignore")
        return False

    print(f"    {len(text.split())} mots extraits")

    try:
        payload = {
            'source_name': source_name,
            'doc_type':    doc_type,
            'category':    category,
            'overwrite':   '1' if overwrite else '0',
        }
        if final_jurisdiction:
            payload['jurisdiction'] = final_jurisdiction

        mime = 'application/pdf' if filepath.lower().endswith('.pdf') else 'application/octet-stream'
        if filepath.lower().endswith('.pdf'):
            raw = open(filepath, 'rb').read()
            file_bytes = _unlock_pdf(raw, filepath)
        else:
            file_bytes = open(filepath, 'rb').read()
        import io
        resp = requests.post(
            f"{BACKEND}/rag/upload",
            files={'file': (filename, io.BytesIO(file_bytes), mime)},
            data=payload,
            timeout=300
        )

        if resp.ok:
            result = resp.json()
            chunks    = result.get('chunks', 0)
            law_name  = result.get('law_name') or ''
            art_mode  = result.get('article_mode', 'chunk')
            jur_used  = result.get('jurisdiction', '')
            print(f"    OK {chunks} {art_mode}s | loi: {law_name or '(non detectee)'} | juridiction: {jur_used}")
            return True
        else:
            print(f"    ERREUR {resp.status_code}: {resp.text[:120]}")
            return False
    except Exception as e:
        print(f"    EXCEPTION: {e}")
        return False


def collect_files(root_folder, doc_type):
    collected = []
    for dirpath, dirnames, filenames in os.walk(root_folder):
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        for filename in sorted(filenames):
            ext = os.path.splitext(filename)[1].lower()
            if ext not in SUPPORTED:
                continue
            filepath = os.path.join(dirpath, filename)
            rel = os.path.relpath(dirpath, root_folder)
            path_parts = [] if rel == '.' else rel.split(os.sep)
            jurisdiction = detect_jurisdiction(path_parts)
            category = detect_category(path_parts) if path_parts else 'general'
            collected.append((filepath, doc_type, category, jurisdiction, filename))
    return collected


def main():
    args = sys.argv[1:]
    if not args or '--help' in args or '-h' in args:
        print(__doc__)
        return

    # Parse args
    folder = None
    doc_type = None
    overwrite = False
    jurisdiction_override = None

    i = 0
    positional = []
    while i < len(args):
        if args[i] == '--overwrite':
            overwrite = True; i += 1
        elif args[i] == '--jurisdiction' and i + 1 < len(args):
            jurisdiction_override = args[i + 1]; i += 2
        else:
            positional.append(args[i]); i += 1

    if len(positional) >= 1: folder   = positional[0]
    if len(positional) >= 2: doc_type = positional[1]

    if not folder or not doc_type:
        print("Usage: python upload_cabinet.py <dossier> <type> [--overwrite] [--jurisdiction droit_marocain]")
        return

    if jurisdiction_override and jurisdiction_override not in VALID_JURISDICTIONS:
        print(f"Juridiction invalide: '{jurisdiction_override}'")
        print(f"Valides: {', '.join(VALID_JURISDICTIONS)}")
        return

    if not os.path.isdir(folder):
        print(f"Dossier introuvable: {folder}")
        return

    files = collect_files(folder, doc_type)
    if not files:
        print(f"Aucun fichier supporte dans {folder}")
        return

    # Resume
    print(f"\n{'='*60}")
    print(f"  {len(files)} fichier(s) dans {folder}")
    print(f"  Mode     : {'OVERWRITE (re-upload tout)' if overwrite else 'SKIP si deja present'}")
    print(f"  Juridiction : {jurisdiction_override or 'auto-detection par dossier'}")
    print(f"{'='*60}")

    groups = {}
    for _, dt, cat, jur, fname in files:
        key = f"{dt} > {cat}" + (f" | {jurisdiction_override or jur or 'auto'}")
        groups.setdefault(key, []).append(fname)

    for key, fnames in sorted(groups.items()):
        print(f"\n  [{key}] - {len(fnames)} fichier(s)")
        for f in fnames[:5]:
            print(f"    - {f}")
        if len(fnames) > 5:
            print(f"    ... et {len(fnames)-5} autres")

    print(f"\n{'='*60}")
    input("\nAppuyez sur ENTREE pour commencer (Ctrl+C pour annuler)...")

    success_count = 0
    skip_count = 0
    results = []

    for idx, (filepath, dt, cat, jur, filename) in enumerate(files, 1):
        print(f"\n[{idx}/{len(files)}]", end="")
        ok = upload_file(filepath, dt, cat, jur, overwrite, jurisdiction_override)
        if ok:
            success_count += 1
            results.append({"file": filename, "type": dt, "category": cat,
                            "jurisdiction": jurisdiction_override or jur, "status": "ok"})
        else:
            skip_count += 1
            results.append({"file": filename, "type": dt, "category": cat,
                            "jurisdiction": jurisdiction_override or jur, "status": "skip"})
        if idx < len(files):
            time.sleep(PAUSE)

    print(f"\n{'='*60}")
    print(f"Termine! {success_count} uploades, {skip_count} ignores")

    with open("upload_report.json", 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"Rapport: upload_report.json")


if __name__ == "__main__":
    main()
