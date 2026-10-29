"""Two-round loop with fake agents, snapshots, the check runner, config and agent adapters."""

import os
import pathlib
import shutil
import tempfile
import time
import unittest
from unittest import mock

from cav import agents as agent_mod
from cav import config
from cav.gitops import Repo, git, git_ok
from cav.orchestrator import Orchestrator
from tests.test_v2 import ROUND_RE, StructuredFake, make_repo

BLOCKING = {"id": "F-01", "severity": "MUST_FIX", "kind": "behavioral", "path": "shared.txt", "line": 1,
            "summary": "the other candidate is wrong", "resolution": "fix it"}


class AgreesInRoundTwo(StructuredFake):
    """Round 1: blocks the other candidate with a behavioural MUST_FIX. Round 2: agrees."""

    def run(self, prompt, cwd):
        first = int(ROUND_RE.search(prompt).group(1)) == 1
        self.approve, self.findings = not first, [BLOCKING] if first else []
        return super().run(prompt, cwd)


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-test-"))
        self.repo, self.base = make_repo(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def case(self, rounds=2):
        return config.Case(
            name="flow", host_repo=self.repo, base=self.base,
            agents={"deepseek": config.AgentSpec("deepseek", "opencode", bootstrap_ref=self.base),
                    "kimi": config.AgentSpec("kimi", "kimi", bootstrap_ref=self.base)},
            max_rounds=rounds, round_seconds=30, stall_seconds=5, retries=0,
        )

    def test_two_rounds_converge(self):
        fakes = {name: AgreesInRoundTwo(name, lambda r, name=name: {
            "shared.txt": "agreed\n" if r >= 2 else f"{name}\n"}) for name in ("deepseek", "kimi")}

        with mock.patch("cav.agents.build", side_effect=lambda spec, log_path, title_prefix="": fakes[spec.name]):
            orchestrator = Orchestrator(self.case(), self.tmp / "run")
            state = orchestrator.run()

        self.assertEqual(state["phase"], "converged")
        self.assertEqual(len(state["history"]), 2)
        self.assertFalse(state["history"][0]["converged"], "a blocking finding stops round 1")
        self.assertFalse(state["history"][0]["blockers"]["trees_equal"])
        self.assertEqual(state["history"][0]["divergence"]["differing"], 1,
                         "round 2 starts from two proposals that differ in one path")
        self.assertTrue(state["history"][1]["converged"])
        final = state["final"]
        self.assertEqual(final["tree"], state["history"][1]["results"]["kimi"]["tree"])
        self.assertEqual(git("show", f"{final['commit']}:shared.txt", cwd=self.repo).strip(), "agreed")
        self.assertTrue((orchestrator.run_dir / "REPORT.md").exists())
        self.assertIn("finished", (orchestrator.run_dir / "run.log").read_text(encoding="utf-8"))

    def test_snapshot_works_without_commits(self):
        fresh = self.tmp / "no-commits"
        fresh.mkdir()
        git("init", "-q", cwd=fresh)
        git("config", "user.email", "t@example.com", cwd=fresh)
        git("config", "user.name", "t", cwd=fresh)
        (fresh / "a.txt").write_text("first\n", encoding="utf-8")

        orchestrator = Orchestrator(self.case(), self.tmp / "snap2")
        sha, problem = orchestrator.snapshot(Repo(fresh), config.AgentSpec("kimi", "kimi"))

        self.assertIsNone(problem)
        self.assertIn("a.txt", git("ls-tree", "-r", "--name-only", sha, cwd=fresh).splitlines())
        self.assertFalse(git_ok("rev-parse", "--verify", "--quiet", "HEAD", cwd=fresh),
                         "snapshot must not create a HEAD in the source repo")

    def test_snapshot_and_commit_work_without_git_identity(self):
        fresh = self.tmp / "no-identity"
        fresh.mkdir()
        git("init", "-q", cwd=fresh)
        (fresh / "a.txt").write_text("first\n", encoding="utf-8")

        blocked = {"GIT_CONFIG_GLOBAL": str(self.tmp / "no-global.cfg"),
                   "GIT_CONFIG_SYSTEM": str(self.tmp / "no-system.cfg")}
        with mock.patch.dict(os.environ, blocked):
            orchestrator = Orchestrator(self.case(), self.tmp / "snap3")
            sha, problem = orchestrator.snapshot(Repo(fresh), config.AgentSpec("kimi", "kimi"))
            self.assertIsNone(problem)
            self.assertTrue(sha)
            (fresh / "b.txt").write_text("second\n", encoding="utf-8")
            self.assertTrue(Repo(fresh).commit_all(fresh, "tool-made commit"))

    def test_snapshot_excludes_and_leaves_repo_alone(self):
        secondary = self.tmp / "secondary"
        secondary.mkdir()
        git("init", "-q", cwd=secondary)
        git("config", "user.email", "t@example.com", cwd=secondary)
        git("config", "user.name", "t", cwd=secondary)
        (secondary / "base.txt").write_text("base\n", encoding="utf-8")
        git("add", "-A", cwd=secondary)
        git("commit", "-q", "-m", "base", cwd=secondary)
        before = git("rev-parse", "HEAD", cwd=secondary)
        (secondary / "changed.txt").write_text("work\n", encoding="utf-8")
        (secondary / "reconciliation").mkdir()
        (secondary / "reconciliation" / "note.md").write_text("meta\n", encoding="utf-8")

        spec = config.AgentSpec("kimi", "kimi", bootstrap_exclude=["reconciliation"])
        orchestrator = Orchestrator(self.case(), self.tmp / "snap")
        sha, problem = orchestrator.snapshot(Repo(secondary), spec)

        self.assertIsNone(problem)
        files = git("ls-tree", "-r", "--name-only", sha, cwd=secondary).splitlines()
        self.assertIn("changed.txt", files)
        self.assertNotIn("reconciliation/note.md", files)
        self.assertEqual(git("rev-parse", "HEAD", cwd=secondary), before)
        status = git("status", "--porcelain", cwd=secondary)
        self.assertIn("changed.txt", status)
        self.assertIn("reconciliation/", status)

    def test_checks_run_python_placeholder(self):
        case = self.case()
        case.checks = ['{python} -c "print(\'ok\')"']
        orchestrator = Orchestrator(case, self.tmp / "checks")
        result = orchestrator.execute_checks(1, "deepseek", self.repo)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["commands"][0]["exit"], 0)

    def test_identical_trees_are_checked_once_and_keep_each_label(self):
        case = self.case()
        case.checks = ['{python} -c "print(1)"']
        orchestrator = Orchestrator(case, self.tmp / "checks-cache")
        results = {name: {"tree": "same"} for name in ("deepseek", "kimi")}
        with mock.patch.object(Orchestrator, "execute_checks", wraps=orchestrator.execute_checks) as run:
            report = orchestrator.run_checks(1, results, {name: self.repo for name in results})
        self.assertEqual(run.call_count, 1)
        self.assertEqual(report["kimi"]["agent"], "kimi")
        self.assertEqual(report["deepseek"]["agent"], "deepseek")
        self.assertIsNot(report["kimi"], report["deepseek"])

    def test_different_trees_are_checked_in_parallel(self):
        case = self.case()
        case.checks = ['{python} -c "import time; time.sleep(1)"']
        orchestrator = Orchestrator(case, self.tmp / "checks-parallel")
        results = {"deepseek": {"tree": "a"}, "kimi": {"tree": "b"}}
        started = time.time()
        report = orchestrator.run_checks(1, results, {name: self.repo for name in results})
        self.assertTrue(all(item["ok"] for item in report.values()))
        self.assertLess(time.time() - started, 1.9, "two 1s checks overlap")


