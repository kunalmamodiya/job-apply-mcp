# job-apply-mcp

> **Auto-apply to Naukri.com jobs with AI-powered form filling.**

A Model Context Protocol (MCP) server that searches, filters, and applies to easy-apply jobs on **Naukri.com**. It drives a real browser via Playwright, fills application forms automatically using your config, and tracks every application in a local database so you never apply to the same job twice.

> **Note:** This tool is Naukri-only. It does not support LinkedIn, Indeed, or other portals.

---

## What It Does

- Searches Naukri for jobs matching your keywords (paginated, fetches up to 100 jobs per keyword)
- **Filters out external-apply jobs** — only applies to Naukri's direct "Easy Apply" listings
- Skips jobs older than 30 days
- Ranks jobs by skill / role / location match
- Auto-fills Naukri's chatbot questions: CTC, experience, notice period, location, gender, primary cloud, contract preference, etc.
- Uploads your resume when prompted
- Tracks every application in a SQLite database (no duplicate applies)
- Rate-limits with random 5-15 second delays between applies
- Two interfaces: **interactive CLI** or **MCP server** (Claude Desktop, Kiro IDE)

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

The config file lives at `~/.job-apply-mcp/config.json`. It auto-creates on first run, but you can pre-create it:

```bash
mkdir -p ~/.job-apply-mcp
nano ~/.job-apply-mcp/config.json   # or any editor
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
    "naukri": { "email": "", "password": "" }
  },
  "autofill": {
    "gender": "Male",
    "date_of_birth": "DD/MM/YYYY",
    "preferred_locations": ["Remote", "Bangalore", "Pune", "Hyderabad", "Delhi"],
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

> **Note:** Naukri credentials are optional. The recommended way is to log in manually once via `save_session` (browser opens, you log in via OTP, cookies are saved and reused).

### 5. Save your Naukri session

```bash
python -c "
import asyncio, sys; sys.path.insert(0,'.')
from tools.session import interactive_login
print(asyncio.run(interactive_login('naukri')))
"
```

A Firefox window opens — log in with email/OTP. Cookies save automatically to `~/.job-apply-mcp/sessions/naukri.json`.

### 6. Run!

**Interactive CLI** (recommended for first-time users):

```bash
python run.py
```

You'll see:

```
  1. Login to a platform (save session)
  2. Search & Apply for jobs
  3. View application status
  4. Search only (no apply)
  5. Exit
```

---

## Customising for Your Job Type

This tool defaults to **DevOps / MLOps / Cloud roles**. To use it for any other field:

### A. Update keywords in your config

The `experience` map should list the technologies *you* know. The chatbot autofill matches these against questions like *"How many years of experience in Python?"*.

### B. Update the candidate profile (optional but recommended)

Edit `tools/profile.py`:

- `skills` — your full skill list
- `target_roles` — job titles you want
- `default_search_keywords` — what to search for by default
- `avoid_keywords` — roles to exclude

Example for a **Frontend Developer** profile:

```python
skills = ("React", "TypeScript", "Next.js", "Tailwind", "GraphQL", ...)
target_roles = ("Frontend Engineer", "React Developer", "UI Engineer", ...)
default_search_keywords = ("React Developer", "Frontend Engineer", ...)
avoid_keywords = ("backend", "devops", "ml", "data science", ...)
```

---

## Using with Claude Desktop or Kiro IDE (Optional)

This is also an **MCP server**, so you can chat with it.

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

| Tool | What it does |
|------|------|
| `search_jobs` | Search Naukri with keywords + filters (location, experience, remote) |
| `filter_jobs` | Rank jobs by match score, skip jobs older than 30 days |
| `apply_job` | Apply to a single job URL |
| `bulk_apply` | Apply to many jobs with rate-limiting + dedup |
| `get_application_status` | Show recent applications grouped by status |
| `save_session` | Open browser to log in to Naukri manually, save cookies |

---

## How Form Filling Works

When a Naukri job has a chatbot/questionnaire, the bot reads each question and matches it against your config:

| Question pattern | Auto-answer |
|------------------|-------------|
| "Current CTC", "Current salary" | `current_ctc` |
| "Expected CTC", "Expected salary" | `expected_ctc` |
| "How many years in Python/AWS/Docker..." | `experience.<tech>` (closest match) |
| "Notice period", "When can you join" | `notice_period` (e.g., "15 days") |
| "Primary cloud", "Preferred cloud" | `primary_cloud` (e.g., "GCP") |
| "Last working day", "LWD" | `last_working_day` (e.g., "Currently Working") |
| "Contract", "C2H", "Contractual" | `contract_based` (e.g., "Yes") |
| "Gender" | `gender` |
| "Comfortable for face-to-face / onsite / relocate" | `Yes` |
| "Date of birth" | `date_of_birth` |
| "Resume / CV / upload" | uploads `resume_path` |

If a question doesn't match any pattern, it picks "Skip this question" or the first sensible option.

---

## File Structure

```
job-apply-mcp/
├── server.py              # MCP server entry point (stdio transport)
├── run.py                 # Interactive CLI runner
├── config.py              # Config loader
├── requirements.txt
├── README.md
└── tools/
    ├── profile.py         # Candidate profile + match scoring
    ├── search.py          # Naukri job search via internal API
    ├── apply.py           # Apply automation + chatbot form filler
    ├── session.py         # Login session + cookie management
    └── tracker.py         # SQLite application history
```

---

## Data & Privacy

All your data stays local. Nothing is sent anywhere except Naukri's servers (when applying).

| Path | Contents |
|------|------|
| `~/.job-apply-mcp/config.json` | Your name, email, resume path, autofill answers |
| `~/.job-apply-mcp/sessions/naukri.json` | Saved login cookies |
| `~/.job-apply-mcp/applications.db` | SQLite log of every application |

**These are NOT in this git repo and will NEVER be pushed.**

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `SSL_ERROR_UNKNOWN` when reaching Naukri | Network blocking — switch to mobile hotspot or different WiFi |
| `No direct apply button found` | Job uses "Apply on company site" — these are intentionally skipped |
| `CAPTCHA detected` | Run `save_session` to log in manually |
| Resume upload fails | Check `resume_path` in config — must be an absolute path that exists |
| All jobs show "Already applied" | The DB has your history. To reset: `rm ~/.job-apply-mcp/applications.db` |
| Naukri rate-limits ("error processing your request") | Wait 24 hours. Don't apply to 100+ jobs in one session |
| Session expired | Run `save_session` again — Naukri sessions last ~30 days |

---

## Disclaimer

- This tool **automates form submission**. Always **review the jobs being applied to** before bulk-applying.
- Excessive use may trigger rate limits or temporary account locks on Naukri.
- Use responsibly — don't spam recruiters with low-quality applications.
- This is for **personal job search use only**. Don't use it commercially or to harvest job data.
- Applications you submit are **real** — recruiters will contact you. Make sure your config and resume are accurate.

---

## Contributing

PRs welcome! Common improvements:
- Better form-fill heuristics for unusual chatbot questions
- Cover-letter generation
- Better match-scoring algorithm
- Support for new search filters
