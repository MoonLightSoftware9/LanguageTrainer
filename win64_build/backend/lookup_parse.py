#!/usr/bin/env python3

import argparse
import re
import sys
from pathlib import Path
from pprint import pprint

from bs4 import BeautifulSoup
from pystardict import Dictionary


DICT_DIR = Path("stardict-Macmillan_English_Dictionary-2.4.2/Macmillan_English_Dictionary")
STAR_TAG_RE = re.compile(r"<abr>(★{1,3})</abr>")


def clean(text):
    if text is None:
        return None
    text = text.replace("\u200b", "")
    return " ".join(text.split()).strip()


def count_stars(text):
    return min(3, text.count("★")) if text else 0


def text_of(node):
    return clean(node.get_text(" ", strip=True))


def color_is(tag, color):
    return tag and tag.name == "c" and tag.get("c") == color


def split_entries(root):
    """
    Разбивает статью на entry-блоки:
    ▪ I. test noun ...
    ▪ II. test verb ...
    """
    entry_headers = []

    for b in root.find_all("b"):
        txt = text_of(b)
        if txt and txt.startswith("▪"):
            entry_headers.append(b)

    if not entry_headers:
        return [root]

    entries = []

    for i, header in enumerate(entry_headers):
        end_header = entry_headers[i + 1] if i + 1 < len(entry_headers) else None

        nodes = []
        node = header

        while node:
            if node is end_header:
                break
            nodes.append(str(node))
            node = node.next_sibling

        fragment = "<entry>" + "".join(nodes) + "</entry>"
        entries.append(BeautifulSoup(fragment, "lxml-xml").entry)

    return entries


def parse_header(entry):
    header_nodes = []

    for child in entry.children:
        if getattr(child, "name", None) == "blockquote":
            break
        header_nodes.append(str(child))

    header = BeautifulSoup("<header>" + "".join(header_nodes) + "</header>", "lxml-xml").header
    header_text = text_of(header) or ""

    homograph = None
    first_b = header.find("b")
    if first_b:
        m = re.search(r"▪\s*([IVXLCDM]+\.)", text_of(first_b) or "")
        if m:
            homograph = m.group(1)

    headword = None
    bolds = header.find_all("b")
    for b in bolds:
        t = text_of(b)
        if t and not t.startswith("▪"):
            headword = t
            break

    index = None
    sup = header.find("sup")
    if sup:
        index = text_of(sup)

    part_of_speech = None
    pos_tag = header.find(lambda t: color_is(t, "orange"))
    if pos_tag:
        part_of_speech = text_of(pos_tag)

    labels = []
    for tag in header.find_all(["abr", "c"]):
        t = text_of(tag)
        if not t:
            continue

        if t in {"UK", "US"}:
            continue
        if "★" in t:
            continue
        if tag.name == "c" and tag.get("c") in {"sienna"}:
            labels.append(t)

    pronunciations = []
    region_tags = [
        tag for tag in header.find_all("abr")
        if text_of(tag) in {"UK", "US"}
    ]

    for region_tag in region_tags:
        region = text_of(region_tag)
        audio = None
        ipa = None

        for nxt in region_tag.next_elements:
            if getattr(nxt, "name", None) == "abr" and text_of(nxt) in {"UK", "US"}:
                break
            if getattr(nxt, "name", None) == "rref" and audio is None:
                audio = text_of(nxt)
            if getattr(nxt, "name", None) == "c" and nxt.get("c") == "teal" and ipa is None:
                ipa = text_of(nxt)

        pronunciations.append({
            "region": region,
            "audio": audio,
            "ipa": ipa,
        })

    forms = parse_forms(header)

    return {
        "homograph": homograph,
        "headword": headword,
        "index": index,
        "part_of_speech": part_of_speech,
        "stars": count_stars(header_text),
        "labels": labels,
        "pronunciations": pronunciations,
        "forms": forms,
        "header_text": header_text,
    }


def parse_forms(header):
    """
    Пытается вытащить формы из скобочного блока:
    singular test plural tests
    present tense I/you/we/they test ...
    """
    tokens = []

    for tag in header.find_all(["i", "c"]):
        t = text_of(tag)
        if not t:
            continue

        if t in {"[", "]"}:
            continue

        if tag.name == "i":
            tokens.append(("label", t))
        elif tag.name == "c" and tag.get("c") == "darkslategray":
            tokens.append(("value", t))

    forms = {}
    pending_labels = []

    for kind, value in tokens:
        if kind == "label":
            pending_labels.append(value)
        elif kind == "value":
            key = " ".join(pending_labels).strip() or value
            forms[key] = value
            pending_labels = []

    return forms


