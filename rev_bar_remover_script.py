"""
Revision Bar Remover (batch)
============================

This script:

1. Opens a folder-selection window so you can pick a folder.
2. Finds every PDF directly inside that folder.
3. Asks you to confirm: remove the revision bars, or only create a
   preview with red boxes.
4. Creates a folder named "Rev Bars Removed" inside the selected
   folder, at the same level as the PDFs.
5. For each PDF, saves a copy there with the revision bars removed,
   under the same file name as the original. (Preview copies get a
   "_RevBarPreview" ending so they are never mistaken for cleaned
   files.) The original PDFs are never modified.
6. Detects revision bars: thick (about 2.8 pt), solid, dark, straight
   vertical lines located in the left-margin zone of the displayed page.
   Works on portrait and rotated (landscape) pages.
7. Chooses the bar zones for each file from its name: a file whose name
   contains "WBM" uses the WBM zones, every other file uses the
   default (FM) zones. Mixed folders are fine.
8. Detection reads the drawing commands in each page's content stream
   and measures each bar itself, so the result does not depend on the
   installed PyMuPDF version.
9. Removal edits ONLY the drawing commands of the bars. Nothing else
   on the page is rewritten, so logos, text, tables and images stay
   exactly as they were.
10. Verifies every result: re-scans the output for leftover bars and
    compares every page visually against the original outside the bar
    areas.
11. Prints a summary table for the whole batch.

Requirements:
    py -m pip install pymupdf pikepdf
"""

import os
from pathlib import Path

import fitz  # PyMuPDF
import pikepdf


# ============================================================
# FOLDER AND OUTPUT SETTINGS
# ============================================================

# Name of the output folder.
OUTPUT_FOLDER_NAME = "Rev Bars Removal script output"

# True:
#   The output folder is created INSIDE the selected folder, next to
#   the PDFs.
#
# False:
#   The output folder is created NEXT TO the selected folder (one
#   level up).
OUTPUT_FOLDER_INSIDE_SELECTED_FOLDER = True

# Final copies keep the exact name of the original PDF.
# Preview copies get this ending added to the name.
PREVIEW_SUFFIX = "_RevBarPreview"

# Open the output folder in Windows Explorer when finished.
OPEN_OUTPUT_FOLDER_WHEN_DONE = True


# ============================================================
# REVISION-BAR DETECTION SETTINGS
# ============================================================

# Horizontal zones (displayed page X-coordinates, in points) where
# revision bars may sit. A bar is accepted if its center X is inside
# any zone.
#
# Two sets of zones exist, chosen automatically for each file from
# its name:
#
#   - Default (FM and any other manual):
#       bars were found at X = 39 (even pages), X = 51 and X = 63
#       (odd pages).
#
#   - WBM (only if the file name contains the keyword below):
#       bars were found at X = 39 (even pages) and X = 75 (odd pages),
#       drifting between about X = 29 and X = 81 depending on the page.
#
# Add a second zone such as (540.0, 590.0) if bars can also appear
# in the right margin.
BAR_X_ZONES_DEFAULT = [
    (30.0, 70.0),
]

BAR_X_ZONES_WBM = [
    (25.0, 85.0),
]

# If this text appears in a PDF file name (not case sensitive),
# the WBM zones are used for that file.
WBM_FILENAME_KEYWORD = "WBM"

# Stroke thickness range of a revision bar, in points.
# Revision bars here are about 2.8 pt. Table borders and box outlines
# are about 0.7 to 1.7 pt and are ignored.
MIN_STROKE_WIDTH = 2.0
MAX_STROKE_WIDTH = 4.0

# Minimum length of a revision bar.
MIN_BAR_HEIGHT = 5.0

# A bar must be this close to perfectly vertical (horizontal drift
# allowed between its two end points, in points).
MAX_HORIZONTAL_DRIFT = 1.0

# A bar's color must be darker than this (0 = black, 1 = white).
MAX_BRIGHTNESS = 0.8

# Ignore candidates within this distance from the top / bottom.
TOP_EXCLUSION = 25.0
BOTTOM_EXCLUSION = 25.0

# Size of the red preview box padding.
PREVIEW_PADDING = 2.0


# ============================================================
# VERIFICATION SETTINGS
# ============================================================

# Compare every page against the original after removal.
VERIFY_OUTPUT = True

