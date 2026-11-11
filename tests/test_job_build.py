"""job_build.Build: remembers what a job has taken so far and can undo it in one place."""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JB = importlib.import_module("job_build")
sys.path.pop(0)


def test_stop_undoes_everything_newest_first_and_carries_the_answer():
    log, b = [], JB.Build()
    b.took("job number", undo=lambda why: log.append("number"))
    b.took("folder", undo=lambda why: log.append("folder"))
    b.took("sheet numbers", undo=lambda why: log.append(f"sheet:{why}"))
    stop = b.stop("The sheet could not be written", 500, because="sheet not written", detail=7)
    assert isinstance(stop, JB.Stop)
    assert stop.answer == ({"error": "The sheet could not be written", "detail": 7}, 500)
    assert log == ["sheet:sheet not written", "folder", "number"]


def test_things_with_no_undo_are_remembered_but_left_alone():
    b = JB.Build()
    b.took("serials")
    assert b.has("serials") and not b.has("folder")
    assert b.rollback() == []


def test_keep_means_nothing_taken_so_far_is_undone_later():
    log, b = [], JB.Build()
    b.took("job number", undo=lambda why: log.append("number"))
    b.keep()
    b.took("data files", undo=lambda why: log.append("files"))
    assert b.rollback() == ["data files"]
    assert log == ["files"] and b.has("job number")


def test_rollback_runs_once():
    log, b = [], JB.Build()
    b.took("folder", undo=lambda why: log.append("folder"))
    b.rollback(); b.rollback()
    assert log == ["folder"]


def test_a_failing_undo_does_not_stop_the_others_or_hide_the_error():
    log, b = [], JB.Build()
    b.took("job number", undo=lambda why: log.append("number"))
    b.took("folder", undo=lambda why: 1 / 0)
    stop = b.stop("Serials could not be taken", 500)
    assert log == ["number"]
    assert stop.answer[0]["error"] == "Serials could not be taken"
    assert [what for what, _ in b.undo_errors] == ["folder"]


def test_refuse_is_a_stop_that_undoes_nothing():
    stop = JB.refuse("Customer is required", 400)
    assert stop.answer == ({"error": "Customer is required"}, 400)
    with pytest.raises(JB.Stop):
        raise stop


def test_warnings_collect_in_order():
    b = JB.Build()
    b.warn("first"); b.warn("second")
    assert b.warnings == ["first", "second"]
