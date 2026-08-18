from pathlib import Path


def test_protected_image_lightbox_does_not_resign_signed_url():
    source = (Path(__file__).parents[2] / "index.html").read_text(encoding="utf-8")
    lightbox = source[
        source.index("function openLightbox(url)"):
        source.index("function closeLightbox()")
    ]

    assert "secureMediaUrl(url).then(showLightbox)" in lightbox
    assert ".then(openLightbox)" not in lightbox
    assert "function showLightbox(url)" in lightbox
