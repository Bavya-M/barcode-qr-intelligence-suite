import os
import io
import json
import numpy as np
import cv2
from PIL import Image
from flask import Flask, request, jsonify, render_template

app = Flask(__name__, template_folder='templates')

# Try importing pyzbar safely (handles missing system libzbar gracefully)
PYZBAR_AVAILABLE = False
try:
    from pyzbar import pyzbar
    PYZBAR_AVAILABLE = True
except Exception as e:
    PYZBAR_AVAILABLE = False
    print(f"Warning: pyzbar library could not be loaded: {e}")

def decode_barcodes(image_np):
    """
    Decodes 1D/2D barcodes from an OpenCV image buffer.
    Includes a multi-stage preprocessing fallback pipeline for enhanced detection.
    """
    if not PYZBAR_AVAILABLE:
        return None, "PyZBar native engine is not available on this server environment."

    # Stage 1: Direct decode on original color/gray image
    decoded_objects = pyzbar.decode(image_np)

    # Stage 2: Preprocessing fallback if no barcodes found
    if not decoded_objects:
        gray = cv2.cvtColor(image_np, cv2.COLOR_BGR2GRAY) if len(image_np.shape) == 3 else image_np
        decoded_objects = pyzbar.decode(gray)

        # Stage 3: Contrast enhancement (CLAHE) fallback
        if not decoded_objects:
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            enhanced = clahe.apply(gray)
            decoded_objects = pyzbar.decode(enhanced)

        # Stage 4: Adaptive thresholding fallback for low light / noisy barcodes
        if not decoded_objects:
            thresh = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
            )
            decoded_objects = pyzbar.decode(thresh)

    results = []
    for obj in decoded_objects:
        # Extract symbology format
        symbology = obj.type

        # Extract decoded payload text
        try:
            payload = obj.data.decode("utf-8")
        except UnicodeDecodeError:
            payload = obj.data.decode("latin-1", errors="replace")

        # Bounding box coordinates
        rect = obj.rect
        bbox = {
            "x": int(rect.left),
            "y": int(rect.top),
            "width": int(rect.width),
            "height": int(rect.height)
        }

        # Polygon points (for exact region outline)
        polygon = []
        if obj.polygon:
            polygon = [{"x": int(pt.x), "y": int(pt.y)} for pt in obj.polygon]

        results.append({
            "symbology": symbology,
            "payload": payload,
            "bounding_box": bbox,
            "polygon": polygon
        })

    return results, None

@app.route("/", methods=["GET"])
def index():
    """Serves the Web Single-Page Application interface."""
    return render_template("index.html")

@app.route("/api/scan", methods=["POST"])
def scan_barcode():
    """
    POST Endpoint: Accepts image payload via multipart/form-data.
    Returns structured JSON with status, symbology, payload, and coordinates.
    """
    if "image" not in request.files and "file" not in request.files:
        return jsonify({
            "status": "error",
            "message": "Missing image payload. Please send an image file under the 'image' or 'file' key."
        }), 400

    file = request.files.get("image") or request.files.get("file")

    if not file or file.filename == "":
        return jsonify({
            "status": "error",
            "message": "Empty file received. Please upload a valid image."
        }), 400

    try:
        # Read image file stream into NumPy array
        file_bytes = np.frombuffer(file.read(), np.uint8)
        image_np = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)

        if image_np is None:
            # Fallback PIL decode if OpenCV imdecode returns None
            file.seek(0)
            pil_img = Image.open(io.BytesIO(file.read())).convert("RGB")
            image_np = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

        img_height, img_width = image_np.shape[:2]

        # Decode barcodes
        results, err_msg = decode_barcodes(image_np)

        if err_msg:
            return jsonify({
                "status": "error",
                "message": err_msg
            }), 500

        if not results:
            return jsonify({
                "status": "no_barcode_found",
                "count": 0,
                "message": "No 1D or 2D barcodes detected in the image.",
                "image_dimensions": {"width": img_width, "height": img_height},
                "results": []
            }), 200

        return jsonify({
            "status": "success",
            "count": len(results),
            "message": f"Successfully decoded {len(results)} barcode(s).",
            "image_dimensions": {"width": img_width, "height": img_height},
            "results": results
        }), 200

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"An error occurred while processing the image: {str(e)}"
        }), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
