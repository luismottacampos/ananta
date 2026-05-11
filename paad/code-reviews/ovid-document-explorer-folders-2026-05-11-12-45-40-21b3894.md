# Agentic Code Review: ovid/document-explorer-folders

**Date:** 2026-05-11 12:45:40
**Branch:** ovid/document-explorer-folders -> main
**Commit:** 21b3894df5726354f9a04e646c5275e76f6599e7
**Files changed:** 45 | **Lines changed:** +9287 / -332
**Diff size category:** Large

## Executive Summary

This branch delivers folder upload for the document explorer plus substantial security/robustness hardening tagged inline as `I*` / `S*` / `C*` review iterations. Overall the feature is well-defended and the plan-doc alignment is high. However, the review surfaces **three critical issues** — most urgently a cross-origin GET exfiltration vector caused by `CORSMiddleware(allow_origins=["*"])` combined with the same-origin guard's GET/HEAD exemption, plus two paths where the topic-rollback logic can destroy a pre-existing or concurrently-active topic. Beyond Critical, the bulk of the Important findings cluster in two areas: decompression-bomb DoS surface in `.docx`/`.pptx` extractors, and front-end races (preflight re-entry, stale fetches, summary completeness).

## Critical Issues

### [C1] Cross-origin GET exfiltration via wildcard CORS + GET exemption

- **File:** `src/ananta/explorers/shared_ui/app_factory.py:80-83, 291-296`
- **Bug:** `_SameOriginGuardMiddleware` exempts GET/HEAD on the comment-stated rationale that the same-origin policy stops cross-origin body reads. But `CORSMiddleware` is configured with `allow_origins=["*"]`, which makes that rationale false — the browser will hand the response body to any origin. Any attacker page the user visits while the explorer is running can `fetch('http://localhost:<port>/api/documents')`, `/api/documents/<id>/download`, `/api/topics/.../export`, traces, etc., and read every uploaded document plus conversation/trace history.
- **Impact:** Cross-origin exfiltration of user data from a localhost daemon. Directly contradicts the documented threat model.
- **Suggested fix:** Either (a) drop `CORSMiddleware` entirely (the explorer is single-origin in normal use), (b) replace `allow_origins=["*"]` with the explicit served origin, or (c) remove the GET/HEAD exemption in `_SameOriginGuardMiddleware`.
- **Confidence:** High
- **Found by:** Security

### [C2] Whitespace-padded topic name causes upload rollback to delete a pre-existing topic

- **File:** `src/ananta/explorers/document/api.py:430, 543-550`
- **Bug:** `topic_existed_before = bool(topic) and topic in state.topic_mgr.list_topics()` compares the raw form value against the topic manager's normalised names. Submit `topic="  Reports  "`; the membership test misses; `topic_mgr.create("  Reports  ")` normalises to `"Reports"` and is idempotent for the existing topic; on full-upload-failure the rollback calls `topic_mgr.delete("  Reports  ")` which also normalises and `shutil.rmtree`s the pre-existing topic directory — destroying its items and on-disk state.
- **Impact:** Silent data loss. A routine "all files unsupported" upload mistake plus a stray leading/trailing space deletes an unrelated topic.
- **Suggested fix:** Normalise once at the route entry (`topic = topic.strip() or None`) and use the normalised value for the existence check, `create()`, and rollback `delete()`. Belt-and-suspenders: only delete if `topic_existed_before` is False **and** `topic_mgr.list_items(topic) == []`.
- **Confidence:** High
- **Found by:** Logic-A, Error-A, Contract-A (three-way agreement)

### [C3] Concurrent uploads to a brand-new topic — one failing request wipes the other's data

- **File:** `src/ananta/explorers/document/api.py:430-552`
- **Bug:** `topic_existed_before` is read outside any lock. Two concurrent uploads (A, B) targeting the same brand-new topic both observe `False`. If A succeeds (adds items, returns) and B fails (every file unsupported / over cap), B's rollback runs `delete(topic)` and wipes the topic A just populated.
- **Impact:** Concurrent data loss. The very rollback added in commit `d53f236` to fix S6 creates a more dangerous race.
- **Suggested fix:** Make the rollback condition observable-state-based, not flag-based: under `_mutation_lock`, only delete the topic if `list_items(topic) == []` AFTER the per-file loop completes. Consider exposing `topic_mgr.delete_if_empty(name)` to make the atomic check explicit.
- **Confidence:** High
- **Found by:** Logic-A, Concurrency-A

