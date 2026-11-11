# PRODUCT.md

**Who loses most.** The student who trusts a wrong key and *learns the wrong thing*, and the diligent one who is right and gets marked wrong (R020, R021). A broken question hurts every student who meets it, until someone gets to it. So the queue is ordered by **harm × distinct reporters**: wrong/unsound key and wrong solutions first, missing data next, typos last. Closed cases and auto-applied fixes are out of the way so faculty at 10 PM see only what needs a human decision.

**Never auto-apply:** (1) anything that **authors** content: new distractors, missing figures, a lost option that isn't recoverable from the record; (2) an answer key without ≥2 independent confirmations; (3) chapter/syllabus moves (a judgment call, so the question stays live meanwhile); (4) anything triggered by a comment that tries to instruct the system. Code, not the model, enforces scope: a repair may only touch its listed fields and every `find` must match exactly once.

**Week-1 metric:** *Time from first report to a correct fix on defect questions*, split by auto vs human. Guardrail: **rollback rate of auto-applied edits** (if >2% reviewed edits are reverted, tighten the guards).

**Beyond the brief:** use the engine **before** students report: run the key-blind solver across all ~8 lakh questions and flag disagreements. In this sample the stored solution contradicted the key in 3 cases (R001, R004, R005) that a cheap check finds with no student ever hitting them.

**Student replies:** short, warm, and when the student is wrong, show the one step that fixes their working (R029, R044), so a report becomes a micro-lesson rather than a rejection.