# Resolution used for the comparison render.
VERIFY_DPI = 60

# Area around each removed bar that is allowed to change (points).
VERIFY_MASK_PADDING = 4.0


# ============================================================
# OPERATION MODE AND DISPLAY SETTINGS
# ============================================================

# True:
#   A window asks you each run whether to remove the bars or only
#   create a preview.
#
# False:
#   No question is asked. PREVIEW_ONLY below decides.
ASK_MODE_WITH_WINDOW = True

# Used only when ASK_MODE_WITH_WINDOW is False.
#   True  = preview copies with red boxes (nothing removed)
#   False = final copies with the bars removed
PREVIEW_ONLY = False

# Print one line per page that contains bars.
PRINT_PAGE_DETAILS = False

# Print the position of every single bar (very long output).
PRINT_CANDIDATES = False


# ============================================================
# PER-FILE ZONE SELECTION
# ============================================================

# Zones used for the file currently being processed.
ACTIVE_ZONES = BAR_X_ZONES_DEFAULT


def zones_for_file(pdf_path):
    """
    Return the bar zones and the manual type for a PDF.
    Only the file name is checked, never the folder path.
    """

    if WBM_FILENAME_KEYWORD.lower() in pdf_path.name.lower():
        return BAR_X_ZONES_WBM, "WBM"

    return BAR_X_ZONES_DEFAULT, "Default (FM)"


# ============================================================
# COLOR HELPERS
# ============================================================

def brightness_from_gray(gray):
    return gray


def brightness_from_rgb(red, green, blue):
    return (red + green + blue) / 3.0


def brightness_from_cmyk(cyan, magenta, yellow, black):
    ink = (cyan + magenta + yellow) / 3.0
    return 1.0 - min(1.0, black + (1.0 - black) * ink)


def brightness_from_color_tuple(color):
    """
    Convert a PyMuPDF color tuple to a brightness value.
    Returns 0 (dark) when the color is unknown.
    """

    if color is None:
        return 0.0

    if len(color) == 1:
        return brightness_from_gray(color[0])

    if len(color) == 3:
        return brightness_from_rgb(*color)

    if len(color) == 4:
        return brightness_from_cmyk(*color)

    return 0.0


# ============================================================
# GEOMETRY HELPERS
# ============================================================

def is_in_bar_zone(center_x):
    """
    Return True if the X-coordinate is inside a revision-bar zone
    of the file currently being processed.
    """

    for zone_min, zone_max in ACTIVE_ZONES:
        if zone_min <= center_x <= zone_max:
            return True

    return False


def displayed_bar_matches(
    x_start,
    y_start,
    x_end,
    y_end,
    effective_width,
    page_height,
):
    """
    Decide whether a straight line, expressed in displayed page
    coordinates, is a revision bar.
    """

    if not (
        MIN_STROKE_WIDTH <= effective_width <= MAX_STROKE_WIDTH
    ):
        return False

    if abs(x_end - x_start) > MAX_HORIZONTAL_DRIFT:
        return False

    top = min(y_start, y_end)
    bottom = max(y_start, y_end)

    if bottom - top < MIN_BAR_HEIGHT:
        return False

    if top < TOP_EXCLUSION:
        return False

    if bottom > page_height - BOTTOM_EXCLUSION:
        return False

    center_x = (x_start + x_end) / 2.0

    return is_in_bar_zone(center_x)


def rectangles_overlap_or_touch(rect_a, rect_b, tolerance=1.0):
    """
    Determine whether two rectangles overlap or are close
    enough to be treated as one revision bar.
    """

    horizontal_overlap = not (
        rect_a.x1 < rect_b.x0 - tolerance
        or rect_b.x1 < rect_a.x0 - tolerance
    )

    vertical_overlap = not (
        rect_a.y1 < rect_b.y0 - tolerance
        or rect_b.y1 < rect_a.y0 - tolerance
    )

    return horizontal_overlap and vertical_overlap


def merge_rectangles(rectangles):
    """
    Merge adjoining rectangles so one revision bar stored as
    several segments is reported once.
    """

    merged = list(rectangles)

    changes_found = True

    while changes_found:
        changes_found = False
        new_merged_list = []

        while merged:
            current_rectangle = merged.pop(0)
            index = 0

            while index < len(merged):
                if rectangles_overlap_or_touch(
                    current_rectangle,
                    merged[index]
                ):
                    current_rectangle = (
                        current_rectangle | merged.pop(index)
                    )
                    changes_found = True
                else:
                    index += 1

            new_merged_list.append(current_rectangle)

        merged = new_merged_list

    return merged


