"""N19 (#219): CALLS edge args x callee signature reconciliation.

caller face = edges.properties_json args[{i,e,k,v}] (CBM verbatim);
callee face = symbols.signature (schema v3 column). The signature STRING
is the only trusted basis: CBM param_count counts type-annotated params
only (~21% disagreement with the signature string measured on dogfood,
#219 probe) and builtins carry a placeholder param_count=0 - both are
reference values, never the reconciliation base.

Rules (report not gate - mdcode/langmatrix precedent, rc always 0):
- arity: required <= n_args <= total (positional + keyword combined);
  a param is optional when it has a default (top-level '=' in the raw
  param), a ts '?' suffix, or is *args/**kwargs (variadic - arity upper
  bound and kwarg-name check are suppressed respectively)
- unknown_kwarg: keyword argument key not among signature param names
  (suppressed when the signature has **kwargs)
- Method (label) leading self/cls is stripped before reconciliation;
  builtins.* callees and callees without a signature are skipped and
  counted honestly (no guessing); args element not a dict is ignored
  (CBM noise tolerance); missing 'args' key on the edge skips the edge
  (an explicit empty list [] participates: 0 args vs required > 0 is a
  genuine too_few).
"""

from __future__ import annotations

import json
import sqlite3


def _normalize(sig: str) -> str:
    """Collapse newlines so multi-line signatures parse as one line."""
    return " ".join(sig.replace("\r", " ").replace("\n", " ").split())


