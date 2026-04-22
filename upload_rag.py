"""
upload_rag.py — Pipeline d'upload RAG v2
Chunking article par article, classification automatique de juridiction,
tags thématiques, embedding Voyage AI.

Usage:
  python upload_rag.py --file code_travail_marocain.pdf
  python upload_rag.py --folder ./docs/maroc/
  python upload_rag.py --file contrat_cdi_type.docx --jurisdiction droit_marocain --category modele_valide
  python upload_rag.py --file tout.pdf --dry-run     # aperçu sans insérer

Variables d'environnement:
  SUPABASE_URL, SUPABASE_SERVICE_KEY  (obligatoires)
  VOYAGE_API_KEY                       (recommandé — embeddings juridiques)
  OPENAI_API_KEY                       (fallback si pas Voyage)
"""

import os, sys, re, json, hashlib, unicodedata, requests
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "") or os.environ.get("SUPABASE_KEY", "")
VOYAGE_KEY   = os.environ.get("VOYAGE_API_KEY", "")
OPENAI_KEY   = os.environ.get("OPENAI_API_KEY", "")

VALID_JURISDICTIONS = [
    "droit_marocain", "droit_francais", "droit_algerien",
    "droit_tunisien", "droit_anglais", "universel"
]
VALID_CATEGORIES = [
    "loi_codifiee",     # Code du Travail, DOC, Code de Commerce
    "loi_speciale",     # Loi 09-08, Loi 15-95, RGPD
    "decret",           # Decrets d'application
    "modele_valide",    # Clauses validees par un avocat
    "modele_reference", # Contrats types du marche
    "doctrine",         # Articles, commentaires
    "jurisprudence",    # Arrets, decisions
    "contract",         # Alias generique
    "law",              # Alias generique pour loi
]
SUPPORTED_EXT = {".pdf", ".docx", ".txt", ".md"}

# ── Mapping juridiction par mots-cles ─────────────────────────────────────────
_JUR_KEYWORDS = {
    "droit_marocain": [
        "dahir", "royaume du maroc", "code du travail marocain",
        "tribunal de commerce de casablanca", "bank al-maghrib", "bam",
        "dahir des obligations", "doc ", "loi 09-08", "loi 15-95",
        "cour de cassation marocaine", "bulletin officiel",
        "dirham", "mad", "cnss", "cimr", "caisse nationale",
    ],
    "droit_francais": [
        "code civil francais", "code du travail francais",
        "urssaf", "pole emploi", "cour de cassation francaise",
        "tribunal judiciaire", "conseil de prudhommes",
        "journal officiel de la republique francaise", "jorf",
        "rgpd", "cnil", "smic",
        "article l.", "article r.", "article d.",
    ],
    "droit_algerien": [
        "code du travail algerien", "code civil algerien",
        "dinar algerien", "dza", "tribunal algerien",
        "journal officiel algerien",
    ],
    "droit_tunisien": [
        "code du travail tunisien", "code des obligations tunisien",
        "dinar tunisien", "tnd", "tribunal tunisien",
    ],
    "droit_anglais": [
        "employment rights act", "companies act",
        "governed by english law", "governed by the laws of england",
        "high court of england",
    ],
}

# ── Mapping thematique pour les tags ─────────────────────────────────────────
_TAG_KEYWORDS = {
    "cdd":             ["contrat a duree determinee", "cdd", "fixed-term"],
    "cdi":             ["contrat a duree indeterminee", "cdi", "permanent contract"],
    "licenciement":    ["licenciement", "rupture du contrat", "dismissal", "termination"],
    "preavis":         ["preavis", "notice period", "delai de conge"],
    "periode_essai":   ["periode d essai", "probatoire", "probation"],
    "non-concurrence": ["non-concurrence", "non-competition", "concurrence deloyale"],
    "confidentialite": ["confidentialite", "secret", "nda", "non-disclosure"],
    "salaire":         ["remuneration", "salaire", "indemnite", "compensation"],
    "heures_sup":      ["heures supplementaires", "overtime"],
    "conges":          ["conges", "vacances", "leave", "annual leave"],
    "responsabilite":  ["responsabilite", "liability", "indemnisation"],
    "propriete_intellectuelle": ["propriete intellectuelle", "droits pi", "intellectual property"],
    "donnees_personnelles": ["donnees personnelles", "rgpd", "gdpr", "vie privee"],
    "resiliation":     ["resiliation", "termination", "resolution"],
    "paiement":        ["paiement", "delai de paiement", "facture", "invoice"],
    "garantie":        ["garantie", "warranty"],
    "force_majeure":   ["force majeure", "cas fortuit"],
    "arbitrage":       ["arbitrage", "arbitration", "mediation", "tribunal arbitral"],
}

