"""AAW AUTONOMOUS ITERATIONS V0.2 — adversarial Git containment.

Verifies, rather than assumes, what `GitWorkspaceEnvironment.assert_safe`
detects when ANOTHER process mutates Git between two phase boundaries.

Fixture: canonical repo (main checked out) + isolated registered worktree on
`aaw/it` + local bare remote + an independent second clone ("another machine").
Every mutation is run by a separate Python process that shells out to git, so
nothing shares state with the controller's process.

The resulting contract is deliberately narrower than "detects any push":

  BLOCKED   AAW's own GuardedGit refuses merge/push/rebase/... without a
            one-shot human-approved token (test_autonomy.py).
  DETECTED  at the next phase boundary: local protected refs, canonical HEAD /
            branch / status, remote-tracking protected refs, worktree on a
            protected branch, rewrite of history observed at a boundary.
  REMOTE    a push or force-push to the remote made from a *different clone*
            is invisible locally; it is detected only with `remote_check=True`
            (an explicit `git ls-remote` at every boundary).
  NOT DETECTED  rewriting commits that were created *and* rewritten between
            two boundaries (never observed); pushes of non-protected branches.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import autonomy_contract as ac
import autonomy_controller as ctl


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def external(*commands):
    """Run git commands from a separate process (the 'other agent')."""
    code = textwrap.dedent(f"""
        import subprocess, sys
        for argv in {json.dumps([list(c) for c in commands])}:
            r = subprocess.run(argv, capture_output=True, text=True)
            if r.returncode != 0:
                sys.stderr.write(" ".join(argv) + "\\n" + r.stderr)
                sys.exit(r.returncode)
    """)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def build_fixture(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    repo, wt, remote, other = root / "repo", root / "wt", root / "remote.git", root / "other"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for cwd in (repo,):
        git(cwd, "config", "user.email", "t@example.invalid")
        git(cwd, "config", "user.name", "T")
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    git(repo, "worktree", "add", "-q", "-b", "aaw/it", str(wt))
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    git(other, "config", "user.email", "o@example.invalid")
    git(other, "config", "user.name", "O")
    return {"repo": repo, "wt": wt, "remote": remote, "other": other}


def commit(cwd, message):
    return [["git", "-C", str(cwd), "add", "."], ["git", "-C", str(cwd), "commit", "-q", "-m", message]]


def write(path: Path, text="y\n"):
    path.write_text(text, encoding="utf-8")


def detected(env) -> str | None:
    try:
        env.assert_safe()
        return None
    except ac.GitPolicyViolation as exc:
        return str(exc)


# Each scenario mutates from another process and returns nothing; the
# expectation is (without remote_check, with remote_check).
def s_commit_in_worktree(f):
    write(f["wt"] / "b.txt")
    external(*commit(f["wt"], "checkpoint"))


def s_merge_into_main_in_canonical(f):
    s_commit_in_worktree(f)
    external(["git", "-C", str(f["repo"]), "merge", "-q", "--ff-only", "aaw/it"])


def s_update_ref_main_from_worktree(f):
    s_commit_in_worktree(f)
    external(["git", "-C", str(f["wt"]), "update-ref", "refs/heads/main", "HEAD"])


def s_push_to_remote_main_from_this_repo(f):
    s_commit_in_worktree(f)
    external(["git", "-C", str(f["wt"]), "push", "-q", "origin", "HEAD:main"])


def s_push_to_remote_main_from_other_clone(f):
    write(f["other"] / "c.txt")
    external(*commit(f["other"], "elsewhere"), ["git", "-C", str(f["other"]), "push", "-q", "origin", "main"])


def s_fetch_moves_origin_main(f):
    s_push_to_remote_main_from_other_clone(f)
    external(["git", "-C", str(f["repo"]), "fetch", "-q", "origin"])


def s_force_push_remote_main_from_other_clone(f):
    write(f["other"] / "c.txt")
    external(*commit(f["other"], "rewrite"), ["git", "-C", str(f["other"]), "commit", "-q", "--amend", "-m", "rewritten"],
             ["git", "-C", str(f["other"]), "reset", "-q", "--hard", "HEAD~1"],
             ["git", "-C", str(f["other"]), "commit", "-q", "--allow-empty", "-m", "divergent"],
             ["git", "-C", str(f["other"]), "push", "-q", "-f", "origin", "main"])


def s_force_push_remote_main_from_this_repo(f):
    write(f["wt"] / "b.txt")
    external(*commit(f["wt"], "x"), ["git", "-C", str(f["wt"]), "push", "-q", "-f", "origin", "HEAD:main"])


def s_checkout_main_in_worktree(f):
    external(["git", "-C", str(f["wt"]), "checkout", "-q", "--ignore-other-worktrees", "main"])


def s_canonical_switches_branch(f):
    external(["git", "-C", str(f["repo"]), "switch", "-q", "-c", "side"])


def s_rebase_drops_an_observed_commit(f):
    # caller observes HEAD after the first commit (see test below)
    base = git(f["wt"], "rev-parse", "HEAD~1")
    write(f["wt"] / "d.txt")
    external(*commit(f["wt"], "second"), ["git", "-C", str(f["wt"]), "rebase", "-q", "--onto", base, "HEAD~1"])


def s_amend_observed_commit(f):
    external(["git", "-C", str(f["wt"]), "commit", "-q", "--amend", "-m", "amended"])


def s_rewrite_unobserved_commits_only(f):
    write(f["wt"] / "e.txt")
    external(*commit(f["wt"], "fresh"), ["git", "-C", str(f["wt"]), "commit", "-q", "--amend", "-m", "fresh-amended"])


def s_push_non_protected_branch(f):
    s_commit_in_worktree(f)
    external(["git", "-C", str(f["wt"]), "push", "-q", "origin", "aaw/it"])


# name, scenario, needs an observed commit first, detected (local), detected (remote_check)
SCENARIOS = [
    ("A_commit_in_worktree", s_commit_in_worktree, False, False, False),
    ("B_merge_into_main_in_canonical", s_merge_into_main_in_canonical, False, True, True),
    ("B_update_ref_main_from_worktree", s_update_ref_main_from_worktree, False, True, True),
    ("C_push_remote_main_from_this_repo", s_push_to_remote_main_from_this_repo, False, True, True),
    ("C_push_remote_main_from_other_clone", s_push_to_remote_main_from_other_clone, False, False, True),
    ("D_fetch_moves_origin_main", s_fetch_moves_origin_main, False, True, True),
    ("E_rebase_drops_observed_commit", s_rebase_drops_an_observed_commit, True, True, True),
    ("E_amend_observed_commit", s_amend_observed_commit, True, True, True),
    ("E_rewrite_unobserved_commits_only", s_rewrite_unobserved_commits_only, False, False, False),
    ("F_checkout_main_in_worktree", s_checkout_main_in_worktree, False, True, True),
    ("F_canonical_checkout_switches_branch", s_canonical_switches_branch, False, True, True),
    ("G_force_push_remote_main_from_other_clone", s_force_push_remote_main_from_other_clone, False, False, True),
    ("G_force_push_remote_main_from_this_repo", s_force_push_remote_main_from_this_repo, False, True, True),
    ("X_push_non_protected_branch", s_push_non_protected_branch, False, False, False),
]


def run_scenario(root: Path, scenario, observed_first: bool, remote_check: bool) -> str | None:
    f = build_fixture(root)
    env = ctl.GitWorkspaceEnvironment(f["repo"], f["wt"], remote_check=remote_check)
    if observed_first:
        write(f["wt"] / "b.txt")
        external(*commit(f["wt"], "observed"))
    assert detected(env) is None  # boundary: the observed state is clean
    scenario(f)
    return detected(env)


def containment_matrix(root: Path) -> list[dict]:
    """Used by the V0.2 evidence script to record the measured matrix."""
    rows = []
    for name, scenario, observed, *_ in SCENARIOS:
        local = run_scenario(root / f"{name}_local", scenario, observed, False)
        remote = run_scenario(root / f"{name}_remote", scenario, observed, True)
        rows.append({"scenario": name, "detected_local_refs": bool(local), "detected_with_remote_check": bool(remote),
                     "local_reason": local, "remote_check_reason": remote})
    return rows


@pytest.mark.parametrize("name, scenario, observed, expect_local, expect_remote", SCENARIOS,
                         ids=[s[0] for s in SCENARIOS])
def test_containment_matrix(tmp_path, name, scenario, observed, expect_local, expect_remote):
    local = run_scenario(tmp_path / "local", scenario, observed, False)
    assert bool(local) is expect_local, local
    remote = run_scenario(tmp_path / "remote", scenario, observed, True)
    assert bool(remote) is expect_remote, remote


def test_branch_force_on_checked_out_main_is_refused_by_git_itself(tmp_path):
    # `update-ref` (scenario above) is the bypass; plain `branch -f` is refused by git.
    f = build_fixture(tmp_path)
    write(f["wt"] / "b.txt")
    external(*commit(f["wt"], "x"))
    with pytest.raises(AssertionError, match="cannot force update the branch|used by worktree|checked out"):
        external(["git", "-C", str(f["wt"]), "branch", "-f", "main", "HEAD"])


def test_unreadable_remote_fails_closed_when_remote_check_is_on(tmp_path):
    f = build_fixture(tmp_path)
    env = ctl.GitWorkspaceEnvironment(f["repo"], f["wt"], remote_check=True)
    git(f["repo"], "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert "unreadable" in (detected(env) or "")


def test_a_detected_violation_stops_the_controller_at_the_next_boundary(tmp_path):
    from test_autonomy import Harness, mandate_fixture, plan
    f = build_fixture(tmp_path / "ws")
    env = ctl.GitWorkspaceEnvironment(f["repo"], f["wt"])
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "only"}]
    h = Harness(tmp_path, mandate=m, env=env).defaults().script("plan", plan(["A"]))

    def merging_implementer(ctx):
        write(f["wt"] / "b.txt")
        external(*commit(f["wt"], "work"), ["git", "-C", str(f["repo"]), "merge", "-q", "--ff-only", "aaw/it"])
        return {"summary": "implemented (and merged behind the controller's back)", "checks": []}
    h.scripts["execute"] = [merging_implementer]
    state = h.controller().run()
    assert state["escalation"]["code"] == ac.E_GIT and h.calls == ["plan", "execute"]
    assert not state["hold"]["promotable"]


def test_changed_files_lists_the_first_modified_path_intact(tmp_path):
    # Regression: porcelain output through the stripping `git()` helper turned
    # " M a.txt" into "txt"-style truncations; name-only listings do not.
    f = build_fixture(tmp_path)
    env = ctl.GitWorkspaceEnvironment(f["repo"], f["wt"])
    write(f["wt"] / "a.txt", "changed\n")
    write(f["wt"] / "new.txt", "n\n")
    assert env.changed_files() == ["a.txt", "new.txt"]


def _real_run(tmp_path, f, executes):
    from test_autonomy import Harness, mandate_fixture, plan
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "only"}]
    env = ctl.GitWorkspaceEnvironment(f["repo"], f["wt"])
    h = Harness(tmp_path, mandate=m, env=env).defaults().script("plan", plan(["A"]))
    h.scripts["execute"] = executes
    return h


class Crash(BaseException):
    pass


def test_resume_on_a_real_dirty_worktree_keeps_the_original_baseline(tmp_path):
    f = build_fixture(tmp_path / "ws")
    base = git(f["wt"], "rev-parse", "HEAD")

    def implement(ctx):
        write(f["wt"] / "b.txt")
        external(*commit(f["wt"], "checkpoint commit"))
        write(f["wt"] / "c.txt")  # plus uncommitted work
        return {"summary": "implemented", "checks": []}
    h = _real_run(tmp_path, f, [implement])
    crash = {"armed": True}

    def crashing_review(ctx):
        if crash.pop("armed", False):
            raise Crash()
        return {"verdict": "PASS"}
    h.scripts["review"] = [crashing_review]
    with pytest.raises(Crash):
        h.controller().run()
    with pytest.raises(ac.GitPolicyViolation, match="dirty"):
        ctl.GitWorkspaceEnvironment(f["repo"], f["wt"])  # a fresh start would refuse this worktree
    h.env = ctl.GitWorkspaceEnvironment(f["repo"], f["wt"], resuming=True)
    state = h.controller(resume=True).run()
    assert state["status"] == ac.AWAITING_HUMAN and state["hold"]["promotable"], state["escalation"]
    assert h.env.describe()["base_head"] == base  # not the post-crash HEAD
    reviewed = h.ctxs["review"][-1]["raw"]
    assert "+++ b/b.txt" in reviewed["diff"] and "+++ b/c.txt" in reviewed["diff"]  # committed + uncommitted
    assert [c.split(" ", 1)[1] for c in reviewed["commits"]] == ["checkpoint commit"]


def test_resume_detects_a_merge_made_while_the_controller_was_down(tmp_path):
    f = build_fixture(tmp_path / "ws")

    def implement(ctx):
        write(f["wt"] / "b.txt")
        external(*commit(f["wt"], "work"))
        return {"summary": "implemented", "checks": []}
    h = _real_run(tmp_path, f, [implement])
    h.scripts["review"] = [lambda ctx: (_ for _ in ()).throw(Crash())]
    with pytest.raises(Crash):
        h.controller().run()
    external(["git", "-C", str(f["repo"]), "merge", "-q", "--ff-only", "aaw/it"])  # during the outage
    h.env = ctl.GitWorkspaceEnvironment(f["repo"], f["wt"], resuming=True)
    state = h.controller(resume=True).run()
    assert state["escalation"]["code"] == ac.E_GIT and "while the controller was down" in state["escalation"]["detail"]
