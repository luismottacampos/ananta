# Agentic Code Review: ovid/document-explorer-folders

**Date:** 2026-05-07 18:42:35
**Branch:** ovid/document-explorer-folders -> main
**Commit:** 9836e313e6011a4bfd51abd508c6c60347cf288d
**Files changed:** 28 | **Lines changed:** +4333 / -120
**Diff size category:** Large

## Executive Summary

The folder-upload feature is well-tested and largely matches the design plan. Six specialists found 26 candidate issues; verification confirmed 22 (7 Important, 15 Suggestion). The two highest-impact findings are an unsilenced regression in the legacy single-file picker (now reports "uploaded" for files the server rejected) and an unbounded per-request file count on the upload route that creates a real DoS surface. A narrow but destructive `_make_project_id` collision can wipe pre-existing project data. Several modal-state edge cases — uncaught network errors, double-click races, missing post-cancel refresh — leave the user stranded after error or cancel paths.

## Critical Issues

None found. (The destructive rollback in F4 has narrow trigger conditions; rated Important rather than Critical.)

## Important Issues

### [I1] Single-file picker shows success toast for failed uploads (regression)
- **File:** `src/ananta/explorers/document/frontend/src/App.tsx:115-124` (consumer); `src/ananta/explorers/shared_ui/frontend/src/api/client.ts:5-15` (transport)
- **Bug:** The `/api/documents/upload` endpoint changed from returning 4xx on per-file rejection to 200 with `failed` rows. The shared `request<T>` helper only checks `resp.ok`, so failed-row arrays are parsed as success. `App.tsx`'s `handleUpload` does not inspect returned rows and unconditionally shows `${files.length} file${...} uploaded`. Selecting three `.exe` files now shows "3 files uploaded" with no error indication.
- **Impact:** Silent failure for the existing single/multi-file picker path. Users believe uploads succeeded; nothing landed on the server. Pre-existing flow regressed by changes made for the new folder feature.
- **Suggested fix:** In the single-file path, partition the response by `status` and surface failed rows (toast or per-row error UI). Refactor to share row-handling with `uploadFolderInBatches` to avoid divergence.
- **Confidence:** High
- **Found by:** Contract & Integration

### [I2] /api/documents/upload has no per-request file-count cap (DoS surface)
- **File:** `src/ananta/explorers/document/api.py:165-298`; constants in `src/ananta/explorers/document/config.py`
- **Bug:** Per-file (50MB) and aggregate (200MB) byte caps are enforced server-side, but `len(files)` is unbounded. `MAX_FOLDER_FILES = 500` is enforced only client-side in `folder-walk.ts`. A hostile client can submit 100,000+ tiny files in a single multipart under the 200MB cap. Each iteration runs `mkdir` + two `write` calls + `create_project` + topic add — synchronous filesystem ops on the asyncio event loop.
- **Impact:** Inode exhaustion, hundreds of thousands of project directories, multi-minute synchronous request that starves the FastAPI worker. Directly reachable on any deployment beyond local-only.
- **Suggested fix:** At the top of `upload_documents`, check `len(files) > MAX_FOLDER_FILES` and raise `HTTPException(413, ...)`. Source `MAX_FOLDER_FILES` from `config.py` so the cap matches the frontend.
- **Confidence:** High
- **Found by:** Security

### [I3] Click-folder picker bypasses MAX_FOLDER_FILES cap
- **File:** `src/ananta/explorers/document/frontend/src/components/UploadArea.tsx:63-74`; `src/ananta/explorers/document/frontend/src/lib/use-folder-upload.ts:40-42`
- **Bug:** When the user clicks "Upload folder" (vs drag-drop), `UploadArea` constructs `{kind: 'walked', files: walked, rootName}` from `Array.from(e.target.files)` without any cap. In the hook, `else { walked = input.files }` skips `walkEntries` and its `MAX_FOLDER_FILES` early-bail. A click-folder selection of 5,000 files goes straight to `filterFiles` and chunked upload.
- **Impact:** Asymmetric limit enforcement. Drop path enforces 500-file cap; click path does not. The backend has no count cap either (see I2), so this lets a normal user accidentally enqueue thousands of files.
- **Suggested fix:** In the `'walked'` branch of `useFolderUpload.start`, check `if (input.files.length > MAX_FOLDER_FILES)` and surface the same summary error the entries branch produces.
- **Confidence:** High
- **Found by:** Logic & Correctness