def leaf_blocks(entry):
    return [
        b for b in entry.find_all("blockquote")
        if not b.find("blockquote")
    ]


def sense_number(block):
    txt = text_of(block)
    if not txt:
        return None

    m = re.match(r"^(\d+[a-z]?\.)\s+", txt)
    if m:
        return m.group(1)

    return None


def parse_thesaurus(block):
    items = []
    krefs = block.find_all("kref")
    subs = block.find_all("sub")

    for i, kref in enumerate(krefs):
        relation = text_of(subs[i]) if i < len(subs) else None
        items.append({
            "target": text_of(kref),
            "relation": relation,
        })

    return items


def parse_examples(block):
    return [text_of(ex) for ex in block.find_all("ex") if text_of(ex)]


def parse_patterns(block):
    patterns = []

    for b in block.find_all("b"):
        t = text_of(b)
        if t and not re.match(r"^\d+[a-z]?\.$", t):
            patterns.append(t)

    return patterns


def parse_senses(entry):
    senses = []
    current = None
    phrases = []
    phrasal_verbs = []
    see_also = []
    unclassified = []

    for block in leaf_blocks(entry):
        block_text = text_of(block)
        if not block_text:
            continue

        num = sense_number(block)

        if num:
            definition = clean(re.sub(r"^\d+[a-z]?\.\s*", "", block_text))

            current = {
                "number": num,
                "definition": definition,
                "labels": [
                    text_of(c)
                    for c in block.find_all(lambda t: color_is(t, "sienna"))
                    if text_of(c)
                ],
                "examples": parse_examples(block),
                "patterns": parse_patterns(block),
                "thesaurus": [],
                "variants": parse_variants(block),
                "notes": [],
                "raw_text": block_text,
            }
            senses.append(current)
            continue

        if block_text.startswith("Thesaurus:"):
            target = current if current else None
            if target is not None:
                target["thesaurus"].extend(parse_thesaurus(block))
            else:
                unclassified.append(block_text)
            continue

        if block_text.startswith("Phrases:"):
            phrases.extend([text_of(k) for k in block.find_all("kref") if text_of(k)])
            continue

        if block_text.startswith("Phrasal Verb:"):
            phrasal_verbs.extend([text_of(k) for k in block.find_all("kref") if text_of(k)])
            continue

        if block_text.startswith("See also:"):
            see_also.extend([text_of(k) for k in block.find_all("kref") if text_of(k)])
            continue

        examples = parse_examples(block)
        patterns = parse_patterns(block)

        if current is None:
            variants = parse_variants(block)
            current = {
                "number": None,
                "definition": remove_variant_prefixes(block_text, variants),
                "labels": [
                    text_of(c)
                    for c in block.find_all(lambda t: color_is(t, "sienna"))
                    if text_of(c)
                ],
                "examples": examples,
                "patterns": patterns,
                "thesaurus": [],
                "variants": variants,
                "notes": [],
                "raw_text": block_text,
            }
            senses.append(current)
            continue

        if examples:
            current["examples"].extend(examples)

        if patterns:
            current["patterns"].extend(patterns)

        if not examples and not patterns:
            current["notes"].append(block_text)

    return senses, phrases, phrasal_verbs, see_also, unclassified


def parse_variants(block):
    variants = []

    for sub in block.find_all("sub"):
        if text_of(sub) == "variant":
            prev_b = sub.find_previous("b")
            if prev_b:
                variants.append(text_of(prev_b))

    return variants


def remove_variant_prefixes(text, variants):
    cleaned = text
    for variant in variants:
        cleaned = cleaned.replace(f"{variant} variant ", "")
    return clean(cleaned)


