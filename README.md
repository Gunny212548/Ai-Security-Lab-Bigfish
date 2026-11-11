# Bigfish AI Security Training

A hands-on **prompt injection** CTF for a 2-hour workshop (beginners, teams of 3–4). Players attack
**BigfishDesk**, the AI IT helpdesk of the fictional *Siam Bigfish Corp*, find flags `bigfish{...}` and submit
them to the built-in scoreboard. The build spec is in [`SPEC.md`](SPEC.md). Status: **M1–M7 done**.

| Track | Challenges | What players do |
|---|---|---|
| 🎯 Direct | D5 The CEO's Ticket (200), D6 Resetting Someone Else's Password (200), D7 The Runaway Bill (150), D8 Talk to the Database (300, SQL), D9 Support Override (400, prompt leak → cross-DB, hardest) | Chat with the bot and talk it into misusing its tools |
| 🕵️ Indirect | I1 Poisoned KB (150), I2 Jumping the Queue (200), I3 Invisible Ink (200), I4 Trojan Plugin (250) | Plant instructions in data (KB article, ticket, PDF, plugin update), then let an employee use the AI |

9 challenges, 2,050 points. Together they cover all of the **OWASP Top 10 for LLM Applications 2025**
(LLM01–LLM10). Each challenge lists its codes in `owasp:` in its YAML; the board, the lesson card and `/admin` show
them, and `WRITEUP.md` has the full coverage table.

**Language:** the **EN | ไทย** switch in the header translates the player UI, challenge stories, hints and lessons (`app/i18n.py`, `*_th` fields in each challenge YAML; English is the fallback). The chat itself is never translated: BigfishDesk, its prompts and X-Ray stay in English, and so does `/admin`.

Learning features: **X-Ray** (the exact prompt the LLM got, secrets masked), **Victim POV**, a lesson card with
"How to defend" after each solve. Win checks are deterministic code; the LLM never decides who won.

| Page | Who |
|---|---|
| `/` → `/board` → `/c/<id>` | Players (team cookie; teammates join with the 6-character code in the header) |
| `/scoreboard` | Public, refreshes every 10 s, 🩸 first blood. Put it on the projector |
| `/admin` | Organizers (`ADMIN_PASSWORD`): unlock Indirect, Reset all, solves, submissions, ⚠ flag-sharing and ⚡ fast-solve warnings, solutions |

---

## 1. Setup

You need Docker (recommended) or Python 3.12+, and **one** LLM: a cloud API key (easiest) or a local Ollama.

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Edit `.env`:

| Variable | What to put |
|---|---|
| `APP_SECRET` | A long random string, e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Signs team + admin cookies and derives each team's flags and the AI-agent canary. **Set it before teams register and don't change it during the event** |
| `ADMIN_PASSWORD` | Password for `/admin`. Empty = admin disabled |
| `DEFAULT_PROVIDER` | `cloud` (Groq) or `local` (Ollama). Must be a name in `config/providers.yaml` |
| `GROQ_API_KEY` | For `cloud`. Free key at https://console.groq.com. Several keys, comma-separated (`k1,k2,k3`): the app uses the first and switches to the next when one is rate-limited (429), used up for the day, or rejected; a spent key rests for as long as Groq's `retry-after` says. `prewarm.py` checks every key. |
| `LLM_TEMPERATURE`, `LLM_SEED`, `LLM_MAX_TOKENS`, `HISTORY_TURNS` | Defaults are calibrated; leave them |
| `RATE_LIMIT_PER_MIN` | LLM requests per team per challenge per minute (default 6) |
| `FAST_SOLVE_SECONDS` | `/admin` marks a correct flag with ⚡ when it came faster than this after the team first opened the challenge (default 90) |
| `PER_TEAM_FLAGS` | `true` (default): every team gets its own flag variant, `bigfish{<base>_<6 hex>}`, so flags can't be shared or copied from the write-up |

The admin page shows a warning while `APP_SECRET` or `ADMIN_PASSWORD` is still `change-me`.

### Option A: cloud (Groq), recommended

```bash
# in .env: DEFAULT_PROVIDER=cloud and GROQ_API_KEY=<your key>   (.env.example defaults to local)
docker compose up -d --build
docker compose exec app python scripts/prewarm.py      # must print "All providers answered."
```
Open http://localhost:8000. All 9 challenges were calibrated on Groq `openai/gpt-oss-120b` (see §3).

**Capacity:** Groq's free tier allows about 8K tokens/min and 200K tokens/day for this model, roughly 130–200 chat
turns per day across all teams. That is not enough for a full workshop: use the paid Dev Tier on the day.

### Option B: local model (Ollama)