### [I4] confirm() does not catch upload errors — modal stuck on "progress"
- **File:** `src/ananta/explorers/document/frontend/src/lib/use-folder-upload.ts:61-78`; `src/ananta/explorers/document/frontend/src/api/documents.ts:36-38`
- **Bug:** `uploadFolderInBatches` throws on `!res.ok` (e.g., 500, 413, network drop). The hook's `try/finally` only resets `setAbortCtl(null)` — there is no `catch`. The error propagates out unhandled; the modal stays in `{kind: 'progress'}` with no toast and no error summary. The Continue button isn't on the progress view; only Cancel works.
- **Impact:** A transient backend failure mid-folder-upload freezes the modal with no indication of what went wrong, no failed-row reporting, and no retry path.
- **Suggested fix:** Wrap the `uploadFolderInBatches` call in `try/catch`. On error, transition to a `summary` (or new `error`) state with an error message and any partial successes already returned; clear `pending`.
- **Confidence:** High
- **Found by:** Error Handling, Concurrency & State

### [I5] Doc list not refreshed after partial-success cancel
- **File:** `src/ananta/explorers/document/frontend/src/App.tsx:75-79`
- **Bug:** `useEffect` bumps `docsVersion` only when `state?.kind === 'summary'`. `cancel()` (use-folder-upload.ts:100-104) sets state to `null` directly without going through summary. By design, the in-flight batch always commits server-side (documented in `documents.ts:32-35`), so successful pre-cancel batches are real documents. The sidebar stays stale until manual reload.
- **Impact:** Files appear in the topic that the user thought they cancelled. Confusion; no path to discover them other than refresh.
- **Suggested fix:** Bump `docsVersion` on cancel as well. Either trigger it from `cancel()` directly or extend the effect: `if (folderUpload.state?.kind === 'summary' || (was === 'progress' && state === null))`.
- **Confidence:** High
- **Found by:** Logic & Correctness

### [I6] _make_project_id collision destructively rolls back pre-existing project
- **File:** `src/ananta/explorers/document/api.py:54-61` (id derivation), `283-296` (rollback)
- **Bug:** `project_id = slug + sha256(filename + datetime.now(UTC).isoformat())[:8]`. 8 hex chars (32 bits) plus microsecond-resolution timestamp. Two duplicate-named files in fast succession (no `await` between iterations except `file.read`) can hash to the same id. Then: `mkdir(exist_ok=True)` succeeds, `original_path.write_bytes` overwrites the first file's bytes, `state.ananta.create_project(project_id)` raises `ProjectExistsError`, the `except Exception` branch runs `shutil.rmtree(upload_dir)` and `delete_project(project_id)` — destroying the pre-existing project's data along with the failing file. Folder uploads frequently contain duplicate basenames (multiple READMEs in subdirs).
- **Impact:** Data loss on collision. Likelihood is low but blast radius is high — a "failed upload" silently wipes an unrelated stored document.
- **Suggested fix:** Replace the timestamp component with `secrets.token_hex(4)` (cryptographically random 8-hex) so collisions are negligible. Alternatively, change `mkdir(exist_ok=True)` to `exist_ok=False` and regenerate the id on `FileExistsError`. Track a `created_project` flag in the upload loop and gate the destructive rollback behind it.
- **Confidence:** High (multi-specialist agreement: Logic 85, Security 75, Security 70 — same root cause)
- **Found by:** Logic & Correctness, Security

