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


def test_attachment_preview_opens_in_separate_telegram_window():
    source = (Path(__file__).parents[2] / "index.html").read_text(encoding="utf-8")
    attachment = source[
        source.index("function fileHTML(t)"):
        source.index("function loadProtectedPreview(url)")
    ]
    opener = source[
        source.index("function openImageWindow(url)"):
        source.index("// ============================ THEME")
    ]

    assert attachment.count('onclick="openImageWindow(this.dataset.full)"') == 2
    assert "if (tg && tg.openLink)" in opener
    assert "tg.openLink(signedUrl);" in opener
    assert "if (!w) showLightbox(signedUrl);" in opener
