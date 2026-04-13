"""
Quick smoke test — runs the pipeline from CLI without the UI.

Usage:
    python run_cli.py --deck path/to/deck.pdf --repo https://github.com/user/repo \\
                      --transcript path/to/transcript.txt --url https://app.example.com

At least one of --deck / --repo / --transcript is required.
"""
import argparse
import json
import logging
import sys
import uuid
from pathlib import Path

# Make app importable
sys.path.insert(0, str(Path(__file__).parent))

from app.pipeline import SubmissionInput, run_pipeline


def pretty_progress(step: str, status: str, detail: str):
    symbol = {"start": "⟳", "ok": "✓", "warn": "!", "fail": "✗"}.get(status, "·")
    print(f"  {symbol} {step:40s} {detail}")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--deck", help="Path to PDF/PPTX deck")
    parser.add_argument("--repo", help="Git repo URL or local path")
    parser.add_argument("--transcript", help="Path to transcript .txt")
    parser.add_argument("--url", help="Prototype URL to validate")
    parser.add_argument("--out", default="report.json", help="Output JSON path")
    args = parser.parse_args()

    if not (args.deck or args.repo or args.transcript):
        parser.error("At least one of --deck / --repo / --transcript required")

    inputs = SubmissionInput(
        submission_id=f"S_{uuid.uuid4().hex[:8]}",
        deck_path=args.deck,
        transcript_path=args.transcript,
        repo_url_or_path=args.repo,
        prototype_url=args.url,
    )

    print(f"\n=== Running pipeline for {inputs.submission_id} ===\n")
    report = run_pipeline(inputs, on_progress=pretty_progress)

    out_path = Path(args.out)
    out_path.write_text(json.dumps(report.to_json_dict(), indent=2, default=str))
    print(f"\n=== Done. Report written to {out_path} ===\n")

    # Brief console summary
    if report.scores:
        avg = sum(s.score for s in report.scores) / len(report.scores)
        print(f"Overall: {avg:.1f}/5")
        for s in report.scores:
            print(f"  {s.criterion:35s} {s.score}/5  ({s.confidence:.0%} conf)")


if __name__ == "__main__":
    main()
