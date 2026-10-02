"""
Streamlit ChatGPT Slide Translator — Chrome (attach mode)

Install:
    python -m pip install -r requirements.txt

Run:
    1. Double-click start_chrome.bat and log in to ChatGPT by hand.
    2. python -m streamlit run app.py

Notes:
- Uses the ChatGPT WEB UI, not the OpenAI API.
- The app does NOT launch the browser. It attaches to a Chrome window you
  opened yourself with --remote-debugging-port=9222.
- Chrome needs its own profile folder (--user-data-dir) for this to work;
  your login is saved there, so you only log in once.
"""

from pathlib import Path
import re
import time
import shutil
import tempfile
from urllib.parse import urlparse

import streamlit as st
from playwright.sync_api import sync_playwright


CDP_URL = "http://localhost:9222"
IMAGE_TYPES = ["png", "jpg", "jpeg", "webp"]


st.set_page_config(
    page_title="ChatGPT Slide Translator",
    page_icon="📚",
    layout="wide",
)

st.title("📚 ChatGPT Slide Translator")
st.caption(
    "Sends your slides to ChatGPT one by one using a Chrome window "
    "you opened and logged into yourself."
)


# -------------------------------------------------------------------
# Session state
# -------------------------------------------------------------------

if "running" not in st.session_state:
    st.session_state.running = False


# -------------------------------------------------------------------
# Chrome connection
# -------------------------------------------------------------------

def connect_to_chrome(pw):
    """Attach to the Chrome window started with start_chrome.bat."""
    try:
        browser = pw.chromium.connect_over_cdp(CDP_URL)
    except Exception:
        raise RuntimeError(
            "Could not connect to Chrome. Start it with start_chrome.bat "
            "(close other Chrome windows using that profile first) and "
            "log in to ChatGPT."
        )

    for context in browser.contexts:
        for page in context.pages:
            if "chatgpt.com" in page.url:
                page.bring_to_front()
                return browser, page

    raise RuntimeError(
        "Connected to Chrome, but no ChatGPT tab was found. "
        "Open https://chatgpt.com/ in that Chrome window."
    )


# -------------------------------------------------------------------
# ChatGPT interaction
# -------------------------------------------------------------------

def attach_image(page, image_path):
    """Attach one image using ChatGPT's file input."""
    inputs = page.locator('input[type="file"]')

    if inputs.count() == 0:
        buttons = page.get_by_role(
            "button",
            name=re.compile(r"attach|upload|add files|plus", re.I),
        )

        if buttons.count():
            try:
                buttons.last.click()
                time.sleep(0.7)
            except Exception:
                pass

    inputs = page.locator('input[type="file"]')

    if inputs.count() == 0:
        raise RuntimeError(
            "Could not find ChatGPT's file-upload control. "
            "The ChatGPT website may have changed."
        )

    inputs.last.set_input_files(str(image_path))


def find_message_box(page):
    """Return the most likely ChatGPT message input."""
    textareas = page.locator("textarea")

    if textareas.count():
        for i in range(textareas.count() - 1, -1, -1):
            try:
                box = textareas.nth(i)
                if box.is_visible():
                    return box
            except Exception:
                pass

    editors = page.locator('[contenteditable="true"]')

    if editors.count():
        for i in range(editors.count() - 1, -1, -1):
            try:
                box = editors.nth(i)
                if box.is_visible():
                    return box
            except Exception:
                pass

    raise RuntimeError("Could not find ChatGPT's message box.")


def send_prompt(page, prompt):
    box = find_message_box(page)
    box.fill(prompt)
    box.press("Enter")


def is_generating(page):
    """Detect whether ChatGPT is currently generating."""
    try:
        stop_buttons = page.get_by_role(
            "button",
            name=re.compile(r"stop generating|stop", re.I),
        )

        for i in range(stop_buttons.count()):
            try:
                if stop_buttons.nth(i).is_visible():
                    return True
            except Exception:
                pass
    except Exception:
        pass

    return False


def image_signature(img):
    """A lightweight identifier for an image already in the page."""
    try:
        src = img.get_attribute("src") or ""
    except Exception:
        src = ""

    if src.startswith("data:"):
        # Data URLs can be huge; the length is enough for tracking.
        return ("data", len(src))

    return ("src", src)