### [I7] Continue button not disabled — double-click double-uploads
- **File:** `src/ananta/explorers/document/frontend/src/components/FolderUploadModal.tsx:103-109`; `src/ananta/explorers/document/frontend/src/lib/use-folder-upload.ts:51-98`
- **Bug:** The Continue button has no `disabled` state and no debounce. Two click events queued from one physical action (touch screens, accessibility tools, automation) can both run before React commits the state transition to `progress`. Both `confirm()` invocations close over the same `pending` and fire the same FormData twice. Server creates duplicate documents (different `project_id` because the timestamp differs across microseconds, so no dedup).
- **Impact:** User-visible duplicate documents in the topic. Easily reproduced on touch screens or with rapid clicks.
- **Suggested fix:** Disable Continue immediately on click (`useState` flag or transition state). Also guard `confirm()` with `if (state?.kind === 'progress' || abortCtl) return`.
- **Confidence:** Medium-High
- **Found by:** Concurrency & State

## Suggestions

- **[S1]** `walkEntries` off-by-one allows 501 files instead of 500. `folder-walk.ts:97-103` — change `>` to `>=`. (Logic, Concurrency)
- **[S2]** `partitionIntoBatches` correctness depends on undocumented invariant `TARGET_BATCH_BYTES >= MAX_UPLOAD_BYTES`. Add inline assertion or comment. `folder-walk.ts:119-137`. (Logic)
- **[S3]** Aggregate-cap 413 mid-batch discards `results` accumulated for prior successful files in the same request. `api.py:217-224, 281-282`. Unreachable via standard client (50MB chunks vs 200MB cap) but inconsistent with partial-success philosophy. (Error Handling)
- **[S4]** `start()` has no re-entry guard; a second drop during in-flight upload overwrites the modal state. `use-folder-upload.ts:25-49`. (Concurrency)
- **[S5]** `uploadFolderInBatches` does not pass `signal` to `fetch` (intentional design); server keeps committed in-flight batch on cancel. Combined with I5, the user experience is "cancel was a lie." `documents.ts:32-35`. (Concurrency)
- **[S6]** `cancel()` reads stale `abortCtl` from React state via `useCallback` deps `[abortCtl]`; cancel between renders can be a no-op. `use-folder-upload.ts:100-104`. Use `useRef` instead. (Logic, Concurrency)
- **[S7]** Empty-extension reason text reads `"unsupported file type: "` (trailing space) for `Makefile`, `Dockerfile`. `api.py:200-202`. (Error Handling)
- **[S8]** Empty `webkitRelativePath` produces `relative_path = ""` stored on the server. `UploadArea.tsx:66-71`. Subtitle is hidden by truthy check, but the empty value persists in `meta.json`. Fall back to single-file path when `webkitRelativePath` is unpopulated. (Error Handling)
- **[S9]** Cleanup logic (`rmtree + remove_item_from_all + delete_project`) duplicated at `api.py:236`, `:283-296`, and `:313-323` with divergent error handling. Extract a shared helper. (Contract)
- **[S10]** `FolderInput` / `FolderUploadInput` types byte-duplicated across `UploadArea.tsx:7-9` and `use-folder-upload.ts:16-18`. Lift into a shared module. (Contract)
- **[S11]** No build-time enforcement that `folder-walk.ts` mirrors `config.py` and `extractors.py` allowlist. Add a Python test that parses the TS file and asserts equality, or fetch from a `/api/documents/limits` endpoint. (Contract)
- **[S12]** `App.tsx` `useEffect` double-fires in React strict mode and bumps `docsVersion` by 2. Track previous-state in a ref to gate on actual transitions. `App.tsx:75-79`. (Concurrency, dev-only)
- **[S13]** CLAUDE.md "Security" section claims `<untrusted_document_content>` literal tag wrapping; the real implementation in `src/ananta/rlm/boundary.py` uses per-query `UNTRUSTED_CONTENT_<128-bit-hex>_BEGIN/END` random tokens via `wrap_untrusted`. Update steering doc. (Security — steering contradiction)
- **[S14]** `upload_session_id` accepted as free-form `str | None`; not validated as UUID. `api.py:170, 251`. Defense-in-depth: enforce UUID format. (Security)
- **[S15]** `relative_path` accepted with no length cap, no control-char filter, no `..` rejection. `api.py:169, 250, 261`. Currently piped to `ParsedDocument.metadata` (boundary tokens defend against prompt injection at RLM layer). Future-proof: cap length and reject control chars at ingress. (Security)