_CONTRACT_TYPE_KEYWORDS = {
    "CDI":        ["contrat a duree indeterminee", "cdi", "permanent"],
    "CDD":        ["contrat a duree determinee", "cdd", "fixed-term"],
    "prestation": ["prestation de services", "service agreement", "contrat de prestation"],
    "NDA":        ["confidentialite", "nda", "non-disclosure"],
    "vente":      ["contrat de vente", "sale agreement", "achat-vente"],
    "distribution": ["distribution", "distributeur", "franchis"],
}


def norm(s):
    return unicodedata.normalize("NFD", s.lower()).encode("ascii", "ignore").decode()


# ── Detection automatique de juridiction ──────────────────────────────────────
def detect_jurisdiction(text, title=""):
    """Retourne (juridiction, score_confiance 0-1)."""
    sample = norm(text[:5000] + " " + title)
    scores = {}
    for jur, keywords in _JUR_KEYWORDS.items():
        hits = sum(1 for kw in keywords if norm(kw) in sample)
        scores[jur] = hits

    best_jur   = max(scores, key=scores.get)
    best_score = scores[best_jur]
    total      = sum(scores.values()) or 1
    confidence = min(best_score / max(total, 1), 1.0)

    if best_score == 0:
        return "universel", 0.0
    if best_score < 2:
        return "universel", confidence * 0.5
    return best_jur, min(confidence * 2, 1.0)


# ── Detection de categorie ────────────────────────────────────────────────────
def detect_category(text, title="", hint=""):
    if hint and hint in VALID_CATEGORIES:
        return hint
    t = norm(title + " " + text[:1000])
    if any(k in t for k in ["jurisprudence", "arret", "decision", "cour de cassation"]):
        return "jurisprudence"
    if any(k in t for k in ["doctrine", "commentaire", "note de doctrine"]):
        return "doctrine"
    if any(k in t for k in ["decret", "arrete", "circulaire"]):
        return "decret"
    if any(k in t for k in ["loi 09-08", "loi 15-95", "rgpd", "gdpr"]):
        return "loi_speciale"
    if any(k in t for k in ["code du", "dahir", "loi n", "bulletin officiel", "journal officiel"]):
        return "loi_codifiee"
    if any(k in t for k in ["modele", "type", "standard", "valide", "validated"]):
        return "modele_valide"
    return "modele_reference"


# ── Extraction de tags thematiques ────────────────────────────────────────────
def extract_tags(text, title=""):
    sample = norm(text[:3000] + " " + title)
    return [tag for tag, keywords in _TAG_KEYWORDS.items()
            if any(norm(kw) in sample for kw in keywords)]


# ── Detection des types de contrat ───────────────────────────────────────────
def extract_contract_types(text, title=""):
    sample = norm(text[:3000] + " " + title)
    return [ct for ct, keywords in _CONTRACT_TYPE_KEYWORDS.items()
            if any(norm(kw) in sample for kw in keywords)]


