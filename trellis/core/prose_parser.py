"""Lark-based parser for the Prose pipeline format.

Prose is an alternative to YAML for expressing pipeline templates.  Both formats
produce the same canonical dict structure used by the rest of Trellis::

    {
        "name": str,
        "description": str,
        "agents": [str, ...],
        "post_ready": [str, ...],
        "parallel_groups": [[str, ...], ...],
        "gating": {"default": str, "overrides": {str: str}},
    }
"""

from __future__ import annotations

from lark import Lark, Transformer, v_args

# Grammar uses explicit INDENT/DEDENT tokens inserted by _preprocess().
# Newlines are ignored — structure comes entirely from indentation tokens.
GRAMMAR = r"""
    start: pipeline

    pipeline: "pipeline" NAME ":" INDENT statement+ DEDENT

    ?statement: description
              | session
              | gate
              | parallel_block

    description: "description:" QUOTED_STRING
    session: "session:" NAME
    gate: "gate:" GATE_MODE
    parallel_block: "parallel:" INDENT parallel_session+ DEDENT

    parallel_session: "session:" NAME

    NAME: /[a-zA-Z_][a-zA-Z0-9_-]*/
    GATE_MODE: "auto" | "human-review" | "llm-decides"
    QUOTED_STRING: "\"" /[^"]*/ "\""
    INDENT: "<INDENT>"
    DEDENT: "<DEDENT>"

    %import common.NEWLINE
    %import common.WS_INLINE
    %ignore WS_INLINE
    %ignore NEWLINE
"""


def _preprocess(text: str) -> str:
    """Convert indentation to explicit <INDENT>/<DEDENT> tokens.

    Strips comments and blank lines, then emits <INDENT> / <DEDENT> markers
    based on indentation changes.
    """
    lines: list[str] = []
    indent_stack = [0]

    for raw_line in text.splitlines():
        stripped = raw_line.lstrip()
        # Skip blank lines and comments
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(raw_line) - len(stripped)

        if indent > indent_stack[-1]:
            indent_stack.append(indent)
            lines.append("<INDENT>")
        else:
            while indent < indent_stack[-1]:
                indent_stack.pop()
                lines.append("<DEDENT>")

        lines.append(stripped)

    # Close any remaining indents
    while len(indent_stack) > 1:
        indent_stack.pop()
        lines.append("<DEDENT>")

    return "\n".join(lines) + "\n"


_parser = Lark(GRAMMAR, parser="lalr", maybe_placeholders=False)


@v_args(inline=True)
class PipelineTransformer(Transformer):
    """Transform the parse tree into the canonical pipeline dict."""

    def QUOTED_STRING(self, token):
        # Strip surrounding quotes
        return str(token)[1:-1]

    def NAME(self, token):
        return str(token)

    def GATE_MODE(self, token):
        return str(token)

    def INDENT(self, token):
        return token

    def DEDENT(self, token):
        return token

    def description(self, value):
        return ("description", value)

    def session(self, name):
        return ("session", name)

    def gate(self, mode):
        return ("gate", mode)

    def parallel_session(self, name):
        return name

    def parallel_block(self, *args):
        # Filter out INDENT/DEDENT tokens, keep only agent name strings
        names = [a for a in args if isinstance(a, str) and str(a) not in ("<INDENT>", "<DEDENT>")]
        return ("parallel", names)

    def pipeline(self, *args):
        # First non-token string is the name, tuples are statements
        name = None
        statements = []
        for a in args:
            if isinstance(a, tuple):
                statements.append(a)
            elif isinstance(a, str) and str(a) not in ("<INDENT>", "<DEDENT>") and name is None:
                name = a

        result: dict = {
            "name": name,
            "description": "",
            "agents": [],
            "post_ready": [],
            "parallel_groups": [],
            "gating": {"default": "auto", "overrides": {}},
        }

        last_agent: str | None = None

        for stmt in statements:
            kind, value = stmt
            if kind == "description":
                result["description"] = value
            elif kind == "session":
                result["agents"].append(value)
                last_agent = value
            elif kind == "gate":
                if last_agent is None:
                    # Gate before any session → sets default
                    result["gating"]["default"] = value
                else:
                    result["gating"]["overrides"][last_agent] = value
            elif kind == "parallel":
                result["post_ready"].extend(value)
                result["parallel_groups"].append(value)

        # Auto-group main agents if any exist
        if result["agents"]:
            result["parallel_groups"].insert(0, list(result["agents"]))

        return result

    def start(self, pipeline):
        return pipeline


def parse_pipeline_prose(text: str) -> dict:
    """Parse a ``.prose`` pipeline file and return the canonical pipeline dict."""
    preprocessed = _preprocess(text)
    tree = _parser.parse(preprocessed)
    return PipelineTransformer().transform(tree)


def emit_pipeline_prose(data: dict) -> str:
    """Serialize a pipeline dict to ``.prose`` format."""
    lines: list[str] = []
    name = data.get("name", "unnamed")
    lines.append(f"pipeline {name}:")

    desc = data.get("description", "")
    if desc:
        lines.append(f'  description: "{desc}"')

    # Parallel block for post_ready agents
    post_ready = data.get("post_ready", [])
    if post_ready:
        lines.append("")
        lines.append("  parallel:")
        for agent in post_ready:
            lines.append(f"    session: {agent}")

    # Main agents with gates
    agents = data.get("agents", [])
    gating = data.get("gating", {})
    overrides = gating.get("overrides", {})

    for agent in agents:
        lines.append("")
        lines.append(f"  session: {agent}")
        mode = overrides.get(agent, gating.get("default", "auto"))
        lines.append(f"  gate: {mode}")

    lines.append("")
    return "\n".join(lines)