## Plan Alignment

The branch implements the plan in `docs/plans/2026-05-05-folder-upload-{design,implementation}.md` essentially in full.

- **Implemented:** All Phase A (config.py, schemas, per-file partial-success loop, idempotency guard, `relative_path` in ParsedDocument), all Phase B (filterFiles, walkEntries, MAX_FOLDER_FILES, partitionIntoBatches), all Phase C (drop-zone disabled, directory drops routed, Upload-folder click button, 3-state modal), all Phase D (chunked upload with cancel-between-batches, useFolderUpload hook, sidebar subtitle rendering, CHANGELOG entries).
- **Not yet implemented:** Manual verification (D4) is unverifiable from the diff. The `TODO.md` change tracks v1 cuts.
- **Deviations (intentional, documented):**
  - Sidebar subtitle rendered through `TopicSidebar.tsx` via a new `subtitle` field on the shared `DocumentItem` type, rather than directly in `DocumentItem.tsx` as the plan suggested. Rationale in commit `d0e7460`.
  - `confirm()` adds a defensive `signal.aborted` short-circuit beyond what the plan specified (covers a real cancel race; commit `9836e31`).
  - `readEntries` recursion deferred via `queueMicrotask` for Safari cursor-advance robustness; plan used direct recursion.
  - `status` field tightened to `Literal["created","failed"]` (plan suggested as optional improvement).

No drift detected.

## Review Metadata

- **Agents dispatched:** Logic & Correctness, Error Handling & Edge Cases, Contract & Integration, Concurrency & State, Security, Plan Alignment, Verifier
- **Scope:** 28 changed files (folder-upload feature + supporting tests + shared UI subtitle), plus adjacent reads of `extractors.py`, `boundary.py`, `storage/filesystem.py`, `shared_ui/frontend/src/api/client.ts`, `shared_ui/topics.py`, `CLAUDE.md`
- **Raw findings:** 26 (before verification)
- **Verified findings:** 22 (after verification)
- **Filtered out:** 4 (`total_bytes` accounting — design-correct; `DocumentItem` "filename twice" — not duplicated content; `relative_path` ordering invariant — `idx` correctly aligns with `enumerate(files)`)
- **Steering files consulted:** `CLAUDE.md`
- **Plan/design docs consulted:** `docs/plans/2026-05-05-folder-upload-design.md`, `docs/plans/2026-05-05-folder-upload-implementation.md`

## Model Attribution

- **Orchestrator:** claude-opus-4-7[1m] (source: system-prompt)
- **Specialists:**
  - Logic & Correctness: inherit<claude-opus-4-7[1m]> (source: dispatched+inherited)
  - Error Handling & Edge Cases: inherit<claude-opus-4-7[1m]> (source: dispatched+inherited)
  - Contract & Integration: inherit<claude-opus-4-7[1m]> (source: dispatched+inherited)
  - Concurrency & State: inherit<claude-opus-4-7[1m]> (source: dispatched+inherited)
  - Security: inherit<claude-opus-4-7[1m]> (source: dispatched+inherited)
  - Plan Alignment: inherit<claude-opus-4-7[1m]> (source: dispatched+inherited)
  - Verifier: inherit<claude-opus-4-7[1m]> (source: dispatched+inherited)
- **Probe time:** 2026-05-07T15:28:09Z