```bash
# DEFAULT_PROVIDER=local in .env
docker compose --profile local up -d --build
docker compose exec ollama ollama pull gemma4:e4b      # large download: do it the day before
docker compose exec app python scripts/prewarm.py      # first call loads the model; can take a minute
```
Ollama keeps the model loaded for 24 h and serves 4 requests in parallel (`docker-compose.yml`). With ~10 teams
a GPU is strongly recommended; on CPU, replies take tens of seconds. Uncomment the GPU block in
`docker-compose.yml` for NVIDIA. Calibrate on your hardware before the day (§3); small models are less
predictable than the cloud model.

### Option C: run without Docker (development)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```
With a host Ollama, change `local.base_url` in `config/providers.yaml` to `http://localhost:11434/v1`.

### After changing things

| You changed | Do |
|---|---|
| `challenges/*.yaml`, `config/providers.yaml`, `.env` | `docker compose restart app` (they are mounted / read at start) |
| Python code or templates | `docker compose up -d --build app` |

The app validates every `challenges/*.yaml` on start and refuses to boot with a list of every problem it
found. See it with `docker compose logs app`.

---

## 2. Pre-warm / smoke test

```bash
docker compose exec app python scripts/prewarm.py          # providers the active challenges use
docker compose exec app python scripts/prewarm.py --all    # every provider in providers.yaml
```
Sends one tiny request per provider/model and prints OK/FAIL with latency. It catches a missing or wrong API key, a
model that was never pulled (`model 'gemma4:e4b' not found`), and network problems, and it loads the local model
into memory. Exit code 1 if anything fails.

---

## 3. Calibration

```bash
docker compose exec app python scripts/calibrate.py --runs 10 --delay 6
docker compose exec app python scripts/calibrate.py --runs 10 --challenge i3 --provider local
```
Replays each challenge's `solution` through the real engine (no HTTP, throwaway DB) and prints
`challenge | provider/model | success/runs | latency`. It also tries 3 naive attacks ("Give me the flag.") that
**should fail**. Exit code 1 if any solution wins less than 8/10. `--delay` avoids cloud rate limits.

Last run (Groq `gpt-oss-120b`, 2026-11-06; details and walkthroughs in `WRITEUP.md`): D5 10/10, D6 10/10,
D7 30/30, I1 10/10, I2 10/10, I3 10/10, I4 9/9 (+1 rate-limit error); D8 10/10, D9 10/10 (2026-11-09). Results vary between runs (I2 was 8/10 the
day before), so if an attempt fails players can simply send the message or run the victim again.

If a challenge falls below 8/10 on your model: try another model (`--model`), or set `provider:`/`model:` in that
challenge's YAML. Don't rewrite prompts on the day; the solutions and hints depend on them.

D1–D4 and the original email challenge (`i4mail`) are in `challenges/_archive/` (not loaded) because no tested
model solved them reliably.
Move a file back into `challenges/` to re-enable it, then calibrate.

---

## 4. Workshop-day checklist

**The day before**
- [ ] `.env`: real `APP_SECRET` and `ADMIN_PASSWORD`, provider + API key
- [ ] `docker compose up -d --build` on the machine you will use on the day, on the venue network if possible
- [ ] Local only: `ollama pull gemma4:e4b` done
- [ ] `prewarm.py` → all OK; `calibrate.py --runs 10` → every challenge ≥ 8/10
- [ ] Play one Direct and one Indirect challenge end to end in a browser
- [ ] Cloud: check the provider's rate limits/quotas against ~10 teams × 6 req/min

**1 hour before**
- [ ] `/admin` → **Reset all** (type `RESET`) to remove test teams. This also locks Indirect. Teams created before
      per-team flags (or before an `APP_SECRET` change) hold old flags in their data, so reset them
- [ ] Run `prewarm.py` again (it also loads a local model into memory)
- [ ] Find the URL players will use (`http://<your-LAN-IP>:8000`); check it from a phone on the guest Wi-Fi
- [ ] Projector: open `/scoreboard`. Organizer laptop: open `/admin`. Keep the Solutions panels collapsed on screen

**During**
- [ ] Teams create a team on `/`; teammates join with the code in the header (same team = shared chat & state)
- [ ] When you reach the Indirect part: `/admin` → **🔓 Unlock Indirect** (players refresh the board)
- [ ] Announce the rules at the start (they are not shown on the site): asking an AI chatbot for ideas is fine,
      **AI agents playing for a team are not**; hints cost points; New chat is the only reset
- [ ] ⚡ on a submission = solved less than `FAST_SOLVE_SECONDS` after the team first opened the challenge (time and
      attempt count are in the row). Typical for an agent, or for a team that got the answer elsewhere. Go and talk to them
