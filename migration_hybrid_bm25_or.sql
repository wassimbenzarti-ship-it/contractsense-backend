-- ============================================================
-- Fix BM25 — recherche hybride avec lexèmes en OR
-- À exécuter dans l'éditeur SQL Supabase
--
-- PROBLÈME : plainto_tsquery('french', query) exige que le document
-- contienne TOUS les mots de la requête (opérateur AND). Une requête
-- comme "article 16 loi 21-18 sûretés mobilières durée inscription"
-- ne matche AUCUN document → le côté BM25 de la fusion RRF est vide
-- → seul le vecteur contribue, et il privilégie les chunks d'en-têtes.
--
-- FIX : convertir la requête en lexèmes reliés par OR. ts_rank_cd
-- classe naturellement plus haut les documents qui matchent plus
-- de termes — on garde la pertinence sans exiger un match total.
-- ============================================================

DROP FUNCTION IF EXISTS search_rag_hybrid(text, vector, int, text);

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
  score         double precision
)
LANGUAGE sql STABLE
AS $$
  WITH
  -- Requête OR : chaque lexème devient optionnel, ts_rank_cd favorise les multi-matchs
  q AS (
    SELECT CASE
      WHEN string_agg('''' || lexeme || '''', ' | ') IS NULL THEN NULL
      ELSE to_tsquery('french', string_agg('''' || lexeme || '''', ' | '))
    END AS tsq
    FROM unnest(tsvector_to_array(to_tsvector('french', query_text))) AS lexeme
  ),
  -- Semantic search
  vec AS (
    SELECT rd.id,
           ROW_NUMBER() OVER (ORDER BY rd.embedding_vector <=> query_embedding) AS rank_vec
    FROM rag_documents rd
    WHERE rd.embedding_vector IS NOT NULL
      AND (p_jurisdiction IS NULL
           OR rd.jurisdiction = p_jurisdiction
           OR rd.jurisdiction IN ('universel','auto'))
    ORDER BY rd.embedding_vector <=> query_embedding
    LIMIT 40
  ),
  -- BM25 full-text search (OR des lexèmes, classé par densité de matchs)
  bm25 AS (
    SELECT rd.id,
           ROW_NUMBER() OVER (
             ORDER BY ts_rank_cd(
               to_tsvector('french', coalesce(rd.title,'') || ' ' || coalesce(rd.law_name,'') || ' ' || coalesce(rd.content,'')),
               q.tsq
             ) DESC
           ) AS rank_bm25
    FROM rag_documents rd, q
    WHERE q.tsq IS NOT NULL
      AND to_tsvector('french', coalesce(rd.title,'') || ' ' || coalesce(rd.law_name,'') || ' ' || coalesce(rd.content,''))
          @@ q.tsq
      AND (p_jurisdiction IS NULL
           OR rd.jurisdiction = p_jurisdiction
           OR rd.jurisdiction IN ('universel','auto'))
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

-- Test rapide après exécution (devrait retourner des chunks Loi 21-18)
-- SELECT title, score FROM search_rag_hybrid(
--   'sûretés mobilières durée inscription registre',
--   (SELECT embedding_vector FROM rag_documents WHERE title ILIKE '%21-18%partie 1%' LIMIT 1),
--   10, NULL);
