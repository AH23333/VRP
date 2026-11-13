"""R-V census guards: module-scope state discipline in vrp/.

A-face guard 1: module-level set constants must be frozen - no ast.Set
literal and no set() constructor call at module scope (the pre-R-V state
had 22 mutable set literals + 8 set("...") calls, none ever mutated;
a long-lived MCP process shares this module state, so an accidental
.add() on a "constant" would poison every subsequent scan).

A-face guard 2: module-level dict names must never be directly mutated
(attribute mutator call / subscript store / augmented assign / del) -
stdlib has no frozendict, so lookup tables stay dict and the discipline
is enforced structurally instead. Allowlist: render_chain's six memo
dicts, whose only sanctioned direct mutations are the three subscript
stores inside its dedup accessors (plus _reset_state(), which clears
via a loop variable and is therefore not a direct target).

Memo lifecycle pin: render_chain's memos are the ONLY intentional
module-level mutable state in vrp/ - they must key dedup idempotently
and _reset_state() must empty all six (build_chain calls it at entry,
render_chain.py:1255, so state never leaks across builds).
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vrp import render_chain
from vrp import s0_precheck

VRP_DIR = Path(render_chain.__file__).parent

MEMO_ALLOWLIST = {
    "_dir_stem_of", "_dir_stem_used", "_grid_stem_of",
    "_grid_stem_used", "_s4f_of", "_s4f_used",
}
MUTATORS = {
    "add", "discard", "remove", "pop", "clear", "update",
    "setdefault", "intersection_update", "difference_update",
    "symmetric_difference_update",
}


def _module_scope(tree: ast.Module):
    """Yield module-level statements, descending into top-level if/try."""
    for node in tree.body:
        if isinstance(node, (ast.If, ast.Try)):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    yield sub
        else:
            yield node


def _module_dict_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in _module_scope(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        val = node.value
        if isinstance(val, ast.Dict) or (
            isinstance(val, ast.Call)
            and isinstance(val.func, ast.Name)
            and val.func.id == "dict"
        ):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def test_module_level_sets_are_frozen() -> None:
    """No mutable set literal / set() call may appear at module scope."""
    offenders: list[str] = []
    for py in sorted(VRP_DIR.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        for node in _module_scope(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            val = node.value
            bad = isinstance(val, ast.Set) or (
                isinstance(val, ast.Call)
                and isinstance(val.func, ast.Name)
                and val.func.id == "set"
            )
            if bad:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        offenders.append(f"{py.name}:{node.lineno} {t.id}")
    assert not offenders, "module-level mutable sets:\n  " + "\n  ".join(offenders)


def test_module_level_dicts_never_directly_mutated() -> None:
    """Direct mutation of a module-level dict name is banned outside the memo allowlist."""
    offenders: list[str] = []
    for py in sorted(VRP_DIR.glob("*.py")):
        src = py.read_text(encoding="utf-8-sig")
        tree = ast.parse(src)
        dict_names = _module_dict_names(tree)
        if not dict_names:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in MUTATORS:
                    base = node.func.value
                    if isinstance(base, ast.Name) and base.id in dict_names:
                        offenders.append(f"{py.name}:{node.lineno} {base.id}.{node.func.attr}()")
            if isinstance(node, (ast.Subscript, ast.Attribute)):
                ctx = getattr(node, "ctx", None)
                if isinstance(ctx, (ast.Store, ast.Del)):
                    base = node.value if isinstance(node, ast.Subscript) else None
                    if isinstance(base, ast.Name) and base.id in dict_names:
                        if py.name != "render_chain.py" or base.id not in MEMO_ALLOWLIST:
                            offenders.append(f"{py.name}:{node.lineno} {base.id}[...] = / del")
            if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                if node.target.id in dict_names:
                    offenders.append(f"{py.name}:{node.lineno} {node.target.id} <op>=")
    assert not offenders, "module-level dict mutated directly:\n  " + "\n  ".join(offenders)


def test_render_chain_memo_lifecycle() -> None:
    """Memos dedup idempotently; _reset_state() empties all six."""
    s1 = render_chain.dir_page_stem("modX", "pkg/sub")
    s2 = render_chain.dir_page_stem("modX", "pkg/sub")
    assert s1 == s2 and ("modX", "pkg/sub") in render_chain._dir_stem_of
    g1 = render_chain.grid_stem_of("modX", "pkg/sub/file.py")
    assert g1 and ("modX", "pkg/sub/file.py") in render_chain._grid_stem_of
    f1 = render_chain.file_page_stem("pkg/sub", "thing.py")
    assert f1 and ("pkg/sub", "thing.py") in render_chain._s4f_of
    render_chain._reset_state()
    for name in MEMO_ALLOWLIST:
        assert not getattr(render_chain, name), f"{name} not cleared by _reset_state()"
    assert render_chain.dir_page_stem("modX", "pkg/sub") == s1  # idempotent regeneration
    render_chain._reset_state()


def test_precheck_ignore_dirs_frozen() -> None:
    """Spot-pin: the widest-shared skip set is a frozenset constant."""
    assert isinstance(s0_precheck.IGNORE_DIRS, frozenset)
    assert ".git" in s0_precheck.IGNORE_DIRS
