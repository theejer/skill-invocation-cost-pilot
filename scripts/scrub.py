"""Remove local paths, account identifiers and keys from everything under results/.

python scripts/scrub.py           rewrite files in place; exit 1 if a user folder remains
python scripts/scrub.py --check   list what would change or remains; exit 1 if anything would or does
"""

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

from common import RESULTS, ROOT, RUN_ROOT, load_env_file

ID_KEYS = ["creator_user_id", "creator_account_id", "account_id", "accountId", "user_id", "userId",
           "organization_id", "organizationId", "org_id", "email", "account_uuid", "accountUuid"]
SECRET_KEYS = ["access_token", "refresh_token", "id_token", "api_key", "apiKey"]
KEY_VARS = ["OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"]

SEP = r"(?:\\+|/+|-)"
DRIVE = r"(?:(?:[A-Za-z]:|/[A-Za-z]|[A-Za-z]-)" + SEP + ")?"
USER_FOLDER = re.compile(r"(?:(?<![A-Za-z0-9])(?:[A-Za-z]:|/[A-Za-z]|[A-Za-z]-)" + SEP + r"(?:Users|home)" + SEP
                         + r"[A-Za-z0-9]|(?-i:(?<![A-Za-z0-9._~-])/(?:Users|home)/[A-Za-z0-9])"
                         + r"|(?<![A-Za-z0-9])Users" + SEP + re.escape(Path.home().name) + r"(?![A-Za-z0-9]))",
                         re.IGNORECASE)
FILE_OWNER = re.compile(r"(?<=\s)" + re.escape(Path.home().name) + r"(?=\s+\d+\s)")


def path_pattern(path):
    parts = [p for p in Path(path).resolve().parts[1:] if p]
    names = [re.sub(r"\\[^A-Za-z0-9]|[^A-Za-z0-9\\]", "[^A-Za-z0-9]", re.escape(p)) for p in parts]
    return re.compile(DRIVE + SEP.join(names) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def rules():
    out = []
    paths = ((RUN_ROOT, "<runs>"), (ROOT, "<repo>"), (tempfile.gettempdir(), "<tmp>"), (Path.home(), "<home>"))
    for path, token in sorted(paths, key=lambda p: len(Path(p[0]).resolve().parts), reverse=True):
        out.append((path_pattern(path), token))
    out.append((FILE_OWNER, "<user>"))
    keys = "|".join(map(re.escape, ID_KEYS + SECRET_KEYS))
    out.append((re.compile(rf'("(?:{keys})"\s*:\s*)"(?!<redacted>")[^"]*"'), r'\1"<redacted>"'))
    out.append((re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "<redacted>"))
    out.append((re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "<redacted>"))
    out.append((re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16})\b"),
                "<redacted>"))
    out.append((re.compile(r"\b(Bearer\s+)(?!<redacted>)[A-Za-z0-9._~+/=-]{8,}"), r"\1<redacted>"))
    out.append((re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.invalid\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"))
    for name in KEY_VARS:
        value = os.environ.get(name)
        if value and len(value) > 8:
            out.append((re.compile(re.escape(value)), "<redacted>"))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    load_env_file()
    patterns = rules()
    changed = remaining = 0
    for path in sorted(p for p in RESULTS.rglob("*") if p.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            print(f"skipped (not text): {path.relative_to(RESULTS)}")
            continue
        new, hits = text, 0
        for pattern, token in patterns:
            new, n = pattern.subn(token, new)
            hits += n
        if hits:
            changed += 1
            print(f"{path.relative_to(RESULTS)}: {hits}")
            if not args.check:
                path.write_text(new, encoding="utf-8", newline="")
        for match in [*USER_FOLDER.finditer(new), *FILE_OWNER.finditer(new)]:
            remaining += 1
            print(f"user folder remains: {path.relative_to(RESULTS)}: {new[max(0, match.start() - 20):match.end() + 20]!r}")
    print(f"{changed} file(s) {'to scrub' if args.check else 'scrubbed'}, {remaining} user folder(s) remain")
    if remaining or (args.check and changed):
        sys.exit(1)


if __name__ == "__main__":
    main()
