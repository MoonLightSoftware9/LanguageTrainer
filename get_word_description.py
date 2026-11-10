#!/usr/bin/env python3

import sys
from pathlib import Path

from pystardict import Dictionary



def main():
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} WORD", file=sys.stderr)
        sys.exit(1)

    word = sys.argv[1]

    dictionary = Dictionary(
      "stardict-Macmillan_English_Dictionary-2.4.2/Macmillan_English_Dictionary"
    )

    try:
        article = dictionary[word]
    except KeyError:
        print(f"word not found: {word}", file=sys.stderr)
        sys.exit(2)

    print(article)


if __name__ == "__main__":
    main()
