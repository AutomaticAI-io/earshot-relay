"""The directory listings (official MCP Registry, Glama, the Claude Code plugin) must agree with the code they describe.

These files are read by third parties, so a drift (a version nobody bumped, a name that no longer matches the image
label) fails there, long after the release. They fail here instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from earshot_relay import __version__

ROOT = Path(__file__).resolve().parent.parent


def _json(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text())


def test_server_json_matches_the_release_and_the_image_label():
    server = _json("server.json")
    assert server["version"] == __version__
    (pkg,) = [p for p in server["packages"] if p["registryType"] == "oci"]
    assert pkg["identifier"] == f"ghcr.io/automaticai-io/earshot-relay:{__version__}"
    assert pkg["transport"] == {"type": "stdio"}
    assert len(server["description"]) <= 100  # the registry rejects longer
    label = re.search(r'io\.modelcontextprotocol\.server\.name="([^"]+)"', (ROOT / "Dockerfile").read_text())
    assert label and label.group(1) == server["name"]  # case-sensitive: the registry compares it exactly


def test_server_json_never_embeds_a_credential():
    text = (ROOT / "server.json").read_text()
    for var in re.findall(r'"api_token": \{[^}]*\}', text):
        assert '"isSecret": true' in var
    assert "default" not in re.search(r'"api_token": \{[^}]*\}', text).group(0)


def test_plugin_and_marketplace_agree():
    market = _json(".claude-plugin/marketplace.json")
    (entry,) = market["plugins"]
    plugin = _json(f"{entry['source'][2:]}/.claude-plugin/plugin.json")
    assert entry["name"] == plugin["name"]  # a mismatch breaks `claude plugin install <name>@<marketplace>`
    assert plugin["version"] == __version__
    assert plugin["userConfig"]["api_token"]["sensitive"] is True
    mcp = _json(f"{entry['source'][2:]}/.mcp.json")["mcpServers"]["earshot"]
    assert mcp["type"] == "http" and mcp["url"] == "${user_config.mcp_url}"
    assert "${user_config.api_token}" in mcp["headers"]["Authorization"]


def test_glama_claim_file_lists_a_user_not_an_org():
    glama = _json("glama.json")
    assert glama["maintainers"] and all("/" not in m for m in glama["maintainers"])
