import base64
import mimetypes
import os


def file_to_data_uri(file_path: str) -> str:
    """Convert a local image file to a base64 data URI.

    Args:
        file_path: Path to the image file.

    Returns:
        A ``data:<mime>;base64,…`` string ready to be used with OpenAI APIs,
        or ``None`` if the file cannot be read.
    """
    if not os.path.isfile(file_path):
        print(f"File not found: {file_path}")
        return None

    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/png"  # safe default for unknown image types

    with open(file_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")

    return f"data:{mime_type};base64,{encoded}"


# Backward-compatible alias – existing callers that do
#   from xagent.utils.image_upload import upload_image
# will keep working without changes.
upload_image = file_to_data_uri


if __name__ == "__main__":
    # Example usage
    file_path = "tests/assets/test_image.png"
    data_uri = file_to_data_uri(file_path)
    if data_uri:
        print(f"Data URI generated ({len(data_uri)} chars)")
    else:
        print("Failed to generate data URI")