def add_preview_box(page, rect):
    """
    Draw a red rectangle around a detected revision bar.
    The underlying PDF content is not removed.
    """

    padded = fitz.Rect(
        rect.x0 - PREVIEW_PADDING,
        rect.y0 - PREVIEW_PADDING,
        rect.x1 + PREVIEW_PADDING,
        rect.y1 + PREVIEW_PADDING,
    )

    shape = page.new_shape()

    shape.draw_rect(padded)

    shape.finish(
        color=(1, 0, 0),
        fill=None,
        width=1.0,
    )

    shape.commit(overlay=True)


def get_stroke_thickness(point_a, point_b, ctm, line_width):
    """
    Return the drawn thickness of a stroked line, measured across
    the line, taking the current transformation matrix into account.

    Some revision bars are drawn inside a stretched coordinate system
    (for example "1.18 0 0 10.15 cm"). Measuring the thickness across
    the line gives the true value in that case.

    If the measured thickness is outside the bar range but the stated
    line width is inside it, the stated line width is used.
    """

    determinant = abs(ctm.a * ctm.d - ctm.b * ctm.c)

    direction_x = point_b.x - point_a.x
    direction_y = point_b.y - point_a.y

    mapped_x = ctm.a * direction_x + ctm.c * direction_y
    mapped_y = ctm.b * direction_x + ctm.d * direction_y

    mapped_length = (mapped_x ** 2 + mapped_y ** 2) ** 0.5
    original_length = (
        direction_x ** 2 + direction_y ** 2
    ) ** 0.5

    if mapped_length == 0 or original_length == 0:
        return line_width

    thickness = (
        line_width * determinant * original_length / mapped_length
    )

    if MIN_STROKE_WIDTH <= thickness <= MAX_STROKE_WIDTH:
        return thickness

    return line_width


# ============================================================
# DETECTION (page content stream scan)
# ============================================================

PATH_BREAKING_OPERATORS = {"re", "c", "v", "y", "h"}

PAINT_OPERATORS = {
    "f", "F", "f*", "B", "B*", "b", "b*", "n",
}


def operand_number(operand):
    return float(operand)


