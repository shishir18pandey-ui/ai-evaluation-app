# AI Evidence Layer - Complete Architecture & Flow Guide

## Quick Overview

This application evaluates AI/tech project submissions by analyzing **4 input sources** and producing **evidence-grounded scores** with citations.

**Key Philosophy:** Every score must earn its way up from retrieved evidence. If we can't cite it, we can't score it.

---

## Data Flow: From Input to Output

```
INPUT PHASE
-----------
    Deck (PDF/PPTX)         Transcript (text)       Code Repo (git URL)      Prototype URL
    ↓                        ↓                       ↓                        ↓

EXTRACTION PHASE (Optimized: 1 LLM call per source)
-----------
    extract_from_deck()      extract_from_            extract_from_repo()    validate_prototype()
    → Claims per slide       transcript()             → Capabilities         → Feature testing
    → Evidence list          → Demo vs claimed        → Implementation        → Working/broken
                             → Evidence list         → Evidence list         → Evidence list

STORAGE PHASE
-----------
    All Evidence → Chroma Vector Store (one collection per submission)
    ├── Text: claim + evidence snippet
    ├── Metadata: source_type, source_id, confidence
    └── Embedding: for semantic search

UNIFICATION PHASE
-----------
    build_unified_submission() → Merge all 4 sources into ONE narrative
    Returns: UnifiedSubmission {
        problem: "what problem is solved",
        solution: "how does it solve it",
        claimed_features: ["feature A", "feature B", ...],
        tech_stack: ["tech 1", "tech 2", ...],
        implementation_depth: "assessment"
    }

VALIDATION PHASE
-----------
    validate_claims() → Cross-reference each claim against evidence
    For each claim:
    1. Classify: planned | subjective | unverifiable | verifiable
    2. Retrieve related evidence from store
    3. Batch judge evidence in 1 LLM call
    4. Decide status: verified | contradicted | partial | unsupported | planned
    
    Returns: ClaimValidation[] with supporting/contradicting evidence

SCORING PHASE
-----------
    score_rubric() → Score on 5 criteria (1-5 scale)
    For each criterion:
    1. Retrieve filtered evidence (source-appropriate)
    2. LLM scores with reasoning
    3. Collect citations
    4. Return RubricScore with confidence
    
    Returns: RubricScore[]

OUTPUT PHASE
-----------
    SubmissionReport {
        submission_id,
        summary,
        unified_submission,
        prototype_validation,
        claim_validations,
        scores (with citations & confidence),
        created_at
    }
    ↓
    JSON file + Chainlit UI display
```

---

## Module Breakdown

### **1. CORE MODELS** (`app/core/models.py`)

Defines all data structures used throughout the pipeline.

| Class | Purpose | Fields |
|-------|---------|--------|
| **Evidence** | Atomic unit of information | source_type, source_id, claim_or_fact, evidence_text, confidence, metadata |
| **ClaimValidation** | Result of validating one claim | claim, status, supporting_evidence[], contradicting_evidence[], reasoning |
| **PrototypeValidation** | Result of testing live prototype | url, accessible, page_title, features_tested[], errors[], screenshots[] |
| **UnifiedSubmission** | Single source of truth from all sources | problem, solution, claimed_features[], tech_stack[], implementation_depth, sources |
| **RubricScore** | Score on one criterion | criterion, score (1-5), reasoning, citations[], confidence |
| **SubmissionReport** | Final output JSON | submission_id, summary, unified_submission, prototype_validation, claim_validations[], scores[], created_at |

**Key Insight:** Every object carries source metadata for traceability. No "magic" data without an audit trail.

---

### **2. VECTOR STORE** (`app/storage/vector_store.py`)

Wraps Chroma (open-source vector database) with metadata filtering.

**Class:** `EvidenceStore(submission_id)`

Methods:
- `add(evidence_list)` — store Evidence items with embeddings
- `query(query_text, k=6, source_types=None)` — retrieve top-k similar evidence, optionally filtered by source
- `count_by_source()` — sanity check: how many items per source
- `reset()` — wipe and recreate collection

**Design Decision:** One collection per submission = clean isolation + easy to replay analysis.

**Metadata Filtering:** Scoring "Implementation Quality" retrieves only `code` + `url` evidence (not marketing claims from deck).

---

### **3. EXTRACTORS** (`app/extractors/`)

Each extractor handles one input source type. **All optimized to 1 LLM call per source.**

#### **3a. Deck Extractor** (`deck.py`)

- Reads PDF or PPTX
- Labels each slide by number
- **1 LLM call** to extract ALL claims from the entire deck
- Returns: `list[Evidence]` with `source_id="slide_N"`

