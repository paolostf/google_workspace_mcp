"""
Persistent CLI wrapper for interacting with a running Workspace MCP server.

Reuses the project's existing FileTreeStore and FernetEncryptionWrapper to
cache OAuth tokens on disk so that ``fastmcp list/call`` does not re-trigger
the full browser-based OAuth flow on every invocation.

Usage::

    uv run workspace-cli list
    uv run workspace-cli call search_gmail_messages query="is:unread" max_results=5
"""

import argparse
import asyncio
import json
import logging
import os
import re
import stat
import sys
from typing import Any

from cryptography.fernet import Fernet
from fastmcp import Client
from fastmcp.client.auth import OAuth
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from core.storage import make_sanitized_file_store

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://localhost:8000/mcp"
CLI_HOME = os.path.expanduser("~/.workspace-mcp")
TOKEN_DIR = os.path.join(CLI_HOME, "cli-tokens")
KEY_PATH = os.path.join(CLI_HOME, ".cli-encryption-key")
DEFAULT_PROFILE = "default"
PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _profile_token_dir(profile: str = DEFAULT_PROFILE) -> str:
    """Return an isolated token directory for one validated CLI profile."""
    if not isinstance(profile, str) or not PROFILE_PATTERN.fullmatch(profile):
        raise ValueError(
            "profile must be 1-64 characters using letters, numbers, dot, underscore, or hyphen"
        )
    if profile == DEFAULT_PROFILE:
        return TOKEN_DIR
    return os.path.join(TOKEN_DIR, profile)


def _get_token_storage(profile: str = DEFAULT_PROFILE) -> FernetEncryptionWrapper:
    """Return an encrypted, disk-backed token store.

    On first run the directory tree and a random Fernet key are created.
    The key file is restricted to owner-only access (0o600).
    """
    token_dir = _profile_token_dir(profile)
    os.makedirs(token_dir, exist_ok=True)

    try:
        fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(KEY_PATH, "rb") as fh:
            key = fh.read()
    else:
        key = Fernet.generate_key()
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        os.chmod(KEY_PATH, stat.S_IRUSR | stat.S_IWUSR)

    return FernetEncryptionWrapper(
        key_value=make_sanitized_file_store(token_dir),
        fernet=Fernet(key),
    )


def build_oauth(
    profile: str = DEFAULT_PROFILE,
    scopes: list[str] | None = None,
) -> OAuth:
    """Build an OAuth helper with persistent encrypted token storage."""
    storage = _get_token_storage(profile)
    return OAuth(token_storage=storage, scopes=scopes)


async def _list_tools(
    url: str,
    profile: str = DEFAULT_PROFILE,
    scopes: list[str] | None = None,
) -> None:
    """Connect, authenticate once, and print available tools."""
    try:
        auth = build_oauth(profile, scopes)
    except Exception as e:
        print(
            f"Error: failed to initialize OAuth ({type(e).__name__})", file=sys.stderr
        )
        sys.exit(1)
    try:
        async with Client(url, auth=auth) as client:
            tools = await client.list_tools()
    except Exception as e:
        print(f"Error: failed to list tools ({type(e).__name__})", file=sys.stderr)
        sys.exit(1)
    for tool in tools:
        desc = (tool.description or "").split("\n")[0]
        print(f"  {tool.name:40s} {desc}")
    print(f"\n{len(tools)} tools available")


async def _call_tool(
    url: str,
    tool_name: str,
    raw_args: list[str],
    profile: str = DEFAULT_PROFILE,
    scopes: list[str] | None = None,
) -> None:
    """Connect, authenticate once, call a single tool, and print the result."""
    kwargs: dict[str, Any] = {}
    for arg in raw_args:
        if "=" not in arg:
            print(f"Error: argument '{arg}' must be in key=value form", file=sys.stderr)
            sys.exit(1)
        k, v = arg.split("=", 1)
        try:
            kwargs[k] = json.loads(v)
        except json.JSONDecodeError:
            kwargs[k] = v

    async with Client(url, auth=build_oauth(profile, scopes)) as client:
        result = await client.call_tool(tool_name, kwargs)
        for block in result.content:
            if hasattr(block, "text"):
                try:
                    parsed = json.loads(block.text)
                    print(json.dumps(parsed, indent=2))
                except (json.JSONDecodeError, TypeError):
                    print(block.text)
            else:
                print(block)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="workspace-cli",
        description="CLI for Workspace MCP with persistent OAuth token caching",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("WORKSPACE_MCP_URL", DEFAULT_URL),
        help=f"MCP server URL (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        help=(
            "Isolated encrypted OAuth profile (default: default). Use one profile "
            "per Google account when connecting to the same remote MCP URL."
        ),
    )
    parser.add_argument(
        "--scopes",
        help=(
            "Comma-separated OAuth scopes. Omit to accept the server defaults; "
            "set this for least-privilege task profiles."
        ),
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List available tools")

    call_parser = sub.add_parser("call", help="Call a tool")
    call_parser.add_argument("tool", help="Tool name")
    call_parser.add_argument("args", nargs="*", help="key=value arguments")

    args = parser.parse_args()

    try:
        _profile_token_dir(args.profile)
    except ValueError as exc:
        parser.error(str(exc))
    scopes = None
    if args.scopes is not None:
        scopes = [scope.strip() for scope in args.scopes.split(",") if scope.strip()]
        if not scopes:
            parser.error("--scopes requires at least one non-empty scope")

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "list":
        asyncio.run(_list_tools(args.url, args.profile, scopes))
    elif args.command == "call":
        asyncio.run(_call_tool(args.url, args.tool, args.args, args.profile, scopes))


if __name__ == "__main__":
    main()
