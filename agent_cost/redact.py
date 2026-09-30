"""Mask credential values in the labels that go into reports.

Loop signatures and bloat labels are copied into the terminal report, the
Markdown file and the JSON output, and those get shared: pasted into a PR,
uploaded as a CI artifact. Agents often run commands with a credential
inline (`curl -H "Authorization: Bearer ..."`, `export API_KEY=...`), so the
value is replaced with *** before a label is stored. The rest of the command
is kept, so the action stays recognisable. Masking is pattern based: it
catches the shapes below, and a credential written some other way can still
get through, so a report is worth a read before it is shared.

The rule:

1. A NAME is split into lowercase words on non-alphanumerics and on camelCase
   boundaries: accessToken is access + token, X-Api-Key is x + api + key.
2. NAME is a credential name when its words include token, secret, password,
   passwd, passphrase, credential, credentials, auth, authorization, apikey
   or privatekey; or include key together with api, access, private, secret,
   encryption, signing, client, master or session; or are exactly pwd. A
   NAME right after $ or ${ is a shell variable being read ($PWD, $TOKEN),
   not an assignment, and is left alone.
3. A credential name in NAME=value, NAME: value, "name": "value",
   --name value or --name=value has its value masked, whatever its length,
   unless the value is none, null, true, false or a number. So
   --max-token=4096 stays and --password hunter2 does not.
4. A NAME whose only credential word is a bare key (key, --key, CACHE_KEY)
   is a sort field, a cache key or a dict key far more often than a secret,
   so its value is masked only when it looks generated: 16 or more
   characters, every one a letter, a digit or one of _ - + / =.
5. An Authorization header value is always masked, whatever its scheme
   (Bearer, Basic, ...), and so is a Bearer token anywhere. Authorization
   right after $ or ${ is a variable being read, as in rule 2
   ($AUTHORIZATION:/app, ${AUTHORIZATION:-none}), and is left alone. A token
   prefix (sk-, sk_live_, rk_, ghp_ and the other gh?_ kinds, github_pat_,
   glpat-, xox?-, AIza, AKIA, ASIA) is masked when 12 or more of
   [A-Za-z0-9_-] follow it.
6. A value that rule 3 or 4 keeps is scanned again like the rest of the
   text, so an unclosed quote (key='s default" && export API_TOKEN=...) or a
   long unquoted value (?key=v1/token=...) cannot hide a credential written
   after it or inside it.

Three readings go a little past the letter of the rule:

- a word that ends in password, passwd or passphrase counts (PGPASSWORD),
  and pwd counts as the last word of a longer name (MYSQL_PWD). Those are
  the standard password variables of PostgreSQL and MySQL.
- the lone word auth written as `auth: text`, with no quotes around it, is
  how a commit message or a heading names the auth area (`auth: fix login
  redirect`), so in that one form it gets the generated-value test from
  rule 4. auth=..., "auth": "...", --auth ... and longer names such as
  X-Auth-Token are masked as rule 3 says.
- sk_test_ keys are masked like sk_live_ keys, as they were before.

Everything is linear on its input, because a label can be an untruncated
file path of any length: a NAME must begin where a run of name characters
begins, no two unbounded quantifiers can match the same run, and the scan
below only ever moves forward. Rule 6 has values start inside one another
(key=key=key=...), all running to the same end, so the rule 4 test reads a
run of token characters once and remembers where it ends, instead of reading
it again for every value that starts inside it.
"""

from __future__ import annotations

import re

_MASK = r"\1***"

# Header values, userinfo and token shapes: fixed patterns, run first.
_PATTERNS = [
    # Authorization: Bearer <value>, Authorization: Basic <value>, but not
    # $AUTHORIZATION or ${AUTHORIZATION:-...}, a variable being read
    re.compile(r"""(?i)(?<!\$)(?<!\$\{)(\bauthorization["']?\s*[:=]\s*"""
               r"""(?:(?:bearer|basic|token|digest)\s+)?)[^\s'"]+"""),
    re.compile(r"""(?i)(\bbearer\s+)[^\s'"]+"""),
    # scheme://user:password@host
    re.compile(r"(?i)(\b[a-z][a-z0-9+.-]{0,30}://[^/\s:@]*:)[^/\s@]+(?=@)"),
    # curl -u user:password, --user user:password
    re.compile(r"""((?:^|\s)(?:-u|--user)(?:\s+|=)[^\s:'"]*:)[^\s'"]+"""),
    # well-known token prefixes; the prefix is kept so the kind stays visible
    re.compile(r"\b(sk-|(?:sk|rk)_(?:live|test)_|rk_|gh[pousr]_|github_pat_"
               r"|glpat-|xox[abprs]-|AIza|AKIA|ASIA)[A-Za-z0-9_-]{12,}"),
]

