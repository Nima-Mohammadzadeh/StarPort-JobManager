from __future__ import annotations

from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from .models import ProofJob
from .settings import ROOT
from .storage import path_cache_key

MM_PER_INCH = 25.4

# Small process-local source cache. Inlay images are reused constantly while an
# operator adjusts X/Y/rotation; reopening them from a OneDrive path for every
# preview was a major source of UI stalls.
_INLAY_SOURCE_CACHE = {}


def _load_inlay_source(path: Path) -> Image.Image:
    try:
        st = path.stat()
        fingerprint = (st.st_mtime_ns, st.st_size)
    except OSError:
        fingerprint = (0, 0)
    key = path_cache_key(path)
    cached = _INLAY_SOURCE_CACHE.get(key)
    if cached and cached[0] == fingerprint:
        return cached[1].copy()
    with Image.open(path) as source:
        image = source.convert("RGBA")
    _INLAY_SOURCE_CACHE[key] = (fingerprint, image.copy())
    while len(_INLAY_SOURCE_CACHE) > 6:
        _INLAY_SOURCE_CACHE.pop(next(iter(_INLAY_SOURCE_CACHE)))
    return image


def effective_inlay_size(job: ProofJob) -> Tuple[float, float]:
    if int(job.inlay_rotation) % 180 == 90:
        return float(job.inlay_height_mm), float(job.inlay_width_mm)
    return float(job.inlay_width_mm), float(job.inlay_height_mm)


def resolve_inlay_image(raw_path: str) -> Path | None:
    raw = str(raw_path or "").strip()
    if not raw:
        return None
    path = Path(raw)
    candidates = [path]
    if not path.is_absolute():
        candidates.extend([ROOT / path, ROOT / "config" / path, ROOT / "config" / "inlay_images" / path.name])
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def placement_detail(job: ProofJob) -> Tuple[bool, str]:
    label_w = float(job.label_width) * MM_PER_INCH
    label_h = float(job.label_height) * MM_PER_INCH
    inlay_w, inlay_h = effective_inlay_size(job)
    if inlay_w <= 0 or inlay_h <= 0:
        return False, "Inlay construction dimensions are not configured."
    x = float(job.inlay_x_mm)
    y = float(job.inlay_y_mm)
    if x < 0 or y < 0:
        return False, "Inlay X/Y offsets cannot be negative."
    if x + inlay_w > label_w + 0.001 or y + inlay_h > label_h + 0.001:
        return False, (
            f"Inlay exceeds label boundary: label {label_w:.1f} x {label_h:.1f} mm; "
            f"placed inlay {inlay_w:.1f} x {inlay_h:.1f} mm at X {x:.1f}, Y {y:.1f}."
        )
    return True, (
        f"Inlay {inlay_w:.1f} x {inlay_h:.1f} mm at X {x:.1f}, Y {y:.1f} mm "
        f"inside label {label_w:.1f} x {label_h:.1f} mm."
    )


def _prepare_inlay_rgba(job: ProofJob, width_px: int, height_px: int, opacity_percent: int) -> Image.Image | None:
    source = resolve_inlay_image(job.inlay_image_path)
    if not source or width_px <= 0 or height_px <= 0:
        return None
    try:
        inlay = _load_inlay_source(source)
        rotation = int(job.inlay_rotation) % 360
        if rotation:
            # PIL positive rotation is counter-clockwise. For a conventional
            # label rotation selector, use clockwise rotation.
            inlay = inlay.rotate(-rotation, expand=True, resample=Image.Resampling.BICUBIC)
        inlay = inlay.resize((max(1, width_px), max(1, height_px)), Image.Resampling.LANCZOS)
        alpha = inlay.getchannel("A")
        factor = max(0.0, min(float(opacity_percent), 100.0)) / 100.0
        alpha = ImageEnhance.Brightness(alpha).enhance(factor)
        inlay.putalpha(alpha)
        return inlay
    except Exception:
        return None


