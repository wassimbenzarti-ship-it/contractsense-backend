# CLAUDE.md — Mémoire partagée contractsense-backend

> **LIS CE FICHIER EN ENTIER AVANT DE MODIFIER app.py.**
> Plusieurs agents Claude travaillent en parallèle sur ce repo. Ce fichier est la source de vérité partagée.

---

## Règles de travail obligatoires

### Avant chaque session
```bash
git fetch origin
git checkout claude/fix-cors-admin-endpoint-CmD16  # ou la branche assignée
git pull
```

### Ne JAMAIS faire
- `Write` sur `app.py` entier — utiliser uniquement `Edit` ciblé (diff minimal)
- Pousser directement sur `main`
- Commencer à modifier sans avoir fait `git pull` d'abord
- Uploader un fichier complet depuis une copie locale potentiellement obsolète

### Toujours faire
- Créer une branche par fix : `git checkout -b claude/nom-du-fix`
- Vérifier la syntaxe après chaque modification : `python3 -c "import ast; ast.parse(open('app.py').read())"`
- Commiter avec un message descriptif qui liste les fonctions modifiées

---

## Architecture de app.py (2887 lignes)

### Stack
- **Framework** : Flask + Flask-CORS
- **Hébergement** : Railway (gunicorn, 2 workers gthread, 4 threads, timeout 600s)
- **LLM** : Anthropic Claude (claude-sonnet-4-6 par défaut)
- **Embeddings** : Voyage AI `voyage-law-2` (1024 dimensions) — clé `VOYAGE_API_KEY`
- **Base de données** : Supabase (PostgreSQL + pgvector)
- **Stockage** : Supabase Storage (bucket `contracts`)

### Variables d'environnement requises
| Variable | Usage |
|----------|-------|
| `ANTHROPIC_API_KEY` | Claude API |
| `VOYAGE_API_KEY` | Embeddings sémantiques 1024d |
| `SUPABASE_URL` | URL Supabase |
| `SUPABASE_KEY` | Clé anon Supabase |
| `SUPABASE_SERVICE_KEY` | Clé service Supabase (admin) |
| `SMTP_*` | Email (optionnel) |
| `CMI_*` | Paiement CMI Maroc |

---

## Composants critiques — NE PAS SUPPRIMER

### 1. Moteur RAG (lignes ~360–475 + ~610–745)

Le RAG est le cœur différenciant du produit. Il a été cassé une fois par écrasement accidentel (commit f9324e4).

**Fonctions RAG dans l'ordre d'appel :**

```
get_embedding()           → Voyage AI 1024d, fallback TF-IDF 512d
search_rag_hybrid()       → BM25 + vector via RPC search_rag_hybrid (Supabase)
search_rag_pgvector()     → vector seul via RPC search_rag
search_rag_keyword()      → fallback keyword si pas de vecteur
extract_article_refs()    → extrait "Art. 16 CT" etc. du contenu RAG
```

**Pipeline RAG dans `analyze_contract` :**
1. `search_rag_hybrid` (BM25 + pgvector) — meilleur résultat
2. Si vide → `search_rag_pgvector` seul
3. Si vide → fetch direct Supabase + cosine similarity Python
4. Si vide → `search_rag_keyword` (fallback textuel)
5. Boost docs matching la juridiction du contrat
6. Séparation en **2 contextes** :
   - `model_context` = modèles de contrats + clauses validées → protection client
   - `legal_context` = lois / doctrine / jurisprudence → conformité

**Catégories légales (`LEGAL_CATS`)** : `{"loi", "law", "doctrine", "jurisprudence", "legal", "legislation"}`

**Sources protégées** (ne pas citer dans `rag_source`) : `lexisnexis, dalloz, lamy, mernissi, traite-de-droit, pdf-free, lexis`

**RPCs Supabase requis** :
- `search_rag(query_embedding, match_count, filter_type)` — pgvector cosine
- `search_rag_hybrid(query_text, query_embedding, match_count, p_jurisdiction)` — BM25 + vector

### 2. Fonction `analyze_contract` (ligne ~595)

