"""Run the review offline over a folder of submissions.

    python -m co_lectr.cli co_lectr/samples --require run_agent load_config --review

Findings and questions are written to Firestore on `--review` runs, so a second
run over an unchanged submission, reviewed against the same milestone and the
same taught chapters, reuses what is already stored and spends no Gemini
requests. Without `--review` nothing is written: a stored review with no
questions would look reviewed to the next run.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import time
from pathlib import Path

from .aggregator import digest, render
from .layer1 import analyse, python_files
from .reviewer import MODEL, repeat_rules
from .store import CLASS_FILE, UNASSIGNED, Store, class_id_from, review_id, spec_id


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


def review_with_backoff(submission: Path, findings: list, chapters: list[str],
                        recurring: dict[str, int] | None = None, attempts: int = 4) -> list[dict]:
    """One review, retried on quota exhaustion."""
    from .reviewer import review

    for attempt in range(attempts):
        try:
            return asyncio.run(review(submission, findings, chapters, recurring))
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
    parser.add_argument("--run-tests", action="store_true",
                        help="run pytest, which EXECUTES the submission's own test code (off by default)")
    parser.add_argument("--review", action="store_true", help="also run layer 2 (needs GOOGLE_API_KEY)")
    parser.add_argument("--chapter", nargs="*", default=[], help="chapters taught so far, scoping what may be raised")
    parser.add_argument("--milestone", default="local", help="which chapter milestone this run belongs to")
    parser.add_argument("--pace", type=float, default=45.0,
                        help="seconds between submissions on the free tier; 0 to disable")
    parser.add_argument("--no-store", action="store_true", help="do not write to Firestore")
    args = parser.parse_args()

    store = None if args.no_store else open_store()

    submissions = sorted(p for p in args.root.iterdir() if p.is_dir())
    # One read of each class file: the review loop and the digests both need it.
    class_of = {p.name: class_id_from(p) for p in submissions}
    results = {}
    failed = {}
    for submission in submissions:
        try:
            results[submission.name] = analyse(
                submission, required_symbols=tuple(args.require), run_tests=args.run_tests)
        except Exception as exc:  # a hung pytest, a ruff that would not start
            # One submission must not cost the rest of the class their review.
            # Left out of `results` rather than counted as clean: no findings is
            # a real and different answer.
            failed[submission.name] = f"{type(exc).__name__}: {exc}"

    for student, findings in results.items():
        print(f"\n== {student} - {len(findings)} finding(s)")
        for f in findings:
            print(f"   {f.path}:{f.line}  {f.rule}  {f.message}")
    for student, why in failed.items():
        print(f"\n== {student} - NOT ANALYSED: {why}")

    spec = spec_id(args.milestone, args.require, args.chapter)
    classes = set()
    if args.review:
        paced = 0
        for submission in submissions:
            student = submission.name
            if student in failed:
                continue
            class_id = class_of[student]
            classes.add(class_id)
            rid = review_id(f"local/{student}", 0, f"{fingerprint(submission)}.{spec}")

            stored = store.get_review(rid) if store else None
            if stored is not None:
                questions = stored.get("questions", [])
                origin = "from Firestore, unchanged since last run"
            else:
                if paced and args.pace:
                    time.sleep(args.pace)
                paced += 1
                # Read the profile only on a real review: the cached branch above
                # never reaches here, so an unchanged submission costs no reads.
                recurring = store.profile(student).get("recurring", {}) if store else {}
                questions = review_with_backoff(submission, results[student], args.chapter, recurring)
                origin = "reviewed by " + MODEL
                repeats = repeat_rules(results[student], recurring)
                if repeats:
                    origin += f", {len(repeats)} rule(s) seen before"
                if store:
                    store.record(rid=rid, student=student, class_id=class_id,
                                 milestone=args.milestone, chapters_taught=args.chapter,
                                 findings=results[student], questions=questions, model=MODEL)

            print(f"\n-- {student} [{class_id}]: {len(questions)} question(s)  ({origin})")
            for q in questions:
                print(f"   {q.get('path')}:{q.get('line')}  {q['question']}")

    print()
    if failed:
        print(f"{len(failed)} submission(s) could not be analysed: {', '.join(sorted(failed))}\n")
    # One digest per class, never pooled across them: "4 of 6 in 12-A" is a reteach
    # signal, and the same four counted against every class averages it away.
    for class_id in sorted(set(class_of.values())):
        members = [s for s, c in class_of.items() if c == class_id]
        if class_id == UNASSIGNED:
            print(f"{len(members)} submission(s) with no readable {CLASS_FILE.as_posix()} - "
                  f"reviewed, but counted in no class digest: {', '.join(sorted(members))}")
            continue
        print(f"Class {class_id} - {len(members)} submission(s)")
        print(render(digest({s: results[s] for s in members if s in results}),
                     class_size=len(members)))
        print()

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
