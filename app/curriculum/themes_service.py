from .themes_config import THEMES

EXPECTED_VOCAB_IDS = set(range(3, 29))


def validate_theme_partition():
    seen_ids = set()
    duplicate_ids = set()
    all_ids = []
    unknown_ids = set()

    for theme in THEMES:
        for vocab_id in theme["vocabulary_ids"]:
            all_ids.append(vocab_id)
            if vocab_id in seen_ids:
                duplicate_ids.add(vocab_id)
            else:
                seen_ids.add(vocab_id)
            if vocab_id not in EXPECTED_VOCAB_IDS:
                unknown_ids.add(vocab_id)

    missing_ids = EXPECTED_VOCAB_IDS - seen_ids

    issues = []
    if duplicate_ids:
        issues.append(f"duplicate vocabulary IDs: {sorted(duplicate_ids)}")
    if missing_ids:
        issues.append(f"missing vocabulary IDs: {sorted(missing_ids)}")
    if unknown_ids:
        issues.append(f"unknown vocabulary IDs: {sorted(unknown_ids)}")

    if issues:
        raise ValueError("; ".join(issues))

    return True


def get_all_themes():
    return THEMES


def get_theme_by_id(theme_id: str):
    for theme in THEMES:
        if theme["theme_id"] == theme_id:
            return theme
    return None


def get_vocabularies_by_theme(theme_id: str):
    for theme in THEMES:
        if theme["theme_id"] == theme_id:
            return theme["vocabulary_ids"]
    raise KeyError(f"Theme not found: {theme_id}")


def get_theme_by_vocabulary_id(vocab_id: int):
    for theme in THEMES:
        if vocab_id in theme["vocabulary_ids"]:
            return theme
    raise KeyError(f"Vocabulary ID not found in any theme: {vocab_id}")


validate_theme_partition()
