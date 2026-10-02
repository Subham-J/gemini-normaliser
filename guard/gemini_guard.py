#!/usr/bin/env python3
"""gemini guard: runtime gates for Gemini CLI hooks.

Prose rules decay in long Gemini sessions (see contract.md). This script turns
the ones that matter into checks that run on every turn and tool call:

  BeforeAgent  re-anchor contract + latest directive; read-only lock from wording
  BeforeTool   read-only gate, protected paths, destructive shell, retry limit,
               stale-file check, replace pre-check, comment-loss check, budget
  AfterTool    failure classification, read hashes, output size cap
  AfterAgent   verify-before-done gate after edits

Usage: gemini_guard.py <EventName>            (Gemini CLI, .gemini/settings.json)
       gemini_guard.py --agy <EventName>      (Antigravity app / agy / T3 Code, .agents/hooks.json)
GEMINI_GUARD_DEBUG=1 (or a file .gemini/guard-debug) logs every payload to .gemini/tmp/guard/debug.jsonl.
Reads hook JSON on stdin, prints one JSON object on stdout. Stdlib only.
Fails open: any internal error prints {} and exits 0.
"""
import difflib
import hashlib
import json
import os
import re
import sys
import time

GUARD_VERSION = "1"

DEFAULTS = {
    "max_same_failure": 2,          # identical failing call allowed this many times
    "max_tool_calls_per_turn": 120,
    "max_output_chars": 40000,      # bigger tool output goes to disk, not context
    "comment_loss_threshold": 5,    # write_file dropping this many comment lines warns once
    "protected_paths": [],          # append-only folders: new files and replace ok; no overwrite, move, delete
}

WRITE_TOOLS = {"write_file", "replace"}
READ_ONLY_RE = re.compile(
    r"\b(plan only|only plan|just plan|review only|only review|read[- ]only|"
    r"just (look|explain|analy[sz]e|review|tell me)|"
    r"(do not|don'?t|dont) (change|edit|modify|touch|write) (anything|any (files?|code))|"
    r"(do not|don'?t|dont) (implement|build|execute) (it|this|anything|yet)|"
    r"no (code )?(changes|edits)( yet)?|without (changing|editing) anything)\b", re.I)
UNLOCK_RE = re.compile(
    r"\b(go ahead|implement( it| this| the plan)?|execute|apply( it| the)?|make the change|"
    r"do it|proceed|fix it|build it|approved)\b", re.I)
MUTATING_SHELL_RE = re.compile(
    r"(^|[;&|]\s*)(rm|mv|cp|mkdir|touch|chmod|chown|ln|truncate|tee)\b|"
    r"\bsed\s+-i|\bperl\s+-p?i|>\s*(?!/dev/null)[^&\s]|"
    r"\bgit\s+(commit|push|reset|checkout|restore|clean|rebase|merge|stash|rm|mv|add)\b|"
    r"\b(pip3?|npm|pnpm|yarn|brew|uv)\s+(install|add|remove|uninstall)\b|"
    r"\b(wrangler|deploy\.sh|ship\.sh)\b", re.I)
DESTRUCTIVE_SHELL_RE = re.compile(
    r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r|\bgit\s+reset\s+--hard|"
    r"\bgit\s+push\s+(-f|--force)|\bgit\s+clean\s+-[a-z]*f|\bgit\s+checkout\s+--\s|"
    r"\bgit\s+branch\s+-D|\bfind\b.*\s-delete\b|\bdrop\s+table\b", re.I)
VERIFY_SHELL_RE = re.compile(
    r"\b(pytest|unittest|git\s+diff|git\s+status|npm\s+(run\s+)?test|node\s+--test|"
    r"check_paths|check\.py|ruff|mypy|tsc|make\s+test|test_\w+\.py|\.test\.)", re.I)
STATUS_RE = re.compile(r"\bSTATUS:\s*(SUCCESS|FAILURE|BLOCKED|NEEDS_USER|NEEDS_REPLAN)\b")
COMMENT_RE = re.compile(r"^\s*(#|//|/\*|\*|<!--|--|;)")

