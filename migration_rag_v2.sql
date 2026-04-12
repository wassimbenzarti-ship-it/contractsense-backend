-- ============================================================
-- RAG v2 Migration — Article-level chunking + hybrid search
-- Exécuter dans l'éditeur SQL Supabase
-- ============================================================

-- 1. Nouvelles colonnes de métadonnées
ALTER TABLE rag_documents
  ADD COLUMN IF NOT EXISTS article_number  TEXT,
  ADD COLUMN IF NOT EXISTS article_title   TEXT,
  ADD COLUMN IF NOT EXISTS law_name        TEXT,
  ADD COLUMN IF NOT EXISTS law_date        TEXT,
  ADD COLUMN IF NOT EXISTS tags            TEXT[],
  ADD COLUMN IF NOT EXISTS contract_types  TEXT[],
  ADD COLUMN IF NOT EXISTS document_id     TEXT,
  ADD COLUMN IF NOT EXISTS chunk_index     INTEGER DEFAULT 0;

-- 2. Index full-text search (BM25 hybride) — français + arabe
CREATE INDEX IF NOT EXISTS rag_fts_fr_idx
  ON rag_documents
  USING gin(to_tsvector('french',
    coalesce(title,'') || ' ' ||
    coalesce(law_name,'') || ' ' ||
    coalesce(content,'')
  ));

-- 3. Index sur tags et contract_types (filtrage rapide)
CREATE INDEX IF NOT EXISTS rag_tags_idx          ON rag_documents USING gin(tags);
CREATE INDEX IF NOT EXISTS rag_contract_types_idx ON rag_documents USING gin(contract_types);
CREATE INDEX IF NOT EXISTS rag_article_number_idx ON rag_documents (article_number) WHERE article_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS rag_document_id_idx    ON rag_documents (document_id)    WHERE document_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS rag_law_name_idx       ON rag_documents (law_name)       WHERE law_name IS NOT NULL;

-- 4. Fonction de recherche hybride BM25 + vecteur
-- Drop toutes les surcharges existantes
DROP FUNCTION IF EXISTS search_rag(vector, int, text, uuid);
DROP FUNCTION IF EXISTS search_rag(vector, int, text, text);
DROP FUNCTION IF EXISTS search_rag(vector, int);
DROP FUNCTION IF EXISTS search_rag(vector);
DROP FUNCTION IF EXISTS search_rag_hybrid(text, vector, int, text);
DROP FUNCTION IF EXISTS search_rag_hybrid(text, vector, int);

CREATE OR REPLACE FUNCTION search_rag(
  query_embedding   vector,
  match_count       int      DEFAULT 15,
  filter_type       text     DEFAULT NULL,
  filter_user       text     DEFAULT NULL
)
RETURNS TABLE (
  id            text,
  title         text,
  content       text,
  source        text,
  category      text,
  party_label   text,
  jurisdiction  text,
  article_number text,
  article_title  text,
  law_name       text,
  tags           text[],
  contract_types text[],
  document_id    text,
  chunk_index    int,
  similarity    float
)
LANGUAGE sql STABLE
AS $$
  SELECT
    id, title, content, source, category, party_label, jurisdiction,
    article_number, article_title, law_name, tags, contract_types,
    document_id, chunk_index,
    1 - (embedding_vector <=> query_embedding) AS similarity
  FROM rag_documents
  WHERE embedding_vector IS NOT NULL
    AND (filter_type IS NULL OR category = filter_type)
  ORDER BY embedding_vector <=> query_embedding
  LIMIT match_count;
$$;

-- 5. Fonction recherche hybride (BM25 + vecteur, fusion RRF)
CREATE OR REPLACE FUNCTION search_rag_hybrid(
  query_text        text,
  query_embedding   vector,
  match_count       int     DEFAULT 15,
  p_jurisdiction    text    DEFAULT NULL
)
RETURNS TABLE (
  id            text,
  title         text,
  content       text,
  source        text,
  category      text,
  party_label   text,
  jurisdiction  text,
  article_number text,
  article_title  text,
  law_name       text,
  tags           text[],
  contract_types text[],
  document_id    text,
  chunk_index    int,
  score         float
)
LANGUAGE sql STABLE
AS $$
  WITH
  -- Semantic search
  vec AS (
    SELECT id,
           ROW_NUMBER() OVER (ORDER BY embedding_vector <=> query_embedding) AS rank_vec
    FROM rag_documents
    WHERE embedding_vector IS NOT NULL
      AND (p_jurisdiction IS NULL
           OR jurisdiction = p_jurisdiction
           OR jurisdiction IN ('universel','auto'))
    ORDER BY embedding_vector <=> query_embedding
    LIMIT 40
  ),
  -- BM25 full-text search
  bm25 AS (
    SELECT id,
           ROW_NUMBER() OVER (
             ORDER BY ts_rank_cd(
               to_tsvector('french', coalesce(title,'') || ' ' || coalesce(law_name,'') || ' ' || coalesce(content,'')),
               plainto_tsquery('french', query_text)
             ) DESC
           ) AS rank_bm25
    FROM rag_documents
    WHERE to_tsvector('french', coalesce(title,'') || ' ' || coalesce(law_name,'') || ' ' || coalesce(content,''))
          @@ plainto_tsquery('french', query_text)
      AND (p_jurisdiction IS NULL
           OR jurisdiction = p_jurisdiction
           OR jurisdiction IN ('universel','auto'))
    LIMIT 40
  ),
  -- Reciprocal Rank Fusion
  fused AS (
    SELECT
      COALESCE(vec.id, bm25.id) AS id,
      COALESCE(1.0/(60 + vec.rank_vec),  0) * 0.6 +
      COALESCE(1.0/(60 + bm25.rank_bm25), 0) * 0.4  AS rrf_score
    FROM vec
    FULL OUTER JOIN bm25 ON vec.id = bm25.id
  )
  SELECT
    r.id, r.title, r.content, r.source, r.category, r.party_label, r.jurisdiction,
    r.article_number, r.article_title, r.law_name, r.tags, r.contract_types,
    r.document_id, r.chunk_index,
    f.rrf_score AS score
  FROM fused f
  JOIN rag_documents r ON r.id = f.id
  ORDER BY f.rrf_score DESC
  LIMIT match_count;
$$;

-- Vérification
SELECT 'Migration RAG v2 OK' AS status,
       COUNT(*) AS total_docs,
       COUNT(article_number) AS docs_with_article,
       COUNT(law_name) AS docs_with_law_name
FROM rag_documents;
