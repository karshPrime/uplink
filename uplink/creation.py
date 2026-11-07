from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from uplink.config import Config
from uplink.errors import UplinkError
from uplink.projects import env_value, project_for_env, slugify, valid_project_name

REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+")
# the same rule agent-base's copier.yml applies to the character answer
CHARACTER = re.compile(r"[A-Za-z][A-Za-z .'-]{0,39}")
SOUL_LIMIT = 64 * 1024
OUTPUT_LIMIT = 64 * 1024
JOBS_KEPT = 100


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def character_slug(character: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", character.lower()).strip("-")


@dataclass
class Job:
    id: str
    project: str
    state: str = "queued"
    step: str | None = None
    output: str = ""
    created_at: str = field(default_factory=_now)
    finished_at: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Request:
    name: str
    repo: str
    character: str
    soul: str


class Creation:
    """Creates projects with devenv init, one at a time, as background jobs."""

    def __init__(self, config: Config, executor: Executor | None = None):
        self.config = config
        # one worker, so two creations never race for the same name or character
        self._executor = executor or ThreadPoolExecutor(max_workers=1)
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._pending: dict[str, Request] = {}
        self._lock = threading.Lock()

    def characters(self) -> list[dict]:
        found = []
        envs = self.config.envs_dir
        if not envs.is_dir():
            return found
        for env_dir in sorted(envs.iterdir()):
            if env_dir.is_symlink():
                continue
            project = project_for_env(env_dir, self.config)
            if project is not None:
                found.append(
                    {
                        "project": project,
                        "character": env_value(env_dir, "CHARACTER") or None,
                    }
                )
        return found

    def _check_repo(self, fork: str | None, repo: str | None) -> str:
        if (fork is None) == (repo is None):
            raise UplinkError(400, "invalid_repo", "give exactly one of fork or repo")
        chosen = fork or repo
        owner, _, name = chosen.partition("/")
        if not REPO.fullmatch(chosen) or name in {".", ".."}:
            raise UplinkError(
                400, "invalid_repo", f"not an owner/repo name: {chosen!r}"
            )
        allowed = {o.lower() for o in self.config.allowed_repo_owners}
        if owner.lower() not in allowed:
            raise UplinkError(
                403, "owner_not_allowed", f"repositories of {owner} are not allowed"
            )
        return chosen

    def _check_unused(self, name: str, character: str) -> None:
        target = self.config.projects_dir / name
        env_dir = self.config.envs_dir / slugify(name)
        if target.exists() or target.is_symlink() or env_dir.exists():
            raise UplinkError(409, "already_exists", f"{name} already exists")
        wanted = character_slug(character)
        for taken in self.characters():
            if taken["character"] and character_slug(taken["character"]) == wanted:
                raise UplinkError(
                    409,
                    "character_taken",
                    f"{taken['project']} already plays {taken['character']}",
                )
        for pending in self._pending.values():
            if slugify(pending.name) == slugify(name):
                raise UplinkError(409, "already_exists", f"{name} is being created")
            if character_slug(pending.character) == wanted:
                raise UplinkError(
                    409, "character_taken", f"{pending.name} is taking {character}"
                )

    def _read_soul(self, name: str) -> str:
        soul_dir = self.config.soul_dir
        path = soul_dir / f"{name}_soul.md"
        if soul_dir.is_symlink():
            raise UplinkError(400, "invalid_soul", f"{soul_dir} is a symlink")
        try:
            # never followed: the folder belongs to an agent, which could point it at a secret
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise UplinkError(404, "soul_not_found", f"no soul at {path}") from None
        except OSError as e:
            raise UplinkError(400, "invalid_soul", f"{path}: {e.strerror}") from None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise UplinkError(400, "invalid_soul", f"{path} is not a regular file")
        with os.fdopen(fd, "rb") as f:
            data = f.read(SOUL_LIMIT + 1)
        if len(data) > SOUL_LIMIT:
            raise UplinkError(400, "invalid_soul", f"{path} is over 64 KiB")
        try:
            return data.decode()
        except UnicodeDecodeError:
            raise UplinkError(400, "invalid_soul", f"{path} is not UTF-8") from None

    def submit(
        self, name: str, fork: str | None, repo: str | None, character: str
    ) -> Job:
        if not valid_project_name(name):
            raise UplinkError(400, "invalid_name", f"not a project name: {name!r}")
        chosen = self._check_repo(fork, repo)
        if not CHARACTER.fullmatch(character):
            raise UplinkError(
                400, "invalid_character", f"not a character name: {character!r}"
            )
        d = shutil.which(self.config.d_command)
        if d is None:
            raise UplinkError(
                503, "d_unavailable", f"{self.config.d_command} is not on the PATH"
            )
        soul = self._read_soul(name)
        with self._lock:
            self._check_unused(name, character)
            job = Job(id=uuid.uuid4().hex, project=name)
            self._jobs[job.id] = job
            while len(self._jobs) > JOBS_KEPT:
                self._jobs.popitem(last=False)
            self._pending[job.id] = Request(name, chosen, character, soul)
        self._executor.submit(self._run, job, d)
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise UplinkError(404, "job_not_found", f"no job {job_id!r}")
        return job

    def _log(self, job: Job, text: str) -> None:
        job.output = (job.output + text)[-OUTPUT_LIMIT:]

    def _env(self) -> dict[str, str]:
        # a caller must never pick devenv's template, so only uplink's own settings reach it
        env = {k: v for k, v in os.environ.items() if not k.startswith("DEVENV_")}
        env["DEVENV_PROJECTS"] = str(self.config.projects_dir)
        env["DEVENV_HOME"] = str(self.config.envs_dir)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        return env

    def _step(self, job: Job, step: str, command: list[str], cwd: Path) -> bool:
        job.step = step
        try:
            done = subprocess.run(
                command,
                cwd=cwd,
                env=self._env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self.config.command_timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            self._log(job, _text(e.stdout) + f"\n{step} timed out\n")
            return False
        self._log(job, done.stdout)
        return done.returncode == 0

    def _run(self, job: Job, d: str) -> None:
        request = self._pending[job.id]
        job.state = "running"
        try:
            ok = self._create(job, d, request)
        except Exception as e:
            self._log(job, f"\n{type(e).__name__}: {e}\n")
            ok = False
        finally:
            with self._lock:
                self._pending.pop(job.id, None)
        job.state = "done" if ok else "failed"
        if ok:
            job.step = None
        job.finished_at = _now()

    def _create(self, job: Job, d: str, request: Request) -> bool:
        command = [
            d,
            "init",
            request.name,
            "--defaults",
            "--data",
            f"fork_url={request.repo}",
            "--data",
            f"character={request.character}",
        ]
        if not self._step(job, "init", command, self.config.projects_dir):
            return False
        project = self.config.projects_dir / request.name
        job.step = "soul"
        shard = project / ".shard"
        if shard.is_symlink() or not shard.is_dir():
            self._log(job, f"{shard} is missing or a symlink\n")
            return False
        soul = shard / "soul.md"
        if soul.is_file() and soul.read_text() == request.soul:
            return True
        soul.write_text(request.soul)
        return self._step(
            job,
            "commit",
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "commit",
                "-q",
                "-m",
                f"Soul for {request.character}",
                "--",
                ".shard/soul.md",
            ],
            project,
        )


def _text(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""
