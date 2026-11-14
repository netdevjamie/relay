"""Reading YAML that Relay and the owner did not write: a project's profile,
which a branch or a worktree can change, and the frontmatter of a role's
report.

YAML's own loader expands what a small file asks it to (a 557-byte profile
of merge keys held `relay next` 42 seconds and 1.7 GB) and raises errors of
its own on a value it cannot build. Here the text is read as the parser's
events, one at a time, and reading stops at the first anchor, alias, merge
key or tag, past MOST_NODES values, lists and mappings, or past MOST_DEPTH
levels of nesting: what a file under its caller's size limit can cost does
not grow with what it holds. A real profile or frontmatter needs none of it.

A value is built as YAML's safe loader builds it (text, a number, a date,
true or false, null), so every file that is read gives what that loader
gave. Only one value is built at a time, and only after it was read.

Standalone: it imports nothing of Relay's.
"""

import re

import yaml

# The most values, lists and mappings read of one text; a real profile
# holds a few dozen. Reading stops at the one after.
MOST_NODES = 5_000
# How deep lists and mappings may be nested; a real profile goes three
# deep. YAML's parser slows with the square of the depth.
MOST_DEPTH = 10
# The longest frontmatter read; a real one is a few lines.
MOST_FRONTMATTER_CHARACTERS = 64 * 1024

# Why a text is not read, worded to follow the name of what holds it.
HOLDS_A_REFERENCE = "holds a YAML anchor, alias or merge key (&name, *name or an unquoted <<)"
HOLDS_A_TAG = "holds a YAML tag (such as !!str)"
TOO_MANY_NODES = f"holds more than {MOST_NODES:,} values, lists and mappings"
TOO_DEEP = f"nests lists and mappings more than {MOST_DEPTH} deep"
MORE_THAN_ONE_DOCUMENT = "holds more than one YAML document"
CANNOT_BE_BUILT = "holds a value YAML cannot build, such as the date 2026-99-99"
UNHASHABLE_KEY = "holds a list or mapping as a key"
FRONTMATTER_TOO_LONG = f"is longer than {MOST_FRONTMATTER_CHARACTERS // 1024} KB"
UNREADABLE = "could not be read as YAML"

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_MERGE_KEY = "<<"
_RESOLVER = yaml.resolver.Resolver()
_CONSTRUCTOR = yaml.constructor.SafeConstructor()
_TEXT_TAG = _RESOLVER.DEFAULT_SCALAR_TAG
# A plain `=` key, which YAML's safe loader reads as the text "=".
_VALUE_TAG = "tag:yaml.org,2002:value"


class NotRead(Exception):
    """The text holds what Relay does not read. The message is why."""


def frontmatter(text: str) -> dict:
    """The YAML frontmatter at the top of a markdown text, {} when it has
    none. Raises NotRead, never YAML's errors, when it has one that is not
    read, is not YAML or is not a mapping."""
    match = _FRONTMATTER.match(text)
    if not match:
        return {}
    if len(match.group(1)) > MOST_FRONTMATTER_CHARACTERS:
        raise NotRead(FRONTMATTER_TOO_LONG)
    try:
        written = load(match.group(1))
    except yaml.YAMLError as problem:
        raise NotRead(f"is not valid YAML ({problem.__class__.__name__})") from None
    if written is None:
        return {}
    if not isinstance(written, dict):
        raise NotRead(f"is a {type(written).__name__}, not a mapping")
    return written


def load(text: str):
    """The one YAML document in the text, as YAML's safe loader builds it;
    None for an empty one. Raises NotRead for what is not read and
    yaml.YAMLError for text that is not YAML. The caller limits the size."""
    stream = events(text)
    written, documents = None, 0
    try:
        for event in stream:
            if isinstance(event, yaml.DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise NotRead(MORE_THAN_ONE_DOCUMENT)
            elif isinstance(event, yaml.NodeEvent):
                written = _built(event, stream)
    except (NotRead, yaml.YAMLError):
        raise
    except Exception as problem:  # anything else YAML's parser raises
        raise NotRead(UNREADABLE) from problem
    return written


def events(text: str):
    """The parser's events for the text, each one looked at before it is
    handed on. Raises NotRead at the first that is not read."""
    nodes, depth = 0, 0
    for event in yaml.parse(text, Loader=yaml.SafeLoader):
        if isinstance(event, yaml.NodeEvent):
            nodes += 1
            _refuse_what_is_not_read(event, nodes)
        if isinstance(event, yaml.CollectionStartEvent):
            depth += 1
            if depth > MOST_DEPTH:
                raise NotRead(TOO_DEEP)
        elif isinstance(event, yaml.CollectionEndEvent):
            depth -= 1
        yield event


def _refuse_what_is_not_read(node, nodes_so_far: int) -> None:
    if nodes_so_far > MOST_NODES:
        raise NotRead(TOO_MANY_NODES)
    # An alias carries the name it points at as its anchor.
    if node.anchor is not None or _is_merge_key(node):
        raise NotRead(HOLDS_A_REFERENCE)
    if node.tag is not None:
        raise NotRead(HOLDS_A_TAG)


def _is_merge_key(event) -> bool:
    """An unquoted `<<`, which YAML reads as "merge the mapping that
    follows into this one" and never as text."""
    return isinstance(event, yaml.ScalarEvent) and event.implicit[0] and event.value == _MERGE_KEY


def _built(node, stream, is_key: bool = False):
    """The value a node holds, a list's or a mapping's read from the
    events that follow its start. Nesting is bounded by MOST_DEPTH."""
    if isinstance(node, yaml.ScalarEvent):
        return _scalar(node, is_key)
    if isinstance(node, yaml.SequenceStartEvent):
        return _list(stream)
    if isinstance(node, yaml.MappingStartEvent):
        return _mapping(stream)
    raise NotRead(UNREADABLE)


def _list(stream) -> list:
    items = []
    for item in stream:
        if isinstance(item, yaml.SequenceEndEvent):
            return items
        items.append(_built(item, stream))
    raise NotRead(UNREADABLE)


def _mapping(stream) -> dict:
    """Keys and values; the last of a key written twice counts, as YAML's
    safe loader does."""
    mapping = {}
    for key_event in stream:
        if isinstance(key_event, yaml.MappingEndEvent):
            return mapping
        key = _built(key_event, stream, is_key=True)
        if isinstance(key, (list, dict)):
            raise NotRead(UNHASHABLE_KEY)
        mapping[key] = _built(next(stream), stream)
    raise NotRead(UNREADABLE)


def _scalar(scalar, is_key: bool):
    """A scalar built by YAML's safe loader for the kind YAML reads it as."""
    tag = _RESOLVER.resolve(yaml.ScalarNode, scalar.value, scalar.implicit)
    if is_key and tag == _VALUE_TAG:
        tag = _TEXT_TAG
    construct = _CONSTRUCTOR.yaml_constructors.get(tag, _CONSTRUCTOR.yaml_constructors[None])
    try:
        return construct(_CONSTRUCTOR, yaml.ScalarNode(tag, scalar.value, style=scalar.style))
    except yaml.YAMLError:
        raise
    except Exception as problem:  # a date that does not exist, an integer too long
        raise NotRead(CANNOT_BE_BUILT) from problem