def scan_page_for_bars(pdf_page, fitz_page):
    """
    Read the drawing commands of one page and find the revision bars.

    Returns:
        instructions        all parsed content-stream instructions
        indexes_to_remove   instruction indexes that draw the bars
        rectangles          displayed-page rectangle of each bar
    """

    to_displayed = (
        fitz_page.transformation_matrix
        * fitz_page.rotation_matrix
    )

    page_height = fitz_page.rect.height

    try:
        instructions = list(
            pikepdf.parse_content_stream(pdf_page)
        )

    except Exception as error:
        print(
            f"    WARNING page {fitz_page.number + 1}: "
            f"could not read the page content. Reason: {error}"
        )
        return [], set(), []

    indexes_to_remove = set()
    rectangles = []

    state_stack = []

    ctm = fitz.Matrix(1, 0, 0, 1, 0, 0)
    line_width = 1.0
    stroke_brightness = 0.0

    path_points = []
    path_indexes = []
    path_is_simple = True

    for index, instruction in enumerate(instructions):
        if isinstance(
            instruction,
            pikepdf.ContentStreamInlineImage
        ):
            continue

        operands = instruction.operands
        operator = str(instruction.operator)

        if operator == "q":
            state_stack.append(
                (ctm, line_width, stroke_brightness)
            )

        elif operator == "Q":
            if state_stack:
                (
                    ctm,
                    line_width,
                    stroke_brightness
                ) = state_stack.pop()

        elif operator == "cm" and len(operands) == 6:
            numbers = [operand_number(o) for o in operands]
            ctm = fitz.Matrix(*numbers) * ctm

        elif operator == "w" and len(operands) == 1:
            line_width = operand_number(operands[0])

        elif operator == "G" and len(operands) == 1:
            stroke_brightness = brightness_from_gray(
                operand_number(operands[0])
            )

        elif operator == "RG" and len(operands) == 3:
            stroke_brightness = brightness_from_rgb(
                *[operand_number(o) for o in operands]
            )

        elif operator == "K" and len(operands) == 4:
            stroke_brightness = brightness_from_cmyk(
                *[operand_number(o) for o in operands]
            )

        elif operator == "m" and len(operands) == 2:
            path_points = [
                (
                    operand_number(operands[0]),
                    operand_number(operands[1]),
                )
            ]
            path_indexes = [index]
            path_is_simple = True

        elif operator == "l" and len(operands) == 2:
            path_points.append(
                (
                    operand_number(operands[0]),
                    operand_number(operands[1]),
                )
            )
            path_indexes.append(index)

        elif operator in PATH_BREAKING_OPERATORS:
            path_is_simple = False

        elif operator in ("S", "s"):
            if (
                operator == "S"
                and path_is_simple
                and len(path_points) == 2
                and stroke_brightness <= MAX_BRIGHTNESS
            ):
                point_a = fitz.Point(*path_points[0])
                point_b = fitz.Point(*path_points[1])

                effective_width = get_stroke_thickness(
                    point_a,
                    point_b,
                    ctm,
                    line_width,
                )

                displayed_a = (point_a * ctm) * to_displayed
                displayed_b = (point_b * ctm) * to_displayed

                if displayed_bar_matches(
                    displayed_a.x,
                    displayed_a.y,
                    displayed_b.x,
                    displayed_b.y,
                    effective_width,
                    page_height,
                ):
                    indexes_to_remove.update(path_indexes)
                    indexes_to_remove.add(index)

                    half = effective_width / 2.0

                    rectangles.append(
                        fitz.Rect(
                            min(displayed_a.x, displayed_b.x) - half,
                            min(displayed_a.y, displayed_b.y),
                            max(displayed_a.x, displayed_b.x) + half,
                            max(displayed_a.y, displayed_b.y),
                        )
                    )

            path_points = []
            path_indexes = []
            path_is_simple = True

        elif operator in PAINT_OPERATORS:
            path_points = []
            path_indexes = []
            path_is_simple = True

    return instructions, indexes_to_remove, rectangles


def is_dashed(drawing):
    """
    Return True if a PyMuPDF drawing uses a dash pattern.
    """

    dashes = drawing.get("dashes")

    if not dashes:
        return False

    return dashes.strip() not in ("[] 0", "[] 0.0", "")


def find_bars_seen_by_pymupdf(page):
    """
    Independent cross-check. Find thick vertical solid lines that
    PyMuPDF can see on the page, including those inside form objects.
    The width limits are relaxed because PyMuPDF versions report
    stretched line widths differently.

    Returns the list of displayed-page rectangles.
    """

    rotation = page.rotation_matrix
    page_height = page.rect.height
    rectangles = []

    try:
        drawings = page.get_drawings()

    except Exception:
        return rectangles

    for drawing in drawings:
        if drawing.get("fill") is not None:
            continue

        if drawing.get("color") is None:
            continue

        if is_dashed(drawing):
            continue

        if brightness_from_color_tuple(
            drawing.get("color")
        ) > MAX_BRIGHTNESS:
            continue

        items = drawing.get("items", [])

        if len(items) != 1 or items[0][0] != "l":
            continue

        width = drawing.get("width") or 0.0

        if not (1.5 <= width <= 30.0):
            continue

        start = fitz.Point(items[0][1]) * rotation
        end = fitz.Point(items[0][2]) * rotation

        if displayed_bar_matches(
            start.x,
            start.y,
            end.x,
            end.y,
            MIN_STROKE_WIDTH,
            page_height,
        ):
            rectangles.append(
                fitz.Rect(
                    min(start.x, end.x) - 1.0,
                    min(start.y, end.y),
                    max(start.x, end.x) + 1.0,
                    max(start.y, end.y),
                )
            )

    return rectangles


# ============================================================
# REMOVAL (pikepdf content-stream editing)
# ============================================================

def remove_bars_from_page(pdf, pdf_page, instructions, indexes):
    """
    Rewrite one page's content stream without the bar commands.
    Every other command is kept exactly.
    """

    kept_instructions = [
        instruction
        for index, instruction in enumerate(instructions)
        if index not in indexes
    ]

    new_stream = pikepdf.unparse_content_stream(kept_instructions)

    pdf_page.Contents = pdf.make_stream(new_stream)


