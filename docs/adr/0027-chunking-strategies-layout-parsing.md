# ADR-0027: Chunking strategies, parent-child retrieval, layout-aware parsing

**Status:** Accepted. This is step 3 of the "build any agentic workflow" plan (steps 1–2 are ADR-0025 and
ADR-0026).

## Context
Users asked for document chunking, semantic chunking, recursive character splitting, parent-child chunking
and layout-aware parsing. The Chunker had `recursive`, `fixed` and `markdown`, and the parsers flattened
most structure: HTML lists and tables, PDF headings.

## Decision

### Chunker 1.1.0
Version 1.0.0 is kept unchanged for existing graphs. 1.1.0 adds an optional `embedding_model` input and a
`parents` output, and needs `network`/`credentials` only when it embeds. Every strategy still yields
**exact slices** of the source text, so offsets, citations and retrieval evaluation stay valid.

| strategy | what it does |
|---|---|
| recursive | character/token splitting on paragraphs → lines → sentences → words, merged with overlap (as before) |
| fixed | sliding window (as before) |
| sentence | whole sentences packed up to the size; overlap repeats whole trailing sentences |
| markdown | heading sections, then recursive (as before) |
| layout | follows structure: a new chunk at each heading, headings kept with the content below, tables / lists / fenced code never cut unless one alone exceeds the size (then split between rows or items) |
| semantic | sentence windows (±1 sentence); a break where neighbouring windows are least similar (distance above the chosen percentile, interpolated like `numpy.percentile`); small groups merged; long topics packed by sentence |
| document | one chunk per document, split recursively only when it exceeds the size |
| parent_child | parent sections (`parent_chunk_size`) each split into small children; children carry `parent_chunk_id`, `parent_text` and the parent's offsets |

- **Semantic similarity:** with an Embedding Model connected, similarity uses its embeddings (local
  fastembed by default). Without one, it falls back to dependency-free TF-IDF over lightly stemmed content
  words, so the strategy works offline and is deterministic. A `semantic_breaks` trace event records how
  many breaks were made and which similarity was used.
- **Form:** fields appear per strategy, using the `show_when` hint from ADR-0026.

### Parent Expander (`agenticmind.rag.parent_expander@1.0.0`)
- **What it does:** after retrieval, it replaces children with their parents, de-duplicated in rank order.
  Each parent keeps the best child score and lists the `matched_chunks`.
- **No extra store:** the parent text travels in child metadata, which every vector store adapter already
  round-trips. Documents without parent metadata pass through.
- **Result:** precise matching on small chunks, fuller context for the model.

### Layout-aware parsing
- **PDF (`pdf_layout` option on the Document and S3 Loaders):**
  - lines are rebuilt from pypdf text runs;
  - lines set at 15 % or more above the body font size (the most common size by characters) become
    Markdown headings, the largest being `#`;
  - vertical gaps start paragraphs.
  - Off by default, because it relies on font sizes. Tables in PDFs are not detected.
- **HTML / EPUB:** list items become `- `, table rows `| a | b |`, and `<pre>` becomes fenced code with its
  line breaks and indentation kept.
- **DOCX:** tables become `| a | b |` rows, matching what the layout chunker recognises.

### Template
**RAG with parent-child chunks:** layout-aware loading → `parent_child` chunks → index → retrieve → Parent
Expander → answer with citations.

## Verification
- **Unit tests:**
  - sentence segmentation and packing with whole-sentence overlap;
  - semantic breaks at a real topic change (lexical and with an embedding model), with the size limit
    honoured;
  - layout blocks (heading, paragraph, table, list with a wrapped item, code), heading paths, oversized
    tables split between rows;
  - each strategy yields exact slices;
  - document splits only when too long;
  - parent-child offsets and parent text; Parent Expander ordering, de-duplication, best score, offsets,
    `max_parents`, pass-through;
  - 1.0.0 keeps its contract.
- **Parsing tests:**
  - a generated PDF with 24/16/11 pt lines gives `#`/`##` headings and a paragraph break, and feeds the
    layout chunker with section paths;
  - HTML lists, tables and pre blocks; DOCX tables.

## Consequences
- The token measure is still approximate (words and punctuation), not a model tokenizer.
- Semantic chunking embeds every sentence window: costlier than other strategies with remote embedding
  APIs, and cheap with the local model.
- Children carry their parent's text, so the stored payload grows roughly by the parent size per child.
  This is within the metadata limits of all adapters at the default sizes.
- PDF layout detection is heuristic. Multi-column PDFs keep the order of the content stream.

## Revisit when
Model-exact tokenizers, table extraction from PDFs (e.g. a layout model), late chunking, or contextual
chunk headers ("contextual retrieval") are needed.
