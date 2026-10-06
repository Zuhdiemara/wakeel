"""Text normalisation shared by search and guards, for English and Arabic."""
import re
import unicodedata

_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_ARABIC = re.compile(r"[؀-ۿ]")
_TOKEN = re.compile(r"[\w؀-ۿ]+")
_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

STOP = set("""a an the of to in on for and or is are was were be by with at from this that it as my me i you your
we our please can could would should do does did have has had not no""".split()) | set(
    "في من على الى إلى عن مع هذا هذه ذلك التي الذي و او أو ان أن كان لي انا أنا هل لا ما".split())


def normalise(s: str) -> str:
    """Lowercase, Western digits, and the usual Arabic folds: no diacritics or
    tatweel, alef forms to ا, alef maqsura to ي, taa marbuta to ه."""
    s = unicodedata.normalize("NFKC", s).translate(_INDIC).lower()
    s = _DIACRITICS.sub("", s).replace("ـ", "")
    return re.sub("[إأآٱ]", "ا", s).replace("ى", "ي").replace("ة", "ه")


def tokens(s: str) -> list[str]:
    out = []
    for t in _TOKEN.findall(normalise(s)):
        if t in STOP:
            continue
        if _ARABIC.match(t) and len(t) > 4 and t.startswith("ال"):
            t = t[2:]  # the definite article: "البطاقة" and "بطاقة" should match
        out.append(t)
    return out


def is_arabic(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return bool(letters) and sum(bool(_ARABIC.match(c)) for c in letters) / len(letters) > 0.4
