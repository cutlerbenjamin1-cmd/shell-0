"""Interactive console driver for shell-0.

Launches the real server over MCP stdio and lets you drive its tools by hand:
pick a tool, enter arguments, see the parsed result. This uses the same live
stdio path (stdio_driver) as the integration tests, so what you see here is
exactly what an MCP client sees.

Usage:
    python manual.py [--audit-tmp]

Commands at the prompt:
    <tool> | <number>      select a tool (numbers come from `list`)
    <json> | key=value...  arguments for the selected tool, then Enter
    list                   list tools again
    schema [tool]          show a tool's input schema
    back                   deselect the current tool
    help                   show this help
    quit | exit            leave (Ctrl-D / Ctrl-C also work)

Argument examples (once a tool is selected):
    {"action": "read", "path": "README.md"}
    action=list path=.
    action=grep path=. pattern=def regex=true
"""
import argparse
import asyncio
import json
import shutil
import tempfile
import textwrap

from stdio_driver import call, connect

HELP = __doc__.split("Commands at the prompt:")[-1]


def _print(payload):
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _wrap(text, indent="  "):
    width = max(shutil.get_terminal_size((100, 24)).columns - 2, 40)
    return textwrap.fill(text, width=width,
                         initial_indent=indent, subsequent_indent=indent)


def _parse_args_line(line):
    """A JSON object, or space-separated key=value pairs (values JSON-parsed when
    possible, else kept as strings)."""
    line = line.strip()
    if not line:
        return {}
    if line.startswith("{"):
        return json.loads(line)
    args = {}
    for tok in line.split():
        if "=" not in tok:
            raise ValueError(f"expected key=value, got {tok!r}")
        k, v = tok.split("=", 1)
        try:
            args[k] = json.loads(v)
        except json.JSONDecodeError:
            args[k] = v
    return args


async def _ainput(prompt):
    return await asyncio.to_thread(input, prompt)


async def repl(session):
    tools = (await session.list_tools()).tools
    by_name = {t.name: t for t in tools}

    def show_tools():
        print("\nTools:")
        for i, t in enumerate(tools, 1):
            print(f"  {i}. {t.name}")
        print("\nSelect a tool by number or name to see its full description.\n")

    show_tools()
    selected = None
    while True:
        prompt = f"shell-0[{selected}]> " if selected else "shell-0> "
        try:
            line = (await _ainput(prompt)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not line:
            continue
        low = line.lower()
        if low in ("quit", "exit"):
            return
        if low == "help":
            print(HELP)
            continue
        if low == "list":
            show_tools()
            continue
        if low == "back":
            selected = None
            continue
        if low.startswith("schema"):
            parts = line.split(maxsplit=1)
            name = parts[1].strip() if len(parts) > 1 else selected
            tool = by_name.get(name)
            print(f"unknown tool: {name}" if not tool else "")
            if tool:
                _print(tool.inputSchema)
            continue

        # tool selection by name or number
        target = None
        if line in by_name:
            target = line
        elif line.isdigit() and 1 <= int(line) <= len(tools):
            target = tools[int(line) - 1].name
        if target:
            selected = target
            tool = by_name[target]
            schema = tool.inputSchema
            props = ", ".join(schema.get("properties", {}).keys())
            req = ", ".join(schema.get("required", []))
            print(f"\n{target}")
            desc = (tool.description or "").strip()
            if desc:
                print(_wrap(desc))
            print()
            if props:
                print(f"properties: {props}")
            if req:
                print(f"required: {req}")
            print("enter args as JSON or key=value pairs, then Enter (`schema` for detail).")
            continue

        # otherwise: arguments for the selected tool
        if not selected:
            print("no tool selected. type a tool name/number, or `list`.")
            continue
        try:
            args = _parse_args_line(line)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"bad arguments: {exc}")
            continue
        try:
            result = await call(session, selected, args)
        except Exception as exc:
            print(f"call failed: {exc}")
            continue
        _print(result)


async def main():
    ap = argparse.ArgumentParser(description="Interactive console driver for shell-0.")
    ap.add_argument("--audit-tmp", action="store_true",
                    help="redirect the server's forensic audit to a temp dir (keeps ./data clean)")
    opts = ap.parse_args()

    env = None
    if opts.audit_tmp:
        d = tempfile.mkdtemp(prefix="shell0-manual-")
        env = {"FS_AUDIT_ROOT": f"{d}/fs", "EXEC_AUDIT_ROOT": f"{d}/exec"}
        print(f"audit -> {d}")

    print("Launching shell-0 over stdio...  (`quit` or Ctrl-D to exit)")
    async with connect(env=env) as session:
        await repl(session)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
