# Local Agent development rules

## Product behavior

- Treat the runtime tool registry in `agent.py` as the source of truth.
- Never present a plan, generated script, or attempted command as a completed action.
- Claims about files, processes, tests, applications, or external actions require observed tool evidence.
- Missing capabilities must produce a clear limitation instead of a fabricated workaround.
- External side effects use two phases: prepare and preview first, then execute only after explicit confirmation.
- Intermediate model commentary belongs in the activity panel; the conversation shows only the final answer.

## Implementation

- Put enforceable capability and safety checks in Python, not only in prompts.
- Keep tools small, typed, cancellable, and bounded by timeouts.
- Add a regression test for every deterministic routing or safety rule.
- Preserve the local-only architecture and avoid adding large dependencies without documenting disk impact.

## Verification

Run these checks after runtime or UI changes:

```powershell
py -m unittest discover -s tests -v
py -m py_compile agent.py web_server.py
node --check web\app.js
```
