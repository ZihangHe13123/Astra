"""Recognize direct desktop input in code submitted to host execution tools.

This is a content classifier for inline commands, not a process sandbox.
"""

import ast
import re
import shlex
from pathlib import PurePath

_SYSTEM_EVENTS = re.compile(r"""(?:["']System Events["']|["']com\.apple\.systemevents["'])""", re.I)
_APPLESCRIPT_INPUT = re.compile(
    r"\b(?:click|keystroke|key\s+code|perform\s+action|drag|scroll)\b|"
    r"\bset\s+(?:the\s+)?(?:value|focused|frontmost|position|selected|size)\s+of\b", re.I)
_JXA_INPUT = re.compile(r"\.\s*(?:click|keystroke|keyCode|perform|drag|scroll)\s*\(|"
                        r"\.\s*(?:value|focused|frontmost|position|selected|size)\s*=(?!=)", re.I)
_NATIVE_INPUT = re.compile(r"\b(?:CGEventPost(?:ToPid|ToPSN)?|AXUIElementPerformAction|"
                           r"AXUIElementSetAttributeValue|AXUIElementSetMultipleAttributeValues)\s*\(")
_AUTOGUI_ACTIONS = frozenset({
    "click", "doubleClick", "tripleClick", "rightClick", "leftClick", "middleClick", "mouseDown",
    "mouseUp", "move", "moveTo", "moveRel", "drag", "dragTo", "dragRel", "scroll", "hscroll", "vscroll",
    "press", "keyDown", "keyUp", "write", "typewrite", "hotkey", "hold",
})


def _automation(source: str) -> str | None:
    # Quoted control names can contain verbs. Examine the statements for actions
    # and retain the original source only to identify the System Events target.
    statements = re.sub(r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""", '""', source)
    statements = re.sub(r"(?:--|//)[^\n]*", "", statements)
    has_application = re.search(r"\b(?:tell\s+(?:the\s+)?app(?:lication)?\b|Application\s*\()", statements, re.I)
    if has_application and re.search(r"(?:\bto\s+|^[ \t]*|\.\s*)activate\b", statements, re.I | re.M):
        return "将应用调到前台"
    if _SYSTEM_EVENTS.search(source) and (_APPLESCRIPT_INPUT.search(statements) or _JXA_INPUT.search(statements)):
        return "通过系统事件操作界面"
    if re.search(r"\bNSRunningApplication\b", statements) and re.search(r"\bactivate(?:WithOptions_?)?\s*\(", statements):
        return "将应用调到前台"
    if _NATIVE_INPUT.search(statements) or (re.search(r"\bCGEvent\b", statements)
                                            and re.search(r"\.\s*post(?:ToPid)?\s*\(", statements)):
        return "通过系统接口发送输入或修改界面控件"
    return None


def inline_python_action(source: str) -> str | None:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return _automation(source)
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"

    def name(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(aliases.get(node.id, node.id))
        return ".".join(reversed(parts))

    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        target = name(call.func)
        base, _, method = target.rpartition(".")
        if base == "pyautogui" and method in _AUTOGUI_ACTIONS:
            return "使用 PyAutoGUI 发送键盘或鼠标输入"
        if _NATIVE_INPUT.search(method + "(") or (method in {"post", "postToPid"} and "CGEvent" in source):
            return "通过系统接口发送输入或修改界面控件"
        if method in {"activate", "activateWithOptions", "activateWithOptions_"} and "NSRunningApplication" in source:
            return "将应用调到前台"
        if target in {"os.system", "os.popen", "subprocess.run", "subprocess.call", "subprocess.Popen",
                      "subprocess.check_call", "subprocess.check_output"} and call.args:
            arg = call.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                result = inline_shell_action(arg.value, nested=True)
            elif isinstance(arg, (ast.List, ast.Tuple)):
                literals = [item.value if isinstance(item, ast.Constant) and isinstance(item.value, str) else ""
                            for item in arg.elts]
                result = _command(literals, "", nested=True)
            else:
                result = None
            if result:
                return result
    return None


def _command(tokens: list[str], source: str, *, nested: bool) -> str | None:
    while tokens and (tokens[0] in {"env", "/usr/bin/env", "command", "exec", "sudo"}
                      or re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", tokens[0])):
        tokens = tokens[1:]
    if not tokens:
        return None
    program = PurePath(tokens[0]).name
    args = tokens[1:]
    if program == "cliclick":
        if not args or not all(arg in {"-h", "--help", "-V", "--version", "p", "p:.", "w:0"}
                               or arg.startswith(("p:", "cp:", "w:")) for arg in args):
            return "模拟键盘或鼠标输入"
    if program in {"xdotool", "ydotool"} and any(arg in {
        "key", "type", "click", "keydown", "keyup", "mousedown", "mouseup", "mousemove", "windowactivate"
    } for arg in args):
        return "模拟键盘或鼠标输入"
    if program == "osascript":
        fragments = [arg[2:] or (args[i+1] if i+1 < len(args) else "")
                     for i, arg in enumerate(args) if arg.startswith("-e")]
        return _automation("\n".join(fragments) if fragments else source)
    if program in {"swift", "node", "jsc", "ruby"}:
        return _automation(" ".join(args) if args and "<<" not in source else source)
    if re.fullmatch(r"python(?:w|\d+(?:\.\d+)*)?", program):
        if "-c" in args and args.index("-c")+1 < len(args):
            code = args[args.index("-c")+1]
        elif "<<" in source and "\n" in source:
            code = "\n".join(source.splitlines()[1:-1])
        else:
            return None
        # Nested Python wrappers are outside the inline shell walk's recursion budget.
        return _automation(code) if nested else inline_python_action(code)
    if not nested and program in {"sh", "bash", "zsh"}:
        for i, arg in enumerate(args[:-1]):
            if arg.startswith("-") and "c" in arg:
                return inline_shell_action(args[i+1], nested=True)
    return None


def inline_shell_action(source: str, *, nested: bool = False) -> str | None:
    lex = shlex.shlex(source, posix=True, punctuation_chars=";&|()")
    lex.whitespace = " \t\r"
    # Split shell command lines while preserving newlines *inside* quoted inline code.
    lex.whitespace_split = False
    lex.wordchars += ".-/:$@=+,%[]<>*?!~"
    group: list[str] = []
    try:
        for token in lex:
            if token == "\n" or all(ch in ";&|()" for ch in token):
                if action := _command(group, source, nested=nested):
                    return action
                group.clear()
            else:
                group.append(token)
    except ValueError:
        return _automation(source)
    return _command(group, source, nested=nested)
