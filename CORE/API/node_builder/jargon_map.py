"""Curated jargon CROSSOVER table for node-wizard discovery.

The candidate pool is the ceiling on discovery quality, and the hardest gap is
JARGON: a user says "make a 3d model from photos" but the canonical tool is filed
under "photogrammetry"; "transcribe audio" -> "speech recognition"; "remove
background" -> "image matting/segmentation". The web/Wikipedia extraction bridges
many of these, but the highest-signal, most-reliable bridge for COMMON task
families is a hand-curated map: colloquial phrasing -> canonical domain jargon +
the canonical tool names (verified to exist on PyPI/GitHub).

This is a deliberate reliability floor for the tasks people actually ask for. The
long tail is still handled by the live web/Wikipedia extraction; this table just
guarantees the common families never miss. Entries are cheap to add.

`crossover_for(description)` returns the merged jargon + seed github repos + pypi
packages for every entry whose `match` keywords appear in the description.
Deterministic, no network.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# SPDX licences verified from each repo's ACTUAL LICENSE file, for curated repos
# where GitHub's licence API returns NOASSERTION/null (a non-standard LICENSE
# filename or a wrapper README defeats GitHub's auto-detection) even though the
# project is permissively licensed. `describe-search` backfills license_spdx from
# this map before the repo-safety gate, so a GitHub detection MISS doesn't
# silently drop a human-verified tool (e.g. COLMAP / Meshroom, the canonical
# photogrammetry tools, both report NOASSERTION).
#
# ONLY verified-permissive entries belong here — a genuinely copyleft repo is
# deliberately ABSENT so the licence gate still blocks it. Verified 2026-08-30
# from each repo's LICENSE text:
#   colmap/colmap        -> "licensed under the new BSD license"  -> BSD-3-Clause
#   alicevision/meshroom -> "licensed under the MPL2 license"     -> MPL-2.0
# (cdcseacave/openMVS is AGPL-3.0 and is intentionally NOT here — the gate
#  should keep blocking it.)
_KNOWN_LICENSES: Dict[str, str] = {
    "colmap/colmap": "BSD-3-Clause",
    "alicevision/meshroom": "MPL-2.0",
}


def known_license_for(full_name: str) -> Optional[str]:
    """Verified SPDX for a curated repo GitHub can't auto-detect, else None.

    Case-insensitive on ``owner/repo``. Used only to backfill a null/NOASSERTION
    licence for a repo we've hand-verified as permissive — never to override a
    licence GitHub actually reported.
    """
    return _KNOWN_LICENSES.get((full_name or "").strip().lower())


# Function words to ignore when matching a keyword phrase against a description,
# so "resize an image" still matches the "resize image" keyword.
_ARTICLES = {
    "a", "an", "the", "of", "from", "to", "into", "in", "on", "for", "with",
    "and", "or", "your", "my", "using", "as", "by", "at", "set",
}

# Each entry:
#   match  — substrings; if ANY appears in the (lowercased) description, it fires
#   jargon — canonical domain terms to ALSO search on GitHub (the bridge)
#   github — canonical "owner/repo"s to seed directly (verified real)
#   pypi   — canonical package names to seed/verify directly
_CROSSOVERS: List[Dict[str, List[str]]] = [
    # ---- audio ----
    {"match": ["transcribe", "speech to text", "speech-to-text", "audio to text", "transcription", "dictation", "podcast to text", "podcast transcript", "meeting notes"],
     "jargon": ["speech recognition", "automatic speech recognition", "asr"],
     "github": ["openai/whisper", "guillaumekln/faster-whisper", "alphacep/vosk-api"],
     "pypi": ["openai-whisper", "faster-whisper", "SpeechRecognition", "vosk"]},
    {"match": ["text to speech", "text-to-speech", "speech synthesis", "tts", "voice over", "narrat", "read aloud", "audiobook"],
     "jargon": ["speech synthesis", "text to speech", "voice cloning"],
     "github": ["coqui-ai/TTS", "rhasspy/piper", "suno-ai/bark"],
     "pypi": ["TTS", "piper-tts", "gTTS", "pyttsx3"]},
    {"match": ["separate vocals", "isolate vocals", "remove vocals", "stem separation", "split song", "karaoke"],
     "jargon": ["music source separation", "stem separation"],
     "github": ["deezer/spleeter", "facebookresearch/demucs", "sigsep/open-unmix-pytorch"],
     "pypi": ["spleeter", "demucs"]},
    {"match": ["extract audio", "video to audio", "rip audio", "audio from video"],
     "jargon": ["audio extraction", "multimedia"],
     "github": ["Zulko/moviepy", "kkroening/ffmpeg-python"],
     "pypi": ["moviepy", "ffmpeg-python", "pydub"]},
    # ---- image ----
    {"match": ["remove background", "background removal", "cut out", "transparent background"],
     "jargon": ["image matting", "background removal", "salient object detection"],
     "github": ["danielgatis/rembg", "xuebinqin/U-2-Net"],
     "pypi": ["rembg", "backgroundremover"]},
    {"match": ["upscale", "super resolution", "super-resolution", "enhance resolution", "increase resolution"],
     "jargon": ["super resolution", "image super-resolution"],
     "github": ["xinntao/Real-ESRGAN", "TencentARC/GFPGAN", "JingyunLiang/SwinIR"],
     "pypi": ["realesrgan", "basicsr"]},
    {"match": ["detect faces", "face detection", "facial recognition", "face recognition", "blur face", "anonymize face", "pixelate face"],
     "jargon": ["face detection", "face recognition", "facial landmark"],
     "github": ["ageitgey/face_recognition", "serengil/deepface", "davisking/dlib"],
     "pypi": ["face-recognition", "deepface", "mediapipe", "dlib"]},
    {"match": ["colorize", "colorization", "black and white", "colourize"],
     "jargon": ["image colorization", "colorization"],
     "github": ["jantic/DeOldify", "richzhang/colorization"],
     "pypi": ["deoldify"]},
    {"match": ["ocr", "scanned document", "text from image", "read text from", "recognize text"],
     "jargon": ["optical character recognition", "text recognition", "document ai"],
     "github": ["tesseract-ocr/tesseract", "JaidedAI/EasyOCR", "PaddlePaddle/PaddleOCR"],
     "pypi": ["pytesseract", "easyocr", "paddleocr", "doctr"]},
    {"match": ["resize image", "crop image", "watermark", "image filter", "thumbnail", "convert image"],
     "jargon": ["image processing", "imaging"],
     "github": ["python-pillow/Pillow", "opencv/opencv-python"],
     "pypi": ["Pillow", "opencv-python", "wand"]},
    {"match": ["heic", "heif"],
     "jargon": ["heif", "high efficiency image format"],
     "github": ["carsales/pyheif", "bigcat88/pillow_heif"],
     "pypi": ["pillow-heif", "pyheif"]},
    # ---- generative / diffusion ----
    {"match": ["text to image", "text-to-image", "image from text", "generate image", "ai art", "ai image"],
     "jargon": ["diffusion model", "text-to-image", "stable diffusion"],
     "github": ["huggingface/diffusers", "CompVis/stable-diffusion", "AUTOMATIC1111/stable-diffusion-webui"],
     "pypi": ["diffusers", "transformers"]},
    {"match": ["image to image", "style transfer", "inpaint", "outpaint"],
     "jargon": ["diffusion", "image-to-image", "neural style transfer"],
     "github": ["huggingface/diffusers"], "pypi": ["diffusers"]},
    # ---- 3d ----
    {"match": ["3d model from photo", "3d from photo", "photo to 3d", "mesh from photo", "scan to 3d",
               "3d reconstruction", "photogrammetr",
               "model from photograph", "photograph to 3d", "3d from photograph", "mesh from photograph"],
     "jargon": ["photogrammetry", "structure from motion", "multi-view stereo"],
     "github": ["colmap/colmap", "alicevision/meshroom", "openMVG/openMVG", "cdcseacave/openMVS"],
     "pypi": ["pycolmap"]},
    {"match": ["nerf", "neural radiance", "gaussian splatting"],
     "jargon": ["neural radiance field", "gaussian splatting"],
     "github": ["nerfstudio-project/nerfstudio"], "pypi": ["nerfstudio"]},
    {"match": ["decimate", "simplify mesh", "reduce polygons", "retopolog"],
     "jargon": ["mesh decimation", "mesh simplification"],
     "github": ["Wuziyi616/IF-Defense"], "pypi": ["pymeshlab", "open3d", "trimesh"]},
    # ---- video ----
    {"match": ["detect objects", "object detection", "object tracking", "count objects", "count cars", "count people", "counting"],
     "jargon": ["object detection", "object tracking"],
     "github": ["ultralytics/ultralytics", "facebookresearch/detectron2"],
     "pypi": ["ultralytics", "supervision"]},
    {"match": ["compress video", "transcode", "convert video", "mov to mp4", "video format"],
     "jargon": ["video transcoding", "ffmpeg"],
     "github": ["kkroening/ffmpeg-python", "Zulko/moviepy"],
     "pypi": ["ffmpeg-python", "moviepy"]},
    {"match": ["thumbnail from video", "video thumbnail", "video frame"],
     "jargon": ["video frame extraction"],
     "github": ["Zulko/moviepy"], "pypi": ["moviepy", "opencv-python"]},
    # ---- text / nlp ----
    {"match": ["summarize", "summarisation", "summary of"],
     "jargon": ["text summarization", "abstractive summarization"],
     "github": ["miso-belica/sumy", "huggingface/transformers"],
     "pypi": ["sumy", "transformers"]},
    {"match": ["sentiment", "emotion of text", "opinion mining"],
     "jargon": ["sentiment analysis", "text classification"],
     "github": ["cjhutto/vaderSentiment", "sloria/TextBlob"],
     "pypi": ["vaderSentiment", "textblob", "transformers"]},
    {"match": ["translate", "translation", "translator"],
     "jargon": ["machine translation", "neural machine translation"],
     "github": ["argosopentech/argos-translate", "LibreTranslate/LibreTranslate", "UKPLab/EasyNMT"],
     "pypi": ["argostranslate", "deep-translator", "transformers"]},
    {"match": ["named entity", "ner", "extract entities", "part of speech", "tokenize"],
     "jargon": ["natural language processing", "named entity recognition"],
     "github": ["explosion/spaCy", "nltk/nltk"], "pypi": ["spacy", "nltk", "stanza"]},
    {"match": ["language detect", "detect language"],
     "jargon": ["language identification"],
     "github": ["pemistahl/lingua-py"], "pypi": ["langdetect", "lingua-language-detector"]},
    # ---- documents / data ----
    {"match": ["pdf to image", "pdf to png", "pdf to jpg", "rasterize pdf", "render pdf"],
     "jargon": ["pdf rendering", "pdfium"],
     "github": ["pypdfium2-team/pypdfium2"], "pypi": ["pypdfium2", "pdf2image"]},
    {"match": ["merge pdf", "split pdf", "combine pdf", "pdf pages"],
     "jargon": ["pdf manipulation"],
     "github": ["py-pdf/pypdf", "pikepdf/pikepdf"], "pypi": ["pypdf", "pikepdf"]},
    {"match": ["extract text from pdf", "pdf text", "parse pdf", "read pdf"],
     "jargon": ["pdf text extraction"],
     "github": ["jsvine/pdfplumber", "pymupdf/PyMuPDF"], "pypi": ["pdfplumber", "pdfminer.six"]},
    {"match": ["invoice", "receipt", "document extraction", "form extraction", "key value from document"],
     "jargon": ["document understanding", "information extraction", "document ai"],
     "github": ["invoice-x/invoice2data"], "pypi": ["invoice2data", "donut-python"]},
    {"match": ["csv to json", "json to csv", "excel to csv", "convert csv", "parse csv", "spreadsheet"],
     "jargon": ["data wrangling", "tabular data"],
     "github": ["pandas-dev/pandas"], "pypi": ["pandas", "tablib"]},
    {"match": ["scrape", "crawl", "web scraping", "extract from website"],
     "jargon": ["web scraping", "html parsing"],
     "github": ["scrapy/scrapy"], "pypi": ["scrapy", "beautifulsoup4", "playwright"]},
    # ---- codes ----
    {"match": ["qr code", "qrcode"],
     "jargon": ["qr code generation"],
     "github": ["lincolnloop/python-qrcode"], "pypi": ["qrcode", "segno"]},
    {"match": ["barcode", "read barcode", "scan barcode"],
     "jargon": ["barcode reading", "barcode detection"],
     "github": ["NaturalHistoryMuseum/pyzbar"], "pypi": ["pyzbar", "python-barcode"]},
    # ---- ML classify / embed ----
    {"match": ["classify image", "image classification", "recognize image"],
     "jargon": ["image classification", "computer vision"],
     "github": ["huggingface/pytorch-image-models"], "pypi": ["timm", "transformers"]},
    {"match": ["embedding", "semantic search", "vector search", "similarity search"],
     "jargon": ["sentence embeddings", "semantic search"],
     "github": ["UKPLab/sentence-transformers"], "pypi": ["sentence-transformers", "faiss-cpu"]},
]


def _merge(dst: Dict[str, List[str]], src: Dict[str, List[str]]) -> None:
    for k in ("jargon", "github", "pypi"):
        seen = {x.lower() for x in dst[k]}
        for v in src.get(k, []) or []:
            if v and v.lower() not in seen:
                dst[k].append(v)
                seen.add(v.lower())


def _norm_set(text: str) -> set:
    """Word set of `text` with each token in both singular and plural form, so
    matching is insensitive to plurals ("photo"/"photos")."""
    ws: set = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        ws.add(w)
        ws.add(w[:-1] if w.endswith("s") else w + "s")
    return ws


def _phrase_words(phrase: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", phrase.lower()) if w not in _ARTICLES]


def crossover_for(description: str) -> Dict[str, List[str]]:
    """Merged {jargon, github, pypi} for every crossover entry that matches.

    An entry matches when ANY of its `match` keywords either appears as a raw
    substring (for distinctive tokens like "photogrammetr", "heic", "qr code")
    OR has all of its content words present in the description (article/plural-
    insensitive, so "resize an image" matches "resize image" and "make a 3d model
    from a set of photos" matches "3d model from photo"). Empty when nothing hits."""
    d = (description or "").lower()
    out: Dict[str, List[str]] = {"jargon": [], "github": [], "pypi": []}
    if not d:
        return out
    dset = _norm_set(d)
    for entry in _CROSSOVERS:
        fired = False
        for kw in entry["match"]:
            if kw in d:
                fired = True
                break
            pw = _phrase_words(kw)
            # The all-content-words-present rule is for MULTI-word phrases whose
            # articles/order vary ("resize an image" == "resize image"). A phrase
            # that reduces to a SINGLE common content word must NOT fire on that
            # lone word appearing anywhere — that let "convert image to grayscale"
            # match the diffusion "image to image" entry (which reduces to just
            # {"image"}) and flooded diffusers to the top (repro 2026-09-03).
            # Single-word triggers still match via the substring rule above, so
            # distinctive ones ("watermark", "ocr", "heic") are unaffected.
            if len(set(pw)) >= 2 and all(w in dset for w in pw):
                fired = True
                break
        if fired:
            _merge(out, entry)
    return out
