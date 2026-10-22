"""Source-range scanner for attachment references, without rewriting prose.

This is not a general Markdown renderer. Raw pre/script/style/textarea blocks and comments remain literal. General
HTML blocks and lazy/nested list continuation semantics remain incomplete. Work and nesting are bounded by
input size and explicit limits; no recursive regex or recursive Python calls.
"""
import bisect
import re

from .client import GVError


def escaped(source, pos):
    start = pos
    while pos > 0 and source[pos - 1] == '\\':
        pos -= 1
    return (start - pos) % 2 == 1


def _label(value):
    return ' '.join(value.split()).casefold()


def _blocks(text):
    """Mask code and container prefixes while preserving source offsets."""
    view = list(text)
    lines, starts, contents = text.splitlines(keepends=True), [], []
    offset, fence, list_indent, html = 0, None, 0, None
    for line in lines:
        starts.append(offset)
        prefix = re.match(r'^(?: {0,3}>[ \t]?)*', line).end()
        quote_depth = line[:prefix].count('>')
        rest = line[prefix:]
        marker = re.match(r' {0,3}(?:[-+*]|[0-9]{1,9}[.)])[ \t]+', rest)
        if marker:
            list_indent = marker.end()
            prefix += list_indent
        elif list_indent:
            spaces = len(rest) - len(rest.lstrip(' '))
            if spaces >= list_indent:
                prefix += list_indent
            elif rest.strip():
                list_indent = 0
        rest = line[prefix:]
        contents.append(offset + prefix)
        view[offset:offset + prefix] = ' ' * prefix
        marker = re.match(r' {0,3}(`{3,}|~{3,})(.*)', rest)
        if fence and (quote_depth < fence[1] or list_indent < fence[2]):
            fence = None
        if not fence and html is None:
            raw = re.match(r' {0,3}<(pre|script|style|textarea)(?:[ >])', rest, re.I)
            if raw:
                html = '</' + raw[1].lower() + '>'
            elif re.match(r' {0,3}<!--', rest):
                html = '-->'
        if html is not None:
            view[offset:offset + len(line)] = ['\n' if c == '\n' else ' ' for c in line]
            if html in rest.lower():
                html = None
        elif fence:
            view[offset:offset + len(line)] = ['\n' if c == '\n' else ' ' for c in line]
            if (marker and marker[1][0] == fence[0][0] and len(marker[1]) >= len(fence[0])
                    and not marker[2].strip()):
                fence = None
        elif marker and (marker[1][0] != '`' or '`' not in marker[2]):
            fence = (marker[1], quote_depth, list_indent)
            view[offset:offset + len(line)] = ['\n' if c == '\n' else ' ' for c in line]
        elif rest.startswith(('    ', '\t')):
            view[offset:offset + len(line)] = ['\n' if c == '\n' else ' ' for c in line]
        offset += len(line)
    return ''.join(view), starts, contents


def _destination(source, pos):
    """Return destination bounds, including optional angle delimiters."""
    start = pos
    if pos == len(source):
        return None
    if source[pos] == '<':
        pos += 1
        while pos < len(source):
            c = source[pos]
            if c in '\r\n<':
                return None
            if c == '>':
                return start, pos + 1
            pos += 2 if c == '\\' else 1
        return None
    depth = 0
    while pos < len(source):
        c = source[pos]
        if c == '\\':
            pos += 2
            continue
        if c.isspace() or (c == ')' and not depth):
            break
        if c == '(':
            depth += 1
            if depth > 32:
                raise GVError('Markdown destination nesting exceeds 32', 'usage_error')
        elif c == ')':
            depth -= 1
        elif c == '<':
            return None
        pos += 1
    return (start, pos) if pos > start and depth == 0 else None


def _code_runs(source):
    # Exact delimiter-run matching (a longer run cannot close a shorter one).
    # A span cannot cross a blank line/block boundary. Index all runs once so
    # malformed input with many unmatched delimiters remains bounded.
    runs, paragraph = [], 0
    for m in re.finditer(r'\n[ \t]*\n|`+', source):
        if m[0][0] == '\n':
            paragraph += 1
        else:
            runs.append((m.start(), m.end(), (paragraph, len(m[0]))))
    next_run, by_key = {}, {}
    for begin, end, key in reversed(runs):
        if key in by_key:
            next_run[begin] = by_key[key]
        by_key[key] = (begin, end)
    return next_run



