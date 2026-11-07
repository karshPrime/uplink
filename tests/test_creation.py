import json
from types import SimpleNamespace

import pytest

from tests.devenv import make_env, make_project
from uplink import creation as creation_module
from uplink.creation import Creation
from uplink.projects import slugify

TEMPLATE_SOUL = "# SOUL.md\n\nwritten by the template\n"
SOUL = "# SOUL.md — Bartmoss\n\nA netrunner.\n"


class Inline:
    def submit(self, fn, *args):
        fn(*args)


class Held:
    """Keeps submitted jobs waiting, as a busy worker would."""

    def __init__(self):
        self.waiting = []

    def submit(self, fn, *args):
        self.waiting.append((fn, args))


@pytest.fixture
def init_fails():
    return {"init": False}


@pytest.fixture
def runs(monkeypatch, config, init_fails):
    calls = []

    def run(command, cwd=None, env=None, **kwargs):
        calls.append({"command": command, "cwd": cwd, "env": env})
        if command[1] == "init":
            if init_fails["init"]:
                return SimpleNamespace(returncode=1, stdout="cloning failed\n")
            name = command[2]
            character = next(a for a in command if a.startswith("character="))
            project = config.projects_dir / name
            (project / ".shard").mkdir(parents=True)
            (project / ".shard" / "soul.md").write_text(TEMPLATE_SOUL)
            make_env(
                config,
                slugify(name),
                project_root=project,
                character=character.split("=", 1)[1],
            )
            return SimpleNamespace(returncode=0, stdout="rendered\n")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(creation_module.subprocess, "run", run)
    monkeypatch.setattr(
        creation_module.shutil, "which", lambda name: f"/usr/local/bin/{name}"
    )
    return calls


@pytest.fixture
def made(app, config, runs):
    app.state.creation = Creation(config, executor=Inline())
    config.soul_dir.mkdir()
    (config.soul_dir / "Bartmoss_soul.md").write_text(SOUL)
    return app.state.creation


def create(client, auth, **fields):
    body = {
        "name": "Bartmoss",
        "fork": "karshPrime/agent-bartmoss",
        "character": "Bartmoss",
    }
    body.update(fields)
    body = {k: v for k, v in body.items() if v is not None}
    return client.post("/projects", headers=auth, json=body)


def test_needs_token(client, made, runs):
    response = client.post("/projects", json={"name": "x", "character": "X"})
    assert response.status_code == 401
    assert runs == []


def test_creates_a_project(client, auth, made, runs, config, monkeypatch):
    monkeypatch.setenv("DEVENV_TEMPLATE", "gh:someone/evil")
    response = create(client, auth)
    assert response.status_code == 202
    job = response.json()
    assert response.headers["location"] == f"/jobs/{job['id']}"
    assert (job["project"], job["state"], job["step"]) == ("Bartmoss", "done", None)
    init, commit = runs
    assert init["command"] == [
        "/usr/local/bin/d",
        "init",
        "Bartmoss",
        "--defaults",
        "--data",
        "fork_url=karshPrime/agent-bartmoss",
        "--data",
        "character=Bartmoss",
    ]
    assert init["cwd"] == config.projects_dir
    assert "DEVENV_TEMPLATE" not in init["env"]
    assert init["env"]["DEVENV_PROJECTS"] == str(config.projects_dir)
    assert init["env"]["DEVENV_HOME"] == str(config.envs_dir)
    assert (config.projects_dir / "Bartmoss" / ".shard" / "soul.md").read_text() == SOUL
    assert commit["cwd"] == config.projects_dir / "Bartmoss"
    assert commit["command"][-3:] == ["Soul for Bartmoss", "--", ".shard/soul.md"]
    assert "core.hooksPath=/dev/null" in commit["command"]


def test_creates_from_a_repo_that_is_not_a_fork(client, auth, made, runs):
    response = create(client, auth, fork=None, repo="karshPrime/Bartmoss")
    assert response.status_code == 202
    assert "fork_url=karshPrime/Bartmoss" in runs[0]["command"]


@pytest.mark.parametrize(
    "fields",
    [
        {"fork": None},
        {"repo": "karshPrime/other"},
    ],
)
def test_needs_exactly_one_repository(client, auth, made, runs, fields):
    response = create(client, auth, **fields)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_repo"
    assert runs == []


@pytest.mark.parametrize(
    "fork",
    [
        "karshPrime",
        "karshPrime/..",
        "karshPrime/agent/x",
        "https://github.com/karshPrime/agent-x",
        "-x/agent",
        "karshPrime/agent x",
        "karshPrime/agent;id",
    ],
)
def test_refuses_bad_repository_names(client, auth, made, runs, fork):
    response = create(client, auth, fork=fork)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_repo"
    assert runs == []


def test_refuses_owners_that_are_not_allowed(client, auth, made, runs):
    response = create(client, auth, fork="someone/agent-bartmoss")
    assert response.status_code == 403
    assert response.json()["error"] == "owner_not_allowed"
    assert runs == []


def test_owner_matches_in_any_case(client, auth, made, runs):
    assert create(client, auth, fork="karshprime/agent-bartmoss").status_code == 202


