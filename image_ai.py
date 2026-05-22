"""Perceptual hash-based duplicate image detection (lightweight AI approach)."""
import os

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

PHASH_SIZE = 8
SIMILARITY_THRESHOLD = 10


def compute_phash(image_path):
    if not HAS_PIL or not os.path.isfile(image_path):
        return None
    try:
        img = Image.open(image_path).convert("L").resize(
            (PHASH_SIZE, PHASH_SIZE), Image.Resampling.LANCZOS
        )
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        return "".join("1" if p >= avg else "0" for p in pixels)
    except OSError:
        return None


def hamming_distance(h1, h2):
    if not h1 or not h2 or len(h1) != len(h2):
        return 999
    return sum(a != b for a, b in zip(h1, h2))


def is_similar_hash(h1, h2, threshold=SIMILARITY_THRESHOLD):
    return hamming_distance(h1, h2) <= threshold


def find_similar_image_in_db(db, phash):
    if not phash:
        return None
    rows = db.execute(
        "SELECT id, image_hash, image FROM complaints WHERE image_hash IS NOT NULL AND image IS NOT NULL"
    ).fetchall()
    for row in rows:
        if row["image_hash"] and is_similar_hash(phash, row["image_hash"]):
            return row["id"]
    return None
