---
name: Deploy after task-agent merge
description: After any task-agent merges (MERGED status), main agent MUST immediately restart workflow + run bash deploy.sh without waiting for user instruction.
---

# Deploy after task-agent merge

## Rule
When `[MERGED] Task #N` appears in automatic_updates, the main agent MUST:
1. `restart_workflow("Start application")` — pick up merged code
2. Check logs briefly (no errors)
3. `bash deploy.sh "описание изменений (#N)"` — directly, never as a background task or Project Task

No waiting for user permission. No asking "should I deploy?". Just do it.

**Why:** User explicitly requested this and had to repeat the request multiple times. The deploy is always GitHub + Amvera. The merge is already approved by the user — deploy is the expected next step.

**How to apply:** Every time automatic_updates contains `[MERGED] Task #N`, trigger the deploy sequence immediately as the next action.