ERROR_CLASSES = [
    ("patch-mismatch", r"0 occurrences|could not find the string|failed to edit|old_string"),
    ("path", r"no such file|not found|does not exist|enoent|is a directory|not a directory"),
    ("permission", r"permission denied|eacces|operation not permitted|not allowed"),
    ("syntax", r"syntaxerror|indentationerror|unexpected token|parse error"),
    ("dependency", r"modulenotfound|importerror|cannot find module|command not found"),
    ("timeout", r"timed out|timeout"),
    ("stale-state", r"modified since|has changed|stale"),
    ("test-failure", r"\d+ failed|assertionerror|failures?:|not ok"),
    ("runtime", r"traceback|exception|error"),
]
NEXT_STEP = {
    "patch-mismatch": "re-read the exact lines, copy old_string from the read, keep it to the few lines that change",
    "path": "list the directory or glob for the real path; do not guess it again",
    "permission": "stop; this needs the user or a different path",
    "syntax": "read the reported line, fix only that, re-run",
    "dependency": "check the project's run command or venv; do not install without asking",
    "timeout": "narrow the command or run it in the background; do not repeat as is",
    "stale-state": "re-read the file, then re-plan the edit",
    "test-failure": "read the first failing assertion; fix the cause, not the test",
    "runtime": "read the traceback's last frame in project code; test one hypothesis",
    "unknown": "state two hypotheses and the check that separates them",
}

CONTRACT_REMINDER = (
    "[guard] contract re-check (.gemini/guard/contract.md): obey the LATEST user message over earlier momentum; "
    "stay inside the stated scope, no unrequested features or refactors; one hypothesis is not a diagnosis; "
    "same failing call twice -> change method; after edits run git diff + the tests, then end with "
    "'STATUS: SUCCESS|FAILURE|BLOCKED|NEEDS_USER|NEEDS_REPLAN'."
)


def project_dir(payload):
    return os.environ.get("GEMINI_PROJECT_DIR") or payload.get("cwd") or os.getcwd()


def guard_dir(root):
    d = os.path.join(root, ".gemini", "tmp", "guard")
    os.makedirs(d, exist_ok=True)
    return d


def load_config(root):
    cfg = dict(DEFAULTS)
    try:
        with open(os.path.join(root, ".gemini", "guard.json")) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def state_path(root, session):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session or "default")[:80]
    return os.path.join(guard_dir(root), safe + ".json")


def load_state(root, session):
    try:
        with open(state_path(root, session)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"read_only": False, "failures": {}, "reads": {}, "warned": [],
                "turn_calls": 0, "dirty": False, "verified": True, "directive": "", "retried": False}


def save_state(root, session, state):
    path = state_path(root, session)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return None


def abspath(root, p):
    if not p:
        return ""
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(root, p))


def call_key(tool, args):
    blob = json.dumps(args, sort_keys=True, default=str)
    return tool + ":" + hashlib.sha1(blob.encode()).hexdigest()[:16]


def is_protected(root, path, cfg):
    rel = os.path.relpath(path, root) if path else ""
    for pat in cfg.get("protected_paths", []):
        if rel == pat.rstrip("/") or rel.startswith(pat.rstrip("/") + "/"):
            return pat
    return None


def deny(reason):
    return {"decision": "deny", "reason": "[guard] " + reason}


def response_text(resp):
    if not isinstance(resp, dict):
        return str(resp or "")
    parts = []
    for k in ("error", "llmContent", "returnDisplay"):
        v = resp.get(k)
        if v:
            parts.append(v if isinstance(v, str) else json.dumps(v, default=str))
    return "\n".join(parts)


def failed(tool, resp):
    if not isinstance(resp, dict):
        return False
    if resp.get("error"):
        return True
    text = response_text(resp)
    if tool == "run_shell_command":
        m = re.search(r"Exit Code:\s*(-?\d+)|exited with code (-?\d+)", text)
        return bool(m and (m.group(1) or m.group(2)) != "0")
    return bool(re.match(r"\s*(error|failed)", text, re.I)) or "0 occurrences" in text