**Key:** Instead of calling LLM for each slide (8-15 calls), we batch all slides into ONE call.

#### **3b. Transcript Extractor** (`video.py`)

- Parses demo transcript (text)
- Chunks by natural boundaries
- Extracts: demonstrated features, claimed features, hedged claims
- **1 LLM call** for entire transcript
- Returns: `list[Evidence]` with `source_id="video_MM:SS"`

**Distinction:** "Demonstrated" (user actually did it) vs "claimed" (narrator said but didn't show).

#### **3c. Code Extractor** (`code.py`)

- Clones git repo OR reads local directory
- Selects important files (README, main entry points, config)
- **1 LLM call** per file to analyze what it implements
- Returns: `list[Evidence]` with `source_id="path/to/file.py"`

**Principle:** List only capabilities with clear code evidence. No speculation about "might be".

#### **3d. Prototype Extractor** (`prototype.py`)

- Opens live URL with Playwright
- LLM guides click sequences to test claimed features
- Records: working, broken, not_found status
- Screenshots captured for review
- Returns: `PrototypeValidation` + `list[Evidence]` with `source_id="url_feature_name"`

**Special Role:** URL evidence is "ground truth" — if prototype shows feature doesn't work, it overrides other sources.

---

### **4. LLM CLIENT** (`app/llm/client.py`)

Single abstraction layer over the model provider (currently Groq + Llama 3.3 70B).

Functions:
- `llm_complete(system, user, json_mode=False)` — text response
- `llm_json(system, user)` — structured JSON response
- `get_client()` — lazy-init Groq client
- Rate limiting (25 RPM for Groq free tier)
- Retry logic with exponential backoff

**Why One Place:** Easy to swap to OpenAI, Anthropic, or local Ollama. Just change provider here.

---

### **5. UNIFIER** (`app/core/unifier.py`)

Merges evidence from all 4 sources into a single narrative.

Function: `build_unified_submission(store: EvidenceStore) -> UnifiedSubmission`

Process:
1. Query store for diverse evidence across problem, architecture, features, implementation
2. Group by source type
3. Send all evidence to LLM
4. LLM synthesizes: problem statement, solution, features, tech stack, depth

**Why Separate:** Unification is deterministic once evidence is collected. Avoids redundant LLM calls.

---

### **6. CLAIM VALIDATOR** (`app/validators/claim_validator.py`)

Cross-references claims against evidence with **strict source roles**.

Function: `validate_claims(claims: list[str], store: EvidenceStore) -> list[ClaimValidation]`

#### **Claim Classification**

| Type | Pattern | Handling |
|------|---------|----------|
| **planned** | "will be", "future", "roadmap", "coming soon" | Exempt from contradiction |
| **subjective** | "clean", "elegant", "intuitive", "user-friendly" | Unverifiable (not DOM-searchable) |
| **unverifiable** | "built with X", "uses Y framework", tech stack | Only verify from code |
| **verifiable** | Feature claims, functional capabilities | Test against code + url |

#### **Source Roles (Enforced)**

- **deck** → CLAIMS ONLY (never validates itself)
- **video** → SUPPORTING evidence (demo narration)
- **code** → IMPLEMENTATION proof
- **url** → GROUND TRUTH (highest priority, overrides others)

#### **Status Decision Logic**

```
IF planned → "planned" (no penalty)
IF subjective → "unverifiable"
IF url contradicts → "contradicted" (strongest signal)
IF code contradicts → "contradicted"
IF url supports → "verified" (ground truth)
IF code + video support → "verified" (both implementation + demo)
IF code only → "verified" (implementation level)
IF video only → "unsupported" (narrated but not implemented/demoed)
IF nothing → "unsupported"
```

**Optimization:** Batch judge all evidence for one claim in 1 LLM call (not 6 calls).

---

### **7. RUBRIC SCORER** (`app/scoring/rubric_scorer.py`)

Scores submission on 5 criteria (1-5 scale), evidence-first.

Function: `score_rubric(store, unified_submission, validations) -> list[RubricScore]`

#### **5 Criteria & Source Weighting**

| Criterion | Retrieves From | Why |
|-----------|-----------------|-----|
| **Problem Understanding** | deck + video | How clearly is the problem articulated? |
| **Technical Approach** | deck + code | Is the architecture sound? |
| **Implementation Quality** | code + url | What actually works? |
| **Innovation / Originality** | deck + code | Is it novel? |
| **Communication & Demo Clarity** | deck + video | Are they clear in explaining? |

#### **Scoring Scale**

1 = Absent or deeply flawed
2 = Below expectations (significant gaps)
3 = Adequate / meets basics (MVP-level)
4 = Strong / above average
5 = Exceptional

#### **Critical Rule**

**No evidence = no score.** If `Implementation Quality` has zero code evidence, score = 1 with confidence = 0.2.

Each score includes:
- `citations`: list of source_ids that informed the score
- `confidence`: 0-1 reflecting evidence quality/quantity
- `reasoning`: why this score was given

---

### **8. PIPELINE ORCHESTRATOR** (`app/pipeline.py`)

Runs all 9 steps in deterministic order.

Function: `run_pipeline(inputs: SubmissionInput, on_progress: ProgressFn) -> SubmissionReport`

**Steps:**
1. Extract from deck
2. Extract from transcript
3. Extract from code
4. Store all evidence in vector DB
5. Build unified submission
6. Validate prototype URL (if provided)
7. Cross-reference claims
8. Score against rubric
9. Generate final report

**Progress Callbacks:** Each step calls `on_progress(step_name, status, detail)` for UI updates.

**Error Handling:** If a source fails, pipeline skips it with warning and continues. Robust to missing inputs.

---

### **9. USER INTERFACES**

#### **9a. Chainlit UI** (`app/ui/chainlit_app.py`)

Chat-based interactive interface. Users ask questions about the submission, retrieve evidence on-demand.

#### **9b. Streamlit UI** (`app/ui/streamlit_chat.py`)

Alternative dashboard interface for reviewing submission scores and evidence.

---

## Configuration

File: `app/core/config.py`

**All tunables in ONE place:**

- `GROQ_API_KEY` — LLM provider credential
- `LLM_MODEL`, `LLM_TEMPERATURE` — LLM parameters
- `CHROMA_DIR` — vector DB persistence path
- `EMBEDDING_MODEL` — sentence-transformers model
- `MAX_SLIDES_TO_PROCESS`, `MAX_CODE_FILES` — size limits
- `EVIDENCE_TOP_K` — default retrieval limit

---

## Example: Walking Through One Submission

```
USER PROVIDES:
├── deck.pdf (10 slides)
├── demo_transcript.txt (2000 words)
├── repo_url: https://github.com/user/project
└── prototype_url: https://app.example.com

PIPELINE EXECUTION:

1. extract_from_deck()
   → Reads PDF, labels each slide
   → 1 LLM call: "Extract all claims"
   → Returns: 12 Evidence items
      slide_1: "Problem: teams drown in feedback"
      slide_3: "Feature: sentiment scoring with ML"
      slide_5: "Tech: Python + FastAPI + OpenAI"

2. extract_from_transcript()
   → Chunks transcript (2000 words → ~1500 char chunks)
   → 1 LLM call: "Separate demonstrated vs claimed"
   → Returns: 8 Evidence items
      video_00:42: "Demonstrated: login flow works"
      video_03:15: "Claimed: Slack integration"
      video_05:00: "Hedged: mobile app (coming soon)"

3. extract_from_repo()
   → Clones repo, finds important files
   → 1 LLM call per file (~10 files)
   → Returns: 15 Evidence items
      src/sentiment.py: "Implements: rule-based keyword matching"
      src/api.py: "Implements: /analyze endpoint"
      src/integrations/slack.py: "Stub only, not implemented"

4. validate_prototype()
   → Opens https://app.example.com
   → Tests: sentiment scoring, Slack alerts
   → Returns: PrototypeValidation
      sentiment_scoring: working
      slack_alerts: not_found
   → 2 Evidence items added

5. Vector Store
   → All 37 Evidence items stored in Chroma
   → Metadata indexed by source_type

6. build_unified_submission()
   → Queries store: problem, architecture, features, implementation
   → Groups by source
   → 1 LLM call to synthesize
   → Returns:
      problem: "Teams struggle to analyze customer feedback at scale"
      solution: "Automated multi-source feedback classifier"
      claimed_features: ["sentiment scoring", "slack alerts", "csv export"]
      tech_stack: ["Python", "FastAPI", "OpenAI"]

7. validate_claims()
   → Validates: "sentiment scoring with ML", "Slack integration", ...
   → For "sentiment scoring":
      - Retrieved code evidence shows rule-based, not ML
      - Deck claims ML, code shows keyword matching
      → Status: "contradicted"
      → Citations: ["slide_3", "src/sentiment.py"]
   
   → For "Slack integration":
      - Code shows stub only
      - Demo narrates feature but doesn't show it
      - Prototype shows not_found
      → Status: "contradicted"
      → Citations: ["src/integrations/slack.py", "url_test_slack_alerts"]

8. score_rubric()
   → Problem Understanding: Score 4/5 (clearly articulated in deck + demo)
     Citations: ["slide_1", "video_00:15"]
   
   → Technical Approach: Score 2/5 (architecture not clearly documented, ML claim contradicted)
     Citations: ["src/api.py", "src/sentiment.py"]
   
   → Implementation Quality: Score 3/5 (basic features work, but several contradictions)
     Citations: ["url_sentiment_scoring", "src/api.py"]
   
   → Innovation: Score 2/5 (uses standard tools without novel twist)
     Citations: ["slide_4", "src/sentiment.py"]
   
   → Communication Clarity: Score 4/5 (demo is clear, but hedges on Slack)
     Citations: ["demo transcript", "video_05:00"]
   
   → Average: 3.0/5

9. Output SubmissionReport
   {
     "submission_id": "S_a3b1c2d4",
     "summary": "Feedback analyzer. Overall score: 3.0/5.",
     "unified_submission": { ... },
     "claim_validations": [
       {
         "claim": "Sentiment scoring with ML",
         "status": "contradicted",
         "supporting_evidence": [...],
         "contradicting_evidence": [
           {
             "source_id": "src/sentiment.py",
             "source_type": "code",
             "claim_or_fact": "Rule-based keyword matching"
           }
         ],
         "reasoning": "[verifiable] Code does not implement the claimed feature."
       },
       ...
     ],
     "scores": [
       {
         "criterion": "Implementation Quality",
         "score": 3,
         "reasoning": "Basic features work...",
         "citations": ["url_sentiment_scoring", "src/api.py"],
         "confidence": 0.78
       },
       ...
     ]
   }
```

---

## Design Decisions & Why

### 1. Single Deterministic Pipeline (Not Multi-Agent)
**Why:** CrewAI/AutoGen were considered but rejected.
- Data flow is deterministic (no autonomous planning needed)
- Rubric requires consistent scoring across submissions
- Non-determinism from agent trajectories hurts consistency

### 2. Evidence-First Scoring, Always
**Why:** Assignments require citations.
- Every `RubricScore` carries `citations`
- Scorer retrieves filtered evidence before generating any score
- No evidence = score of 1 with confidence 0.2

### 3. Source-Typed Retrieval
**Why:** Prevent misuse of evidence types.
- "Implementation Quality" retrieves only `code` + `url` (not deck marketing)
- "Problem Understanding" retrieves `deck` + `video` (not code docs)

### 4. One Collection per Submission
**Why:** Isolation + reproducibility.
- Clean per-submission scope
- Easy to re-run with fresh state
- Reviewers can inspect Chroma data directly

### 5. Batched LLM Calls (Not Sequential)
**Why:** Speed + token efficiency.
- Deck: 1 call for all claims (not 1 per slide)
- Code: 1 call per file (not 1 per function)
- Validator: 1 call for all evidence on claim (not 6 calls)

### 6. Open-Weight LLM (Llama 3.3 via Groq)
**Why:** Free tier, fast, reproducible.
- Full pipeline runs in ~2 minutes
- 40-50 LLM calls total (reasonable cost)
- No rate limits for most production deployments

---

## How to Extend

### Add a New Rubric Criterion

1. Add to `CRITERION_SOURCE_WEIGHTS` in `rubric_scorer.py`
2. Add to `CRITERION_QUERIES` (what to retrieve)
3. Add to `CRITERION_GUIDANCE` (how to score it)
4. Scorer will automatically retrieve + score

### Add a New Evidence Source

1. Create `app/extractors/newsource.py` with `extract_from_newsource() -> list[Evidence]`
2. Add to `run_pipeline()` in `pipeline.py`
3. Evidence flows through existing validator + scorer automatically

### Swap LLM Provider

1. Edit `app/llm/client.py`
2. Change `Groq()` to `OpenAI()` or local `Ollama()`
3. Rest of pipeline unchanged

---

## Edge Cases & Handling

| Case | Handling |
|------|----------|
| Missing deck | Pipeline skips, emits warning, continues |
| Broken repo URL | Logged as failure, pipeline continues |
| Huge repo (10k files) | Capped at 30 files, size-filtered |
| Non-JSON LLM response | Retry + fallback-parse + empty dict |
| Rate limits | Tenacity retries with exponential backoff |
| Unfound prototype URL | Recorded as `accessible=False`, scoring continues |

---

## Next Steps for Production

- [ ] Whisper integration for direct video file support (transcripts only for now)
- [ ] Screenshot-based visual validation (feed prototype screenshots to vision model)
- [ ] Multi-run consistency check (run pipeline 3× on same submission, report variance)
- [ ] Batch evaluation (score 10+ submissions, rank them)
- [ ] Human-in-the-loop override (reviewer flags score, pipeline reweights evidence)

---

**Design Philosophy in One Line:**
> Every score earns its way up from retrieved evidence. If we can't cite it, we can't score it.
