"""
Streamlit UI: hybrid dashboard + chat for the AI Evidence Layer.

Layout:
- Sidebar: file uploads + input fields + "Start Evaluation" button
- Main area: two tabs
    1. Dashboard — structured evaluation report (cards, scores, flags)
    2. Chat with evidence — RAG over the same Chroma store
"""
import sys
import json
import uuid
import logging
from pathlib import Path

import streamlit as st

# Make project root importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.pipeline import SubmissionInput, run_pipeline
from app.core import config
from app.core.chat import answer_question
from app.storage import EvidenceStore

logging.basicConfig(level=logging.INFO)


# ═══════════════════════════════════════════════════════════════════
# Page config + global styles
# ═══════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="AI Evidence Layer",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS — tightens Streamlit's default spacing, card look
st.markdown("""
<style>
    /* Tighten top padding */
    .block-container { padding-top: 2rem; padding-bottom: 2rem; }

    /* Card style */
    .evidence-card {
        background: #fafafa;
        border: 1px solid #e5e7eb;
        border-radius: 8px;
        padding: 16px 20px;
        margin-bottom: 12px;
    }
    .card-title {
        font-size: 14px;
        font-weight: 600;
        color: #374151;
        margin-bottom: 10px;
        display: flex;
        align-items: center;
        gap: 8px;
    }
    .status-pill {
        display: inline-block;
        padding: 2px 10px;
        border-radius: 12px;
        font-size: 12px;
        font-weight: 500;
    }
    .status-working { background: #d1fae5; color: #065f46; }
    .status-broken { background: #fee2e2; color: #991b1b; }
    .status-partial { background: #fef3c7; color: #92400e; }
    .status-verified { background: #d1fae5; color: #065f46; }
    .status-contradicted { background: #fee2e2; color: #991b1b; }
    .status-unsupported { background: #fef3c7; color: #92400e; }

    /* Score bar */
    .score-row { display: flex; align-items: center; gap: 12px; margin: 6px 0; }
    .score-bar-bg {
        flex: 1; height: 8px; background: #e5e7eb;
        border-radius: 4px; overflow: hidden;
    }
    .score-bar-fill { height: 100%; }

    /* Header */
    .app-header {
        background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
        color: white; padding: 16px 24px; border-radius: 8px;
        margin-bottom: 24px;
        display: flex; align-items: center; gap: 12px;
    }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════
# Session state
# ═══════════════════════════════════════════════════════════════════
if "report" not in st.session_state:
    st.session_state.report = None
if "submission_id" not in st.session_state:
    st.session_state.submission_id = None
if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []
if "progress_log" not in st.session_state:
    st.session_state.progress_log = []


# ═══════════════════════════════════════════════════════════════════
# Header
# ═══════════════════════════════════════════════════════════════════
st.markdown("""
<div class="app-header">
    <div style="font-size: 24px;">🔍</div>
    <div>
        <div style="font-size: 20px; font-weight: 600;">AI Evidence Layer</div>
        <div style="font-size: 13px; opacity: 0.8;">Evaluate submissions by cross-referencing claims across artefacts</div>
    </div>
</div>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════
# Sidebar — Inputs
# ═══════════════════════════════════════════════════════════════════
with st.sidebar:
    st.header("📦 Upload Submission")
    st.caption("Provide artefacts for evaluation. At least one required.")

    deck_file = st.file_uploader(
        "Pitch Deck (PDF / PPTX)",
        type=["pdf", "pptx", "ppt"],
        help="The submission's slide deck",
    )

    transcript_file = st.file_uploader(
        "Demo Transcript (text file)",
        type=["txt", "md"],
        help="Paste transcript as a .txt file. Video → Whisper coming soon.",
    )
    transcript_paste = st.text_area(
        "...or paste transcript here",
        height=100,
        placeholder="Paste the demo transcript...",
    )

    repo_url = st.text_input(
        "Git Repository URL",
        placeholder="https://github.com/user/project",
    )

    prototype_url = st.text_input(
        "Prototype URL (optional)",
        placeholder="https://app.example.com",
    )

    st.divider()

    start_button = st.button(
        "▶ Start Evaluation",
        type="primary",
        use_container_width=True,
        disabled=not (deck_file or transcript_file or transcript_paste or repo_url),
    )

    if st.session_state.report:
        st.divider()
        st.caption(f"Session: `{st.session_state.submission_id}`")
        if st.button("🗑 Clear & start new", use_container_width=True):
            st.session_state.report = None
            st.session_state.submission_id = None
            st.session_state.chat_messages = []
            st.session_state.progress_log = []
            st.rerun()


# ═══════════════════════════════════════════════════════════════════
# Evaluation — triggered by button
# ═══════════════════════════════════════════════════════════════════
def _save_upload(uploaded_file) -> str:
    """Save uploaded file to disk, return path."""
    path = Path(config.UPLOAD_DIR) / uploaded_file.name
    path.write_bytes(uploaded_file.getbuffer())
    return str(path)


if start_button:
    # Save uploads
    deck_path = _save_upload(deck_file) if deck_file else None

    transcript_text = transcript_paste or None
    if transcript_file and not transcript_text:
        transcript_text = transcript_file.getvalue().decode("utf-8", errors="ignore")

    submission_id = f"S_{uuid.uuid4().hex[:8]}"
    st.session_state.submission_id = submission_id
    st.session_state.progress_log = []

    inputs = SubmissionInput(
        submission_id=submission_id,
        deck_path=deck_path,
        transcript_text=transcript_text,
        repo_url_or_path=repo_url or None,
        prototype_url=prototype_url or None,
    )

    # Progress UI
    progress_placeholder = st.empty()

    def on_progress(step: str, status: str, detail: str):
        emoji = {"start": "⏳", "ok": "✅", "warn": "⚠️", "fail": "❌"}.get(status, "•")
        line = f"{emoji} **{step}** — {detail}"
        st.session_state.progress_log.append(line)
        with progress_placeholder.container():
            st.info("**Running pipeline...**\n\n" + "\n\n".join(st.session_state.progress_log))

    try:
        with st.spinner("Evaluating submission... this takes 1-3 minutes."):
            report = run_pipeline(inputs, on_progress=on_progress)
        st.session_state.report = report
        progress_placeholder.empty()

        # Surface any per-source failures (e.g. rate limits) instead of a blanket
        # green "complete" — a source that failed silently would otherwise look
        # identical to one that genuinely had nothing to extract.
        failed_steps = [ln for ln in st.session_state.progress_log if ln.startswith("❌")]
        if failed_steps:
            st.warning(
                "⚠️ Evaluation finished, but some steps failed (often API rate limits — "
                "try again in a minute):\n\n" + "\n\n".join(failed_steps)
            )
        else:
            st.success(f"✅ Evaluation complete — submission `{submission_id}`")
    except Exception as e:
        st.error(f"Pipeline failed: {e}")
        logging.exception("Pipeline error")


# ═══════════════════════════════════════════════════════════════════
# Main area — tabs
# ═══════════════════════════════════════════════════════════════════
if not st.session_state.report:
    # Landing state — welcome the user
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("""
        <div style="text-align: center; padding: 60px 20px; color: #6b7280;">
            <div style="font-size: 56px;">📊</div>
            <div style="font-size: 18px; font-weight: 500; color: #374151; margin-top: 12px;">
                Ready to evaluate a submission
            </div>
            <div style="font-size: 14px; margin-top: 8px;">
                Upload artefacts in the sidebar and click <b>Start Evaluation</b> to begin.
            </div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("#### How it works")
        st.markdown("""
        1. **Ingest** deck, transcript, code repo, and (optionally) a live prototype URL
        2. **Extract** structured evidence from each source using an LLM
        3. **Validate** the prototype with Playwright — actual clicks, actual observations
        4. **Cross-reference** claims: flag anything in the deck that isn't backed by code or demo
        5. **Score** against 5 rubric criteria — every score earns its way up from retrieved evidence
        6. **Chat** with the evidence to dig deeper
        """)

else:
    report = st.session_state.report
    tab1, tab2, tab3 = st.tabs(["📊 Dashboard", "💬 Chat with evidence", "📄 JSON output"])

    # ─────────────────────────────────────────────────────────────
    # TAB 1: Dashboard
    # ─────────────────────────────────────────────────────────────
    with tab1:
        if not report.unified_submission:
            st.warning(
                "**No evidence could be extracted.** This usually means either the "
                "artefacts had no readable content, or the LLM calls were rate-limited "
                "(the free Groq tier has per-minute limits — wait a minute and retry). "
                "Check the pipeline steps above for any ❌ failures."
            )
        else:
            us = report.unified_submission
            avg_score = sum(s.score for s in report.scores) / len(report.scores) if report.scores else 0

            # Top metrics row
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                st.metric("Overall Score", f"{avg_score:.1f} / 5")
            with c2:
                verified = sum(1 for v in report.claim_validations if v.status == "verified")
                total = len(report.claim_validations)
                st.metric("Claims Verified", f"{verified} / {total}")
            with c3:
                if report.prototype_validation:
                    pv = report.prototype_validation
                    if pv.accessible:
                        working = sum(1 for f in pv.features_tested if f.get("status") == "working")
                        st.metric("Prototype", f"{working}/{len(pv.features_tested)} working")
                    else:
                        st.metric("Prototype", "Inaccessible")
                else:
                    st.metric("Prototype", "Not tested")
            with c4:
                st.metric("Features Identified", len(us.claimed_features))

            st.divider()

            # Summary card
            st.subheader("📋 Submission Summary")
            with st.container(border=True):
                st.markdown(f"**Problem**: {us.problem}")
                st.markdown(f"**Solution**: {us.solution}")
                st.markdown(f"**Implementation depth**: {us.implementation_depth}")
                if us.tech_stack:
                    st.markdown(f"**Tech stack**: {', '.join(us.tech_stack)}")

            # Two-column: Prototype + Evidence
            st.subheader("🔎 Validation Details")
            col_a, col_b = st.columns(2)

            # Prototype validation
            with col_a:
                with st.container(border=True):
                    st.markdown("#### 🌐 Prototype Validation")
                    if report.prototype_validation:
                        pv = report.prototype_validation
                        if pv.accessible:
                            all_working = all(f.get("status") == "working" for f in pv.features_tested)
                            any_working = any(f.get("status") == "working" for f in pv.features_tested)
                            if all_working:
                                st.markdown('<span class="status-pill status-working">✓ Fully working</span>', unsafe_allow_html=True)
                            elif any_working:
                                st.markdown('<span class="status-pill status-partial">⚠ Partially working</span>', unsafe_allow_html=True)
                            else:
                                st.markdown('<span class="status-pill status-broken">✗ Not working</span>', unsafe_allow_html=True)

                            st.caption(f"Title: _{pv.page_title}_")
                            for ft in pv.features_tested:
                                icon = {"working": "✅", "broken": "❌", "not_found": "🔍", "not_tested": "⏸"}.get(ft["status"], "•")
                                with st.expander(f"{icon} {ft['feature']}", expanded=False):
                                    st.caption(f"Status: **{ft['status']}**")
                                    st.text(ft.get("evidence", ""))
                        else:
                            st.error(f"❌ Not accessible: {'; '.join(pv.errors)}")
                    else:
                        st.info("No prototype URL provided")

            # Claim validation
            with col_b:
                with st.container(border=True):
                    st.markdown("#### 🔬 Claim Validation")
                    counts = {"verified": 0, "partial": 0, "unsupported": 0, "contradicted": 0}
                    for v in report.claim_validations:
                        counts[v.status] = counts.get(v.status, 0) + 1

                    c1, c2 = st.columns(2)
                    c1.metric("✓ Verified", counts["verified"])
                    c1.metric("⚠ Partial", counts["partial"])
                    c2.metric("? Unsupported", counts["unsupported"])
                    c2.metric("✗ Contradicted", counts["contradicted"])

                    flagged = [v for v in report.claim_validations
                                if v.status in ("contradicted", "unsupported")]
                    if flagged:
                        st.markdown("**🚩 Flagged claims**")
                        for v in flagged[:5]:
                            pill_class = f"status-{v.status}"
                            st.markdown(
                                f'<div style="margin: 8px 0;">'
                                f'<span class="status-pill {pill_class}">{v.status}</span> '
                                f'<span style="font-size: 13px;">{v.claim[:120]}</span>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )
                            st.caption(v.reasoning)

            # Rubric scores
            st.subheader("🎯 Rubric Scores")
            with st.container(border=True):
                for s in report.scores:
                    pct = (s.score / 5) * 100
                    color = "#10b981" if s.score >= 4 else "#f59e0b" if s.score >= 3 else "#ef4444"
                    st.markdown(f"""
                    <div class="score-row">
                        <div style="flex: 1; font-size: 14px;">{s.criterion}</div>
                        <div class="score-bar-bg">
                            <div class="score-bar-fill" style="width: {pct}%; background: {color};"></div>
                        </div>
                        <div style="min-width: 40px; font-weight: 600; text-align: right;">
                            {s.score}/5
                        </div>
                        <div style="min-width: 60px; font-size: 12px; color: #6b7280; text-align: right;">
                            {s.confidence:.0%} conf
                        </div>
                    </div>
                    """, unsafe_allow_html=True)
                    with st.expander(f"Why {s.score}/5?", expanded=False):
                        st.markdown(f"**Reasoning**: {s.reasoning}")
                        if s.citations:
                            citations_str = " · ".join(f"`{c}`" for c in s.citations[:6])
                            st.markdown(f"**Citations**: {citations_str}")

    # ─────────────────────────────────────────────────────────────
    # TAB 2: Chat with evidence
    # ─────────────────────────────────────────────────────────────
    with tab2:
        st.markdown("""
        💡 **Ask questions about this submission.** Answers are grounded in the evidence extracted
        from the deck, transcript, code, and prototype. Every answer cites its sources.
        """)

        # Example questions
        if not st.session_state.chat_messages:
            st.caption("Try asking:")
            example_cols = st.columns(3)
            examples = [
                "What are the main weaknesses?",
                "Does the code match the deck's claims?",
                "Is the prototype actually working?",
            ]
            for col, ex in zip(example_cols, examples):
                if col.button(ex, use_container_width=True):
                    st.session_state.chat_messages.append({"role": "user", "content": ex})
                    st.rerun()

        # Render chat history
        for msg in st.session_state.chat_messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                if msg.get("sources"):
                    st.caption(f"📎 Sources: {' · '.join(f'`{s}`' for s in msg['sources'][:6])}")

        # If last message is from user and unanswered, answer it
        if (st.session_state.chat_messages and
                st.session_state.chat_messages[-1]["role"] == "user"):
            with st.chat_message("assistant"):
                with st.spinner("Searching evidence..."):
                    try:
                        store = EvidenceStore(st.session_state.submission_id)
                        result = answer_question(
                            st.session_state.chat_messages[-1]["content"],
                            store,
                            conversation_history=st.session_state.chat_messages[:-1],
                        )
                        st.markdown(result["answer"])
                        if result["sources_used"]:
                            st.caption(f"📎 Retrieved {result['retrieved_count']} evidence items")
                        st.session_state.chat_messages.append({
                            "role": "assistant",
                            "content": result["answer"],
                            "sources": result["sources_used"],
                        })
                    except Exception as e:
                        st.error(f"Chat failed: {e}")

        # Chat input
        user_q = st.chat_input("Ask anything about this submission...")
        if user_q:
            st.session_state.chat_messages.append({"role": "user", "content": user_q})
            st.rerun()

    # ─────────────────────────────────────────────────────────────
    # TAB 3: Raw JSON
    # ─────────────────────────────────────────────────────────────
    with tab3:
        st.markdown("#### Raw evaluation output")
        st.caption("This is the structured JSON matching the assignment's output spec.")
        json_data = report.to_json_dict()
        st.json(json_data)

        json_str = json.dumps(json_data, indent=2, default=str)
        st.download_button(
            "⬇ Download JSON",
            data=json_str,
            file_name=f"report_{report.submission_id}.json",
            mime="application/json",
        )