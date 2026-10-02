"""Tests for gemini_guard.py. Run: python3 -m unittest discover -s guard"""
import json
import os
import subprocess
import sys

import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "gemini_guard.py")


def make_proj():
    tmp_path = Path(tempfile.mkdtemp())
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "guard.json").write_text(json.dumps({"protected_paths": ["notes/archive"]}))
    (tmp_path / "a.py").write_text("# one\n# two\n# three\n# four\n# five\nx = 1\ny = 2\n")
    return tmp_path


def run(proj, event, **payload):
    payload.setdefault("session_id", "s1")
    payload.setdefault("cwd", str(proj))
    env = dict(os.environ, GEMINI_PROJECT_DIR=str(proj))
    r = subprocess.run([sys.executable, SCRIPT, event], input=json.dumps(payload),
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def tool(proj, event, name, args, resp=None):
    p = {"tool_name": name, "tool_input": args}
    if resp is not None:
        p["tool_response"] = resp
    return run(proj, event, **p)


def denied(out):
    return out.get("decision") == "deny"



class GuardTest(unittest.TestCase):
    def setUp(self):
        self.proj = make_proj()

    def test_before_agent_injects_contract(self):
        proj = self.proj
        out = run(proj, "BeforeAgent", prompt="fix the bug")
        assert "STATUS:" in out["hookSpecificOutput"]["additionalContext"]

    def test_read_only_lock_and_unlock(self):
        proj = self.proj
        run(proj, "BeforeAgent", prompt="plan only, don't change anything")
        assert denied(tool(proj, "BeforeTool", "replace", {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}))
        assert denied(tool(proj, "BeforeTool", "run_shell_command", {"command": "sed -i '' s/x/y/ a.py"}))
        assert not denied(tool(proj, "BeforeTool", "run_shell_command", {"command": "git status"}))
        run(proj, "BeforeAgent", prompt="looks good, go ahead")
        assert not denied(tool(proj, "BeforeTool", "replace", {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}))

    def test_scoped_negative_does_not_lock(self):
        proj = self.proj
        run(proj, "BeforeAgent", prompt="fix the parser but don't commit")
        assert not denied(tool(proj, "BeforeTool", "replace", {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}))

    def test_replace_missing_old_string_shows_nearest(self):
        proj = self.proj
        out = tool(proj, "BeforeTool", "replace", {"file_path": "a.py", "old_string": "x = 11", "new_string": "x = 2"})
        assert denied(out) and "line 6" in out["reason"]

    def test_replace_whitespace_drift_allowed(self):
        proj = self.proj
        out = tool(proj, "BeforeTool", "replace", {"file_path": "a.py", "old_string": "x  =  1", "new_string": "x = 2"})
        assert not denied(out)

    def test_replace_ambiguous(self):
        proj = self.proj
        (proj / "b.py").write_text("z = 0\nz = 0\n")
        assert denied(tool(proj, "BeforeTool", "replace", {"file_path": "b.py", "old_string": "z = 0", "new_string": "z = 1"}))
        assert not denied(tool(proj, "BeforeTool", "replace", {"file_path": "b.py", "old_string": "z = 0", "new_string": "z = 1", "allow_multiple": True}))

    def test_repeated_failure_blocked(self):
        proj = self.proj
        args = {"command": "python3 nope.py"}
        for _ in range(2):
            assert not denied(tool(proj, "BeforeTool", "run_shell_command", args))
            out = tool(proj, "AfterTool", "run_shell_command", args,
                       {"llmContent": "Error: can't open file 'nope.py': No such file or directory\nExit Code: 2"})
            assert "failure class: path" in out["hookSpecificOutput"]["additionalContext"]
        out = tool(proj, "BeforeTool", "run_shell_command", args)
        assert denied(out) and "do not retry" in out["reason"]
        assert not denied(tool(proj, "BeforeTool", "run_shell_command", {"command": "ls"}))

    def test_destructive_shell_blocked(self):
        proj = self.proj
        for c in ["rm -rf build", "git reset --hard HEAD", "git push --force origin main"]:
            assert denied(tool(proj, "BeforeTool", "run_shell_command", {"command": c})), c

    def test_protected_paths(self):
        proj = self.proj
        d = proj / "notes" / "archive"
        d.mkdir(parents=True)
        (d / "note.md").write_text("hand written\n")
        assert denied(tool(proj, "BeforeTool", "write_file", {"file_path": "notes/archive/note.md", "content": "x"}))
        assert denied(tool(proj, "BeforeTool", "run_shell_command", {"command": "mv notes/archive/note.md /tmp/"}))
        assert not denied(tool(proj, "BeforeTool", "write_file", {"file_path": "notes/archive/new.md", "content": "x"}))
        assert not denied(tool(proj, "BeforeTool", "replace", {"file_path": "notes/archive/note.md", "old_string": "hand written", "new_string": "hand written\nmore"}))

    def test_stale_file(self):
        proj = self.proj
        tool(proj, "AfterTool", "read_file", {"file_path": "a.py"}, {"llmContent": "..."})
        (proj / "a.py").write_text("x = 1\nchanged by user\n")
        out = tool(proj, "BeforeTool", "replace", {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 3"})
        assert denied(out) and "re-read" in out["reason"]

    def test_comment_loss_warns_once(self):
        proj = self.proj
        args = {"file_path": "a.py", "content": "x = 1\ny = 2\n"}
        assert denied(tool(proj, "BeforeTool", "write_file", args))
        assert not denied(tool(proj, "BeforeTool", "write_file", args))

    def test_big_output_spilled(self):
        proj = self.proj
        out = tool(proj, "AfterTool", "run_shell_command", {"command": "cat big"}, {"llmContent": "z" * 50000 + "\nExit Code: 0"})
        assert denied(out) and ".gemini/tmp/guard/out-" in out["reason"]
        spilled = [f for f in os.listdir(proj / ".gemini" / "tmp" / "guard") if f.startswith("out-")]
        assert spilled

    def test_verify_gate(self):
        proj = self.proj
        run(proj, "BeforeAgent", prompt="fix it")
        tool(proj, "AfterTool", "replace", {"file_path": "a.py", "old_string": "x", "new_string": "y"}, {"llmContent": "Successfully modified file"})
        out = run(proj, "AfterAgent", prompt="fix it", prompt_response="done!", stop_hook_active=False)
        assert denied(out) and "git diff" in out["reason"]
        # second time in the same turn: never nag twice
        assert not denied(run(proj, "AfterAgent", prompt="fix it", prompt_response="done!", stop_hook_active=False))

    def test_verify_gate_passes_after_tests(self):
        proj = self.proj
        run(proj, "BeforeAgent", prompt="fix it")
        tool(proj, "AfterTool", "replace", {"file_path": "a.py", "old_string": "x", "new_string": "y"}, {"llmContent": "ok"})
        tool(proj, "AfterTool", "run_shell_command", {"command": "python3 -m pytest -q"}, {"llmContent": "3 passed\nExit Code: 0"})
        out = run(proj, "AfterAgent", prompt="fix it", prompt_response="fixed.\nSTATUS: SUCCESS")
        assert not denied(out)

    def test_budget(self):
        proj = self.proj
        (proj / ".gemini" / "guard.json").write_text(json.dumps({"max_tool_calls_per_turn": 2}))
        run(proj, "BeforeAgent", prompt="go")
        tool(proj, "BeforeTool", "glob", {"pattern": "*"})
        tool(proj, "BeforeTool", "glob", {"pattern": "*.py"})
        assert denied(tool(proj, "BeforeTool", "glob", {"pattern": "*.md"}))

    def test_fails_open_on_garbage(self):
        proj = self.proj
        r = subprocess.run([sys.executable, SCRIPT, "BeforeTool"], input="not json", capture_output=True, text=True,
                           env=dict(os.environ, GEMINI_PROJECT_DIR=str(proj)))
        assert r.returncode == 0 and r.stdout.strip() == "{}"

class AntigravityTest(unittest.TestCase):
    def setUp(self):
        self.proj = make_proj()
        self.tr = self.proj / "transcript.jsonl"

    def agy(self, event, **payload):
        payload.setdefault("conversationId", "c1")
        payload.setdefault("workspacePaths", [str(self.proj)])
        payload.setdefault("transcriptPath", str(self.tr))
        r = subprocess.run([sys.executable, SCRIPT, "--agy", event], input=json.dumps(payload),
                           capture_output=True, text=True, cwd=str(self.proj))
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    def say(self, idx, text, reply=None):
        with open(self.tr, "a") as f:
            f.write(json.dumps({"step_index": idx, "type": "USER_INPUT",
                                "content": "<USER_REQUEST>\n%s\n</USER_REQUEST>" % text}) + "\n")
            if reply:
                f.write(json.dumps({"step_index": idx + 1, "type": "PLANNER_RESPONSE", "content": reply}) + "\n")

    def test_pre_invocation_injects_once_per_user_turn(self):
        self.say(0, "fix the bug")
        out = self.agy("PreInvocation", invocationNum=1)
        assert "STATUS:" in out["injectSteps"][0]["ephemeralMessage"]
        assert self.agy("PreInvocation", invocationNum=2) == {}

    def test_read_only_blocks_antigravity_edit(self):
        self.say(0, "plan only, don't change anything")
        self.agy("PreInvocation")
        a = str(self.proj / "a.py")
        out = self.agy("PreToolUse", stepIdx=3, toolCall={"name": "replace_file_content", "args": {
            "TargetFile": a, "TargetContent": "x = 1", "ReplacementContent": "x = 2", "AllowMultiple": False}})
        assert out.get("decision") == "deny"
        assert self.agy("PreToolUse", stepIdx=4, toolCall={"name": "view_file", "args": {"AbsolutePath": a}}) == {"decision": "ask"}

    def test_missing_target_content(self):
        a = str(self.proj / "a.py")
        out = self.agy("PreToolUse", stepIdx=1, toolCall={"name": "multi_replace_file_content", "args": {
            "TargetFile": a, "ReplacementChunks": [{"TargetContent": "x = 1", "ReplacementContent": "x"},
                                                   {"TargetContent": "y = 22", "ReplacementContent": "y"}]}})
        assert out.get("decision") == "deny" and "line 7" in out["reason"]

    def test_retry_block_and_note(self):
        call = {"name": "run_command", "args": {"CommandLine": "python3 nope.py", "Cwd": str(self.proj)}}
        for i in (1, 2):
            assert self.agy("PreToolUse", stepIdx=i, toolCall=call) == {"decision": "ask"}
            assert self.agy("PostToolUse", stepIdx=i, error="exit status 2: No such file or directory") == {}
        out = self.agy("PreToolUse", stepIdx=3, toolCall=call)
        assert out.get("decision") == "deny"
        self.say(0, "go")
        msg = self.agy("PreInvocation")["injectSteps"][0]["ephemeralMessage"]
        assert "failure class: path" in msg

    def test_nonzero_exit_from_transcript(self):
        call = {"name": "run_command", "args": {"CommandLine": "python3 gone.py"}}
        for i in (5, 7):
            with open(self.tr, "a") as f:
                f.write(json.dumps({"step_index": i, "type": "GENERIC",
                                    "content": "The command exited with code 2.\nOutput:\nNo such file or directory"}) + "\n")
            self.agy("PreToolUse", stepIdx=i, toolCall=call)
            self.agy("PostToolUse", stepIdx=i, error="")
        assert self.agy("PreToolUse", stepIdx=9, toolCall=call).get("decision") == "deny"

    def test_result_written_after_post_hook(self):
        call = {"name": "run_command", "args": {"CommandLine": "python3 late.py"}}
        for i in (5, 7):
            self.agy("PreToolUse", stepIdx=i, toolCall=call)
            self.agy("PostToolUse", stepIdx=i, error="")  # transcript not written yet
            with open(self.tr, "a") as f:
                f.write(json.dumps({"step_index": i, "type": "GENERIC",
                                    "content": "The command exited with code 1.\nOutput:\nboom"}) + "\n")
            self.agy("PreInvocation")
        assert self.agy("PreToolUse", stepIdx=9, toolCall=call).get("decision") == "deny"

    def test_stop_gate_after_edit(self):
        self.say(0, "fix it", reply="done")
        self.agy("PreInvocation")
        a = str(self.proj / "a.py")
        self.agy("PreToolUse", stepIdx=2, toolCall={"name": "write_to_file", "args": {"TargetFile": str(self.proj / "n.py"), "CodeContent": "z = 1"}})
        self.agy("PostToolUse", stepIdx=2)
        out = self.agy("Stop", terminationReason="model_stop")
        assert out.get("decision") == "continue" and "git diff" in out["reason"]
        assert self.agy("Stop", terminationReason="model_stop") == {}


if __name__ == "__main__":
    unittest.main()