# ============================================================
# VERIFICATION
# ============================================================

def render_gray(page):
    pixmap = page.get_pixmap(
        dpi=VERIFY_DPI,
        colorspace=fitz.csGRAY,
        alpha=False,
    )

    return pixmap


def count_changed_pixels_outside_masks(
    pixmap_a,
    pixmap_b,
    masks,
):
    """
    Count pixels that differ between two renders, ignoring the
    mask rectangles (given in pixel coordinates).
    """

    if (
        pixmap_a.width != pixmap_b.width
        or pixmap_a.height != pixmap_b.height
    ):
        return -1

    width = pixmap_a.width
    height = pixmap_a.height
    samples_a = pixmap_a.samples
    samples_b = pixmap_b.samples

    changed = 0

    for y in range(height):
        start = y * width
        end = start + width

        if samples_a[start:end] == samples_b[start:end]:
            continue

        for x in range(width):
            if samples_a[start + x] == samples_b[start + x]:
                continue

            inside_mask = False

            for mask in masks:
                if (
                    mask.x0 <= x <= mask.x1
                    and mask.y0 <= y <= mask.y1
                ):
                    inside_mask = True
                    break

            if not inside_mask:
                changed += 1

    return changed


def verify_output(source_path, output_path, bars_by_page):
    """
    Re-scan the output for leftover bars (page content and
    PyMuPDF) and compare every page visually with the original
    outside the removed-bar areas.

    Returns a list of problem messages (empty if all is well).
    """

    problems = []

    original = fitz.open(source_path)
    result = fitz.open(output_path)
    result_pdf = pikepdf.open(output_path)

    scale = VERIFY_DPI / 72.0

    try:
        for page_index in range(original.page_count):
            original_page = original[page_index]
            result_page = result[page_index]

            _, _, leftovers = scan_page_for_bars(
                result_pdf.pages[page_index],
                result_page,
            )

            seen_by_pymupdf = find_bars_seen_by_pymupdf(result_page)

            if leftovers or seen_by_pymupdf:
                problems.append(
                    f"page {page_index + 1}: revision bar(s) still "
                    f"present (content scan: {len(leftovers)}, "
                    f"PyMuPDF: {len(seen_by_pymupdf)})"
                )

            masks = []

            for rect in bars_by_page.get(page_index, []):
                padded = fitz.Rect(
                    rect.x0 - VERIFY_MASK_PADDING,
                    rect.y0 - VERIFY_MASK_PADDING,
                    rect.x1 + VERIFY_MASK_PADDING,
                    rect.y1 + VERIFY_MASK_PADDING,
                )

                masks.append(
                    fitz.Rect(
                        padded.x0 * scale,
                        padded.y0 * scale,
                        padded.x1 * scale,
                        padded.y1 * scale,
                    )
                )

            changed = count_changed_pixels_outside_masks(
                render_gray(original_page),
                render_gray(result_page),
                masks,
            )

            if changed != 0:
                problems.append(
                    f"page {page_index + 1}: content changed outside "
                    f"the revision bars ({changed} pixels)"
                )

    finally:
        original.close()
        result.close()
        result_pdf.close()

    return problems


# ============================================================
# PROCESSING ONE PDF
# ============================================================

