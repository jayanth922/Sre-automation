#!/usr/bin/env python3
"""Unit tests for generative learning content (project #5, generative)."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "generative_course.py"
_spec = importlib.util.spec_from_file_location("generative_course", _MODULE_PATH)
gc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gc
_spec.loader.exec_module(gc)


class FakeLLM:
    def __init__(self, content):
        self._content = content

    async def ainvoke(self, messages):
        class R:
            content = self._content
        return R()


GOOD_JSON = """
{"title": "CrashLoopBackOff 101",
 "sections": [{"heading": "What it is", "slides": ["Pods restart repeatedly", "Common causes"]}],
 "quiz": [{"question": "First step?", "options": ["Ignore", "Check logs"], "answer_index": 1}]}
"""


def test_generate_parses_llm_json():
    course = asyncio.run(gc.generate_learning_module("CrashLoopBackOff", FakeLLM(GOOD_JSON)))
    assert course.title == "CrashLoopBackOff 101"
    assert course.sections[0].heading == "What it is"
    assert course.quiz[0].answer_index == 1
    assert course.generated is True


def test_generate_tolerates_prose_around_json():
    wrapped = "Sure, here is your course:\n" + GOOD_JSON + "\nHope that helps!"
    course = asyncio.run(gc.generate_learning_module("X", FakeLLM(wrapped)))
    assert course.sections and course.title == "CrashLoopBackOff 101"


def test_generate_falls_back_on_bad_output():
    course = asyncio.run(gc.generate_learning_module("OOMKilled", FakeLLM("not json at all")))
    assert course.generated is False
    assert course.sections  # fallback outline present


def test_course_to_markdown_renders_slides_and_quiz():
    course = asyncio.run(gc.generate_learning_module("CrashLoopBackOff", FakeLLM(GOOD_JSON)))
    md = gc.course_to_markdown(course)
    assert "# CrashLoopBackOff 101" in md
    assert "- Pods restart repeatedly" in md
    assert "[✓] Check logs" in md


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