def classify(text):
    low = text.lower()
    for name, pat in ERROR_CLASSES:
        if re.search(pat, low):
            return name
    return "unknown"


# ---- events -----------------------------------------------------------------

def before_agent(p, root, cfg, st):
    prompt = p.get("prompt") or ""
    if prompt.startswith("[guard]"):
        return {}  # our own AfterAgent retry, not a new user directive
    if READ_ONLY_RE.search(prompt):
        st["read_only"] = True
    elif UNLOCK_RE.search(prompt):
        st["read_only"] = False
    st["directive"] = prompt[:400]
    st["turn_calls"] = 0
    st["retried"] = False
    mode = "READ-ONLY (no write_file/replace/mutating shell until the user says go ahead)" if st["read_only"] else "EXECUTE"
    open_fail = sum(1 for v in st["failures"].values() if v.get("n", 0) >= cfg["max_same_failure"])
    ctx = "%s\n[guard] mode: %s. latest directive outranks every earlier plan.%s" % (
        CONTRACT_REMINDER, mode,
        " %d call(s) are retry-blocked this session; use a different method." % open_fail if open_fail else "")
    return {"hookSpecificOutput": {"hookEventName": "BeforeAgent", "additionalContext": ctx}}


def before_tool(p, root, cfg, st):
    tool = p.get("tool_name") or ""
    args = p.get("tool_input") or {}
    st["turn_calls"] = st.get("turn_calls", 0) + 1
    if st["turn_calls"] > cfg["max_tool_calls_per_turn"]:
        return deny("tool budget for this turn is spent (%d calls). stop and report "
                    "STATUS: BLOCKED with what is done and what is left." % cfg["max_tool_calls_per_turn"])

    key = call_key(tool, args)
    f = st["failures"].get(key)
    if f and f.get("n", 0) >= cfg["max_same_failure"]:
        return deny("this exact call already failed %d times (%s). do not retry it. next: %s"
                    % (f["n"], f.get("cls", "unknown"), NEXT_STEP.get(f.get("cls"), NEXT_STEP["unknown"])))

    cmd = args.get("command", "") if tool == "run_shell_command" else ""
    path = abspath(root, args.get("file_path", ""))

    if cmd and DESTRUCTIVE_SHELL_RE.search(cmd):
        return deny("destructive command blocked: %r. ask the user in chat and let them run it." % cmd[:120])

    if st.get("read_only") and (tool in WRITE_TOOLS or (cmd and MUTATING_SHELL_RE.search(cmd))):
        return deny("the user asked for no changes (read-only). report findings or a plan instead; "
                    "the lock lifts when the user says go ahead.")

    if cmd and MUTATING_SHELL_RE.search(cmd):
        for pat in cfg.get("protected_paths", []):
            if pat.rstrip("/") in cmd:
                return deny("%s is append-only (.gemini/guard.json): no shell writes, moves or deletes "
                            "there. edit with replace, or ask the user." % pat)
    if tool == "write_file" and path and os.path.exists(path):
        hit = is_protected(root, path, cfg)
        if hit:
            return deny("%s is append-only (.gemini/guard.json): never overwrite a file there. "
                        "use replace to add text below the existing content." % hit)

    if tool in WRITE_TOOLS and path and os.path.exists(path):
        seen = st["reads"].get(path)
        now = file_hash(path)
        if seen and now and seen != now:
            return deny("%s changed on disk since you last read it (the user, another session or a shell command). "
                        "re-read it, then redo the edit." % os.path.relpath(path, root))

    if tool == "replace" and path and os.path.exists(path):
        r = check_replace(path, args)
        if r:
            return deny(r)

    if tool == "write_file" and path and os.path.exists(path):
        r = check_rewrite(path, args.get("content", ""), cfg, st, key)
        if r:
            return deny(r)
    return {}