@pytest.mark.parametrize("name", ["..", ".hidden", "-rf", "a/b", "a b", "$(id)", ""])
def test_refuses_bad_names(client, auth, made, runs, name):
    response = create(client, auth, name=name)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_name"


@pytest.mark.parametrize(
    "character", ["", "1st", "a" * 41, "Bart\n", "--data", "Bart;id"]
)
def test_refuses_bad_characters(client, auth, made, runs, character):
    response = create(client, auth, character=character)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_character"
    assert runs == []


def test_refuses_an_existing_project(client, auth, made, runs, config):
    make_project(config, "Bartmoss")
    response = create(client, auth)
    assert response.status_code == 409
    assert response.json()["error"] == "already_exists"
    assert runs == []


def test_refuses_a_leftover_devenv_folder(client, auth, made, runs, config):
    (config.envs_dir / "bartmoss").mkdir(parents=True)
    assert create(client, auth).json()["error"] == "already_exists"


def test_refuses_a_character_already_played(client, auth, made, runs, config):
    project = config.projects_dir / "Judy"
    (project / "main").mkdir(parents=True)
    make_env(config, "judy", project_root=project, character="Judy")
    (config.soul_dir / "Jude_soul.md").write_text(SOUL)
    response = create(client, auth, name="Jude", character="judy")
    assert response.status_code == 409
    assert response.json()["error"] == "character_taken"
    assert runs == []


def test_refuses_a_name_or_character_already_being_created(
    client, auth, app, config, runs
):
    app.state.creation = Creation(config, executor=Held())
    config.soul_dir.mkdir()
    for name in ["Bartmoss", "Other"]:
        (config.soul_dir / f"{name}_soul.md").write_text(SOUL)
    assert create(client, auth).status_code == 202
    assert create(client, auth).json()["error"] == "already_exists"
    response = create(client, auth, name="Other", fork="karshPrime/agent-other")
    assert response.json()["error"] == "character_taken"


def test_needs_a_soul(client, auth, made, runs, config):
    (config.soul_dir / "Bartmoss_soul.md").unlink()
    response = create(client, auth)
    assert response.status_code == 404
    assert response.json()["error"] == "soul_not_found"


def test_never_follows_a_symlinked_soul(client, auth, made, runs, config, tmp_path):
    secret = tmp_path / "token"
    secret.write_text("the uplink token")
    soul = config.soul_dir / "Bartmoss_soul.md"
    soul.unlink()
    soul.symlink_to(secret)
    response = create(client, auth)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_soul"
    assert runs == []


def test_never_reads_from_a_symlinked_soul_folder(
    client, auth, made, runs, config, tmp_path
):
    real = tmp_path / "elsewhere"
    config.soul_dir.rename(real)
    config.soul_dir.symlink_to(real)
    assert create(client, auth).json()["error"] == "invalid_soul"


@pytest.mark.parametrize("content", [b"x" * (64 * 1024 + 1), b"\xff\xfe not text"])
def test_refuses_souls_that_are_too_big_or_not_text(
    client, auth, made, runs, config, content
):
    (config.soul_dir / "Bartmoss_soul.md").write_bytes(content)
    assert create(client, auth).json()["error"] == "invalid_soul"


def test_refuses_a_soul_that_is_not_a_file(client, auth, made, runs, config):
    soul = config.soul_dir / "Bartmoss_soul.md"
    soul.unlink()
    soul.mkdir()
    assert create(client, auth).json()["error"] == "invalid_soul"


def test_a_failed_init_leaves_the_job_failed(
    client, auth, made, runs, config, init_fails
):
    init_fails["init"] = True
    job = create(client, auth).json()
    assert (job["state"], job["step"]) == ("failed", "init")
    assert "cloning failed" in job["output"]
    assert len(runs) == 1


def test_an_unchanged_soul_is_not_committed(client, auth, made, runs, config):
    (config.soul_dir / "Bartmoss_soul.md").write_text(TEMPLATE_SOUL)
    job = create(client, auth).json()
    assert job["state"] == "done"
    assert [c["command"][1] for c in runs] == ["init"]


def test_reports_a_job(client, auth, made, runs):
    job = create(client, auth).json()
    response = client.get(f"/jobs/{job['id']}", headers=auth)
    assert response.status_code == 200
    assert response.json()["state"] == "done"


def test_unknown_job(client, auth, made):
    response = client.get("/jobs/nope", headers=auth)
    assert response.status_code == 404
    assert response.json()["error"] == "job_not_found"


def test_needs_d(client, auth, made, runs, monkeypatch):
    monkeypatch.setattr(creation_module.shutil, "which", lambda name: None)
    response = create(client, auth)
    assert response.status_code == 503
    assert response.json()["error"] == "d_unavailable"


def test_creation_is_audited(client, auth, made, runs, config):
    job = create(client, auth).json()
    record = json.loads(config.audit_log.read_text().splitlines()[-1])
    assert (record["action"], record["project"], record["outcome"]) == (
        "create",
        "Bartmoss",
        202,
    )
    assert (record["repo"], record["character"], record["job"]) == (
        "karshPrime/agent-bartmoss",
        "Bartmoss",
        job["id"],
    )
