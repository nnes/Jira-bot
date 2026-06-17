#!/usr/bin/env python3
"""
Fetch required Jira fields from createmeta API and auto-update:
  - JIRA_TICKET_SPEC.md  (custom field table)
  - app/config.py        (epic_name_field / epic_link_field defaults)

Usage:
    python scripts/sync_jira_fields.py <PROJECT_KEY>

Example:
    python scripts/sync_jira_fields.py EWL
"""
import asyncio
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

import httpx

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402

ISSUE_TYPES = ["Epic", "Story", "Task", "Bug"]

# ── Jira API ──────────────────────────────────────────────────────────────────

async def fetch_createmeta(project_key: str) -> dict:
    base = settings.jira_server_url.rstrip("/")
    url = (
        f"{base}/rest/api/2/issue/createmeta"
        f"?projectKeys={project_key}"
        f"&expand=projects.issuetypes.fields"
    )
    headers = {"Accept": "application/json"}
    if settings.jira_user_email:
        auth = (settings.jira_user_email, settings.jira_api_token)
        kwargs = {"auth": auth}
    else:
        headers["Authorization"] = f"Bearer {settings.jira_api_token}"
        kwargs = {}

    async with httpx.AsyncClient(timeout=30, headers=headers, **kwargs) as client:
        resp = await client.get(url)
        if resp.status_code == 401:
            print("ERROR: Jira 401 — kiểm tra PAT trong .env")
            sys.exit(1)
        if resp.status_code == 404:
            print(f"ERROR: Project '{project_key}' không tồn tại hoặc không có quyền truy cập")
            sys.exit(1)
        resp.raise_for_status()
        return resp.json()


# ── Parse ──────────────────────────────────────────────────────────────────────

def parse_fields(data: dict) -> Dict[str, dict]:
    """Return {issue_type: {field_id: {name, required}}}"""
    result: dict[str, dict] = {}
    for project in data.get("projects", []):
        for issuetype in project.get("issuetypes", []):
            itype: str = issuetype["name"]
            if itype not in ISSUE_TYPES:
                continue
            result[itype] = {
                fid: {
                    "name": info.get("name", fid),
                    "required": info.get("required", False),
                }
                for fid, info in issuetype.get("fields", {}).items()
            }
    return result


def find_special_fields(fields_by_type: Dict[str, dict]) -> Tuple[Optional[str], Optional[str]]:
    """Detect Epic Name and Epic Link custom field IDs."""
    epic_name_id: Optional[str] = None
    epic_link_id: Optional[str] = None

    for fid, info in fields_by_type.get("Epic", {}).items():
        if "epic name" in info["name"].lower():
            epic_name_id = fid

    for itype in ("Story", "Task"):
        for fid, info in fields_by_type.get(itype, {}).items():
            if "epic link" in info["name"].lower() or info["name"].lower() == "epic":
                epic_link_id = fid
                break

    return epic_name_id, epic_link_id


# ── Update JIRA_TICKET_SPEC.md ────────────────────────────────────────────────

def build_custom_field_table(fields_by_type: Dict[str, dict], server_url: str) -> str:
    """Build markdown table of all custom fields with required-for info."""
    seen: Dict[str, dict] = {}  # fid → {name, required_in: []}

    for itype, fields in fields_by_type.items():
        for fid, info in fields.items():
            if not fid.startswith("customfield_"):
                continue
            if fid not in seen:
                seen[fid] = {"name": info["name"], "required_in": []}
            if info["required"]:
                seen[fid]["required_in"].append(itype)

    domain = server_url.rstrip("/").replace("https://", "").replace("http://", "")
    lines = [
        f"## Jira Custom Field IDs ({domain})",
        "| Field Name | Custom Field ID | Required for |",
        "|------------|----------------|--------------|",
    ]
    for fid, meta in sorted(seen.items(), key=lambda x: x[1]["name"].lower()):
        req = ", ".join(meta["required_in"]) if meta["required_in"] else "—"
        lines.append(f"| {meta['name']} | `{fid}` | {req} |")

    return "\n".join(lines)


def update_spec(table: str) -> None:
    spec_path = ROOT / "JIRA_TICKET_SPEC.md"
    content = spec_path.read_text()

    # Replace existing custom field table if present
    pattern = r"## Jira Custom Field IDs.*?(?=\n## |\Z)"
    replacement = table + "\n"
    if re.search(pattern, content, re.DOTALL):
        new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
    else:
        # Append before first "## 1." section
        new_content = re.sub(r"(## 1\.)", f"{table}\n\n\\1", content, count=1)

    spec_path.write_text(new_content)
    print(f"  ✅ JIRA_TICKET_SPEC.md updated")


# ── Update config.py defaults ─────────────────────────────────────────────────

def update_config(epic_name_id: Optional[str], epic_link_id: Optional[str]) -> None:
    config_path = ROOT / "app" / "config.py"
    content = config_path.read_text()
    changed = False

    if epic_name_id:
        new_content = re.sub(
            r'(jira_epic_name_field:\s*str\s*=\s*")[^"]*(")',
            rf'\g<1>{epic_name_id}\2',
            content,
        )
        if new_content != content:
            content = new_content
            changed = True
            print(f"  ✅ config.py: jira_epic_name_field → {epic_name_id}")

    if epic_link_id:
        new_content = re.sub(
            r'(jira_epic_link_field:\s*str\s*=\s*")[^"]*(")',
            rf'\g<1>{epic_link_id}\2',
            content,
        )
        if new_content != content:
            content = new_content
            changed = True
            print(f"  ✅ config.py: jira_epic_link_field → {epic_link_id}")

    if changed:
        config_path.write_text(content)
    else:
        print("  ℹ️  config.py: field IDs already up to date")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    project_key = sys.argv[1].upper()
    print(f"\n🔍 Fetching createmeta for project: {project_key}")
    print(f"   Server: {settings.jira_server_url}\n")

    data = await fetch_createmeta(project_key)
    fields_by_type = parse_fields(data)

    if not fields_by_type:
        print("ERROR: Không tìm thấy issue type nào. Kiểm tra PROJECT_KEY.")
        sys.exit(1)

    # Print summary
    for itype, fields in fields_by_type.items():
        required = [f"{info['name']} ({fid})" for fid, info in fields.items() if info["required"]]
        print(f"  [{itype}] required fields ({len(required)}):")
        for r in required:
            print(f"    • {r}")
        print()

    epic_name_id, epic_link_id = find_special_fields(fields_by_type)
    print(f"  Epic Name field : {epic_name_id or 'not found'}")
    print(f"  Epic Link field : {epic_link_id or 'not found'}")
    print()

    # Auto-update files
    print("📝 Updating files...")
    table = build_custom_field_table(fields_by_type, settings.jira_server_url)
    update_spec(table)
    update_config(epic_name_id, epic_link_id)

    print("\n✅ Done. Restart server để áp dụng config mới.")


if __name__ == "__main__":
    asyncio.run(main())
