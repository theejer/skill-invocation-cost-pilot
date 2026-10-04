"""Remove local paths, account identifiers and keys from everything under results/.

python scripts/scrub.py           rewrite files in place
python scripts/scrub.py --check   list what would change; exit 1 if anything would
"""

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from common import RESULTS, ROOT, load_env_file

ID_KEYS = ["creator_user_id", "creator_account_id", "account_id", "accountId", "user_id", "userId",
           "organization_id", "organizationId", "org_id", "email", "account_uuid", "accountUuid"]


def path_forms(path):
    raw = str(Path(path).resolve())
    forms = {raw, raw.replace("\\", "/"), raw.replace("\\", "\\\\"), json.dumps(raw)[1:-1]}
    if re.match(r"^[A-Za-z]:", raw):
        forms.add("/" + raw[0].lower() + raw[2:].replace("\\", "/"))
        # streamed tool-call fragments can split the drive letter from the rest of the path
        forms |= {f[2:] for f in list(forms) if re.match(r"^[A-Za-z]:", f)}
    return sorted(forms, key=len, reverse=True)


def rules():
    out = []
    for path, token in ((ROOT, "<repo>"), (tempfile.gettempdir(), "<tmp>"), (Path.home(), "<home>")):
        for form in path_forms(path):
            out.append((re.compile(re.escape(form), re.IGNORECASE), token))
    keys = "|".join(map(re.escape, ID_KEYS))
    out.append((re.compile(rf'("(?:{keys})"\s*:\s*)"(?!<redacted>")[^"]*"'), r'\1"<redacted>"'))
    out.append((re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "<redacted>"))
    out.append((re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.invalid\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"))
    for name in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
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
    changed = 0
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
    print(f"{changed} file(s) {'to scrub' if args.check else 'scrubbed'}")
    if args.check and changed:
        sys.exit(1)


if __name__ == "__main__":
    main()
