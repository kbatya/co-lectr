# Co-Lectr — gallery screenshots to capture

Two live screenshots to round out the Devpost image gallery. Both are the *real*
surfaces judges care about, so a genuine screenshot beats any mockup. Target the
same **3:2 ratio** (e.g. crop to 1500×1000) and keep each under 5 MB.

Gallery order that tells the story:

1. `architecture.png` — the system diagram (already made) ← lead image
2. **A real PR comment** (below)
3. **The `adk web` lecturer view** (below)
4. `card-digest.png` — the class digest (already made)
5. `card-review.png` — code → questions (already made)
6. `card-theme.png` — unique project topics: same idea, different words, caught (already made)

---

## Screenshot 2 — a real PR comment

**What it shows:** Co-Lectr posting one comment of questions on an actual pull
request. This is the "product working" shot.

**How to capture:**

1. Open (or create) a pull request on your test repo `kbatya/colectr-test` that
   introduces a small, obvious issue — e.g. a file with an unused import and a
   bare `except:`. Pushing a new commit to an open PR re-triggers the review.
2. Wait for the webhook → Cloud Run → review round-trip (a few seconds).
3. Scroll to the **Co-Lectr** comment on the PR's *Conversation* tab.
4. Screenshot the comment (include the file/line anchors and the
   `Facts from ruff · ast · pytest …` footer — that footer is the proof it's
   grounded, not generic LLM output).

**Tip:** Zoom the browser to ~110–125% first so the text is crisp when cropped.
Capture just the comment card, not the whole GitHub chrome.

---

## Screenshot 3 — the `adk web` lecturer view

**What it shows:** the lecturer asking a plain-English question and getting the
per-class signal back — the differentiator.

**How to capture:**

1. From the workspace parent directory, with your Gemini key loaded
   (`co_lectr/.env` has `GOOGLE_API_KEY`), start the ADK web UI:

   ```bash
   adk web
   ```

2. In the browser UI, pick the **`co_lectr`** agent.
3. Ask, in order, so the transcript reads well:
   - `Which chapters have been taught? ch1 basics and ch2 functions and files.`
   - `What did class 12-A get wrong?`
4. It will call `class_digest` and answer with the `4/6 bare except:` style
   breakdown. Screenshot the question + answer.

**Notes:**
- `class_digest` recomputes from the local `samples/` checkout, so it needs
  **no Firestore credentials** — only the Gemini key. Easiest path for the demo.
- If you also have `GOOGLE_APPLICATION_CREDENTIALS` set, you can instead ask
  *"what did 12-A get wrong this milestone?"* to hit `stored_class_digest`
  (the Firestore-accumulated picture) — nice, but optional.
- For a second strong frame, ask `Did anyone try to game the reviewer?` to show
  `flagged_reviews`.
- For the **project-theme** feature, ask `Which project themes are waiting for
  approval?` then `Approve student_04's theme` — the agent calls `pending_themes`
  and `approve_theme`, and (with a `GITHUB_TOKEN` set) posts the decision to the
  student's PR. A clean before/after of a topic being reserved and then approved
  makes a strong extra frame.

---

## Also check

- **Thumbnail:** Devpost has a separate thumbnail (the card shown in listings).
  A `thumbnail.jpg` already exists in the repo — make sure it's set on the form.
- All gallery images: JPG/PNG/GIF, ≤ 5 MB, 3:2 preferred, up to 15.
