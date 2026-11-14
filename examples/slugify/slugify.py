"""Turn a title into a URL slug: "Hello, World!" becomes "hello-world"."""

import re
import sys
import unicodedata


def slugify(text: str) -> str:
    """Lower-case ASCII words joined by single hyphens."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", ascii_text.lower())
    return "-".join(words)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print('usage: python3 slugify.py "Some Title"', file=sys.stderr)
        return 2
    print(slugify(argv[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