def compose_label_with_inlay(
    job: ProofJob, preview_path: str, opacity_percent: int | None = None,
    base_image: Image.Image | None = None,
) -> Image.Image:
    """Return an on-screen engineering preview with the selected inlay overlaid.

    The original BarTender/manual preview file is never changed. The label image is
    treated as the full physical label area for X/Y placement scaling.
    """
    if base_image is None:
        with Image.open(preview_path) as source:
            base = source.convert("RGBA")
    else:
        base = base_image.convert("RGBA")
    label_w_mm = float(job.label_width) * MM_PER_INCH
    label_h_mm = float(job.label_height) * MM_PER_INCH
    inlay_w_mm, inlay_h_mm = effective_inlay_size(job)
    if label_w_mm <= 0 or label_h_mm <= 0 or inlay_w_mm <= 0 or inlay_h_mm <= 0:
        return base

    width_px = round(base.width * inlay_w_mm / label_w_mm)
    height_px = round(base.height * inlay_h_mm / label_h_mm)
    opacity = int(job.inlay_preview_opacity if opacity_percent is None else opacity_percent)
    overlay = _prepare_inlay_rgba(job, width_px, height_px, opacity)
    if overlay is None:
        return base

    x_px = round(base.width * float(job.inlay_x_mm) / label_w_mm)
    y_px = round(base.height * float(job.inlay_y_mm) / label_h_mm)
    layer = Image.new("RGBA", base.size, (255, 255, 255, 0))
    layer.alpha_composite(overlay, (x_px, y_px))
    return Image.alpha_composite(base, layer)


def generate_construction_diagram(job: ProofJob, output_path: str, size=(1100, 600)) -> str:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    margin = 70
    header_h = 58
    label_w_mm = max(float(job.label_width) * MM_PER_INCH, 1.0)
    label_h_mm = max(float(job.label_height) * MM_PER_INCH, 1.0)
    avail_w = size[0] - 2 * margin
    avail_h = size[1] - 2 * margin - header_h
    scale = min(avail_w / label_w_mm, avail_h / label_h_mm)

    x0 = (size[0] - label_w_mm * scale) / 2
    y0 = header_h + (avail_h - label_h_mm * scale) / 2 + margin / 2
    x1 = x0 + label_w_mm * scale
    y1 = y0 + label_h_mm * scale

    draw.text((margin, 20), "RFID CONSTRUCTION DIAGRAM - NOT PRINT ART", fill="black", font=font)
    draw.rectangle((x0, y0, x1, y1), outline="black", width=4)
    draw.text((x0, max(y0 - 20, 36)), f"Label: {label_w_mm:.1f} x {label_h_mm:.1f} mm", fill="black", font=font)

    inlay_w_mm, inlay_h_mm = effective_inlay_size(job)
    if inlay_w_mm > 0 and inlay_h_mm > 0:
        ix0 = x0 + float(job.inlay_x_mm) * scale
        iy0 = y0 + float(job.inlay_y_mm) * scale
        ix1 = ix0 + inlay_w_mm * scale
        iy1 = iy0 + inlay_h_mm * scale

        inlay = _prepare_inlay_rgba(
            job,
            max(1, round(inlay_w_mm * scale)),
            max(1, round(inlay_h_mm * scale)),
            max(70, int(job.inlay_preview_opacity)),
        )
        if inlay is not None:
            rgba = img.convert("RGBA")
            rgba.alpha_composite(inlay, (round(ix0), round(iy0)))
            img = rgba.convert("RGB")
            draw = ImageDraw.Draw(img)
            draw.rectangle((ix0, iy0, ix1, iy1), outline=(90, 90, 90), width=3)
        else:
            draw.rectangle((ix0, iy0, ix1, iy1), outline=(180, 70, 20), width=5)
            cy = (iy0 + iy1) / 2
            draw.line((ix0 + 8, cy, ix1 - 8, cy), fill=(180, 70, 20), width=3)
            cx = (ix0 + ix1) / 2
            chip_r = max(4, min(10, int(scale * 0.9)))
            draw.rectangle((cx-chip_r, cy-chip_r, cx+chip_r, cy+chip_r), fill=(180, 70, 20))

        caption_y = min(y1 + 12, size[1] - 38)
        image_note = " | Image configured" if resolve_inlay_image(job.inlay_image_path) else " | Generic outline"
        draw.text(
            (x0, caption_y),
            f"{job.inlay or 'Inlay'} | {job.chip or 'Chip not specified'} | "
            f"X {job.inlay_x_mm:.1f} mm | Y {job.inlay_y_mm:.1f} mm | "
            f"Rotation {job.inlay_rotation} deg{image_note}",
            fill="black",
            font=font,
        )
    else:
        draw.text((x0 + 20, y0 + 20), "Inlay dimensions not configured in the Inlay Manager.", fill=(150, 50, 30), font=font)

    img.save(output, quality=95)
    return str(output)