def find_large_images(page):
    """Find visible images large enough to be slide-sized."""
    result = []
    images = page.locator("img")

    for i in range(images.count()):
        try:
            img = images.nth(i)

            if not img.is_visible():
                continue

            box = img.bounding_box()

            if not box:
                continue

            if box["width"] < 250 or box["height"] < 150:
                continue

            result.append(img)

        except Exception:
            continue

    return result


def get_image_signatures(page):
    return {image_signature(img) for img in find_large_images(page)}


def wait_for_generated_image(
    page,
    previous_image_signatures,
    timeout_seconds=240,
):
    """Wait until a new large image appears and generation has finished."""
    deadline = time.time() + timeout_seconds

    saw_generation = False
    saw_new_image = False
    stable_since = None

    while time.time() < deadline:
        generating = is_generating(page)

        current_images = find_large_images(page)

        new_images = [
            img
            for img in current_images
            if image_signature(img) not in previous_image_signatures
        ]

        if generating:
            saw_generation = True
            stable_since = None

        if new_images:
            saw_new_image = True

        # Complete only after a new image exists, generation has stopped,
        # and the UI has had a short settling period.
        if saw_new_image and not generating:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= 2.5:
                return new_images[-1]

        time.sleep(0.8)

    if saw_new_image:
        raise TimeoutError(
            f"A new image appeared, but ChatGPT did not reach a stable "
            f"finished state within {timeout_seconds} seconds."
        )

    if saw_generation:
        raise TimeoutError(
            f"ChatGPT generated for {timeout_seconds} seconds but no "
            f"new slide image was detected."
        )

    raise TimeoutError(
        f"No new generated slide image appeared within {timeout_seconds} seconds."
    )


def save_image_element(page, img, output_path):
    """Save the selected generated image at full resolution when possible."""
    src = img.get_attribute("src")

    if src and not src.startswith("data:"):
        try:
            parsed = urlparse(src)

            if parsed.scheme in ("http", "https"):
                response = page.request.get(src)

                if response.ok:
                    data = response.body()

                    if len(data) > 10_000:
                        output_path.write_bytes(data)
                        return "original image"
        except Exception:
            pass

    # Fallback: screenshot the rendered image.
    img.screenshot(path=str(output_path))
    return "screen capture"


def wait_until_chatgpt_ready(page, timeout_seconds=30):
    """Wait for the ChatGPT message box to become available."""
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        try:
            if find_message_box(page):
                return True
        except Exception:
            pass

        time.sleep(1)

    return False


# -------------------------------------------------------------------
# Sidebar
# -------------------------------------------------------------------

with st.sidebar:
    st.header("Settings")

    st.markdown(
        """
        **Workflow**

        1. Double-click **start_chrome.bat**.
        2. Log into ChatGPT in that Chrome window.
        3. Upload your slide images here.
        4. Enter your translation prompt.
        5. Click **Start**. Keep the ChatGPT tab visible and don't type in it.
        """
    )

    st.divider()

    timeout = st.number_input(
        "Maximum seconds per slide",
        min_value=30,
        max_value=900,
        value=300,
        step=30,
    )

    st.divider()

    if st.button("🔌 Test Chrome connection", use_container_width=True):
        try:
            with sync_playwright() as pw:
                _, test_page = connect_to_chrome(pw)

                if wait_until_chatgpt_ready(test_page, timeout_seconds=5):
                    st.success("Connected. ChatGPT is ready.")
                else:
                    st.warning(
                        "Connected to the ChatGPT tab, but the message box "
                        "wasn't found. Are you logged in?"
                    )
        except Exception as e:
            st.error(str(e))

    st.caption(f"Chrome debug address: {CDP_URL}")


# -------------------------------------------------------------------
# Main UI
# -------------------------------------------------------------------

st.subheader("1. Slides")

uploaded_files = st.file_uploader(
    "Upload your slide images",
    type=IMAGE_TYPES,
    accept_multiple_files=True,
    help="Upload all slides. They will be processed in filename order.",
)

if uploaded_files:
    uploaded_files = sorted(uploaded_files, key=lambda f: f.name.lower())

    st.success(f"{len(uploaded_files)} slide(s) ready.")

    with st.expander("Show slide list"):
        for i, file in enumerate(uploaded_files, 1):
            st.write(f"{i}. {file.name}")