def check_replace(path, args):
    old = args.get("old_string") or ""
    if not old:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    n = text.count(old)
    if n == 1 or (n > 1 and args.get("allow_multiple")):
        return None
    if n > 1:
        return ("the edit target text occurs %d times in %s. add 1-2 unique neighbouring lines, or allow "
                "multiple replacements if every one should change." % (n, os.path.basename(path)))
    squash = lambda s: re.sub(r"\s+", " ", s).strip()
    if squash(old) and text.count(old.strip()) == 1:
        return None  # only edge whitespace differs; the tool's own matcher handles it
    if squash(old) and squash(old) in squash(text):
        return None  # whitespace-only drift; let the tool's flexible matcher try
    lines = text.splitlines()
    first = old.strip().splitlines()[0] if old.strip() else ""
    near = difflib.get_close_matches(first, lines, n=1, cutoff=0.6)
    hint = ""
    if near:
        i = lines.index(near[0])
        hint = " nearest real text at line %d:\n%s" % (i + 1, "\n".join(lines[i:i + 4]))
    return ("the edit target text is not in %s (0 matches, whitespace-insensitive too). do not retry from memory: "
            "read the exact lines first and copy them; change only the lines that differ.%s"
            % (os.path.basename(path), hint))


def check_rewrite(path, content, cfg, st, key):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            old = f.read()
    except OSError:
        return None
    old_c = [l.strip() for l in old.splitlines() if COMMENT_RE.match(l)]
    new_c = set(l.strip() for l in content.splitlines() if COMMENT_RE.match(l))
    lost = [c for c in old_c if c and c not in new_c]
    if len(lost) >= cfg["comment_loss_threshold"] and key not in st["warned"]:
        st["warned"].append(key)
        return ("this full rewrite drops %d comment lines (e.g. %r). use replace for the lines that change. "
                "if dropping them is intended, send the identical write again." % (len(lost), lost[0][:80]))
    return None


def after_tool(p, root, cfg, st):
    tool = p.get("tool_name") or ""
    args = p.get("tool_input") or {}
    resp = p.get("tool_response") or {}
    key = call_key(tool, args)
    path = abspath(root, args.get("file_path", ""))
    out = {}
    extra = []

    if failed(tool, resp):
        cls = classify(response_text(resp))
        f = st["failures"].setdefault(key, {"n": 0})
        f["n"] += 1
        f["cls"] = cls
        extra.append("[guard] failure class: %s (attempt %d/%d for this exact call). next: %s"
                     % (cls, f["n"], cfg["max_same_failure"], NEXT_STEP[cls]))
    else:
        st["failures"].pop(key, None)
        if tool == "read_file" and path:
            st["reads"][path] = file_hash(path)
        if tool in WRITE_TOOLS and path:
            st["reads"][path] = file_hash(path)
            st["dirty"], st["verified"] = True, False
        cmd = args.get("command", "") if tool == "run_shell_command" else ""
        if cmd and MUTATING_SHELL_RE.search(cmd):
            st["dirty"], st["verified"] = True, False
        if cmd and VERIFY_SHELL_RE.search(cmd):
            st["verified"] = True

    content = resp.get("llmContent") if isinstance(resp, dict) else None
    if tool != "read_file" and isinstance(content, str) and len(content) > cfg["max_output_chars"]:
        name = "out-%d-%s.txt" % (int(time.time()), re.sub(r"\W", "", tool)[:20])
        dest = os.path.join(guard_dir(root), name)
        with open(dest, "w") as fh:
            fh.write(content)
        rel = os.path.relpath(dest, root)
        return {"decision": "deny", "reason": (
            "[guard] output was %d chars, too big for context; full text saved to %s. "
            "grep or read slices of that file.\n--- head ---\n%s\n--- tail ---\n%s"
            % (len(content), rel, content[:3000], content[-3000:]))}
    if extra:
        out = {"hookSpecificOutput": {"hookEventName": "AfterTool", "additionalContext": "\n".join(extra)}}
    return out