Fonction centrale. Reçoit le texte du contrat, retourne les modifications JSON.
**Ne jamais simplifier ou raccourcir cette fonction sans vérifier que toutes les étapes sont préservées :**
- Numérotation des paragraphes `[P0]`, `[P1]`...
- Recherche RAG hybride (voir ci-dessus)
- Détection de juridiction inline
- Filtre droit du travail pour contrats non-emploi
- Prompt HAUTE PRIORITÉ (résiliation ≠ durée)
- Injection `model_context` + `legal_context` + `get_legal_framework()`
- Parsing JSON robuste (regex fallback)
- Calcul `_rag_coverage`

### 3. Fonction `get_legal_framework(contract_type)` (ligne ~69)

Retourne les règles légales impératives par type de contrat (droit marocain).
Types supportés : `employment`, `nda`, `service`, `purchase`, `partnership`, `saas`, `generic`.
**Encodage** : UTF-8 propre (a été corrigé d'un double-encodage 7x en avril 2026).

### 4. Système de rôles et workflow

```
juriste → soumet analyse → directeur valide → /queue/add → admin intègre au RAG
```

- `/analyses/validate-by-director/<id>` → marque `status=validated`, appelle `/queue/add`
- `/analyses/request-revision/<id>` → marque `status=revision_requested`
- `/queue/validate` → intègre dans `rag_documents` via `save_rag_doc()`

### 5. CORS

Toutes les réponses passent par `_add_cors()` via `@app.after_request`.
Ne pas ajouter `Connection: keep-alive` (interdit HTTP/2 Railway).
Origins autorisées : `westfieldavocats.com`, `ai.westfieldavocats.com`, `localhost`.

---

## Tables Supabase

| Table | Usage |
|-------|-------|
| `rag_documents` | Docs RAG. Colonnes: `id, title, content, source, category, party_label, jurisdiction, embedding (JSON), embedding_vector (pgvector 1024d)` |
| `analyses` | Analyses sauvegardées. Colonnes: `id, user_id, user_email, filename, contract_type, partie, modifications (JSON), status, director_notes, director_email` |
| `user_accounts` | Comptes. Colonnes: `email, role (directeur/juriste), is_admin, parent_email, payment_status, analyses_remaining, subscription_end` |
| `rag_suggestions` | Suggestions avant validation admin |

---

## Routes principales

| Route | Description |
|-------|-------------|
| `POST /identify-parties` | Détecte les parties du contrat (étape 1) |
| `POST /analyze` | Analyse complète → modifications JSON (étape 2) |
| `POST /analyze-clause` | Analyse d'une clause isolée (max 8000 chars) |
| `POST /detect-jurisdiction` | Détecte le droit applicable |
| `POST /export` | Génère DOCX avec track changes |
| `POST /rag/upload` | Upload doc au RAG (admin) |
| `GET  /rag/list` | Liste les docs RAG groupés par source |
| `GET  /rag/diag` | Diagnostic Voyage AI + pgvector + coverage |
| `POST /queue/add` | Ajoute une analyse validée à la file RAG |
| `POST /queue/validate` | Intègre un item de la file dans le RAG |
| `POST /account/info` | Info compte + reset quota hebdo free tier |
| `POST /chat` | Chatbot contractuel (claude-sonnet-4-6) |
| `POST /payment/initiate` | Init paiement CMI Maroc |
| `POST /payment/callback` | Callback CMI (ACTION=POSTAUTH) |

---

## Historique des incidents

| Date | Commit | Problème | Impact |
|------|--------|----------|--------|
| 2026-04-14 10:41 | f9324e4 | Upload manuel app.py obsolète pour fix CMI | Suppression du RAG hybride, model_context, legal_context, jurisdiction boost |
| 2026-04-15 | 860c9b8 | Restauration complète du RAG | Corrigé |
| 2026-04-15 | 64f3247 | Encodage UTF-8 7x corrigé | 261 strings garbled → texte propre français |

---

## Avant de modifier app.py — checklist

- [ ] `git pull` effectué
- [ ] La fonction à modifier est identifiée précisément (numéro de ligne)
- [ ] Je n'utilise pas `Write` sur le fichier entier
- [ ] Je vérifie la syntaxe après modification
- [ ] Le diff ne supprime pas de code existant non lié à mon fix
- [ ] Je commite avec un message précis listant les fonctions touchées