def process_pdf(input_pdf, output_folder, preview_only):
    """
    Process one PDF and save its copy in the output folder.

    Returns a dictionary describing the result.
    """

    global ACTIVE_ZONES

    ACTIVE_ZONES, manual_type = zones_for_file(input_pdf)

    if preview_only:
        destination = (
            output_folder / f"{input_pdf.stem}{PREVIEW_SUFFIX}.pdf"
        )
    else:
        destination = output_folder / input_pdf.name

    result = {
        "name": input_pdf.name,
        "manual": manual_type,
        "pages_with_bars": 0,
        "bars": 0,
        "status": "OK",
        "messages": [],
        "destination": destination,
    }

    if input_pdf.resolve() == destination.resolve():
        raise ValueError(
            "The output PDF cannot overwrite the source PDF."
        )

    document = fitz.open(input_pdf)
    pdf = pikepdf.open(input_pdf)

    try:
        print(f"    Type: {manual_type}   Pages: {document.page_count}")

        scans_by_page = {}
        rectangles_by_page = {}

        for page_index, page in enumerate(document):
            instructions, indexes, rectangles = scan_page_for_bars(
                pdf.pages[page_index],
                page,
            )

            pymupdf_count = len(find_bars_seen_by_pymupdf(page))

            if pymupdf_count > len(rectangles):
                result["messages"].append(
                    f"page {page_index + 1}: PyMuPDF sees "
                    f"{pymupdf_count} bar(s) but only "
                    f"{len(rectangles)} were found in the page "
                    f"content (possibly inside a form object)"
                )

            if not rectangles:
                continue

            scans_by_page[page_index] = (instructions, indexes)
            rectangles_by_page[page_index] = rectangles
            result["bars"] += len(rectangles)

            merged = merge_rectangles(rectangles)

            if PRINT_PAGE_DETAILS:
                print(
                    f"    PDF page {page_index + 1:>4} | "
                    f"bars = {len(merged)}"
                )

            if PRINT_CANDIDATES:
                for number, rect in enumerate(merged, start=1):
                    print(
                        f"        Bar {number}: "
                        f"center X={(rect.x0 + rect.x1) / 2.0:.2f}, "
                        f"Y={rect.y0:.2f}-{rect.y1:.2f}, "
                        f"height={rect.height:.2f}"
                    )

        result["pages_with_bars"] = len(rectangles_by_page)

        if preview_only:
            for page_index, rectangles in rectangles_by_page.items():
                for rect in merge_rectangles(rectangles):
                    add_preview_box(document[page_index], rect)

            document.save(destination, deflate=True)

        else:
            for page_index, (
                instructions,
                indexes
            ) in scans_by_page.items():
                remove_bars_from_page(
                    pdf=pdf,
                    pdf_page=pdf.pages[page_index],
                    instructions=instructions,
                    indexes=indexes,
                )

            pdf.save(destination)

    finally:
        document.close()
        pdf.close()

    if not preview_only and VERIFY_OUTPUT:
        print("    Verifying output...")

        result["messages"].extend(
            verify_output(
                input_pdf,
                destination,
                rectangles_by_page,
            )
        )

    if result["messages"]:
        result["status"] = "CHECK"

    return result


# ============================================================
# INTERACTIVE WINDOWS
# ============================================================

def choose_folder():
    """
    Show a folder-selection window. Returns a Path, or None if the
    window was cancelled. Falls back to typing the path if the
    window cannot be shown.
    """

    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        selected = filedialog.askdirectory(
            title="Select the folder that contains the PDFs",
            parent=root,
        )

        root.destroy()

    except Exception:
        selected = input(
            "Type or paste the folder path "
            "(leave empty to cancel): "
        ).strip().strip('"')

    if not selected:
        return None

    return Path(selected)


def confirm_mode(folder, pdf_count, output_folder):
    """
    Ask what to do with the PDFs found.

    Returns:
        "final"    remove the revision bars
        "preview"  create previews with red boxes
        None       cancel
    """

    if not ASK_MODE_WITH_WINDOW:
        return "preview" if PREVIEW_ONLY else "final"

    message = (
        f"Found {pdf_count} PDF file(s) in:\n{folder}\n\n"
        f"Copies will be saved in:\n{output_folder}\n\n"
        f"YES = remove the revision bars\n"
        f"NO = preview only (red boxes, nothing removed)\n"
        f"CANCEL = exit"
    )

    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        answer = messagebox.askyesnocancel(
            "Revision Bar Remover",
            message,
            parent=root,
        )

        root.destroy()

    except Exception:
        print(message)

        typed = input(
            "Type R to remove, P for preview, "
            "or anything else to cancel: "
        ).strip().lower()

        answer = (
            True if typed == "r"
            else False if typed == "p"
            else None
        )

    if answer is True:
        return "final"

    if answer is False:
        return "preview"

    return None


def show_message(title, message):
    """
    Show an information window (and print the message).
    """

    print(message)

    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        messagebox.showinfo(title, message, parent=root)

        root.destroy()

    except Exception:
        pass


# ============================================================
# BATCH HELPERS
# ============================================================

def find_pdfs(folder):
    """
    Return the PDFs directly inside the folder (not in subfolders),
    skipping copies created by this script.
    """

    pdfs = []

    for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file():
            continue

        if path.suffix.lower() != ".pdf":
            continue

        if path.stem.endswith(PREVIEW_SUFFIX):
            continue

        pdfs.append(path)

    return pdfs


