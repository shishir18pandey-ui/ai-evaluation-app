"""
Chainlit UI for the AI Evidence Layer.

Flow:
1. Greet user, collect inputs (deck file, repo URL, optional transcript, optional prototype URL)
2. Run pipeline with live progress via cl.Step
3. Show rich final report + download JSON
"""
import json
import logging
import uuid
from pathlib import Path

import chainlit as cl

from app.pipeline import SubmissionInput, run_pipeline
from app.core import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


# ─── Helpers ──────────────────────────────────────────────────────────
def _format_report_markdown(report) -> str:
    if not report.unified_submission:
        return f"**Evaluation failed**: {report.summary}"

    us = report.unified_submission
    avg = sum(s.score for s in report.scores) / len(report.scores) if report.scores else 0

    lines = [
        f"## Evaluation Complete — {report.submission_id}",
        f"\n**Overall**: {avg:.1f} / 5.0",
        f"\n### Summary",
        f"**Problem**: {us.problem}",
        f"**Solution**: {us.solution}",
        f"**Implementation depth**: {us.implementation_depth}",
    ]
    if us.tech_stack:
        lines.append(f"**Tech stack**: {', '.join(us.tech_stack)}")

    # Prototype
    if report.prototype_validation:
        pv = report.prototype_validation
        lines.append("\n### Prototype validation")
        if pv.accessible:
            lines.append(f"- URL: {pv.url} — **accessible** (title: _{pv.page_title}_)")
            for ft in pv.features_tested:
                emoji = {"working": "✓", "broken": "✗", "not_found": "?", "not_tested": "—"}.get(ft["status"], "—")
                lines.append(f"  - {emoji} **{ft['feature']}**: {ft['status']} — {ft.get('evidence', '')[:150]}")
        else:
            lines.append(f"- URL: {pv.url} — **not accessible**. {'; '.join(pv.errors)}")

    # Claims
    lines.append("\n### Claim validation")
    counts = {"verified": 0, "partial": 0, "unsupported": 0, "contradicted": 0}
    for v in report.claim_validations:
        counts[v.status] = counts.get(v.status, 0) + 1
    lines.append(f"- Verified: **{counts['verified']}** · Partial: **{counts['partial']}** "
                 f"· Unsupported: **{counts['unsupported']}** · Contradicted: **{counts['contradicted']}**")

    flagged = [v for v in report.claim_validations if v.status in ("contradicted", "unsupported")]
    if flagged:
        lines.append("\n**Flagged claims**:")
        for v in flagged[:6]:
            lines.append(f"- _{v.claim[:100]}_ — **{v.status}**: {v.reasoning}")

    # Rubric scores
    lines.append("\n### Rubric scores")
    for s in report.scores:
        bar = "█" * s.score + "░" * (5 - s.score)
        lines.append(f"- **{s.criterion}** — {bar} **{s.score}/5** (conf {s.confidence:.0%})")
        lines.append(f"  - _{s.reasoning}_")
        if s.citations:
            lines.append(f"  - Citations: `{'`, `'.join(s.citations[:5])}`")

    return "\n".join(lines)


# ─── Chainlit handlers ─────────────────────────────────────────────────
@cl.on_chat_start
async def start():
    await cl.Message(
        author="Evidence Layer",
        content=(
            "### AI Evidence Layer\n"
            "I evaluate project submissions by cross-referencing claims across the deck, demo, code, and live prototype.\n\n"
            "**To begin**, upload a deck (PDF or PPTX) below. I'll then ask for repo URL, transcript, and prototype URL."
        ),
    ).send()

    # Step 1: Deck file upload
    files = await cl.AskFileMessage(
        content="Upload your pitch deck (PDF or PPTX).",
        accept=["application/pdf",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"],
        max_size_mb=20,
        timeout=300,
    ).send()

    deck_path = None
    if files:
        f = files[0]
        deck_path = str(Path(config.UPLOAD_DIR) / f.name)
        Path(deck_path).write_bytes(Path(f.path).read_bytes())

    # Step 2: Repo URL
    repo_res = await cl.AskUserMessage(
        content="Paste the **git repo URL** (public). Or type `skip`.",
        timeout=300,
    ).send()
    repo_url = None
    if repo_res and repo_res["output"].strip().lower() != "skip":
        repo_url = repo_res["output"].strip()

    # Step 3: Transcript
    transcript_res = await cl.AskUserMessage(
        content="Paste the **demo transcript** (or type `skip`).",
        timeout=300,
    ).send()
    transcript_text = None
    if transcript_res and transcript_res["output"].strip().lower() != "skip":
        transcript_text = transcript_res["output"]

    # Step 4: Prototype URL
    url_res = await cl.AskUserMessage(
        content="Paste the **prototype URL** (optional, type `skip` to skip).",
        timeout=300,
    ).send()
    prototype_url = None
    if url_res and url_res["output"].strip().lower() != "skip":
        prototype_url = url_res["output"].strip()

    # Check we have something to work with
    if not deck_path and not repo_url and not transcript_text:
        await cl.Message(
            content="No artefacts provided. Please refresh and try again.",
        ).send()
        return

    # Build inputs and run pipeline
    inputs = SubmissionInput(
        submission_id=f"S_{uuid.uuid4().hex[:8]}",
        deck_path=deck_path,
        transcript_text=transcript_text,
        repo_url_or_path=repo_url,
        prototype_url=prototype_url,
    )

    await _run_with_progress(inputs)


async def _run_with_progress(inputs: SubmissionInput):
    """Run pipeline with live Chainlit step updates."""

    # Collect progress events, render as steps
    active_steps = {}

    def _sync_progress(step_name, status, detail):
        # Bridge sync callback to Chainlit — we'll use a simple message updates
        # since cl.Step has async contracts. Post as messages for simplicity.
        pass  # actual progress handled below via explicit async steps

    # Instead of relying on sync callbacks bridging to async, we just run
    # the pipeline and render after. For real-time progress, each major step
    # could be made async and awaited individually — keeping the MVP clean.
    msg = cl.Message(content="⏳ Running evaluation pipeline... this takes 1-3 minutes.")
    await msg.send()

    progress_log = []

    def collect(step_name, status, detail):
        symbol = {"start": "⟳", "ok": "✓", "warn": "!", "fail": "✗"}.get(status, "·")
        progress_log.append(f"{symbol} **{step_name}** — {detail}")

    try:
        # Run pipeline in a thread so UI stays responsive
        import asyncio
        loop = asyncio.get_event_loop()
        report = await loop.run_in_executor(
            None, run_pipeline, inputs, collect
        )
    except Exception as e:
        logging.exception("Pipeline crashed")
        await cl.Message(content=f"❌ Pipeline error: {e}").send()
        return

    # Show progress trace
    await cl.Message(content="### Pipeline trace\n" + "\n".join(progress_log)).send()

    # Show final report
    await cl.Message(content=_format_report_markdown(report)).send()

    # Attach JSON output
    json_data = json.dumps(report.to_json_dict(), indent=2, default=str)
    json_file = Path(config.UPLOAD_DIR) / f"report_{inputs.submission_id}.json"
    json_file.write_text(json_data)

    await cl.Message(
        content="### Full JSON report",
        elements=[cl.File(name=f"report_{inputs.submission_id}.json",
                           path=str(json_file),
                           display="inline")],
    ).send()
