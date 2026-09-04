"""What the user actually sees — the answer minus the model talking to itself.

Two leaks showed up in the first live Telegram session, both of them the model
narrating its own process into the final message:

    你说得对，我犯了张冠李戴的错误。让我重新核对每个数字的真实归属……
    ## 结论：持仓里最危险的是 ARM

    用户问「为什么？」，但没有上下文指明具体针对什么……让我直接询问澄清。
    ---
    你的问题「为什么？」缺少上下文，我无法确定你想问的是哪件事。

The first is a reply addressed to the grounding checker, which the user never
saw complain. The second is deliberation the model itself separated from the
answer with a horizontal rule — it knew which half was the answer.

The tempting fix is a prompt line: "don't narrate your reasoning". This project
has already measured what those are worth — three rounds of instructing the
model to show its arithmetic changed nothing, and making the *checker* accept
shown arithmetic changed behaviour immediately. So this is a rule the code
enforces, not one the prompt requests.

Deliberately narrow. It fires only when the model itself marked the boundary
with a rule or a heading, and only when what sits before that boundary reads as
deliberation. Prose that runs straight into its answer with no marker is left
alone: guessing where an unmarked answer begins would eventually eat one.
"""

from __future__ import annotations

import re

#: A horizontal rule on its own line — the model's own "the answer starts here".
_RULE = re.compile(r"^[ \t]*(?:-{3,}|_{3,}|\*{3,})[ \t]*$", re.M)

#: A markdown heading, the other marker the model uses for the same purpose.
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+\S", re.M)

#: First-person process talk. 让我们 is excluded on purpose — "让我们看看这几只"
#: is a normal way to open an answer, while 让我 alone is the model narrating.
_DELIBERATION = re.compile(
    r"让我(?!们)|我需要|我应该|我先|我来看|用户(?:问|想问|说|的问题)|你说得对"
    r"|我犯了|我此前|我不该|重新核对|直接询问|澄清一下|让我直接")

#: How much text may be discarded as preamble. A bound rather than a judgment:
#: if the marker sits this far in, whatever precedes it is too substantial to
#: throw away on a heuristic, however much it sounds like thinking out loud.
MAX_PREAMBLE = 500


def strip_deliberation(text: str) -> str:
    """Drop a leading block of process narration the model marked off itself.

    Returns ``text`` unchanged unless every condition holds: there is a rule or
    heading, what precedes it reads as deliberation, it is under MAX_PREAMBLE
    characters, and something is left afterwards.
    """
    body = text or ""
    for pattern, keep_marker in ((_RULE, False), (_HEADING, True)):
        match = pattern.search(body)
        if not match:
            continue
        preamble = body[: match.start()]
        remainder = body[match.start() if keep_marker else match.end():].strip()
        if (preamble.strip() and remainder
                and len(preamble) <= MAX_PREAMBLE
                and _DELIBERATION.search(preamble)):
            return remainder
    return body.strip()