class ConfigTest(unittest.TestCase):
    ROOT = pathlib.Path(__file__).resolve().parents[1] / "cases"

    def test_shipped_cases_load(self):
        case = config.load(self.ROOT / "msmon-five-phase.local.json")
        self.assertEqual(sorted(case.agents), ["deepseek", "kimi"])
        self.assertEqual(case.agents["kimi"].bootstrap_ref, "origin/candidate/kimi-phase-1-5")
        self.assertEqual(len(case.checks), 4)
        self.assertEqual(case.other("kimi"), "deepseek")
        self.assertEqual(case.agents["deepseek"].model, "deepseek/deepseek-flash#high")
        self.assertEqual(case.agents["kimi"].model, "kimi-code/k3.1-pro")
        self.assertEqual(case.agents["kimi"].thinking, "high")
        config.load(self.ROOT / "msmon-five-phase.json")

    def test_tool_case_has_no_shared_base(self):
        case = config.load(self.ROOT / "cross-agent-validation.json")
        self.assertIsNone(case.base)
        self.assertEqual(case.agents["deepseek"].bootstrap_ref, "HEAD")
        self.assertEqual(case.secondary_repo.name, "cross-agent-validation")


class AgentLifecycleTest(unittest.TestCase):
    def test_reset_clears_abort_and_failure_state(self):
        agent = agent_mod.Agent(config.AgentSpec("kimi", "kimi"), "log.txt")
        agent.state.error = "round timeout"
        agent.abort()
        agent.reset()
        self.assertFalse(agent._abort.is_set())
        self.assertIsNone(agent.state.error)

    def test_opencode_abort_kills_running_process(self):
        spec = config.AgentSpec("deepseek", "opencode")
        with mock.patch("cav.agents._opencode_cli", return_value="opencode-cli.exe"):
            agent = agent_mod.OpenCodeAgent(spec, "log.txt")
        agent._proc = mock.Mock()
        agent._proc.poll.return_value = None
        agent.abort()
        agent._proc.kill.assert_called_once_with()

    def test_aborted_opencode_keeps_the_monitor_reason(self):
        class Proc:
            returncode = -9

            def __init__(self):
                self.stdout = iter(())

            def wait(self):
                return -9

        spec = config.AgentSpec("deepseek", "opencode")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("cav.agents._opencode_cli", return_value="opencode-cli.exe"), \
                mock.patch("cav.agents.subprocess.Popen", return_value=Proc()):
            agent = agent_mod.OpenCodeAgent(spec, pathlib.Path(tmp) / "run.log")
            agent.state.error = "blocked: waiting:permission"
            agent.abort()
            result = agent.run("prompt", tmp)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "blocked: waiting:permission")


class ModelPinningTest(unittest.TestCase):
    def test_opencode_command_pins_model(self):
        spec = config.AgentSpec("deepseek", "opencode", model="deepseek/deepseek-flash#high")
        with mock.patch("cav.agents._opencode_cli", return_value="opencode-cli.exe"):
            agent = agent_mod.OpenCodeAgent(spec, "log.txt")
        command = agent._command("hello")
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "deepseek/deepseek-flash#high")
        self.assertEqual(command[-1], "hello")

    def test_kimi_profile_pins_model_and_thinking(self):
        spec = config.AgentSpec("kimi", "kimi", model="kimi-code/k3.1-pro", thinking="high")
        agent = agent_mod.KimiAgent(spec, "log.txt")
        self.assertEqual(agent._agent_config(),
                         {"model": "kimi-code/k3.1-pro", "permission_mode": "yolo", "thinking": "high"})


if __name__ == "__main__":
    unittest.main()
