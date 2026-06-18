-- Fix 1: search_rag (3-arg, plpgsql) — add ivfflat.probes to avoid low-recall ANN misses
CREATE OR REPLACE FUNCTION public.search_rag(query_embedding vector, match_count integer DEFAULT 10, filter_type text DEFAULT NULL::text)
 RETURNS TABLE(id text, title text, content text, source text, category text, party_label text, similarity double precision)
 LANGUAGE plpgsql
AS $function$
BEGIN
  SET LOCAL ivfflat.probes = 20;
  RETURN QUERY
  SELECT
    r.id::text,
    r.title,
    r.content,
    r.source,
    r.category,
    r.party_label,
    1 - (r.embedding_vector <=> query_embedding) AS similarity
  FROM rag_documents r
  WHERE
    r.embedding_vector IS NOT NULL
    AND (filter_type IS NULL OR r.category = filter_type)
  ORDER BY r.embedding_vector <=> query_embedding
  LIMIT match_count;
END;
$function$;

-- Fix 2: search_rag (4-arg, sql) — same probes fix
CREATE OR REPLACE FUNCTION public.search_rag(query_embedding vector, match_count integer DEFAULT 15, filter_type text DEFAULT NULL::text, filter_user text DEFAULT NULL::text)
 RETURNS TABLE(id text, title text, content text, source text, category text, party_label text, jurisdiction text, article_number text, article_title text, law_name text, tags text[], contract_types text[], document_id text, chunk_index integer, similarity double precision)
 LANGUAGE sql
 STABLE
AS $function$
  SET LOCAL ivfflat.probes = 20;
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
$function$;

-- Fix 3: search_rag_hybrid — add ivfflat.probes AND fix missing ORDER BY before LIMIT 40
-- in the bm25 CTE (it previously had a ROW_NUMBER() rank computed but no ORDER BY governing
-- which 40 rows survived the LIMIT, so it returned an arbitrary subset, not the top 40 by
-- BM25 relevance).
CREATE OR REPLACE FUNCTION public.search_rag_hybrid(query_text text, query_embedding vector, match_count integer DEFAULT 15, p_jurisdiction text DEFAULT NULL::text)
 RETURNS TABLE(id text, title text, content text, source text, category text, party_label text, jurisdiction text, article_number text, article_title text, law_name text, tags text[], contract_types text[], document_id text, chunk_index integer, score double precision)
 LANGUAGE sql
 STABLE
AS $function$
  SET LOCAL ivfflat.probes = 20;
  WITH
  q AS (
    SELECT CASE
      WHEN string_agg('''' || lexeme || '''', ' | ') IS NULL THEN NULL
      ELSE to_tsquery('french', string_agg('''' || lexeme || '''', ' | '))
    END AS tsq
    FROM unnest(tsvector_to_array(to_tsvector('french', query_text))) AS lexeme
  ),
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
    ORDER BY ts_rank_cd(
               to_tsvector('french', coalesce(rd.title,'') || ' ' || coalesce(rd.law_name,'') || ' ' || coalesce(rd.content,'')),
               q.tsq
             ) DESC
    LIMIT 40
  ),
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
$function$;
