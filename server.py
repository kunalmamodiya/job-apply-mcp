#!/usr/bin/env python3
"""
job-apply-mcp  —  MCP server that automates job searching and applying
across LinkedIn, Naukri, Wellfound, Indeed India, and Hirist.

Transport: stdio  (for Claude Desktop integration)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# Ensure the project root is on sys.path so local imports work when run as a
# standalone script (e.g.  python server.py  or via Claude Desktop).
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_config
from tools.apply import apply_job, bulk_apply
from tools.profile import PROFILE
from tools.search import filter_jobs, search_jobs
from tools.session import SUPPORTED_PLATFORMS, interactive_login
from tools.tracker import get_application_summary

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,  # MCP stdio uses stdout for protocol — log to stderr
)
logger = logging.getLogger("job-apply-mcp")

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------
server = Server("job-apply-mcp")

# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="search_jobs",
        description=(
            "Search for DevOps / AI-ML / MLOps jobs across LinkedIn, Naukri, "
            "Wellfound, Indeed India, and Hirist simultaneously using browser "
            "automation.  Returns job listings ranked by relevance to the "
            "embedded candidate profile."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Search keywords. Defaults to the candidate's target "
                        "roles if omitted."
                    ),
                },
                "location": {
                    "type": "string",
                    "default": "India",
                    "description": "Location filter for the search.",
                },
                "experience_years": {
                    "type": "integer",
                    "default": 3,
                    "description": "Minimum years of experience.",
                },
                "remote": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, prefer remote positions.",
                },
                "platforms": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(SUPPORTED_PLATFORMS),
                    },
                    "description": (
                        "Which platforms to search. Defaults to all 5. "
                        "Example: [\"naukri\", \"linkedin\"]"
                    ),
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="filter_jobs",
        description=(
            "Filter and rank a list of jobs (from search_jobs) by match score "
            "against the candidate profile.  Excludes roles in the avoid list "
            "and returns the top 20."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "jobs": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Job list returned by search_jobs.",
                },
                "min_match_score": {
                    "type": "number",
                    "default": 0.7,
                    "description": "Minimum relevance score (0.0 – 1.0).",
                },
            },
            "required": ["jobs"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="apply_job",
        description=(
            "Apply to a single job via browser automation.  Handles login "
            "sessions, fills standard fields, and uploads the resume.  "
            "Detects CAPTCHAs and notifies the user."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "job_url": {
                    "type": "string",
                    "description": "Direct URL to the job posting.",
                },
                "platform": {
                    "type": "string",
                    "enum": list(SUPPORTED_PLATFORMS),
                    "description": "Which platform the job is on.",
                },
                "cover_note": {
                    "type": "string",
                    "default": "",
                    "description": "Optional cover note / message.",
                },
            },
            "required": ["job_url", "platform"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="bulk_apply",
        description=(
            "Apply to multiple jobs in sequence with rate-limiting (30–60 s "
            "delay).  Skips already-applied jobs.  Use dry_run=true to "
            "preview without actually applying."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "jobs": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Job list (from search_jobs / filter_jobs).",
                },
                "max_applications": {
                    "type": "integer",
                    "default": 10,
                    "description": "Maximum number of applications to submit.",
                },
                "dry_run": {
                    "type": "boolean",
                    "default": True,
                    "description": "Preview mode — no real applications.",
                },
            },
            "required": ["jobs"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_application_status",
        description=(
            "Retrieve tracked applications from the local database, grouped "
            "by platform and status (applied, viewed, responded, rejected)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "default": 7,
                    "description": "Look back this many days.",
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="save_session",
        description=(
            "Open a visible browser window so you can manually log in to a "
            "job platform.  The authenticated cookies are saved for future "
            "automated use."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "enum": list(SUPPORTED_PLATFORMS),
                    "description": "Platform to log in to.",
                },
            },
            "required": ["platform"],
            "additionalProperties": False,
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    logger.info("Tool called: %s  args=%s", name, json.dumps(arguments, default=str)[:500])

    try:
        if name == "search_jobs":
            result = await search_jobs(
                keywords=arguments.get("keywords"),
                location=arguments.get("location", "India"),
                experience_years=arguments.get("experience_years", 3),
                remote=arguments.get("remote", False),
                platforms=arguments.get("platforms"),
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "jobs_found": len(result),
                    "jobs": result,
                }, indent=2),
            )]

        elif name == "filter_jobs":
            result = filter_jobs(
                jobs=arguments["jobs"],
                min_match_score=arguments.get("min_match_score", 0.7),
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "filtered_count": len(result),
                    "jobs": result,
                }, indent=2),
            )]

        elif name == "apply_job":
            result = await apply_job(
                job_url=arguments["job_url"],
                platform=arguments["platform"],
                cover_note=arguments.get("cover_note", ""),
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "bulk_apply":
            result = await bulk_apply(
                jobs=arguments["jobs"],
                max_applications=arguments.get("max_applications", 10),
                dry_run=arguments.get("dry_run", True),
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_application_status":
            result = get_application_summary(
                days=arguments.get("days", 7),
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "save_session":
            result = await interactive_login(
                platform=arguments["platform"],
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        else:
            return [TextContent(
                type="text",
                text=json.dumps({"error": f"Unknown tool: {name}"}),
            )]

    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return [TextContent(
            type="text",
            text=json.dumps({"error": str(exc)}),
        )]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    logger.info("Starting job-apply-mcp server (stdio transport)")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