# ── Detection du nom de loi ───────────────────────────────────────────────────
def detect_law_name(text, title=""):
    """Retourne (law_name, law_date)."""
    known = [
        ("Code du Travail Marocain",           ["code du travail marocain", "code travail maroc"]),
        ("Dahir des Obligations et Contrats",   ["dahir des obligations", "doc "]),
        ("Code de Commerce Marocain",           ["code de commerce marocain"]),
        ("Code du Travail Francais",            ["code du travail francais"]),
        ("Code Civil Francais",                 ["code civil francais"]),
        ("RGPD",                                ["reglement general sur la protection", "rgpd", "gdpr"]),
        ("Loi 09-08 (Protection donnees Maroc)",["loi 09-08"]),
        ("Loi 15-95 (Code Commerce Maroc)",     ["loi 15-95"]),
    ]
    sample = norm(text[:2000] + " " + (title or ""))
    for name, keywords in known:
        if any(norm(kw) in sample for kw in keywords):
            date_m = re.search(r'\b(19|20)\d{2}\b', text[:500])
            return name, date_m.group() if date_m else None
    t = (title or "").strip()
    if t and len(t) > 5:
        return t, None
    return None, None


# ── Chunking article par article ──────────────────────────────────────────────
def split_into_articles(text):
    """
    Decoupe un texte legal en articles individuels.
    Retourne une liste de {'number': '16', 'title': '...', 'content': '...'}.
    Fallback: chunks de 1200c avec chevauchement si aucun article detecte.
    """
    art_pattern = re.compile(
        r'(?:^|\n)\s*(?:Article|Art\.?|ARTICLE|article)\s+'
        r'(\d+(?:[.\-]\d+)?(?:\s+(?:bis|ter|quater))?)'
        r'(?:\s*[-:.\u2013\u2014]\s*(.+?))?(?=\n|$)',
        re.IGNORECASE | re.MULTILINE
    )
    matches = list(art_pattern.finditer(text))

    if len(matches) < 2:
        return _chunk_plain(text)

    articles = []
    for i, m in enumerate(matches):
        art_num   = m.group(1).strip()
        art_title = (m.group(2) or "").strip()[:100]
        start     = m.start()
        end       = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content   = text[start:end].strip()

        if len(content) < 30:
            continue

        articles.append({
            "number":  art_num,
            "title":   art_title,
            "content": content[:2000],
        })

    return articles if articles else _chunk_plain(text)


def _chunk_plain(text, size=1200, overlap=200):
    chunks, start, idx = [], 0, 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append({"number": None, "title": None, "content": text[start:end]})
        if end >= len(text):
            break
        start += size - overlap
        idx   += 1
    return chunks


# ── Embeddings ────────────────────────────────────────────────────────────────
def get_embedding(text):
    if VOYAGE_KEY:
        try:
            import voyageai
            vo = voyageai.Client(api_key=VOYAGE_KEY)
            r  = vo.embed([text[:4000]], model="voyage-law-2", input_type="document")
            return r.embeddings[0]
        except Exception as e:
            print(f"  Voyage error: {e}")
    if OPENAI_KEY:
        r = requests.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
            json={"model": "text-embedding-3-small", "input": text[:8000]},
            timeout=30,
        )
        if r.ok:
            return r.json()["data"][0]["embedding"]
        print(f"  OpenAI error: {r.text[:100]}")
    return None


# ── Extraction texte ──────────────────────────────────────────────────────────
def extract_text(filepath):
    ext = filepath.suffix.lower()
    if ext in (".txt", ".md"):
        return filepath.read_text(encoding="utf-8", errors="ignore")
    if ext == ".docx":
        try:
            import docx
            doc = docx.Document(str(filepath))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except ImportError:
            print("  pip install python-docx requis")
            return None
    if ext == ".pdf":
        for loader in (_load_pdf_plumber, _load_pdf_pypdf2):
            text = loader(filepath)
            if text:
                return text
        print("  pip install pdfplumber ou PyPDF2 requis")
        return None
    return None


def _is_binary_garbage(text, sample_size=2000):
    """Retourne True si le texte extrait ressemble à du binaire (PDF crypté)."""
    if not text:
        return True
    sample = text[:sample_size]
    printable = sum(1 for c in sample if c.isprintable() or c in '\n\r\t')
    ratio = printable / max(len(sample), 1)
    # Moins de 70% de caractères imprimables = binaire
    if ratio < 0.70:
        return True
    # Beaucoup de caractères non-ASCII suspects (hors accents français)
    weird = sum(1 for c in sample if ord(c) > 127 and c not in 'àâäéèêëîïôöùûüçœæÀÂÄÉÈÊËÎÏÔÖÙÛÜÇŒÆ°«»€–—…')
    if weird / max(len(sample), 1) > 0.15:
        return True
    return False


