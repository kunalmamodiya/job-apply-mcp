# job-apply-mcp

MCP (Model Context Protocol) server that automates job searching and applying across **5 platforms** for a DevOps & AI/ML Engineer profile:

| Platform | URL |
|----------|-----|
| LinkedIn | in.linkedin.com/jobs |
| Naukri.com | naukri.com |
| Wellfound | wellfound.com |
| Indeed India | in.indeed.com |
| Hirist | hirist.tech |

## Setup

### 1. Install dependencies

```bash
cd job-apply-mcp
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure your profile

Create (or edit) `~/.job-apply-mcp/config.json`:

```json
{
  "resume_path": "/absolute/path/to/your/resume.pdf",
  "name": "Pulkit Jain",
  "email": "you@example.com",
  "phone": "+91-XXXXXXXXXX",
  "location": "Jaipur, India",
  "experience_years": 3,
  "credentials": {
    "linkedin": { "email": "", "password": "" },
    "naukri":   { "email": "", "password": "" },
    "wellfound":{ "email": "", "password": "" },
    "indeed":   { "email": "", "password": "" },
    "hirist":   { "email": "", "password": "" }
  }
}
```

> The config file is auto-created with empty defaults the first time the server runs.

### 3. Save browser sessions

Before applying, log in to each platform so the server can reuse your session cookies:

```
Use the save_session tool with platform = "linkedin"
```

This opens a visible browser — log in manually, then the session is saved to `~/.job-apply-mcp/sessions/<platform>.json`.

## Claude Desktop Integration

Add the following to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "job-apply-mcp": {
      "command": "python",
      "args": ["/full/path/to/job-apply-mcp/server.py"],
      "env": {}
    }
  }
}
```

> Replace `/full/path/to/` with the actual path on your machine.

## Tools

### search_jobs

Search all 5 platforms concurrently.

**Input:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| keywords | string[] | (candidate target roles) | Search terms |
| location | string | "India" | Location filter |
| experience_years | int | 3 | Min. years of experience |
| remote | bool | false | Prefer remote jobs |

**Example prompt:**
> Search for MLOps and DevOps jobs in Bangalore, prefer remote

### filter_jobs

Rank and filter results by match score.

**Input:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| jobs | object[] | (required) | Output from search_jobs |
| min_match_score | float | 0.7 | Minimum relevance (0–1) |

### apply_job

Apply to a single job posting.

**Input:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| job_url | string | (required) | Job posting URL |
| platform | string | (required) | linkedin / naukri / wellfound / indeed / hirist |
| cover_note | string | "" | Optional cover note |

### bulk_apply

Apply to multiple jobs with rate-limiting.

**Input:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| jobs | object[] | (required) | Job list |
| max_applications | int | 10 | Max applications |
| dry_run | bool | true | Preview mode (no real applies) |

### get_application_status

View tracked applications from the local database.

**Input:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| days | int | 7 | Look-back window |

### save_session

Open a browser to log in manually. Cookies are saved for future automation.

**Input:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| platform | string | (required) | Platform name |

## How It Works

1. **Search** — Playwright launches headless Chromium, opens all 5 job sites in parallel, scrapes listings.
2. **Match** — Each job is scored (0–1) against the embedded candidate profile using title similarity, skill overlap, and location matching.
3. **Filter** — Jobs below the threshold or in excluded categories (frontend, mobile, pure data science) are removed.
4. **Apply** — Playwright navigates to the job page, clicks Apply, fills fields, uploads the resume. A 30–60 second random delay between applications avoids bot detection.
5. **Track** — Every application is logged in a local SQLite database at `~/.job-apply-mcp/applications.db`.

## CAPTCHA Handling

If a CAPTCHA is detected during search or apply, the tool pauses and returns a message asking you to run `save_session` to log in manually. It never tries to solve CAPTCHAs automatically.

## File Structure

```
job-apply-mcp/
  server.py          # MCP server entry point (stdio transport)
  config.py          # Config loader (~/.job-apply-mcp/config.json)
  requirements.txt
  README.md
  tools/
    __init__.py
    profile.py       # Candidate profile & relevance scoring
    search.py        # search_jobs / filter_jobs (Playwright scrapers)
    apply.py         # apply_job / bulk_apply (Playwright automation)
    session.py       # save_session / cookie management
    tracker.py       # SQLite application tracking
```

## Data Storage

| Path | Purpose |
|------|---------|
| `~/.job-apply-mcp/config.json` | User config (resume, contact, credentials) |
| `~/.job-apply-mcp/sessions/*.json` | Saved browser cookies per platform |
| `~/.job-apply-mcp/applications.db` | SQLite database of all applications |