def parse_thesaurus_article(raw):
    """Parse a Macmillan Thesaurus topic entry.

    A topic (e.g. ``accidents``) is a titled list of synonyms. In the StarDict
    XML the synonyms come as alternating top-level ``<blockquote>`` pairs: a
    *term* block (``<b>word</b> <sup>..</sup> <c c="orange">pos</c> <kref>..</kref>``)
    immediately followed by a deeply nested *definition* block. The ``<kref>``
    is the headword to look up in the main Macmillan dictionary; register/domain
    markers (``formal``, ``linguistics``, ``British`` ...) are pulled out of the
    gloss into ``labels``.
    """
    soup = BeautifulSoup(f"<root>{raw}</root>", "lxml-xml")
    root = soup.root

    k = root.find("k")
    topic = text_of(k) if k else None

    title = None
    category = None
    for child in root.children:
        name = getattr(child, "name", None)
        if name == "blockquote":
            break
        if name == "b" and title is None:
            title = text_of(child)
        if name == "c" and child.get("c") == "red":
            sup = child.find("sup")
            if sup and category is None:
                category = text_of(sup)

    blocks = [c for c in root.children if getattr(c, "name", None) == "blockquote"]
    items = []
    i = 0
    while i < len(blocks):
        term = blocks[i]
        # Definition blocks are nested; a stray one without a preceding term is
        # skipped rather than mistaken for a synonym.
        if term.find("blockquote"):
            i += 1
            continue

        bold = term.find("b")
        word = text_of(bold) if bold else None
        sup = term.find("sup")
        pos_tag = term.find(lambda t: color_is(t, "orange"))
        kref = term.find("kref")

        definition = None
        labels = []
        if i + 1 < len(blocks) and blocks[i + 1].find("blockquote"):
            leaf = blocks[i + 1]
            while leaf.find("blockquote"):
                leaf = leaf.find("blockquote")
            for marker in leaf.find_all(
                lambda t: t.name == "c" and t.get("c") in {"rosybrown", "darkgreen", "sienna"}
            ):
                text = text_of(marker)
                if text:
                    labels.append(text)
                marker.extract()
            for marker in leaf.find_all("abr"):
                text = text_of(marker)
                if text:
                    labels.append(text)
                marker.extract()
            definition = text_of(leaf)
            i += 2
        else:
            i += 1

        if not word:
            continue

        items.append({
            "word": word,
            "sup": text_of(sup) if sup else None,
            "pos": text_of(pos_tag) if pos_tag else None,
            "kref": text_of(kref) if kref else word,
            "definition": definition,
            "labels": labels,
        })

    return {
        "topic": topic,
        "title": title or (topic.capitalize() if topic else None),
        "category": category,
        "items": items,
    }


def parse_article(raw):
    soup = BeautifulSoup(f"<root>{raw}</root>", "lxml-xml")
    root = soup.root

    k = root.find("k")
    word = text_of(k) if k else None

    result = {
        "word": word,
        "stars": 0,
        "entries": [],
    }

    for entry_node in split_entries(root):
        header = parse_header(entry_node)
        senses, phrases, phrasal_verbs, see_also, unclassified = parse_senses(entry_node)

        result["entries"].append({
            **header,
            "senses": senses,
            "phrases": phrases,
            "phrasal_verbs": phrasal_verbs,
            "see_also": see_also,
            "unclassified": unclassified,
        })

    result["stars"] = max((entry["stars"] for entry in result["entries"]), default=0)

    return result


def parse_article_stars(raw):
    return max((len(match) for match in STAR_TAG_RE.findall(raw)), default=0)


def parse_star_filter(value):
    selected_stars = set()

    for item in value.split(","):
        item = item.strip()
        if not item:
            raise argparse.ArgumentTypeError("expected comma-separated values from 1 to 3")

        try:
            stars = int(item)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("stars must be comma-separated integers from 1 to 3") from exc

        if stars not in {1, 2, 3}:
            raise argparse.ArgumentTypeError("stars must be comma-separated integers from 1 to 3")

        selected_stars.add(stars)

    return frozenset(selected_stars)


def parse_args(argv):
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--word", metavar="WORD", help="print parsed structure for WORD")
    group.add_argument(
        "--stars",
        type=parse_star_filter,
        metavar="{1,2,3}[,{1,2,3}...]",
        help="print words with exactly these star counts, separated by commas",
    )

    if not argv:
        parser.print_help(sys.stderr)
        sys.exit(1)

    return parser.parse_args(argv)


def print_word(dictionary, word):
    try:
        raw = dictionary[word]
    except KeyError:
        print(f"word not found: {word}", file=sys.stderr)
        sys.exit(2)
    if not raw:
        print(f"word not found: {word}", file=sys.stderr)
        sys.exit(2)

    parsed = parse_article(raw)

    pprint(parsed, width=120, sort_dicts=False)


def print_words_by_stars(dictionary, selected_stars):
    for word in dictionary.keys():
        try:
            raw = dictionary[word]
        except KeyError:
            continue

        if raw is not None and parse_article_stars(raw) in selected_stars:
            print(word)


def main():
    args = parse_args(sys.argv[1:])
    dictionary = Dictionary(str(DICT_DIR), in_memory=args.stars is not None)

    if args.word:
        print_word(dictionary, args.word)
    else:
        print_words_by_stars(dictionary, args.stars)


if __name__ == "__main__":
    main()
