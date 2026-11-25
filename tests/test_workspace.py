import pytest

from aidlc_g.examples import notifications as ex
from aidlc_g.orchestrator import SliceEnvironment
from aidlc_g.testkit import expect_error
from aidlc_g.workspace import normalize_path


@pytest.fixture
def env():
    return SliceEnvironment.create(ex.SEED)


@pytest.mark.parametrize("path", ["/etc/passwd", "src/../uat/x.py", "./src/a.py", "src//a.py", "src\\a.py", "", "src/a\x00.py"])
def test_path_normalization_rejects_escapes(path):
    with pytest.raises(Exception):
        normalize_path(path)


def test_traversal_cannot_reach_protected_tree(env):
    code = expect_error(lambda: env.workspace.commit(env.tokens["impl_agent"], {"src/../uat/test_x.py": b"x"}, "m"))
    assert code == "INVALID_PATH"


def test_commit_is_atomic(env):
    head = env.workspace.head
    code = expect_error(lambda: env.workspace.commit(
        env.tokens["impl_agent"], {"src/ok.py": b"ok", "uat/bad.py": b"bad"}, "mixed"))
    assert code == "PATH_NOT_PERMITTED" and env.workspace.head == head


def test_reads_are_cited_with_revision_and_digest(env):
    excerpt = env.workspace.read(env.tokens["requirements_agent"], "docs/notifications.md")
    assert excerpt.uri == "repo://docs/notifications.md" and excerpt.revision == env.workspace.head
    assert excerpt.digest.startswith("sha256:") and excerpt.trust == "untrusted"


def test_requirements_discovery_is_read_only(env):
    code = expect_error(lambda: env.workspace.commit(env.tokens["requirements_agent"], {"docs/x.md": b"x"}, "m"))
    assert code == "PATH_NOT_PERMITTED"


def test_only_runner_gets_full_checkout(env):
    assert expect_error(lambda: env.workspace.snapshot(env.tokens["impl_agent"], env.workspace.head)) == "ROLE_NOT_PERMITTED"
    assert "docs/CONTRIBUTING.md" in env.workspace.snapshot(env.tokens["ci-runner-1"], env.workspace.head)


def test_identity_comes_from_gate(env):
    assert expect_error(lambda: env.workspace.read("forged-token", "docs/notifications.md")) == "INVALID_TOKEN"


def test_build_digest_ignores_tests(env):
    before = env.workspace.build_digest()
    env.workspace.commit(env.tokens["uat_agent"], {"uat/test_new.py": b"def test(): pass\n"}, "tests", branch="work")
    assert env.workspace.build_digest(branch="work") == before
    env.workspace.commit(env.tokens["impl_agent"], {"src/new.py": b"x = 1\n"}, "code", branch="work")
    assert env.workspace.build_digest(branch="work") != before


def test_revisions_are_immutable_snapshots(env):
    first = env.workspace.head
    env.workspace.commit(env.tokens["impl_agent"], {"src/legacy_email_prefs.py": b"changed\n"}, "edit", branch="work")
    old = env.workspace.read(env.tokens["impl_agent"], "src/legacy_email_prefs.py", revision=first).content
    assert old.startswith("_opt_out")
