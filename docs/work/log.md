---
type: Work Log
---

# Work log


## 2026-10-18 — FarmCore revamp

User authorized specification, tickets, private access first, independent feature chats, implementation agents, QA and integration. Published milestones 1–4 as a 12-ticket graph. Claude review incorporated with typed rules retained and evidence/validation caveats. No configured external tracker.

Independent spec QA: clarified coordinator-only migration authorship; owner membership revoke/disabled-user behavior; separate private/authenticated/demo test fixtures; ticket 11 now depends on recommendation review (06). Ticket01 explicitly closes operator-token privacy before member identity follows.

## 2026-10-18 — Dispatch and research delivery

- Integration branch `codex/farmcore-revamp`; draft PR https://github.com/LucasGeno/farmcore/pull/6.
- Access chat `01a150fa-4d94-7840-9845-c8783c9319bb`: tickets 01→02→03, checkout `/Users/Lucas.reed/.codex/worktrees/farmcore-private-access/FarmCore`, feature branch `codex/farmcore-private-access`.
- Weather chat `01a150fa-514f-7253-8c93-373858e739c0`: ticket04, checkout `/Users/Lucas.reed/.codex/worktrees/farmcore-live-weather/FarmCore`, feature branch `codex/farmcore-live-weather`.
- Independent spec QA clarified migration ownership, immediate membership revocation, safe test fixtures, capture ownership and review dependency. All incorporated before implementation integration.
- Research delivered to `/Users/Lucas.reed/Documents/lucas-vault/projects/EU Agri Hackathon/2026-10-18-project-case-studies.md`: 10 case studies + index + ecosystem/data note, linked existing event notes. Source-inspected, tests not executed; attribution/licence limitations explicit.
- Docker Desktop started and local PostGIS 17/3.5 dev server verified accepting connections on localhost55442. Existing test fixture creates a unique disposable database per session; no production migrations run.
- Remaining frontier after access02: reviewed parameters05 and audited corrections09. Planning07 needs weather04 + parameters05. Completion11 needs review06 + scheduling08 + broader capture10. Final independent QA12 follows all workstreams.

## 2026-10-18 — Integration ownership and dependency checks

- Integration ownership transferred to chat `01a150fc-0888-7131-94db-523b8c029daa`; original parent stops checkout edits. Existing access/weather chats continue assigned scopes and report to integration.
- Fresh integration baseline at `d0f44f7`: Ruff check/format pass (442 files); frontend Node tests 30 pass; one Alembic head `inventory_0001`. Full Python regression running; no completion claim yet. Generic preflight mypy probe unavailable (no configured mypy).
- Weather coordination found advisor empty historical observations can still produce a reassuring no-risk headline. Ticket05 owns the missing/insufficient-observation guard (including absent station), explicit unavailable assessment and regression evidence. Ticket04 retains forecast ingestion/store/ranking ownership; M1 is not complete until this advisor guard is verified.
- Read-only planning exploration saved to `/tmp/farmcore-planning-seams.md`: stock is computed from done canonical logs, completion reuses canonical source merging, farm advisory lock precedes row locks, and reevaluation follows successful commit. This is implementation context, not a second coordination tracker.

- Integration pre-feature non-data regression completed: `uv run pytest -m 'not data' -q` — 2656 passed, 3 skipped, 268 deselected in 311.40s. Skips remain skips; data-marked cases not exercised in this baseline. Gauntlet secrets self-test rejects 4/4 bad controls and accepts 2/2 benign controls.
- Weather QA extends ticket04 ownership to receipt-kind wording in notifications/service.py and assistant/tools.py; fetched receipt time must not be presented as provider-issued time. Access coordinator informed of overlapping files.

