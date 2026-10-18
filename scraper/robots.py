"""Robots rules with RFC 9309 wildcard, end-marker and longest-match support.

Only parsing and matching happen here; fetching and caching belong to the
shared scraping policy. Crawl-delay and Request-rate use the stdlib parser.
"""

import re
from fractions import Fraction
from urllib.parse import quote, urlsplit
from urllib.robotparser import RobotFileParser


_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PERCENT = re.compile(r"%([0-9a-fA-F]{2})")


def _normalize(value):
    # Keep encoded reserved characters distinct from URL delimiters. Literal
    # '*' and '$' in a URL must also match their encoded rule equivalents.
    value = quote(value, safe="/?:@!&'()+,;=-._~%[]")

    def octet(match):
        character = chr(int(match.group(1), 16))
        return character if character in _UNRESERVED else "%" + match.group(1).upper()

    return _PERCENT.sub(octet, value)


def _matches(parts, anchored, path):
    """Match '*' segments without a potentially exponential regular expression."""
    if len(parts) == 1:
        return path == parts[0] if anchored else path.startswith(parts[0])
    if not path.startswith(parts[0]):
        return False
    position = len(parts[0])
    for part in parts[1:-1]:
        found = path.find(part, position)
        if found < 0:
            return False
        position = found + len(part)
    last = parts[-1]
    if anchored:
        return path.endswith(last) and len(path) - len(last) >= position
    return path.find(last, position) >= 0


class RobotsRules:
    """The subset of RobotFileParser used by ScrapePolicy, without network I/O."""

    def __init__(self, url=""):
        self.url = url
        self._groups = []
        self._parsed = False

    def parse(self, lines):
        groups = []
        agents, records = [], []
        for raw in lines:
            line = raw.lstrip("\ufeff").split("#", 1)[0].strip()
            key, separator, value = line.partition(":")
            if not separator:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent" and value:
                if records:
                    groups.append((agents, records))
                    agents, records = [], []
                agents.append(value.lower())
            elif agents and key in ("allow", "disallow", "crawl-delay", "request-rate"):
                records.append((key, value))
            # Empty lines, comments and unrelated records do not end a group.
        if agents:
            groups.append((agents, records))

        self._groups = []
        for agents, records in groups:
            rules = []
            scheduler = RobotFileParser(self.url)
            scheduler.parse(["User-agent: *"] + [f"{key}: {value}" for key, value in records])
            for key, value in records:
                if key not in ("allow", "disallow") or not value:
                    continue
                anchored = value.endswith("$")
                pattern = value[:-1] if anchored else value
                parts = tuple(_normalize(part) for part in pattern.split("*"))
                # Wildcards and the end marker carry no literal path octets.
                specificity = sum(len(_PERCENT.sub("x", part)) for part in parts)
                rules.append((specificity, key == "allow", parts, anchored))
            self._groups.append((tuple(agents), rules, scheduler))
        self._parsed = True

    def _selected(self, useragent):
        useragent = useragent.lower()
        scored = [
            (max((len(agent) for agent in group[0] if agent != "*" and agent in useragent), default=0), group)
            for group in self._groups
        ]
        specificity = max((score for score, _ in scored), default=0)
        if specificity:
            # Merge repeated groups for the most specific matching product.
            return [group for score, group in scored if score == specificity]
        return [group for _, group in scored if "*" in group[0]]

    def can_fetch(self, useragent, url):
        if not self._parsed:
            return False
        parsed = urlsplit(url)
        path = parsed.path or "/"
        if parsed.query or "?" in url.split("#", 1)[0]:
            path += "?" + parsed.query
        path = _normalize(path)
        matches = [
            (specificity, allowed)
            for _, rules, _ in self._selected(useragent)
            for specificity, allowed, parts, anchored in rules
            if _matches(parts, anchored, path)
        ]
        # The longest rule wins; an Allow wins an equal-length conflict.
        return max(matches)[1] if matches else True

    def crawl_delay(self, useragent):
        delays = [scheduler.crawl_delay("*") for _, _, scheduler in self._selected(useragent)]
        return max((delay for delay in delays if delay is not None), default=None)

    def request_rate(self, useragent):
        rates = [scheduler.request_rate("*") for _, _, scheduler in self._selected(useragent)]
        rates = [rate for rate in rates if rate and rate.requests > 0 and rate.seconds > 0]
        return max(rates, key=lambda rate: Fraction(rate.seconds, rate.requests), default=None)
