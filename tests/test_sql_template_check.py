"""

Tests for the quoted-variable check on SQL templates.

Values are bound, so a ``{{var}}`` inside a quoted string would be read as
literal text. The check refuses such templates and shows the fixed form. It
tracks single-quoted strings (with ``''`` escapes), double-quoted identifiers,
``--`` line comments and ``/* */`` block comments.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import pytest

from api_dock.sql_template_check import check_quoted_variables


#
# PUBLIC
#
class TestQuotedVariablesRejected:
    """A {{var}} inside quotes is an error that shows the fixed template."""

    @pytest.mark.parametrize("template, fixed", [
        ("UPPER(name) = UPPER('{{name}}')", "UPPER(name) = UPPER({{name}})"),
        ("category = '{{category}}'", "category = {{category}}"),
        ("name ILIKE '%{{q}}%'", "name ILIKE '%' || {{q}} || '%'"),
        ("code = 'a{{x}}b{{y}}'", "code = 'a' || {{x}} || 'b' || {{y}}"),
        ("note = 'it''s {{x}}'", "note = 'it''s ' || {{x}}"),
        ("a = '{{x}}' AND b = '{{y}}'", "a = {{x}} AND b = {{y}}"),
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
        template = "-- don't match subspecies\nWHERE name = '{{name}}'"
        with pytest.raises(ValueError) as error:
            check_quoted_variables(template)
        assert "WHERE name = {{name}}" in str(error.value)


class TestTemplatesAccepted:
    """Templates without quoted variables pass unchanged."""

    @pytest.mark.parametrize("template", [
        "UPPER(name) = UPPER({{name}})",
        "name ILIKE '%' || {{q}} || '%'",
        "name LIKE 'a%' AND id = {{id}}",
        "note = 'it''s' AND id = {{id}}",
        "-- don't match subspecies\nWHERE name = {{name}}",
        "/* it's a block\n comment */ id = {{id}}",
        "-- '{{old}}' was the 0.7 form\nid = {{id}}",
        'SELECT "Column Name" FROM t WHERE id = {{id}}',
        "SELECT 1",
        "SELECT $$it's$$ AS note WHERE id = {{id}}",
        "SELECT $tag$it's $$ here$tag$ AS note WHERE id = {{id}}",
        "SELECT '{{' AS opening WHERE id = {{id}}",
        "SELECT '}}{{' AS braces",
        'SELECT "{{" FROM t',
        "SELECT $1",
    ])
    def test_accepted(self, template: str) -> None:
        """No error is raised and nothing is returned."""
        assert check_quoted_variables(template) is None
