"""Run the review offline over a folder of submissions.

    python -m co_lectr.cli co_lectr/samples --require run_agent load_config --review

Findings and questions are written to Firestore on `--review` runs, so a second
run over an unchanged submission reuses what is already stored and spends no
Gemini requests. Without `--review` nothing is written: a stored review with no
questions would look reviewed to the next run.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from co_lectr.aggregator import digest, render
from co_lectr.layer1 import analyse, python_files
from co_lectr.reviewer import MODEL
from co_lectr.store import Store, class_id_from, review_id


def fingerprint(root: Path) -> str:
    """Content hash of a submission, standing in for a commit sha offline.

    Same code, same id - so re-running the samples costs nothing after the first
    pass. In CI the real commit sha is used instead.
    """
    h = hashlib.sha256()
    for path in python_files(root):
        h.update(str(path.relative_to(root)).replace("\\", "/").encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:12]


def open_store() -> Store | None:
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        print("(no Firestore credentials - running without persistence)")
        return None
    return Store.open()


def review_with_backoff(submission: Path, findings: list, chapters: list[str], attempts: int = 4) -> list[dict]:
    """One review, retried on quota exhaustion."""
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
    parser.add_argument("--milestone", default="local", help="which chapter milestone this run belongs to")
    parser.add_argument("--pace", type=float, default=45.0,
                        help="seconds between submissions on the free tier; 0 to disable")
    parser.add_argument("--no-store", action="store_true", help="do not write to Firestore")
    args = parser.parse_args()

    load_dotenv(Path(__file__).parent / ".env")
    store = None if args.no_store else open_store()

    submissions = sorted(p for p in args.root.iterdir() if p.is_dir())
    results = {
        p.name: analyse(p, required_symbols=tuple(args.require), run_tests=not args.no_tests)
        for p in submissions
    }

    for student, findings in results.items():
        print(f"\n== {student} - {len(findings)} finding(s)")
        for f in findings:
            print(f"   {f.path}:{f.line}  {f.rule}  {f.message}")

    classes = set()
    if args.review:
        paced = 0
        for submission in submissions:
            student = submission.name
            class_id = class_id_from(submission)
            classes.add(class_id)
            rid = review_id(f"local/{student}", 0, fingerprint(submission))

            stored = store.get_review(rid) if store else None
            if stored is not None:
                questions = stored.get("questions", [])
                origin = "from Firestore, unchanged since last run"
            else:
                if paced and args.pace:
                    time.sleep(args.pace)
                paced += 1
                questions = review_with_backoff(submission, results[student], args.chapter)
                origin = "reviewed by " + MODEL
                if store:
                    store.record(rid=rid, student=student, class_id=class_id,
                                 milestone=args.milestone, chapters_taught=args.chapter,
                                 findings=results[student], questions=questions, model=MODEL)

            print(f"\n-- {student} [{class_id}]: {len(questions)} question(s)  ({origin})")
            for q in questions:
                print(f"   {q.get('path')}:{q.get('line')}  {q['question']}")

    print()
    print(render(digest(results), class_size=len(results)))

    if store and classes:
        for class_id in sorted(classes):
            rows = store.digest(class_id, args.milestone)
            if not rows:
                continue
            print(f"\nFirestore - class {class_id}, milestone {args.milestone}")
            for row in rows:
                print(f"  {len(row['students'])}  {row['rule']}  ({row['occurrences']} occurrence(s))")


if __name__ == "__main__":
    main()
