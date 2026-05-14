"""Secret-pattern scrubbing for cron commands (STAGE-002-007D discovery hardening).

When a cron command is discovered and stored in the database, we scrub common
secret patterns (passwords, API keys, tokens) to prevent accidental exposure
in logs, UI, or audit trails.

Key design decision: Scrubbing happens at STORAGE time (in upsert_discovered),
NOT at parse time. The fingerprint is computed from the RAW command before
scrubbing, ensuring convergence with the wrapper installer (which uses the
unscrubbed command for fingerprinting). The scrubbed version is what gets
stored in the database and displayed to users.

Patterns scrubbed:
- MySQL/MariaDB: -pPASSWORD (no space after -p)
- Long-form password: --password=VALUE or --password VALUE
- Environment vars: MYSQL_PWD=VALUE, PGPASSWORD=VALUE
- API keys: --api-key=VALUE, --apikey=VALUE
- Generic tokens: --token=VALUE

False-positives avoided:
- ssh -p PORT is NOT scrubbed (port flag uses space: -p 22)
- --user, --host, etc. are NOT scrubbed (not secrets)
"""

from __future__ import annotations

import re


def scrub_secrets(command: str) -> str:
    """Scrub common secret patterns from a cron command string.

    Args:
        command: Raw cron command as it appears on disk.

    Returns:
        The same command with secrets replaced by <redacted>.
    """
    if not command:
        return command

    result = command

    # Long-form password patterns BEFORE short-form -p (to avoid -p matching part of --password).
    # Long-form with equals: --password=SECRET
    # Example: psql --password=hunter2 mydb → psql --password=<redacted> mydb
    result = re.sub(r"--password=(\S+)", r"--password=<redacted>", result)

    # Long-form with space: --password SECRET
    # Match --password followed by whitespace and then a non-whitespace token.
    # Example: psql --password hunter2 mydb → psql --password <redacted> mydb
    result = re.sub(r"--password\s+(\S+)", r"--password <redacted>", result)

    # MySQL/MariaDB: -pPASSWORD (immediate, no space after -p).
    # Matches -p followed by one or more non-whitespace characters.
    # Use negative lookbehind (?<![A-Za-z0-9_-]) to ensure -p is a flag token start:
    # NOT preceded by a word character, underscore, or hyphen. This prevents matching
    # embedded -p (e.g., 'run-parts'), while still matching -p at string-start or after space.
    # Already handled --password above, so we don't need to check for that.
    # Careful: this is NOT -p PORT (which has a space, e.g., 'ssh -p 22').
    # Example: mysqldump -u user -psecret123 db → mysqldump -u user -p<redacted> db
    result = re.sub(r"(?<![A-Za-z0-9_-])-p(\S+)", r"-p<redacted>", result)

    # Environment variables: MYSQL_PWD=SECRET, PGPASSWORD=SECRET, etc.
    # Example: MYSQL_PWD=foo mysqldump db → MYSQL_PWD=<redacted> mysqldump db
    result = re.sub(r"(MYSQL_PWD|PGPASSWORD)=(\S+)", r"\1=<redacted>", result)

    # API key with equals: --api-key=SECRET or --apikey=SECRET
    # Example: curl --api-key=secret123 https://x → curl --api-key=<redacted> https://x
    # Preserve the original form (with or without hyphen) in the output
    def _redact_api_key(m: re.Match[str]) -> str:
        """Redact API key while preserving the flag form (--api-key or --apikey)."""
        prefix = m.group(0)[: m.group(0).index("=") + 1]
        return f"{prefix}<redacted>"

    result = re.sub(r"--api-?key=(\S+)", _redact_api_key, result)

    # Generic token pattern: --token=SECRET
    # Example: curl --token=xyz https://x → curl --token=<redacted> https://x
    result = re.sub(r"--token=(\S+)", r"--token=<redacted>", result)

    return result


__all__ = ["scrub_secrets"]
