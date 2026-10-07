"""

Tests for the quoted-variable check on SQL templates.

Values are bound, so a ``{{var}}`` inside a quoted string would be read as
literal text. The check refuses such templates and shows the fixed form. A
``{{var}}`` inside a comment is refused too, as is a template that ends inside a
comment, which would comment out the SQL added after it. The scanner tracks
single-quoted strings (with ``''`` escapes), dollar-quoted strings,
double-quoted identifiers, ``--`` line comments and nested ``/* */`` block
comments.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import pytest

from api_dock.sql_template_check import (
    check_comment_at_end,
    check_commented_variables,
    check_quoted_variables,
)


#
# PUBLIC
#
class TestQuotedVariablesRejected:
    """A {{var}} inside quotes is an error that shows the fixed template."""

    @pytest.mark.parametrize("template, fixed", [
        ("UPPER(name) = UPPER('%{{name}}%')", "UPPER(name) = UPPER('%' || {{name}} || '%')"),
        ("category = 'x{{category}}'", "category = 'x' || {{category}}"),
        ("name ILIKE '%{{q}}%'", "name ILIKE '%' || {{q}} || '%'"),
        ("code = 'a{{x}}b{{y}}'", "code = 'a' || {{x}} || 'b' || {{y}}"),
        ("note = 'it''s {{x}}'", "note = 'it''s ' || {{x}}"),
        ("a = 'a{{x}}' AND b = '{{y}}b'", "a = 'a' || {{x}} AND b = {{y}} || 'b'"),
    ])
    def test_single_quoted_variable(self, template: str, fixed: str) -> None:
        """The error contains the template with each quoted variable unquoted."""
        with pytest.raises(ValueError) as error:
            check_quoted_variables(template)
        assert fixed in str(error.value)

    def test_double_quoted_identifier(self) -> None:
        """A variable used as a quoted column name can't be bound."""
        with pytest.raises(ValueError, match="column name"):
            check_quoted_variables('SELECT "{{col}}" FROM t')

    @pytest.mark.parametrize("template, fixed", [
        ("a = $${{x}}$$", "a = {{x}}"),
        ("a = $tag$%{{x}}%$tag$", "a = '%' || {{x}} || '%'"),
        ("a = $$it's {{x}}$$", "a = 'it''s ' || {{x}}"),
    ])
    def test_dollar_quoted_variable(self, template: str, fixed: str) -> None:
        """A variable inside a dollar-quoted string is quoted too."""
        with pytest.raises(ValueError) as error:
            check_quoted_variables(template)
        assert fixed in str(error.value)

    def test_comment_apostrophe_does_not_hide_later_quote(self) -> None:
        """An apostrophe in a comment doesn't hide a quoted variable after it."""
        template = "-- don't match subspecies\nWHERE name LIKE '{{name}}%'"
        with pytest.raises(ValueError) as error:
            check_quoted_variables(template)
        assert "WHERE name LIKE {{name}} || '%'" in str(error.value)

    @pytest.mark.parametrize("template", [
        "name = '{{name}}'",
        "UPPER(name) = UPPER('{{name}}') AND id = {{id}}",
        "a = '{{x}}' AND b = '{{y}}'",
    ])
    def test_whole_string_variable_is_allowed(self, template: str) -> None:
        """A string that is exactly one variable is read as that variable, so it passes."""
        check_quoted_variables(template)


class TestCommentedVariablesRejected:
    """A {{var}} inside a comment is an error that asks for it to be removed."""

    @pytest.mark.parametrize("template", [
        "SELECT 1 -- {{name}}\nWHERE id = {{id}}",
        "SELECT 1 /* {{name}} */",
        "-- '{{old}}' was the 0.7 form\nid = {{id}}",
        "SELECT 1 /* outer /* inner */ {{name}} */",
    ])
    def test_commented_variable(self, template: str) -> None:
        """The error says to remove the variable and suggests no rewrite."""
        with pytest.raises(ValueError) as error:
            check_commented_variables(template)
        message = str(error.value)
        assert "comment" in message
        assert "remove" in message
        assert "Use:" not in message


class TestCommentAtEndRejected:
    """A template ending inside a comment would comment out the SQL after it."""

    @pytest.mark.parametrize("template", [
        "SELECT * FROM t -- all rows",
        "a = {{a}} -- first filter\n   ",
        "ORDER BY x /* newest first",
        "SELECT 1 /* outer /* inner */",
    ])
    def test_ends_in_comment(self, template: str) -> None:
        """The error says to move the comment onto its own line or use /* */."""
        with pytest.raises(ValueError, match=r"own line.*/\* \*/"):
            check_comment_at_end(template)


class TestTemplatesAccepted:
    """Templates without quoted variables pass unchanged."""

    @pytest.mark.parametrize("template", [
        "UPPER(name) = UPPER({{name}})",
        "name ILIKE '%' || {{q}} || '%'",
        "name LIKE 'a%' AND id = {{id}}",
        "note = 'it''s' AND id = {{id}}",
        "-- don't match subspecies\nWHERE name = {{name}}",
        "/* it's a block\n comment */ id = {{id}}",
        'SELECT "Column Name" FROM t WHERE id = {{id}}',
        "SELECT 1",
        "SELECT $$it's$$ AS note WHERE id = {{id}}",
        "SELECT $tag$it's $$ here$tag$ AS note WHERE id = {{id}}",
        "SELECT '{{' AS opening WHERE id = {{id}}",
        "SELECT '}}{{' AS braces",
        'SELECT "{{" FROM t',
        "SELECT $1",
        "SELECT 1 /* outer /* inner */ still comment */ WHERE id = {{id}}",
        "SELECT 1 /* outer /* inner */ it's */ WHERE id = {{id}}",
        "SELECT 1 /*/ it's */ WHERE id = {{id}}",
        "SELECT 1 /* outer /* inner */ comment */ {{name}}",
        "-- note\nWHERE id = {{id}}",
        "SELECT * FROM t /* note */",
        "name = 'a -- b'",
        "name = 'a /* b'",
    ])
    def test_accepted(self, template: str) -> None:
        """No check raises and none returns anything."""
        assert check_quoted_variables(template) is None
        assert check_commented_variables(template) is None
        assert check_comment_at_end(template) is None