## Important Issues

### [I1] download_document serves wrong-extension bytes after rename

- **File:** `src/ananta/explorers/document/api.py:606-647`
- **Bug:** `rename_document` allows `new_name` with any extension. `download_document` derives the Content-Disposition filename from `meta["filename"]` but serves the bytes from the actual `original.<orig-ext>` on disk. Rename `report.pdf` to `payload.html` and the browser sees an HTML-labeled response containing PDF bytes (and vice versa).
- **Impact:** File/declared-type desync; MIME-sniffing surprises for downstream tools; potential phishing/render-confusion vector.
- **Suggested fix:** Either reject rename when the new extension differs from the on-disk `original.<ext>`, or always re-derive the served filename's extension from the actual on-disk file in `download_document`.
- **Confidence:** High
- **Found by:** Logic-A

### [I2] Decompression-bomb DoS on .docx / .pptx via eager OOXML parsing

- **File:** `src/ananta/explorers/document/extractors.py:166-205`
- **Bug:** `DocxDocument(str(path))` and `PptxPresentation(str(path))` eagerly parse the entire OOXML object graph into memory before the per-paragraph / per-shape cumulative-bytes streaming cap fires. A 50 MiB compressed file with repetitive XML inflates to multi-GB in-memory Python objects.
- **Impact:** Per-file DoS reachable by any upload-authenticated caller; the 50 MiB single-file and 200 MiB aggregate caps don't bound peak resident memory.
- **Suggested fix:** Before instantiating the parser, inspect the zip archive via `zipfile.ZipFile`, sum `ZipInfo.file_size`, and reject inputs whose declared uncompressed size exceeds (e.g.) 4× `MAX_EXTRACTED_TEXT_BYTES`. Same treatment for `.xlsx` (its `xl/sharedStrings.xml` is eagerly parsed even in read-only mode).
- **Confidence:** High
- **Found by:** Security, Error-A

### [I3] Per-leaf decompression-bomb bypass in extractors