def _load_pdf_plumber(fp):
    try:
        import pdfplumber
        with pdfplumber.open(str(fp)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        if _is_binary_garbage(text):
            return None
        return text
    except Exception:
        return None


def _load_pdf_pypdf2(fp):
    try:
        import PyPDF2
        with open(fp, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            if reader.is_encrypted:
                print("  ⚠ PDF chiffré/protégé — déverrouillez-le avant upload (ex: qpdf --decrypt)")
                return None
            text = "\n".join(p.extract_text() or "" for p in reader.pages)
        if _is_binary_garbage(text):
            return None
        return text
    except Exception:
        return None


# ── Supabase helpers ──────────────────────────────────────────────────────────
def _sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": "Bearer " + SUPABASE_KEY,
        "Content-Type":  "application/json",
    }


def doc_exists(title, jurisdiction, article_number=None):
    params = {
        "select": "id",
        "title":        f"eq.{title}",
        "jurisdiction": f"eq.{jurisdiction}",
        "limit":        "1",
    }
    if article_number:
        params["article_number"] = f"eq.{article_number}"
    r = requests.get(SUPABASE_URL + "/rest/v1/rag_documents",
                     headers=_sb_headers(), params=params, timeout=15)
    return bool(r.ok and r.json())


def upload_doc(row):
    row_copy = dict(row)
    emb = row_copy.pop("embedding", None)
    if emb and isinstance(emb, list):
        row_copy["embedding_vector"] = "[" + ",".join(str(x) for x in emb) + "]"
        row_copy["embedding"]        = json.dumps(emb)
    r = requests.post(
        SUPABASE_URL + "/rest/v1/rag_documents",
        headers={**_sb_headers(), "Prefer": "return=minimal"},
        json=row_copy,
        timeout=30,
    )
    return r.status_code in (200, 201)


# ── Traitement d'un fichier ───────────────────────────────────────────────────
def process_file(filepath, jurisdiction_hint, category_hint, dry_run, overwrite=False):
    if filepath.suffix.lower() not in SUPPORTED_EXT:
        print(f"  Ignore (format non supporte): {filepath.name}")
        return 0, 0

    print(f"\n{'='*60}")
    print(f"  Fichier : {filepath.name}")

    text = extract_text(filepath)
    if not text or len(text.strip()) < 50:
        print("  Ignore (texte vide ou trop court)")
        return 0, 0

    # Classification automatique
    jur, jur_conf = detect_jurisdiction(text, filepath.stem)
    if jurisdiction_hint and jurisdiction_hint in VALID_JURISDICTIONS:
        jur = jurisdiction_hint
        print(f"  Juridiction : {jur} (forcee)")
    else:
        marker = "OK" if jur_conf >= 0.6 else "?"
        print(f"  Juridiction : {jur} [{marker} confiance {jur_conf:.0%}]")

    category  = detect_category(text, filepath.stem, category_hint or "")
    law_name, law_date = detect_law_name(text, filepath.stem)
    tags       = extract_tags(text, filepath.stem)
    ct_types   = extract_contract_types(text, filepath.stem)
    doc_id     = hashlib.md5(filepath.name.encode()).hexdigest()[:12]

    print(f"  Categorie  : {category}")
    print(f"  Loi        : {law_name or '(non detectee)'} {law_date or ''}")
    print(f"  Tags       : {', '.join(tags) or '(aucun)'}")
    print(f"  Types      : {', '.join(ct_types) or '(aucun)'}")

    articles = split_into_articles(text)
    art_mode = "article" if (articles and articles[0]["number"]) else "chunk"
    print(f"  Chunks     : {len(articles)} ({art_mode}s)")

    ok, skip = 0, 0
    base_title = filepath.stem.replace("_", " ").replace("-", " ")

    for i, art in enumerate(articles):
        art_num   = art.get("number")
        art_title = art.get("title", "") or ""
        content   = art.get("content", "")

        if art_num:
            title = f"{law_name or base_title} — Art. {art_num}"
            if art_title:
                title += f" — {art_title[:60]}"
        elif len(articles) == 1:
            title = base_title
        else:
            title = f"{base_title} (partie {i+1}/{len(articles)})"

        if not overwrite and doc_exists(title, jur, art_num):
            print(f"    [SKIP] {title[:70]}")
            skip += 1
            continue

        if dry_run:
            emb_note = "embed OK" if (VOYAGE_KEY or OPENAI_KEY) else "sans embed"
            ref = f"Art.{art_num}" if art_num else f"chunk {i+1}"
            print(f"    [DRY]  {ref}: {title[:65]} ({len(content)}c, {emb_note})")
            ok += 1
            continue

        embedding = get_embedding(f"{title}\n{content}")

        row = {
            "title":          title,
            "content":        content,
            "source":         f"upload/{filepath.stem}",
            "category":       category,
            "jurisdiction":   jur,
            "party_label":    None,
            "article_number": art_num,
            "article_title":  art_title or None,
            "law_name":       law_name,
            "law_date":       law_date,
            "tags":           tags or None,
            "contract_types": ct_types or None,
            "document_id":    doc_id,
            "chunk_index":    i,
            "embedding":      embedding,
        }
        if upload_doc(row):
            ok += 1
            print(f"    [OK]   Art.{art_num or i+1}: {title[:60]} ({'embed' if embedding else 'sans embed'})")
        else:
            print(f"    [FAIL] {title[:60]}")

    return ok, skip


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    if not args or "--help" in args or "-h" in args:
        print(__doc__)
        return

    jurisdiction, category, folder, file_path, dry_run, overwrite = None, None, None, None, False, False
    i = 0
    while i < len(args):
        if   args[i] == "--jurisdiction" and i+1 < len(args): jurisdiction = args[i+1]; i += 2
        elif args[i] == "--category"     and i+1 < len(args): category     = args[i+1]; i += 2
        elif args[i] == "--folder"       and i+1 < len(args): folder       = args[i+1]; i += 2
        elif args[i] == "--file"         and i+1 < len(args): file_path    = args[i+1]; i += 2
        elif args[i] == "--dry-run":   dry_run = True; i += 1
        elif args[i] == "--overwrite": overwrite = True; i += 1
        else: i += 1

    if jurisdiction and jurisdiction not in VALID_JURISDICTIONS:
        print(f"Juridiction invalide: '{jurisdiction}'  Valides: {', '.join(VALID_JURISDICTIONS)}")
        sys.exit(1)

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERREUR: SUPABASE_URL + SUPABASE_SERVICE_KEY requis")
        sys.exit(1)

    print("=" * 60)
    print("UPLOAD RAG v2 — Chunking article par article")
    print(f"Juridiction : {jurisdiction or 'auto-detection'}")
    print(f"Categorie   : {category or 'auto-detection'}")
    print(f"Embeddings  : {'Voyage AI (voyage-law-2)' if VOYAGE_KEY else 'OpenAI' if OPENAI_KEY else 'AUCUN'}")
    print(f"Mode        : {'DRY RUN' if dry_run else 'UPLOAD REEL'} {'(overwrite ON)' if overwrite else ''}")

    files = []
    if file_path:
        files = [Path(file_path)]
    elif folder:
        files = [f for f in Path(folder).rglob("*") if f.suffix.lower() in SUPPORTED_EXT]
        print(f"Fichiers trouves : {len(files)}")

    if not files:
        print("Aucun fichier a traiter.")
        return

    total_ok, total_skip = 0, 0
    for f in files:
        ok, skip = process_file(f, jurisdiction, category, dry_run, overwrite)
        total_ok   += ok
        total_skip += skip

    print(f"\n{'='*60}")
    print(f"RESUME")
    print(f"  Uploades : {total_ok}")
    print(f"  Ignores  : {total_skip} (deja existants)")
    print(f"  Fichiers : {len(files)}")


if __name__ == "__main__":
    main()
