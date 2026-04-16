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

### Prerequisites

- Python 3.10+
- Git
- [Microsoft Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe) (Windows only — install this first)

### 1. Clone the repo

```bash
git clone https://github.com/pulkit017/job-apply-mcp.git
cd job-apply-mcp
```

### 2. Create a virtual environment

**Windows (PowerShell):**
```powershell
python -m venv .venv
```

> If you get a script execution error when activating, run this first:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

### 3. Install dependencies

Use the venv's pip directly (no activation needed):

**Windows:**
```powershell
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\playwright install
```

**macOS/Linux:**
```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install
```

### 4. Configure your profile

Edit `config.py` and update the `DEFAULT_CONFIG` with your details:

```python
DEFAULT_CONFIG = {
    "resume_path": "C:\\path\\to\\YourResume.pdf",  # Windows
    # "resume_path": "/path/to/YourResume.pdf",     # macOS/Linux
    "name": "Your Name",
    "email": "you@example.com",
    "phone": "+91-XXXXXXXXXX",
    "location": "Your City, India",
    "experience_years": 3,
    ...
}
```

> The config is also auto-saved to `~/.job-apply-mcp/config.json` on first run.

### 5. Connect to Kiro (MCP)

Create `.kiro/settings/mcp.json` in the project root:

**Windows:**
```json
{
  "mcpServers": {
    "job-apply-mcp": {
      "command": "C:\\path\\to\\job-apply-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\job-apply-mcp\\server.py"],
      "disabled": false,
      "autoApprove": ["search_jobs", "filter_jobs", "get_application_status"]
    }
  }
}
```

**macOS/Linux:**
```json
{
  "mcpServers": {
    "job-apply-mcp": {
      "command": "/path/to/job-apply-mcp/.venv/bin/python",
      "args": ["/path/to/job-apply-mcp/server.py"],
      "disabled": false,
      "autoApprove": ["search_jobs", "filter_jobs", "get_application_status"]
    }
  }
}
```

> Replace `C:\\path\\to\\` with the actual absolute path on your machine.

Then open Kiro, go to the MCP Server view in the panel, and click **Reconnect**.

### 6. Save browser sessions

Before applying, log in to each platform so the server can reuse your session cookies:

```
Use the save_session tool with platform = "linkedin"
```

This opens a visible browser — log in manually, then the session is saved to `~/.job-apply-mcp/sessions/<platform>.json`.

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
