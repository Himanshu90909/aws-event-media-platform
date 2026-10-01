"""Prompt templates for the media-jobs agent.

Prompts live here, separate from agent logic and tools, so wording changes
never touch code paths and reviewers can see exactly what the model reads.
"""

QUERY_SYSTEM = (
    "You are a media-pipeline assistant for an event-driven AWS platform. "
    "Answer strictly from the provided evidence excerpts and cite them as [n]. "
    "If the evidence does not answer the question, say so plainly. "
    "Never invent job ids, statuses, or file names."
)

QUERY_USER = """Evidence:
{context}

Question: {question}

Answer with citations like [1]. If the evidence is insufficient, say exactly that."""


def render_query_prompt(question: str, numbered_excerpts: list[str]) -> tuple[str, str]:
    """Return (system, user) prompts for a question with numbered evidence."""
    context = "\n".join(f"[{i}] {excerpt}" for i, excerpt in enumerate(numbered_excerpts, start=1))
    return QUERY_SYSTEM, QUERY_USER.format(context=context or "(no evidence)", question=question)
