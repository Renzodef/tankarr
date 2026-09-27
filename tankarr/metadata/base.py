from __future__ import annotations

import abc
import html
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any


class MetadataSourceError(RuntimeError):
    """A metadata source could not complete a request."""


def clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def normalized_title(value: object) -> str:
    text = unicodedata.normalize("NFKD", clean_text(value)).casefold()
    text = "".join(
        character for character in text if not unicodedata.combining(character)
    )
    text = text.replace("&", " and ").replace("+", " plus ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


_EDITION_LANGUAGE_TOKENS = frozenset(
    {
        "arabic",
        "bengali",
        "chinese",
        "czech",
        "danish",
        "dutch",
        "en",
        "english",
        "filipino",
        "finnish",
        "fr",
        "french",
        "german",
        "greek",
        "hebrew",
        "hindi",
        "hungarian",
        "indonesian",
        "it",
        "italian",
        "japanese",
        "korean",
        "malay",
        "norwegian",
        "polish",
        "portuguese",
        "romanian",
        "ru",
        "russian",
        "spanish",
        "swedish",
        "thai",
        "turkish",
        "ukrainian",
        "vietnamese",
    }
)


def _fold_romanized_title(text: str) -> str:
    """Fold the long-vowel spellings of a romanized title.

    Only the vowel pairs romanization disagrees about ("Shoujo"/"Shōjo",
    "Yuu"/"Yū"); an English word keeps its doubled vowels, so unrelated
    titles cannot collapse into one another.
    """

    return re.sub(r"uu", "u", re.sub(r"(?:ou|oo)", "o", text))


def work_title_variants(value: object) -> set[str]:
    """Return conservative work-title forms for translated edition labels.

    Catalogues sometimes model ``Title English Edition`` as the only record
    available for the underlying work.  The language/edition suffix is not
    part of that work's identity, but arbitrary subtitles must remain intact.
    """

    normalized = normalized_title(value)
    if not normalized:
        return set()
    # Sources write the same title with different spacing and romanization
    # ("Neko Kappa"/"Nekokappa", "Shōjo"/"Shoujo"). Those are spellings of one
    # title, not different works, so every form is a variant of it.
    folded = _fold_romanized_title(normalized)
    variants = {normalized, folded, folded.replace(" ", "")}
    tokens = normalized.split()
    if len(tokens) < 3 or tokens[-1] != "edition":
        return variants

    suffix_length = 0
    if tokens[-2] in _EDITION_LANGUAGE_TOKENS:
        suffix_length = 2
        if len(tokens) > 3 and tokens[-3] == "official":
            suffix_length = 3
    elif (
        len(tokens) > 3
        and tokens[-2] == "language"
        and tokens[-3] in _EDITION_LANGUAGE_TOKENS
    ):
        suffix_length = 3
        if len(tokens) > 4 and tokens[-4] == "official":
            suffix_length = 4

    if suffix_length and len(tokens) > suffix_length:
        variants.add(" ".join(tokens[:-suffix_length]))
    return variants


def subtitle_prefix(value: object) -> str:
    """The title before an explanatory subtitle, when the edition adds one.

    English editions often expand a title ("Igaguri" → "Igaguri: Young Judo
    Master"). The prefix alone is weaker evidence than a full title match, so
    the matcher scores it below an exact title and still needs corroboration.
    """

    raw = clean_text(value)
    for separator in (":", " - ", " – ", " — "):
        head, found, tail = raw.partition(separator)
        if found and normalized_title(head) and normalized_title(tail):
            prefix = normalized_title(head)
            return prefix if prefix != normalized_title(raw) else ""
    return ""


def normalized_person(value: object) -> tuple[str, ...]:
    """Compare both western and family-name-first creator spellings."""

    ignored = {"and", "the", "author", "artist", "story", "art"}
    return tuple(
        sorted(
            token
            for token in normalized_title(value).split()
            if token and token not in ignored
        )
    )


def _fold_romanized_person(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """Fold only common long-vowel variants used by manga catalogues.

    Providers disagree on whether names such as ``Kyouko``/``Kyoko`` and
    ``Taiyou``/``Taiyo`` spell out a Japanese long vowel.  Applying this only
    to multi-token person names, and requiring another unchanged token in the
    matcher, avoids treating arbitrary one-word pen names as aliases.
    """

    return tuple(sorted(_fold_romanized_token(token) for token in tokens))


def _fold_romanized_token(token: str) -> str:
    """Collapse the long-vowel spellings catalogues disagree about.

    ``Kyouko``/``Kyoko``, ``Yuuichi``/``Yuichi`` and ``Keiichi``/``Keichi`` are
    the same name written by different romanization conventions.
    """

    folded = re.sub(r"uu", "u", re.sub(r"(?:ou|oo)", "o", token))
    return re.sub(r"([aeiou])\1", r"\1", folded)


def _tokens_match(left: str, right: str) -> float:
    """How strongly two name tokens denote the same person's name part."""

    if left == right:
        return 1.0
    folded_left = _fold_romanized_token(left)
    folded_right = _fold_romanized_token(right)
    if folded_left == folded_right:
        return 0.96
    if (
        len(folded_left) > 3
        and len(folded_right) > 3
        and folded_left[0] == folded_right[0]
        and SequenceMatcher(None, folded_left, folded_right).ratio() >= 0.9
    ):
        # One transposed or dropped letter in a romanized name.
        return 0.92
    return 0.0


def _matched_token_pairs(
    left: tuple[str, ...], right: tuple[str, ...]
) -> tuple[int, float]:
    """Greedily pair the tokens of two names; return the count and the weakest
    pair's strength."""

    remaining = list(right)
    matched = 0
    weakest = 1.0
    for token in left:
        best_score, best_index = 0.0, -1
        for index, candidate in enumerate(remaining):
            score = _tokens_match(token, candidate)
            if score > best_score:
                best_score, best_index = score, index
        if best_index >= 0:
            remaining.pop(best_index)
            matched += 1
            weakest = min(weakest, best_score)
    return matched, weakest


def canonical_person_name(value: object) -> str:
    """Normalize catalogue names explicitly written as ``FAMILY Given``.

    The uppercase surname convention is common in manga catalogues. Reordering
    only that unambiguous form avoids guessing for ordinary names while making
    one creator consistent with western-order providers and Komga's index.
    """

    name = " ".join(clean_text(value).split())
    parts = name.split()
    first_letters = (
        "".join(character for character in parts[0] if character.isalpha())
        if parts
        else ""
    )
    if (
        len(parts) > 1
        and len(first_letters) > 1
        and parts[0].isupper()
        and any(not part.isupper() for part in parts[1:])
    ):
        return " ".join([*parts[1:], parts[0].title()])
    return name


def _creator_overlap(left: list[str], right: list[str]) -> float:
    if not left or not right:
        return 0.0
    left_names = {normalized_person(item) for item in left if normalized_person(item)}
    right_names = {normalized_person(item) for item in right if normalized_person(item)}
    if not left_names or not right_names:
        return 0.0
    if left_names & right_names:
        return 1.0
    best = 0.0
    for first in left_names:
        first_tokens = set(first)
        for second in right_names:
            second_tokens = set(second)
            if len(first) > 1 and len(second) > 1:
                first_folded = _fold_romanized_person(first)
                second_folded = _fold_romanized_person(second)
                # A shared unmodified token (normally the family name) anchors
                # the long-vowel equivalence and prevents broad phonetic guesses.
                if first_folded == second_folded and first_tokens & second_tokens:
                    best = max(best, 0.96)
            matched, weakest = _matched_token_pairs(first, second)
            shorter = min(len(first), len(second))
            if shorter > 1 and matched == shorter:
                # Every part of the shorter name appears in the longer one. A
                # local import often stores several creators in one string
                # ("Koji Aihara Kentaro Takekuma"), and one contained full name
                # identifies the creator as surely as an exact match.
                best = max(best, weakest)
            best = max(
                best,
                len(first_tokens & second_tokens) / len(first_tokens | second_tokens),
            )
    return best


def _distinct_people(values: list[str]) -> dict[tuple[str, ...], str]:
    """Return one representative for each conservatively normalized creator."""

    result: dict[tuple[str, ...], str] = {}
    for value in values:
        key = normalized_person(value)
        if key and key not in result:
            result[key] = value
    return result


def creator_name_similarity(left: object, right: object) -> float:
    """Return a conservative, order-independent similarity for two creators.

    Catalogue adapters use this when a provider exposes a creator index.  It
    deliberately shares the exact same normalization as the final matcher so
    author-first discovery cannot silently use a looser identity policy.
    """

    left_name = clean_text(left)
    right_name = clean_text(right)
    if not left_name or not right_name:
        return 0.0
    return _creator_overlap([left_name], [right_name])


@dataclass(frozen=True)
class SeriesMatchAssessment:
    confidence: float
    ranking_score: float
    reason: str
    title_relation: str
    title_similarity: float
    creator_similarity: float
    evidence: tuple[str, ...]
    contradictions: tuple[str, ...]


def _work_type_family(value: object) -> str:
    normalized = normalized_title(value).replace(" ", "_")
    if normalized in {"manga", "one_shot", "doujinshi"}:
        return "manga"
    if normalized in {"manhwa", "webtoon"}:
        return "manhwa"
    if normalized == "manhua":
        return "manhua"
    if normalized in {"comic", "comics", "graphic_novel"}:
        return "comic"
    if normalized in {"novel", "light_novel", "book"}:
        return "book"
    return normalized


def _positive_number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


# Highest confidence a match may reach while the two sides name creators that
# do not agree. Deliberately between the review floor (0.5) and the automatic
# threshold (0.86): such a match is always worth showing and never worth
# taking without being looked at.
CREATOR_DISAGREEMENT_CEILING = 0.80


def assess_series_match(
    target: dict[str, Any],
    candidate: dict[str, Any],
    *,
    exact_matches: int = 0,
) -> SeriesMatchAssessment:
    """Assess one catalogue identity using independent, auditable evidence.

    Primary-title equality is stronger than an alternate-title hit.  Creator,
    original publication year, work type, and stable counts corroborate that
    identity; contradictions reduce it.  Search rank is intentionally absent.
    """

    target_primary = normalized_title(target.get("title"))
    target_primary_variants = work_title_variants(target.get("title"))
    target_aliases = {
        variant
        for item in target.get("alternate_titles") or []
        for variant in work_title_variants(item)
    }
    candidate_primary = normalized_title(candidate.get("title"))
    candidate_subtitle_prefix = subtitle_prefix(candidate.get("title"))
    target_subtitle_prefix = subtitle_prefix(target.get("title"))
    candidate_primary_variants = work_title_variants(candidate.get("title"))
    candidate_aliases = {
        variant
        for item in [
            *(candidate.get("alternate_titles") or []),
            candidate.get("hit_title"),
        ]
        for variant in work_title_variants(item)
    }
    target_titles = {
        item for item in {*target_primary_variants, *target_aliases} if item
    }
    candidate_titles = {
        item for item in {*candidate_primary_variants, *candidate_aliases} if item
    }
    if not target_primary or not candidate_titles:
        return SeriesMatchAssessment(
            0.0,
            0.0,
            "missing title evidence",
            "missing",
            0.0,
            0.0,
            (),
            ("missing title",),
        )

    similarity = max(
        SequenceMatcher(None, left, right).ratio()
        for left in target_titles
        for right in candidate_titles
    )
    if target_primary == candidate_primary:
        title_relation, score = "primary_exact", 0.62
    elif target_primary_variants & candidate_primary_variants:
        title_relation, score = "edition_qualified_exact", 0.62
    elif candidate_primary in target_aliases:
        title_relation, score = "candidate_primary_matches_alias", 0.60
    elif target_primary in candidate_aliases:
        title_relation, score = "primary_matches_candidate_alias", 0.60
    elif target_aliases & candidate_aliases:
        title_relation, score = "alias_exact", 0.56
    elif target_primary and target_primary == candidate_subtitle_prefix:
        # As strong as an alias hit: the edition kept the work's title and
        # added an explanation, so the creator still has to corroborate it.
        title_relation, score = "candidate_subtitle_expands_primary", 0.60
    elif candidate_primary and candidate_primary == target_subtitle_prefix:
        title_relation, score = "primary_subtitle_expands_candidate", 0.60
    elif similarity >= 0.96:
        # A spelling/romanization variant plus the same creator is enough to
        # identify a work without depending on publication-specific dates.
        title_relation, score = "near_exact", 0.60
    elif similarity >= 0.90:
        title_relation, score = "similar", 0.40
    else:
        title_relation, score = "weak", min(0.36, similarity * 0.40)

    evidence = [title_relation.replace("_", " ")]
    contradictions: list[str] = []
    if (
        title_relation in {"primary_exact", "edition_qualified_exact"}
        and exact_matches == 1
    ):
        score += 0.24
        evidence.append("unique work-title result")

    # Most Suwayomi extensions expose no author at all. Demanding creator
    # corroboration from a source that cannot provide it turns every exact
    # title into a question for the operator - nobody should have to confirm
    # that "Galaxy Express 999" is "Galaxy Express 999". When the candidate
    # carries no creator to check, an exact title stands on its own; a rival
    # with an equally exact title still makes the match ambiguous, and that
    # is decided by the margin rule rather than here.
    # An alias counts too, but only a *canonical* one: the catalogue records
    # "Harlock Saga" and "Nibelung no Yubiwa" as names of Der Ring des
    # Nibelungen, so a source using one has identified the work, not merely
    # resembled it. A title that is only *similar* gets nothing here.
    if (
        title_relation
        in {
            "primary_exact",
            "edition_qualified_exact",
            "candidate_primary_matches_alias",
            "primary_matches_candidate_alias",
            "alias_exact",
        }
        and not (candidate.get("authors") or [])
        # Only when the title picks out one candidate. Two results carrying
        # the same exact title (a work and its other-language edition, or two
        # homonyms) cannot be told apart by the title, and that is precisely
        # when a human has something to add.
        and exact_matches <= 1
    ):
        # Worth exactly what a matching creator is worth: when the source
        # cannot name an author, a title the catalogue itself records for
        # this work is the strongest evidence obtainable, and demanding more
        # only produces questions nobody can answer better than this.
        score += 0.26
        evidence.append("catalogue title, source names no creator")

    creator_similarity = _creator_overlap(
        list(target.get("authors") or []), list(candidate.get("authors") or [])
    )
    if creator_similarity >= 0.99:
        score += 0.26
        evidence.append("creator")
    elif creator_similarity >= 0.90:
        score += 0.26
        evidence.append("romanization-equivalent creator")
    elif creator_similarity >= 0.50:
        score += 0.12
        evidence.append("partial creator")
    elif creator_similarity >= 0.30:
        score += 0.06
        evidence.append("shared creator surname")
    elif target.get("authors") and candidate.get("authors"):
        score -= 0.08
        contradictions.append("creator disagreement")

    # An anthology/collection can expose a contained story as an alternate
    # title. One matching contributor must not turn the parent publication
    # into the identity of that contributor's individual work. This is a
    # provider-independent guard: it uses the shape of the evidence rather
    # than a title, source ID, or catalogue-specific allow/deny list.
    target_people = _distinct_people(list(target.get("authors") or []))
    candidate_people = _distinct_people(list(candidate.get("authors") or []))
    matched_candidate_people = {
        candidate_key
        for candidate_key, candidate_name in candidate_people.items()
        if any(
            creator_name_similarity(target_name, candidate_name) >= 0.90
            for target_name in target_people.values()
        )
    }
    alias_only_relation = title_relation in {
        "candidate_primary_matches_alias",
        "primary_matches_candidate_alias",
        "alias_exact",
    }
    broader_creator_set = (
        alias_only_relation
        and bool(target_people)
        and len(candidate_people) >= 4
        and len(candidate_people) >= len(target_people) * 3
        and len(matched_candidate_people) * 2 < len(candidate_people)
    )
    if broader_creator_set:
        score -= 0.55
        contradictions.append(
            "alternate title belongs to a broader multi-creator work "
            f"({len(matched_candidate_people)}/{len(candidate_people)} creators match)"
        )

    target_year = _positive_number(target.get("year"))
    candidate_year = _positive_number(candidate.get("year"))
    if target_year is not None and candidate_year is not None:
        delta = abs(target_year - candidate_year)
        if delta <= 1:
            score += 0.13
            evidence.append("original year")
        elif delta <= 3:
            score += 0.08
            evidence.append("near original year")
        elif delta <= 5:
            score += 0.04
            evidence.append("plausible original year")
        elif str(candidate.get("catalogue_scope") or "work") == "work":
            penalty = 0.12 if delta > 15 else 0.07
            score -= penalty
            contradictions.append("original year disagreement")

    target_type = _work_type_family(target.get("work_type"))
    candidate_type = _work_type_family(candidate.get("work_type"))
    candidate_scope = str(candidate.get("catalogue_scope") or "work")
    if target_type and candidate_type:
        if target_type == candidate_type:
            score += 0.08
            evidence.append("work type")
        elif candidate_scope == "work":
            score -= 0.18
            contradictions.append("work type disagreement")

    # How long the work is, compared with how long the candidate is. Sources
    # that expose no author and no year - most Suwayomi extensions - leave a
    # title-only match stuck below the threshold forever, and every one of
    # those becomes a question for the operator. Length is the one fact such a
    # source always knows, and two works sharing an alias *and* a length is
    # far less likely than sharing an alias alone, so a tight agreement is
    # allowed to carry a match on its own.
    #
    # It never subtracts: a source holding three chapters of a 55-chapter work
    # is that work, only incomplete. Disagreement means "no evidence here",
    # not "wrong work" - measured on this library, MangaDex reported 3 of 55
    # chapters for a title it carried correctly.
    exact_or_alias = title_relation in {
        "primary_exact",
        "edition_qualified_exact",
        "candidate_primary_matches_alias",
        "primary_matches_candidate_alias",
        "alias_exact",
        "near_exact",
    }
    count_bonus = 0.0
    count_label = ""
    for field in ("volume_count", "chapter_count"):
        left = _positive_number(target.get(field))
        right = _positive_number(candidate.get(field))
        if left is None or right is None:
            continue
        relative_delta = abs(left - right) / max(left, right)
        # Sources carry split parts, omake and season prologues the catalogue
        # does not count, so "the same length" is a band, not an equality:
        # 55 canonical chapters against 58 on the source is the same work.
        if abs(left - right) <= 1 or relative_delta <= 0.10:
            # As strong as a creator match: the title names the work and its
            # length confirms it, which is what a human would check.
            count_bonus = max(count_bonus, 0.26 if exact_or_alias else 0.10)
            count_label = f"same {field.split('_')[0]} count"
        elif relative_delta <= 0.15:
            count_bonus = max(count_bonus, 0.08)
            count_label = count_label or f"close {field.split('_')[0]} count"
        elif right > max(left * 3, left + 10):
            # Fewer chapters than the work has is an incomplete source;
            # several times MORE is another work wearing its title. Katsuhiro
            # Otomo's Hansel & Gretel was filed under Junko Mizuno's because
            # the source named no creator and its 22 chapters against the
            # work's 1 cost the match nothing.
            score -= 0.30
            contradictions.append(
                f"source carries {right:.0f} {field.split('_')[0]}s "
                f"where the work has {left:.0f}"
            )
    if count_bonus:
        score += count_bonus
        evidence.append(count_label)

    # Two works can share a title; they cannot share a title *and* have
    # different creators and still be one work. When both sides name authors
    # and none of them agree, that is the strongest refutation of identity
    # obtainable, and it has to outrank the title evidence rather than cost a
    # few hundredths of it: Yuuichi Yokoyama's "Garden" was mapped to another
    # work of the same name at 0.99 - exact title, unique result, matching
    # length - and a whole different comic was downloaded under it.
    #
    # The cap leaves the match above the review floor instead of discarding
    # it. A source naming its scanlation group where the catalogue names the
    # mangaka is still probably right, and that is a question with an answer
    # a human has; it is not a fact the score may assume.
    if "creator disagreement" in contradictions or any(
        "where the work has" in item for item in contradictions
    ):
        score = min(score, CREATOR_DISAGREEMENT_CEILING)
    confidence = round(max(0.0, min(0.99, score)), 3)
    ranking_score = round(score, 3)
    reason = ", ".join(evidence)
    if contradictions:
        reason += "; conflicts: " + ", ".join(contradictions)
    return SeriesMatchAssessment(
        confidence,
        ranking_score,
        reason,
        title_relation,
        round(similarity, 3),
        round(creator_similarity, 3),
        tuple(evidence),
        tuple(contradictions),
    )


def score_series_match(
    target: dict[str, Any], candidate: dict[str, Any], *, exact_matches: int
) -> tuple[float, str]:
    assessment = assess_series_match(target, candidate, exact_matches=exact_matches)
    return assessment.confidence, assessment.reason


class MetadataSource(abc.ABC):
    name = "metadata"
    label = "Metadata"
    supports_series = True
    supports_volumes = False
    # MAL can return every identity field in its search response. Other
    # catalogues are conservatively re-fetched before a match is accepted.
    search_results_complete_for_matching = False
    identity_search_queries = 1
    supports_author_lookup = False
    # Some catalogues permit exact-ID lookup but not dependable discovery.
    # They remain available for manual correlations without joining automatic
    # title searches or pre-add completeness checks.
    automatic_matching = True
    allows_link_only_correlation = False

    @property
    @abc.abstractmethod
    def configured(self) -> bool: ...

    @property
    def unavailable_reason(self) -> str | None:
        return None

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "configured": self.configured,
            "supports_series": self.supports_series,
            "supports_volumes": self.supports_volumes,
            "supports_author_lookup": self.supports_author_lookup,
            "automatic_matching": self.automatic_matching,
            "allows_link_only": self.allows_link_only_correlation,
            "unavailable_reason": self.unavailable_reason,
        }

    @abc.abstractmethod
    async def search_series(
        self, query: str, limit: int = 10
    ) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    async def get_series(self, external_id: str) -> dict[str, Any]: ...

    async def search_volume(
        self,
        series: dict[str, Any],
        volume: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return []

    async def search_series_by_author(
        self, author: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return works from an exact creator identity when the API supports it."""

        return []

    async def aclose(self) -> None:
        return None
