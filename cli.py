"""Run the review offline over a folder of submissions.

    python -m co_lectr.cli co_lectr/samples --require run_agent load_config
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from dotenv import load_dotenv

from co_lectr.aggregator import digest, render
from co_lectr.layer1 import analyse


def review_with_backoff(submission: Path, findings: list, chapters: list[str], attempts: int = 4) -> list[dict]:
    """One review, retried on quota exhaustion.

    The Gemini free tier allows 5 requests per minute, and a single review costs
    more than one request once the agent calls its read tool. A class of 28 will
    hit this; a paid tier or batching is the real fix.
    """
    from co_lectr.reviewer import review

    for attempt in range(attempts):
        try:
            return asyncio.run(review(submission, findings, chapters))
        except Exception as exc:  # ADK wraps the 429 in its own error type
            if "RESOURCE_EXHAUSTED" not in str(exc) or attempt == attempts - 1:
                raise
            wait = 20 * (attempt + 1)
            print(f"   (quota exhausted, waiting {wait}s)")
            time.sleep(wait)
    return []


def main() -> None:
    parser = argparse.ArgumentParser(prog="co-lectr")
    parser.add_argument("root", type=Path, help="folder holding one directory per student")
    parser.add_argument("--require", nargs="*", default=[], help="symbols the assignment spec asks for")
    parser.add_argument("--no-tests", action="store_true", help="skip pytest (does not execute student code)")
    parser.add_argument("--review", action="store_true", help="also run layer 2 (needs GOOGLE_API_KEY)")
    parser.add_argument("--chapter", nargs="*", default=[], help="chapters taught so far, scoping what may be raised")
    parser.add_argument("--pace", type=float, default=45.0,
                        help="seconds between submissions; the free tier allows 5 requests/minute "
                             "and one review costs several")
    args = parser.parse_args()

    # GOOGLE_API_KEY lives in co_lectr/.env, the same file `adk web` reads.
    load_dotenv(Path(__file__).parent / ".env")

    submissions = sorted(p for p in args.root.iterdir() if p.is_dir())
    results = {
        p.name: analyse(p, required_symbols=tuple(args.require), run_tests=not args.no_tests)
        for p in submissions
    }

    for student, findings in results.items():
        print(f"\n== {student} - {len(findings)} finding(s)")
        for f in findings:
            print(f"   {f.path}:{f.line}  {f.rule}  {f.message}")

    if args.review:
        for index, submission in enumerate(submissions):
            if index:
                time.sleep(args.pace)
            questions = review_with_backoff(submission, results[submission.name], args.chapter)
            print(f"\n-- {submission.name}: {len(questions)} question(s)")
            for q in questions:
                print(f"   {q.get('path')}:{q.get('line')}  {q['question']}")

    print()
    print(render(digest(results), class_size=len(results)))


if __name__ == "__main__":
    main()
