"""R-U census guards: CLI flag vocabulary and print-surface discipline.

P1 guard: the CBM project-name override flag must expose both spellings
("--cbm-name" canonical, "--name" alias) on every subcommand that accepts it,
with one dest - the pre-R-U state had "--name" on five subcommands and
"--cbm-name" on three, so a user carrying muscle memory across subcommands
got "unrecognized arguments" despite identical semantics.

P2 guards: cli.py stdout discipline - print() is allowed only inside the two
output helpers (_print_step/_print_json) or with file=sys.stderr; every
json.dumps call in cli.py must use the canonical form (ensure_ascii=False,
indent=2, default=str) so result encoding can never fork per call site
(the pre-R-U state had 4/14 call sites missing default=str and one missing
ensure_ascii+indent entirely - cmd_status's missing-db path).
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vrp import cli

CLI_PATH = Path(cli.__file__)


def _tree() -> ast.Module:
    return ast.parse(CLI_PATH.read_text(encoding="utf-8-sig"))


def _add_argument_calls(tree: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
    ]


def test_cbm_name_flag_vocabulary_closed() -> None:
    """Both name-override spellings on all sites, single dest, no third spelling."""
    names = {"--cbm-name", "--name"}
    sites = []
    for node in _add_argument_calls(_tree()):
        opts = {a.value for a in node.args if isinstance(a, ast.Constant)}
        if opts & names:
            sites.append((node.lineno, opts, {k.arg for k in node.keywords}))
    assert len(sites) == 8, f"expected 8 name-override sites, got {len(sites)}"
    for lineno, opts, kws in sites:
        assert opts == names, f"line {lineno}: incomplete alias set {sorted(opts)}"
        assert "dest" in kws, f"line {lineno}: dest must be explicit"
        assert "action" not in kws, f"line {lineno}: name override must be a string option"


def test_cli_prints_are_step_helpers_or_stderr() -> None:
    """print() outside the two helpers must target stderr."""
    tree = _tree()
    helper_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("_print_step", "_print_json"):
            helper_lines.update(range(node.lineno, node.end_lineno + 1))
    violations = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
            and node.lineno not in helper_lines
        ):
            to_stderr = any(
                k.arg == "file" and "stderr" in ast.unparse(k.value) for k in node.keywords
            )
            if not to_stderr:
                violations.append(node.lineno)
    assert not violations, f"stray stdout print() at cli.py lines: {violations}"


def test_cli_json_dumps_canonical_form() -> None:
    """Every json.dumps in cli.py keeps the canonical kwargs (no per-site fork)."""
    for node in ast.walk(_tree()):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "dumps"
        ):
            assert isinstance(node.func.value, ast.Name) and node.func.value.id == "json", (
                f"line {node.lineno}: expected json.dumps"
            )
            kws = {k.arg for k in node.keywords}
            missing = {"ensure_ascii", "indent", "default"} - kws
            assert not missing, f"line {node.lineno}: json.dumps missing {sorted(missing)}"


def test_name_override_aliases_parse_identically() -> None:
    """Both spellings reach one dest on every name-taking subcommand."""
    parser = cli.build_parser()
    subs = [
        "index", "etl", "run", "freeze", "dossier", "transact", "transact-batch", "refresh",
    ]
    for sub in subs:
        argv_tail = [str(Path("t") / "tx.json")] if sub in ("transact", "transact-batch") else []
        by_long = parser.parse_args([sub, "r", *argv_tail, "--cbm-name", "proj-x"])
        by_short = parser.parse_args([sub, "r", *argv_tail, "--name", "proj-x"])
        assert by_long.cbm_name == "proj-x", f"{sub}: --cbm-name broke"
        assert by_short.cbm_name == "proj-x", f"{sub}: --name alias broke"
        assert not hasattr(by_long, "name"), f"{sub}: stale 'name' dest lingers"