def after_agent(p, root, cfg, st):
    if p.get("stop_hook_active") or st.get("retried"):
        st["dirty"] = False  # asked once; never nag twice
        return {}
    text = p.get("prompt_response") or ""
    need = []
    if st.get("dirty") and not st.get("verified"):
        need.append("you changed files but ran no check since: run `git diff` on the touched files "
                    "and the project's tests, compare against the user's request, and fix scope creep")
    if st.get("dirty") and not STATUS_RE.search(text):
        need.append("end with one line 'STATUS: SUCCESS|FAILURE|BLOCKED|NEEDS_USER|NEEDS_REPLAN'")
    if not need:
        if STATUS_RE.search(text):
            st["dirty"] = False
        return {}
    st["retried"] = True
    return {"decision": "deny", "reason": "[guard] before finishing: " + "; ".join(need) + "."}


HANDLERS = {"BeforeAgent": before_agent, "BeforeTool": before_tool,
            "AfterTool": after_tool, "AfterAgent": after_agent}


# ---- Antigravity adapter (app, agy cli, T3 Code's antigravity-acp) ---------------
# Antigravity hooks live in .agents/hooks.json, use camelCase payloads and their own
# tool names. They are translated to the Gemini CLI shapes above so one set of gates
# serves both. Hook cwd is the folder holding hooks.json.

def _truthy(v):
    return v is True or str(v).lower() == "true"


def agy_tool(call):
    """Antigravity toolCall -> (gemini tool name, gemini args), or (name, None) if ungated."""
    name = (call or {}).get("name") or ""
    a = (call or {}).get("args") or {}
    if name == "run_command":
        return "run_shell_command", {"command": a.get("CommandLine", "")}
    if name == "view_file":
        return "read_file", {"file_path": a.get("AbsolutePath", "")}
    if name == "replace_file_content":
        return "replace", {"file_path": a.get("TargetFile", ""), "old_string": a.get("TargetContent", ""),
                           "allow_multiple": _truthy(a.get("AllowMultiple"))}
    if name == "multi_replace_file_content":
        chunks = a.get("ReplacementChunks") or []
        if isinstance(chunks, str):
            try:
                chunks = json.loads(chunks)
            except ValueError:
                chunks = []
        return "replace", {"file_path": a.get("TargetFile", ""), "chunks": [
            {"old_string": c.get("TargetContent", ""), "allow_multiple": _truthy(c.get("AllowMultiple"))}
            for c in chunks if isinstance(c, dict)]}
    if name == "write_to_file":
        return "write_file", {"file_path": a.get("TargetFile", ""), "content": a.get("CodeContent", "")}
    return name, None


def transcript_steps(path, max_bytes=3000000):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            raw = f.read().decode("utf-8", "replace")
    except (OSError, TypeError):
        return []
    out = []
    for line in raw.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def last_of(steps, kind):
    for s in reversed(steps):
        if s.get("type") == kind:
            return s
    return None


AGY_PASS = {"decision": os.environ.get("GUARD_AGY_PASS", "ask")}  # Antigravity reads {} as deny


def agy_pre_tool(p, root, cfg, st):
    out = _agy_pre_tool(p, root, cfg, st)
    return out if out.get("decision") == "deny" else dict(AGY_PASS)


def _agy_pre_tool(p, root, cfg, st):
    agy_resolve(p, root, cfg, st)
    tool, args = agy_tool(p.get("toolCall"))
    if args is None:
        return {}
    st.setdefault("pending", {})[str(p.get("stepIdx"))] = [tool, args]
    if args.get("chunks") is not None:  # multi-replace: check each chunk as its own replace
        for c in args["chunks"]:
            r = before_tool({"tool_name": "replace", "tool_input": dict(c, file_path=args["file_path"])},
                            root, cfg, st)
            if r.get("decision") == "deny":
                return r
        return {}
    return before_tool({"tool_name": tool, "tool_input": args}, root, cfg, st)


def agy_post_tool(p, root, cfg, st):
    key = str(p.get("stepIdx"))
    if key in (st.get("pending") or {}):
        st.setdefault("done", {})[key] = p.get("error") or ""
        agy_resolve(p, root, cfg, st)
    return {}  # PostToolUse cannot talk to the model; notes go out on the next PreInvocation


