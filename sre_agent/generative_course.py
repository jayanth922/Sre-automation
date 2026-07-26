#!/usr/bin/env python3
"""
Generative learning content (project #5, the "generative course/UI" idea).

The video's #5 is AI that *generates* a course — structured slides you click
through, plus a quiz — for any topic (paradigm.study), rather than static
content. The SRE application: generate an on-call **training module** for an
incident class (or any topic) so a resolved incident becomes teachable material.

This is genuinely generative: an LLM produces the course structure (sections →
slides + a quiz) as JSON, which we parse into a typed ``Course``. A deterministic
outline is used only as a fallback so generation never hard-fails. The LLM is
injected, so parsing/rendering is unit-testable with a stub.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


@dataclass
class QuizQuestion:
    question: str
    options: List[str]
    answer_index: int


@dataclass
class CourseSection:
    heading: str
    slides: List[str] = field(default_factory=list)


@dataclass
class Course:
    topic: str
    title: str
    sections: List[CourseSection] = field(default_factory=list)
    quiz: List[QuizQuestion] = field(default_factory=list)
    generated: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic": self.topic,
            "title": self.title,
            "sections": [{"heading": s.heading, "slides": s.slides} for s in self.sections],
            "quiz": [{"question": q.question, "options": q.options, "answer_index": q.answer_index} for q in self.quiz],
            "generated": self.generated,
        }


def _fallback_course(topic: str) -> Course:
    return Course(
        topic=topic,
        title=f"Intro to {topic}",
        sections=[
            CourseSection("Overview", [f"What is {topic}?", f"Why {topic} matters for on-call."]),
            CourseSection("Detection", ["Signals to watch.", "Relevant alerts and dashboards."]),
            CourseSection("Response", ["First steps.", "Safe remediation and rollback."]),
        ],
        quiz=[QuizQuestion(f"What is the first step when {topic} occurs?",
                           ["Ignore it", "Assess impact and severity", "Delete the service"], 1)],
        generated=False,
    )


def _parse_course(topic: str, raw: str) -> Course:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    data = json.loads(match.group(0)) if match else json.loads(raw)
    sections = [
        CourseSection(str(s.get("heading", "Section")), [str(x) for x in (s.get("slides") or [])])
        for s in (data.get("sections") or [])
    ]
    quiz = [
        QuizQuestion(str(q.get("question", "")), [str(o) for o in (q.get("options") or [])], int(q.get("answer_index", 0)))
        for q in (data.get("quiz") or [])
    ]
    if not sections:
        raise ValueError("no sections in generated course")
    return Course(topic=topic, title=str(data.get("title", f"Course: {topic}")), sections=sections, quiz=quiz)


async def generate_learning_module(topic: str, llm: Any) -> Course:
    """Generate a course (sections/slides + quiz) for a topic via the LLM."""
    from langchain_core.messages import HumanMessage, SystemMessage

    try:
        resp = await llm.ainvoke([
            SystemMessage(content=(
                "You generate concise on-call training courses. Respond with ONLY JSON: "
                '{"title": str, "sections": [{"heading": str, "slides": [str, ...]}], '
                '"quiz": [{"question": str, "options": [str,...], "answer_index": int}]}'
            )),
            HumanMessage(content=f"Create a short course for the topic: {topic}"),
        ])
        return _parse_course(topic, str(getattr(resp, "content", resp)))
    except Exception as e:
        logger.warning(f"GenerativeCourse: generation failed ({e}); using fallback outline.")
        return _fallback_course(topic)


def course_to_markdown(course: Course) -> str:
    lines = [f"# {course.title}", ""]
    for i, section in enumerate(course.sections, 1):
        lines.append(f"## {i}. {section.heading}")
        for slide in section.slides:
            lines.append(f"- {slide}")
        lines.append("")
    if course.quiz:
        lines.append("## Quiz")
        for i, q in enumerate(course.quiz, 1):
            lines.append(f"{i}. {q.question}")
            for j, opt in enumerate(q.options):
                marker = "✓" if j == q.answer_index else " "
                lines.append(f"   - [{marker}] {opt}")
        lines.append("")
    return "\n".join(lines)
