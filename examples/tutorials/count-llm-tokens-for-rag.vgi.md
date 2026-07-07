---
title: Count LLM tokens and chunk text for RAG in DuckDB
slug: count-llm-tokens-for-rag
worker: tiktoken
data_version: "2026.1.0"
description: Budget prompt costs, filter documents that exceed a context window, and split text into overlapping token-aware chunks for retrieval — with exact OpenAI BPE counts, all in SQL.
keywords: [llm tokens, tiktoken, rag, chunking, context window, duckdb]
difficulty: intermediate
est_minutes: 7
tier: recipe
dataset: {name: "Inline document rows", provenance: "synthetic, in-tutorial VALUES"}
datePublished: 2026-07-06
dateModified: 2026-07-06
runtime: {wasm: auto}
---

## What will it cost to embed this corpus?

That is a token-counting question, and today it probably lives in a Python job
that pulls rows out of the warehouse, runs `tiktoken`, and writes counts back.
`vgi-tiktoken` bundles the OpenAI encodings into the worker, so the count happens
next to the rows — no round trip, no model download.

Start with the bill for one document:

```sql {role=step expect=scalar}
SELECT tiktoken.main.count_tokens('The quick brown fox jumps over the lazy dog.') AS tokens;
```
```result
tokens
10
```

## Price the whole table, sorted by cost

Because `count_tokens` is a scalar, a corpus-wide estimate is a single
aggregate-friendly query — you can see immediately which rows dominate the bill.

```sql {role=step expect=rows}
WITH docs(id, body) AS (
  VALUES
    (1, 'Short note.'),
    (2, 'A medium paragraph that carries a bit more detail for the model to read.'),
    (3, 'Tiny.')
)
SELECT id, tiktoken.main.count_tokens(body) AS tokens
FROM docs
ORDER BY tokens DESC;
```
```result
id    tokens
2     15
1     3
3     2
```

## Reject what won't fit the window

The same count becomes a guardrail: keep only documents under your context budget
(a deliberately tiny 10 tokens here) before they ever reach the model.

```sql {role=step expect=rows}
WITH docs(id, body) AS (
  VALUES (1, 'Short note.'), (2, 'A longer paragraph that will exceed the demo budget for sure.')
)
SELECT id, tiktoken.main.count_tokens(body) AS tokens
FROM docs
WHERE tiktoken.main.count_tokens(body) <= 10;
```
```result
id    tokens
1     3
```

## Split the survivors into retrieval chunks

`chunk_by_tokens` slides a window of `max_tokens` with `overlap` shared tokens —
the standard RAG move, done in the database instead of a preprocessing script.

```sql {role=step expect=scalar}
SELECT len(tiktoken.main.chunk_by_tokens(
  'Retrieval augmented generation splits long documents into overlapping windows so each chunk fits the embedding model context.',
  16, 4
)) AS chunk_count;
```
```result
chunk_count
3
```

## A word on which numbers are exact

Be honest about the tokenizer, because the cost model depends on it:

- For **OpenAI** families (`cl100k_base` → GPT-4/3.5, `o200k_base` → GPT-4o and
  the o-series) the count is **exact** — it is the same BPE the API bills against.
- For **Anthropic Claude, Meta Llama, Google Gemini, Mistral**, treat it as a
  **close estimate**. Those vendors ship different tokenizers; the shape is right,
  the last few percent isn't. Validate the model column with
  `tiktoken.main.encoding_for_model(model) IS NOT NULL` before you trust a count
  as exact.

Get that caveat right and the rest is free: no tokenizer service, no drift between
your estimate and the invoice. Downstream, `vgi-embed` + `vgi-tantivy` +
`vgi-rerank` turn these chunks into a full in-database RAG stack.