def agy_resolve(p, root, cfg, st, force=False):
    """Score finished calls. Antigravity writes a tool's result to its transcript (same step
    index) a moment after PostToolUse, so unresolved steps are retried on later events."""
    done = st.get("done") or {}
    if not done:
        return
    steps = {x.get("step_index"): x for x in transcript_steps(p.get("transcriptPath"), 400000)}
    for key in list(done):
        result = steps.get(int(key)) if key.lstrip("-").isdigit() else None
        if result is None and not done[key] and not force:
            continue  # result not written yet; try again on the next event
        err = done.pop(key)
        tool, args = st["pending"].pop(key, [None, None])
        if not tool:
            continue
        resp = {"llmContent": (result or {}).get("content") or "ok"}
        if err:
            resp["error"] = err
        for c in (args.get("chunks") or [None]):
            a = dict(c, file_path=args["file_path"]) if c else args
            out = after_tool({"tool_name": tool, "tool_input": a, "tool_response": resp}, root, cfg, st)
            note = (out.get("hookSpecificOutput") or {}).get("additionalContext")
            if note:
                st.setdefault("notes", []).append(note)


def agy_pre_invocation(p, root, cfg, st):
    agy_resolve(p, root, cfg, st)
    steps = transcript_steps(p.get("transcriptPath"))
    user = last_of(steps, "USER_INPUT")
    msgs = []
    if user and user.get("step_index") != st.get("user_step"):
        st["user_step"] = user.get("step_index")
        text = user.get("content") or ""
        m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", text, re.S)
        out = before_agent({"prompt": (m.group(1) if m else text).strip()}, root, cfg, st)
        ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext")
        if ctx:
            msgs.append(ctx)
    msgs.extend(st.pop("notes", []))
    if not msgs:
        return {}
    return {"injectSteps": [{"ephemeralMessage": "\n".join(msgs)}]}


def agy_stop(p, root, cfg, st):
    agy_resolve(p, root, cfg, st, force=True)
    if p.get("error") or p.get("terminationReason") in ("max_steps_exceeded", "error"):
        return {}
    steps = transcript_steps(p.get("transcriptPath"))
    reply = last_of(steps, "PLANNER_RESPONSE") or {}
    out = after_agent({"prompt_response": reply.get("content") or "", "stop_hook_active": False}, root, cfg, st)
    if out.get("decision") == "deny":
        return {"decision": "continue", "reason": out["reason"]}
    return {}


AGY_HANDLERS = {"PreToolUse": agy_pre_tool, "PostToolUse": agy_post_tool,
                "PreInvocation": agy_pre_invocation, "Stop": agy_stop}


def main(argv):
    args = argv[1:]
    agy = "--agy" in args
    args = [a for a in args if a != "--agy"]
    event = args[0] if args else ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        event = event or payload.get("hook_event_name", "")
        handler = (AGY_HANDLERS if agy else HANDLERS).get(event)
        if not handler:
            print("{}")
            return 0
        if agy:
            root = (payload.get("workspacePaths") or [None])[0] or os.path.dirname(os.getcwd())
            session = payload.get("conversationId", "default")
        else:
            root = project_dir(payload)
            session = payload.get("session_id", "default")
        if os.environ.get("GEMINI_GUARD_DEBUG") or os.path.exists(os.path.join(root, ".gemini", "guard-debug")):
            with open(os.path.join(guard_dir(root), "debug.jsonl"), "a") as fh:
                fh.write(json.dumps({"event": event, "agy": agy, "payload": payload}, default=str)[:20000] + "\n")
        cfg = load_config(root)
        st = load_state(root, session)
        out = handler(payload, root, cfg, st)
        save_state(root, session, st)
        print(json.dumps(out))
    except Exception as e:  # fail open: a broken guard must never wedge the session
        sys.stderr.write("gemini_guard: %s\n" % e)
        print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