def get_output_folder(folder):
    if OUTPUT_FOLDER_INSIDE_SELECTED_FOLDER:
        return folder / OUTPUT_FOLDER_NAME

    return folder.parent / OUTPUT_FOLDER_NAME


def print_summary(results, preview_only):
    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)

    bars_label = "Bars found" if preview_only else "Bars removed"

    print(
        f"{'File':<38} {'Type':<8} {'Pages':>6} "
        f"{bars_label:>12}  Status"
    )
    print("-" * 72)

    for result in results:
        name = result["name"]

        if len(name) > 37:
            name = name[:34] + "..."

        manual = "WBM" if result["manual"] == "WBM" else "FM"

        print(
            f"{name:<38} {manual:<8} "
            f"{result['pages_with_bars']:>6} "
            f"{result['bars']:>12}  {result['status']}"
        )

    print("-" * 72)

    for result in results:
        for message in result["messages"]:
            print(f"{result['name']} -> {message}")

    successes = sum(1 for r in results if r["status"] == "OK")
    checks = sum(1 for r in results if r["status"] == "CHECK")
    failures = sum(1 for r in results if r["status"] == "FAILED")

    print(
        f"Files processed: {len(results)}   "
        f"OK: {successes}   Need checking: {checks}   "
        f"Failed: {failures}"
    )

    if checks or failures:
        print(
            "Files marked CHECK or FAILED need attention. "
            "The messages above say why."
        )

    print("=" * 72)


# ============================================================
# MAIN PROCESS
# ============================================================

def main():
    print("=" * 72)
    print("REVISION BAR REMOVER (batch)")
    print("=" * 72)
    print(f"PyMuPDF version:   {fitz.VersionBind}")
    print(f"pikepdf version:   {pikepdf.__version__}")
    print("=" * 72)

    folder = choose_folder()

    if folder is None:
        print("No folder selected. Nothing was done.")
        return

    if not folder.is_dir():
        raise ValueError(
            f"The selected folder could not be found:\n{folder}"
        )

    if folder.name == OUTPUT_FOLDER_NAME:
        show_message(
            "Revision Bar Remover",
            f"You selected the output folder:\n{folder}\n\n"
            f"Select the folder that contains the original PDFs "
            f"instead.",
        )
        return

    pdfs = find_pdfs(folder)

    if not pdfs:
        show_message(
            "Revision Bar Remover",
            f"No PDF files were found directly in:\n{folder}",
        )
        return

    output_folder = get_output_folder(folder)

    mode = confirm_mode(folder, len(pdfs), output_folder)

    if mode is None:
        print("Cancelled. Nothing was done.")
        return

    preview_only = mode == "preview"

    output_folder.mkdir(parents=True, exist_ok=True)

    print(f"Selected folder:   {folder}")
    print(f"PDF files found:   {len(pdfs)}")
    print(f"Output folder:     {output_folder}")
    print(
        f"Operation:         "
        f"{'Preview' if preview_only else 'Final removal'}"
    )
    print("=" * 72)

    results = []

    for number, pdf_path in enumerate(pdfs, start=1):
        print(f"[{number}/{len(pdfs)}] {pdf_path.name}")

        try:
            result = process_pdf(
                pdf_path,
                output_folder,
                preview_only,
            )

            print(
                f"    Pages with bars: {result['pages_with_bars']}   "
                f"Bars: {result['bars']}   "
                f"Status: {result['status']}"
            )

        except Exception as error:
            result = {
                "name": pdf_path.name,
                "manual": zones_for_file(pdf_path)[1],
                "pages_with_bars": 0,
                "bars": 0,
                "status": "FAILED",
                "messages": [f"could not be processed: {error}"],
                "destination": None,
            }

            print(f"    FAILED: {error}")

        results.append(result)

    print_summary(results, preview_only)

    print(f"\nCopies saved in:\n{output_folder}")
    print("The original PDFs were not modified.")
    print("=" * 72)

    if OPEN_OUTPUT_FOLDER_WHEN_DONE and hasattr(os, "startfile"):
        try:
            os.startfile(str(output_folder))
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        print("\n" + "=" * 72)
        print("ERROR")
        print("=" * 72)
        print(error)
        print("=" * 72)

    finally:
        input("\nPress Enter to close...")