def references(text):
    """Return inline nodes and used definitions as source ranges.

    Bracket nesting, multiline code spans and quote/list fence containers are
    scanned separately. Definitions are indexed before uses, including forward,
    collapsed and shortcut references; the first normalized label wins.
    """
    source, starts, contents = _blocks(text)
    view = list(source)
    definitions, previous_stop = {}, 0
    code_runs = _code_runs(source)
    code_spans = []
    pos = 0
    while pos < len(source):
        if source[pos] == '\\':
            pos += 2
        elif source[pos] == '`':
            end = pos + 1
            while end < len(source) and source[end] == '`':
                end += 1
            if pos in code_runs:
                end = code_runs[pos][1]
                code_spans.append((pos, end))
            pos = end
        else:
            pos += 1
    code_starts = [a for a, _ in code_spans]
    pattern = re.compile(r'^ *\[((?:\\.|[^\[\]\\\r\n]){1,999})\]:[ \t]*(?:\r?\n[ \t]*)?', re.M)
    definition_tail = re.compile(r'[ \t]*(?:(?:\r?\n[ \t]*)?(?:"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|\((?:\\.|[^()\\])*\))[ \t]*)?(?:\r?\n|$)')
    for m in pattern.finditer(source):
        if m.start() < previous_stop:
            continue
        label_start = source.index('[', m.start(), m.end())
        span = bisect.bisect_right(code_starts, label_start) - 1
        if span >= 0 and label_start < code_spans[span][1]:
            continue
        destination = _destination(source, m.end())
        if destination is None:
            continue
        begin, end = destination
        # A definition's destination ends a line or is followed by a title.
        tail = definition_tail.match(source, end)
        if tail is None:
            continue
        stop = tail.end()
        definitions.setdefault(_label(m[1]), (begin, end, text[begin:end]))
        previous_stop = stop
        view[m.start():stop] = ['\n' if c == '\n' else ' ' for c in source[m.start():stop]]
    source = ''.join(view)
    next_run = _code_runs(source)
    inline_tail = re.compile(r'\s*(?:("(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|\((?:\\.|[^()\\])*\))\s*)?\)')
    reference_label = re.compile(r'\[((?:\\.|[^\[\]\\]){0,999})\]')
    i, stack, inline, used = 0, [], [], []
    while i < len(source):
        c = source[i]
        if c == '\\':
            i += 2
            continue
        if c == '`':
            end = i + 1
            while end < len(source) and source[end] == '`':
                end += 1
            i = next_run[i][1] if i in next_run else end
            continue
        if c == '[':
            if len(stack) >= 128:
                raise GVError('Markdown label nesting exceeds 128', 'usage_error')
            stack.append(i)
        elif c == ']' and stack:
            begin = stack.pop()
            image = begin > 0 and source[begin - 1] == '!' and not escaped(source, begin - 1)
            node_start, node_end = begin - int(image), i + 1
            label = text[begin + 1:i]
            after = i + 1
            if after < len(source) and source[after] == '(':
                pos = after + 1
                while pos < len(source) and source[pos].isspace():
                    pos += 1
                dest = _destination(source, pos)
                if dest is not None:
                    a, b = dest
                    tail = inline_tail.match(source, b)
                    if tail:
                        node_end = tail.end()
                        n = bisect.bisect_right(starts, begin) - 1
                        finish_line = bisect.bisect_right(starts, node_end - 1) - 1
                        left = source[contents[n]:node_start]
                        line_end = starts[finish_line + 1] if finish_line + 1 < len(starts) else len(source)
                        right = source[node_end:line_end]
                        before = n == 0 or not source[contents[n - 1]:starts[n]].strip()
                        after_blank = (finish_line + 1 == len(starts) or not source[contents[finish_line + 1]:
                            starts[finish_line + 2] if finish_line + 2 < len(starts) else len(source)].strip())
                        standalone = not left.strip() and not right.strip() and before and after_blank
                        inline.append((a, b, text[a:b], image, label, node_start, node_end, standalone))
                        i = node_end
                        continue
            key = _label(label)
            explicit = reference_label.match(source, after)
            if explicit:
                key = _label(explicit[1] or label)
                node_end = explicit.end()
            if key in definitions:
                a, b, destination = definitions[key]
                used.append((a, b, destination, image))
                i = node_end
                continue
        elif c == '\n' and (i + 1 == len(source) or source[i + 1] == '\n'):
            stack.clear()
        i += 1
    if len(inline) + len(used) > 10000:
        raise GVError('Markdown reference count exceeds 10000', 'usage_error')
    return inline, used