# A NAME is a run of [A-Za-z0-9_.-] that starts where such a run starts, so
# 'monkey' is never read as 'key' and a long run is tried from one place
# only. Right after $ or ${ it is a variable being read, not an assignment.
_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_.$-])(?<!\$\{)(?:"
    # --name value, -name value
    r"--?(?P<flag>[A-Za-z0-9][A-Za-z0-9_.-]*)\s+"
    # NAME=value, NAME: value, "name": "value", --name=value, NAME := value,
    # and \"name\": \"value\", the JSON inside a double-quoted shell string
    r"""|(?P<name>[A-Za-z0-9_.-]+)(?P<sep>(?:\\?["'])?\s*(?::=|[:=]))\s*"""
    r")")

# The value after a credential name: an optional auth scheme that is kept,
# then a quoted string (\"escaped\" too) or a run up to whitespace, &, ; or a
# quote. A value cannot start with = or :, so `token == x` and `Token::new`
# are left alone.
_QUOTED = r"""\\?"[^"]*"?|\\?'[^']*'?"""
_SECRET_VALUE = re.compile(
    r"((?i:(?:bearer|basic|token|digest)\s+)?)"
    r"""(""" + _QUOTED + r"""|[^\s&;'"=:][^\s&;'"]*)""")
_QUOTES = "\\\"'"

# values that are settings, not secrets: none, null, true, false, numbers
_PLAIN_VALUE = re.compile(r"(?i:none|null|true|false)|[+-]?[0-9]+(?:\.[0-9]+)?")

# a closing bracket at the end of a value that did not open it: {"token": x}
_CLOSERS = {"(": ")", "[": "]", "{": "}"}

# The value after a bare key is a double-quoted or a single-quoted string
# (\"escaped\" too, closed or not), or a run that cannot start with = or :
# and stops at whitespace, & ; ' " and, as in {'key': 1}, at , ) } ]. It
# looks generated when what is left after stripping the quotes and
# backslashes around it is a run of 16 or more token characters. Each form
# is (what opens it, what is stripped on either side of the run, what must
# come right after).
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_\-+/=]*")
_GENERATED_LENGTH = 16
_KEY_FORMS = (
    (re.compile(r'\\?"'), re.compile(r"[\\']*"), re.compile(r'"|\Z')),
    (re.compile(r"\\?'"), re.compile(r'[\\"]*'), re.compile(r"'|\Z")),
    (re.compile(r"(?![=:])"), re.compile(r"\\*"),
     re.compile(r"""(?=[\s&;'",)}\]]|\Z)""")),
)

_CAMEL = re.compile(r"([a-z0-9])([A-Z])")            # accessToken
_CAMEL_ACRONYM = re.compile(r"([A-Z])([A-Z][a-z])")  # APIKey
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")
# a quick filter: a NAME with none of these in it cannot be a credential
_HINT = re.compile(r"(?i)token|secret|pass|credential|auth|key|pwd")

_CREDENTIAL_WORDS = frozenset({
    "token", "secret", "password", "passwd", "passphrase", "credential",
    "credentials", "auth", "authorization", "apikey", "privatekey"})
_KEY_QUALIFIERS = frozenset({
    "api", "access", "private", "secret", "encryption", "signing", "client",
    "master", "session"})
_PASSWORD_ENDINGS = ("password", "passwd", "passphrase")

_SECRET, _BARE_KEY = "secret", "key"


def _words(name: str) -> list[str]:
    """`name` as lowercase words: X-Api-Key is x, api, key and accessToken
    is access, token."""
    name = _CAMEL_ACRONYM.sub(r"\1 \2", _CAMEL.sub(r"\1 \2", name))
    return [word for word in _NOT_ALNUM.split(name.lower()) if word]


