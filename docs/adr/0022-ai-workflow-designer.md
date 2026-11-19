# ADR-0022: AI workflow designer

**Status:** Accepted.

## Context
Users want to describe a workflow in plain language ("take any question and answer yes or no") and have it
built in the selected project, or changed if the project already has a graph. A model writing graphs freely
would bypass the platform's guarantees: typed ports, versioned node contracts, secret references,
validation.

## Decision
- **The model proposes, the platform decides.** `POST /api/projects/{id}/assistant` sends the model:
  - the rules of IR v1;
  - a compact catalog of the newest permitted version of every node (ports, branches, config keys, enums,
    defaults, limits). Unapproved package nodes are not offered;
  - the current (possibly unsaved) canvas graph and the last 12 chat turns.
- **Whole-graph proposals.** The model returns `{"reply", "change": null | {nodes, edges, name?,
  description?}}` with the complete resulting graph. That is simpler and more reliable for the model than
  patch operations. The server computes the diff (nodes added, changed and removed; connections added
  and removed).
- **Validated with the editor's own pipeline.** The proposal is parsed as IR and checked with
  `validate_project`. Blocking diagnostics go back to the model for up to 3 attempts. Environment
  diagnostics (`SECRET_NOT_CONFIGURED`) are reported as notes and never block.
- **Server-side normalisation:**
  - positions of existing nodes are kept; new nodes are laid out in columns by graph depth, below the
    existing ones;
  - `max_tokens` below 512 is raised. Live testing showed models choosing 16 for one-word answers, which
    leaves reasoning models no output tokens. The prompt also says so.
- **Applying is a revision.** The endpoint never saves. The web app saves an applicable proposal as the
  next revision (auto-apply, as requested) and offers **Undo**, which saves the previous graph as a new
  revision. A proposal with remaining graph errors is shown with its problems and can be applied as a
  draft explicitly.
- **Provider:** OpenRouter with `google/gemini-3.8-flash` by default, key from the `OPENROUTER_API_KEY`
  secret. Configurable via `AGENTICMIND_ASSISTANT_PROVIDER`, `..._MODEL`, `..._API_KEY_SECRET` and
  `..._BASE_URL`. `GET /api/assistant` reports the model and whether the key is configured (never its
  value). Generated LLM nodes reference secrets by name only.
- **UI:**
  - a round "Build with AI" launcher floating in the bottom-right corner above the whole app: white
    sparkles on the logo's purple-to-magenta gradient (`.am-brand-gradient`: #6b1bfd → #8a1dfc → #fd22c4)
    with a soft glow. The same gradient is used for the header mark, user messages and Send;
  - hovering makes the sparkles blink once; clicking warms the circle to the logo's red end
    (#fd22c4 → #fd2079 → #fd3536 → #fd4216), turns the sparkles gold and blinks them, then the chat pops out
    of that corner over a blurred backdrop; the sparkles keep twinkling while the assistant works;
  - replies render as markdown;
  - Esc or a backdrop click closes it, and reduced-motion preferences disable the animations;
  - threads are kept per project for the session; there is also a **New blank project** shortcut.

## Verification
- **8 API tests** with a scripted provider:
  - building in a blank project, then saving and running the result;
  - repair from diagnostics and from a non-JSON reply;
  - giving up with problems shown;
  - answering a question without changing the graph;
  - changing an existing project (positions kept, diff, unsaved canvas used as the base, history sent);
  - missing-key error, catalog contents, helpers.
- **Live with Gemini in the browser:**
  - "answer any question yes or no" built input → LLM → output in a new blank project;
  - "add a one-sentence reason" added a second model and output;
  - the run answered "Yes" (is the Pacific the largest ocean?) and "No" (can penguins fly?);
  - Undo restored the blank graph.
- The live run caught the `max_tokens=16` problem (MODEL_EMPTY_RESPONSE), now normalised and tested.

## Consequences
- Each request costs about 10–25k tokens (the catalog is about 30 KB of text), plus repairs.
- Whole-graph replies grow with graph size. Very large graphs would need patch operations or catalog
  retrieval.
- Validity is structural: a valid graph may still not do what was meant. The reply, the diff, Undo and
  the Evaluation Lab are the safeguards.

## Revisit when
Graphs exceed a few dozen nodes, a provider with native structured output is the default, or
authentication adds per-user threads and quotas.
