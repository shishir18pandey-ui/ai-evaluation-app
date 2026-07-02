---
title: AI Evidence Layer
emoji: 🔍
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# AI Evidence Layer

An AI-assisted evaluation system that ingests multi-modal project submissions (deck, demo, code, live prototype) and produces **evidence-grounded, cited, reproducible scores** against a rubric.

Built for the AI Evaluation MVP assignment.

## What it does

Given a submission:

1. Parses the **deck** (PDF/PPTX) and extracts structured claims per slide
2. Parses the **demo transcript** and separates *demonstrated* features from *merely claimed* ones
3. Walks the **code repo** and identifies what capabilities are actually implemented
4. Probes the **live prototype** with Playwright — tests claimed features with LLM-guided clicks
5. Stores all typed `Evidence` in a **Chroma vector DB** with source metadata
6. **Cross-references claims** — flags features in the deck that have no code backing, features in the code that weren't demoed, and contradictions between sources
7. Scores on 5 rubric criteria, retrieving evidence per-criterion and citing specific source IDs (`slide_5`, `src/auth.py`, `video_02:10`, `url_login_flow`)

Final output is structured JSON matching the assignment spec + a Streamlit dashboard report.

---

## Quick start

```bash
# 1. Install
python -m venv venv && source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# 2. Configure — get a free Groq key at https://console.groq.com
cp .env.example .env
# edit .env, paste your GROQ_API_KEY

# 3. Run the UI
streamlit run app/ui/streamlit_chat.py

# Or run from CLI
python run_cli.py --deck sample/deck.pdf --repo https://github.com/user/project \
                  --transcript sample/demo.txt --url https://app.example.com
```