def _kind(name: str) -> str | None:
    """_SECRET for a credential name, _BARE_KEY for a name whose only
    credential word is key, None for any other name."""
    if not _HINT.search(name):
        return None
    words = _words(name)
    if any(word in _CREDENTIAL_WORDS or word.endswith(_PASSWORD_ENDINGS)
           for word in words):
        return _SECRET
    if "key" in words:
        return _SECRET if _KEY_QUALIFIERS.intersection(words) else _BARE_KEY
    if words and words[-1] == "pwd":
        return _SECRET
    return None


def _secret_span(text: str, at: int, flag: bool):
    """(start, end) of the value at `at` after a credential name when rule 3
    masks it, else None."""
    match = _SECRET_VALUE.match(text, at)
    if match is None:
        return None
    value, start = match.group(2), match.start(2)
    if flag and value[0] in "-<>|":
        return None  # --password-stdin -u me, --with-token < file: no value
    if value[0] not in _QUOTES:
        # {"token": null} ends at the brace, ${TOKEN} keeps its own
        value = value.rstrip("," + "".join(
            close for open_, close in _CLOSERS.items() if open_ not in value))
    bare = value.strip(_QUOTES)
    if bare == "" or _PLAIN_VALUE.fullmatch(bare) is not None:
        return None
    return start, start + len(value)


class _GeneratedValues:
    """The rule 4 test for the values after bare keys in one text.

    Under rule 6 the values can start inside one another (key=key=key=...x)
    and all run to the same end. So the run of token characters under a
    position, and what comes after it, are read once and remembered for
    every value that starts inside that run.
    """

    def __init__(self, text: str):
        self._text = text
        # the last run read: its start, its end, and for each form the end
        # of a value that closes right after it (None when none does)
        self._run: tuple[int, int, dict] = (-1, -1, {})

    def span(self, at: int):
        """(start, end) of the value at `at` when it looks generated, else
        None."""
        text = self._text
        for form, (opener, edge, after) in enumerate(_KEY_FORMS):
            opened = opener.match(text, at)
            if opened is not None:
                break
        else:
            return None  # a value cannot start with = or :
        start = edge.match(text, opened.end()).end()
        run_start, run_end, ends = self._run
        if not run_start <= start <= run_end:
            run_end = _TOKEN_RUN.match(text, start).end()
            ends = {}
            self._run = (start, run_end, ends)
        if run_end - start < _GENERATED_LENGTH:
            return None
        if form not in ends:
            closed = after.match(text, edge.match(text, run_end).end())
            ends[form] = None if closed is None else closed.end()
        end = ends[form]
        return None if end is None else (at, end)


def _mask_assignments(text: str) -> str:
    """`text` with the values that rules 1 to 4 and 6 mask replaced by ***."""
    out = []
    pos = 0
    generated = _GeneratedValues(text)
    while True:
        match = _ASSIGNMENT.search(text, pos)
        if match is None:
            break
        flag = match.group("flag")
        name = flag if flag is not None else match.group("name")
        kind = _kind(name)
        if kind is _SECRET and flag is None and name.lower() == "auth":
            sep = match.group("sep")
            if sep[0] not in _QUOTES and sep[-1] == ":":
                kind = _BARE_KEY  # auth: fix login redirect
        at = match.end()
        if kind is _SECRET:
            span = _secret_span(text, at, flag is not None)
        elif kind is _BARE_KEY:
            span = generated.span(at)
        else:
            span = None
        if span is None:
            # no value, or one the rule keeps: the scan goes on from the
            # start of it, so an unclosed quote cannot hide what follows
            out.append(text[pos:at])
            pos = at
            continue
        start, end = span
        out.append(text[pos:start])
        out.append("***")
        pos = end
    out.append(text[pos:])
    return "".join(out)


def redact_secrets(text: str) -> str:
    """`text` with the credential shapes described above replaced by ***,
    everything else kept."""
    for pattern in _PATTERNS:
        text = pattern.sub(_MASK, text)
    return _mask_assignments(text)