st.subheader("2. Translation prompt")

default_prompt = """Translate this slide into the target language.

Use the context of the slide and the subject matter to produce natural language that a native speaker would actually use. Do NOT translate literally when that would sound unnatural.

Preserve the meaning, technical terminology, tone, and intended meaning of the original.

Preserve the original slide design, layout, diagrams, images, colors, and visual structure.

Return the completed translated slide as an image."""

prompt = st.text_area(
    "Your ChatGPT prompt",
    value=default_prompt,
    height=220,
    help="Paste your own prompt here if you already have one that gives you good results.",
)


st.subheader("3. Run")

start = st.button(
    "🚀 Start translating all slides",
    type="primary",
    disabled=not uploaded_files or st.session_state.running,
    use_container_width=True,
)


# -------------------------------------------------------------------
# Translation
# -------------------------------------------------------------------

if start:
    st.session_state.running = True

    try:
        base_dir = Path(tempfile.mkdtemp(prefix="chatgpt_slides_"))
        input_dir = base_dir / "input"
        output_dir = base_dir / "translated"

        input_dir.mkdir()
        output_dir.mkdir()

        local_files = []

        for uploaded in uploaded_files:
            path = input_dir / uploaded.name
            path.write_bytes(uploaded.getbuffer())
            local_files.append(path)

        completed = 0
        failed = 0

        # Connect fresh for this run. Leaving the "with" block only
        # disconnects; Chrome stays open and logged in.
        with sync_playwright() as pw:
            browser, page = connect_to_chrome(pw)

            if not wait_until_chatgpt_ready(page, timeout_seconds=15):
                raise RuntimeError(
                    "ChatGPT's message box was not found. Make sure you are "
                    "logged in and a chat is open."
                )

            progress = st.progress(0)
            status = st.empty()
            current_slide = st.empty()

            for index, image_path in enumerate(local_files):
                output_path = output_dir / f"{image_path.stem}_translated.png"

                current_slide.image(
                    str(image_path),
                    caption=(
                        f"Slide {index + 1}/{len(local_files)} — "
                        f"{image_path.name}"
                    ),
                    width="stretch",
                )

                try:
                    status.info(
                        f"Preparing slide {index + 1}/{len(local_files)}..."
                    )

                    # Images already on the page BEFORE upload; the
                    # generated image must be a new one.
                    before_upload = get_image_signatures(page)

                    attach_image(page, image_path)
                    time.sleep(1.5)

                    status.info(
                        f"Sending prompt for slide "
                        f"{index + 1}/{len(local_files)}..."
                    )

                    send_prompt(page, prompt)

                    status.info(
                        f"Waiting for the translated image "
                        f"({index + 1}/{len(local_files)})..."
                    )

                    generated_img = wait_for_generated_image(
                        page,
                        previous_image_signatures=before_upload,
                        timeout_seconds=int(timeout),
                    )

                    status.info(
                        f"Saving translated slide "
                        f"{index + 1}/{len(local_files)}..."
                    )

                    method = save_image_element(page, generated_img, output_path)

                    completed += 1
                    st.success(
                        f"✓ {image_path.name} → {output_path.name} ({method})"
                    )

                except Exception as exc:
                    failed += 1

                    error_path = output_dir / f"{image_path.stem}_ERROR.txt"
                    error_path.write_text(str(exc), encoding="utf-8")

                    st.error(f"✗ {image_path.name}: {exc}")

                progress.progress((index + 1) / len(local_files))

            status.empty()

        # Zip the results.
        zip_base = base_dir / "translated_slides"
        zip_path = Path(
            shutil.make_archive(str(zip_base), "zip", root_dir=output_dir)
        )

        st.divider()
        st.success(f"Finished. {completed} translated, {failed} failed.")

        st.download_button(
            "⬇️ Download all translated slides",
            data=zip_path.read_bytes(),
            file_name="translated_slides.zip",
            mime="application/zip",
            use_container_width=True,
        )

    except Exception as exc:
        st.error(f"Batch processing stopped: {exc}")

    finally:
        st.session_state.running = False