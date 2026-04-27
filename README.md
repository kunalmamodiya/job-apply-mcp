# job-apply-mcp

> **Auto-apply to jobs across 8 Indian job portals using AI-powered form filling.**

A Model Context Protocol (MCP) server that searches, filters, and applies to jobs across major Indian job portals — LinkedIn, Naukri, Wellfound, Indeed, Hirist, Glassdoor, Instahyre, and Cutshort. It uses Playwright to drive a real browser, fills application forms automatically using your config, and tracks every application in a local database.

Originally built for a **DevOps / MLOps** profile, but works for **any role** by editing the config.

---

## Features

- **8 platforms** supported: LinkedIn, Naukri, Wellfound, Indeed, Hirist, Glassdoor, Instahyre, Cutshort
- **Easy-Apply only filter** — skips jobs requiring external application sites
- **Smart form auto-fill** — answers chatbot questions automatically (CTC, experience, notice period, location, gender, etc.)
- **Resume upload** — handles visible, hidden, and file-chooser uploads
- **Match scoring** — ranks jobs by skill / role / location relevance
- **Date filter** — skips jobs older than 30 days
- **Pagination** — fetches up to 100 jobs per keyword (5 pages)
- **Application tracking** — SQLite DB prevents duplicate applications
- **Rate limiting** — 5–15 second delay between applies
- **Persistent sessions** — saved login cookies + browser profiles
- **Two ways to use** — interactive CLI (`run.py`) or via Claude Desktop / Kiro IDE (MCP)

---

## Quick Start (5 Minutes)

### 1. Install Python 3.11+
```bash
python3 --version  # must be 3.11 or higher
```

### 2. Clone the repo
```bash
git clone https://github.com/pulkit017/job-apply-mcp.git
cd job-apply-mcp
```

### 3. Install dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate          # macOS/Linux
# .venv\Scripts\activate           # Windows PowerShell

pip install mcp playwright
python -m playwright install firefox
```

### 4. Set up your profile
The config file lives at `~/.job-apply-mcp/config.json`. It will auto-create on first run, but you can create it manually:

```bash
mkdir -p ~/.job-apply-mcp
nano ~/.job-apply-mcp/config.json
```

Paste this template and **fill in your details**:

```json
{
  "resume_path": "/full/path/to/your_resume.pdf",
  "name": "Your Full Name",
  "email": "you@example.com",
  "phone": "+91-XXXXXXXXXX",
  "location": "India",
  "experience_years": 3,
  "credentials": {
    "linkedin": { "email": "", "password": "" },
    "naukri":   { "email": "", "password": "" },
    "wellfound":{ "email": "", "password": "" },
    "indeed":   { "email": "", "password": "" },
    "hirist":   { "email": "", "password": "" }
  },
  "autofill": {
    "gender": "Male",
    "date_of_birth": "DD/MM/YYYY",
    "preferred_locations": ["Remote", "Bangalore", "Pune", "Hyderabad"],
    "experience": {
      "devops": "3.9",
      "kubernetes": "3",
      "docker": "3.5",
      "aws": "1",
      "azure": "3",
      "gcp": "1.5",
      "terraform": "2",
      "python": "3"
    },
    "notice_period": "15 days",
    "current_ctc": "9",
    "expected_ctc": "16",
    "total_experience": "3.9",
    "primary_cloud": "GCP",
    "last_working_day": "Currently Working",
    "contract_based": "Yes"
  }
}
```

> **Note:** Credentials are optional. The recommended approach is to log in manually via `save_session` (browser opens, you log in once, cookies saved).

### 5. Save your session for at least one platform
```bash
python -c "
import asyncio, sys; sys.path.insert(0,'.')
from tools.session import interactive_login
print(asyncio.run(interactive_login('naukri')))
"
```

A Firefox window opens — log in (email/password or OTP). Cookies save automatically.

### 6. Run!

**Interactive CLI** (easiest for first-time users):
```bash
python run.py
```

You'll see a menu:
```
  1. Login to a platform (save session)
  2. Search & Apply for jobs
  3. View application status
  4. Search only (no apply)
  5. Exit
