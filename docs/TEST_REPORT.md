# Test and performance report

Run date: 15 November 2026. Everything below was measured on the prototype in this repository. Nothing is estimated.

## Summary

| Measure | Result | Sample |
|---|---|---|
| Automated tests | 29 of 29 pass | Review app, with n8n replaced by a stub |
| Workflow reliability | 8 of 8 requests returned a usable result | 8 synthetic requests |
| Processing time per request | Median 298 s (range 275 to 358 s) | 8 requests, 4 CPU cores, no graphics card |
| Stated facts captured | 24 of 31 (77%) | Answer key in `data/sample_requests.json` |
| Missing information detected, AI only | 18 of 26 (69%) | Same answer key |
| Missing information detected, AI plus fixed rules | 20 of 26 (77%) | Same answer key |
| "Stated" labels that passed the quote check | 23 of 35 (66%) | All "stated" claims in the run |
| Suggested task owners not present in the request | 4 of 20 (20%) | All suggested owners in the run |
| Deadline wrongly listed as missing | 6 of 6 requests that supplied a date | Requests with a date |

**Not measured:** summary usefulness, human correction rate, and any time saving against manual work. These need a human reviewer and are described under "What still needs a person".

**No productivity claim is made.** Five minutes of unattended processing per request says nothing about time saved until a person has done the same task by hand under the same conditions.

## Test environment

| Item | Value |
|---|---|
| Machine | Linux cloud container, 4 CPU cores, 15 GB memory, no graphics card |
| Model | Qwen2.5 7B, 4-bit quantised (`qwen2.5:7b`), Apache 2.0 |
| Ollama | 0.40.16 |
| n8n | 2.35.7, Community Edition |
| Setup | Ollama, n8n and the review app run directly on the machine, not in Docker |

## 1. Automated tests of the review app

`python -m pytest tests` runs 29 tests. They cover:

| Area | What is checked |
|---|---|
| Valid input | A valid request is stored, audited and handed to the workflow |
| Invalid input | Empty name, empty department, too-short and too-long description, invalid priority, wrong date format, past date, missing submitter: each is rejected with a message and nothing is stored |
| Unsafe input | HTML in a field is escaped, not executed |
| Missing information | Missing date, stakeholders or objective each raise a fixed "system check"; a deadline under 14 days is flagged |
| AI output handling | AI items arrive as pending and are absent from the project record; a "stated" label without a real quote is downgraded; risks are never recorded as stated |
| Error handling | Malformed AI output, an AI error, and an unreachable n8n each mark the request as failed without crashing; retry works |
| Security of the callback | A wrong or missing token is refused; a result cannot be delivered twice |
| Human approval | Accept, edit and reject behave correctly; a reviewer name is required; a bad due date is refused; the stakeholder update stays a draft until approved |
| Audit trail | Each decision is logged; the log cannot be updated or deleted |
| No outbound channel | The app's only outbound call is the trigger to n8n |

These tests use a stand-in for n8n and the model. They prove the control logic, not the quality of the AI.

## 2. End-to-end evaluation with the real model

`python eval/run_eval.py` submits each sample request through the intake form, waits for n8n and the model, and compares the result with an answer key. Raw output: `eval/results/eval-20261115-045517.json`.

| Request | Seconds | Items proposed | Stated facts found | Gaps found (AI) | Gaps found (AI + rules) | Missed |
|---|---|---|---|---|---|---|
| R01 CRM migration | 277 | 14 | 2 of 4 | 3 of 3 | 3 of 3 | invoicing export, named owner |
| R02 Onboarding checklist | 275 | 16 | 3 of 4 | 2 of 3 | 2 of 3 | named owner; which tool |
| R03 Board report automation | 358 | 18 | 4 of 4 | 2 of 3 | 3 of 3 | |
| R04 Cookie banner (vague on purpose) | 275 | 14 | 2 of 2 | 2 of 4 | 3 of 4 | unclear requirements |
| R05 Barcode scanning pilot | 300 | 12 | 2 of 5 | 2 of 3 | 2 of 3 | named owner, pilot length, error-rate comparison; vendor |
| R06 Support knowledge base | 305 | 15 | 4 of 4 | 3 of 4 | 3 of 4 | which tool |
| R07 Invoice approval workflow | 335 | 17 | 4 of 4 | 2 of 3 | 2 of 3 | invoice volume |
| R08 Office move | 297 | 13 | 3 of 4 | 2 of 3 | 2 of 3 | team size; who does the moving |

### What the numbers show

- **The workflow is dependable on this sample.** All eight requests completed with output that matched the required structure. Eight runs are too few to state a failure rate.
- **It is slow on a CPU.** About five minutes per request, for three model calls. Acceptable for a background step, not for a live conversation. A machine with a graphics card would be much faster; that was not tested.
- **About three quarters of the key facts were captured.** The most common miss was a named person who "will" do something: in three of the four requests that contained one, the person was not attached to a task.
- **Missing-information detection is useful but generic.** The model reliably names budget, owner and success criteria. It rarely names gaps specific to the request, such as which vendor or how many invoices.
- **The fixed rules earned their place.** Two gaps the model missed were caught by simple rules that involve no AI.

### Errors the review step exists to catch

These were found by inspecting the stored output. They are the reason no AI output is saved without review.

| Error | How often | What happens in the prototype |
|---|---|---|
| The model labelled an item "stated" but its quote was not in the request | 12 of 35 claims | Automatically relabelled as "AI suggestion". Most were paraphrases of real content, not inventions, so the check is strict by design |
| The model listed a deadline as missing although a date was supplied | 6 of 6 dated requests | Reviewer rejects it. This is a consistent false alarm |
| The model suggested an owner that does not appear in the request, such as "Quality assurance team" | 4 of 20 suggested owners | Shown as an AI suggestion; reviewer changes or clears it before accepting |
| The summary did not mark inferred content with "(Suggested)" as instructed | 7 of 8 summaries | The whole summary is a draft until approved |

One thing the model did consistently well: all 8 stakeholder updates correctly reported that no work had been completed, and none invented progress.

## 3. What still needs a person

The following measures from the test plan cannot be produced by a script. The protocol below is ready to run; the figures are intentionally blank.

| Measure | How to measure it | Result |
|---|---|---|
| Human correction rate | Review all 8 requests in the app, then open `/api/review-stats`. Rate = (edited + rejected) / reviewed | Not yet measured |
| Summary usefulness | Score each of the 8 summaries from 1 to 5 on accuracy, completeness and readiness to send | Not yet measured |
| Baseline: manual processing time | For 4 of the requests, time yourself writing the task list, risk list and stakeholder update by hand | Not yet measured |
| AI-assisted time | For the other 4, time yourself reviewing and correcting the AI output in the app | Not yet measured |
| Comparison | Swap the two groups with a second reviewer if possible, so the result does not depend on which requests were easier | Not yet measured |

## 4. Limitations

- **Small sample.** Eight synthetic requests written for this project. Results on real requests will differ.
- **The answer key measures recall by keyword.** It shows whether a fact was captured, not whether every proposed item is correct. Precision was only spot-checked, as listed above.
- **One prompt revision was made** after reading the output for request R01, which is also in the test set. A stricter test would hold that request out.
- **One model, one machine.** No comparison with a smaller or larger model, and no graphics-card timing.
- **Docker Compose was not run.** Container image downloads were cut off in the development environment. The stack was tested with the same components running directly.
- **English only.** German-language requests were not tested.
- **Single user, no login.** The review app is a local demonstration and must not be exposed to a network.
- **The reviewer's name is typed, not authenticated.** The audit log records who the reviewer said they were.