def split_params(sig: str) -> list[str]:
    """Top-level comma split of the inside of '(...)'.

    Nested (), [], {} do not split (e.g. 'dict[int, str]'). Outer
    parens stripped when present; a bare string without parens is
    treated as the whole param list (tolerant parse).
    """
    inner = _normalize(sig).strip()
    if inner.startswith("("):
        inner = inner[1:]
    if inner.endswith(")"):
        inner = inner[:-1]
    inner = inner.strip()
    if not inner:
        return []
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in inner:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def _split_top(s: str, sep: str) -> str:
    """Cut s at the first top-level occurrence of sep (outside brackets)."""
    depth = 0
    i = 0
    while i < len(s):
        ch = s[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and s.startswith(sep, i):
            if sep == "=" and i + 1 < len(s) and s[i + 1] == "=":
                i += 2
                continue
            return s[:i]
        i += 1
    return s


def parse_param(raw: str) -> dict:
    """One raw param text -> {name, required, kind}.

    kind: 'pos' | 'star' (*args) | 'dstar' (**kwargs).
    Order: prefixes ('*'/'**') -> default value ('=') -> type annotation
    (':') -> ts '?' suffix. required = kind 'pos' with no default.
    """
    r = _normalize(raw)
    kind = "pos"
    if r.startswith("**"):
        kind = "dstar"
        r = r[2:]
    elif r.startswith("*"):
        kind = "star"
        r = r[1:]
    has_default = False
    head = _split_top(r, "=")  # default value cut (== guarded)
    if head != r:
        has_default = True
        r = head
    head = _split_top(r, ":")  # type annotation cut
    if head != r:
        r = head
    r = r.strip()
    if r.endswith("?"):  # ts optional suffix (after the name)
        has_default = True
        r = r[:-1].strip()
    return {"name": r, "required": kind == "pos" and not has_default, "kind": kind}


def parse_signature(sig: str | None, label: str | None) -> dict | None:
    """Signature string -> reconciliation model, or None (parse fail).

    Model: {names, required, total, has_star, has_dstar}.
    Method leading self/cls stripped. Empty signature/parens = 0 params.
    """
    if not sig or not sig.strip():
        return None
    if "(" not in sig:
        return None
    params = [parse_param(x) for x in split_params(sig)]
    if label == "Method" and params and params[0]["name"] in ("self", "cls"):
        params = params[1:]
    names = [p["name"] for p in params if p["name"]]
    pos = [p for p in params if p["kind"] == "pos"]
    return {
        "names": names,
        "required": sum(1 for p in pos if p["required"]),
        "total": len(params),
        "has_star": any(p["kind"] == "star" for p in params),
        "has_dstar": any(p["kind"] == "dstar" for p in params),
    }


def reconcile_call(args: list, model: dict) -> str | None:
    """args (CALLS edge list) x signature model -> None | verdict.

    Verdicts: 'too_few' | 'too_many' | 'unknown_kwarg'.
    Non-dict args elements are ignored (CBM noise tolerance).
    """
    pos = 0
    kw_names: list[str] = []
    for a in args:
        if not isinstance(a, dict):
            continue
        k = a.get("k")
        if k:
            kw_names.append(str(k))
        else:
            pos += 1
    n = pos + len(kw_names)
    if n < model["required"]:
        return "too_few"
    if n > model["total"] and not model["has_star"]:
        return "too_many"
    if kw_names and not model["has_dstar"]:
        known = set(model["names"])
        for k in kw_names:
            if k not in known:
                return "unknown_kwarg"
    return None


def run_argcheck(con: sqlite3.Connection) -> dict:
    """Full reconciliation sweep over CALLS edges. Read-only."""
    rows = con.execute(
        "SELECT e.src_fid, e.dst_fid, e.properties_json,"
        " src.qualified_name AS src_qn, dst.qualified_name AS dst_qn,"
        " dst.signature AS sig, dst.label AS label"
        " FROM edges e"
        " JOIN symbols dst ON dst.fid = e.dst_fid"
        " LEFT JOIN symbols src ON src.fid = e.src_fid"
        " WHERE e.type = 'CALLS'"
    ).fetchall()
    counters = {
        "total_calls": len(rows),
        "builtins_skipped": 0,
        "no_signature_skipped": 0,
        "no_args_skipped": 0,
        "checked": 0,
        "ok": 0,
        "too_few": 0,
        "too_many": 0,
        "unknown_kwarg": 0,
    }
    samples: dict[str, list] = {"too_few": [], "too_many": [], "unknown_kwarg": []}
    for r in rows:
        src_qn, dst_qn, props, sig, label = r[3], r[4], r[2], r[5], r[6]
        if dst_qn.startswith("builtins."):
            counters["builtins_skipped"] += 1
            continue
        model = parse_signature(sig, label)
        if model is None:
            counters["no_signature_skipped"] += 1
            continue
        args = None
        if props:
            try:
                d = json.loads(props)
            except ValueError:
                d = None
            if isinstance(d, dict):
                args = d.get("args")
        if args is None:
            counters["no_args_skipped"] += 1
            continue
        if not isinstance(args, list):
            args = []
        counters["checked"] += 1
        verdict = reconcile_call(args, model)
        if verdict is None:
            counters["ok"] += 1
            continue
        counters[verdict] += 1
        if len(samples[verdict]) < 8:
            samples[verdict].append(
                {
                    "caller": src_qn,
                    "callee": dst_qn,
                    "signature": _normalize(sig or ""),
                    "n_args": sum(1 for a in args if isinstance(a, dict)),
                    "param_names": model["names"],
                }
            )
    return {"counters": counters, "samples": samples}


def render_text(result: dict) -> str:
    c = result["counters"]
    lines = [
        "N19 argcheck: CALLS args x callee signature reconciliation (report not gate)",
        "  total CALLS edges : %d" % c["total_calls"],
        "  builtins skipped  : %d (placeholder param_count, no signature face)" % c["builtins_skipped"],
        "  no-signature skip : %d" % c["no_signature_skipped"],
        "  no-args skip      : %d (edge without args key)" % c["no_args_skipped"],
        "  checked           : %d" % c["checked"],
        "  ok                : %d" % c["ok"],
        "  too_few           : %d (fewer args than required params)" % c["too_few"],
        "  too_many          : %d (more args than declared params)" % c["too_many"],
        "  unknown_kwarg     : %d (keyword key not in signature names)" % c["unknown_kwarg"],
    ]
    for kind in ("too_few", "too_many", "unknown_kwarg"):
        for s in result["samples"].get(kind, [])[:4]:
            lines.append(
                "  [%s] %s -> %s%s args=%d names=%s"
                % (kind, s["caller"] or "?", s["callee"], s["signature"], s["n_args"], s["param_names"])
            )
    lines.append("base = signature string (param_count is reference only); rc always 0")
    return "\n".join(lines)