```

---

## Customising for Your Job Type

This MCP defaults to DevOps / MLOps roles. To use it for **any other role**, edit two things:

### A. Update your config keywords
In your `~/.job-apply-mcp/config.json`, the `experience` map should list the technologies you know. The chatbot autofill matches these against questions like *"How many years of experience in Python?"*

### B. Update the candidate profile (optional)
For better job matching, edit `tools/profile.py`:
- `skills` — your full skill list
- `target_roles` — job titles you want
- `default_search_keywords` — what to search for by default
- `avoid_keywords` — roles to exclude (e.g., frontend, mobile)

Example for a **Frontend Developer** profile:
```python
skills = ("React", "TypeScript", "Next.js", "Tailwind", "GraphQL", ...)
target_roles = ("Frontend Engineer", "React Developer", "UI Engineer", ...)
default_search_keywords = ("React Developer", "Frontend Engineer", ...)
avoid_keywords = ("backend", "devops", "ml", "data science", ...)
```

---

## Using with Claude Desktop or Kiro IDE

This is also an **MCP server**, so you can talk to it through Claude Desktop or Kiro IDE.

### Claude Desktop
Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):
```json
{
  "mcpServers": {
    "job-apply-mcp": {
      "command": "/full/path/to/job-apply-mcp/.venv/bin/python",
      "args": ["/full/path/to/job-apply-mcp/server.py"]
    }
  }
}
```

### Kiro IDE
Edit `~/.kiro/settings/mcp.json`:
```json
{
  "mcpServers": {
    "job-apply-mcp": {
      "command": "/full/path/to/job-apply-mcp/.venv/bin/python",
      "args": ["/full/path/to/job-apply-mcp/server.py"],
      "disabled": false
    }
  }
}
```

Then in chat: *"Search Naukri for DevOps jobs and apply to the top 10"*

---

## Available Tools

| Tool | Description |
|------|-------------|
| `search_jobs` | Search across one or more platforms with keywords + filters |
| `filter_jobs` | Rank jobs by match score, skip jobs older than 30 days |
| `apply_job` | Apply to a single job URL |
| `bulk_apply` | Apply to many jobs with rate limiting + dedup |
| `get_application_status` | Show recent applications grouped by platform & status |
| `save_session` | Open browser to log in manually, save cookies |

---

## How Form Filling Works

When a job has a chatbot/questionnaire, the bot reads the question and matches it against your config:

| Question pattern | Auto-answer |
|------------------|-------------|
| "Current CTC", "Current salary" | `current_ctc` |
| "Expected CTC", "Expected salary" | `expected_ctc` |
| "How many years in Python/AWS/Docker..." | `experience.<tech>` |
| "Notice period", "When can you join" | `notice_period` |
| "Primary cloud", "Preferred cloud" | `primary_cloud` |
| "Last working day", "LWD" | `last_working_day` |
| "Contract", "C2H", "Contractual" | `contract_based` |
| "Gender" | `gender` |
| "Comfortable for face-to-face / onsite / relocate" | `Yes` |
| "Date of birth" | `date_of_birth` |
| "Resume / CV / upload" | uploads `resume_path` |

If a question doesn't match any pattern, it picks a sensible default (e.g., "Skip" or "Yes" depending on context).

---

## File Structure

```
job-apply-mcp/
├── server.py              # MCP server entry point
├── run.py                 # Interactive CLI runner
├── config.py              # Config loader
├── requirements.txt
├── README.md
└── tools/
    ├── profile.py         # Candidate profile + match scoring
    ├── search.py          # Per-platform job search + pagination
    ├── apply.py           # Apply automation + form filling
    ├── session.py         # Login session management
    └── tracker.py         # SQLite application history
```

---

## Data & Privacy

All your data stays local. **Nothing is sent anywhere except the job portals you're applying to.**

| Path | What it contains |
|------|------------------|
| `~/.job-apply-mcp/config.json` | Your name, email, resume path, autofill answers |
| `~/.job-apply-mcp/sessions/*.json` | Saved login cookies per platform |
| `~/.job-apply-mcp/browser-profiles/` | Persistent browser profile (LinkedIn) |
| `~/.job-apply-mcp/applications.db` | SQLite log of every application |

**These are NOT in this git repo and will NEVER be pushed.**

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Naukri OK status=200` then `SSL_ERROR_UNKNOWN` later | Network instability — try a different network or mobile hotspot |
| `No direct apply button found` | Job is "Apply on company site" — these are intentionally skipped |
| `CAPTCHA detected` | Run `save_session` to log in manually first |
| Resume upload fails | Check `resume_path` in config is an absolute path that exists |
| All jobs show "Already applied" | The DB has your history — to test fresh, delete `~/.job-apply-mcp/applications.db` |
| Naukri rate-limits ("error processing your request") | Slow down — wait 24 hours, you've done too many in a short time |
| LinkedIn search returns 0 jobs | Re-run `save_session linkedin` — sessions expire after a few days |

---

## Contributing

PRs welcome! Common improvements:
- Add new platforms (Foundit, Shine, etc.)
- Improve form-fill heuristics for specific job sites
- Add support for cover-letter generation
- Improve match scoring algorithm

---

## Disclaimer

- This tool **automates form submission**. Always **review the jobs being applied to** before bulk-applying.
- Excessive use may trigger rate limits or temporary account locks on job portals.
- Use responsibly — don't spam recruiters with low-quality applications.
- This is for **personal job search use only**. Don't use it commercially or to harvest job data.