- Full pre-feature randomized baseline: 2901 passed, 25 skipped, one inherited fixture-order failure in 1450.04s (seed1025145244). Module-only reproduction red1/green4 when only random reordering is disabled confirms its documented writer-before-reader contract. Final gates randomize ordinary cases and run these four unchanged lifecycle cases separately in order; no product/harness fix introduced. Final full data regression remains distinct from approved non-data feature coverage gates.
- Weather draft PR7 commit318873c independently reviewed; two public-job probes failed (extreme offset timestamp rolls back prior farm, response cap checked after buffered download). Worker fixes committed97084be; root reviewer rechecking both public probes. PR remains draft pending fresh evidence.

## 2026-10-19 — Recovered verification and continued execution

- Original parent returned integration checkout ownership at clean `3998ce9`; root owns subsequent integration edits. Parent retains PR7 final review disposition pending a concrete total-deadline fix; root must not merge PR7 until that review is green. Aggregate PR6 remains incomplete.
- Existing dev PostGIS was down after interruption. Original parent restored only the existing farmcore-local-db-1; throwaway migrated fixtures can again use localhost55442. Interrupted/empty logs and environment exit3 runs are not passes.
- Access draft PR8 at `3d9862a` contains the reviewed source/tests (diff SHA256 `4a2ddf84dd7dd27da0d1c24c3f29fcb3976f1736f69bf12eb513ccf9c50d7d18`). Fresh root QA: no Spec/Standards findings, 8 public adversarial probes passed in1.46s. Fresh focused gate failed: 2 obsolete anonymous-audit-view expectations, 1401 passed, 3 skipped, 58 deselected in241.11s; changed coverage71/72 (98%). Worker debugging/adapting the approved admin-only audit contract and covering the missing behavior; PR remains draft/unmerged.
- PR7 at `97084be` had both initial root QA defects fixed (timestamp overflow isolation, streaming byte limit) and independently rerun green. Parent then reproduced a slow-drip response holding the global job lock beyond per-read timeout; weather worker owns an enforced total header/body deadline and real loopback TDD. No final green gate or merge claimed.
- Read-only correction preparation at `/tmp/farmcore-corrections-seams.md` records existing observation/irrigate types, computed stock semantics, audited revision/retry/stale-preview requirements across imports. Parameters05 and corrections09 remain blocked on member identity02.

## 2026-10-19 — Frozen local evidence and speculative identity work

- PR8 frozen head `cef01d7` (same source/tests as `f8861ef`): final local tier3 at base3b7cca7/seed19012026 passed 1404, skipped 3, deselected 58 in247.39s; changed coverage100%; fresh4 manual mutants killed/restored, root8 public probes pass, ordered4 and visitor2 pass. Exact-head hosted run39281467499 remains queued; not merged/done. Local Linux ARM64 Docker build/import smoke passed separately.
- PR7 frozen head `c6762c8`: final feature tier3 at base3b7cca7/seed2688013886 passed 711, skipped 3, deselected 136 in251.55s; changed coverage100%; ordered4 pass; seven manual mutants killed with unchanged original source; fresh parent5-case liveness review clear. Exact-head hosted run39281777500 remains queued; not merged/done. Type/dependency-audit tools unavailable in that feature environment were explicitly skipped; manual mutation/smoke fallbacks reported. Historical-observation guard remains ticket05.
- Superseded queued workflow cancellation requests were rejected as already completed, while reads still report queued. No cancellation succeeded; no result inferred and no CI settings changed.
- Ticket02 now proceeds speculatively from frozen01 in `/Users/Lucas.reed/.codex/worktrees/farmcore-member-sessions/FarmCore`, branch `codex/farmcore-member-sessions`. Its dependency on unmerged01 remains explicit; no02 merge before01 exact-head CI and integration gate. Access coordinator exclusively authors02/03 migrations; workers propose schema. Single `core_0003` identity revision and eight schema/legacy-preservation checks pass locally; role/session work is ongoing, not complete.
- Isolated02 public-auth UI slice reports HTTP/browser RED→GREEN for public logout landing and no unauthorized widgets; four HTTPS desktop/mobile normal/reduced-motion journeys have no page errors/private polls/overflow. Early QA additionally caught and fixed lost Sunlight preference. Root must independently rerun the final frozen02; final12 browser blocker is not yet cleared.
- Root created an isolated combined validation worktree `/Users/Lucas.reed/.codex/worktrees/farmcore-combined-qa/FarmCore` at aeb7ece, applying exact frozen01/04 patches without conflicts. Staged validation tree `53e48c630df30f58b08e051a7fc675ccced3ee3a`; ordinary randomized non-data suite+coverage running (seed19012026), four lifecycle tests separate. This is test input only: no feature/integration branch merge or milestone completion implied.

