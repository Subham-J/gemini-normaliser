# gemini-normaliser

Runtime guardrails that make Gemini behave like a disciplined coding agent in **Antigravity** (desktop app, `agy` CLI, and T3 Code through its Antigravity bridge) and **Gemini CLI**.

Most Gemini reliability advice is prose: a longer `GEMINI.md`, more rules. The usual failures come from Gemini drifting off those rules in long sessions. gemini-normaliser turns the rules that matter into **hooks that run on every turn and tool call**. When Gemini breaks one, it gets a tool error with a specific next step.

One Python file, stdlib only, Python 3.9+. Fails open: if the guard breaks, your session keeps going.

## What it stops

| Gemini failure | What the guard does |
|---|---|
| "Plan only" / "don't change anything" turns into edits ([#13501], [#24814]) | Read-only lock from the user's wording. File writes, edits and mutating shell are denied until the user says go ahead |
| Exact-string edits fail, then get retried from memory ([#3109], [#10538], [#13389]) | Every edit is checked against the file first. A missing target is refused with the nearest real lines shown |
| The same failing call, over and over ([#5761], [#22141]) | An identical call that failed twice is blocked. The failure is classified (path, patch-mismatch, permission, syntax, dependency, timeout, test-failure, runtime) with a next step |
| Overwriting the user's concurrent edits ([#27090]) | File hash at last read compared before every write. Changed on disk → re-read first |
| Full-file rewrites that erase comments | A rewrite dropping ≥5 comment lines is refused once |
| Instructions forgotten as the session grows ([#11799], [#17093], [#26123]) | A 4-line contract and the current mode are re-injected each user turn |
| Declaring "done" without checking | After edits, Gemini is sent back once to run `git diff` and the tests and end with `STATUS: SUCCESS / FAILURE / BLOCKED / NEEDS_USER / NEEDS_REPLAN` |
| Destructive shell | `rm -rf`, `git reset --hard`, force push, `git clean -f`, `git branch -D` are denied |
| Precious files | `protected_paths` are append-only: no overwrite, move or delete |
| Runaway loops and cost | Per-turn tool-call budget (default 120) |
| Huge tool output wedging the session ([#27738]) | Gemini CLI only: output over 40k chars goes to disk; the model gets the head, tail and path |

Rules a hook cannot judge (scope creep, over-engineering, anchoring on one hypothesis) live in a short model-facing contract, [`guard/contract.md`](guard/contract.md), loaded through `GEMINI.md`.

## Install

```bash
git clone https://github.com/Subham-J/gemini-normaliser
python3 gemini-normaliser/install.py /path/to/your/project --protect data/app.db
```

That writes, inside your project only:

```
.gemini/guard/          gemini_guard.py, contract.md, tests
.agents/hooks.json      Antigravity hooks (merged; your other hooks are kept)
.gemini/settings.json   Gemini CLI hooks (merged; your other settings are kept)
.gemini/guard.json      config: protected_paths and budgets
GEMINI.md               one import line added
.gitignore              .gemini/tmp/ added
```

Run it again to update, with `--check` to see status, or with `--uninstall` to remove it. Antigravity picks up `.agents/hooks.json` with the workspace; nothing else to do. Gemini CLI loads project hooks only in a trusted folder (`/permissions` → trust).

**Check your rule files' size.** Antigravity keeps only the first 24,000 bytes of each `AGENTS.md` / `GEMINI.md`. `install.py --check` flags any file over the cap. If yours is longer, move reference sections to the end so the rules that matter stay in.

## Configure

`.gemini/guard.json`:

```json
{
  "protected_paths": ["data/app.db", "notes/archive"],
  "max_same_failure": 2,
  "max_tool_calls_per_turn": 120,
  "max_output_chars": 40000,
  "comment_loss_threshold": 5
}
```

Protected paths take new files and in-place edits. Overwrites, moves and deletes are denied.

## How it works

`gemini_guard.py` is one script with two adapters. Both feed the same gates.

| | Antigravity (`--agy`) | Gemini CLI |
|---|---|---|
| wiring | `.agents/hooks.json` | `.gemini/settings.json` |
| before a tool | `PreToolUse` | `BeforeTool` |
| after a tool | `PostToolUse` | `AfterTool` |
| each turn | `PreInvocation` (injects an ephemeral message) | `BeforeAgent` |
| before finishing | `Stop` (`decision: continue`) | `AfterAgent` (`decision: deny` = retry) |

Session state lives in `.gemini/tmp/guard/<session>.json`: failure counts, read hashes and mode. That file is also the quickest way to see what fired.

### Antigravity quirks found while building this (agy, October 2026)

- **PreToolUse must always return a decision.** `{}` is read as *deny*. The guard returns `ask` when it has no objection; measured to behave the same as having no hook.
- **A failed command arrives with an empty `error`.** The result (`The command exited with code N`) is in the transcript at the same step index, written *after* PostToolUse runs. The guard scores it on the next event.
- **PostToolUse cannot talk to the model.** Failure notes go out as an `ephemeralMessage` on the next `PreInvocation`.
- **A normal finish is `terminationReason: NO_TOOL_CALL`**, not `model_stop`.
- **Rule files are capped at 24 KB each**, and imports use `@[label](path)`, not `@./path`.
- Tool names: `run_command`, `view_file`, `replace_file_content`, `multi_replace_file_content`, `write_to_file`.

## Debug and test

```bash
touch .gemini/guard-debug          # log every hook payload to .gemini/tmp/guard/debug.jsonl
python3 -m unittest discover -s .gemini/guard     # in an installed project
python3 -m unittest discover -s guard && python3 -m unittest discover -s tests   # in this repo
```

## Not built, on purpose

- **A tool router.** Built-in toolsets sit inside Google's 10–20 tool guidance.
- **An AST patch engine.** The edit pre-check catches the measured failure for a fraction of the cost.
- **A harness that owns the agent loop.** The hooks get most of the control without replacing Antigravity's or Gemini CLI's loop.

PRs that measure a failure first are welcome.

## Limits

- The read-only lock is triggered by wording. It catches "plan only", "don't change anything", "read-only", "review only" and "just explain". Scoped phrases like "don't commit" or "don't touch X" deliberately do not lock everything.
- Shell mutation detection is a regex, not a sandbox. Use your harness's sandbox for real isolation.
- Nothing here proves Gemini got better on *your* tasks. Run the same task with and without it.
- In T3 Code, it runs through T3's Antigravity bridge, which ships the same hook engine. Only the `agy` CLI was tested live.

Not affiliated with Google.

[#3109]: https://github.com/google-gemini/gemini-cli/issues/3109
[#5761]: https://github.com/google-gemini/gemini-cli/issues/5761
[#10538]: https://github.com/google-gemini/gemini-cli/issues/10538
[#11799]: https://github.com/google-gemini/gemini-cli/issues/11799
[#13389]: https://github.com/google-gemini/gemini-cli/issues/13389
[#13501]: https://github.com/google-gemini/gemini-cli/issues/13501
[#17093]: https://github.com/google-gemini/gemini-cli/issues/17093
[#22141]: https://github.com/google-gemini/gemini-cli/issues/22141
[#24814]: https://github.com/google-gemini/gemini-cli/issues/24814
[#26123]: https://github.com/google-gemini/gemini-cli/issues/26123
[#27090]: https://github.com/google-gemini/gemini-cli/issues/27090
[#27738]: https://github.com/google-gemini/gemini-cli/issues/27738

## License

MIT
