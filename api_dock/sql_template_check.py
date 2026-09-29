"""

SQL Template Check Module for API Dock

Refuses SQL templates that put a {{variable}} inside quotes. Values are sent to
the database separately from the SQL text, so a marker inside a quoted string
would be read as literal text instead of the value.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import re
from typing import List, Optional, Tuple


#
# CONSTANTS
#
# A whole {{variable}} placeholder, captured so re.split keeps it. It matches
# what sql_builder binds, so a lone '{{' in a string is left alone.
VARIABLE_TOKEN: re.Pattern[str] = re.compile(r'(\{\{[^{}]+\}\})')

# The opening of a dollar-quoted string: $$ or $tag$. A tag can't start with a
# digit, so a numbered parameter such as $1 is not matched.
DOLLAR_QUOTE: re.Pattern[str] = re.compile(r'\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$')

# Segment kinds produced by _split_sql.
CODE: str = "code"
STRING: str = "string"
IDENTIFIER: str = "identifier"
COMMENT: str = "comment"


#
# PUBLIC
#
def check_quoted_variables(template: str) -> None:
    """Raise if a {{variable}} appears inside a quoted string or identifier.

    Single-quoted strings (with '' as an escaped quote), dollar-quoted strings
    ($$...$$ and $tag$...$tag$), double-quoted identifiers, -- line comments
    and /* */ block comments are recognized.
    Variables inside comments are ignored. The template is not changed.

    Args:
        template: SQL template to check.

    Raises:
        ValueError: If a variable is quoted. For a string the message
            contains the template with that string rewritten without quotes
            around the variable.
    """
    segments = _split_sql(template)

    for kind, text in segments:
        if kind == IDENTIFIER and VARIABLE_TOKEN.search(text):
            raise ValueError(
                f"A column name can't come from a variable here: {text}. "
                "Variables are sent as values, not SQL text."
            )

    if any(_is_quoted_variable(kind, text) for kind, text in segments):
        fixed = ''.join(
            _unquoted_form(text) if _is_quoted_variable(kind, text) else text
            for kind, text in segments
        )
        raise ValueError(
            "Variables are sent as values, so they cannot be inside quotes. "
            f"Use: {fixed}"
        )


#
# INTERNAL
#
def _is_quoted_variable(kind: str, text: str) -> bool:
    """Check whether a segment is a string containing a variable.

    Args:
        kind: Segment kind from _split_sql.
        text: Segment text.

    Returns:
        True if the segment is a string with a {{variable}} in it.
    """
    return kind == STRING and VARIABLE_TOKEN.search(text) is not None


def _split_sql(template: str) -> List[Tuple[str, str]]:
    """Split SQL into code, string, identifier and comment segments.

    Joining the segments' text gives back the template exactly.

    Args:
        template: SQL text.

    Returns:
        List of ``(kind, text)`` pairs in order. Quoted and comment segments
        include their delimiters.
    """
    segments: List[Tuple[str, str]] = []
    code_start = 0
    position = 0
    while position < len(template):
        special = _special_segment(template, position)
        if special is None:
            position += 1
            continue
        kind, end = special
        if code_start < position:
            segments.append((CODE, template[code_start:position]))
        segments.append((kind, template[position:end]))
        position = code_start = end
    if code_start < len(template):
        segments.append((CODE, template[code_start:]))
    return segments


def _special_segment(template: str, start: int) -> Optional[Tuple[str, int]]:
    """Identify a string, identifier or comment starting at a position.

    Args:
        template: SQL text.
        start: Index to look at.

    Returns:
        Tuple of the segment kind and the index just past its end, or None if
        start is ordinary code. An unterminated segment runs to the end.
    """
    if template.startswith('--', start):
        end = template.find('\n', start)
        return COMMENT, len(template) if end == -1 else end
    if template.startswith('/*', start):
        end = template.find('*/', start + 2)
        return COMMENT, len(template) if end == -1 else end + 2
    if template[start] == "'":
        return STRING, _quoted_end(template, start, "'")
    dollar = DOLLAR_QUOTE.match(template, start)
    if dollar and not _follows_identifier(template, start):
        end = template.find(dollar.group(), dollar.end())
        return STRING, len(template) if end == -1 else end + len(dollar.group())
    if template[start] == '"':
        return IDENTIFIER, _quoted_end(template, start, '"')
    return None


def _follows_identifier(template: str, start: int) -> bool:
    """Check whether a position directly follows a name or number.

    A $ right after a name character is part of that name, not the start of
    a dollar-quoted string.

    Args:
        template: SQL text.
        start: Index to look at.

    Returns:
        True if the character before start is a letter, digit, _ or $.
    """
    return start > 0 and (template[start - 1].isalnum() or template[start - 1] in '_$')


def _quoted_end(template: str, start: int, quote: str) -> int:
    """Find the end of a quoted segment, treating a doubled quote as escaped.

    Args:
        template: SQL text.
        start: Index of the opening quote.
        quote: The quote character.

    Returns:
        Index just past the closing quote, or the template length if unterminated.
    """
    position = start + 1
    while position < len(template):
        if template.startswith(quote * 2, position):
            position += 2
        elif template[position] == quote:
            return position + 1
        else:
            position += 1
    return len(template)


def _unquoted_form(literal: str) -> str:
    """Rewrite a quoted string so its variables are outside the quotes.

    ``'%{{q}}%'`` becomes ``'%' || {{q}} || '%'`` and ``'{{x}}'`` becomes
    ``{{x}}``. Empty strings are dropped. The text of a dollar-quoted string
    is written in single quotes, with each ' doubled.

    Args:
        literal: A single-quoted or dollar-quoted SQL string, including its
            quotes.

    Returns:
        SQL expression joining the string's text and variables with ||.
    """
    content = _string_content(literal)
    parts = [part for part in VARIABLE_TOKEN.split(content) if part]
    return ' || '.join(
        part if VARIABLE_TOKEN.fullmatch(part) else f"'{part}'" for part in parts
    )


def _string_content(literal: str) -> str:
    """Get a string's text in single-quoted form, without its quotes.

    Args:
        literal: A single-quoted or dollar-quoted SQL string, including its
            quotes. It may be unterminated.

    Returns:
        The text between the quotes, with each ' doubled for a dollar-quoted
        string. A single-quoted string's text already has them doubled.
    """
    dollar = DOLLAR_QUOTE.match(literal)
    if dollar is None:
        closed = len(literal) > 1 and literal.endswith("'")
        return literal[1:-1] if closed else literal[1:]
    quote = dollar.group()
    body = literal[len(quote):]
    if body.endswith(quote):
        body = body[:-len(quote)]
    return body.replace("'", "''")