- **File:** `src/ananta/explorers/document/extractors.py:155-238`
- **Bug:** Per-format extractors check `cumulative >= MAX_EXTRACTED_TEXT_BYTES` only AFTER materialising each leaf (a PDF page's text, a docx paragraph, a pptx text frame, an xlsx cell) into a Python string. Peak memory is bounded by the largest single leaf, not by the cap. A single pathologically large leaf is fully realised first.
- **Impact:** The peak-memory invariant the surrounding code claims to enforce is not actually enforced.
- **Suggested fix:** For each leaf, truncate to `MAX_EXTRACTED_TEXT_BYTES - cumulative` before appending: `text = text[:remaining]`.
- **Confidence:** High
- **Found by:** Error-A

### [I4] Cancelled query thread keeps writing the conversation session

- **File:** `src/ananta/explorers/shared_ui/websockets.py:156-167, 300-344, 507-552`
- **Bug:** When a new query arrives, `query_task.cancel()` is called — but the task is awaiting `loop.run_in_executor(...)`, and cancelling the future does not interrupt the executor thread. The old thread runs to completion, then calls `session.add_exchange(...)`. The new query also enters `add_exchange`. Two threads write the same `conversation.json`; even with tempfile+rename, the loser's write is lost wholesale.
- **Impact:** Silent conversation-history corruption on rapid re-query.
- **Suggested fix:** Either gate `add_exchange` on `not cancel_event.is_set()` after the executor returns, or guard `add_exchange` with a per-session lock that the WS handler holds across the executor + add_exchange critical section.
- **Confidence:** High
- **Found by:** Concurrency-A

### [I5] multi_project_query trace attribution to a non-loaded project

- **File:** `src/ananta/explorers/shared_ui/websockets.py:487-497, 529-535`
- **Bug:** The engine selector iterates `document_ids` looking for the first project with a non-None `rlm_engine`, recording `first_project_id`. But that project may have failed `load_docs` and is absent from `loaded_project_ids`. Trace storage / lookup uses `first_project_id`, so traces can be filed under a project the user thinks contributed nothing.
- **Impact:** Trace orphaning; user's trace history shows blank for the legitimately consulted projects.
- **Suggested fix:** Source `first_project_id` from `loaded_project_ids`, not from raw `document_ids`.
- **Confidence:** High
- **Found by:** Concurrency-A

### [I6] Topic readers don't hold the mutation lock

- **File:** `src/ananta/explorers/shared_ui/topics.py:189-198, 200-207, 233-236, 238-250, 257-264, 306-320`
- **Bug:** `list_topics`, `_resolve`, `list_items`, `list_all_items`, `find_topics_for_item` iterate topic dirs and read `topic.json` without acquiring `_mutation_lock`. Combined with non-atomic writes (see I7), readers can observe torn JSON which `_read_meta` treats as corrupt (returns `None`), silently hiding the topic.
- **Impact:** Inconsistent reads; transient "topic disappeared" UI flicker during routine writes.
- **Suggested fix:** Either acquire `_mutation_lock` around read loops, or convert writes to tempfile+os.replace (I7) so reads never observe torn content.
- **Confidence:** High
- **Found by:** Concurrency-A

### [I7] topic.json writes are non-atomic

- **File:** `src/ananta/explorers/shared_ui/topics.py:146, 181, 220, 231, 277, 292`
- **Bug:** Every mutator uses `meta_path.write_text(json.dumps(...))`. Concurrent readers (which lack the mutation lock — see I6) can observe truncated bytes; `_read_meta` returns `None` on JSONDecodeError and silently omits the topic. A crash mid-write loses the modification entirely. `WebConversationSession._save` already uses tempfile+os.replace for the same problem and is a direct model.
- **Impact:** Torn writes corrupt the listing transiently; crashes lose mutations.
- **Suggested fix:** Add `_atomic_write_json(path, payload)` mirroring `session.py`'s tempfile+os.replace pattern, and route all six write sites through it.
- **Confidence:** High
- **Found by:** Concurrency-A

### [I8] Shared `request<T>` mangles FastAPI 422 array detail as `[object Object]`

- **File:** `src/ananta/explorers/shared_ui/frontend/src/api/client.ts:5-15`
- **Bug:** `throw new Error(err.detail || resp.statusText)` — FastAPI 422 returns `detail` as an array of `{loc, msg, type}` objects, and `new Error(arr)` produces `Error: [object Object],[object Object]`. Only `document/frontend/src/api/documents.ts` was migrated to `formatHttpError`; rename / topic create / topic rename / model update all still flow through this helper.
- **Impact:** Every Pydantic 422 surfaces as `[object Object]` in toasts — looks broken.
- **Suggested fix:** Apply the same array-detail handling that `formatHttpError` does, or extract a shared error-message helper used by both clients.
- **Confidence:** High
- **Found by:** Contract-B

### [I9] start() re-entry guard misses the preflight-pending window

- **File:** `src/ananta/explorers/document/frontend/src/lib/use-folder-upload.ts:47-117`
- **Bug:** Guard covers `abortCtlRef.current || walkingRef.current`. After the walk finishes, `walkingRef.current = false` and `pending` is set; `abortCtlRef.current` is still null until the user clicks Continue. A second folder drop in that window walks the new folder and overwrites the pending+state — the first drop's preflight modal silently swaps to a different folder behind the user's Continue click.
- **Impact:** User confirms folder A but uploads folder B.
- **Suggested fix:** Add a third guard: refuse re-entry while `state?.kind === 'preflight'` (also covers `'progress'` and `'summary'`). A `pendingRef` mirrored from state is the cleanest implementation.
- **Confidence:** High
- **Found by:** Concurrency-B (overlaps Error-B F38/F41)

### [I10] Failed batch silently drops rows of batch K and K+1..N from the summary

- **File:** `src/ananta/explorers/document/frontend/src/api/documents.ts:100-111`
- **Bug:** When batch K's fetch fails, `BatchUploadError.partial` carries only batches 1..K-1's rows. Files in batch K (whose request started) and batches K+1..N (never attempted) are absent from the summary; the user sees "X ingested · upload — server error (500)" with no per-file reasons for the dozens-to-hundreds of unaccounted files.
- **Impact:** Stated invariant ("every file accounted for in the summary") is broken on any mid-batch failure.
- **Suggested fix:** When batch K errors, synthesise `status: 'failed'` rows for every file in batches K..N and append to `partial` before throwing.
- **Confidence:** High
- **Found by:** Logic-B

### [I11] walkEntries silently drops files when `getFile()` rejects

- **File:** `src/ananta/explorers/document/frontend/src/lib/folder-walk.ts:140-176`
- **Bug:** Inside `visit`, `getFile()` rejection lands on `catch { return }` with no SkippedFile sentinel emitted. A folder where every file is unreadable yields zero walked entries and the summary modal says "0 files" with no skipped reasons.
- **Impact:** Silent data loss for the user's mental model; no diagnostic feedback for a common drag-drop edge case (network mounts, sandboxed download folders, broken symlinks).
- **Suggested fix:** Either extend `WalkedFile` with a `skipReason` discriminator, or thread an out-of-band `unreadable: SkippedFile[]` array through `walkEntries` into the preflight summary.
- **Confidence:** High
- **Found by:** Logic-B, Error-B

### [I12] UploadArea click-picker `<input>` value is not reset after upload

- **File:** `src/ananta/explorers/document/frontend/src/components/UploadArea.tsx:131-137`
- **Bug:** The click-pick `<input>` never resets `e.target.value`. The folder-picker `<input>` does (line 80). Selecting the same file twice in a row from the click picker produces no `change` event the second time — silent no-op.
- **Impact:** User cannot retry the same file after upload (success or failure) without picking a different file or reloading.
- **Suggested fix:** In the click-pick `onChange`, set `e.target.value = ''` before calling `handleFiles`, mirroring the folder-picker code path.
- **Confidence:** High
- **Found by:** Logic-B

### [I13] useEffect on `docsVersion` lacks a cancellation flag

- **File:** `src/ananta/explorers/document/frontend/src/App.tsx:60-80`
- **Bug:** Rapid `docsVersion` bumps (delete + upload + delete) can resolve out of order. With no `cancelled` flag, a stale resolution can land after a newer call returned, leaving the sidebar showing stale state until the next bump.
- **Impact:** Sidebar inconsistency after rapid mutations.
- **Suggested fix:** Add `let cancelled = false`; guard `setAllDocs` / `setUncategorizedDocs` behind it; return `() => { cancelled = true }` from the effect.
- **Confidence:** High
- **Found by:** Concurrency-B

### [I14] openDocDetail topics fetch has no race guard

- **File:** `src/ananta/explorers/document/frontend/src/App.tsx:115-119`
- **Bug:** `api.documents.topics(doc.project_id).then(setViewingDocTopics)` — clicking docA then docB quickly can show docA's topics in docB's detail panel because the requests resolve out of order.
- **Impact:** User sees wrong document's topics; subsequent add/remove acts on the wrong target.
- **Suggested fix:** Capture `doc.project_id` and verify `viewingDoc?.project_id === id` (via functional `setViewingDoc`) before calling `setViewingDocTopics`.
- **Confidence:** High
- **Found by:** Concurrency-B

### [I15] Stale confirm() summary overwrites a fresh preflight

- **File:** `src/ananta/explorers/document/frontend/src/lib/use-folder-upload.ts:170-218`
- **Bug:** Line 186 has an identity guard on `setPending` (compares `prev?.accepted === accepted`). But the `setState({kind:'summary', ...})` on line 213 has no analogous guard. If upload A errors at the same instant cancel() runs and the user drops folder B, A's late-arriving summary clobbers B's preflight.
- **Impact:** UX corruption during rapid cancel-and-restart sequences.
- **Suggested fix:** Apply the same identity-check guard on `setState(summary)` — only commit if the current state still belongs to this upload.
- **Confidence:** Medium
- **Found by:** Error-B

## Suggestions

- [S1] Slug-collision conflict returned as 422 not 409 — `src/ananta/explorers/shared_ui/routes.py:47-54` + `topics.py:135-140`. (Logic-A, Error-A)
- [S2] `_failed_row` emits `project_id=""` while schema declares non-optional `str` — `src/ananta/explorers/document/schemas.py:71-75` + `api.py:162-174`. React key collision risk. (Contract-A)
- [S3] `create_topic` / `rename_topic` return un-normalised name — `src/ananta/explorers/shared_ui/routes.py:80, 285`. (Contract-A)
- [S4] `_CONTROL_BYTES` regex duplicated in three modules — `api.py:68`, `websockets.py:45`, `topics.py:76`. (Contract-A)
- [S5] "Unsupported file type" casing diverges between `api.py:489` and `extractors.py:138` — breaks summary grouping. (Contract-A)
- [S6] FE folder-cap message diverges from BE 413 wording — `folder-walk.ts:108-114` vs `api.py:410-413`. (Contract-B)
- [S7] `ananta.prompts.loader` imports from `ananta.rlm.boundary` — layering inversion vs sibling local-import pattern. `loader.py:7`. (Contract-A)
- [S8] `_persist_one_upload` rollback swallows cleanup errors with no log — `api.py:292-299`. (Error-A)
- [S9] `rename_document` returns 404 for corrupt-but-existing meta.json — `api.py:616-619` + helper `:306-314`. (Error-A)
- [S10] `_resolve_final_var` misclassifies legitimate `None` as "variable not found" — `engine.py:689-697`. (Error-A)
- [S11] `download_document` legacy fallback `startswith("original")` over-matches — `api.py:640-644`. (Error-A)
- [S12] `total_bytes` invariant broken when aggregate cap trips mid-loop — `api.py:510-519`. (Contract-A)
- [S13] PATCH `/documents/{doc_id:path}` greedy converter shadows nested routes — `api.py:605`. (Contract-A)
- [S14] `remove_item_from_all` holds `_mutation_lock` through full directory iteration — `topics.py:266-277`. (Contract-A)
- [S15] `delete_document` existence-check race; second concurrent DELETE returns 500 not 404 — `api.py:573-585`. (Contract-A)
- [S16] Body-cap 413 / SameOrigin 403 short-circuit responses miss CORS headers — `app_factory.py:96-104, 112-121`. Moot if [C1] fixed by removing wildcard CORS. (Contract-A)
- [S17] WebSocket frames not size-capped — `websockets.py:148`. (Security)
- [S18] `file.filename` length uncapped on upload (vs 512-char cap on rename) — `api.py:100-123, 441-479`. (Security)
- [S19] `upload_session_id` form field persisted unvalidated — `api.py:251-252, 402`. (Security)
- [S20] `_origin_matches_host` accepts schemeless `://host` — `app_factory.py:36-47`. Defense-in-depth. (Security)
- [S21] Duplicated doc-row JSX in TopicSidebar (topic + uncategorized sections) — `TopicSidebar.tsx:429-461, 611-657`. (Contract-B)
- [S22] "upload failed: " prefix coupled across 3 sites by string equality — `documents.ts:54, 104, 110` + `use-folder-upload.ts:208`. (Contract-B)
- [S23] WS `error.message` cast `(msg.message as string) ?? '...'` is type-unsafe — `App.tsx:37-42`. (Contract-B)
- [S24] `handleDocRename` silently cancels on empty input — `TopicSidebar.tsx:152-166`. Pre-existing pattern. (Error-B)
- [S25] Empty `rootName` from click path renders unlabeled cap-exceeded summary row — `use-folder-upload.ts:100, 106`. (Error-B)
- [S26] Non-JSON 4xx error bodies collapse to generic status phrase — `documents.ts:33-58`. (Error-B)
- [S27] `walkEntries` has no cancellation signal; slow walk of a huge folder cannot be aborted — `use-folder-upload.ts:41-117`. (Concurrency-B)
- [S28] TopicSidebar outside-click detection couples to Tailwind class strings — `TopicSidebar.tsx:76-93`. (Logic-B)
- [S29] `TestConcurrentMutation` coverage gap: no tests for concurrent create/rename, reader-vs-writer torn reads, or the upload-rollback destructive race (C2/C3) — `tests/unit/explorers/shared_ui/test_topics.py:480-536`. (Concurrency-A)

## Plan Alignment

Plan docs consulted:
- `docs/plans/2026-05-05-folder-upload-design.md`
- `docs/plans/2026-05-05-folder-upload-implementation.md`

- **Implemented:** All four plan phases (A: backend persist + schemas + per-file partial success; B: FE folder walk + filter + batching; C: drop zone + folder modal; D: API client + hook + sidebar subtitle). All numbered tasks A0–D4 have visible code counterparts with test coverage. Centralised config module (`document/config.py`), `relative_path` + `upload_session_id` propagation, `_failed_row` partial-success rows, `FolderCapExceededError` typed throw, `partitionIntoBatches`, `FolderUploadModal` three views, `uploadFolderInBatches`, `useFolderUpload` hook all match.
- **Not yet implemented (neutral):** All items in the design's "Out of scope for v1" list remain unimplemented and are tracked in `TODO.md:63-79` — broader sensitive-info filtering, all-or-nothing batch rollback, auto-create-topic-from-folder-name, env-configurable limits. This is intended deferred work, not drift.
- **Deviations (deliberate evolutions, not drift):**
  - `MAX_EXTRACTED_TEXT_BYTES` (16 MiB extraction cap) added beyond plan — DoS hardening consistent with CLAUDE.md "beyond PoC".
  - `_make_project_id` widened to 48-bit random suffix with 5-attempt retry (plan amended in-place after C4/I14 review).
  - Aggregate cap behaviour: plan said `raise HTTPException(413)` mid-loop; implementation emits failed rows instead (preserves partial-success contract; documented inline as "C1").
  - `filterFiles` accepts `WalkedFile[]` (strict improvement over plan's `File[]`); cap counts only accepted files; cap signalled via typed error (strict improvement over plan's regex-match).
  - Substantial extra input-validation surface in `api.py` (filename/path denylists, control-byte regex, traversal guard, 422 length-mismatch check) — beyond-plan hardening tagged inline as `I*` / `S*`.
  - Per-file disk work dispatched via `asyncio.to_thread` (I2 event-loop fix; not in plan).
  - CHANGELOG correctly contradicts the plan's "single-file uploads observably unchanged" claim — the plan was wrong, the CHANGELOG is accurate.
  - Unrelated shared-explorer fixes piggyback on the branch (S17, S41, I3, "Add to" submenu duplicate-file fix). Plan-aware piggybacks surfaced during review iterations.

**No plan item silently dropped. Nothing in the implementation contradicts the design's stated decisions.**

## Review Metadata

- **Agents dispatched:** Logic-A (backend), Logic-B (frontend), Error-A (backend), Error-B (frontend), Contract-A (backend), Contract-B (frontend), Concurrency-A (backend), Concurrency-B (frontend), Security (cross-cutting), Plan-Alignment, Verifier.
- **Scope:** All 45 changed files plus surrounding context (callers/callees one level deep). Backend Python: `document/{api,config,extractors,schemas,websockets}.py`, `shared_ui/{app_factory,routes,topics,websockets}.py`, `prompts/loader.py`, `rlm/engine.py`. Frontend TS/TSX: `document/frontend/src/{App.tsx, api/{client,documents}.ts, components/{DocumentItem,FolderUploadModal,UploadArea}.tsx, lib/{folder-walk,use-folder-upload}.ts, types.ts}`, `shared_ui/frontend/src/{components/TopicSidebar.tsx, types/index.ts}`.
- **Raw findings:** 54 (before verification)
- **Verified findings:** 47 (3 Critical + 15 Important + 29 Suggestions)
- **Filtered out:** 7 — F3 (body-cap can't race with handler entry the way described), F8 (litellm except-pass has inline justification per CLAUDE.md), F24 (drain_task is awaited in finally; no leak), F36 (synchronous `abortCtlRef.current = ctl` assignment protects against double-click within the same tick), F52 (commitVersion bump correctly gated on `abortCtlRef.current` which is only set after at least one batch is in-flight), plus internal sub-findings the verifier subsumed.
- **Steering files consulted:** `./CLAUDE.md`
- **Plan/design docs consulted:** `docs/plans/2026-05-05-folder-upload-design.md`, `docs/plans/2026-05-05-folder-upload-implementation.md`

## Model Attribution

- **Orchestrator:** `claude-opus-4-7[1m]` (source: system-prompt)
- **Specialists:**
  - Logic-A: `inherit<claude-opus-4-7[1m]>` (source: dispatched+inherited)
  - Logic-B: `inherit<claude-opus-4-7[1m]>` (source: dispatched+inherited)
  - Error-A: `inherit<claude-opus-4-7[1m]>` (source: dispatched+inherited)
  - Error-B: `inherit<claude-opus-4-7[1m]>` (source: dispatched+inherited)
  - Contract-A: `inherit<claude-opus-4-7[1m]>` (source: dispatched+inherited)
  - Contract-B: `inherit<claude-opus-4-7[1m]>` (source: dispatched+inherited)
  - Concurrency-A: `inherit<claude-opus-4-7[1m]>` (source: dispatched+inherited)
  - Concurrency-B: `inherit<claude-opus-4-7[1m]>` (source: dispatched+inherited)
  - Security: `inherit<claude-opus-4-7[1m]>` (source: dispatched+inherited)
  - Plan-Alignment: `inherit<claude-opus-4-7[1m]>` (source: dispatched+inherited)
  - Verifier: `inherit<claude-opus-4-7[1m]>` (source: dispatched+inherited)
- **Probe time:** 2026-05-11T07:59:38Z