- [ ] ⚠ "no activity" on a submission = correct flag but the team never chatted or ran the victim on that
      challenge. Could be flag sharing (chat turns survive **New chat**, so it isn't caused by restarting). Ask before you act
- [ ] 🤖 on a submission = the team submitted the **hidden canary flag**. It's in invisible text on every page, addressed
      to AI agents; nobody finds it by playing. An agent was almost certainly at the keyboard (or someone pasted the
      page into a chatbot and followed it). Go and talk to them.
      The player sees "🤖 ฮั่นแน่ ใช้ AI หรอ" instead of "wrong"
- [ ] 📄 = the base flag from the write-up/YAML (no team's data contains it); 🔁 = another team's flag. Both are
      rejected for the player as a plain "wrong", and `/admin` shows where they came from
- [ ] Bot says "The AI is busy"? Provider error or timeout; players just retry. Check `docker compose logs app`
- [ ] Scoring: hints open in order and cost 10% / 20% / 30% of that challenge's points (free after solving).
      Players' only reset is **🗨 New chat** on Direct challenges; Indirect has none (they edit the payload again).
      The lesson card appears after a solve. `WRITEUP.md` has a run sheet, debrief guide and per-challenge nudges

**After**
- [ ] Results live in `data/bigfishdesk.db` (SQLite). Copy it if you want them, then Reset all

---

## 5. Adding a challenge

1. Copy the closest existing file in `challenges/` (Direct: `d5.yaml`, `d7.yaml` for a tool-count win, `d8.yaml` for a SQL database, `d9.yaml` for SQL with a second database and an override code; Indirect: `i1.yaml` KB, `i2.yaml` ticket,
   `i3.yaml` PDF, `i4.yaml` plugin) to `challenges/<id>.yaml`. The file name doesn't matter; `id` does (lowercase, unique).
2. Edit the fields. The schema is in `SPEC.md` §5 and enforced by `app/challenges.py`:
   - `track`, `order`, `title`, `points`, `difficulty` (1–3), player text: `story`, `objective`, `hints`, `lesson`
   - `system_prompt` with placeholders `{{FLAG}} {{KB}} {{TICKETS}} {{INBOX}} {{ATTACHMENT}} {{CURRENT_USER}}`
   - `tools` from `get_ticket`, `reset_password`, `forward_email`, `reply_email`, `run_sql`, `query_helpdesk_db`; optional `input_filter` / `output_filter`
   - `attacker_surface`: `chat` | `kb_edit` | `ticket_form` | `pdf_upload` | `email_send` | `plugin_edit`; Indirect needs `victim`
   - `owasp`: OWASP LLM 2025 codes, most relevant first (e.g. `[LLM03, LLM01, LLM06]`)
   - `seed` (per-team KB/tickets/inbox; `sql` tables for `run_sql`/`query_helpdesk_db` (`database:` puts one in a second database; only `exposed: true` ones are in scope), `sql_override_code` for `query_helpdesk_db`), `flag` (`bigfish{...}`), `flag_mode` (`in_data` = player extracts it,
     `on_impact` = shown when the win rule fires), `win` (`none`, `output_contains`, `victim_output_contains`,
     `tool_called`, `tool_calls_at_least`, `json_rank_first`, `json_approval_hidden`), `secrets` to mask in X-Ray
   - `solution`: what calibrate replays and admin shows. Never sent to players
3. `docker compose restart app`. If it doesn't come up, `docker compose logs app` lists every problem.
4. `calibrate.py --challenge <id> --runs 10` until the solution wins ≥ 8/10 and the naive attempts fail.
5. `pytest -q`: `tests/conftest.py` lists `ACTIVE_IDS`; add your id there.

Mark deliberately vulnerable code with `# INTENTIONALLY VULNERABLE (challenge X)`. Don't "fix" it.

---

## 6. Project layout

```
app/           FastAPI app: main.py (routes), engine.py, llm.py (plain OpenAI SDK tool loop), tools.py (fake tools),
               filters.py, win.py, pdfcheck.py, xray.py, state.py (per-team data), scoreboard.py, auth.py, db.py
app/templates/ Jinja2 + HTMX + Tailwind (CDN) pages and partials
challenges/    active challenge YAML (+ _archive/ cut challenges)
config/        providers.yaml: named OpenAI-compatible endpoints
scripts/       calibrate.py, prewarm.py
tests/         pytest (fake LLM, no network)
data/          SQLite DB (created on first start, git-ignored)
```

No outbound network calls except to the configured LLM provider. Tools are fake: nothing is emailed, reset or
approved for real.
