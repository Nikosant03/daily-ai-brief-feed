"""Regression tests for the bug that stalled the public feed from 2026-09-25
to 2026-10-01: a 0-byte mp3 arrived in pending/, `gh release upload` refused
it, the upload loop aborted on it, and so every later episode -- and the
feed.xml rebuild, and the pending/ cleanup -- was lost on every push after
that."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import publish_and_build_feed as pf


@pytest.fixture
def pending(tmp_path, monkeypatch):
    directory = tmp_path / "pending"
    directory.mkdir()
    monkeypatch.setattr(pf, "PENDING_DIR", directory)
    return directory


def test_empty_mp3_is_discarded_and_never_uploaded(pending, monkeypatch):
    (pending / "2026-09-28.mp3").write_bytes(b"")
    calls = []
    monkeypatch.setattr(pf, "_gh", lambda *args: calls.append(args) or "")

    uploaded, problems = pf.upload_pending_episodes()

    assert uploaded == []
    assert calls == [], "an empty mp3 must never be offered to gh"
    assert len(problems) == 1 and "empty" in problems[0]
    assert not (pending / "2026-09-28.mp3").exists(), "the empty file must be cleared, not retried forever"


def test_one_bad_file_does_not_block_the_good_episode_behind_it(pending, monkeypatch):
    """The exact shape of the outage: 2026-09-28 empty, 2026-09-30 fine.
    Sorted order puts the bad one first."""
    (pending / "2026-09-28.mp3").write_bytes(b"")
    (pending / "2026-09-30.mp3").write_bytes(b"real mp3 bytes")
    uploaded_args = []
    monkeypatch.setattr(pf, "_gh", lambda *args: uploaded_args.append(args) or "")

    uploaded, problems = pf.upload_pending_episodes()

    assert [p.name for p in uploaded] == ["2026-09-30.mp3"]
    assert len(uploaded_args) == 1 and "2026-09-30.mp3" in uploaded_args[0][3]
    assert len(problems) == 1


def test_a_failing_upload_does_not_block_later_days_either(pending, monkeypatch):
    (pending / "2026-09-29.mp3").write_bytes(b"bytes")
    (pending / "2026-09-30.mp3").write_bytes(b"bytes")

    def fake_gh(*args):
        if "2026-09-29.mp3" in args[3]:
            raise RuntimeError("gh said no")
        return ""

    monkeypatch.setattr(pf, "_gh", fake_gh)

    uploaded, problems = pf.upload_pending_episodes()

    assert [p.name for p in uploaded] == ["2026-09-30.mp3"]
    assert len(problems) == 1 and "gh said no" in problems[0]


def test_gh_errors_carry_the_stderr_that_explains_them(monkeypatch):
    """The 2026-09-28 failure log showed only "returned non-zero exit status 1"
    because capture_output=True swallowed gh's own message."""
    completed = subprocess.CompletedProcess(
        args=["gh"], returncode=1, stdout="", stderr="HTTP 422: Validation Failed"
    )
    monkeypatch.setattr(pf.subprocess, "run", lambda *a, **k: completed)

    with pytest.raises(RuntimeError, match="422: Validation Failed"):
        pf._gh("release", "upload", "episodes", "x.mp3")


def test_clean_run_reports_no_problems(pending, monkeypatch):
    (pending / "2026-09-30.mp3").write_bytes(b"bytes")
    monkeypatch.setattr(pf, "_gh", lambda *args: "")

    uploaded, problems = pf.upload_pending_episodes()

    assert [p.name for p in uploaded] == ["2026-09-30.mp3"]
    assert problems == []