## 2026-10-19 — Accepted installation and module architecture

- User approved independently isolated hosted/self-hosted installations and modular architecture, and requested a spec plus implementation tickets while current QA/merges continue.
- Published the subsequent initiative under docs/work/isolated-instances-modules/: one Change Spec, a dependency index and 20 bounded tickets. Same OSS runtime/profile across hosting modes; separate customer runtime/database/files/secrets; optional hosting manager; durable tasks/outbox; module lifecycle and compatible neutral-domain migration; weather-risk and fertilization-record proof apps.
- Merchant checkout/provider selection, production provisioning/deployment, unrestricted hosted code uploads, permanent data deletion and new scientific recommendation algorithms remain out of scope. This publication does not claim runtime implementation.
- Core/worker/module/domain changes depend on current revamp integration QA. Packaging/recovery have explicit privacy/identity prerequisites. Current PRs retain independent exact-head QA/CI and final revamp acceptance; no status in the older log is upgraded by this entry.
- Durable accepted rationale recorded through /doc in the existing EU Agri Hackathon vault folder; ticket/task state remains solely in this repository. Domain glossary distinguishes Customer, Installation, Farm, Module profile, Planned job and Background task.
- Documentation verification: 20 contiguous Work Tickets; valid YAML; acyclic dependencies with all19 ancestors reaching final20; 24 repository Markdown files link-checked; append-only log preserved. Independent review found and resolved outbox/module ordering, premature module-configuration scope and consistent database/files recovery requirements; final recheck reported no remaining findings. Ruff check/format passed (442 unchanged Python files). No product implementation or product-test pass claimed for this publication.

## 2026-10-19 — Reshaped installation and module initiative (revision 2)

- Critical review of revision 1 found: hosting-platform scope (portal, control DB, provisioning state machine) ahead of any customer; a 13-deep serial graph that made fertilization wait on customer suspension; a contradiction between modules shipping migrations and a single coordinator-only chain; a hand-rolled lease/fencing queue where Postgres libraries exist; a database/file backup consistency problem that does not exist (no private bytes persist outside Postgres today: voice notes use a tempdir, ISOXML is parsed in memory).
- Lucas asked for the initiative to be reshaped. Revision 2 records decisions D1–D7 with rejected alternatives: one Compose project per installation (shared-process multi-site and shared hosted installation rejected, revisit trigger on measured cost); private bytes in Postgres; Procrastinate-or-minimal-table spike with app-owned publication fence; transactional outbox with an `external_effects` pause; entry-point apps with per-app Alembic branches; expand–migrate–contract with `pint` units; hosting as declarative operator fleet tooling, deferred behind a demand trigger.
- 20 tickets replaced by 18 in four independent tracks (portable, background work, neutral domain, module ecosystem) plus deferred ticket 18 (hosting). Rev1→rev2 mapping is in tickets.md. The vault architecture note still describes revision 1 and needs a `/doc` update.
- No product implementation or test pass claimed.
- Independent read-only review of revision 2 found 15 issues; all applied. Main ones: forecast, chat and kpi depend on advisor (ticket 14 now decouples them); the real outbound effect is `chat.push_advice`, not notifications (02 and 07 retargeted); split owner/runtime DB roles; restore fences queued tasks; Decimal-only units; explicit Alembic branch shape and a build-time bundle. Links, YAML and acyclic dependencies re-checked.