**Local development without rate limits**: point the app at a local [Ollama](https://ollama.com)
server instead of Groq — no daily/per-minute quota, and no API key needed. Requires
`ollama serve` running and the model pulled locally (`ollama pull qwen3-coder:30b`).
Set in `.env`:
```
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen3-coder:30b
```
This is for local iteration only — the public Hugging Face Space deployment stays on
Groq, since there's no GPU/model server available in that container.

---

## Architecture

```
┌─────────────┐  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐
│    Deck     │  │  Transcript  │  │  Code repo   │  │ Prototype URL  │
│  (PDF/PPTX) │  │    (text)    │  │   (git URL)  │  │   (optional)   │
└──────┬──────┘  └──────┬───────┘  └──────┬───────┘  └────────┬───────┘
       ▼                ▼                 ▼                   ▼
   Deck extract    Video extract     Code extract     Playwright probe
   (claims/slide)  (demo vs claim)   (capabilities)   (LLM-guided)
       └────────────────┴─────────────────┴───────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │  Chroma vector DB     │
                    │  (typed Evidence +    │
                    │   source metadata)    │
                    └───────────┬───────────┘
                                │
           ┌────────────────────┼────────────────────┐
           ▼                    ▼                    ▼
      Unify submission    Cross-reference        Rubric scorer
      (single source      (claim vs reality)     (evidence-first
       of truth)                                  with citations)
                                │
                    ┌───────────▼───────────┐
                    │  SubmissionReport     │
                    │  (JSON + UI render)   │
                    └───────────────────────┘
```

### Key design decisions

**Single deterministic pipeline, not multi-agent.** CrewAI / AutoGen were considered and rejected: the data flow is deterministic (no autonomous planning needed), and the rubric explicitly requires *consistent scoring across submissions* — non-determinism from agent trajectories would hurt that. The one place autonomy genuinely helps is the Playwright validator, where the LLM plans safe interactions from unknown page layouts.

**Evidence before scoring, always.** Every `RubricScore` carries `citations` (list of `source_id` strings). The scorer retrieves filtered evidence per criterion before generating any score. No retrieval = score of 1 with confidence 0.2.

**Source-typed retrieval.** `Implementation Quality` retrieves from `code` + `url` evidence. `Communication & Demo Clarity` retrieves from `deck` + `video`. This prevents marketing claims from inflating implementation scores.

**One collection per submission.** Clean isolation, easy to re-run with fresh state, and reviewers can inspect the Chroma data directly.

**Open-weight LLM (Llama 3.1 8B via Groq).** Free tier, fast enough to run the full pipeline (~40-50 LLM calls) in under 2 minutes. Chosen over the 70B variant for its far more generous free-tier daily quota (500K tokens / 14.4K requests vs 100K / 1K) — see `.env.example` for other model options.

---

## Project layout

```
app/
├── core/
│   ├── models.py         # Evidence, ClaimValidation, SubmissionReport, ...
│   ├── config.py         # All tunables in one place
│   └── unifier.py        # Builds the 'single source of truth'
├── llm/client.py         # ONE place to swap LLM provider
├── extractors/           # One module per source type
│   ├── deck.py
│   ├── video.py
│   ├── code.py
│   └── prototype.py      # Playwright + LLM-guided exploration
├── storage/vector_store.py  # Chroma wrapper with metadata filters
├── validators/claim_validator.py  # Cross-reference logic (core IP)
├── scoring/rubric_scorer.py       # Evidence-first scoring
├── pipeline.py           # Main orchestrator
└── ui/streamlit_chat.py  # Dashboard + chat UI
```

---

## Example output

```json
{
  "submission_id": "S_a3b1c2d4",
  "summary": "AI-powered customer feedback analyzer. Overall score: 3.8/5 across 5 criteria.",
  "unified_submission": {
    "problem": "Teams drown in unstructured customer feedback...",
    "solution": "Classifies and summarizes reviews using an LLM...",
    "claimed_features": ["Sentiment scoring", "Slack alerts", "CSV export"],
    "tech_stack": ["Python", "FastAPI", "OpenAI", "Slack SDK"]
  },
  "prototype_validation": {
    "url": "https://app.example.com",
    "accessible": true,
    "features_tested": [
      {"feature": "Sentiment scoring", "status": "working", "evidence": "..."},
      {"feature": "Slack alerts", "status": "not_found", "evidence": "..."}
    ]
  },
  "claim_validation": {
    "verified": 14, "partial": 5, "unsupported": 3, "contradicted": 1,
    "details": [
      {
        "claim": "ML-based sentiment scoring",
        "status": "contradicted",
        "reasoning": "Deck claims ML model; code uses rule-based keyword matching (src/scorer.py)"
      }
    ]
  },
  "scores": [
    {
      "criterion": "Technical Approach",
      "score": 4,
      "reasoning": "Clear architectural decisions visible in src/api.py and src/pipeline.py...",
      "citations": ["slide_5", "src/api.py", "src/pipeline.py"],
      "confidence": 0.82
    }
  ]
}
```

---

## Handling edge cases

- **Missing artefacts**: pipeline skips the source, emits a warning, proceeds with what's available
- **Broken prototype URL**: recorded as evidence (`url_load` with `accessible=False`), scoring continues
- **Huge repos**: capped at 30 files, size-filtered, prioritized by importance (README, entry points first)
- **Non-JSON LLM responses**: retry + fallback-parse + graceful empty return
- **Rate limits**: `tenacity` retries with exponential backoff

---

## Deploy your own (Hugging Face Spaces)

This repo includes a `Dockerfile` ready for the [Hugging Face Spaces](https://huggingface.co/spaces) Docker SDK.

```bash
# 1. Create a new Space at huggingface.co/new-space — SDK: Docker
# 2. Point a remote at it and push this repo
git remote add space https://huggingface.co/spaces/<username>/<space-name>
git push space main
# 3. In the Space's Settings → "Variables and secrets", add GROQ_API_KEY
#    (get a free key at https://console.groq.com)
```

The Space builds the image, installs Chromium for the Playwright prototype validator, and serves the Streamlit dashboard on port 7860.

**Known limitations of the public demo**:
- Groq's free tier is rate-limited to ~25 requests/minute, shared across every visitor — heavy concurrent use will slow evaluations down.
- The free Spaces tier has ephemeral storage: the Chroma DB resets on every Space restart. This is fine here since each pipeline run resets its own submission's collection anyway.

## What I'd add next (out of MVP scope)

- **Whisper integration** for direct video file support (transcripts only for now)
- **Screenshot-based visual validation** — feed prototype screenshots to vision model
- **Multi-run consistency check** — run the pipeline 3× on the same submission, report variance
- **Batch evaluation** — score 10+ submissions and rank them
- **Human-in-the-loop override** — reviewer can flag a score, pipeline incorporates the feedback into retrieval weighting

---

## Design philosophy in one line

> Every score earns its way up from retrieved evidence. If we can't cite it, we can't score it.
